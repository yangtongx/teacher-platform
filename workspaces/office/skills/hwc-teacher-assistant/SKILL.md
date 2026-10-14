---
name: hwc-teacher-assistant
description: 学生成绩管理与家校沟通：读取飞书表格或本地成绩文件、考试成绩对比分析、生成并一对一私发 Word 成绩报告
metadata:
  {
    "openclaw":
      {
        "emoji": "📊",
        "requires": { "bins": ["python3"] },
      },
  }
---

# 教师助手：成绩管理与家校沟通

## 何时使用

老师提到下面这些事时用本技能：

- 给了一个**飞书表格链接**，要把成绩读进工作区；
- **上传了 Excel/CSV 成绩文件**，要归档并分析；
- 要**对比两次考试**、看班级整体、看谁进步谁退步、看各科薄弱点；
- 要给某位家长**发孩子的成绩报告**（Word）。

**不属于本技能**：教案设计、出题、组卷、试卷与答案解析（那是「教案与出题专家」的事）；
纯文档排版（用 `docx-export`）、题目清单表（用 `xlsx-export`）。

## 目录与数据约定

所有脚本以 `python3 skills/hwc-teacher-assistant/scripts/<脚本名>.py` 调用，
**cwd = 工作区根目录**（生产上是 `/srv/openclaw-teacher/workspaces/office`）。
本技能不依赖服务器上的其它路径，`data/` 相对工作区根目录，不存在会自动创建。

```
data/
├── grades/                     # 规范化后的成绩表：一行一个学生、首行表头
│   ├── 2026-03-10.xlsx         # 文件名 = 考试日期（YYYY-MM-DD）
│   └── 2026-04-10.xlsx
└── reports/
    ├── 2026-04-10-对比分析.md   # 流程2 的班级报告（给老师看）
    └── 张三-成绩报告.docx       # 流程3/4 的学生报告（给家长）
```

约定：

- `data/grades/{日期}.xlsx` 的**文件名就是考试日期**，`analyze_exams.py` 与
  `send_word_report.py` 都按文件名排「最近 N 次」，所以不要随手改名。
- 同名日期已存在时 `read_sheet.py` **默认拒绝写入**（不覆盖旧成绩），要覆盖必须加 `--force`。
- 脚本会合并同一份 Excel 里**表头一致**的所有工作表；表头不一致的工作表不会被静默丢弃，
  会在输出里明确提示「已跳过」并列名。
- 三个脚本必须在同一个 `scripts/` 目录里一起部署：`send_word_report.py` 会 `import` 同目录的
  `analyze_exams.py`，用的是同一套列名识别与统计口径，保证「报告里的数字」和「分析报告里的数字」一致。

## 脚本一：read_sheet.py（流程1 读取数据）

方式 A 飞书链接：解析类型（`sheets` 电子表格 / `base` 多维表格）→ 调 API → 存入 `data/grades/{日期}.xlsx`。
方式 B 本地文件：读 Excel/CSV → 从文件名解析考试时间 → 存入 `data/grades/{日期}.xlsx`。

| 参数 | 必填 | 说明 |
|------|------|------|
| `--url <链接>` | 与 `--local` 二选一 | 形如 `https://xxx.feishu.cn/sheets/<token>` 或 `/base/<token>`，可带 `?sheet=xxx` |
| `--local <路径>` | 与 `--url` 二选一 | 本地 `.xlsx` / `.xlsm` / `.csv`（旧版 `.xls` 不支持，请另存为 xlsx/csv） |
| `--check` | 否 | **只诊断**：验证凭据是否能换到 `tenant_access_token`、链接是否能读到表元信息 / 本地文件是否可读。不下数据、不写文件 |
| `--date <YYYY-MM-DD\|YYYYMMDD>` | 否 | 覆盖考试日期，优先于文件名解析 |
| `--sheet <工作表名>` | 否 | 只读指定工作表（默认合并表头一致的全部工作表） |
| `--data-dir <目录>` | 否 | 默认 `data` |
| `--force` | 否 | 目标日期文件已存在时覆盖 |
| `--timeout <秒>` | 否 | HTTP 超时，默认 15 |

考试日期解析顺序：`--date` → 文件名里的 `2026-04-10` / `2026.4.10` / `2026年4月10日` /
`20260410` / `2026年4月`（按当月 1 日）→ 都解析不到就用**今天**，并在输出里写明
「名称里没有可识别的日期，按今天处理」。输出 xlsx 首行是表头，**保留原始列名**。

```bash
# 只验证凭据和链接，不下数据（推荐每次先跑一遍）
python3 skills/hwc-teacher-assistant/scripts/read_sheet.py --check --url "https://xxx.feishu.cn/sheets/shtcnXXXX?sheet=0b12"

# 方式A：飞书电子表格 / 多维表格
python3 skills/hwc-teacher-assistant/scripts/read_sheet.py --url "https://xxx.feishu.cn/sheets/shtcnXXXX"
python3 skills/hwc-teacher-assistant/scripts/read_sheet.py --url "https://xxx.feishu.cn/base/bascnXXXX"

# 方式B：老师上传的成绩文档（日期从文件名取）
python3 skills/hwc-teacher-assistant/scripts/read_sheet.py --local uploads/期中成绩2026-04-10.xlsx
python3 skills/hwc-teacher-assistant/scripts/read_sheet.py --local uploads/月考.csv --date 2026-04-10
```

成功输出示例（原样回给老师即可）：

```
[完成] 已写入：/srv/openclaw-teacher/workspaces/office/data/grades/2026-04-10.xlsx
       取数方式：本地成绩文档（方式B）
       考试日期：2026-04-10（来自名称「2026-04-10」）
       表头 6 列：姓名、学号、语文、数学、英语、总分
       数据 42 行（一行一个学生）
```

## 脚本二：analyze_exams.py（流程2 成绩分析）

读两次（或多次）成绩 → 对比排名/总分/各科 → 识别进退步学生 → 生成 Markdown 分析报告。
**完全离线**：只读本地 xlsx/csv，不联网、不调用任何大模型。

| 参数 | 必填 | 说明 |
|------|------|------|
| `--current <路径>` | 是 | 本次成绩文件 |
| `--previous <路径>` | 否，可多次 | 对比成绩；名次升降以**日期最近**的一次为基准 |
| `--output <路径>` | 否 | Markdown 输出路径，默认 `data/reports/{本次文件名}-对比分析.md`；以 `.json` 结尾时改写结构化 JSON |
| `--top <N>` | 否 | 进步/退步/名次名单条数，默认 5 |
| `--json` | 否 | 把结构化结果打印到 stdout（便于后续流程取数） |
| `--full-score <分>` | 否 | 总分满分，用于及格率/优秀率 |
| `--subject-full 科目=满分` | 否，可多次 | 指定某科满分，如 `--subject-full 语文=150` |
| `--pass-ratio <比例>` | 否 | 及格线比例，默认 0.6 |
| `--excellent-ratio <比例>` | 否 | 优秀线比例，默认 0.85 |
| `--sheet <工作表名>` | 否 | 只分析指定工作表 |
| `--data-dir <目录>` | 否 | 默认 `data` |

列名容错：学生姓名（姓名/学生姓名/名字/学生/name）、学号（学号/考号/准考证号/…）、
总分（总分/总成绩/合计/总计/…）都能识别；其余「至少有一个分数」的列当作科目。
认不出姓名列、一个科目列都没有、或检测到「科目+成绩」的长表格式时，会**中文报错并列出实际读到的列名**。

报告包含：班级总分平均/最高/最低/及格率/优秀率、各次考试趋势、各科平均分与得分率、
学生总分与**名次升降**（↑ 前进 / ↓ 后退）、进步与退步学生名单（各 `--top` 条）、
名次进步/退步名单、单科薄弱点提示、教学建议（只基于报告里的数字）、数据说明与缺口。

满分口径（影响及格率/优秀率/得分率）：`--full-score` 与 `--subject-full` 优先；
没指定时按**本次观测最高分**在 100/120/150/200/300 里往上取最小的一个推定，
报告里会写明「推定」。**没有数据的项一律写「数据不足」，不编造结论。**

```bash
python3 skills/hwc-teacher-assistant/scripts/analyze_exams.py \
    --current data/grades/2026-04-10.xlsx \
    --previous data/grades/2026-03-10.xlsx \
    --output data/reports/2026-04-10-对比分析.md

# 多次对比 + 精确满分口径 + 结构化输出
python3 skills/hwc-teacher-assistant/scripts/analyze_exams.py \
    --current data/grades/2026-04-10.xlsx \
    --previous data/grades/2026-02-10.xlsx \
    --previous data/grades/2026-03-10.xlsx \
    --subject-full 语文=150 --subject-full 数学=150 --subject-full 英语=150 \
    --full-score 450 --top 10 --json
```

## 脚本三：send_word_report.py（流程3 发送成绩报告 / 流程4 私聊通知）

取最近 N 次（默认 3）成绩 → 生成学生 Word 报告 → 加 `--send` 时**一对一私发**给家长。

| 参数 | 必填 | 说明 |
|------|------|------|
| `--student <姓名>` | 是 | 学生姓名（写法要和成绩表里完全一致） |
| `--student-id <学号>` | 同名学生必填 | 同名学生会报错并要求用它区分 |
| `--grades <路径...>` | 否 | 成绩文件，可多个；不传则取 `data/grades/` 下最近 `--limit` 次 |
| `--output <路径>` | 否 | docx 输出路径，默认 `data/reports/{姓名}-成绩报告.docx` |
| `--to-open-id <ou_xxx>` | `--send` 时必填 | 家长**个人** open_id；群 `oc_`/部门 `od_`/`union_id` 一律拒绝 |
| `--send` | 否 | **真正发送**。不加就是演练模式：只生成 docx，不发任何消息 |
| `--message <文本>` | 否 | 随报告一起发的文本，默认用内置模板 |
| `--class-name <班级>` | 否 | 写进报告抬头 |
| `--limit <N>` | 否 | 合并最近几次成绩，默认 3 |
| `--data-dir <目录>` | 否 | 默认 `data` |
| `--timeout <秒>` | 否 | HTTP 超时，默认 15 |

Word 报告含：总成绩与班级名次（含名次变化）、各科成绩（含变化）、最近一次各科明细
（个人得分/班级平均/满分/班级得分率）、数据来源与说明。中文字体显式写了 `w:eastAsia`（默认宋体）。

```bash
# 只生成（默认行为，不发消息）
python3 skills/hwc-teacher-assistant/scripts/send_word_report.py \
    --student 张三 --grades data/grades/2026-03-10.xlsx data/grades/2026-04-10.xlsx \
    --output data/reports/张三-成绩报告.docx

# 自动取最近 3 次成绩并生成
python3 skills/hwc-teacher-assistant/scripts/send_word_report.py --student 张三

# 生成并一对一私发给家长
python3 skills/hwc-teacher-assistant/scripts/send_word_report.py \
    --student 张三 --to-open-id ou_xxxxxxxxxxxxxxxx --send
```

## 飞书应用创建与权限

1. 打开 <https://open.feishu.cn/app> → **创建企业自建应用**（个人版飞书私聊能力受限，见下）。
2. 「凭证与基础信息」里复制 **App ID**（`cli_` 开头）与 **App Secret**。
3. 「权限管理」里开通下列权限：

   | 权限 | 用途 |
   |------|------|
   | `sheets:spreadsheet:readonly` | 读电子表格（流程1 方式A） |
   | `im:chat.members:read` | 读群成员列表，按姓名匹配家长 open_id（流程4） |
   | `im:message` | 发消息（一对一私发、文件消息） |

   注：多维表格（`/base/` 链接）另需多维表格读取权限（如 `bitable:app:readonly`），
   未开通时接口会返回权限类错误码，按下面的排查表处理。
4. **发布版本并等管理员审核通过**，权限才会真正生效（只勾选不发布是最常见的坑）。
5. 让家长先和机器人建立会话（企业版：家长加入组织通讯录并可见该应用），否则发消息会失败。
6. 如果表格是别人创建的，还要把应用/机器人**加为该文档的协作者**，否则读表会报无权限。

## 凭据配置（绝不硬编码）

脚本按这个顺序找凭据，**代码里没有任何默认值或硬编码凭据**：

1. 环境变量 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`；
2. 凭据文件 `~/.openclaw/.env`（可用环境变量 `FEISHU_ENV_FILE` 指定其它路径）。

`~/.openclaw/.env` 写法（`KEY=VALUE`，支持 `#` 注释、`export ` 前缀、值两侧引号）：

```ini
# 飞书自建应用凭据（只放服务器，绝不提交仓库）
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

```bash
mkdir -p ~/.openclaw && chmod 700 ~/.openclaw
printf 'FEISHU_APP_ID=cli_xxx\nFEISHU_APP_SECRET=xxx\n' >> ~/.openclaw/.env
chmod 600 ~/.openclaw/.env
```

私有化部署（自建域名）可用 `FEISHU_BASE_URL` 覆盖开放平台域名，默认 `https://open.feishu.cn`。

**凭据未配置时的行为**（当前生产环境就是这一状态）：

- `read_sheet.py --check --url ...`：打印中文诊断（缺哪个变量、该写在哪里、需要哪些权限），**退出码 3**；
- `read_sheet.py --url ...`（真正取数）：**退出码 3 并报错**，不写文件、不伪造空数据；
- `send_word_report.py --send ...`：**退出码 3 并报错**，不会出现「假装已发送」；
- `analyze_exams.py` 与 `send_word_report.py` 的**只生成**路径不需要凭据，现在就能跑。

## 数据安全与红线

**数据安全**

- 学生信息、家长联系方式、成绩**只在工作区内处理**，只写进 `data/`；
- **不向任何第三方服务泄露**，包括大模型 API：三个脚本都不调用任何模型，也不上传成绩到任何外部接口；
- 唯一的外发路径是**老师明确要求的多维表格/电子表格读取**与**`--send` 时发给该家长本人的飞书私聊**；
- 凭据只存在于环境变量或 `~/.openclaw/.env`，不写进日志、不写进报告、不进仓库。

**家校沟通红线（代码层面强制）**

- 邮件/报告 = 自动附带 Word 报告（`send_word_report.py` 默认就生成 docx）；
- 私聊 = **一对一私发，绝不在群里发**：
  - `--to-open-id` 只接受个人 open_id（`ou_` 开头）；
  - 传入群 `oc_` / 部门 `od_` / `union_id` 时**直接拒绝并退出码 5**，不会发出任何请求；
  - 调消息接口时 `receive_id_type` 固定写死 `open_id`，没有「发到群里」的入口；
  - 默认 dry-run：不加 `--send` 永远不发消息，发送前会先把「收件人 + 学生 + 附件路径」打印出来。

## 退出码

| 退出码 | 含义 |
|--------|------|
| 0 | 成功 |
| 2 | 输入不合法（参数、文件、列名识别失败、目标文件已存在） |
| 3 | 缺少凭据或依赖（缺 `FEISHU_APP_ID`/`FEISHU_APP_SECRET`、缺 openpyxl/python-docx） |
| 4 | 飞书接口或网络失败（响应里的 `code`/`msg` 会原样打印） |
| 5 | 触发安全红线（例如往群 chat_id 发私聊） |

## 失败排查表

| 现象（脚本输出） | 可能原因 | 处理 |
|------------------|----------|------|
| `飞书凭据未配置：缺少 FEISHU_APP_ID、FEISHU_APP_SECRET` | 没配凭据 | 按上文配 `~/.openclaw/.env`，或 `export FEISHU_APP_ID/SECRET` |
| `没有取到 tenant_access_token：code=…` | App ID/Secret 错、应用被停用、权限版本未发布 | 重新复制「凭证与基础信息」，确认应用已发布 |
| `飞书接口报错 code=… msg=…` | 接口层错误 | **以 msg 为准**；下表是几条最常见的 |
| `链接里没有找到 /sheets/<token> 或 /base/<token>` | 链接不是这两种，或复制的是首页/搜索结果链接 | 在表格里点「分享 → 复制链接」，或用知识库提示里的办法取直链 |
| `这是飞书知识库（wiki）链接` | 给了 `/wiki/` 链接 | 打开该表格后从地址栏复制 `/sheets/` 或 `/base/` 链接 |
| `表格里没有名为「x」的工作表` | `--sheet` 写错 | 按报错里列出的实际工作表名重试 |
| `无法识别学生姓名字段` / `没有识别到任何科目列` | 表头不是常见写法 | 按报错里列出的实际列名，把表头改成「姓名 / 学号 / 总分」这类写法 |
| `检测到长表格式（每行一科）` | 表里有「科目 + 成绩」两列 | 先用数据透视表转成宽表（一行一个学生、一列一科） |
| `目标文件已存在：data/grades/….xlsx` | 同日期成绩已导入过 | 确认要覆盖就加 `--force`，或用 `--date` 指定别的日期 |
| `本次没有读到该学生的成绩` | 姓名写法不一致 / 缺考 | 用成绩表里的原始写法，或加 `--student-id` |
| `及格率 / 优秀率：数据不足` | 没给满分口径 | 加 `--full-score`（和 `--subject-full`）后重跑 |
| 报告里出现「数据不足」 | 该次成绩缺列/缺考 | 属正常兜底：脚本不会用别的数字硬凑 |

最常见飞书错误码（以官方《通用错误码》文档为准，`msg` 通常更具体）：

| code | 常见含义 | 处理 |
|------|----------|------|
| 99991663 | `tenant_access_token` 无效或已过期（多见于凭据问题） | 核对 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`，确认应用已发布 |
| 99991664 | 凭据（`app_id` / `app_secret`）不匹配 | 重新复制一组完整凭据，注意别带空格 |
| 9499 | Bad Request，参数或权限不对 | 核对链接 token 是否正确、应用是否被授权访问该表格/多维表格 |
| 权限类错误（如提示 forbidden / permission denied） | 权限没开或版本没发布；文档没把应用加成协作者 | 开放平台加权限 → 发布版本 → 把应用加为该文档协作者 |

## 已知限制（未验证 / 待补）

- **飞书凭据尚未配置**，所以「飞书取数」与「私聊发送」这两条联网链路**从未在真机上跑通过**，
  只有离线单测用假的 HTTP 响应覆盖了代码路径。
- **邮件通道未实现**：本技能把报告生成好（docx），发送走的是**飞书 IM 一对一私聊**；
  SMTP/邮件网关不在本技能范围内，要发邮件请老师把生成的 docx 作为附件自行发送。
- **多维表格（`/base/`）只读第一张数据表**，且需要额外申请多维表格权限。
- 飞书**个人版**无法私聊组织外用户，私聊能力需要企业版/教育版（见 `SOUL.md`）。
