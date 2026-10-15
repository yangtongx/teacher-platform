"""课件样式记在老师账号上，不跟某一段对话走。

服务器上的科目样式来自 Apollo's Templates 教育分类，免费供教学使用。
每个科目只用一种颜色：语文鼠尾草绿、数学石板蓝、英语天蓝，其余用森林绿。
同一页下载的 Pencil Box 也放在模板目录里，生成时不选用，避免一个科目好几套样子。
老师上传的课件只属于这位老师。只有老师说「以后这门课都用这个样式」时，
才把这份上传记成他以后的样式，不替换服务器上给所有人用的科目模板。
"""

from __future__ import annotations

import shutil
import zipfile
from io import BytesIO
from pathlib import Path

from app import db
from app.config import DATA_DIR, ROOT, THREAD
from app.corpus import SUBJECT_NAMES

TEMPLATE_DIR = ROOT / "ppt_templates"

# 模型只报科目名，不接触文件路径。
SUBJECT_FILES = {
    "语文": "powerpoint-coursebook-sage.potx",
    "数学": "powerpoint-coursebook-slate.potx",
    "英语": "powerpoint-coursebook-sky.potx",
    "通用": "powerpoint-coursebook-forest.potx",
}

CHOOSE_SUBJECT = "用这个科目的课件样式"
CHOOSE_UPLOAD = "用我刚上传的那份课件样式"


class StyleNeeded(Exception):
    """还不能导出，要先问老师或先确认上一回的样式。"""


def template_file(subject: str) -> Path:
    name = SUBJECT_FILES.get(subject) or SUBJECT_FILES["通用"]
    return TEMPLATE_DIR / name


def describe_style(user, conversation_id: int) -> str:
    """告诉模型现在该直接做、问一次，还是只确认一句。"""
    pref = db.ppt_pref(int(user["id"]))
    upload = db.latest_ppt_upload(int(user["id"]), conversation_id)
    subject = resolve_subject(user, "")
    if pref and _pref_ready(pref, int(user["id"])):
        label = _label(pref)
        return (
            "请确认。用 AskUser 只问这一句："
            f"这节课仍用你上次的课件样式（{label}）。"
            "选项只能是：可以、我要换。"
            "老师说可以，导出时 use 填 keep。"
            "老师说换，再问一次，选项只能是"
            f"「{CHOOSE_SUBJECT}」和「{CHOOSE_UPLOAD}」。"
            "选前者 use 填 subject，选后者 use 填 upload。"
            "不要把样式说成助手自己做的。不要写文件路径。"
        )
    if upload:
        return (
            "请选择。用 AskUser 问老师用哪一种。"
            f"选项只能是：「{CHOOSE_SUBJECT}」和「{CHOOSE_UPLOAD}」。"
            f"选前者导出时 use 填 subject，科目是{subject}。"
            "选后者 use 填 upload。"
            "不要把样式说成助手自己做的。不要写文件路径。"
        )
    return (
        f"直接用{subject}课件样式。导出时 use 填 subject。不必再问样式。"
        "不要把样式说成助手自己做的。"
    )


def prepare_export(user, conversation_id: int, use: str, subject: str) -> tuple[Path, str]:
    """按老师这次的选择找出模板文件。需要先问或先确认时抛出 StyleNeeded。"""
    user_id = int(user["id"])
    chosen = (use or "").strip()
    pref = db.ppt_pref(user_id)
    upload = db.latest_ppt_upload(user_id, conversation_id)
    if chosen == "keep":
        if pref is None or not _pref_ready(pref, user_id):
            raise StyleNeeded("还没有保存过课件样式。请先用科目样式。")
        return _path_for(pref, user_id), _cover_subject(pref, user)
    if chosen == "upload":
        if upload is None:
            raise StyleNeeded("这段对话里没有课件文件。请上传 .pptx，这次改用科目样式。")
        dest = _personal_path(user_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_upload_path(upload), dest)
        db.set_ppt_pref(user_id, "upload", "")
        return dest, _cover_subject({"style": "upload", "subject": ""}, user)
    if chosen == "subject":
        name = resolve_subject(user, subject)
        db.set_ppt_pref(user_id, "subject", name)
        return template_file(name), name
    if pref and _pref_ready(pref, user_id):
        raise StyleNeeded(
            "请先向老师确认："
            f"这节课仍用你上次的课件样式（{_label(pref)}）。"
            "确认后 use 填 keep。"
        )
    if upload is not None:
        raise StyleNeeded(
            "请先问老师：用这个科目的课件样式，还是用刚上传的那份课件样式。"
        )
    name = resolve_subject(user, subject)
    db.set_ppt_pref(user_id, "subject", name)
    return template_file(name), name


def resolve_subject(user, requested: str) -> str:
    """老师这节课点了名就用那一科，否则用账号上的科目，对不上就用通用。"""
    asked = _known_subject(requested)
    if asked:
        return asked
    account = _known_subject(str(user["subject"] or ""))
    if account and account != "通用":
        return account
    raw = str(user["subject"] or "").strip()
    if raw in SUBJECT_NAMES:
        mapped = SUBJECT_NAMES[raw]
        if mapped in SUBJECT_FILES and mapped != "通用":
            return mapped
    return "通用"


def store_pptx(user_id: int, filename: str, data: bytes) -> None:
    """把上传的课件存下来，只当样式，不把正文交给识别。"""
    if not _is_pptx(data):
        raise ValueError("这不是课件文件。请上传 .pptx。")
    text = (
        "【课件样式】老师上传的课件，只提供版式、颜色和字体。"
        "不要把里面的旧课文抄进新课件。图片和 PDF 不能当课件样式。"
    )
    upload_id = db.add_upload(user_id, THREAD, filename, text)
    stored = f"{upload_id}.pptx"
    folder = DATA_DIR / "ppt-uploads"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / stored).write_bytes(data)
    db.remember_ppt_upload(user_id, upload_id, stored)


def _is_pptx(data: bytes) -> bool:
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile:
        return False
    names = archive.namelist()
    if len(names) > 400:
        return False
    return "ppt/presentation.xml" in names


def _known_subject(value: str) -> str:
    text = (value or "").strip()
    if text in SUBJECT_NAMES:
        text = SUBJECT_NAMES[text]
    if text in SUBJECT_FILES:
        return text
    return ""


def _pref_ready(pref: dict, user_id: int) -> bool:
    if pref.get("style") == "subject":
        return template_file(pref.get("subject") or "通用").is_file()
    if pref.get("style") == "upload":
        return _personal_path(user_id).is_file()
    return False


def _path_for(pref: dict, user_id: int) -> Path:
    if pref.get("style") == "upload":
        return _personal_path(user_id)
    return template_file(pref.get("subject") or "通用")


def _label(pref: dict) -> str:
    if pref.get("style") == "upload":
        return "你上传的课件"
    subject = pref.get("subject") or "通用"
    return f"{subject}课件样式"


def _cover_subject(pref: dict, user) -> str:
    if pref.get("style") == "subject":
        return pref.get("subject") or "通用"
    return resolve_subject(user, "")


def _personal_path(user_id: int) -> Path:
    return DATA_DIR / "ppt-styles" / f"{user_id}.pptx"


def _upload_path(stored: str) -> Path:
    return DATA_DIR / "ppt-uploads" / Path(stored).name
