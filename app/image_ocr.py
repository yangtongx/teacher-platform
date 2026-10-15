"""图片和扫描页的识别。

先分出版面块，再在块里按行识别。中文行用 RapidOCR。
一行几乎全是英文且置信度低时，改用 TrOCR 重认这一行。
栏内从上到下，左栏读完再读右栏。
"""

from __future__ import annotations

import threading

MAX_SIDE = 2000
ENGLISH_SCORE = 0.85
KEEP_HINTS = ("text", "title", "header", "footer", "table", "caption", "list", "reference", "paragraph")
SKIP_HINTS = ("figure", "image", "seal", "formula", "equation", "abandon")

_lock = threading.Lock()
_layout_engine = None
_rapid_engine = None
_trocr = None


def keep_kind(name: str) -> bool:
    kind = (name or "").lower().replace("-", " ").replace("_", " ")
    if any(hint in kind for hint in SKIP_HINTS):
        return False
    return any(hint in kind for hint in KEEP_HINTS)


def needs_english_repair(text: str, score: float) -> bool:
    """中文行留着。英文行只有没认准时才交给 TrOCR。"""
    letters = [char for char in text if char.isalpha()]
    if len(letters) < 4 or any("\u4e00" <= char <= "\u9fff" for char in text):
        return False
    latin = sum(1 for char in letters if "a" <= char.lower() <= "z")
    return latin / len(letters) >= 0.8 and score < ENGLISH_SCORE


def order_blocks(blocks: list[dict], page_width: float) -> list[dict]:
    """单栏按从上到下。左右两栏先读完左栏，再读右栏。"""
    usable = [block for block in blocks if keep_kind(block.get("kind") or "text")]
    if not usable:
        return []
    mid = page_width / 2
    left = [block for block in usable if (block["x0"] + block["x1"]) / 2 < mid]
    right = [block for block in usable if (block["x0"] + block["x1"]) / 2 >= mid]

    def by_position(items: list[dict]) -> list[dict]:
        return sorted(items, key=lambda block: (block["y0"], block["x0"]))

    if left and right and min(block["x0"] for block in right) >= max(block["x1"] for block in left):
        return by_position(left) + by_position(right)
    return by_position(usable)


def read_bytes(data: bytes) -> str:
    return read_image(_decode(data))


def read_image(image) -> str:
    image = _limit(image)
    try:
        blocks = _layout_blocks(image)
    except ImportError as exc:
        raise ValueError("服务器还不能识别图片里的文字") from exc
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("这张图片没有识别出来") from exc
    height, width = image.shape[:2]
    if not blocks:
        blocks = [{"x0": 0, "y0": 0, "x1": width, "y1": height, "kind": "text"}]
    chunks = []
    for block in order_blocks(blocks, width):
        crop = image[block["y0"] : block["y1"], block["x0"] : block["x1"]]
        if crop.size == 0:
            continue
        lines = _recognize_crop(crop, block["x0"], block["y0"])
        lines.sort(key=lambda item: (item["y0"], item["x0"]))
        texts = [_maybe_repair(image, line) for line in lines]
        joined = "\n".join(text for text in texts if text)
        if joined:
            chunks.append(joined)
    return "\n".join(chunks)


def _decode(data: bytes):
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise ValueError("服务器还不能识别图片里的文字") from exc
    try:
        from io import BytesIO

        with Image.open(BytesIO(data)) as img:
            array = np.array(img.convert("RGB"))
    except Exception as exc:
        raise ValueError("这张图片读不出来") from exc
    return array[:, :, ::-1]


def _limit(image):
    height, width = image.shape[:2]
    longest = max(height, width)
    if longest <= MAX_SIDE:
        return image
    step = max(1, int(longest / MAX_SIDE))
    return image[::step, ::step]


def _layout_blocks(image) -> list[dict]:
    result = _layout()(image)
    boxes = getattr(result, "boxes", None) or []
    names = getattr(result, "class_names", None) or []
    blocks = []
    height, width = image.shape[:2]
    for box, name in zip(boxes, names):
        if not keep_kind(str(name)):
            continue
        x0, y0, x1, y1 = (int(value) for value in box[:4])
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(width, x1), min(height, y1)
        if x1 - x0 < 8 or y1 - y0 < 8:
            continue
        blocks.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "kind": str(name)})
    return blocks


def _recognize_crop(crop, origin_x: int, origin_y: int) -> list[dict]:
    try:
        result = _rapid()(crop)
    except ImportError as exc:
        raise ValueError("服务器还不能识别图片里的文字") from exc
    except Exception as exc:
        raise ValueError("这张图片没有识别出来") from exc
    return _lines(result, origin_x, origin_y)


def _lines(result, origin_x: int, origin_y: int) -> list[dict]:
    if result is None:
        return []
    texts = getattr(result, "txts", None)
    scores = getattr(result, "scores", None)
    boxes = getattr(result, "boxes", None)
    if texts is None or boxes is None:
        return []
    found = []
    for box, text, score in zip(boxes, texts, scores or []):
        cleaned = str(text or "").strip()
        if not cleaned:
            continue
        points = list(box)
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        found.append(
            {
                "text": cleaned,
                "score": float(score),
                "x0": origin_x + int(min(xs)),
                "y0": origin_y + int(min(ys)),
                "x1": origin_x + int(max(xs)),
                "y1": origin_y + int(max(ys)),
            }
        )
    return found


def _maybe_repair(image, line: dict) -> str:
    if not needs_english_repair(line["text"], line["score"]):
        return line["text"]
    height, width = image.shape[:2]
    x0 = max(0, line["x0"] - 2)
    y0 = max(0, line["y0"] - 2)
    x1 = min(width, line["x1"] + 2)
    y1 = min(height, line["y1"] + 2)
    crop = image[y0:y1, x0:x1]
    if crop.size == 0:
        return line["text"]
    repaired = _trocr_line(crop)
    return repaired or line["text"]


def _layout():
    global _layout_engine
    if _layout_engine is None:
        from rapid_layout import RapidLayout

        with _lock:
            if _layout_engine is None:
                _layout_engine = RapidLayout()
    return _layout_engine


def _rapid():
    global _rapid_engine
    if _rapid_engine is None:
        from rapidocr import RapidOCR

        with _lock:
            if _rapid_engine is None:
                _rapid_engine = RapidOCR()
    return _rapid_engine


def _trocr_line(crop) -> str:
    global _trocr
    try:
        import torch
        from PIL import Image
        from transformers import TrOCRProcessor, VisionEncoderDecoderModel
    except ImportError:
        return ""
    with _lock:
        if _trocr is None:
            name = "microsoft/trocr-base-printed"
            processor = TrOCRProcessor.from_pretrained(name)
            model = VisionEncoderDecoderModel.from_pretrained(name)
            model.eval()
            _trocr = (processor, model)
        processor, model = _trocr
    array = crop[:, :, ::-1] if getattr(crop, "ndim", 0) == 3 and crop.shape[2] == 3 else crop
    image = Image.fromarray(array)
    pixels = processor(images=image, return_tensors="pt").pixel_values
    with torch.no_grad():
        ids = model.generate(pixels, max_new_tokens=64)
    return processor.batch_decode(ids, skip_special_tokens=True)[0].strip()
