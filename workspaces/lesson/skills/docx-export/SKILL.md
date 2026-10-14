---
name: docx-export
description: 把 Markdown 教案或通知导出为 Word 文档（docx），内部用 pandoc 转换
metadata:
  {
    "openclaw":
      {
        "emoji": "📄",
        "requires": { "bins": ["pandoc"] },
      },
  }
---

# Markdown 导出为 Word

## 何时使用

老师要求把教案、通知、纪要等正文导出成 Word 文件时使用。
题目清单不要用这个技能，那是 `xlsx-export`。

## 输入格式

先把正文写成一个 Markdown 文件（`.md`），放在当前 workspace 下：

- 用 `#` 一级标题写文档标题。
- 用 `##` 写小节。
- 需要强调的地方用 `**粗体**`。
- 表格用标准 Markdown 表格语法。

正文不要留空。空文件会被脚本拒绝。

## 执行

```bash
python3 {baseDir}/export.py --input <Markdown路径> --output exports/<文件名>.docx
```

## 输出与失败处理

- 生成的 docx 落在 `exports/` 下，脚本把输出文件的绝对路径打印到 stdout。
- 退出码非 0 时，**把 stderr 的错误原文贴给老师**，不要自己编造成功。
- 常见失败原因：宿主机没装 pandoc、输入文件为空、`exports/` 不可写。