---
name: listening-audio-generator
description: 把 JSONL 听力脚本（instruction/dialogue/question）用 edge-tts 朗读并用 ffmpeg 补停顿、拼成一个听力 mp3（英语听力场景）
metadata:
  {
    "openclaw":
      {
        "emoji": "🎧",
        "requires": { "bins": ["ffmpeg", "ffprobe"] },
      },
  }
---

# 听力音频合成（JSONL → 听力 mp3）

本文件是**听力脚本 JSONL 格式的权威规范**。`workspaces/lesson/AGENTS.md` 工作流二"第4步"
明确写了"听力脚本格式和完整参数详见 `skills/listening-audio-generator/SKILL.md`，执行前必须阅读"，
所以生成 JSONL 之前先读完本文件，不要凭印象写字段。

## 何时使用

- 英语（或任何语言）学科出卷时，老师要求带听力部分 → 生成听力音频。
- 已经有一份听力脚本（对话/短文/题目），需要合成成考场可播放的一个 mp3。
- **不适用**：只想要一段文字朗读（那就直接 `edge-tts`）；把音频转格式、切分（本技能不做，请用 ffmpeg 命令）。

## 调用方式（契约固定，不要改名改参数）

```bash
python3 skills/listening-audio-generator/scripts/generate_audio.py \
  --input tmp/listening_script.jsonl \
  --output exams/{试卷名}_听力.mp3 \
  --speed {根据年级选择语速}
```

- **cwd = 工作区根目录**（线上是 `/srv/openclaw-teacher/workspaces/lesson`）。
- `--input` / `--output` 都写**相对工作区根**的路径；输入通常放 `tmp/`，产物必须放 `exams/`。
- 输出目录不存在会自动创建（含多级）。
- `--speed` 必填项按年级取值，见下面"语速建议表"。

## 输入格式：JSONL 听力脚本

**一行一个 JSON 对象**，UTF-8 编码，空行会被忽略。建议用 Python `json.dump(..., ensure_ascii=False)`
逐行写入 `tmp/listening_script.jsonl`，不要手写拼接字符串。

### 完整字段表

| 字段名 | 类型 | 必填 | 含义 | 示例 |
|---|---|---|---|---|
| `type` | string | ✅ | 行类型，只能是 `instruction` / `dialogue` / `question` / `text`。未知取值会报错并指出**行号** | `"dialogue"` |
| `text` | string | ✅ | 这一行要朗读的原文。不能是空字符串 | `"It opens at eight thirty."` |
| `speaker_id` | string | dialogue/text 必填，其余可选 | 说话人标识，命名规则 `{角色}_{male\|female}_{编号}`。不写时按类型取默认（题目/说明用 `speaker_female_1`） | `"speaker_male_1"` |
| `pause_after` | number | ⬜ | 这一段播完之后额外插入的**静音秒数**。答题时间设 **5.0~8.0**；上限 120 秒 | `6.0` |
| `repeat` | integer | ⬜ | 把这一段的朗读内容连播 N 遍（1~5，默认 1）。用于"每段对话播放两遍"的紧凑写法 | `2` |
| `lang` | string | ⬜ | 强制该行用中文/英文音色，取值 `en` / `zh`。不写则自动判断（键名前缀 + 文本是否含汉字） | `"en"` |

字段以外的键会被忽略（可以放 `note`、`question_no` 之类的备注给老师看）。

### 三种 `type` 的语义

| type | 用途 | 默认音色（英语） | 默认说话人 |
|---|---|---|---|
| `instruction` | 考试说明、转场提示（如开头说明、"You will hear it again."） | `en-US-AriaNeural` | `speaker_female_1` |
| `dialogue` | 对话的一轮发言 / 短文正文 | 按 `speaker_id` 性别分配（男 `en-US-GuyNeural`） | 必须显式写 `speaker_id` |
| `question` | 朗读题干（答题时间写在 `pause_after`） | `en-US-AriaNeural` | `speaker_female_1` |
| `text` | 与 `dialogue` 等价（纯朗读，需写 `speaker_id`） | 同 `dialogue` | 必须显式写 `speaker_id` |

### 结构要求（AGENTS.md 钉死的三条）

1. 开头用 `instruction` 播报听力考试说明。
2. 每段对话/短文**播放两遍**，中间用 `instruction` 播 "You will hear it again."。
3. 对话结束后用 `question` 朗读题目，`pause_after` 设 **5.0~8.0 秒**作为答题时间。

### 写法 A 与写法 B（两种都能正常工作）

**写法 A：显式写两遍 + 中间插`instruction`**（AGENTS.md 的原文写法，推荐，也最易懂）

```json
{"type":"instruction","text":"Part One. Listen to the following conversation.","speaker_id":"speaker_female_1","pause_after":2.0}
{"type":"dialogue","text":"Hi Tom. What time does the school library open?","speaker_id":"speaker_female_1"}
{"type":"dialogue","text":"It opens at eight thirty in the morning.","speaker_id":"speaker_male_1","pause_after":1.0}
{"type":"instruction","text":"You will hear it again.","speaker_id":"speaker_female_1","pause_after":1.0}
{"type":"dialogue","text":"Hi Tom. What time does the school library open?","speaker_id":"speaker_female_1"}
{"type":"dialogue","text":"It opens at eight thirty in the morning.","speaker_id":"speaker_male_1","pause_after":1.0}
{"type":"question","text":"Question one. What time does the school library open?","speaker_id":"speaker_female_1","pause_after":6.0}
```

**写法 B：用 `repeat: 2` 让脚本自动播两遍**（省一半行，适合长对话）

```json
{"type":"instruction","text":"Part One. Listen to the following conversation.","speaker_id":"speaker_female_1","pause_after":2.0}
{"type":"dialogue","text":"Hi Tom. What time does the school library open?","speaker_id":"speaker_female_1","repeat":2}
{"type":"dialogue","text":"It opens at eight thirty in the morning.","speaker_id":"speaker_male_1","repeat":2,"pause_after":1.0}
{"type":"question","text":"Question one. What time does the school library open?","speaker_id":"speaker_female_1","pause_after":6.0}
```

`repeat` 的两个细节（已实测）：

- 只重复**朗读内容**，行内 `pause_after` 只在这一行的**最后一遍之后**生效一次。
  所以写法 B 不会出现"两遍之间也停 6 秒"的听感事故——答题静音仍然只在最后。
- 写法 B **不插** "You will hear it again."。若要保留这句提示，在对话两遍之间单独写一行 `instruction`
  （即写法 B 与 `instruction` 混用：对话 `repeat:2`、前面另加一行 "You will hear it again." 亦可）。

## 参数表

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--input` / `-i` | ✅ | — | 输入 JSONL 路径（相对工作区根） |
| `--output` / `-o` | ✅ | — | 输出 mp3 路径（相对工作区根，如 `exams/期中英语_听力.mp3`）；目录自动创建 |
| `--speed` | ✅（契约写法） | `1.0` | 语速倍数，允许 `0.5`~`1.5`。越界中文报错并退出码 2 |
| `--rate` | ⬜ | — | 直接给 edge-tts 的 rate 字符串（`-10%`）。与 `--speed` 二选一，`--rate` 优先；越界同样报错 |
| `--lang` | ⬜ | 自动 | `en` / `zh`，强制全部段落用该语言音色 |
| `--voice-map` | ⬜ | 内置表 | 音色映射 JSON 文件路径，**增量覆盖**内置 `speaker_id → 音色` 表 |
| `--sample-rate` | ⬜ | `24000` | 输出采样率（edge-tts 原生 24kHz，不建议改） |
| `--bitrate` | ⬜ | `64k` | mp3 码率，可用 64k~128k |
| `--retries` | ⬜ | `3` | 单段 TTS 失败后的重试次数，指数退避（1s、2s、4s） |
| `--dry-run` | ⬜ | 关 | 只校验 JSONL 并打印合成计划，**不联网、不调 ffmpeg、不产出文件**。写脚本后先用它自检 |
| `--keep-temp` | ⬜ | — | 把中间分段 mp3 保留到指定目录，用于排障 |
| `--allow-duration-mismatch` | ⬜ | 关 | 时长对账超差时仍算成功（默认超差即失败退出） |

`--help` 可用，参数与本文档一致。

## 音色映射表

### 内置默认表（`speaker_id → edge-tts 音色`）

英语听力（默认场景）：

| `speaker_id` | edge-tts 音色 | 建议用途 |
|---|---|---|
| `speaker_female_1` | `en-US-AriaNeural` | **播音员 / 题目 / 考试说明**；对话中的女声角色 |
| `speaker_female_2` | `en-US-JennyNeural` | 第二个女声角色/主播 |
| `speaker_female_3` | `en-GB-SoniaNeural` | 英音女声（避免与上面听感混淆） |
| `speaker_male_1` | `en-US-GuyNeural` | 对话男声角色 |
| `speaker_male_2` | `en-US-DavisNeural` | 第二个男声角色 |
| `speaker_male_3` | `en-GB-RyanNeural` | 英音男声 |

中文（中文听力/朗读场景，服务器已实测可列出这些音色）：

| `speaker_id` | edge-tts 音色 |
|---|---|
| `zh_speaker_female_1` | `zh-CN-XiaoxiaoNeural`（播音员/题目默认） |
| `zh_speaker_female_2` | `zh-CN-XiaoyiNeural` |
| `zh_speaker_female_3` | `zh-CN-XiaomoNeural` |
| `zh_speaker_male_1` | `zh-CN-YunxiNeural` |
| `zh_speaker_male_2` | `zh-CN-YunjianNeural` |
| `zh_speaker_male_3` | `zh-CN-YunxiaNeural` |

别名：`narrator` / `announcer` → `en-US-AriaNeural`；`播音员` → `zh-CN-XiaoxiaoNeural`。

### 未命中时的兜底规则

1. `speaker_id` 在表里 → 直接用表里的音色。
2. 不在表里但能看出性别（`_female_\d+` / `_male_\d+`）→ 该语言的题目音色（女）/ 对话音色（男）。
3. 都判断不了 → `--lang`（或自动推断）对应语言的默认对话音色。

英语听力**必须**落到英文音色：只要文本是英文（无汉字），自动推断就是 `en`；用 `zh_speaker_*`
这类前缀会被判为中文，写英文稿时不要用。

### 覆盖方式

```bash
# 临时换一个音色（--voice-map 是增量覆盖，不用重抄整张表）
echo '{"speaker_female_1":"en-US-JennyNeural"}' > tmp/voice_map.json
python3 skills/listening-audio-generator/scripts/generate_audio.py \
  --input tmp/listening_script.jsonl --output exams/期中英语_听力.mp3 \
  --speed 0.9 --voice-map tmp/voice_map.json
```

`--voice-map` 的 JSON 顶层必须是 `{"speaker_id": "音色名"}` 对象；以 `_` 开头的键（如 `_comment`）会被忽略。

## 说话人命名规则

- 格式：`{角色}_{性别}_{编号}`，性别只能是 `male` / `female`，编号从 `1` 起。
- **播音员、考试说明、题目（`instruction` / `question`）一律用 `speaker_female_1`。**
- 对话角色按性别分配不同编号：男声 `speaker_male_1`、`speaker_male_2`…；女声 `speaker_female_2`、
  `speaker_female_3`…（`speaker_female_1` 让给播音员，避免听感与播音员混在一起）。
- 中文脚本用 `zh_speaker_*` 前缀（例：`zh_speaker_male_1`）。
- 不符合格式的 `speaker_id`（如 `"Tom"`、`"A"`）会被拒绝并报错行号——这是故意的，逼脚本写规范。

## 语速建议表

`--speed` 与 edge-tts `rate` 的换算：`rate = round((speed - 1) * 100)`。

| 学段 | `--speed` | 换算出的 `rate` |
|---|---|---|
| 小学低年级（1~2 年级） | `0.75` | `-25%` |
| 小学高年级（3~6 年级） | `0.85` | `-15%` |
| 初中 | `0.9` | `-10%` |
| 高中 | `1.0` | `+0%` |

允许范围 `0.5`~`1.5`；越界直接中文报错、退出码 2（不会静默用 1.0 代替）。

## 停顿与拼接的实现方式（不要用朗读空格凑）

- `pause_after` 用 ffmpeg 的 `anullsrc` 合成**等长静音 mp3**。服务器实测可用的命令：

  ```bash
  ffmpeg -f lavfi -i anullsrc=r=24000:cl=mono -t 3 -c:a libmp3lame -b:a 64k -y pause.mp3
  ```

  （3 秒静音产出约 24620 字节，ffprobe 读到 3.048 秒 —— 实测数据。）

- 语音段与静音段用 ffmpeg **concat demuxer 统一重编码拼接**（服务器实测可用的命令）：

  ```bash
  ffmpeg -f concat -safe 0 -i list.txt -c:a libmp3lame -b:a 64k -ar 24000 -ac 1 -y joined.mp3
  ```

- **concat 清单格式是硬要求**：每一行都必须是 `file '<绝对路径>'`，即**必须有 `file ` 前缀和引号**：

  ```
  file '/tmp/seg_0000.mp3'
  file '/tmp/pause_0000.mp3'
  ```

  写裸路径（只有 `/tmp/seg_0000.mp3`）会让 ffmpeg 报 `Line 1: unknown keyword`，
  而且**退出码不一定是非零**、还留下 0 字节的输出文件。脚本因此不只看退出码：
  拼接后一律校验"文件存在 + 大小 > 0 + ffprobe 时长 ≈ 分段实测累加"，
  任一不通过就换另一种拼接方式重试，都失败则非零退出。

- 所以**不要**在 `text` 里写空格、`...`、换行来"制造停顿"——那样时长不可控，且会被朗读出来。

## 输出

- 产物路径 = `--output` 指定的路径（契约里放在 `exams/{试卷名}_听力.mp3`），父目录自动创建。
- 规格：mp3、单声道、24kHz（edge-tts 语音段原生就是 24000Hz/单声道/48kbps；
  拼接段用 libmp3lame 重编码，默认 64kbps，可用 `--bitrate` 调）。
- 成功时 stdout 打印一份 **JSON 摘要**，可直接读给老师看：

```json
{
  "行数": 12, "分段数": 15, "语速": 0.9, "rate": "-10%",
  "音频总时长": 96.482, "停顿总时长": 42.0,
  "对账实测总时长": 138.512, "对账预计总时长": 138.482, "对账偏差秒": 0.03,
  "输出文件": "/srv/openclaw-teacher/workspaces/lesson/exams/期中英语_听力.mp3",
  "输出文件大小": 851968, "输出文件大小可读": "832.00 KB",
  "采样率": 24000, "码率": "64k"
}
```

## 自检与退出码（脚本自己会做，不需要 agent 另写检查）

| 退出码 | 含义 |
|---|---|
| `0` | 成功（含 `--dry-run` 校验通过） |
| `1` | 运行期失败：缺 ffmpeg/ffprobe、TTS 反复失败、ffprobe 拿不到时长、**时长对账不通过** |
| `2` | 输入/参数错误：坏 JSON、缺字段、未知 type、语速越界等 |

自检内容：

1. **时长对账**：用 ffprobe 实测**每个分段**的时长并累加（比字符估算准），与输出文件实测时长比对，
   偏差超过 `max(0.75 秒, 总时长×3%)` 即判定失败、非零退出。
2. 输出文件不存在、0 字节、时长为 0 → 失败、非零退出（**不只看 ffmpeg 退出码**，
   实测存在"ffmpeg 报成功但产物 0 字节"的情况）。
3. 缺 ffmpeg / ffprobe → 中文报错 + 安装命令 + 退出码 1（**不产出任何半成品**）。
4. 单段 TTS 失败 → 指数退避重试（1s、2s、4s，默认 3 次），最终失败时指出**行号 + 文本前 60 字**。

## 失败排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `缺少系统命令：ffmpeg、ffprobe` | 宿主机没装 ffmpeg | `sudo dnf install -y epel-release && sudo dnf install -y ffmpeg ffmpeg-free`，然后 `ffmpeg -version` 确认 |
| `Python 无法导入 edge_tts` | 当前 `python3` 环境里没有 edge-tts | `python3 -c "import edge_tts; print(edge_tts.__file__)"`；用装了 edge-tts 的解释器调用本脚本（本技能不自行装包） |
| `第 N 行：不是合法 JSON` | 手写 JSON 有尾逗号/引号问题 | 用 `json.dumps` 逐行写；看报错里的行号与列号改那一行 |
| `第 N 行：未知的 type=...` | 类型拼错（如写了 `dialog`） | 只能是 `instruction`/`dialogue`/`question`/`text` |
| `第 N 行：dialogue 类型必须写 speaker_id` | 对话角色没标说话人 | 按命名规则补 `speaker_id` |
| `第 N 行：speaker_id=... 不符合命名规则` | 用了人名/字母 | 换成 `speaker_male_1` 这类规范 id，或在 `--voice-map` 里显式映射 |
| `第 N 行的语音合成连续失败 3 次` | 服务器不能访问 edge-tts 在线服务，或音色名不存在 | 先 `python3 -m edge_tts --list-voices \| head` 验证连通；确认音色名拼写；把该行文本拆短重试 |
| `时长对账不通过` | 拼接不完整 / 个别分段没合成出来 | 加 `--keep-temp tmp/audio_dbg` 保留分段，`ffprobe` 逐个看哪个是 0 秒；多数是某行文本异常（例如整行是标点） |
| ffmpeg 报 `Line 1: unknown keyword` 或产物 0 字节 | concat 清单格式不对（写了裸路径） | 清单每行必须是 `file '<绝对路径>'`。本脚本自己生成清单，正常不会踩；手工排查时注意这条 |
| 输出目录不可写 | `exams/` 权限问题 | 确认当前用户在 workspace 下可写；脚本会自动 `mkdir -p`，但改不了父目录权限 |
| 想确认脚本写对了但不想等合成 | — | 先跑 `--dry-run`，它只校验并打印计划（含每行的音色、语言、停顿、repeat 展开），不联网 |

## 依赖（服务器现状）

- **已装**：`edge-tts`（Python 包，用它朗读）、`requests`、`openpyxl`、`python-docx`、`PyMuPDF`。
- **需要系统包**：`ffmpeg` / `ffprobe` 7.0.2（EPOL 仓库，`libmp3lame` 可用）。脚本会做可用性检测，
  缺失时中文报错 + 安装命令 + 非零退出。
- **不需要**：numpy / pandas / PIL / pydub / librosa —— 本技能一概不用。
- Python 3.11；脚本用 `edge_tts.Communicate(text, voice, rate=...)` 的**流式 API**
  （`async for chunk in communicate.stream()` 取 `type == "audio"` 的分片自己写文件），
  用 `asyncio.run` 驱动，不用已废弃接口。服务器实测单条约 0.3 秒。

## 完整示例（从 0 到产物）

```bash
cd /srv/openclaw-teacher/workspaces/lesson

# 1) 用 python 逐行写 tmp/listening_script.jsonl（字段见上文）
# 2) 先自检 JSONL 与合成计划（不联网）
python3 skills/listening-audio-generator/scripts/generate_audio.py \
  --input tmp/listening_script.jsonl \
  --output exams/期中英语_听力.mp3 \
  --speed 0.9 --dry-run

# 3) 正式合成
python3 skills/listening-audio-generator/scripts/generate_audio.py \
  --input tmp/listening_script.jsonl \
  --output exams/期中英语_听力.mp3 \
  --speed 0.9

# 4) 确认产物
ls -lh exams/*听力.mp3
```

## 测试

`tests/test_skill_audio.py`（unittest）：单元测试**离线、确定性、不发网络请求**，
覆盖 JSONL 解析与行号报错、语速换算与越界、音色映射（中英文各一例）、`repeat` 展开、
时长估算、ffmpeg 命令构造。真实合成是集成测试，默认跳过：

```bash
# 服务器上手动跑一次真实合成（需要网络 + ffmpeg）
RUN_TTS_TEST=1 python3 -m unittest tests.test_skill_audio -v
```
