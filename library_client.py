# -*- coding: utf-8 -*-
"""
library_client.py —— 直连 WorkBuddy 资料库团队空间 REST 客户端
==============================================================
发布后的轻应用运行在云沙箱，无法调用本地 CLI 脚本，必须直连 REST：
    POST https://www.workbuddy.cn/space/api/agent/v1/{query-database|batch-add-records|batch-update-records}
    Header: X-Skill-Token: <token>
    信封:   {code, msg, data}

字段值使用 oneof 结构 {类型key: 值}，例如：
    {"合同编号": {"text": "HT-001"}}
    {"月租金":   {"currency": 45000}}
    {"审批状态": {"select": "待审"}}
    {"日期":     {"date": "2026-06-24"}}
    {"启用":     {"checkbox": true}}
"""
import hashlib
import json
import os
import ssl
import urllib.error
import urllib.request

_TRUE = frozenset({"1", "true", "yes", "y", "on", "enabled"})


def is_sandbox() -> bool:
    """发布沙箱（cloudstudio）模式：走 auth-proxy，无需自带 token。"""
    return os.environ.get("X_IDE_IS_CLOUDSTUDIO", "").strip().lower() in _TRUE


def _detect_base() -> str:
    if is_sandbox():
        return os.environ.get("ZL_SANDBOX_BASE", "http://codebuddy.auth-proxy.local")
    return "https://www.workbuddy.cn"


BASE = os.environ.get("ZL_API_BASE") if os.environ.get("ZL_API_BASE") else _detect_base()
UV = ssl.create_default_context()
HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "zh-CN",
    "Content-Type": "application/json",
    "User-Agent": "zl-lightapp/1.0",
}


def set_token(tok: str) -> None:
    HEADERS["X-Skill-Token"] = tok


def get_token() -> str:
    return HEADERS.get("X-Skill-Token", "")


def _post(path: str, body: dict, timeout: float = 25.0):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=HEADERS,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=UV) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {e.code}: {raw[:200]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络错误: {e.reason}")
    if payload.get("code") not in (0, None):
        raise RuntimeError(f"业务错误 {payload.get('code')}: {payload.get('msg')}")
    return payload.get("data")


def query(db: str, filt=None, fields=None, sorts=None, page_size=200) -> list:
    body = {"databaseId": db, "pageSize": page_size}
    if filt:
        body["filter"] = filt
    if fields:
        body["fields"] = fields
    if sorts:
        body["sorts"] = sorts
    data = _post("/space/api/agent/v1/query-database", body)
    return _norm(data.get("results", [])) if data else []


def _norm(records):
    """把返回记录的 _id 转为 record_id（与官方 CLI 对齐）。"""
    out = []
    for rec in records or []:
        if isinstance(rec, dict):
            rec = dict(rec)
            if "_id" in rec:
                rec["record_id"] = rec.pop("_id")
        out.append(rec)
    return out


def query_all(db: str, filt=None, fields=None, sorts=None) -> list:
    """分页拉取全部记录。"""
    out, cursor = [], None
    for _ in range(50):
        body = {"databaseId": db, "pageSize": 200}
        if filt:
            body["filter"] = filt
        if fields:
            body["fields"] = fields
        if sorts:
            body["sorts"] = sorts
        if cursor:
            body["startCursor"] = cursor
        data = _post("/space/api/agent/v1/query-database", body) or {}
        out.extend(data.get("results", []))
        cursor = data.get("nextCursor")
        if not data.get("hasMore") or not cursor:
            break
    return out


def get_record(db: str, record_id: str) -> dict | None:
    """按 record_id 取单条记录（对齐官方 get-record：返回 data.result 记录体）。"""
    data = _post("/space/api/agent/v1/get-record", {"databaseId": db, "recordId": record_id})
    if not data:
        return None
    rec = data.get("result") or data.get("record") or data
    if isinstance(rec, dict):
        rec = dict(rec)
        if "_id" in rec:
            rec["record_id"] = rec.pop("_id")
        if "record_id" not in rec:
            rec["record_id"] = record_id
        return rec
    return {"record_id": record_id}


def add(db: str, records: list) -> list:
    data = _post("/space/api/agent/v1/batch-add-records", {"databaseId": db, "records": records})
    return (data or {}).get("results", [])


def delete(db: str, record_ids: list) -> list:
    data = _post("/space/api/agent/v1/batch-delete-records", {"databaseId": db, "recordIds": record_ids})
    return (data or {}).get("results", [])


def update(db: str, records: list) -> list:
    """records: [{"record_id": "...", "properties": {字段: {type: 值}}}]"""
    norm = [{"recordId": r["record_id"], "properties": r["properties"]} for r in records]
    data = _post("/space/api/agent/v1/batch-update-records", {"databaseId": db, "records": norm})
    return (data or {}).get("results", [])


class WriteError(RuntimeError):
    """写入未真正生效时抛出，避免"接口返回成功、数据其实没变"这类静默失败。"""


def update_checked(db: str, records: list) -> list:
    """更新并逐条校验结果；条数不符或任一失败即抛 WriteError。"""
    res = update(db, records) or []
    if len(res) != len(records):
        raise WriteError("更新结果条数不符：期望 %d 条，实际 %d 条" % (len(records), len(res)))
    failed = [r for r in res if isinstance(r, dict) and not r.get("success", True)]
    if failed:
        raise WriteError("更新失败：" + str(failed[:2])[:300])
    return res


# ---- 密码哈希 ----------------------------------------------------------
def hash_pwd(salt: str, pwd: str) -> str:
    return hashlib.sha256((salt + pwd).encode("utf-8")).hexdigest()
