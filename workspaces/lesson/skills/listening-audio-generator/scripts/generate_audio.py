#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""听力音频合成脚本（listening-audio-generator）。

把 JSONL 听力脚本（instruction / dialogue / question 三类行）合成为一个听力 mp3：

    1. 逐段用 edge-tts 合成单段 mp3（异步 API，Python 3.11 下不用已废弃接口）；
    2. 需要停顿的地方（pause_after）用 ffmpeg 的 anullsrc 生成**等长静音 mp3**，
       不用"朗读空格/标点"来凑停顿；
    3. 用 ffmpeg concat demuxer 把各段直接拼成一个 mp3（不重编码，避免二次损失）；
    4. 用 ffprobe 实测各分段时长，与输出的总时长对账，把 JSON 摘要打到 stdout。

契约来源：`workspaces/lesson/AGENTS.md` 工作流二"第4步：生成听力音频"。
调用方式（cwd = 工作区根目录）：

    python3 skills/listening-audio-generator/scripts/generate_audio.py \
      --input tmp/listening_script.jsonl \
      --output exams/{试卷名}_听力.mp3 \
      --speed 0.9

JSONL 字段规范见同目录 ../SKILL.md。

退出码：
    0  成功（或在 --dry-run 下校验通过）
    1  运行期失败（ffmpeg 缺失、TTS 反复失败、ffprobe 对账不过等）
    2  输入格式/参数错误（坏 JSON、缺字段、未知 type、语速越界等）
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 本脚本认识的 4 种行类型。未知类型一律报错并指出行号。
KNOWN_TYPES: Tuple[str, ...] = ("instruction", "dialogue", "question", "text")

#: 默认语速（1.0 = edge-tts 原生速度，不加速不减速）。
DEFAULT_SPEED: float = 1.0

#: 允许的语速范围（含端点）。
SPEED_MIN: float = 0.5
SPEED_MAX: float = 1.5

#: 单个 pause_after 的上限（秒）。超过这个值几乎肯定是把毫秒当秒写了。
PAUSE_MAX: float = 120.0

#: repeat 的上限，防止脚本写出 repeat: 999 把服务器拖死。
REPEAT_MAX: int = 5

#: 音频规格：与 edge-tts 的输出保持一致（24kHz 单声道），这样拼接时参数完全对齐。
#: 服务器实测：edge-tts 产物就是 mp3 / 24000Hz / 单声道 / 48kbps。
#: 默认码率取 64k —— 服务器实测 `-b:a 64k` 的静音与拼接命令都可用（libmp3lame）。
DEFAULT_SAMPLE_RATE: int = 24000
DEFAULT_BITRATE: str = "64k"

#: 单段 TTS 失败后的默认重试次数（总尝试次数 = 1 + retries）。
DEFAULT_RETRIES: int = 3

#: 退避基数（秒）。第 n 次重试前等待 base * 2^(n-1) 秒。
RETRY_BASE_DELAY: float = 1.0

#: 时长对账容差 = max(DURATION_ABS_TOL, 实测值 * DURATION_REL_TOL)。
#: concat 时每段首尾可能有几毫秒的帧补齐误差，分段越多累计越大，所以给相对容差。
DURATION_ABS_TOL: float = 0.75
DURATION_REL_TOL: float = 0.03

#: speaker_id 命名规则：{角色}_{性别}_{编号}，性别只能是 male / female。
SPEAKER_ID_RE = re.compile(r"^[a-z][a-z0-9_]*_(?:male|female)_\d+$")

#: 语言推断：只要出现中日韩统一表意文字就认为需要中文音色。
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

#: 英文音色库（edge-tts 内置），按性别/角色分档。
VOICES_EN: Dict[str, str] = {
    "instruction": "en-US-AriaNeural",
    "question": "en-US-AriaNeural",
    "dialogue": "en-US-GuyNeural",
}

#: 中文音色库（edge-tts 内置，服务器已实测可列出 zh-CN-XiaoxiaoNeural 等）。
VOICES_ZH: Dict[str, str] = {
    "instruction": "zh-CN-XiaoxiaoNeural",
    "question": "zh-CN-XiaoxiaoNeural",
    "dialogue": "zh-CN-YunxiNeural",
}

#: 默认音色映射表：speaker_id -> edge-tts 音色名。
#: 播音员/题目用 speaker_female_1；对话角色按性别分编号（男女各一套，编号从 1 起）。
DEFAULT_VOICE_MAP: Dict[str, str] = {
    # --- 英语听力：播音员 / 题目（女声）---
    "speaker_female_1": "en-US-AriaNeural",
    "speaker_female_2": "en-US-JennyNeural",
    "speaker_female_3": "en-GB-SoniaNeural",
    # --- 英语听力：对话角色（男声 / 女声）---
    "speaker_male_1": "en-US-GuyNeural",
    "speaker_male_2": "en-US-DavisNeural",
    "speaker_male_3": "en-GB-RyanNeural",
    # --- 中文（如中文听力/语文朗读场景）---
    "zh_speaker_female_1": "zh-CN-XiaoxiaoNeural",
    "zh_speaker_female_2": "zh-CN-XiaoyiNeural",
    "zh_speaker_female_3": "zh-CN-XiaomoNeural",
    "zh_speaker_male_1": "zh-CN-YunxiNeural",
    "zh_speaker_male_2": "zh-CN-YunjianNeural",
    "zh_speaker_male_3": "zh-CN-YunxiaNeural",
    # --- 常见简写别名（脚本作者常这么写）---
    "narrator": "en-US-AriaNeural",
    "announcer": "en-US-AriaNeural",
    "播音员": "zh-CN-XiaoxiaoNeural",
}

#: 内容类型 -> 该类行的 "规范说话人 id"（默认表里查不到具体 id 时按此兜底）。
CANONICAL_SPEAKER: Dict[str, str] = {
    "instruction": "speaker_female_1",
    "question": "speaker_female_1",
    "dialogue": "speaker_male_1",
}


# ---------------------------------------------------------------------------
# 异常：区分"输入错误(2)"与"运行错误(1)"
# ---------------------------------------------------------------------------


class InputError(Exception):
    """输入数据/参数不合法。对应退出码 2。"""


class RunError(Exception):
    """运行环境或执行过程失败。对应退出码 1。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Segment:
    """JSONL 里的一行（原始行，还没展开 repeat）。"""

    index: int          #: 1 起的行号（就是 JSONL 文件里的行号）
    type: str           #: instruction / dialogue / question / text
    text: str
    speaker_id: Optional[str] = None
    pause_after: float = 0.0
    repeat: int = 1
    lang: Optional[str] = None
    raw: Optional[dict] = None


@dataclass
class AudioPiece:
    """展开后实际要合成/拼接的一段。"""

    seg: Segment
    occurrence: int     #: 第几遍（repeat 展开用），从 1 起
    text: str
    speaker_id: str
    voice: str
    lang: str
    pause_after: float
    #: 下面两个字段在合成/探测后回填
    path: Optional[Path] = None
    duration: Optional[float] = None


# ---------------------------------------------------------------------------
# 纯逻辑：语速 / 音色 / 语言
# ---------------------------------------------------------------------------


def speed_to_rate(speed: float) -> str:
    """把语速倍数换算成 edge-tts 的 rate 百分比字符串。

    rate = round((speed - 1) * 100)，例如 0.9 -> "-10%"、1.25 -> "+25%"、1.0 -> "+0%"。

    越界（不在 0.5~1.5）抛 InputError。非数值同样抛 InputError（由 CLI 层捕获）。
    """
    try:
        value = float(speed)
    except (TypeError, ValueError):
        raise InputError(f"--speed 必须是数字，收到的是：{speed!r}")
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        raise InputError(f"--speed 必须是有限数字，收到的是：{speed!r}")
    if value < SPEED_MIN or value > SPEED_MAX:
        raise InputError(
            f"--speed 越界：{value} 不在 {SPEED_MIN}~{SPEED_MAX} 之间。"
            f"参考：小学低年级 0.75，小学高年级 0.85，初中 0.9，高中 1.0"
        )
    percent = int(round((value - 1.0) * 100))
    sign = "+" if percent >= 0 else "-"
    return f"{sign}{abs(percent)}%"


def rate_to_speed(rate: str) -> float:
    """rate 字符串（如 "-10%"）反算回语速倍数，供 --rate 参数与测试使用。"""
    match = re.fullmatch(r"\s*([+-]?)\s*(\d+(?:\.\d+)?)\s*%\s*", str(rate))
    if not match:
        raise InputError(f"--rate 格式不合法：{rate!r}，应形如 '-10%' 或 '+0%'")
    sign = -1.0 if match.group(1) == "-" else 1.0
    return 1.0 + sign * float(match.group(2)) / 100.0


def validate_rate_string(rate: str) -> str:
    """校验 edge-tts 的 rate 字符串，越界抛 InputError。返回原字符串。"""
    speed = rate_to_speed(rate)
    if speed < SPEED_MIN or speed > SPEED_MAX:
        raise InputError(
            f"--rate 越界：{rate} 相当于语速 {speed:.3f}，超出 {SPEED_MIN}~{SPEED_MAX} 允许范围"
        )
    return f"{'+' if speed >= 1 else '-'}{abs(int(round((speed - 1) * 100)))}%"


def detect_lang(text: str, speaker_id: Optional[str] = None) -> str:
    """推断一段文本该用中文音色还是英文音色：返回 'zh' 或 'en'。

    判定顺序：说话人 id 前缀（zh_ / cn_）→ 文本里的 CJK 字符 → 默认英文。
    """
    if speaker_id:
        sid = speaker_id.strip().lower()
        if sid.startswith(("zh_", "cn_", "zh-", "cn-", "chinese")):
            return "zh"
    if CJK_RE.search(text or ""):
        return "zh"
    return "en"


def default_voice_for(speaker_id: str, lang: str, kind: str = "dialogue") -> str:
    """默认音色表查询。先查具体 id，再按 kind + 性别兜底，查不到抛 InputError。"""
    sid = (speaker_id or "").strip()
    if sid in DEFAULT_VOICE_MAP:
        return DEFAULT_VOICE_MAP[sid]
    # 大小写不敏感再试一次
    lowered = {k.lower(): v for k, v in DEFAULT_VOICE_MAP.items()}
    if sid.lower() in lowered:
        return lowered[sid.lower()]

    table = VOICES_ZH if lang == "zh" else VOICES_EN
    # 说话人 id 末尾的性别可以决定用男声还是女声
    gender = "female" if re.search(r"_female_\d+$", sid) else ("male" if re.search(r"_male_\d+$", sid) else None)
    if gender == "female":
        return table["instruction"]
    if gender == "male":
        return table["dialogue"]
    return table.get(kind, table["dialogue"])


def voice_for(
    speaker_id: str,
    lang: str,
    kind: str,
    voice_map: Optional[Dict[str, str]] = None,
) -> str:
    """解析某一行该用哪个 edge-tts 音色。

    voice_map 优先级最高（可由 --voice-map 覆盖），查不到时回落 DEFAULT_VOICE_MAP。
    speaker_id 为空时按 kind 取规范说话人（instruction/question 用 speaker_female_1）。
    """
    sid = (speaker_id or "").strip() or CANONICAL_SPEAKER.get(kind, "speaker_male_1")
    merged: Dict[str, str] = {}
    merged.update(DEFAULT_VOICE_MAP)
    if voice_map:
        merged.update(voice_map)
    if sid in merged:
        return merged[sid]
    lowered = {k.lower(): v for k, v in merged.items()}
    if sid.lower() in lowered:
        return lowered[sid.lower()]
    return default_voice_for(sid, lang, kind)


def load_voice_map(path: os.PathLike | str) -> Dict[str, str]:
    """读取 --voice-map 指向的 JSON 文件（必须是 str -> str 的对象）。"""
    p = Path(path)
    if not p.is_file():
        raise InputError(f"--voice-map 指向的文件不存在：{p}")
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise InputError(f"--voice-map 不是合法 JSON：{p}（{exc}）") from exc
    if not isinstance(data, dict):
        raise InputError(f"--voice-map 的 JSON 顶层必须是对象，收到的是 {type(data).__name__}")
    out: Dict[str, str] = {}
    for key, value in data.items():
        if key.startswith("_"):  # 允许 "_comment" 之类的注释键
            continue
        if not isinstance(value, str) or not value.strip():
            raise InputError(f"--voice-map 里 {key!r} 的值必须是非空字符串音色名，收到：{value!r}")
        out[str(key)] = value.strip()
    if not out:
        raise InputError(f"--voice-map 里没有任何有效映射：{p}")
    return out


# ---------------------------------------------------------------------------
# 纯逻辑：JSONL 解析与校验（所有报错都带行号）
# ---------------------------------------------------------------------------


def _coerce_pause_after(value: Any, lineno: int) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"第 {lineno} 行：pause_after 必须是数字（秒），收到 {value!r}")
    pause = float(value)
    if pause < 0:
        raise InputError(f"第 {lineno} 行：pause_after 不能为负数，收到 {pause}")
    if pause > PAUSE_MAX:
        raise InputError(
            f"第 {lineno} 行：pause_after={pause} 超过上限 {PAUSE_MAX} 秒，"
            f"是否把毫秒当成秒写了？答题时间一般 5.0~8.0 秒"
        )
    return pause


def _coerce_repeat(value: Any, lineno: int) -> int:
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(f"第 {lineno} 行：repeat 必须是整数，收到 {value!r}")
    if value < 1:
        raise InputError(f"第 {lineno} 行：repeat 必须 >= 1，收到 {value}")
    if value > REPEAT_MAX:
        raise InputError(f"第 {lineno} 行：repeat={value} 超过上限 {REPEAT_MAX}")
    return value


def parse_line(line: str, lineno: int) -> Optional[Segment]:
    """解析 JSONL 里的一行。空行返回 None。任何问题都抛带行号的 InputError。"""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise InputError(
            f"第 {lineno} 行：不是合法 JSON（{exc.msg}，列 {exc.colno}）。"
            f"原文前 80 字：{stripped[:80]!r}"
        ) from exc
    if not isinstance(obj, dict):
        raise InputError(f"第 {lineno} 行：JSONL 每行必须是一个对象，收到 {type(obj).__name__}")

    seg_type = obj.get("type")
    if seg_type is None:
        raise InputError(f"第 {lineno} 行：缺少必填字段 type（可选值：{'/'.join(KNOWN_TYPES)}）")
    if not isinstance(seg_type, str):
        raise InputError(f"第 {lineno} 行：type 必须是字符串，收到 {seg_type!r}")
    seg_type = seg_type.strip().lower()
    if seg_type not in KNOWN_TYPES:
        raise InputError(
            f"第 {lineno} 行：未知的 type={obj.get('type')!r}，"
            f"只支持 {'/'.join(KNOWN_TYPES)}"
        )

    text = obj.get("text")
    if text is None:
        raise InputError(f"第 {lineno} 行：缺少必填字段 text")
    if not isinstance(text, str):
        raise InputError(f"第 {lineno} 行：text 必须是字符串，收到 {type(text).__name__}")
    if not text.strip():
        raise InputError(f"第 {lineno} 行：text 是空字符串，这一段会被静默跳过，请删掉或补内容")

    speaker_id = obj.get("speaker_id")
    if speaker_id is not None:
        if not isinstance(speaker_id, str) or not speaker_id.strip():
            raise InputError(f"第 {lineno} 行：speaker_id 必须是非空字符串，收到 {speaker_id!r}")
        speaker_id = speaker_id.strip()
        if not SPEAKER_ID_RE.match(speaker_id.lower()):
            raise InputError(
                f"第 {lineno} 行：speaker_id={speaker_id!r} 不符合命名规则 "
                f"{{角色}}_{{male|female}}_{{编号}}，例如 speaker_female_1、speaker_male_2"
            )
    elif seg_type in ("dialogue", "text"):
        raise InputError(
            f"第 {lineno} 行：{seg_type} 类型必须写 speaker_id"
            f"（播音员/题目用 speaker_female_1，对话角色按性别编号）"
        )

    lang = obj.get("lang")
    if lang is not None:
        if not isinstance(lang, str) or lang.strip().lower() not in ("en", "zh", "en-us", "zh-cn"):
            raise InputError(f"第 {lineno} 行：lang 只能是 'en' 或 'zh'，收到 {lang!r}")
        lang = "zh" if lang.strip().lower().startswith("zh") else "en"

    return Segment(
        index=lineno,
        type=seg_type,
        text=text,
        speaker_id=speaker_id,
        pause_after=_coerce_pause_after(obj.get("pause_after"), lineno),
        repeat=_coerce_repeat(obj.get("repeat"), lineno),
        lang=lang,
        raw=obj,
    )


def parse_jsonl(path: os.PathLike | str) -> List[Segment]:
    """读取并校验整个 JSONL 文件。返回 Segment 列表（空行被忽略）。"""
    p = Path(path)
    if not p.is_file():
        raise InputError(f"--input 指向的文件不存在：{p}")
    if p.stat().st_size == 0:
        raise InputError(f"--input 是空文件：{p}")

    segments: List[Segment] = []
    with open(p, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            seg = parse_line(line, lineno)
            if seg is not None:
                segments.append(seg)
    if not segments:
        raise InputError(f"--input 里没有任何有效行（全是空行？）：{p}")
    return segments


def expand_segments(
    segments: Sequence[Segment],
    voice_map: Optional[Dict[str, str]] = None,
    lang_override: Optional[str] = None,
) -> List[AudioPiece]:
    """展开 repeat，解析每段的说话人、语言与音色，得到合成计划。

    repeat: 2 表示这一段连播两遍（用于"每段对话播放两遍"的紧凑写法）。
    注意：repeat 只重复"朗读内容"，行内的 pause_after 只在最后一遍之后生效一次，
    这样才不会出现"两遍之间也停 8 秒"的听感事故。
    """
    pieces: List[AudioPiece] = []
    for seg in segments:
        lang = lang_override or seg.lang or detect_lang(seg.text, seg.speaker_id)
        speaker_id = (seg.speaker_id or CANONICAL_SPEAKER.get(seg.type, "speaker_male_1"))
        voice = voice_for(speaker_id, lang, seg.type, voice_map)
        for occurrence in range(1, seg.repeat + 1):
            is_last = occurrence == seg.repeat
            pieces.append(
                AudioPiece(
                    seg=seg,
                    occurrence=occurrence,
                    text=seg.text,
                    speaker_id=speaker_id,
                    voice=voice,
                    lang=lang,
                    pause_after=seg.pause_after if is_last else 0.0,
                )
            )
    return pieces


def build_plan(
    segments: Sequence[Segment],
    voice_map: Optional[Dict[str, str]] = None,
    lang_override: Optional[str] = None,
) -> List[AudioPiece]:
    """parse + expand 的组合入口，便于测试与复用。"""
    return expand_segments(segments, voice_map=voice_map, lang_override=lang_override)


# ---------------------------------------------------------------------------
# 纯逻辑：预计时长 & ffmpeg 命令构造
# ---------------------------------------------------------------------------


def estimate_text_seconds(text: str, speed: float = 1.0, lang: Optional[str] = None) -> float:
    """按字符数估算朗读时长（秒）。

    这只是**兜底估算**，用于 --dry-run 和缺失实测时长时的补救；
    正式对账一律用各分段 mp3 的 ffprobe 实测时长累加（更准）。

    经验系数：英文 ~4.4 词/秒、平均 5.6 字符/词；中文 ~4.5 字/秒；标点额外 0.18 秒。
    """
    if not text:
        return 0.0
    lang = lang or detect_lang(text)
    if lang == "zh":
        units = float(len(text))
        per_unit = 1.0 / 4.5
    else:
        units = float(len(text)) / 5.6
        per_unit = 1.0 / 4.4
    seconds = units * per_unit
    punctuation = len(re.findall(r"[.,!?;:。，！？；：、…—\"')\]]", text))
    seconds += punctuation * 0.18
    seconds = max(seconds, 0.35)
    return seconds / max(speed, 0.01)


def silence_command(
    seconds: float,
    out_path: os.PathLike | str,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    bitrate: str = DEFAULT_BITRATE,
) -> List[str]:
    """构造"生成等长静音 mp3"的 ffmpeg 命令（anullsrc + libmp3lame）。"""
    if seconds <= 0:
        raise InputError(f"静音时长必须 > 0，收到 {seconds}")
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-f", "lavfi",
        "-i", f"anullsrc=r={sample_rate}:cl=mono",
        "-t", f"{float(seconds):.3f}",
        "-c:a", "libmp3lame",
        "-b:a", bitrate,
        "-ar", str(sample_rate),
        "-ac", "1",
        str(out_path),
    ]


def concat_command(
    list_path: os.PathLike | str,
    out_path: os.PathLike | str,
    copy_streams: bool = False,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    bitrate: str = DEFAULT_BITRATE,
) -> List[str]:
    """构造 concat demuxer 拼接命令。

    默认**重编码**（`-c:a libmp3lame -b:a 64k -ar 24000 -ac 1`）—— 这条是服务器上实测跑通的
    命令；copy_streams=True 时改用 `-c copy`（各分段参数完全一致时可行，但**必须**更严格地
    校验产物，见 concat_audio 的说明）。
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_path),
    ]
    if copy_streams:
        cmd += ["-c", "copy"]
    else:
        cmd += ["-c:a", "libmp3lame", "-b:a", bitrate, "-ar", str(sample_rate), "-ac", "1"]
    cmd.append(str(out_path))
    return cmd


def probe_command(path: os.PathLike | str) -> List[str]:
    """构造 ffprobe 取时长的命令。

    服务器实测可用形式（脚本对账用的就是它）：
        ffprobe -v error -show_entries format=duration -of csv=p=0 <文件>
    输出就是一行裸秒数，例如 `11.832`。
    """
    return [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(path),
    ]


def probe_detail_command(path: os.PathLike | str) -> List[str]:
    """构造 ffprobe 取详细信息（采样率/声道/码率）的命令，排障时用。"""
    return [
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]


def concat_list_text(paths: Iterable[os.PathLike | str]) -> str:
    """生成 concat demuxer 的清单文本。

    **格式是硬要求**（服务器实测坑）：每一行都必须是 `file '<路径>'`，
    带 `file ` 前缀与引号。写裸路径时 ffmpeg 报 `Line 1: unknown keyword`，
    而且**退出码不一定是非零、还会留下 0 字节的输出文件**，所以调用方必须再校验产物。
    路径写成绝对路径；单个引号按 ffmpeg 规则转义为 `'\\''`。
    """
    lines = []
    for raw in paths:
        resolved = Path(raw).resolve()
        escaped = str(resolved).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    return "\n".join(lines) + "\n"


def duration_tolerance(expected: float) -> float:
    """时长对账的允许偏差。"""
    return max(DURATION_ABS_TOL, abs(expected) * DURATION_REL_TOL)


# ---------------------------------------------------------------------------
# 运行环境探测
# ---------------------------------------------------------------------------


def which_ffmpeg() -> Optional[str]:
    """找 ffmpeg 可执行文件；找不到返回 None。"""
    return shutil.which("ffmpeg")


def which_ffprobe() -> Optional[str]:
    """找 ffprobe 可执行文件；找不到返回 None。"""
    return shutil.which("ffprobe")


def has_edge_tts() -> bool:
    """检查 edge-tts 是否可导入（不导入，只看能否找到模块）。"""
    try:
        return importlib.util.find_spec("edge_tts") is not None
    except (ImportError, ValueError):
        return False


def check_environment() -> None:
    """合成前的可用性检测。缺东西一律中文报错 + 非零退出，绝不静默产出半成品。"""
    missing = []
    if not which_ffmpeg():
        missing.append("ffmpeg")
    if not which_ffprobe():
        missing.append("ffprobe")
    if missing:
        raise RunError(
            "缺少系统命令：" + "、".join(missing) + "。\n"
            "本技能用 ffmpeg 生成停顿静音并拼接音频，用 ffprobe 做时长对账，二者缺一不可（不能降级）。\n"
            "OpenCloudOS 9 安装命令（ffmpeg 来自 EPOL 仓库）：\n"
            "    sudo dnf install -y epel-release\n"
            "    sudo dnf install -y ffmpeg ffmpeg-free\n"
            "装好后验证：\n"
            "    ffmpeg -version && ffprobe -version"
        )
    if not has_edge_tts():
        raise RunError(
            "Python 无法导入 edge_tts（本技能用它合成语音）。\n"
            "本技能只使用已装好的 edge-tts，不自行安装依赖。请先确认：\n"
            "    python3 -c \"import edge_tts, sys; print(edge_tts.__file__)\"\n"
            "若报 ModuleNotFoundError，说明当前 python3 环境里没有 edge-tts，"
            "请用装了 edge-tts 的那个解释器调用本脚本。"
        )


def run_command(cmd: Sequence[str], what: str) -> None:
    """执行外部命令，失败时抛出带 stderr 原文的 RunError。"""
    try:
        proc = subprocess.run(list(cmd), capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RunError(f"{what} 执行失败：{exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RunError(f"{what} 执行失败（退出码 {proc.returncode}）：\n{detail[:2000]}\n命令：{' '.join(cmd)}")


def probe_media(path: os.PathLike | str) -> Dict[str, Any]:
    """用 ffprobe 取媒体信息（JSON）。"""
    cmd = probe_command(path)
    try:
        proc = subprocess.run(list(cmd), capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RunError(f"ffprobe 执行失败：{exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RunError(f"ffprobe 无法解析音频 {path}（退出码 {proc.returncode}）：\n{detail[:800]}")
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RunError(f"ffprobe 输出不是合法 JSON（{exc}）：{proc.stdout[:400]!r}") from exc


def media_duration(path: os.PathLike | str) -> float:
    """取音频时长（秒）。用 `ffprobe -show_entries format=duration -of csv=p=0`。

    先解析这一行裸秒数；如果它给不出（个别容器只在 stream 上有时长），
    再退回 JSON 形式从 stream 里捞。两条都拿不到就报 RunError。
    """
    cmd = probe_command(path)
    try:
        proc = subprocess.run(list(cmd), capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RunError(f"ffprobe 执行失败：{exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RunError(f"ffprobe 无法解析音频 {path}（退出码 {proc.returncode}）：\n{detail[:800]}")

    text = (proc.stdout or "").strip().splitlines()
    if text:
        try:
            value = float(text[0].strip())
            if value > 0:
                return value
        except ValueError:
            pass

    # 兜底：换 JSON 形式再取一次（有些容器 format.duration 是 N/A）
    info = probe_media(path)
    fmt = info.get("format") or {}
    raw = fmt.get("duration")
    if raw is None:
        for stream in info.get("streams") or []:
            if stream.get("duration") is not None:
                raw = stream["duration"]
                break
    if raw is None:
        raise RunError(f"ffprobe 拿不到 {path} 的时长（文件损坏或不是音频？）")
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise RunError(f"ffprobe 返回的时长不是数字：{raw!r}") from exc


# ---------------------------------------------------------------------------
# 合成：TTS 与静音
# ---------------------------------------------------------------------------


async def _synthesize_async(text: str, voice: str, rate: str, out_path: Path) -> None:
    """edge-tts 异步合成一段音频到 out_path。

    用**流式 API**逐块收音频并自己写文件（服务器实测这条路径可用且很快：
    英文 `en-US-AriaNeural` 约 0.3s/条）。产物是 mp3 / 24000Hz / 单声道 / 48kbps。
    空产物视为失败 —— 否则会静默拼出一段没有声音的音频。
    """
    import edge_tts  # 延迟导入：没装 edge-tts 时也能跑 --dry-run 和单元测试

    communicate = edge_tts.Communicate(text, voice, rate=rate)
    written = 0
    with open(out_path, "wb") as fh:
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                fh.write(chunk["data"])
                written += len(chunk["data"])
    if written == 0:
        raise RuntimeError(f"edge-tts 没有返回任何音频数据（voice={voice}, text={text[:40]!r}）")


def synthesize_one(text: str, voice: str, rate: str, out_path: Path) -> None:
    """合成单段音频（同步接口，内部驱动 asyncio）。

    边界函数：单元测试/集成测试可以通过替换本函数来避免联网
    （见 tests/test_skill_audio.py 与 --dry-run）。
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_synthesize_async(text, voice, rate, out_path))
    else:  # 已经在事件循环里（例如被别的异步程序 import 调用）
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_synthesize_async(text, voice, rate, out_path))
        finally:
            loop.close()


def synthesize_with_retry(
    piece: AudioPiece,
    rate: str,
    out_path: Path,
    retries: int = DEFAULT_RETRIES,
    base_delay: float = RETRY_BASE_DELAY,
    synthesizer: Callable[[str, str, str, Path], None] = synthesize_one,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """带指数退避重试的单段合成。最终失败时指出**行号**和**文本前 60 字**。"""
    last_error: Optional[BaseException] = None
    attempts = max(1, retries + 1)
    for attempt in range(1, attempts + 1):
        try:
            synthesizer(piece.text, piece.voice, rate, out_path)
            return
        except Exception as exc:  # noqa: BLE001 - 任何失败都要重试并汇报
            last_error = exc
            if attempt < attempts:
                delay = base_delay * (2 ** (attempt - 1))
                print(
                    f"[重试] 第 {piece.seg.index} 行（{piece.seg.type}，音色 {piece.voice}）"
                    f"第 {attempt} 次合成失败：{exc}；{delay:.1f} 秒后重试",
                    file=sys.stderr,
                )
                sleep(delay)
    preview = piece.text[:60].replace("\n", " ")
    suffix = "…" if len(piece.text) > 60 else ""
    raise RunError(
        f"第 {piece.seg.index} 行的语音合成连续失败 {attempts} 次，已放弃。\n"
        f"  类型 type={piece.seg.type}，音色 voice={piece.voice}，语速 rate={rate}\n"
        f"  文本：{preview}{suffix}\n"
        f"  最后一次错误：{last_error}\n"
        f"  排查：1) 服务器能否访问 edge-tts 的在线服务（合成需要联网）；"
        f"2) 音色名 {piece.voice} 是否存在（python3 -m edge_tts --list-voices）；"
        f"3) 把该行拆短或换个音色再试。"
    )


def generate_silence(
    seconds: float,
    out_path: Path,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    bitrate: str = DEFAULT_BITRATE,
) -> None:
    """用 ffmpeg anullsrc 生成等长静音 mp3。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run_command(silence_command(seconds, out_path, sample_rate, bitrate), "生成静音段")


def concat_audio(
    pieces: Sequence[Path],
    out_path: Path,
    workdir: Path,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    bitrate: str = DEFAULT_BITRATE,
) -> None:
    """用 ffmpeg concat demuxer 把若干 mp3 拼成一个 mp3（默认重编码）。

    为什么默认重编码：服务器实测的命令是
    `ffmpeg -f concat -safe 0 -i list.txt -c:a libmp3lame -b:a 64k -ar 24000 -ac 1 -y out.mp3`，
    统一参数、结果可控。`-c copy` 只有在各分段参数完全一致时才安全，且失败时
    ffmpeg 可能仍返回 0 并留下 0 字节文件 —— 所以这里**两条路径都**在返回后校验产物
    （存在、非 0 字节），拿不到可用文件就换另一条路径重试，都失败才报错。
    """
    if not pieces:
        raise RunError("没有任何音频分段可以拼接（合成计划为空）")
    list_path = workdir / "concat_list.txt"
    list_path.write_text(concat_list_text(pieces), encoding="utf-8")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    errors: List[str] = []
    for copy_streams, what in ((False, "重编码"), (True, "直接拷贝流")):
        if out_path.exists():
            try:
                out_path.unlink()
            except OSError:
                pass
        try:
            run_command(
                concat_command(
                    list_path, out_path, copy_streams=copy_streams,
                    sample_rate=sample_rate, bitrate=bitrate,
                ),
                f"拼接音频（{what}）",
            )
        except RunError as exc:
            errors.append(f"{what}：{exc}")
            continue
        # 关键：不看退出码就下结论。实测 ffmpeg 有可能"成功"退出却产出 0 字节文件。
        if out_path.is_file() and out_path.stat().st_size > 0:
            return
        errors.append(f"{what}：ffmpeg 返回成功，但产物缺失或为 0 字节（{out_path}）")

    raise RunError(
        "拼接音频失败，两种方式都没产出可用文件：\n  " + "\n  ".join(errors) +
        "\n  排查：确认 concat 清单每行都是 `file '<绝对路径>'`（裸路径会报 unknown keyword）；"
        "确认分段文件都存在且非空；可加 --keep-temp 保留分段逐个 ffprobe。"
    )



# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def plan_summary(
    pieces: Sequence[AudioPiece],
    speed: float,
    rate: str,
) -> Dict[str, Any]:
    """--dry-run 与正式运行共用的计划摘要（纯文本时长估算）。"""
    est_audio = sum(estimate_text_seconds(p.text, speed, p.lang) for p in pieces)
    pause_total = sum(p.pause_after for p in pieces)
    return {
        "行数": len({p.seg.index for p in pieces}),
        "分段数": len(pieces),
        "语速": speed,
        "rate": rate,
        "预计音频时长": round(est_audio, 3),
        "停顿总时长": round(pause_total, 3),
        "预计总时长": round(est_audio + pause_total, 3),
        "展开明细": [
            {
                "行号": p.seg.index,
                "类型": p.seg.type,
                "第几遍": p.occurrence,
                "speaker_id": p.speaker_id,
                "voice": p.voice,
                "语言": p.lang,
                "停顿秒": round(p.pause_after, 3),
                "文本前40字": p.text[:40],
            }
            for p in pieces
        ],
    }


def run(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        speed = args.speed
        if args.rate:
            validate_rate_string(args.rate)
            rate = args.rate
            speed = rate_to_speed(args.rate)
        else:
            rate = speed_to_rate(speed)
        if not (SPEED_MIN <= speed <= SPEED_MAX):
            raise InputError(
                f"语速 {speed} 越界，允许范围 {SPEED_MIN}~{SPEED_MAX}"
            )

        segments = parse_jsonl(args.input)
        voice_map = load_voice_map(args.voice_map) if args.voice_map else None
        pieces = build_plan(segments, voice_map=voice_map, lang_override=args.lang)
    except InputError as exc:
        print(f"[输入错误] {exc}", file=sys.stderr)
        return 2

    summary = plan_summary(pieces, speed, rate)

    if args.dry_run:
        summary["模式"] = "dry-run（未合成、未联网、未调用 ffmpeg）"
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    try:
        check_environment()
    except RunError as exc:
        print(f"[环境错误] {exc}", file=sys.stderr)
        return 1

    output = Path(args.output)
    tmp_root = args.keep_temp if args.keep_temp else tempfile.mkdtemp(prefix="listening-audio-")
    workdir = Path(tmp_root)
    workdir.mkdir(parents=True, exist_ok=True)
    # 实测坑：部分环境下 mkdtemp 建出来的目录权限是 000（Windows 上曾复现写入被拒），
    # 这里显式补一次权限，保证后续写分段文件不会 EACCES。
    try:
        os.chmod(workdir, 0o700)
    except OSError:
        pass

    audio_measured = 0.0
    pause_measured = 0.0
    actual = 0.0
    expected = 0.0
    size = 0

    try:
        # 1) 逐段合成（文本走 edge-tts；停顿在下一步用 ffmpeg 静音补）
        for i, piece in enumerate(pieces):
            path = workdir / f"seg_{i:04d}.mp3"
            synthesize_with_retry(
                piece, rate, path, retries=args.retries, synthesizer=synthesize_one
            )
            piece.path = path

        # 2) 生成停顿静音段（每段 pause_after 之后插一段等长静音；不用朗读空格凑停顿）
        ordered: List[Path] = []
        for i, piece in enumerate(pieces):
            assert piece.path is not None
            ordered.append(piece.path)
            if piece.pause_after > 0:
                sil = workdir / f"pause_{i:04d}.mp3"
                generate_silence(piece.pause_after, sil, args.sample_rate, args.bitrate)
                ordered.append(sil)

        # 3) 用 ffprobe 实测每个分段时长（比字符估算准，用于对账）
        for piece in pieces:
            assert piece.path is not None
            piece.duration = media_duration(piece.path)
            audio_measured += piece.duration
        pause_measured = sum(p.pause_after for p in pieces)

        # 4) 拼接
        concat_audio(ordered, output, workdir, args.sample_rate, args.bitrate)

        # 5) 自检产物：存在、非空、时长对账
        if not output.is_file():
            raise RunError(f"拼接后没有找到输出文件：{output}")
        size = output.stat().st_size
        if size == 0:
            raise RunError(f"输出文件是 0 字节（合成没有真正产出音频）：{output}")

        actual = media_duration(output)
        expected = audio_measured + pause_measured
        tol = duration_tolerance(expected)
        if actual <= 0:
            raise RunError(f"输出文件时长为 0（{output}），产物不可用")
        if abs(actual - expected) > tol and not args.allow_duration_mismatch:
            raise RunError(
                f"时长对账不通过：实测 {actual:.3f} 秒，各分段实测累加 {expected:.3f} 秒，"
                f"偏差 {abs(actual - expected):.3f} 秒 > 允许 {tol:.3f} 秒。\n"
                f"  音频段 {len(pieces)} 段，共 {audio_measured:.3f} 秒；停顿 {pause_measured:.3f} 秒。\n"
                f"  产物可能不完整，已按失败处理。若确实可以接受，加 --allow-duration-mismatch 重跑。"
            )
    except RunError as exc:
        print(f"[执行错误] {exc}", file=sys.stderr)
        return 1
    finally:
        if not args.keep_temp:
            shutil.rmtree(workdir, ignore_errors=True)

    summary.update(
        {
            "模式": "正式合成",
            "音频总时长": round(audio_measured, 3),
            "停顿总时长": round(pause_measured, 3),
            "对账实测总时长": round(actual, 3),
            "对账预计总时长": round(expected, 3),
            "对账偏差秒": round(actual - expected, 3),
            "输出文件": str(output.resolve()),
            "输出文件大小": size,
            "输出文件大小可读": human_size(size),
            "采样率": args.sample_rate,
            "码率": args.bitrate,
        }
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def human_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.2f} GB"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="generate_audio.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "把 JSONL 听力脚本合成为一个听力 mp3（edge-tts 朗读 + ffmpeg 静音/拼接）。\n"
            "JSONL 字段规范见同目录上一级的 SKILL.md。"
        ),
        epilog=(
            "示例：\n"
            "  python3 skills/listening-audio-generator/scripts/generate_audio.py \\\n"
            "    --input tmp/listening_script.jsonl \\\n"
            "    --output exams/期中英语_听力.mp3 \\\n"
            "    --speed 0.9\n\n"
            "只看合成计划不联网（校验 JSONL 用）：\n"
            "  加上 --dry-run\n\n"
            "语速参考：小学低年级 0.75，小学高年级 0.85，初中 0.9，高中 1.0"
        ),
    )
    parser.add_argument("--input", "-i", required=True, help="输入 JSONL 听力脚本路径（相对工作区根，如 tmp/listening_script.jsonl）")
    parser.add_argument("--output", "-o", required=True, help="输出 mp3 路径（相对工作区根，如 exams/xxx_听力.mp3）；目录不存在会自动创建")
    parser.add_argument(
        "--speed",
        type=float,
        default=DEFAULT_SPEED,
        help=f"语速倍数，{SPEED_MIN}~{SPEED_MAX}，默认 {DEFAULT_SPEED}（rate = round((speed-1)*100)%%）",
    )
    parser.add_argument("--rate", default=None, help="直接指定 edge-tts 的 rate（如 '-10%%'），与 --speed 二选一，--rate 优先")
    parser.add_argument("--lang", choices=["en", "zh"], default=None, help="强制全部段落使用该语言的音色（默认按键名与文本自动判断）")
    parser.add_argument("--voice-map", default=None, help="音色映射 JSON 文件，覆盖内置 speaker_id -> voice 表")
    parser.add_argument("--sample-rate", type=int, default=DEFAULT_SAMPLE_RATE, help=f"输出采样率，默认 {DEFAULT_SAMPLE_RATE}")
    parser.add_argument("--bitrate", default=DEFAULT_BITRATE, help=f"mp3 码率，默认 {DEFAULT_BITRATE}（可用 64k~128k）")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help=f"单段 TTS 失败后的重试次数，默认 {DEFAULT_RETRIES}（指数退避）")
    parser.add_argument("--dry-run", action="store_true", help="只解析校验并打印合成计划，不联网、不调 ffmpeg、不产出文件")
    parser.add_argument("--keep-temp", default=None, help="保留中间分段 mp3 到该目录（排障用）")
    parser.add_argument("--allow-duration-mismatch", action="store_true", help="时长对账超差时仍然算成功（默认超差即失败）")
    return parser


def main() -> int:
    """命令行入口：把意外异常也收敛成中文报错 + 非零退出码（不留英文堆栈给老师）。"""
    try:
        return run()
    except InputError as exc:
        print(f"[输入错误] {exc}", file=sys.stderr)
        return 2
    except RunError as exc:
        print(f"[执行错误] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("[中断] 收到 Ctrl-C，已停止（未完成的输出文件不可用）", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - 兜底：不能把英文堆栈直接丢给老师
        print(
            f"[未预期错误] {type(exc).__name__}: {exc}\n"
            f"  这是脚本没有预料到的失败，请把上面这行原文贴回工单。\n"
            f"  常见原因：临时目录不可写、磁盘写满、edge-tts 版本接口变化。",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
