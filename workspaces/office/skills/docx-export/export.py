#!/usr/bin/env python3
"""把 Markdown 转成 docx（内部调用 pandoc）。

退出码：0 成功 / 2 输入不合法 / 3 缺少依赖 / 4 转换失败。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# 一个合法 docx 至少含 xml 骨架，远大于这个值。
# 这个下限用于抓住"pandoc 报告成功但产物是空壳"的情况。
MIN_BYTES = 1000


def convert(src: Path, dst: Path) -> None:
    if not src.is_file():
        raise ValueError(f"输入文件不存在：{src}")
    text = src.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"输入文件是空的：{src}")

    pandoc = shutil.which("pandoc")
    if pandoc is None:
        raise RuntimeError("找不到 pandoc。请安装：apt-get install -y pandoc")

    dst.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [pandoc, str(src), "-o", str(dst), "--from", "markdown", "--standalone"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"pandoc 失败（退出码 {proc.returncode}）：{proc.stderr.strip()}"
        )
    if not dst.is_file():
        raise RuntimeError(f"pandoc 报告成功但没有生成文件：{dst}")
    size = dst.stat().st_size
    if size < MIN_BYTES:
        raise RuntimeError(f"生成的 docx 只有 {size} 字节，疑似空文档：{dst}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="把 Markdown 转为 docx")
    parser.add_argument("--input", required=True, help="输入 Markdown 路径")
    parser.add_argument("--output", required=True, help="输出 docx 路径")
    args = parser.parse_args(argv)

    try:
        out_path = Path(args.output)
        convert(Path(args.input), out_path)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 4
    except OSError as exc:
        print(f"错误：写入失败：{exc}", file=sys.stderr)
        return 4

    print(out_path.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())