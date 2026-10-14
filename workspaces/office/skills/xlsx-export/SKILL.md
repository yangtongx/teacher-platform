---
name: xlsx-export
description: 把结构化题目 JSON 导出为 Excel 文件，含题干、答案、解析、题型、难度、知识点列
metadata:
  {
    "openclaw":
      {
        "emoji": "📊",
        "requires": { "bins": ["python3"] },
      },
  }
---

# 题目导出为 Excel

## 何时使用

老师要求把题目导出成 Excel / 表格 / 试卷文件时使用。
教案正文不要用这个技能，那是 `docx-export`。

## 输入格式

调用脚本前，先把题目写成一个 JSON 文件，放在当前 workspace 下。结构（示例用四空格缩进）：

    {
      "questions": [
        {
          "stem": "题干文字",
          "answer": "参考答案",
          "analysis": "解析：考查点与易错点",
          "type": "选择题",
          "difficulty": "中",
          "point": "知识点名称"
        }
      ]
    }

- `stem` 与 `answer` 必填。缺任一，脚本会报错退出，不会生成文件。
- `analysis`、`type`、`difficulty`、`point` 可选，缺失时该列留空。
- 不要用 Markdown 表格代替 JSON。脚本只认 JSON。

## 执行

```bash
python3 {baseDir}/export.py --input <题目JSON路径> --output exports/<文件名>.xlsx
```

## 输出与失败处理

- 生成的 xlsx 落在 `exports/` 下，脚本把输出文件的绝对路径打印到 stdout。
- 把打印出的路径告诉老师，并说明文件含哪些列。
- 脚本退出码非 0 时，**把 stderr 的错误原文贴给老师**，不要自己编造成功。