#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""upload_share.py - 把产出文件上传到华为云 OBS（私有桶），生成预签名下载链接。

对应技能 skills/obs-share/SKILL.md。约定 cwd = 工作区根目录
（如 /srv/openclaw-teacher/workspaces/lesson 或 .../workspaces/office）。

做四件事：

  1. 从环境变量或 ~/.openclaw/.env 读 OBS 凭据（只读，任何输出都不打印 AK/SK 的值）；
  2. headBucket 探测桶；返回 404 时用 listBuckets 自动诊断「区域不一致」并打印正确 endpoint；
  3. 上传对象名为 <prefix>/<YYYY-MM-DD>/<8位随机十六进制>-<安全化文件名>，然后
     getObjectMetadata 校验字节数；--verify（默认开）再用预签名链接真的 GET 一次校验内容；
  4. 每次成功上传，往 cwd 的 obs-uploads.jsonl 追加一行审计记录（链接不含签名）。

安全闸门：`data/` 目录下（学生信息 / 家长联系方式 / 成绩）的文件必须显式加
--allow-sensitive 才允许上传，否则退出码 5。原因见 SKILL.md「安全闸门」一节。

用法示例（cwd = 工作区根）：

  # 只诊断，不上传：读凭据、探测桶、打印 endpoint/区域/ACL 是否私有/对象数
  python3 skills/obs-share/scripts/upload_share.py --check

  # 上传一个教案，拿到 24 小时有效的预签名链接
  python3 skills/obs-share/scripts/upload_share.py --file lessons/第一单元_教案.docx

  # 一次传两个文件，7 天有效，结果用 JSON 输出（stdout 只有一个 JSON 文档）
  python3 skills/obs-share/scripts/upload_share.py \\
      --file exams/期中_试卷.docx --file exams/期中_答案.docx --expires 604800 --json

  # 成绩报告在 data/ 下，属于学生数据出域：必须知情同意
  python3 skills/obs-share/scripts/upload_share.py \\
      --file data/reports/张三-成绩报告.docx --allow-sensitive

退出码：0 成功 / 2 参数、文件或自校验错误 / 3 凭据或依赖问题 / 4 OBS 远端错误 /
5 安全闸门拒绝（data/ 下未加 --allow-sensitive，或 --check 发现桶不是私有的）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

# --------------------------------------------------------------------------
# 常量与退出码
# --------------------------------------------------------------------------

EXIT_OK = 0
EXIT_ARG = 2
EXIT_CRED = 3
EXIT_REMOTE = 4
EXIT_SENSITIVE = 5

# 链接有效期：默认 24 小时，上限 7 天。
# 注意：7 天是**我们自己设的策略上限，不是 OBS 的限制**。
# 实测（2026-09-16，本桶）：expires=604800 / 604801 / 2592000（30 天）签出来的链接
# OBS 服务端**都返回 200**，也就是说技术上可以签更长。卡在 7 天的理由是：
# 链接一旦转发出去，持链接者就能在有效期内下载；有效期越长、泄露面越大。
EXPIRES_DEFAULT = 86400
EXPIRES_MAX = 604800

DEFAULT_PREFIX = "teacher-assistant"
SENSITIVE_DIR = "data"
AUDIT_FILENAME = "obs-uploads.jsonl"

# 凭据键名（只认这四个；AK/SK 的值只读不打印）。
ENV_KEYS = ("OBS_ACCESS_KEY", "OBS_SECRET_KEY", "OBS_BUCKET", "OBS_ENDPOINT")
# 凭据文件位置，可用环境变量 OBS_ENV_FILE 覆盖（便于测试与多环境）。
ENV_FILE_OVERRIDE = "OBS_ENV_FILE"
# 注意：这里**不能**写成 ("~", ".openclaw", ".env")。
# env_file_path() 已经用 Path.home() 取到 /root，再 joinpath("~", ...) 会拼出
# /root/~/.openclaw/.env 这种带字面量 ~ 的错路径，导致读不到凭据文件
# （实测表现：明明配好了 .env，脚本仍报"凭据未配置"）。
DEFAULT_ENV_FILE = (".openclaw", ".env")

# HTTP 校验超时（秒）。
HTTP_TIMEOUT = 30
OK_STATUSES = (200, 204)

# 本项目生产桶在服务器上实测的区域。只用于「区域不一致」的报错提示，不是凭据，
# 也绝不参与请求构造（请求一律以 OBS_ENDPOINT 为准）。
PROJECT_KNOWN_BUCKET = "openclaw-teacher"
PROJECT_KNOWN_REGION = "cn-east-3"


class SkillError(Exception):
    """本技能的可预期错误，exit_code 决定进程退出码。"""

    exit_code = EXIT_ARG


class InputError(SkillError):
    """参数、文件、前缀等输入不合法。"""

    exit_code = EXIT_ARG


class VerificationError(SkillError):
    """上传后自校验失败（字节数不一致 / 链接取不回同样内容）。"""

    exit_code = EXIT_ARG


class CredentialError(SkillError):
    """缺少凭据，或凭据内容不可用。"""

    exit_code = EXIT_CRED


class MissingDependencyError(SkillError):
    """服务器上缺 esdk-obs-python（本技能不自动安装任何包）。"""

    exit_code = EXIT_CRED


class RemoteError(SkillError):
    """OBS 远端返回了非预期状态（含区域不一致诊断）。"""

    exit_code = EXIT_REMOTE


class SensitiveGateError(SkillError):
    """命中「学生数据只在本工作区内处理」这条红线。"""

    exit_code = EXIT_SENSITIVE


# --------------------------------------------------------------------------
# 小工具：SDK 返回值形状不稳定，统一用属性/字典双兼容的读取方式
# --------------------------------------------------------------------------


def _get(obj, name, default=None):
    """先按属性读，再按字典键读；读不到返回 default。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        value = obj.get(name, default)
        return default if value is None else value
    value = getattr(obj, name, default)
    return default if value is None else value


def status_of(resp):
    """取 HTTP 状态码；取不到返回 None。"""
    for name in ("status", "status_code", "statusCode"):
        value = _get(resp, name)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def failure_detail(resp) -> str:
    """把 SDK 响应里的状态码/错误码/错误信息拼成一行可读文本。"""
    parts = [f"HTTP {status_of(resp)}"]
    code = _get(resp, "errorCode") or _get(resp, "error_code")
    message = (
        _get(resp, "errorMessage")
        or _get(resp, "error_message")
        or _get(resp, "reason")
    )
    if code:
        parts.append(f"errorCode={code}")
    if message:
        parts.append(f"errorMessage={message}")
    return "，".join(parts)


def now_iso() -> str:
    """本机时区的 ISO8601 时间（带时区偏移）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def expires_at_iso(expires: int) -> str:
    """链接失效时刻（本机时区）。"""
    moment = datetime.now().astimezone() + timedelta(seconds=int(expires))
    return moment.isoformat(timespec="seconds")


def region_of_endpoint(endpoint: str):
    """从 obs.<region>.myhuaweicloud.com 里取 <region>；取不到返回 None。"""
    match = re.fullmatch(r"obs\.([a-z0-9-]+)\.myhuaweicloud\.com", str(endpoint or "").strip(), re.I)
    return match.group(1) if match else None


def endpoint_for_region(region: str) -> str:
    """region -> OBS endpoint。"""
    return f"obs.{str(region).strip()}.myhuaweicloud.com"


def normalize_endpoint(raw: str) -> str:
    """容错：允许 OBS_ENDPOINT 写成带 https:// 或带结尾斜杠的形式。"""
    value = str(raw or "").strip()
    value = re.sub(r"^https?://", "", value, flags=re.I)
    return value.rstrip("/")


# --------------------------------------------------------------------------
# 对象名：不可猜、可读、防路径穿越
# --------------------------------------------------------------------------

# 控制字符（含换行、制表、NUL）一律删掉。
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# 路径分隔符、URL 里会捣乱或容易被转义误解的字符，统一换成 _。
_UNSAFE_NAME_CHARS = re.compile(r"[\\/:*?\"<>|#%&'`;]")
_WHITESPACE_RE = re.compile(r"\s+")
_UNDERSCORE_RUN_RE = re.compile(r"_{2,}")
_MAX_NAME_LENGTH = 180


def sanitize_filename(name: str) -> str:
    """把原始文件名安全化成对象名里的一段。

    - 只取最后一段路径（`..`、`/`、`\\` 都被丢掉），杜绝路径穿越；
    - 删掉控制字符（含换行），保留中文；
    - 空格与 `:` `*` `?` `"` `<` `>` `|` `#` `%` `&` `'` 等换成 `_`；
    - 空、`.`、`..` 这类退化结果统一成 `file`。
    """
    raw = str(name or "")
    raw = raw.replace("\\", "/")          # Windows 分隔符统一成 /
    raw = raw.split("/")[-1]              # 只保留最后一段：防路径穿越
    raw = _CONTROL_RE.sub("", raw)        # 去掉控制字符
    raw = _UNSAFE_NAME_CHARS.sub("_", raw)
    raw = _WHITESPACE_RE.sub("_", raw).strip()
    raw = raw.lstrip(".")                 # 不产生隐藏文件风格的名字
    raw = _UNDERSCORE_RUN_RE.sub("_", raw)
    if not raw or raw in {".", ".."}:
        raw = "file"
    if len(raw) > _MAX_NAME_LENGTH:
        stem, dot, ext = raw.rpartition(".")
        if dot and 0 < len(ext) <= 12:
            raw = stem[: _MAX_NAME_LENGTH - len(ext) - 1] + "." + ext
        else:
            raw = raw[:_MAX_NAME_LENGTH]
    return raw


def sanitize_prefix(prefix: str) -> str:
    """把 --prefix 规范成安全的对象名前缀（保留 / 作为层级分隔）。"""
    raw = _CONTROL_RE.sub("", str(prefix or "").strip().replace("\\", "/"))
    segments = []
    for segment in raw.split("/"):
        segment = _UNSAFE_NAME_CHARS.sub("_", segment).strip().strip(".")
        segment = _UNDERSCORE_RUN_RE.sub("_", segment)
        if segment:
            segments.append(segment)
    if not segments:
        raise InputError(
            "--prefix 不能为空，也不能只由 / 和 . 组成。\n"
            f"省略 --prefix 时默认用 {DEFAULT_PREFIX}。"
        )
    return "/".join(segments)


def day_string(when=None) -> str:
    """取对象名里的日期段（YYYY-MM-DD）；when 可注入用于测试。"""
    if when is None:
        return date.today().isoformat()
    if isinstance(when, datetime):
        return when.date().isoformat()
    if isinstance(when, date):
        return when.isoformat()
    text = str(when).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise InputError(f"日期格式不正确：{text}（应为 YYYY-MM-DD）")
    return text


def build_object_key(prefix: str, filename: str, when=None, token=None) -> str:
    """生成对象名：<prefix>/<YYYY-MM-DD>/<8位随机十六进制>-<安全化文件名>。

    随机后缀保证：同名文件重复上传不会互相覆盖，外人也没法靠文件名猜到对象名。
    token 可注入（测试用），默认取 secrets.token_hex(4)（8 位十六进制）。
    """
    suffix = str(token) if token else secrets.token_hex(4)
    return f"{sanitize_prefix(prefix)}/{day_string(when)}/{suffix}-{sanitize_filename(filename)}"


def bare_object_url(bucket: str, endpoint: str, key: str) -> str:
    """不含签名的裸 URL：写进审计日志用，直接打开会 403（桶是私有的）。"""
    return f"https://{bucket}.{endpoint}/{key}"


# --------------------------------------------------------------------------
# 参数校验
# --------------------------------------------------------------------------


def validate_expires(value) -> int:
    """校验 --expires：1 ~ 604800 秒。"""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        raise InputError(f"--expires 必须是整数秒：{value!r}") from None
    if seconds < 1:
        raise InputError(
            f"链接有效期越界：--expires={seconds} 秒。\n"
            f"请传 1 ~ {EXPIRES_MAX} 之间的秒数（默认 {EXPIRES_DEFAULT} 秒 = 24 小时）。"
        )
    if seconds > EXPIRES_MAX:
        days = seconds / 86400
        raise InputError(
            f"链接有效期越界：--expires={seconds} 秒（约 {days:.1f} 天），上限是 {EXPIRES_MAX} 秒（7 天）。\n"
            f"说明：这个 7 天上限是**我们设的策略**，不是 OBS 的限制 —— 实测 30 天的签名链接\n"
            f"OBS 同样接受。之所以卡在 7 天：链接一旦转发出去，持链接者就能在有效期内下载，\n"
            f"有效期越长泄露面越大。需要更长的可用期，请到期后重新生成链接。\n"
            f"用法：--expires {EXPIRES_MAX}（7 天），或省略该参数用默认的 {EXPIRES_DEFAULT} 秒（24 小时）。"
        )
    return seconds


def resolve_target(raw: str, workspace_root: Path) -> Path:
    """把 --file 的值解析成绝对路径，并检查它确实是一个存在的文件。"""
    text = str(raw or "").strip()
    if not text:
        raise InputError("--file 不能为空。")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = workspace_root / path
    try:
        path = path.resolve()
    except OSError:
        pass
    if path.is_dir():
        raise InputError(
            f"--file 指向的是一个目录：{text}\n"
            "请传具体文件；要传整个目录，请展开成多个 --file。"
        )
    if not path.is_file():
        raise InputError(f"文件不存在：{text}（解析成 {path}）")
    return path


# --------------------------------------------------------------------------
# 凭据：环境变量优先，其次 ~/.openclaw/.env；绝不硬编码、绝不打印值
# --------------------------------------------------------------------------


def env_file_path() -> Path:
    """凭据文件位置：默认 ~/.openclaw/.env，可用 OBS_ENV_FILE 覆盖。"""
    override = (os.environ.get(ENV_FILE_OVERRIDE) or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home().joinpath(*DEFAULT_ENV_FILE)


def parse_env_file(path) -> dict:
    """解析 KEY=VALUE 形式的凭据文件，忽略空行与 # 注释，支持 export 前缀与引号。"""
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


def diagnose_missing_credentials(missing, env_file: Path) -> str:
    """缺凭据时的中文诊断：缺哪个键、该写在哪里、怎么配。"""
    return (
        "OBS 凭据未配置：缺少 " + "、".join(missing) + "\n"
        "读取顺序：1) 环境变量；2) 凭据文件 " + str(env_file) + "\n"
        "\n"
        "配置方法一（推荐，只放服务器，权限 600）：\n"
        "  mkdir -p ~/.openclaw && chmod 700 ~/.openclaw\n"
        "  cat >> ~/.openclaw/.env <<'EOF'\n"
        "  OBS_ACCESS_KEY=<你的AccessKeyId>\n"
        "  OBS_SECRET_KEY=<你的SecretAccessKey>\n"
        "  OBS_BUCKET=openclaw-teacher\n"
        "  OBS_ENDPOINT=obs.cn-east-3.myhuaweicloud.com\n"
        "  EOF\n"
        "  chmod 600 ~/.openclaw/.env\n"
        "\n"
        "配置方法二（临时，仅当前 shell 有效）：\n"
        "  export OBS_ACCESS_KEY=... OBS_SECRET_KEY=... \\\n"
        "         OBS_BUCKET=openclaw-teacher OBS_ENDPOINT=obs.cn-east-3.myhuaweicloud.com\n"
        "\n"
        "要指向别的凭据文件，可设置环境变量 " + ENV_FILE_OVERRIDE + "=/path/to/.env 。\n"
        "安全提醒：凭据只放在服务器上，不要写进仓库、不要贴进聊天；"
        "本脚本只读不打印 AK/SK 的值（诊断里最多打印长度）。"
    )


def load_credentials(environ=None) -> dict:
    """读凭据：逐键优先环境变量，其次凭据文件。缺任何一个键都报错。

    返回 {access_key_id, secret_access_key, bucket, endpoint, source}。
    """
    environ = os.environ if environ is None else environ
    path = env_file_path()
    file_values = parse_env_file(path)

    mapping = (
        ("OBS_ACCESS_KEY", "access_key_id"),
        ("OBS_SECRET_KEY", "secret_access_key"),
        ("OBS_BUCKET", "bucket"),
        ("OBS_ENDPOINT", "endpoint"),
    )
    values, origins = {}, {}
    missing = []
    for env_key, field in mapping:
        from_env = str(environ.get(env_key) or "").strip()
        if from_env:
            values[field] = from_env
            origins[field] = "环境变量"
            continue
        from_file = str(file_values.get(env_key) or "").strip()
        if from_file:
            values[field] = from_file
            origins[field] = str(path)
            continue
        missing.append(env_key)

    if missing:
        raise CredentialError(diagnose_missing_credentials(missing, path))

    values["endpoint"] = normalize_endpoint(values["endpoint"])
    if not values["endpoint"]:
        raise CredentialError(
            "OBS_ENDPOINT 为空或格式不正确。\n"
            "正确写法是 obs.<region>.myhuaweicloud.com，例如 obs.cn-east-3.myhuaweicloud.com"
            "（不要带 https:// 与结尾斜杠，本脚本会自动去掉）。"
        )
    if "/" in values["bucket"] or not values["bucket"]:
        raise CredentialError(f"OBS_BUCKET 不是合法的桶名：{values['bucket']!r}")

    used = set(origins.values())
    if used == {"环境变量"}:
        values["source"] = "环境变量"
    elif used == {str(path)}:
        values["source"] = f"凭据文件 {path}"
    else:
        values["source"] = f"环境变量 + 凭据文件 {path}"
    values["env_file"] = str(path)
    return values


def create_obs_client(credentials: dict):
    """延迟导入 obs 并建客户端（延迟导入是为了让纯逻辑测试在没有 obs 的机器上也能跑）。

    这是本脚本唯一接触 esdk-obs-python 的边界，测试里可以整体替换掉它。
    """
    try:
        from obs import ObsClient  # noqa: PLC0415 - 故意延迟导入
    except ImportError as exc:
        raise MissingDependencyError(
            "缺少 Python 依赖 obs（esdk-obs-python）。\n"
            "服务器上应已装好（import obs 可用）；如确实缺失，请联系维护者安装，"
            "本技能不会自己装任何包。"
        ) from exc
    return ObsClient(
        access_key_id=credentials["access_key_id"],
        secret_access_key=credentials["secret_access_key"],
        server=f"https://{credentials['endpoint']}",
    )


# --------------------------------------------------------------------------
# OBS 操作（全部走传进来的 client，便于测试注入假客户端）
# --------------------------------------------------------------------------


def resolve_bucket_location(client, bucket: str):
    """用 listBuckets 找同名桶的 location；找不到或读不到返回 None。

    只用于「headBucket 404」时的区域诊断：区域写错时 headBucket 会给 404，
    但 listBuckets 是全局接口，能列出该账号下所有桶及其真实区域。
    """
    try:
        resp = client.listBuckets()
    except Exception:  # noqa: BLE001 - SDK 异常类型不稳定，诊断路径统一降级为「查不到」
        return None
    status = status_of(resp)
    if status is not None and status not in OK_STATUSES:
        return None
    body = _get(resp, "body")
    for item in _get(body, "buckets") or []:
        if str(_get(item, "name") or "") == str(bucket):
            location = _get(item, "location")
            if location:
                return str(location).strip()
            return None
    return None


def region_mismatch_message(bucket: str, endpoint: str, location: str) -> str:
    correct = endpoint_for_region(location)
    return (
        f"OBS 区域不一致：按 OBS_ENDPOINT={endpoint} 访问桶 {bucket} 返回 404，"
        "但这个 404 不代表桶不存在。\n"
        f"listBuckets 显示该桶实际在 {location}。\n"
        f"请把 OBS_ENDPOINT 设为：{correct}\n"
        f"改完后 python3 skills/obs-share/scripts/upload_share.py --check 应当返回 200。\n"
        "判据提醒：区域写错时 createBucket 会返回 409 BucketAlreadyOwnedByYou —— "
        "那同样说明桶已经存在于别的区域，不要据此以为桶不存在。"
    )


def bucket_not_found_message(bucket: str, endpoint: str, detail: str) -> str:
    lines = [
        f"OBS 探测桶失败：桶 {bucket} 在 OBS_ENDPOINT={endpoint} 下返回 404，"
        "且 listBuckets 里没有找到同名桶。",
        f"响应：{detail}",
        "两种可能，请逐条核对：",
        "  1. 区域不一致（最常见）：桶在别的区域。把 OBS_ENDPOINT 改成桶所在区域，"
        "形如 obs.<region>.myhuaweicloud.com。",
        "     自查命令：python3 skills/obs-share/scripts/upload_share.py --check",
        "     旁证：用错的区域调 createBucket 会返回 409 BucketAlreadyOwnedByYou，"
        "说明桶已存在于别处。",
        "  2. OBS_BUCKET 写错：核对桶名拼写与账号。",
    ]
    if str(bucket) == PROJECT_KNOWN_BUCKET:
        lines.append(
            f"已知事实：本项目的桶 {PROJECT_KNOWN_BUCKET} 在服务器上实测区域是 "
            f"{PROJECT_KNOWN_REGION}，对应的 endpoint 是 {endpoint_for_region(PROJECT_KNOWN_REGION)}。"
        )
    return "\n".join(lines)


def check_bucket(client, bucket: str, endpoint: str) -> dict:
    """headBucket 探测桶；404 时做区域自动诊断，抛中文 RemoteError。"""
    resp = client.headBucket(bucket)
    status = status_of(resp)
    if status in OK_STATUSES:
        return {"status": status}
    detail = failure_detail(resp)
    if status == 404:
        location = resolve_bucket_location(client, bucket)
        if location:
            raise RemoteError(region_mismatch_message(bucket, endpoint, location))
        raise RemoteError(bucket_not_found_message(bucket, endpoint, detail))
    raise RemoteError(
        f"OBS 探测桶失败：HEAD {bucket} 在 {endpoint} 下返回 {detail}。\n"
        "常见原因：AK/SK 不对或被禁用（403）、桶名拼错（404）、网络/代理不通、"
        "endpoint 写错区域。用 --check 可以看到更完整的诊断。"
    )


def describe_bucket_acl(client, bucket: str) -> dict:
    """读桶 ACL，判断是否私有（只有 owner 权限，没给匿名/所有用户开放）。"""
    resp = client.getBucketAcl(bucket)
    status = status_of(resp)
    if status is not None and status not in OK_STATUSES:
        return {"private": None, "detail": f"getBucketAcl 返回 {failure_detail(resp)}"}
    body = _get(resp, "body")
    grants = _get(body, "grants") or []
    public = []
    for grant in grants:
        grantee = _get(grant, "grantee")
        permission = str(_get(grant, "permission") or "").upper()
        grantee_type = str(_get(grantee, "type") or "").lower()
        group = str(_get(grantee, "group") or _get(grantee, "uri") or "").lower()
        is_everyone = (
            grantee_type in {"group", "everyone"}
            or "allusers" in group
            or "authenticatedusers" in group
        )
        if is_everyone and any(word in permission for word in ("READ", "WRITE", "FULL_CONTROL")):
            public.append(permission or "READ")
    if public:
        return {
            "private": False,
            "grants": len(grants),
            "detail": "桶 ACL 给匿名/所有用户开放了 " + "、".join(sorted(set(public)))
            + "（这不符合预期：本项目的桶必须是私有的）",
        }
    return {
        "private": True,
        "grants": len(grants),
        "detail": f"未发现匿名组权限（授权条目 {len(grants)} 条，只有 owner/具名用户）",
    }


def count_objects(client, bucket: str, max_keys: int = 1000) -> dict:
    """数桶里的对象（只数第一页，超过一页时标明 is_truncated）。"""
    resp = client.listObjects(bucket, max_keys=max_keys)
    status = status_of(resp)
    if status is not None and status not in OK_STATUSES:
        return {"count": None, "truncated": None, "detail": f"listObjects 返回 {failure_detail(resp)}"}
    body = _get(resp, "body")
    contents = _get(body, "contents") or []
    truncated = bool(_get(body, "isTruncated") or _get(body, "is_truncated"))
    detail = f"第一页 {len(contents)} 个对象" + ("（还有更多，未全部列出）" if truncated else "")
    return {"count": len(contents), "truncated": truncated, "detail": detail}


def read_object_size(client, bucket: str, key: str):
    """getObjectMetadata 取远端对象字节数，返回 (状态码, 字节数或 None)。"""
    resp = client.getObjectMetadata(bucket, key)
    status = status_of(resp)
    if status not in OK_STATUSES:
        raise RemoteError(
            f"校验上传结果失败：HEAD {key} 返回 {failure_detail(resp)}。\n"
            f"确认上传是否真的成功（对象名 {key}）。"
        )
    body = _get(resp, "body")
    for holder in (body, resp):
        value = _get(holder, "contentLength")
        if value is None:
            value = _get(holder, "content_length")
        if value is not None:
            try:
                return status, int(value)
            except (TypeError, ValueError):
                continue
    return status, None


def create_signed_url(client, bucket: str, key: str, expires: int) -> str:
    """生成预签名 GET 链接（默认签名方法写死 GET，不给上传/删除入口）。"""
    resp = client.createSignedUrl(
        method="GET", bucketName=bucket, objectKey=key, expires=int(expires)
    )
    signed = _get(resp, "signedUrl") or _get(resp, "signed_url") or (resp if isinstance(resp, str) else None)
    if not signed:
        raise RemoteError(
            f"生成预签名链接失败：createSignedUrl 没有返回 signedUrl（原样返回：{resp!r}）。"
        )
    signed = str(signed)
    if not signed.lower().startswith("http"):
        raise RemoteError(f"生成预签名链接失败：返回内容不是链接：{signed!r}")
    return signed


def http_get_bytes(url: str, timeout: int = HTTP_TIMEOUT):
    """GET 一个 URL，返回 (状态码, 响应字节)。4xx/5xx 不抛异常。"""
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(getattr(response, "status", 200)), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()
    except urllib.error.URLError as exc:
        raise VerificationError(
            f"链接不可下载：请求预签名链接时网络失败：{exc.reason}\n"
            "请确认服务器能访问 OBS（出网、DNS、代理）。"
        ) from exc


def verify_download(url: str, expected_bytes: int, expected_sha256: str) -> dict:
    """用预签名链接 GET 一次，校验状态码、字节数、内容哈希。"""
    status, body = http_get_bytes(url)
    if status != 200:
        raise VerificationError(
            f"链接不可下载：预签名 GET 返回 HTTP {status}（期望 200）。\n"
            "上传本身可能已经成功（getObjectMetadata 字节数一致），"
            "问题出在链接这一侧：常见原因是签名被改动、链接已过期、"
            "或者服务器时间偏差过大。请重新执行上传以生成新链接。"
        )
    if len(body) != int(expected_bytes):
        raise VerificationError(
            f"链接不可下载：预签名 GET 只取回 {len(body)} 字节，本地文件是 {expected_bytes} 字节。\n"
            "内容长度不一致，链接指向的对象不完整或不对。"
        )
    digest = hashlib.sha256(body).hexdigest()
    if digest != expected_sha256:
        raise VerificationError(
            "链接不可下载：预签名 GET 取回的内容与本地文件不一致（sha256 不同）。\n"
            f"本地 sha256 {expected_sha256}\n远端 sha256 {digest}\n"
            "对象名带随机后缀，几乎不可能是串号；请重试，仍失败则检查是否有同名对象被覆盖。"
        )
    return {"status": status, "bytes": len(body), "sha256": digest, "sha256_match": True}


# --------------------------------------------------------------------------
# 安全闸门：data/ 下的学生数据要显式知情同意
# --------------------------------------------------------------------------


def is_sensitive_path(path: Path, workspace_root: Path) -> bool:
    """路径是否落在 <工作区根>/data 下（学生信息、家长联系方式、成绩都在这里）。"""
    try:
        relative = Path(path).resolve().relative_to(Path(workspace_root).resolve())
    except (ValueError, OSError):
        return False
    return bool(relative.parts) and relative.parts[0] == SENSITIVE_DIR


def sensitive_gate_message(paths, workspace_root: Path) -> str:
    listing = "\n".join(f"  - {item}" for item in paths)
    data_dir = Path(workspace_root) / SENSITIVE_DIR
    return (
        "安全闸门拒绝：下面这些文件在 " + str(data_dir) + " 下，属于学生数据：\n"
        + listing + "\n"
        "\n"
        "workspaces/office/AGENTS.md 的数据安全红线写着："
        "「敏感信息（学生信息、家长联系方式、成绩）仅在工作区内处理，不向外部泄露」。\n"
        "把成绩报告上传到华为云 OBS 与这条红线直接冲突 —— 这是把学生数据上传到第三方云。\n"
        "桶是私有的、链接是有签名的，只能降低风险，不能改变「数据已经离开本机」这个事实。\n"
        "\n"
        "如果你（老师或管理员）已经确认这次外发是必要且被允许的，"
        "请显式加上 --allow-sensitive 表示知情同意：\n"
        "  python3 skills/obs-share/scripts/upload_share.py --file <文件> --allow-sensitive\n"
        "\n"
        "「知情同意」的含义：你本人对这次把学生数据上传到第三方云负责，"
        "并且已经确认这样做符合学校/主管部门的要求（例如已获得授权、已做脱敏）。\n"
        "不加 --allow-sensitive 时，脚本不会读取凭据、不会上传任何字节（退出码 5）。\n"
        "\n"
        "建议：能不出域就不出域 —— 教案、试卷、听力音频不含学生个人信息，可以正常分享；"
        "成绩报告优先在工作区内查看，或发到老师本人的工作渠道。"
    )


def allow_sensitive_notice(sensitive_count: int) -> str:
    if sensitive_count:
        return (
            "[警告] --allow-sensitive 已开启：本次会把 "
            f"{sensitive_count} 个位于 data/ 下的学生数据文件上传到第三方云（华为云 OBS）。\n"
            "[警告] 这些文件离开了本机，请确认你已获得授权、并且知道链接该发给谁。"
        )
    return "[提示] --allow-sensitive 已开启，但本次 --file 里没有 data/ 下的文件，闸门实际未被用到。"


# --------------------------------------------------------------------------
# 审计日志
# --------------------------------------------------------------------------


def append_audit_log(workspace_root: Path, entry: dict) -> Path:
    """往 <工作区根>/obs-uploads.jsonl 追加一行 JSON（一行一个文件）。"""
    path = Path(workspace_root) / AUDIT_FILENAME
    line = json.dumps(entry, ensure_ascii=False, sort_keys=False)
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        raise InputError(
            f"审计日志写入失败：{path}（{exc}）\n"
            "「什么东西离开了服务器」必须留痕，所以审计写不进去时本次上传按失败处理。"
        ) from exc
    return path


# --------------------------------------------------------------------------
# 上传主流程
# --------------------------------------------------------------------------


def upload_object(client, credentials, path: Path, prefix: str, expires: int, sensitive: bool) -> dict:
    """上传一个文件、确认远端字节数、生成预签名链接，返回结果字典。

    这里**不做**链接实测：链接实测由调用方在「审计日志已写入」之后进行，
    这样即便链接有问题，「文件已经离开服务器」这件事也一定留了痕。
    """
    bucket = credentials["bucket"]
    endpoint = credentials["endpoint"]

    # 二进制读取不用（也不能）指定 encoding；文本文件一律显式 encoding="utf-8"。
    data = path.read_bytes()
    size = len(data)
    digest = hashlib.sha256(data).hexdigest()

    key = build_object_key(prefix, path.name)
    resp = client.putObject(bucket, key, data)
    status = status_of(resp)
    if status not in OK_STATUSES:
        raise RemoteError(
            f"上传失败：PUT {key} 返回 {failure_detail(resp)}。\n"
            "常见原因：AK/SK 没有该桶的写权限（403）、桶名或 endpoint 不对、"
            "对象名不合法、网络中断。可用 --check 先确认凭据与桶。"
        )

    meta_status, remote_size = read_object_size(client, bucket, key)
    if remote_size is None:
        raise VerificationError(
            f"上传失败：{key} 上传后读不到对象大小（HTTP {meta_status}），无法确认上传完整性。\n"
            "请重新上传；持续出现请用 --check 检查桶状态。"
        )
    if remote_size != size:
        raise VerificationError(
            f"上传失败：{key} 上传后在 OBS 上的字节数是 {remote_size}，"
            f"本地文件 {path} 是 {size} 字节，两者不一致。\n"
            "远端内容不可信，请不要把链接发出去；重试上传，仍不一致请检查网络与代理。"
        )

    url = create_signed_url(client, bucket, key, expires)
    return {
        "source": path.name,
        "source_path": str(path),
        "object_key": key,
        "bucket": bucket,
        "endpoint": endpoint,
        "bytes": size,
        "sha256": digest,
        "expires": int(expires),
        "expires_at": expires_at_iso(expires),
        "sensitive": bool(sensitive),
        "url": url,
        "bare_url": bare_object_url(bucket, endpoint, key),
        "verified": False,
        "verify": None,
    }


def audit_entry(item: dict, credentials: dict, allow_sensitive: bool) -> dict:
    """审计记录：什么文件、多少字节、什么哈希、发到哪个桶、链接（不含签名）。"""
    return {
        "time": now_iso(),
        "action": "obs-upload",
        "source": item["source"],
        "source_path": item["source_path"],
        "object_key": item["object_key"],
        "bytes": item["bytes"],
        "sha256": item["sha256"],
        "expires": item["expires"],
        "expires_at": item["expires_at"],
        "sensitive": item["sensitive"],
        "allow_sensitive": bool(allow_sensitive),
        "bucket": item["bucket"],
        "endpoint": item["endpoint"],
        "url_without_signature": item["bare_url"],
        "credential_source": credentials["source"],
        "verified": item["verified"],
    }


# ---------------------------------------------------------------------------
# 短链登记（2026-09-17 追加）
#
# 【为什么必须换短链】预签名链接是 `...?Expires=…&AccessKeyId=…&Signature=…`。
# 实测两次：模型把它当密码，**主动把 Signature 打码成 `***`** —— 老师点开 403，
# 等于没给文件（prompt 里明确写了"不许打码"也没用）。短链 `https://<域名>/d/<token>`
# 里没有任何像凭据的东西，照抄不会错；文件仍在私有桶，短链到期即失效。
# 登记失败**不影响**上传：退回到预签名链接并在输出里说明。
# ---------------------------------------------------------------------------
DEFAULT_SHARE_SERVICE = "http://127.0.0.1:18800"
DEFAULT_PUBLIC_BASE = "https://openclaw-teacher.xiaoyangtx.cloud"
SHARE_REGISTER_PATH = "/._share/links"


def register_short_link(url: str, filename: str, expires: int,
                        service: str = "", base: str = "") -> dict | None:
    """把预签名链接登记到登录服务，换回短链。失败返回 None（不抛）。"""
    import urllib.error
    import urllib.request

    service = (service or os.environ.get("TEACHER_SHARE_SERVICE") or DEFAULT_SHARE_SERVICE).rstrip("/")
    base = (base or os.environ.get("TEACHER_PUBLIC_BASE") or DEFAULT_PUBLIC_BASE).rstrip("/")
    body = json.dumps({"url": url, "filename": filename, "expires_in": expires, "base": base},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"{service}{SHARE_REGISTER_PATH}", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    short = data.get("short_url") or (base + (data.get("short_path") or ""))
    return {"short_url": short, "token": data.get("token"), "expires_at": data.get("expires_at")} \
        if short else None


def upload_human_lines(payload: dict) -> list:
    lines = [
        f"[完成] 已上传 {len(payload['uploaded'])} 个文件到 OBS 私有桶 {payload['bucket']}"
        f"（endpoint {payload['endpoint']}）",
        "",
    ]
    for index, item in enumerate(payload["uploaded"], 1):
        lines.append(f"{index}. {item['source_path']}")
        lines.append(f"   对象名：{item['object_key']}")
        lines.append(f"   大小：{item['bytes']} 字节，sha256 {item['sha256'][:16]}…")
        if item.get("short_url"):
            # 老师用这条：短、没有签名，模型照抄不会打码
            lines.append(f"   ★ 下载链接（给老师，{item['expires'] // 86400} 天内有效，截止 {item['expires_at']}）：")
            lines.append(f"   {item['short_url']}")
            lines.append(f"   （管理员排查用的带签名直链：{item['url']}）")
        else:
            lines.append(f"   下载链接（{item['expires']} 秒内有效，截止 {item['expires_at']}）：")
            lines.append(f"   {item['url']}")
            lines.append("   ⚠️ 短链登记失败（登录服务不可达？）—— 上面这条必须**原样完整**贴给老师，"
                         "一个字符都不能改、不能打码。")
        if item["verified"]:
            lines.append(
                f"   自校验：getObjectMetadata 字节数一致；预签名 GET "
                f"{item['verify']['status']}，取回字节数与 sha256 均一致"
            )
        else:
            lines.append("   自校验：getObjectMetadata 字节数一致；--no-verify 已关闭链接实测")
    lines.append("")
    if any(item.get("short_url") for item in payload["uploaded"]):
        lines.append(
            "提醒：**给老师的是 ★ 那条短链**（形如 https://域名/d/xxxx），它本身不含任何凭据、"
            "可以放心贴进对话；短链过期后重跑本脚本即可。"
        )
    else:
        lines.append(
            "提醒：链接带签名与有效期，过期后需要重新生成（重跑本脚本即可）；"
            "桶是私有的，去掉 ?Expires=…&AccessKeyId=…&Signature=… 的裸链接打不开（403，属预期行为）。"
        )
        lines.append(
            "提醒：链接本身就是凭据（含 AccessKeyId 与 Signature），只发给该拿文件的老师，不要发到公开群里。"
        )
    lines.append(f"审计：已追加到 {payload['audit_log']}")
    if payload.get("allow_sensitive"):
        lines.append("[警告] 本次开启了 --allow-sensitive，包含 data/ 下的学生数据，已上传到第三方云。")
    return lines


def check_human_lines(payload: dict) -> list:
    lines = [
        "[诊断] OBS 连接诊断（--check 只诊断，不上传、不生成链接）",
        f"  凭据来源：{payload['credential_source']}",
        f"  AK：{payload['access_key_length']} 字符（不显示值）"
        f"  SK：{payload['secret_key_length']} 字符（不显示值）",
        f"  桶：{payload['bucket']}",
        f"  endpoint：{payload['endpoint']}",
        f"  区域：{payload['region'] or '（无法从 endpoint 解析）'}",
        f"  headBucket：{payload['head_bucket']['status']}",
    ]
    acl = payload["acl"]
    private_text = {True: "私有（未给匿名开放）", False: "不是私有的（有匿名/所有用户权限）"}.get(
        acl["private"], "无法判定"
    )
    lines.append(f"  桶 ACL：{private_text}；{acl['detail']}")
    objects = payload["objects"]
    if objects["count"] is None:
        lines.append(f"  对象数：读取失败；{objects['detail']}")
    else:
        lines.append(f"  对象数：{objects['count']}{'（已截断）' if objects['truncated'] else ''}；{objects['detail']}")
    for warning in payload["warnings"]:
        lines.append(f"  [警告] {warning}")
    if payload["ok"]:
        lines.append("  结论：凭据与桶都可用，可以上传。")
    else:
        lines.append("  结论：诊断发现问题，先按上面的警告处理，再尝试上传。")
    return lines


def run_check(client, credentials: dict, as_json: bool) -> int:
    """--check：读凭据、探测桶、打印 endpoint/区域/ACL 是否私有/对象数。"""
    bucket = credentials["bucket"]
    endpoint = credentials["endpoint"]
    payload = {
        "ok": True,
        "mode": "check",
        "bucket": bucket,
        "endpoint": endpoint,
        "region": region_of_endpoint(endpoint),
        "credential_source": credentials["source"],
        "access_key_length": len(credentials["access_key_id"]),
        "secret_key_length": len(credentials["secret_access_key"]),
        "warnings": [],
    }
    code = EXIT_OK
    payload["head_bucket"] = check_bucket(client, bucket, endpoint)

    try:
        acl = describe_bucket_acl(client, bucket)
    except Exception as exc:  # noqa: BLE001 - SDK 异常类型不稳定，诊断要给出结论而不是堆栈
        acl = {"private": None, "detail": f"读取桶 ACL 失败：{exc}"}
    payload["acl"] = acl
    if acl["private"] is None:
        payload["warnings"].append(acl["detail"])
        payload["ok"] = False
        code = EXIT_REMOTE
    elif acl["private"] is False:
        payload["warnings"].append(
            "桶不是私有的：拿到对象名的人不签名也能下载。请把桶 ACL 改回私有（只有 owner）。"
        )
        payload["ok"] = False
        code = EXIT_SENSITIVE

    try:
        objects = count_objects(client, bucket)
    except Exception as exc:  # noqa: BLE001
        objects = {"count": None, "truncated": None, "detail": f"读取对象列表失败：{exc}"}
    payload["objects"] = objects
    if objects["count"] is None:
        payload["warnings"].append(objects["detail"])
        payload["ok"] = False
        if code == EXIT_OK:
            code = EXIT_REMOTE

    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("\n".join(check_human_lines(payload)), file=sys.stderr)
    else:
        print("\n".join(check_human_lines(payload)))
    return code


def run_upload(args, workspace_root: Path) -> int:
    expires = validate_expires(args.expires)
    prefix = sanitize_prefix(args.prefix)

    if not args.file:
        raise InputError(
            "缺少 --file：至少要给一个要上传的文件。\n"
            "只想诊断凭据与桶，请用 --check（不需要 --file）。"
        )

    targets = [resolve_target(item, workspace_root) for item in args.file]
    sensitive_targets = [path for path in targets if is_sensitive_path(path, workspace_root)]
    if sensitive_targets and not args.allow_sensitive:
        raise SensitiveGateError(sensitive_gate_message(sensitive_targets, workspace_root))
    if args.allow_sensitive:
        print(allow_sensitive_notice(len(sensitive_targets)), file=sys.stderr)
    sensitive_set = set(sensitive_targets)

    credentials = load_credentials()
    client = create_obs_client(credentials)
    endpoint, bucket = credentials["endpoint"], credentials["bucket"]
    check_bucket(client, bucket, endpoint)

    uploaded = []
    audit_path = None
    for target in targets:
        print(f"[进度] 正在上传 {target} ...", file=sys.stderr)
        item = upload_object(
            client,
            credentials,
            target,
            prefix=prefix,
            expires=expires,
            sensitive=target in sensitive_set,
        )
        try:
            if args.verify:
                # 不是只看状态码：真的把内容取回来，比字节数（再比 sha256）。
                item["verify"] = verify_download(item["url"], item["bytes"], item["sha256"])
                item["verified"] = True
        finally:
            # 上传已被 getObjectMetadata 确认，文件确实离开了服务器：无论链接实测
            # 成功还是失败，都要先把审计写下来，再决定是否把错误抛给调用方。
            audit_path = append_audit_log(
                workspace_root, audit_entry(item, credentials, args.allow_sensitive)
            )
        # 登记短链（上传已确认成功；失败只记下来，不影响交付）
        short = register_short_link(item["url"], Path(item["source_path"]).name, expires)
        item["short_url"] = (short or {}).get("short_url") or ""
        item["short_token"] = (short or {}).get("token") or ""
        uploaded.append(item)
        print(f"[进度] 已上传并校验 {item['object_key']}", file=sys.stderr)

    payload = {
        "ok": True,
        "mode": "upload",
        "bucket": bucket,
        "endpoint": endpoint,
        "region": region_of_endpoint(endpoint),
        "prefix": prefix,
        "expires": expires,
        "expires_at": expires_at_iso(expires),
        "credential_source": credentials["source"],
        "allow_sensitive": bool(args.allow_sensitive),
        "sensitive_files": [str(path) for path in sensitive_targets],
        "verified": bool(args.verify),
        "audit_log": audit_path.name,
        "uploaded": uploaded,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("\n".join(upload_human_lines(payload)), file=sys.stderr)
    else:
        print("\n".join(upload_human_lines(payload)))
    return EXIT_OK


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------


class ChineseArgumentParser(argparse.ArgumentParser):
    """argparse 的报错也走本技能的中文错误与退出码（2）。"""

    def error(self, message):  # noqa: D102 - 覆盖基类行为
        raise InputError(f"参数错误：{message}\n用 --help 查看完整参数说明。")


def build_parser() -> argparse.ArgumentParser:
    parser = ChineseArgumentParser(
        prog="upload_share.py",
        description=(
            "把产出文件上传到华为云 OBS 私有桶，生成带签名的下载链接（可发微信）。"
            "cwd 必须是工作区根目录。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "退出码：0 成功 / 2 参数、文件或自校验错误 / 3 凭据或依赖问题 / "
            "4 OBS 远端错误 / 5 安全闸门拒绝（data/ 下未加 --allow-sensitive，"
            "或 --check 发现桶不是私有的）。\n"
            "凭据：环境变量 OBS_ACCESS_KEY / OBS_SECRET_KEY / OBS_BUCKET / OBS_ENDPOINT，"
            "其次 ~/.openclaw/.env（可用 OBS_ENV_FILE 覆盖路径）。\n"
            "审计：每次成功上传都会往 cwd 的 obs-uploads.jsonl 追加一行。"
        ),
    )
    parser.add_argument(
        "--file",
        action="append",
        default=[],
        metavar="路径",
        help="要上传的文件，可重复传入以一次上传多个；相对路径按 cwd（工作区根）解析。",
    )
    parser.add_argument(
        "--expires",
        type=int,
        default=EXPIRES_DEFAULT,
        metavar="秒",
        help=f"链接有效期（秒），默认 {EXPIRES_DEFAULT}（24 小时），上限 {EXPIRES_MAX}（7 天）。",
    )
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        metavar="前缀",
        help=f"对象名前缀，默认 {DEFAULT_PREFIX}；会用 / 分层，例如 {DEFAULT_PREFIX}/2026-09-15/...。",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="结果以单一合法 JSON 打到 stdout，人类可读摘要一律走 stderr。",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只诊断：读凭据、探测桶、打印 endpoint/区域/ACL 是否私有/对象数；不上传、不产出链接。",
    )
    parser.add_argument(
        "--allow-sensitive",
        action="store_true",
        help="知情同意：允许上传 data/ 下的学生数据（成绩报告等）到第三方云；不加则被拒绝（退出码 5）。",
    )
    parser.add_argument(
        "--verify",
        dest="verify",
        action="store_true",
        default=True,
        help="上传后真的用预签名链接 GET 一次，校验状态码、字节数与内容哈希（默认开启）。",
    )
    parser.add_argument(
        "--no-verify",
        dest="verify",
        action="store_false",
        help="跳过链接实测，只校验 getObjectMetadata 的字节数。",
    )
    return parser


def main(argv=None) -> int:
    # parse_args 也在 try 里：argparse 的参数错误（例如 --expires abc）会走
    # ChineseArgumentParser.error -> InputError -> 退出码 2，而不是抛栈。
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        workspace_root = Path.cwd()
        if args.check:
            if args.file:
                print("[提示] --check 只做诊断，本次忽略 --file。", file=sys.stderr)
            credentials = load_credentials()
            client = create_obs_client(credentials)
            return run_check(client, credentials, args.json)
        return run_upload(args, workspace_root)
    except SkillError as exc:
        # 失败时 stdout 保持为空（--json 模式下也不写半截 JSON），错误只在 stderr。
        print(f"[错误] {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("[错误] 已被中断，未完成的上传请重新执行。", file=sys.stderr)
        return EXIT_ARG


if __name__ == "__main__":
    sys.exit(main())
