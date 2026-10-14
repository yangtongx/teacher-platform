#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""试卷机器质检（**可选工具**，工作流不依赖它）。

契约里 `exam-generator` 只有 SKILL.md、没有脚本；本脚本是附加工具，用于在
`tmp/exam.md` / `tmp/answer.md` 交给 `markdown-to-exam-docx/scripts/convert.py`
之前做机器可查的质检：

  1. 题干数与答案数是否一致
  2. 各大题分值之和是否等于总分
  3. 有没有重复题干
  4. 选择题选项数是否完整（A~D 四个）
  5. 学生版里是否误含 `【答案】` 等标记

用法：

    python3 skills/exam-generator/scripts/validate_exam.py \
      --exam tmp/exam.md --answer tmp/answer.md --total 100

退出码：0 = 没有硬错误（可能有警告）；1 = 有硬错误；2 = 用法/文件错误。
只用标准库，不引入任何新依赖。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

QUESTION_RE = re.compile(
    r"^\s*(\d{1,3})\s*[.、．)）]\s*[（(]\s*(\d+(?:\.\d+)?)\s*分\s*[）)]\s*(.*)$"
)
OPTION_RE = re.compile(r"^\s*([A-H])\s*[.、．)）]\s*(.*)$")
OPTION_MARK_RE = re.compile(r"(?:(?<=^)|(?<=\s))([A-H])\s*[.、．)）]")
SCORE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*分")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
INFO_RE = re.compile(r"(满分|总分|分值)\s*[：:]\s*([0-9]+(?:\.[0-9]+)?)")
MARKER_RE = re.compile(r"【(答案|难度|解析|评分细则|知识点)】")
LABEL_RE = re.compile(r"(答案|难度|解析|评分细则|知识点)\s*[：:]")
# 题干规范化时要去掉的标点（中文全角标点先归一化，再统一剔除）
PUNCT_RE = re.compile(
    r"[\s\u3000。，、；：？！“”‘’（）《》〈〉【】…—～·,.!?;:\"'()\[\]{}<>/\\|~`@#$%^&*+=_-]+"
)

# 选择题大题的关键字：只有这些大题才强制要求 A~D 四个选项
CHOICE_KEYWORDS = ("选择", "单选", "多选", "不定项")


def configure_std_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass


def normalize_stem(text: str) -> str:
    """题干规范化：去掉空白与标点，用于重复题检测。"""
    text = re.sub(r"[（(][^（()）]{0,40}[）)]", "", text)
    return PUNCT_RE.sub("", text)


def read_text(path: Path, label: str) -> str:
    if not path.is_file():
        sys.stderr.write(f"错误：{label}文件不存在：{path}\n")
        raise SystemExit(2)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        sys.stderr.write(f"错误：{label}文件不是 UTF-8 编码：{path}（{exc}）\n")
        raise SystemExit(2)
    if not text.strip():
        sys.stderr.write(f"错误：{label}文件为空：{path}\n")
        raise SystemExit(2)
    return text


def parse_paper(text: str) -> dict:
    """按行扫描，得到大题目录与题目列表（含所属大题、选项）。"""
    sections = []          # [{"title": str, "declared": float|None}]
    questions = []         # [{"number","score","stem","options","section"}]
    current_section = None
    current_question = None
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    for raw in lines:
        line = raw.strip()
        if line == "":
            continue
        heading = HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            if level == 2:
                scores = [float(v) for v in SCORE_RE.findall(title)]
                section = {"title": title,
                           "declared": scores[-1] if scores else None}
                sections.append(section)
                current_section = section
            current_question = None
            continue
        question = QUESTION_RE.match(line)
        if question:
            current_question = {
                "number": question.group(1),
                "score": float(question.group(2)),
                "stem": question.group(3).strip(),
                "options": [],
                "section": current_section["title"] if current_section else "",
            }
            questions.append(current_question)
            continue
        option = OPTION_RE.match(line)
        if option and current_question is not None and option.group(2).strip():
            labels = [item.upper() for item in OPTION_MARK_RE.findall(line)]
            if len(labels) < 3:
                labels = [option.group(1).upper()]
            current_question["options"].extend(labels)
            continue
        if current_question is not None:
            if MARKER_RE.match(line) or LABEL_RE.match(line):
                current_question = None  # 答案区开始，不再并入题干
                continue
            # 题干续行
            current_question["stem"] += " " + line
    return {"sections": sections, "questions": questions}


def declared_total(text: str):
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line.startswith("#"):
            continue
        for match in INFO_RE.finditer(line):
            return float(match.group(2))
    return None


def is_choice_section(title: str) -> bool:
    return any(keyword in title for keyword in CHOICE_KEYWORDS)


def validate(exam_text: str, answer_text: str, expect_total):
    errors, warnings = [], []
    exam = parse_paper(exam_text)
    answer = parse_paper(answer_text) if answer_text else {"sections": [],
                                                           "questions": []}

    if not exam["questions"]:
        errors.append("试卷版没有解析到任何题目，请检查题干是否为 `1.（3分）……` 格式")

    # 1) 题干数与答案数一致
    if answer_text:
        if not answer["questions"]:
            errors.append("答案版没有解析到任何题目，请检查答案版是否包含完整题目")
        elif len(exam["questions"]) != len(answer["questions"]):
            errors.append(
                f"题干数与答案数不一致：试卷版 {len(exam['questions'])} 题，"
                f"答案版 {len(answer['questions'])} 题"
            )
        else:
            exam_numbers = [q["number"] for q in exam["questions"]]
            answer_numbers = [q["number"] for q in answer["questions"]]
            if exam_numbers != answer_numbers:
                errors.append(
                    f"题号顺序不一致：试卷版 {exam_numbers}，答案版 {answer_numbers}"
                )
        if not MARKER_RE.search(answer_text):
            errors.append("答案版里找不到任何 `【答案】` 标记")

    # 2) 各大题分值之和 vs 总分
    declared = declared_total(exam_text)
    total = expect_total if expect_total is not None else declared
    section_sum = sum(s["declared"] for s in exam["sections"]
                      if s["declared"] is not None)
    question_sum = sum(q["score"] for q in exam["questions"])
    if total is not None:
        if section_sum and abs(section_sum - total) > 0.01:
            errors.append(f"各大题标注分值之和为 {section_sum:g} 分，与总分 {total:g} 分不符")
        elif not section_sum and abs(question_sum - total) > 0.01:
            errors.append(f"各题分值之和为 {question_sum:g} 分，与总分 {total:g} 分不符")
        print(f"分值核对：各大题标注之和 {section_sum:g} 分，各题分值之和 "
              f"{question_sum:g} 分，总分 {total:g} 分")
    else:
        warnings.append("试卷名称里没有「满分：X 分」，也没有 --total，跳过分值核对")

    # 3) 重复题干
    seen = {}
    for question in exam["questions"]:
        key = normalize_stem(question["stem"])
        if len(key) < 6:
            continue
        if key in seen:
            errors.append(
                f"第 {question['number']} 题与第 {seen[key]} 题题干重复："
                f"{question['stem'][:30]}"
            )
        else:
            seen[key] = question["number"]

    # 4) 选择题选项完整性
    for question in exam["questions"]:
        if is_choice_section(question["section"]):
            labels = question["options"]
            if not labels:
                errors.append(
                    f"第 {question['number']} 题属于「{question['section']}」，但没有选项行"
                )
                continue
            missing = [ch for ch in "ABCD" if ch not in labels]
            if missing:
                errors.append(
                    f"第 {question['number']} 题缺选项：{'/'.join(missing)}"
                    f"（现有 {'/'.join(labels)}）"
                )
            extra = [ch for ch in labels if ch not in "ABCD"]
            if extra:
                warnings.append(
                    f"第 {question['number']} 题有 A~D 之外的选项：{'/'.join(extra)}"
                )

    # 5) 学生版误含答案标记
    leaked = set()
    for match in MARKER_RE.finditer(exam_text):
        leaked.add(match.group(1))
    for raw in exam_text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if line.startswith("【"):
            leaked.add(line[1:line.find("】")] if "】" in line else "答案")
        elif LABEL_RE.match(line):
            leaked.add(LABEL_RE.match(line).group(1))
    if leaked:
        errors.append(
            "学生版里出现了答案标记：" + "、".join(f"【{n}】" for n in sorted(leaked))
            + "（试卷版不得标注难度或答案）"
        )

    return errors, warnings, exam, answer


def main(argv=None) -> int:
    configure_std_streams()
    parser = argparse.ArgumentParser(
        prog="validate_exam.py",
        description="试卷机器质检（可选工具，工作流不依赖）：题干/答案数、分值合计、"
                    "重复题干、选项完整性、学生版答案泄漏。",
        epilog="示例：python3 skills/exam-generator/scripts/validate_exam.py "
               "--exam tmp/exam.md --answer tmp/answer.md --total 100",
    )
    parser.add_argument("--exam", required=True, help="学生版试卷 Markdown，如 tmp/exam.md")
    parser.add_argument("--answer", default=None, help="教师版答案 Markdown，如 tmp/answer.md（可选）")
    parser.add_argument("--total", type=float, default=None,
                        help="期望总分（默认从试卷名称的「满分：X分」读取）")
    args = parser.parse_args(argv)

    exam_text = read_text(Path(args.exam), "试卷")
    answer_text = read_text(Path(args.answer), "答案") if args.answer else ""

    errors, warnings, exam, answer = validate(exam_text, answer_text, args.total)

    print(f"质检对象：{Path(args.exam)}"
          + (f" + {Path(args.answer)}" if args.answer else "（仅学生版）"))
    print(f"题目统计：学生版 {len(exam['questions'])} 题"
          + (f"，答案版 {len(answer['questions'])} 题" if answer_text else ""))
    for item in warnings:
        print(f"警告：{item}")
    for item in errors:
        print(f"错误：{item}")
    print(f"质检结论：硬错误 {len(errors)} 条，警告 {len(warnings)} 条")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
