---
name: markdown-to-exam-docx
description: 把 tmp/exam.md（学生版）与 tmp/answer.md（教师版）转换为试卷版 / 答案版 Word 文档（exams/*_试卷.docx、exams/*_答案.docx）
metadata:
  {
    "openclaw":
      {
        "emoji": "📝",
        "requires": { "bins": ["python3"], "libs": ["python-docx"] },
      },
  }
---

# 试卷 Markdown 导出为 Word（试卷版 + 答案版）

## 何时使用

`workspaces/lesson/AGENTS.md` **工作流二（智能组卷）阶段3** 使用：
`tmp/exam.md` 与 `tmp/answer.md` 已经写好，需要导出成两个 Word 文件时调用。

出题本身（题型分配、分值、质量标准）见 `skills/exam-generator/SKILL.md`。

## 输入的 Markdown 格式

格式由 `skills/exam-generator/SKILL.md` 定义，本脚本按同一规范解析。

### 试卷版（`tmp/exam.md`，学生版）

```markdown
# {试卷名称}（满分：100分 时间：90分钟）      ← `#` 一级标题 = 试卷名称

## 一、单项选择题（每题3分，共30分）          ← `##` 二级标题 = 大题号

1.（3分）题目内容（  ）
A. 选项一　　B. 选项二　　C. 选项三　　D. 选项四

## 二、填空题（每题2分，共20分）

1.（2分）题目内容___________。

## 三、解答题（每题10分，共20分）

1.（10分）题目内容
```

- **选择题四个选项写在同一行**：`A. …　　B. …　　C. …　　D. …`。每个选项单独成行时，转换也会排成横排。
- 题干的分值必须写在 `N.（X分）` 里，脚本据此缩排与统计。
- **试卷版不标注难度等级**，更不能出现 `【答案】`——脚本检测到答案标记会**拒绝生成**（退出码 3）。

### 答案版（`tmp/answer.md`，教师版）

```markdown
# {试卷名称} 参考答案

## 一、单项选择题（每题3分，共30分）

1.（3分）题目内容（  ）        ← 必须包含完整题目（含所有选项）
A. 选项一　　B. 选项二　　C. 选项三　　D. 选项四

【答案】B
【难度】易
【解析】解析文字……
```

- **必须包含完整题目与全部选项**，老师要能脱离试卷单独看答案版。
- 答案区标记固定四种，独立成行：`【答案】`、`【难度】`、`【解析】`、`【评分细则】`
  （`答案：` / `难度：` 这类半角写法也认）。
- 简答题在 `【难度】` 之后再加 `【评分细则】`。
- 答案版的 `#` / `##` 与试卷版保持一致，题号顺序必须一一对应。

## 执行

```bash
# 完整用法（两个文件都出）
python3 skills/markdown-to-exam-docx/scripts/convert.py \
  --input tmp/exam.md \
  --output exams/{试卷名}_试卷.docx \
  --answer-input tmp/answer.md \
  --answer-output exams/{试卷名}_答案.docx

# 只出试卷版
python3 skills/markdown-to-exam-docx/scripts/convert.py \
  --input tmp/exam.md --output exams/{试卷名}_试卷.docx
```

### 参数

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--input` | 是 | — | 学生版 Markdown，如 `tmp/exam.md` |
| `--output` | 是 | — | 学生版 docx，如 `exams/{试卷名}_试卷.docx` |
| `--answer-input` | 否 | 无 | 教师版 Markdown，如 `tmp/answer.md`；给了它才会出答案版 |
| `--answer-output` | 否 | 由 `--output` 推导 | 答案版 docx；不给时把 `_试卷` 换成 `_答案` |
| `--font-cjk` | 否 | `宋体` | 中文字体，可换 `楷体` 等 |
| `--font-latin` | 否 | `Times New Roman` | 西文字体 |
| `--body-size` | 否 | `10.5` | 正文字号 pt（五号 = 10.5） |
| `--line-spacing` | 否 | `1.5` | 行距倍数 |
| `--no-page-number` | 否 | 关 | 不生成页脚「第 X 页 共 Y 页」 |
| `--no-warn` | 否 | 关 | 不打印质检告警 |
| `--help` | 否 | — | 打印用法后退出 0 |

**只给 `--input/--output` 也能工作**（只生成试卷版）；给了 `--answer-input`
而没给 `--answer-output` 时自动推导答案路径。给了 `--answer-output` 却没有
`--answer-input` 会报错退出（退出码 2）。

## 输出路径

- 试卷版：`exams/{试卷名}_试卷.docx`
- 答案版：`exams/{试卷名}_答案.docx`
- 目录不存在时自动创建。脚本把试卷版绝对路径打印到 stdout 第一行：

  ```
  /srv/openclaw-teacher/workspaces/lesson/exams/八年级数学期中_试卷.docx
  试卷版：/srv/openclaw-teacher/workspaces/lesson/exams/八年级数学期中_试卷.docx（相对路径：exams/八年级数学期中_试卷.docx）
    学生版字数统计：202 字
  答案版：/srv/openclaw-teacher/workspaces/lesson/exams/八年级数学期中_答案.docx（相对路径：exams/八年级数学期中_答案.docx）
    答案版字数统计：463 字
  质检警告：0 条
  ```

- 生成后按 AGENTS.md 阶段3/阶段4 交付：**只给老师「文件名 + 下载链接」**（先跑
  `skills/obs-share` 上传换短链）。**绝不把上面这些服务器路径贴给老师** —— 他打不开。
  （上面示例里的绝对路径是脚本 stdout 的原文，仅供你自己核对文件确实生成了。）

## 排版规范（契约固定）

- 页面 A4，上下页边距 2.54cm，左右 2.5cm（留装订与答题空间），页脚居中「第 X 页 共 Y 页」。
- **大题号宋体加粗**（12pt），**正文宋体 + Times New Roman**，**五号字 10.5pt**，**1.5 倍行距**。
- **选项横排**：同一题的 `A.`~`D.` 排在同一行，整行左缩进 1.2cm，题干不缩进。
- 试卷名称居中放大加粗（16pt），`（满分：X分 时间：Y分钟）` 会被提取成标题下的信息行。
- 答案版的 `【答案】/【难度】/【解析】/【评分细则】` 行加粗、左缩进 0.6cm。

## 失败排查

| 现象 | 退出码 | 处理 |
|---|---|---|
| `错误：学生版试卷（--input）里出现了答案标记【答案】…` | 3 | 把答案内容挪到 `tmp/answer.md`，重跑；**绝不能把带答案的稿子当学生版发出去** |
| `错误：答案输入文件不存在：…` | 2 | `tmp/answer.md` 还没写，或路径写错 |
| `错误：给了 --answer-output 但没有 --answer-input…` | 2 | 两个参数要成对出现 |
| `错误：输入文件为空：…` | 2 | 重新生成 Markdown；空文件会被拒绝 |
| `错误：输入文件不是 UTF-8 编码…` | 2 | 用 UTF-8 重写（Windows 下别用 GBK 保存） |
| `错误：缺少依赖 python-docx` | 2 | 依赖缺失，报给运维，不要改用 pandoc 硬凑 |
| `警告：学生版题量（N）与答案版题量（M）不一致` | 0 | 回阶段2 补齐答案版题目 |
| `警告：第 N 题的选项数为 X（选择题应为 4 个 A~D）` | 0 | 选项没写全或没独立成行 |
| `警告：各大题标注分值之和（X）与试卷总分（Y）不一致` | 0 | 分值算错了，回阶段2 重算 |
| `错误：写入 docx 失败：…` | 1 | 输出目录不可写，或文件正被 Word 占用 |

- 退出码非 0 时**把 stderr 的错误原文贴给老师**，不要自己编造成功。
- 告警（退出码 0）不会阻止生成，但分值/题量不一致必须回去改 Markdown 再交付。
- 可选的机器质检（题干数=答案数、分值合计、重复题干、选项完整、学生版泄漏）：
  `python3 skills/exam-generator/scripts/validate_exam.py --exam tmp/exam.md --answer tmp/answer.md --total 100`
