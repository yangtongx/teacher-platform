#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""read_sheet.py - 读取飞书电子表格 / 多维表格，或导入本地成绩文件。

对应 workspaces/office/AGENTS.md 的「流程1：读取数据」：

  方式A 飞书表格链接：解析类型(sheets/base) -> 调用 API -> 存入 data/ -> 展示摘要
  方式B 上传成绩文档：Excel/CSV -> 解析文件名获取考试时间 -> 存入 data/grades/{日期}.xlsx

输出统一写成语义固定的 xlsx：首行表头（保留原始列名），一行一个学生。
数据只落在工作区的 data/ 目录里，不会发往任何外部服务。

用法示例（cwd = 工作区根目录）：
  python3 skills/hwc-teacher-assistant/scripts/read_sheet.py --check --url https://xxx.feishu.cn/sheets/shtcnXXXX
  python3 skills/hwc-teacher-assistant/scripts/read_sheet.py --url https://xxx.feishu.cn/base/bascnXXXX
  python3 skills/hwc-teacher-assistant/scripts/read_sheet.py --local ~/uploads/期中成绩2026-04-10.xlsx
  python3 skills/hwc-teacher-assistant/scripts/read_sheet.py --local 月考.csv --date 2026-04-10

退出码：0 成功 / 2 输入不合法 / 3 缺少凭据或依赖 / 4 飞书接口或网络失败。
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from datetime import date as _date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# 飞书开放平台域名。私有化部署可用环境变量覆盖。
BASE_URL = os.environ.get("FEISHU_BASE_URL", "https://open.feishu.cn").rstrip("/")
# 所有 HTTP 调用统一超时（秒）。
TIMEOUT = 15

EXIT_OK = 0
EXIT_ARG = 2
EXIT_CRED = 3
EXIT_API = 4

KIND_LABELS = {"sheets": "电子表格", "base": "多维表格"}


class SkillError(Exception):
    """本技能的可预期错误，exit_code 决定进程退出码。"""

    exit_code = EXIT_ARG


class InputError(SkillError):
    exit_code = EXIT_ARG


class MissingDependencyError(SkillError):
    exit_code = EXIT_CRED


class MissingCredentialError(SkillError):
    exit_code = EXIT_CRED


class FeishuAPIError(SkillError):
    exit_code = EXIT_API


def _import_dependencies():
    """延迟导入外部依赖，缺库时给出中文诊断而不是 ImportError 堆栈。"""
    missing = []
    try:
        import requests  # noqa: F401
    except ImportError:
        missing.append("requests")
    try:
        from openpyxl import Workbook, load_workbook  # noqa: F401
    except ImportError:
        missing.append("openpyxl")
    if missing:
        raise MissingDependencyError(
            "缺少 Python 依赖：" + "、".join(missing) + "\n"
            "服务器上应已安装 openpyxl 3.1.2 / requests 2.32.3；"
            "如缺失请联系维护者安装（技能自身不安装任何包）。"
        )


# --------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------


def clean_text(value) -> str:
    """把单元格值转成去空白的字符串；None 与空白统一成空串。"""
    if value is None:
        return ""
    return str(value).strip()


def coerce_number(value):
    """明显是数字的字符串转成 int/float，其余原样返回；空值返回 None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if not text:
        return None
    compact = text.replace(",", "").replace("，", "")
    try:
        if re.fullmatch(r"[+-]?\d+", compact):
            return int(compact)
        return float(compact)
    except ValueError:
        return text


# 这些列是标识符，必须保持文本：学号 "0123" 一旦转成数字就丢了前导零。
IDENTIFIER_HINTS = ("学号", "考号", "准考证号", "学籍号", "座号", "编号", "手机", "电话", "身份证")


def is_identifier_column(name: str) -> bool:
    text = clean_text(name).replace(" ", "").replace("\u3000", "")
    return any(hint in text for hint in IDENTIFIER_HINTS)


def column_letter(index: int) -> str:
    """1 -> A, 27 -> AA。"""
    letters = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


# --------------------------------------------------------------------------
# 日期解析（方式B：从文件名取考试时间）
# --------------------------------------------------------------------------

_FULL_DATE_RE = re.compile(r"(20\d{2})\s*[-_./年]\s*(\d{1,2})\s*[-_./月]\s*(\d{1,2})\s*日?")
_COMPACT_DATE_RE = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")
_YEAR_MONTH_RE = re.compile(r"(20\d{2})\s*[-_./年]\s*(\d{1,2})\s*月份?")


def parse_exam_date(text: str):
    """从文件名/文本里解析考试日期。

    返回 (日期字符串, 说明)。解析不到时返回 (今天, 说明) 并明确写出用了今天。
    支持 2026-04-10 / 2026.4.10 / 2026年4月10日 / 20260410 / 2026年4月（按当月 1 日）。
    """
    raw = str(text or "")
    match = _FULL_DATE_RE.search(raw)
    if match:
        year, month, day = (int(g) for g in match.groups())
        try:
            return _date(year, month, day).isoformat(), f"来自名称「{match.group(0)}」"
        except ValueError:
            return None, f"名称里的日期「{match.group(0)}」不是合法日期"

    match = _COMPACT_DATE_RE.search(raw)
    if match:
        year, month, day = (int(g) for g in match.groups())
        try:
            return _date(year, month, day).isoformat(), f"来自名称「{match.group(0)}」"
        except ValueError:
            return None, f"名称里的日期「{match.group(0)}」不是合法日期"

    match = _YEAR_MONTH_RE.search(raw)
    if match:
        year, month = (int(g) for g in match.groups())
        if 1 <= month <= 12:
            return (
                _date(year, month, 1).isoformat(),
                f"名称只给到月份「{match.group(0)}」，按当月 1 日处理",
            )

    return _date.today().isoformat(), "名称里没有可识别的日期，按今天处理"


def parse_date_argument(text: str) -> str:
    """校验 --date 参数，只接受 YYYY-MM-DD / YYYYMMDD。"""
    raw = str(text or "").strip()
    for pattern in (r"(\d{4})-(\d{1,2})-(\d{1,2})", r"(\d{4})(\d{2})(\d{2})"):
        match = re.fullmatch(pattern, raw)
        if match:
            year, month, day = (int(g) for g in match.groups())
            try:
                return _date(year, month, day).isoformat()
            except ValueError:
                break
    raise InputError(f"--date 格式不正确：{raw}（应为 2026-04-10 或 20260410）")


# --------------------------------------------------------------------------
# 飞书链接解析
# --------------------------------------------------------------------------

_LINK_RE = re.compile(r"/(sheets|base)/([A-Za-z0-9_-]+)")


def parse_feishu_url(url: str) -> dict:
    """解析飞书链接，返回 {kind, token, query, raw}。

    kind 为 sheets（电子表格）或 base（多维表格）；token 为链接里的资源标识。
    形如 https://xxx.feishu.cn/sheets/<token>?sheet=xxxx 或 https://xxx.feishu.cn/base/<token>。
    """
    raw = str(url or "").strip()
    if not raw:
        raise InputError("--url 为空，请提供飞书表格链接。")
    if not re.match(r"^https?://", raw, re.I):
        raise InputError(
            "无法识别的飞书链接：" + raw + "\n"
            "期望形如 https://xxx.feishu.cn/sheets/<token> 或 https://xxx.feishu.cn/base/<token> 的链接。"
        )
    if "/wiki/" in raw:
        raise InputError(
            "这是飞书知识库（wiki）链接，本技能只认 /sheets/ 与 /base/ 两种链接。\n"
            "请在知识库里打开该表格后，从浏览器地址栏复制 /sheets/<token> 或 /base/<token> 的链接。"
        )
    match = _LINK_RE.search(raw)
    if not match:
        raise InputError(
            "链接里没有找到 /sheets/<token> 或 /base/<token>：" + raw + "\n"
            "支持的类型：sheets（电子表格）、base（多维表格）。"
        )
    kind, token = match.group(1), match.group(2)
    query = {key: values[0] for key, values in parse_qs(urlparse(raw).query).items() if values}
    return {"kind": kind, "token": token, "query": query, "raw": raw}


# --------------------------------------------------------------------------
# 凭据：环境变量优先，其次 ~/.openclaw/.env，绝不硬编码
# --------------------------------------------------------------------------


def parse_env_file(path) -> dict:
    """解析 KEY=VALUE 形式的凭据文件，忽略空行与 # 注释。"""
    values = {}
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def credentials_path() -> Path:
    """凭据文件位置：默认 ~/.openclaw/.env，可用 FEISHU_ENV_FILE 覆盖。"""
    override = (os.environ.get("FEISHU_ENV_FILE") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".openclaw" / ".env"


def diagnose_missing_credentials(missing) -> str:
    path = credentials_path()
    return (
        "飞书凭据未配置：缺少 " + "、".join(missing) + "\n"
        "请任选一种方式配置（凭据只放服务器上，绝对不要写进代码或提交到仓库）：\n"
        "  1) 环境变量：export FEISHU_APP_ID=cli_xxx; export FEISHU_APP_SECRET=xxx\n"
        "  2) 凭据文件：" + str(path) + "\n"
        "     格式（每行 KEY=VALUE）：\n"
        "       FEISHU_APP_ID=cli_xxx\n"
        "       FEISHU_APP_SECRET=xxx\n"
        "     权限建议收紧：chmod 600 " + str(path) + "\n"
        "凭据取自飞书开放平台 https://open.feishu.cn/app 的自建应用「凭证与基础信息」。\n"
        "所需权限：sheets:spreadsheet:readonly、im:chat.members:read、im:message。"
    )


def load_credentials():
    """返回 (app_id, app_secret, 来源说明)；缺任何一个都抛 MissingCredentialError。"""
    app_id = (os.environ.get("FEISHU_APP_ID") or "").strip()
    app_secret = (os.environ.get("FEISHU_APP_SECRET") or "").strip()
    source = "环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET"

    if not (app_id and app_secret):
        path = credentials_path()
        if path.is_file():
            values = parse_env_file(path)
            app_id = app_id or (values.get("FEISHU_APP_ID") or "").strip()
            app_secret = app_secret or (values.get("FEISHU_APP_SECRET") or "").strip()
            source = "凭据文件 " + str(path)

    missing = [
        name
        for name, value in (("FEISHU_APP_ID", app_id), ("FEISHU_APP_SECRET", app_secret))
        if not value
    ]
    if missing:
        raise MissingCredentialError(diagnose_missing_credentials(missing))
    return app_id, app_secret, source


def mask_secret(value: str) -> str:
    """脱敏打印，避免把凭据写进日志。"""
    text = str(value or "")
    if len(text) <= 8:
        return "*" * len(text)
    return text[:4] + "*" * (len(text) - 8) + text[-4:]


# --------------------------------------------------------------------------
# HTTP 封装（独立函数，便于测试 monkeypatch；统一超时）
# --------------------------------------------------------------------------


def http_post_json(url: str, payload: dict, timeout: int = TIMEOUT, headers=None) -> dict:
    import requests

    try:
        response = requests.post(url, json=payload, timeout=timeout, headers=headers or {})
    except requests.RequestException as exc:
        raise FeishuAPIError("请求飞书接口失败（网络错误）：" + url + "\n原因：" + str(exc)) from exc
    return _decode_json(response, url)


def http_get_json(url: str, params=None, timeout: int = TIMEOUT, headers=None) -> dict:
    import requests

    try:
        response = requests.get(url, params=params, timeout=timeout, headers=headers or {})
    except requests.RequestException as exc:
        raise FeishuAPIError("请求飞书接口失败（网络错误）：" + url + "\n原因：" + str(exc)) from exc
    return _decode_json(response, url)


def _decode_json(response, url: str) -> dict:
    try:
        body = response.json()
    except ValueError as exc:
        raise FeishuAPIError(
            "飞书接口返回的不是 JSON：HTTP " + str(response.status_code) + " " + url + "\n"
            "响应片段：" + str(getattr(response, "text", ""))[:300]
        ) from exc
    if isinstance(body, dict):
        code = body.get("code")
        if code not in (0, None):
            message = body.get("msg") or body.get("message") or "(无 msg)"
            raise FeishuAPIError(
                "飞书接口报错 code=" + str(code) + " msg=" + str(message) + "\n请求：" + url
            )
    if getattr(response, "status_code", 200) >= 400:
        raise FeishuAPIError(
            "飞书接口返回 HTTP " + str(response.status_code) + "：" + url + "\n"
            "响应片段：" + str(getattr(response, "text", ""))[:300]
        )
    return body


def get_tenant_access_token(app_id: str, app_secret: str, timeout: int = TIMEOUT):
    """获取 tenant_access_token，返回 (token, 有效期秒数)。"""
    url = BASE_URL + "/open-apis/auth/v3/tenant_access_token/internal"
    body = http_post_json(url, {"app_id": app_id, "app_secret": app_secret}, timeout=timeout)
    token = body.get("tenant_access_token")
    if not token:
        message = body.get("msg") or body.get("message") or "(无 msg)"
        raise FeishuAPIError(
            "没有取到 tenant_access_token：code=" + str(body.get("code")) + " msg=" + str(message) + "\n"
            "常见原因：app_id / app_secret 写错，或应用未启用。"
        )
    return token, int(body.get("expire") or 0)


def api_get(path: str, access_token: str, params=None, timeout: int = TIMEOUT) -> dict:
    url = path if path.startswith("http") else BASE_URL + path
    body = http_get_json(
        url, params=params, timeout=timeout, headers={"Authorization": "Bearer " + access_token}
    )
    data = body.get("data")
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------
# 飞书取数
# --------------------------------------------------------------------------


def _values_to_table(values):
    """把二维数组转成 (表头, 数据行)，表头取第一行非空行。"""
    rows = [list(row) for row in values or []]
    if not rows:
        return [], []
    header = [clean_text(cell) for cell in rows[0]]
    while header and not header[-1]:
        header.pop()
    data = []
    for row in rows[1:]:
        padded = [row[i] if i < len(row) else None for i in range(len(header))]
        if any(clean_text(cell) for cell in padded):
            data.append(padded)
    return header, data


def fetch_sheet_table(token: str, access_token: str, sheet_name=None, timeout: int = TIMEOUT):
    """读取飞书电子表格：返回 (工作表名, 表头, 数据行)。"""
    meta = api_get(
        "/open-apis/sheets/v3/spreadsheets/" + token + "/sheets/query", access_token, timeout=timeout
    )
    sheets = meta.get("sheets") or []
    if not sheets:
        raise FeishuAPIError("飞书返回的表格元信息里没有任何工作表，无法取数。")

    targets = sheets
    if sheet_name:
        targets = [s for s in sheets if clean_text(s.get("title")) == sheet_name]
        if not targets:
            names = "、".join(clean_text(s.get("title")) for s in sheets)
            raise InputError("表格里没有名为「" + str(sheet_name) + "」的工作表。实际工作表：" + names)

    first = targets[0]
    title = clean_text(first.get("title")) or token
    sheet_id = first.get("sheet_id") or first.get("sheetId")
    grid = first.get("grid_properties") or {}
    row_count = int(grid.get("row_count") or 0)
    col_count = int(grid.get("column_count") or 0)
    if not sheet_id or row_count <= 0 or col_count <= 0:
        raise FeishuAPIError("表格「" + title + "」的元信息缺少 sheet_id 或行列范围，无法取数。")

    cell_range = sheet_id + "!A1:" + column_letter(col_count) + str(row_count)
    data = api_get(
        "/open-apis/sheets/v2/spreadsheets/" + token + "/values/" + cell_range,
        access_token,
        params={"valueRenderOption": "ToString"},
        timeout=timeout,
    )
    value_range = data.get("valueRange") or {}
    header, rows = _values_to_table(value_range.get("values"))
    if not header:
        raise FeishuAPIError("表格「" + title + "」读回来是空的，没有可用的表头。")
    return title, header, rows


def fetch_bitable_table(token: str, access_token: str, timeout: int = TIMEOUT):
    """读取飞书多维表格的第一张数据表：返回 (表名, 表头, 数据行)。"""
    meta = api_get(
        "/open-apis/bitable/v1/apps/" + token + "/tables", access_token,
        params={"page_size": 100}, timeout=timeout,
    )
    tables = meta.get("items") or []
    if not tables:
        raise FeishuAPIError("飞书返回的多维表格里没有任何数据表，无法取数。")
    table = tables[0]
    table_id = table.get("table_id")
    title = clean_text(table.get("name")) or table_id
    if not table_id:
        raise FeishuAPIError("多维表格「" + title + "」缺少 table_id，无法取数。")

    records, page_token = [], None
    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        data = api_get(
            "/open-apis/bitable/v1/apps/" + token + "/tables/" + table_id + "/records",
            access_token, params=params, timeout=timeout,
        )
        records.extend(data.get("items") or [])
        page_token = data.get("page_token")
        if not data.get("has_more") or not page_token:
            break

    header = []
    for record in records:
        for key in (record.get("fields") or {}):
            if key not in header:
                header.append(key)
    rows = []
    for record in records:
        fields = record.get("fields") or {}
        rows.append([_bitable_cell_to_text(fields.get(name)) for name in header])
    if not header:
        raise FeishuAPIError("多维表格「" + title + "」里没有读到任何字段，无法取数。")
    return title, header, rows


def _bitable_cell_to_text(value) -> str:
    """多维表格字段值可能是文本/数字/数组/对象，统一成可读文本。"""
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [_bitable_cell_to_text(item) for item in value]
        return "、".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("text", "name", "value", "en_name"):
            if key in value:
                return _bitable_cell_to_text(value[key])
        return str(value)
    return str(value)


# --------------------------------------------------------------------------
# 本地成绩文件读取
# --------------------------------------------------------------------------

_HEADER_HINTS = ("姓名", "学号", "考号", "总分", "总成绩", "学生")


def find_header_row(rows):
    """在前 10 行里找表头行：优先含已知表头关键字的那行，否则第一行有 2 个以上非空格。"""
    fallback = None
    for index, row in enumerate(rows[:10]):
        cells = [clean_text(cell) for cell in row]
        filled = [cell for cell in cells if cell]
        if len(filled) < 2:
            continue
        if fallback is None:
            fallback = index
        if any(hint in cell for cell in filled for hint in _HEADER_HINTS):
            return index
    return fallback


def table_from_rows(rows):
    """把原始二维行转成 (表头, 数据行)；找不到表头返回 (None, None)。"""
    header_index = find_header_row(rows)
    if header_index is None:
        return None, None
    header = [clean_text(cell) for cell in rows[header_index]]
    while header and not header[-1]:
        header.pop()
    if not header:
        return None, None

    data = []
    for row in rows[header_index + 1:]:
        cells = list(row)
        padded = [cells[i] if i < len(cells) else None for i in range(len(header))]
        if any(clean_text(cell) for cell in padded):
            data.append(padded)

    # 去掉整列都空的幽灵列（常见于 Excel 里的多余格式列）
    keep = [
        i for i in range(len(header))
        if header[i] or any(clean_text(row[i]) for row in data)
    ]
    if not keep:
        return None, None
    header = [header[i] for i in keep]
    data = [[row[i] for i in keep] for row in data]
    return header, data


def read_xlsx_tables(path: Path, sheet_name=None):
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(filename=str(path), data_only=True, read_only=True)
    except Exception as exc:  # openpyxl 对坏文件抛的异常类型很杂
        raise InputError("无法读取 Excel 文件：" + str(path) + "\n原因：" + str(exc)) from exc

    names = workbook.sheetnames
    if sheet_name and sheet_name not in names:
        raise InputError(
            "文件里没有名为「" + str(sheet_name) + "」的工作表。实际工作表：" + "、".join(names)
        )
    targets = [sheet_name] if sheet_name else names

    tables = []
    for name in targets:
        sheet = workbook[name]
        rows = [list(row) for row in sheet.iter_rows(values_only=True)]
        header, data = table_from_rows(rows)
        if header:
            tables.append((name, header, data))
    workbook.close()
    if not tables:
        raise InputError("文件里没有读到可用的表头（前 10 行都没有表头行）：" + str(path))
    return tables


def read_csv_tables(path: Path):
    last_error = None
    for encoding in ("utf-8-sig", "gbk", "utf-8"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                rows = [list(row) for row in csv.reader(handle)]
            header, data = table_from_rows(rows)
            if not header:
                raise InputError("CSV 里没有读到可用的表头：" + str(path))
            return [(path.stem, header, data)]
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    raise InputError(
        "CSV 编码无法识别（试过 utf-8-sig / gbk / utf-8）：" + str(path) + "\n原因：" + str(last_error)
    )


def read_local_tables(path: Path, sheet_name=None):
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        return read_xlsx_tables(path, sheet_name=sheet_name)
    if suffix == ".csv":
        return read_csv_tables(path)
    if suffix == ".xls":
        raise InputError(
            "openpyxl 不支持旧版 .xls 格式：" + str(path) + "\n请在 Excel/WPS 里另存为 .xlsx 或 .csv 后重试。"
        )
    raise InputError("不支持的文件类型：" + str(path) + "（只支持 .xlsx / .xlsm / .csv）")


def merge_tables(tables):
    """合并同一份文件里的多张工作表：表头一致才合并，否则跳过并给出提示。

    SOUL.md 要求「自动检测所有工作表，不遗漏任何数据」，所以表头不一致的表
    不会被静默丢弃，而是写进 notes 并在调用方输出里明确提示。
    """
    header, rows, notes, kept = None, [], [], []
    for name, table_header, table_rows in tables:
        if header is None:
            header, kept = list(table_header), [name]
            rows.extend(table_rows)
            continue
        if [clean_text(c) for c in table_header] == [clean_text(c) for c in header]:
            rows.extend(table_rows)
            kept.append(name)
        else:
            notes.append(
                "工作表「" + str(name) + "」的表头与「" + str(kept[0]) + "」不一致，已跳过；"
                "它的列名是：" + "、".join(clean_text(c) for c in table_header)
            )
    return header, rows, notes, kept


# --------------------------------------------------------------------------
# 写出 xlsx
# --------------------------------------------------------------------------


def write_xlsx(path: Path, header, rows) -> Path:
    from openpyxl import Workbook

    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "成绩"
    sheet.append([clean_text(cell) for cell in header])
    identifiers = {index for index, name in enumerate(header) if is_identifier_column(name)}
    for row in rows:
        cells = list(row)
        padded = [cells[index] if index < len(cells) else None for index in range(len(header))]
        sheet.append([
            value if index in identifiers else coerce_number(value)
            for index, value in enumerate(padded)
        ])
    workbook.save(str(path))
    return path


# --------------------------------------------------------------------------
# --check 诊断
# --------------------------------------------------------------------------


def run_check(args) -> int:
    """只验证凭据与链接可达 / 本地文件可读，不写入任何数据。"""
    print("== 飞书取数环境诊断 ==")

    if args.local:
        path = Path(args.local)
        print("检查本地文件：" + str(path))
        if not path.is_file():
            raise InputError("本地文件不存在：" + str(path))
        tables = read_local_tables(path, sheet_name=args.sheet)
        header, rows, notes, kept = merge_tables(tables)
        print("[OK] 文件可读，工作表：" + "、".join(kept))
        print("[OK] 表头 " + str(len(header)) + " 列：" + "、".join(header))
        print("[OK] 数据 " + str(len(rows)) + " 行")
        for note in notes:
            print("[提示] " + note)
        print("结论：本地文件可用（--check 不写入任何数据）。")
        return EXIT_OK

    link = parse_feishu_url(args.url)
    print("链接类型：" + link["kind"] + "（" + KIND_LABELS[link["kind"]] + "）")
    print("链接 token：" + link["token"])
    if link["query"]:
        print("链接参数：" + ", ".join(k + "=" + v for k, v in link["query"].items()))

    try:
        app_id, app_secret, source = load_credentials()
    except MissingCredentialError as exc:
        print("[FAIL] 凭据不可用。")
        print(str(exc))
        print("结论：缺少飞书凭据，联网取数无法进行（不会伪造数据）。")
        return EXIT_CRED

    print("[OK] 凭据来源：" + source)
    print("     app_id：" + mask_secret(app_id))
    token, expire = get_tenant_access_token(app_id, app_secret, timeout=args.timeout)
    print("[OK] tenant_access_token 获取成功（有效期 " + str(expire) + " 秒），值：" + mask_secret(token))
    print("[OK] --check 只做验证，不打印 token 明文、不写入数据。")

    if link["kind"] == "sheets":
        title, header, rows = fetch_sheet_table(
            link["token"], token, sheet_name=args.sheet, timeout=args.timeout
        )
    else:
        title, header, rows = fetch_bitable_table(link["token"], token, timeout=args.timeout)
    print("[OK] 读到表格「" + title + "」：表头 " + str(len(header)) + " 列、数据 " + str(len(rows)) + " 行")
    print("结论：凭据与链接都可用，可以执行取数（去掉 --check）。")
    return EXIT_OK


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="读取飞书电子表格/多维表格，或导入本地成绩文件，写入 data/grades/{日期}.xlsx",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--url", help="飞书表格链接（/sheets/<token> 或 /base/<token>）")
    source.add_argument("--local", help="本地成绩文件（.xlsx / .xlsm / .csv）")
    parser.add_argument("--check", action="store_true", help="只做诊断：验证凭据与链接/文件，不写入数据")
    parser.add_argument("--date", help="覆盖考试日期（YYYY-MM-DD 或 YYYYMMDD），优先于文件名解析")
    parser.add_argument("--sheet", help="指定工作表名（默认合并表头一致的全部工作表）")
    parser.add_argument("--data-dir", default="data", help="数据目录，默认 data（相对工作区根目录）")
    parser.add_argument("--force", action="store_true", help="目标 xlsx 已存在时覆盖")
    parser.add_argument("--timeout", type=int, default=TIMEOUT, help="HTTP 超时秒数，默认 15")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        _import_dependencies()

        if not args.url and not args.local:
            raise InputError("请提供 --url <飞书链接> 或 --local <本地成绩文件>（或用 --check 做诊断）。")

        if args.check:
            return run_check(args)

        data_root = Path(args.data_dir)

        if args.local:
            source_desc = "本地文件 " + str(Path(args.local))
            path = Path(args.local)
            if not path.is_file():
                raise InputError("本地文件不存在：" + str(path))
            tables = read_local_tables(path, sheet_name=args.sheet)
            header, rows, notes, kept = merge_tables(tables)
            kind_label = "本地成绩文档（方式B）"
        else:
            link = parse_feishu_url(args.url)
            source_desc = "飞书链接 " + link["raw"] + "（" + KIND_LABELS[link["kind"]] + "）"
            app_id, app_secret, cred_source = load_credentials()
            access_token, _ = get_tenant_access_token(app_id, app_secret, timeout=args.timeout)
            if link["kind"] == "sheets":
                title, header, rows = fetch_sheet_table(
                    link["token"], access_token, sheet_name=args.sheet, timeout=args.timeout
                )
            else:
                title, header, rows = fetch_bitable_table(
                    link["token"], access_token, timeout=args.timeout
                )
            notes, kept = [], [title]
            kind_label = "飞书" + KIND_LABELS[link["kind"]] + "（方式A）"

        if args.date:
            exam_date = parse_date_argument(args.date)
            date_note = "来自 --date 参数"
        else:
            if args.local:
                name_for_date = str(Path(args.local).name)
            else:
                name_for_date = Path(urlparse(args.url).path).name
            exam_date, date_note = parse_exam_date(name_for_date)
            if exam_date is None:
                exam_date, date_note = parse_exam_date("")

        target = data_root / "grades" / (exam_date + ".xlsx")
        if target.exists() and not args.force:
            raise InputError(
                "目标文件已存在：" + str(target) + "\n"
                "为避免覆盖已有成绩，默认不写入。确认要覆盖请加 --force，"
                "或用 --date 指定其它考试日期。"
            )
        write_xlsx(target, header, rows)

        print("[完成] 已写入：" + str(target.resolve()))
        print("       取数方式：" + kind_label)
        print("       来源：" + source_desc)
        print("       考试日期：" + exam_date + "（" + date_note + "）")
        print("       表头 " + str(len(header)) + " 列：" + "、".join(header))
        print("       数据 " + str(len(rows)) + " 行（一行一个学生）")
        if len(kept) > 1:
            print("       合并的工作表：" + "、".join(str(k) for k in kept))
        for note in notes:
            print("       [提示] " + note)
        print("       提示：数据只写在工作区 data/ 目录内，未发送给任何外部服务。")
        return EXIT_OK

    except SkillError as exc:
        print("错误：" + str(exc), file=sys.stderr)
        return exc.exit_code
    except OSError as exc:
        print("错误：文件读写失败：" + str(exc), file=sys.stderr)
        return EXIT_API


if __name__ == "__main__":
    sys.exit(main())
