---
name: obs-share
description: 把教案、试卷、听力音频、成绩报告等产出文件上传到华为云 OBS 私有桶，生成带签名的下载链接（可发微信/手机打开）
metadata:
  {
    "openclaw":
      {
        "emoji": "🔗",
        "requires": { "bins": ["python3"] },
      },
  }
---

# 产出交付：生成可分享的下载链接（华为云 OBS）

把工作区里的产出文件上传到华为云 OBS **私有桶**，生成**预签名下载链接**给老师。
链接可以直接发到微信、短信、手机浏览器里点开；这是网页界面之外的第二条交付通道。

## 何时使用

**该用**（老师明确要"链接 / 地址 / 能发出去的文件"）：

- 老师索要下载链接，或说"发到微信 / 发给别人 / 在手机上打开 / 给我个地址"；
- 老师要分享的是**教案 docx、试卷 / 答案 docx、听力 mp3**、通知、会议纪要等；
- 老师人不在服务器旁边，拿不到工作区里的本地路径。

**不该用**：

- 老师只是要看文件内容 → 直接在对话里给结果，或告知**本地路径**（默认做法不变）；
- 老师要发给**家长**的成绩报告 → 走办公助手的原有流程（生成 Word + **一对一私发**），
  **不要主动建议上传到云**；只有老师主动索要链接并且明确同意后才做（见「安全闸门」）；
- **不要批量上传**整个 `lessons/`、`exams/`、`data/` 目录 —— 只传老师点名要的那几个文件；
- `uploads/` 下老师上传的**教材/教参**可能涉及版权，**默认不要上传**；确需上传时先向老师说明。

## 执行

cwd 必须是**工作区根目录**（如 `/srv/openclaw-teacher/workspaces/lesson` 或 `.../workspaces/office`）：

```bash
# 1) 只诊断：读凭据、探测桶、打印 endpoint / 区域 / ACL 是否私有 / 对象数（不上传）
python3 skills/obs-share/scripts/upload_share.py --check

# 2) 上传一个教案
python3 skills/obs-share/scripts/upload_share.py --file lessons/第一单元_教案.docx

# 3) 一次传试卷 + 答案，链接 7 天有效，用 JSON 取结果
python3 skills/obs-share/scripts/upload_share.py \
    --file exams/期中_试卷.docx --file exams/期中_答案.docx --expires 604800 --json

# 4) 成绩报告在 data/ 下：必须先征得老师明确同意，再加 --allow-sensitive
python3 skills/obs-share/scripts/upload_share.py \
    --file data/reports/张三-成绩报告.docx --allow-sensitive
```

## 完整参数表

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `--file <路径>` | 上传时必填 | 无 | 要上传的文件；**可重复传入**一次传多个。相对路径按 cwd（工作区根）解析；目录会被拒绝。 |
| `--expires <秒>` | 否 | `86400`（24 小时） | 链接有效期，取值 `1 ~ 604800`。越界会中文报错并退出码 2。 |
| `--prefix <前缀>` | 否 | `teacher-assistant` | 对象名前缀，可用 `/` 分层；最终对象名是 `<前缀>/<YYYY-MM-DD>/<8位随机十六进制>-<安全化文件名>`。 |
| `--json` | 否 | 关 | 结果以**单一合法 JSON** 打到 stdout；人类可读摘要一律走 **stderr**（本项目踩过坑：摘要混进 stdout 会让 `json.load` 失败）。 |
| `--check` | 否 | 关 | 只诊断：读凭据、`headBucket` 探测桶、打印 endpoint / 区域 / ACL 是否私有 / 对象数；**不上传、不产出链接**，不需要 `--file`。 |
| `--allow-sensitive` | 否 | 关 | 知情同意开关：允许上传 `data/` 下的学生数据。不加时被安全闸门拒绝（退出码 5）。见下文「安全闸门」。 |
| `--verify` / `--no-verify` | 否 | `--verify`（开） | 上传后**真的用预签名链接 GET 一次**，校验 HTTP 200、字节数与 sha256 是否和本地文件一致；`--no-verify` 只校验 `getObjectMetadata` 的字节数。 |
| `--help` | 否 | — | 打印参数说明并退出 0。 |

凭据相关环境变量（可选）：`OBS_ENV_FILE` 指定凭据文件路径（默认 `~/.openclaw/.env`）。

## 输出示例

非 `--json` 模式，人类可读摘要走 stdout：

```text
[进度] 正在上传 /srv/openclaw-teacher/workspaces/lesson/lessons/第一单元_教案.docx ...   ← stderr
[进度] 已上传并校验 teacher-assistant/2026-09-15/1a2b3c4d-第一单元_教案.docx              ← stderr
[完成] 已上传 1 个文件到 OBS 私有桶 openclaw-teacher（endpoint obs.cn-east-3.myhuaweicloud.com）

1. /srv/openclaw-teacher/workspaces/lesson/lessons/第一单元_教案.docx
   对象名：teacher-assistant/2026-09-15/1a2b3c4d-第一单元_教案.docx
   大小：48213 字节，sha256 9f2c1b7d4e0a5f38…
   下载链接（86400 秒内有效，截止 2026-09-16T10:20:30+08:00）：
   https://openclaw-teacher.obs.cn-east-3.myhuaweicloud.com/teacher-assistant/2026-09-15/1a2b3c4d-%E7%AC%AC%E4%B8%80%E5%8D%95%E5%85%83_%E6%95%99%E6%A1%88.docx?Expires=1789000000&AccessKeyId=XXXXXXXXXXXXXXXX&Signature=xxxx
   自校验：getObjectMetadata 字节数一致；预签名 GET 200，取回字节数与 sha256 均一致

提醒：链接带签名与有效期，过期后需要重新生成（重跑本脚本即可）；桶是私有的，去掉 ?Expires=…&AccessKeyId=…&Signature=… 的裸链接打不开（403，属预期行为）。
提醒：链接本身就是凭据（含 AccessKeyId 与 Signature），只发给该拿文件的老师，不要发到公开群里。
审计：已追加到 obs-uploads.jsonl
```

`--json` 模式，stdout **只有**下面这一个 JSON 文档（键已省略部分）：

```json
{
  "ok": true,
  "mode": "upload",
  "bucket": "openclaw-teacher",
  "endpoint": "obs.cn-east-3.myhuaweicloud.com",
  "region": "cn-east-3",
  "prefix": "teacher-assistant",
  "expires": 86400,
  "expires_at": "2026-09-16T10:20:30+08:00",
  "credential_source": "凭据文件 /root/.openclaw/.env",
  "allow_sensitive": false,
  "sensitive_files": [],
  "verified": true,
  "audit_log": "obs-uploads.jsonl",
  "uploaded": [
    {
      "source": "第一单元_教案.docx",
      "source_path": "/srv/openclaw-teacher/workspaces/lesson/lessons/第一单元_教案.docx",
      "object_key": "teacher-assistant/2026-09-15/1a2b3c4d-第一单元_教案.docx",
      "bucket": "openclaw-teacher",
      "endpoint": "obs.cn-east-3.myhuaweicloud.com",
      "bytes": 48213,
      "sha256": "9f2c1b7d4e0a5f38…",
      "expires": 86400,
      "expires_at": "2026-09-16T10:20:30+08:00",
      "sensitive": false,
      "url": "https://openclaw-teacher.obs.cn-east-3.myhuaweicloud.com/…?Expires=…&AccessKeyId=…&Signature=…",
      "bare_url": "https://openclaw-teacher.obs.cn-east-3.myhuaweicloud.com/teacher-assistant/2026-09-15/1a2b3c4d-第一单元_教案.docx",
      "verified": true,
      "verify": {"status": 200, "bytes": 48213, "sha256": "9f2c1b7d4e0a5f38…", "sha256_match": true}
    }
  ]
}
```

`--check` 输出示例（stdout）：

```text
[诊断] OBS 连接诊断（--check 只诊断，不上传、不生成链接）
  凭据来源：凭据文件 /root/.openclaw/.env
  AK：24 字符（不显示值）  SK：40 字符（不显示值）
  桶：openclaw-teacher
  endpoint：obs.cn-east-3.myhuaweicloud.com
  区域：cn-east-3
  headBucket：200
  桶 ACL：私有（未给匿名开放）；未发现匿名组权限（授权条目 1 条，只有 owner/具名用户）
  对象数：0；第一页 0 个对象
  结论：凭据与桶都可用，可以上传。
```

**失败时 stdout 保持为空**（`--json` 模式下也不写半截 JSON），中文错误只在 stderr，程序用退出码判断。

## 凭据配置

键名（四个都要，缺任何一个都会中文报错 + 退出码 3）：

| 键 | 值 |
|----|----|
| `OBS_ACCESS_KEY` | 华为云 AccessKeyId |
| `OBS_SECRET_KEY` | 华为云 SecretAccessKey |
| `OBS_BUCKET` | 桶名，本项目是 `openclaw-teacher` |
| `OBS_ENDPOINT` | `obs.<区域>.myhuaweicloud.com`，本项目是 `obs.cn-east-3.myhuaweicloud.com` |

读取顺序：**① 环境变量 → ② 凭据文件 `~/.openclaw/.env`**（逐键判断，环境变量优先）。
可用 `OBS_ENV_FILE=/path/to/.env` 指向别的文件。

`~/.openclaw/.env` 写法（`KEY=VALUE`，支持 `#` 注释、`export ` 前缀、值两侧引号）：

```ini
# 华为云 OBS 凭据（只放服务器，绝不提交仓库、绝不贴进聊天）
OBS_ACCESS_KEY=XXXXXXXXXXXXXXXXXXXX
OBS_SECRET_KEY=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
OBS_BUCKET=openclaw-teacher
OBS_ENDPOINT=obs.cn-east-3.myhuaweicloud.com
```

```bash
mkdir -p ~/.openclaw && chmod 700 ~/.openclaw
cat >> ~/.openclaw/.env <<'EOF'
OBS_ACCESS_KEY=<你的AccessKeyId>
OBS_SECRET_KEY=<你的SecretAccessKey>
OBS_BUCKET=openclaw-teacher
OBS_ENDPOINT=obs.cn-east-3.myhuaweicloud.com
EOF
chmod 600 ~/.openclaw/.env
```

安全约定：

- **凭据绝不写进仓库任何文件**（脚本里没有、SKILL.md 里也没有），也不写进日志与审计；
- 脚本**任何输出都不打印 AK/SK 的值**，诊断里最多打印**长度**（`AK：24 字符`）；
- 链接里出现的 `AccessKeyId=…` 是签名的一部分（AK 的标识，不是 SK），它随链接本身外发，属预期。

## 区域不一致的排查（踩过的坑，务必先看这一节）

**实测事实：桶 `openclaw-teacher` 的实际区域是 `cn-east-3`（华东-上海一）**，
对应 endpoint `obs.cn-east-3.myhuaweicloud.com`。
把它当成别的区域（例如 `cn-north-4`）查，会得到**误导性的结果**：

| 现象 | 真实含义 | 处理 |
|------|----------|------|
| `headBucket` 返回 **404**（`NoSuchBucket`） | **多半是区域写错**，不代表桶不存在。 | 跑 `upload_share.py --check`：脚本会用 `listBuckets()` 找到同名桶的 `location`，直接报「桶实际在 `<region>`，请把 `OBS_ENDPOINT` 设为 `obs.<region>.myhuaweicloud.com`」。 |
| `createBucket` 返回 **409 `BucketAlreadyOwnedByYou`** | 桶**已经存在**，只是在**另一个区域**（该账号下同名桶已归你所有）。 | 不要据此以为"桶不存在"或"可以重建"；把 `OBS_ENDPOINT` 改到桶所在区域即可。 |
| `listBuckets()` 里能看到桶但 `headBucket` 一直 404 | 同上，区域不一致。 | 以 `listBuckets` 返回的 `location` 为准改 endpoint。 |

自己排查的步骤：

1. `python3 skills/obs-share/scripts/upload_share.py --check` —— 看它打印的 `endpoint`、`区域`、`headBucket`；
2. 报错里若出现「请把 OBS_ENDPOINT 设为：`obs.<region>.myhuaweicloud.com`」，就照抄那个地址改
   `~/.openclaw/.env` 里的 `OBS_ENDPOINT`（或 export 环境变量）；
3. 再跑一次 `--check`，`headBucket` 应为 `200`；
4. **区域写错时不要用 `createBucket` 去"修"** —— 会得到 409，桶不会因此变到你的区域；
5. 该账号下还有别的项目的桶（`anent-interview`、`internet-hospital-a371`、`xiao-yang` 等），
   **绝对不要读写**；本技能只碰 `OBS_BUCKET` 指定的那一个桶。

## 有效期：上限与选择建议

- **上限 604800 秒（7 天）** —— 超过会中文报错、退出码 2。
  **这是本项目自设的策略上限，不是 OBS 的限制**：实测（2026-09-16，本桶）`expires=604800`、`604801`、`2592000`（30 天）签出来的链接，OBS 服务端**都返回 200**，技术上可以签得更长。之所以卡在 7 天：链接一旦转发出去，**持链接者**就能在有效期内下载，有效期越长、泄露面越大。要更长的可用期，正确做法是到期后重新生成链接，而不是签一张超长链接。
- 默认 **86400 秒（24 小时）**，适合"老师马上要发出去、对方当天就下载"；
- 选择建议：

  | 场景 | 建议 `--expires` |
  |------|------------------|
  | 发给老师本人、当场下载 | `3600`（1 小时）或默认 24 小时 |
  | 发微信给同事，对方可能隔天再点 | `86400`（默认） |
  | 发给家长/多人，需要留出缓冲 | `259200`（3 天）~ `604800`（7 天） |
  | 长期归档、要反复下载 | **不要靠超长链接**：到期后重新跑一次脚本生成新链接即可 |

- **链接过期 = 403**，属于正常现象，不是故障：重新执行上传即可拿到新链接（对象名带随机后缀，会生成一个新对象）。
- 链接**带签名**：改动链接里的 `Expires`/`Signature` 任意一个字符都会变成 403（防篡改）。

## 私有桶：裸链接打不开是预期行为

- 桶是**私有**的（ACL 只有 owner 的 FULL_CONTROL）；
- **未签名的直链（去掉 `?Expires=…&AccessKeyId=…&Signature=…`）返回 403，是设计如此，不是故障**，
  不要去"修"它，也不要为了"能直接点开"把桶改成公共读 —— 那会让任何拿到对象名的人都能下载；
- `--check` 会顺便打印桶 ACL 是否私有：若不是私有，会给出 `[警告]` 并以退出码 5 结束（安全闸门）。

## 安全闸门与 `--allow-sensitive`

**红线原文**（`workspaces/office/AGENTS.md` →「数据安全」）：

> 敏感信息（学生信息、家长联系方式、成绩）**仅在工作区内处理，不向外部泄露**。

**冲突**：把成绩报告上传到华为云 OBS，就是把学生数据送出本机、送进**第三方云**。
桶是私有的、链接是有签名的，只能降低风险，**改变不了"数据已经离开这台服务器"这个事实**。

**因此脚本里有硬闸门**：当 `--file` 的路径落在**当前工作区根的 `data/` 目录**下
（例如 `data/reports/张三-成绩报告.docx`）：

- **不加 `--allow-sensitive`** → 中文报错 + **退出码 5**；
  报错里会说明冲突、说明"这是把学生数据上传到第三方云"、并告诉老师/管理员加 `--allow-sensitive`；
  **此时脚本不会读取凭据、不会上传任何字节**；
- **加了 `--allow-sensitive`** → 允许上传，并在输出里打印醒目提示：

  ```text
  [警告] --allow-sensitive 已开启：本次会把 1 个位于 data/ 下的学生数据文件上传到第三方云（华为云 OBS）。
  [警告] 这些文件离开了本机，请确认你已获得授权、并且知道链接该发给谁。
  ```

- 审计日志里会记 `"sensitive": true`、`"allow_sensitive": true`，留痕。

**「知情同意」的含义**：
`--allow-sensitive` 不是"解锁开关"，而是**老师本人的明确决定**：

1. 助手**先问老师**："这会把学生成绩数据上传到华为云（第三方云），确认要这样做吗？"
2. 老师**明确同意后**，才加 `--allow-sensitive` 执行；
3. 老师**没有明确同意 → 不上传**，改为给本地路径，或走原有的一对一私发流程；
4. 同意意味着：**老师本人**对这次把学生数据上传到第三方云负责，
   并已确认这样做符合学校/主管部门的要求（例如已获授权、已做脱敏）；
5. 默认做法不变：**不上传**，红线不变；老师要"给家长发报告"时**默认走生成 Word + 一对一私发**，
   **不要主动建议上传到云**。

**只上传老师点名的那一个文件**，绝不批量上传 `data/` 目录。

## 审计日志：`obs-uploads.jsonl`

- 位置：**当前工作区根目录**下的 `obs-uploads.jsonl`（cwd 是工作区根，所以 lesson 与 office 各有一份）；
- 内容：**每次成功上传追加一行 JSON**，字段包括：
  `time`、`action`、`source` / `source_path`、`object_key`、`bytes`、`sha256`、
  `expires` / `expires_at`、`sensitive`、`allow_sensitive`、`bucket`、`endpoint`、
  `url_without_signature`（**不含签名**的裸 URL）、`credential_source`、`verified`；
- 用途：这是"**什么东西离开了服务器**"的凭据。成绩类文件出域后，靠它回溯"哪天、哪个文件、多少字节、
  什么哈希、发到哪个桶"；链接本身不含签名，所以审计日志里不会留下可直接使用的下载凭据；
- **上传一确认（`getObjectMetadata` 字节数一致）就先写审计**：即使随后的链接实测失败，
  这一行也会留下（字段 `verified` 为 `false`），因为文件确实已经离开了服务器；
- **不要删改这个文件**；审计写不进去时本次上传按失败处理（退出码 2），因为留痕是硬要求。

示例一行（为可读性换行，实际是单行）：

```json
{"time": "2026-09-15T10:20:30+08:00", "action": "obs-upload", "source": "张三-成绩报告.docx",
 "source_path": ".../data/reports/张三-成绩报告.docx", "object_key": "teacher-assistant/2026-09-15/1a2b3c4d-张三-成绩报告.docx",
 "bytes": 30211, "sha256": "…", "expires": 86400, "expires_at": "2026-09-16T10:20:30+08:00",
 "sensitive": true, "allow_sensitive": true, "bucket": "openclaw-teacher",
 "endpoint": "obs.cn-east-3.myhuaweicloud.com",
 "url_without_signature": "https://openclaw-teacher.obs.cn-east-3.myhuaweicloud.com/teacher-assistant/2026-09-15/1a2b3c4d-张三-成绩报告.docx",
 "credential_source": "凭据文件 /root/.openclaw/.env", "verified": true}
```

## 上传后做了什么校验

1. `putObject` 返回 200；
2. `getObjectMetadata(...).body.contentLength` **必须等于本地文件字节数** —— 不一致 → 报「上传失败」+ 退出码 2；
3. `--verify`（默认开）再用 `urllib` **GET 一次预签名链接**：
   - HTTP 必须 200（否则报「链接不可下载」+ 退出码 2，并提示可能是签名被改动 / 已过期 / 服务器时间偏差）；
   - **取回的字节数必须等于本地字节数**（不只看状态码）；
   - 取回内容的 **sha256 也要一致**。

失败信息会明确区分是「**上传失败**」（第 2 步，远端对象不可信，链接不要发）还是
「**链接不可下载**」（第 3 步，上传可能已成功，是链接这一侧的问题，重跑生成新链接）。

## 退出码

| 退出码 | 含义 |
|--------|------|
| 0 | 成功（`--check` 诊断全部正常也算成功） |
| 2 | 参数 / 文件 / 自校验错误（`--expires` 越界、文件不存在、目录、上传后字节数不一致、链接校验失败、审计日志写不进去、参数拼写错误） |
| 3 | 凭据或依赖问题（缺 `OBS_ACCESS_KEY` / `OBS_SECRET_KEY` / `OBS_BUCKET` / `OBS_ENDPOINT`，或服务器缺 `esdk-obs-python`） |
| 4 | OBS 远端错误（`headBucket` / `putObject` / `createSignedUrl` 返回非预期状态、网络不通、区域不一致诊断结果） |
| 5 | 安全闸门（`data/` 下的文件未加 `--allow-sensitive`；或 `--check` 发现桶**不是私有的**） |

## 失败排查表

| 现象（脚本输出） | 可能原因 | 处理 |
|------------------|----------|------|
| `OBS 凭据未配置：缺少 OBS_ACCESS_KEY、…` | 没配凭据 | 按上文写 `~/.openclaw/.env`（权限 600），或 export 环境变量 |
| `OBS 区域不一致：… 返回 404` | `OBS_ENDPOINT` 的区域写错 | 照报错给出的 `obs.<region>.myhuaweicloud.com` 修正 endpoint。**这个 404 不代表桶不存在** |
| `桶 … 返回 404，且 listBuckets 里没有找到同名桶` | 区域错或 `OBS_BUCKET` 拼错 | 先按报错里的提示核对区域（本项目是 `cn-east-3`），再核对桶名；别用 `createBucket` 试 |
| `HEAD … 返回 HTTP 403` | AK/SK 不对、被禁用、没有该桶权限 | 核对 AK/SK 是否完整（别带空格），确认账号下确有该桶 |
| `putObject … 返回 HTTP 403` | 凭据没有写权限 / 桶只读 | 换有写权限的 AK/SK，或请管理员授权 |
| `上传失败：… 上传后在 OBS 上的字节数是 X，本地文件是 Y 字节` | 上传不完整（网络中断/代理改写） | **不要把链接发出去**；重试上传，仍失败检查网络与代理 |
| `链接不可下载：预签名 GET 返回 HTTP 403` | 链接被改动、已过期、或服务器时间偏差大 | 重新执行上传生成新链接；核对服务器时间（`date`） |
| `链接不可下载：请求预签名链接时网络失败` | 服务器出不了网 / DNS / 代理 | 确认服务器能访问 OBS（`--check` 能过说明基本连通） |
| `--expires 有效期越界：…上限是 604800 秒（7 天）` | 传了超过 7 天的有效期 | 改成 ≤ `604800`。**这个上限是我们自设的策略，不是 OBS 的限制**（实测 30 天的链接 OBS 也接受）；需要更长可用期时，到期后重新生成链接，而不是放宽上限 |
| `安全闸门拒绝：下面这些文件在 …/data 下` | 上传的是学生数据但没加 `--allow-sensitive` | **先问老师**，明确同意后再加 `--allow-sensitive`；不同意则给本地路径或一对一私发 |
| `[警告] 桶不是私有的`（`--check`，退出码 5） | 桶 ACL 被改成了公共读 | 请管理员把桶 ACL 改回私有（只有 owner），否则任何拿到对象名的人不签名也能下载 |
| `缺少 Python 依赖 obs（esdk-obs-python）` | 服务器没装 SDK | 联系维护者安装（`esdk-obs-python` 应已装在系统站点目录），本技能不会自己装包 |
| `审计日志写入失败` | 工作区根不可写 | 确认 cwd 是工作区根且可写；留痕是硬要求，写不进去就不算成功 |
| 微信里点链接打不开 | 链接已过期、或整段链接被聊天工具截断 | 重新生成链接；发链接时**整行一起复制**，别手工断行 |

## 未验证 / 已知不确定项

- 本技能只在服务器上做过真机验证（上传、`getObjectMetadata`、预签名 GET 200、未签名 GET 403、
  篡改 Signature 403）；`--check` 里的 **ACL 解析是防御式实现**，针对 `getBucketAcl` 返回结构的
  不同形态做了兼容，若 SDK 返回结构与预期差别较大，会显示「无法判定」而不是给出错误结论；
- 链接有效期以 **OBS 服务端时间**计算，服务器与 OBS 时间偏差过大会让链接立即失效，报错里会提示核对时间；
- 对象名保留中文，签名链接里的中文路径会被百分号编码，属预期；链接很长，分享时务必整行复制。
