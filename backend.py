# -*- coding: utf-8 -*-
"""
租赁线上化 · 轻应用后端（v2，对应 19 张规范表）
====================================================
核心闭环（阶段4）：
    登录 → 租赁卡片草稿 → 生成三台账（合同基础信息=卡片主表；合同法台账=合同金额明细；IFRS台账=IFRS明细）
    → 人工复核/手调定稿 → 提交一级审批 → 审批通过/退回/重提 → 团队空间回读。

角色（13类，见 config.roles）：
    管理员 / 台账维护人 / 台账审批人 / 租赁事务对接人 / 付款审批人 / 资金管理处 / 税务处 /
    业务财务处 / 年报项目组 / 共享交付中心(×3) / IT管理员。

关键约定：
    - token 走 body._token / url?token=，绝不用 Authorization header（发布平台占用）。
    - 金额一律 Decimal 定点数，禁止 float。
    - 台账生成幂等：未提交前可重复生成（先删旧草稿台账行再生成）；提交后冻结。
"""
import json
import os
import sys
import threading
from datetime import datetime, timedelta, date
from decimal import Decimal, ROUND_HALF_UP, getcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import base64
import hashlib
import hmac as _hmac
import re
import urllib.request
import urllib.error
import socket

import library_client as lc
import workflow as W

if os.environ.get('ZL_STORAGE') == 'local':
    import local_store as lc

getcontext().prec = 28

HERE = Path(__file__).resolve().parent
if os.environ.get('ZL_STORAGE') == 'local':
    _keys = 'md_suppliers md_depts md_account_rules md_contacts md_entities md_sites md_params cards amount_overview contract_ledger ifrs_detail oneoff_fees cost_share pay_plan vouchers approval_tickets version_snapshots task_pool users_roles'.split()
    CONFIG = {'tables': {k:k for k in _keys}, 'salt':'lease-local', 'session_ttl_hours':8}
else:
    CONFIG = json.loads((HERE / "config.json").read_text("utf-8"))
SALT = CONFIG["salt"]
TBL = CONFIG["tables"]

# 角色常量
ROLE_ADMIN = "管理员"
ROLE_KEEPER = "台账维护人"
ROLE_APPROVER = "台账审批人"

_glob = threading.RLock()
# 远程团队空间读取缓存：短 TTL 只用于减少重复读，任何写操作都会清空。
_RESPONSE_CACHE = {}
_RESPONSE_CACHE_LOCK = threading.RLock()
def clear_response_cache():
    with _RESPONSE_CACHE_LOCK:
        _RESPONSE_CACHE.clear()
def cached_response(key, producer, ttl=5):
    now = datetime.utcnow().timestamp()
    with _RESPONSE_CACHE_LOCK:
        hit = _RESPONSE_CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = producer()
    with _RESPONSE_CACHE_LOCK:
        _RESPONSE_CACHE[key] = (now, value)
    return value

# ---- 会话（无状态 HMAC，跨实例可用） -----------------------------------
try:
    _SESSION_KEY = os.environ["ZL_SESSION_KEY"]
except KeyError:
    _SESSION_KEY = CONFIG.get("session_key") or "zl-session-key-9f3c7d1e8a2b"
TTL_SECONDS = int(CONFIG["session_ttl_hours"]) * 3600


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload_b64: str) -> str:
    return _b64e(_hmac.new(_SESSION_KEY.encode(), payload_b64.encode(), hashlib.sha256).digest())


def _now() -> datetime:
    return datetime.now()


def create_session(account: str, name: str, role: str) -> str:
    exp = int(_now().timestamp()) + TTL_SECONDS
    payload = f"{account}|{name}|{role}|{exp}"
    p64 = _b64e(payload.encode())
    return f"{p64}.{_sign(p64)}"


def _body_or_query_token(req) -> str:
    body = getattr(req, "_loaded_body", None)
    if isinstance(body, dict):
        t = body.get("_token") or body.get("token")
        if t:
            return str(t)
    try:
        q = parse_qs(urlparse(req.path).query)
    except Exception:
        return ""
    if q.get("token"):
        return q["token"][0]
    return ""


def current(req, explicit_token=None) -> dict | None:
    raw = explicit_token or _body_or_query_token(req)
    if not raw:
        return None
    try:
        p64, sig = raw.split(".", 1)
    except (ValueError, AttributeError):
        return None
    if not _hmac.compare_digest(_sign(p64), sig):
        return None
    try:
        account, name, role, exp = _b64d(p64).decode().split("|")
    except Exception:
        return None
    if int(exp) < int(_now().timestamp()):
        return None
    return {"account": account, "name": name, "role": role}


# ---- 密码哈希（PBKDF2，非单次 sha256） ---------------------------------
_PBKDF2_ITER = 100000


def hash_pwd(pwd: str) -> str:
    salt = (SALT or "zl_salt::").encode("utf-8")
    dk = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt, _PBKDF2_ITER)
    return "pbkdf2$%d$%s$%s" % (_PBKDF2_ITER, SALT or "zl_salt::", dk.hex())


def verify_pwd(pwd: str, stored: str) -> bool:
    if not stored:
        return False
    if not stored.startswith("pbkdf2$"):
        # 兼容旧 sha256 格式
        return lc.hash_pwd(SALT, pwd) == stored
    try:
        _, iters, salt, hexdk = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt.encode("utf-8"), int(iters))
        return _hmac.compare_digest(dk.hex(), hexdk)
    except Exception:
        return False


# ---- 字段值读取归一化（直连 REST 返回值可能是裸值或 oneof 包裹） --------
def val(record, field, default=""):
    v = record.get(field)
    if v is None:
        return default
    if isinstance(v, dict):
        for k in ("text", "select", "number", "currency", "checkbox", "date", "url"):
            if k in v:
                return v[k]
        if "title" in v:
            return v["title"]
        return default
    return v


def norm_d(v):
    if v is None:
        return ""
    s = str(v)
    return s[:10] if len(s) >= 10 else s


def dnum(v) -> Decimal:
    try:
        if isinstance(v, dict):
            v = next(iter(v.values()), 0)
        return Decimal(str(v).replace(",", "") or "0")
    except Exception:
        return Decimal("0")


# ---- 用户读取 ----------------------------------------------------------
def find_user(account: str):
    for u in lc.query(TBL["users_roles"], filt={
            "and": [
                {"property": {"property": "登录账号", "text": {"equals": account}}},
                {"property": {"property": "启用", "checkbox": {"equals": True}}},
            ]}):
        return u
    return None


# ---- 卡片 --------------------------------------------------------------
def _cards_query(filt=None, sorts=None):
    return lc.query(TBL["cards"], filt=filt, sorts=sorts)


def _ledger_by_card(card_id):
    return lc.query(TBL["contract_ledger"], filt={
        "property": {"property": "关联卡片ID", "text": {"equals": card_id}}})


def _ifrs_by_card(card_id):
    return lc.query(TBL["ifrs_detail"], filt={
        "property": {"property": "关联卡片ID", "text": {"equals": card_id}}})


def _overview_by_card(card_id):
    return lc.query(TBL["amount_overview"], filt={
        "property": {"property": "关联卡片ID", "text": {"equals": card_id}}})


# ---- 账号申请：自助注册不可用，须提交申请由管理员开通 -------------------
# 可申请的角色（管理员不开放申请，避免越权拿到全权限）
APPLICABLE_ROLES = ["台账维护人", "台账审批人", "租赁事务对接人", "付款审批人",
                    "资金管理处", "税务处", "业务财务处", "年报项目组",
                    "共享交付中心(运营支持组)", "共享交付中心(收入费用人力资产组)"]


def register_request(b):
    """提交账号申请：写入用户表但「启用=false」，未开通前无法登录。"""
    acct = str(b.get("账号") or "").strip()
    name = str(b.get("姓名") or "").strip()
    pwd = str(b.get("密码") or "")
    role = str(b.get("角色") or "").strip()
    note = str(b.get("申请说明") or "").strip()
    if not acct or len(acct) < 3:
        return {"ok": False, "error": "账号至少 3 个字符"}
    if not re.fullmatch(r"[A-Za-z0-9_.\-@]+", acct):
        return {"ok": False, "error": "账号只能包含字母、数字、下划线、点、横线、@"}
    if not name:
        return {"ok": False, "error": "请填写姓名"}
    if len(pwd) < 6:
        return {"ok": False, "error": "密码至少 6 位"}
    if role not in APPLICABLE_ROLES:
        return {"ok": False, "error": "请选择有效角色"}
    with _glob:
        dup = lc.query(TBL["users_roles"], filt={
            "property": {"property": "登录账号", "text": {"equals": acct}}})
        if dup:
            return {"ok": False, "error": "该账号已存在或已提交过申请，请直接登录或联系管理员"}
        res = lc.add(TBL["users_roles"], [{
            "登录账号": {"text": acct},
            "密码哈希": {"text": hash_pwd(pwd)},
            "姓名": {"text": name},
            "角色": {"select": role},
            "启用": {"checkbox": False},
            "申请说明": {"text": note[:200]},
            "申请时间": {"date": _now().strftime("%Y-%m-%dT%H:%M:%SZ")},
        }])
        failed = [r for r in (res or []) if isinstance(r, dict) and not r.get("success", True)]
        if failed or not res:
            return {"ok": False, "error": "申请提交失败：" + str(failed[:1])[:200]}
        return {"ok": True, "account": acct, "name": name, "role": role}


def list_requests(cur):
    """管理员查看待开通的账号申请。"""
    return lc.query(TBL["users_roles"], filt={
        "property": {"property": "启用", "checkbox": {"equals": False}}})


def decide_request(cur, user_id, approve, role=None):
    """管理员开通或驳回账号申请。驳回=删除申请记录（不保留可登录凭据）。"""
    with _glob:
        u = lc.get_record(TBL["users_roles"], user_id)
        if not u:
            return {"ok": False, "error": "申请不存在"}
        if val(u, "启用", False) in (True, "是"):
            return {"ok": False, "error": "该账号已开通，无需重复处理"}
        if not approve:
            lc.delete(TBL["users_roles"], [user_id])
            return {"ok": True, "action": "rejected", "account": val(u, "登录账号")}
        props = {"启用": {"checkbox": True}}
        if role:
            if role not in APPLICABLE_ROLES and role != ROLE_ADMIN:
                return {"ok": False, "error": "角色不合法"}
            props["角色"] = {"select": role}
        lc.update_checked(TBL["users_roles"], [{"record_id": user_id, "properties": props}])
        chk = lc.get_record(TBL["users_roles"], user_id)
        if not val(chk, "启用", False):
            return {"ok": False, "error": "开通失败：账号仍为未启用状态，请重试"}
        return {"ok": True, "action": "approved", "account": val(u, "登录账号"),
                "role": val(chk, "角色"), "name": val(u, "姓名")}


# ---- 台账生成：金额概览 → 月度展开 -------------------------------------
def _month_first(d):
    return date(d.year, d.month, 1)


def _next_month_first(d):
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def _days_in_month(d):
    return (_next_month_first(d) - d).days


def expand_monthly(segments):
    """把金额概览分段（start/end/monthly）展开成自然月明细。

    返回 [(month_first_date, monthly_amount_in_this_month)]，已合并同月。
    整月按 monthly；首尾不足月按剩余天数/当月天数折算（十进制，4位舍入）。
    seg: {"起始日": "YYYY-MM-DD", "终止日": "YYYY-MM-DD", "月金额": Decimal}
    """
    month_map = {}
    for seg in segments:
        s = datetime.strptime(norm_d(seg["起始日"])[:10], "%Y-%m-%d").date()
        e = datetime.strptime(norm_d(seg["终止日"])[:10], "%Y-%m-%d").date()
        if e < s:
            continue
        monthly = dnum(seg.get("月金额"))
        cur = _month_first(s)
        last = _month_first(e)
        while cur <= last:
            cur_end = _next_month_first(cur) - timedelta(days=1)
            # 本段在本月内的头尾点
            m_start = s if s > cur else cur
            m_end = e if e < cur_end else cur_end
            span_days = (m_end - m_start).days + 1
            full_days = (cur_end - cur).days + 1
            amt = (monthly * Decimal(span_days) / Decimal(full_days)).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP)
            month_map[cur.strftime("%Y-%m-%d")] = month_map.get(cur.strftime("%Y-%m-%d"), Decimal("0")) + amt
            cur = _next_month_first(cur)
    return [(k, month_map[k]) for k in sorted(month_map)]


def monthly_valid_dates(segs):
    """金额概览每段必须有合法且先后正确的起止日期。"""
    for s in segs:
        a, b = norm_d(s.get("起始日"))[:10], norm_d(s.get("终止日"))[:10]
        if len(a) != 10 or len(b) != 10:
            return False
        try:
            if date.fromisoformat(b) < date.fromisoformat(a):
                return False
        except ValueError:
            return False
    return bool(segs)


def _calc_ledger_balances(rows):
    """给定月度行 [{月份, 合同金额, 降租金额, 未付款, 已付款}]，补算实付金额合计与预付科目余额（递推）。"""
    bal = Decimal("0")
    out = []
    for r in rows:
        contract = dnum(r.get("合同金额"))
        cut = dnum(r.get("降租金额"))
        unpaid = dnum(r.get("未付款"))
        paid = dnum(r.get("已付款"))
        actual = (contract + cut).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        bal = (bal + unpaid + paid - actual).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        r2 = dict(r)
        r2["实付金额合计"] = actual
        r2["预付科目余额"] = bal
        out.append(r2)
    return out


def _calc_ifrs(card, monthly_payments):
    """按 IFRS16 折现摊销，返回行列表；末行负债/资产余额归零校验。

    monthly_payments: [(month_str, payment_amount)]
    r = IFRS折现率；付款额=该月租赁付款额（初始用合同金额明细实付合计）。
    """
    r = dnum(val(card, "IFRS折现率"))
    if r <= 0:
        return None
    n = len(monthly_payments)
    if n == 0:
        return None
    q = Decimal("0.0001")
    pvs = []
    for t, (_, pay) in enumerate(monthly_payments, start=1):
        pay = dnum(pay)
        pv = (pay / ((Decimal("1") + r) ** t)).quantize(q, rounding=ROUND_HALF_UP)
        pvs.append(pv)
    asset_orig = sum(pvs).quantize(q, rounding=ROUND_HALF_UP)
    liability = asset_orig
    asset_balance = asset_orig
    rows = []
    for t, (m, pay) in enumerate(monthly_payments, start=1):
        pay = dnum(pay)
        interest = (liability * r).quantize(q, rounding=ROUND_HALF_UP)
        depreciation = (asset_orig / Decimal(n)).quantize(q, rounding=ROUND_HALF_UP)
        liability = (liability + interest - pay).quantize(q, rounding=ROUND_HALF_UP)
        asset_balance = (asset_balance - depreciation).quantize(q, rounding=ROUND_HALF_UP)
        rows.append({
            "月份": m, "行类型": "月度", "资产折旧": depreciation, "负债利息": interest,
            "租赁付款额": pay, "折现倍数": t, "现值": pvs[t - 1],
            "租赁负债余额": liability, "使用权资产余额": asset_balance,
        })
    if abs(rows[-1]["租赁负债余额"]) > Decimal("0.01"):
        raise ValueError("IFRS末行租赁负债余额未归零: %s" % rows[-1]["租赁负债余额"])
    if abs(rows[-1]["使用权资产余额"]) > Decimal("0.01"):
        raise ValueError("IFRS末行使用权资产余额未归零: %s" % rows[-1]["使用权资产余额"])
    return rows


def _delete_ledger_rows(card_id):
    for r in _ledger_by_card(card_id):
        lc.delete(TBL["contract_ledger"], [r["record_id"]])
    for r in _ifrs_by_card(card_id):
        lc.delete(TBL["ifrs_detail"], [r["record_id"]])


def generate_ledgers(cur, card_id):
    """生成三台账：金额概览 → 合同金额明细(合同法台账) + IFRS明细。幂等。返回摘要。"""
    with _glob:
        card = lc.get_record(TBL["cards"], card_id)
        if not card:
            return {"ok": False, "error": "卡片不存在"}
        state = val(card, "审批状态", "草稿")
        if state not in ("草稿", "退回"):
            return {"ok": False, "error": f"当前状态[{state}]不可生成台账，仅草稿/退回可操作"}
        ovs = _overview_by_card(card_id)
        if not ovs:
            return {"ok": False, "error": "请先录入金额概览（计费分段）再生成台账"}

        segs = [{"起始日": val(o, "租赁起始日"), "终止日": val(o, "租赁终止日"),
                 "月金额": val(o, "月金额")} for o in ovs]
        # 防止"录了行但没填金额"生成一叠全 0 台账（免租段月金额为 0 是合法的，但整段不能全为 0）
        total_amt = sum((dnum(o.get("月金额")) * dnum(o.get("月份数")) for o in ovs), Decimal("0"))
        if total_amt == 0:
            return {"ok": False,
                    "error": "各分段「月金额」合计为 0，生成出的台账会全是 0。"
                             "请在「录入金额」里填写每段的月金额后再生成（免租期可单独填 0）。"}
        if not monthly_valid_dates(segs):
            return {"ok": False, "error": "金额概览存在无效起止日期，请检查后重新保存"}
        monthly = expand_monthly(segs)   # [(month, amt)]

        ledger_rows = []
        for m, amt in monthly:
            ledger_rows.append({
                "月份": m, "合同金额": amt, "降租金额": Decimal("0"),
                "未付款": amt, "已付款": Decimal("0"),
            })
        ledger_rows = _calc_ledger_balances(ledger_rows)
        if abs(ledger_rows[-1]["预付科目余额"]) > Decimal("0.01"):
            return {"ok": False, "error": "合同法台账末行预付科目余额未归零"}

        ifrs_rows = None
        if val(card, "是否IFRS", "") == "是":
            _r = dnum(val(card, "IFRS折现率"))
            if _r <= 0:
                return {"ok": False,
                        "error": "本合同「是否IFRS」为“是”，但「IFRS折现率」未填写或为 0，无法计算 IFRS 台账。"
                                 "请到「编辑基础信息」填写 IFRS 折现率（例如 0.04 表示 4%）后再生成。"}
            try:
                ifrs_rows = _calc_ifrs(card, [(mr["月份"], mr["实付金额合计"]) for mr in ledger_rows])
            except ValueError as e:
                return {"ok": False, "error": str(e)}

        # 幂等：清旧再生成
        _delete_ledger_rows(card_id)

        # 写合同金额明细
        if ledger_rows:
            payload = [{
                "关联卡片ID": {"text": card_id},
                "版本ID": {"text": val(card, "版本号", "1")},
                "月份": {"date": r["月份"]},
                "合同金额": {"currency": float(r["合同金额"])},
                "降租金额": {"currency": float(r["降租金额"])},
                "未付款": {"currency": float(r["未付款"])},
                "已付款": {"currency": float(r["已付款"])},
                "实付金额合计": {"currency": float(r["实付金额合计"])},
                "预付科目余额": {"currency": float(r["预付科目余额"])},
                "勾稽通过": {"checkbox": True},
            } for r in ledger_rows]
            lc.add(TBL["contract_ledger"], payload)

        if ifrs_rows:
            payload = [{
                "关联卡片ID": {"text": card_id},
                "版本ID": {"text": val(card, "版本号", "1")},
                "月份": {"date": r["月份"]},
                "行类型": {"select": r["行类型"]},
                "资产折旧": {"currency": float(r["资产折旧"])},
                "负债利息": {"currency": float(r["负债利息"])},
                "租赁付款额": {"currency": float(r["租赁付款额"])},
                "折现倍数": {"number": float(r["折现倍数"])},
                "现值": {"currency": float(r["现值"])},
                "租赁负债余额": {"currency": float(r["租赁负债余额"])},
                "使用权资产余额": {"currency": float(r["使用权资产余额"])},
            } for r in ifrs_rows]
            lc.add(TBL["ifrs_detail"], payload)

        return {"ok": True, "card_id": card_id,
                "ledger_months": len(ledger_rows),
                "ifrs_months": len(ifrs_rows or [])}


# ---- 卡片 CRUD ---------------------------------------------------------
def _readback_ok(db, record_id):
    """写入后回读确认：拿不到 record_id、或回读不到记录，都不算写入成功。"""
    if not record_id:
        return False
    try:
        return bool(lc.get_record(db, record_id))
    except Exception:
        return False


def create_card(cur, b):
    code = str(b.get("合同编码") or "").strip()
    fee = str(b.get("费用类型") or "").strip()
    # OCR 状态：前端带着识别结果建卡时为"已识别待核对"；未带则为"未识别"
    ocr_st = str(b.get("OCR状态") or "").strip()
    if ocr_st not in ("未识别", "已识别待核对", "人工已确认", "核对失败"):
        ocr_st = "未识别"
    if not code:
        return {"ok": False, "error": "合同编码不能为空"}
    with _glob:
        exists = lc.query(TBL["cards"], filt={
            "property": {"property": "合同编码", "text": {"equals": code}}})
        if any(val(row, '费用类型') == fee for row in exists):
            return {"ok": False, "error": f"合同 {code} 的{fee}卡片已存在"}
        rec = {
            "合同编码": {"text": code},
            "卡片名称": {"text": str(b.get("卡片名称") or "").strip()},
            "费用类型": {"select": fee} if fee else None,
            "承租方": {"text": str(b.get("承租方") or "").strip()},
            "实际付款方": {"text": str(b.get("实际付款方") or "").strip()},
            "实际收款方": {"text": str(b.get("实际收款方") or "").strip()},
            "甲方": {"text": str(b.get("甲方") or "").strip()},
            "租赁地址": {"text": str(b.get("租赁地址") or "").strip()},
            "省级": {"text": str(b.get("省级") or "").strip()},
            "市级": {"text": str(b.get("市级") or "").strip()},
            "区镇": {"text": str(b.get("区镇") or "").strip()},
            "片区": {"text": str(b.get("片区") or "").strip()},
            "业务模块": {"text": str(b.get("业务模块") or "").strip()},
            "建筑面积": {"number": float(b.get("建筑面积") or 0)},
            "实用面积": {"number": float(b.get("实用面积") or 0)},
            "租赁起始日": {"date": norm_d(b.get("租赁起始日"))},
            "租赁终止日": {"date": norm_d(b.get("租赁终止日"))},
            "计费算法": {"select": b.get("计费算法") or "整月"},
            "约定付款日期": {"number": float(b.get("约定付款日期") or 0)},
            "结算方式": {"select": b.get("结算方式") or "付当月"},
            "提单部门": {"select": b.get("提单部门") or "财务"},
            "是否含税": {"select": b.get("是否含税") or "含税"},
            "税率": {"text": str(b.get("税率") or "").strip()},
            "是否IFRS": {"select": b.get("是否IFRS") or "否"},
            "IFRS折现率": {"number": float(b.get("IFRS折现率") or 0)},
            "预付租金": {"currency": float(b.get("预付租金") or 0)},
            "合同状态": {"select": b.get("合同状态") or "拟稿"},
            "审批状态": {"select": "草稿"},
            "版本号": {"text": "1"},
            "备注": {"text": str(b.get("备注") or "").strip()},
            "提交人": {"text": cur["name"]},
            # OCR 来源标识：有识别结果即标记为"已识别待核对"，等人工确认后才转为"人工已确认"
            "OCR状态": {"select": ocr_st},
        }
        # 去掉 None 值
        rec = {k: v for k, v in rec.items() if v is not None}
        add_date = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
        rec["提交时间"] = {"date": add_date}
        res = lc.add(TBL["cards"], [rec])
        # 检查插入是否成功（逐条失败不抛错）
        if res:
            r0 = res[0] if isinstance(res, list) else res
            if isinstance(r0, dict) and not r0.get("success", True):
                return {"ok": False, "error": "保存失败：" + str(r0.get("error", ""))[:200]}
        # 从插入结果取 record_id
        card_id = ""
        if res:
            r0 = res[0] if isinstance(res, list) else res
            if isinstance(r0, dict):
                card_id = str(r0.get("id") or r0.get("record_id") or r0.get("_id") or "")
        if not card_id:
            rows = lc.query(TBL["cards"], filt={
                "property": {"property": "合同编码", "text": {"equals": code}}})
            rows = [r for r in rows if val(r, '费用类型') == fee]
            card_id = rows[0]["record_id"] if rows else ""
        # 写入回读确认：没有 record_id 或回读不到，就不能对外宣称"已写入团队空间"
        confirmed = _readback_ok(TBL["cards"], card_id)
        if card_id and confirmed:
            _ensure_task(cur, card_id, code)
            _ensure_site(b)
        return {"ok": True, "code": code, "card_id": card_id,
                "write_confirmed": confirmed,
                "write_status": "已写入团队空间" if confirmed else "写入未确认",
                "warning": "" if confirmed else "接口未返回 record_id 或回读失败，请刷新确认后再继续"}


def _ensure_task(cur, card_id, code):
    """建卡时写入任务池（任务来源=手动新增；OA 同步依赖 R02 接口，暂由手动登记兜底）。"""
    exists = lc.query(TBL["task_pool"], filt={
        "property": {"property": "合同编码", "text": {"equals": code}}})
    if exists:
        return
    lc.add(TBL["task_pool"], [{
        "任务编号": {"text": code},
        "合同编码": {"text": code},
        "任务来源": {"select": "手动新增"},
        "负责人": {"text": cur["name"]},
        "状态": {"select": "待处理"},
    }])


def _ensure_site(b):
    """建卡时把合同里的场地信息登记进 MD_场地（按物理地址去重，避免重复）。"""
    addr = str(b.get("租赁地址") or "").strip()
    if not addr:
        return
    exists = lc.query(TBL["md_sites"], filt={
        "property": {"property": "物理地址", "text": {"equals": addr}}})
    if exists:
        return
    rec = {
        "物理地址": {"text": addr},
        "省级": {"text": str(b.get("省级") or "").strip()},
        "市级": {"text": str(b.get("市级") or "").strip()},
        "区镇": {"text": str(b.get("区镇") or "").strip()},
        "片区": {"text": str(b.get("片区") or "").strip()},
        "业务模块": {"text": str(b.get("业务模块") or "").strip()},
        "建筑面积": {"number": float(b.get("建筑面积") or 0)},
        "租赁状态": {"select": "在用"},
    }
    bary = float(b.get("建筑面积") or 0)
    if bary:
        rec["建筑面积"] = {"number": bary}
    lc.add(TBL["md_sites"], [rec])


def save_amount_overview(cur, card_id, segments):
    """保存金额概览（计费依据）。先完整校验，再替换旧草稿，逐条检查写入结果。"""
    with _glob:
        card = lc.get_record(TBL["cards"], card_id)
        if not card:
            return {"ok": False, "error": "卡片不存在"}
        if val(card, "审批状态", "草稿") not in ("草稿", "退回"):
            return {"ok": False, "error": "仅草稿/退回状态可编辑金额概览"}
        if not isinstance(segments, list) or not segments:
            return {"ok": False, "error": "至少需要一条金额分段"}
        payload = []
        for i, s in enumerate(segments, 1):
            start = norm_d(s.get("租赁起始日") or s.get("s"))
            end = norm_d(s.get("租赁终止日") or s.get("e"))
            if not start or not end:
                return {"ok": False, "error": f"第{i}条分段缺少起止日期"}
            try:
                start_d = date.fromisoformat(start)
                end_d = date.fromisoformat(end)
            except ValueError:
                return {"ok": False, "error": f"第{i}条分段日期格式错误"}
            if end_d < start_d:
                return {"ok": False, "error": f"第{i}条分段终止日早于起始日"}
            months = dnum(s.get("月份数") or s.get("months"))
            amount = dnum(s.get("月金额") or s.get("amt"))
            subtotal = dnum(s.get("金额小计") or s.get("total"))
            if months <= 0:
                return {"ok": False, "error": f"第{i}条分段月份数必须大于0"}
            if amount < 0:
                return {"ok": False, "error": f"第{i}条分段月金额不能为负"}
            if subtotal == 0:
                subtotal = (months * amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            payload.append({
                "关联卡片ID": {"text": card_id},
                "租赁起始日": {"date": start},
                "租赁终止日": {"date": end},
                "月份数": {"number": float(months)},
                "月金额": {"currency": float(amount)},
                "金额小计": {"currency": float(subtotal)},
            })
        # 先写新数据，确认全部成功后再删除旧数据，避免保存失败造成清空
        old = _overview_by_card(card_id)
        result = lc.add(TBL["amount_overview"], payload)
        failed = [r for r in (result or []) if isinstance(r, dict) and not r.get("success", True)]
        if failed or len([r for r in (result or []) if isinstance(r, dict) and r.get("success", False)]) != len(payload):
            return {"ok": False, "error": "金额概览写入失败：" + str(failed[:2])[:300]}
        ids = [str(r.get("id") or r.get("record_id") or "") for r in result]
        if not all(ids) or len(set(ids)) != len(payload):
            return {"ok": False, "write_confirmed": False, "error": "金额概览缺少唯一记录ID，保留旧数据，请核查后重试"}
        for rid, expected in zip(ids, payload):
            actual = lc.get_record(TBL["amount_overview"], rid)
            if not actual:
                return {"ok": False, "write_confirmed": False, "error": "金额概览回读失败，保留旧数据，请核查后重试"}
            for key, encoded in expected.items():
                want = next(iter(encoded.values()))
                got = val(actual, key, None)
                matches = (dnum(got) == dnum(want)) if key in ("月份数", "月金额", "金额小计") else str(got or "")[:10] == str(want)[:10] if key in ("租赁起始日", "租赁终止日") else got == want
                if got is None or not matches:
                    return {"ok": False, "write_confirmed": False, "error": "金额概览回读内容不一致：" + key}
        for o in old:
            if o.get("record_id") not in ids:
                lc.delete(TBL["amount_overview"], [o["record_id"]])
        return {"ok": True, "segments": len(payload), "write_confirmed": True}


# ---- 删除卡片 ----------------------------------------------------------
def _purge_card_children(card_id):
    """删除卡片所有关联子表记录（分批，delete 单次≤100）。"""
    def _del(db, fid):
        rows = lc.query(db, filt={"property": {"property": fid, "text": {"equals": card_id}}})
        ids = [r["record_id"] for r in rows]
        for i in range(0, len(ids), 100):
            lc.delete(db, ids[i:i+100])
    _del(TBL["amount_overview"], "关联卡片ID")
    _del(TBL["contract_ledger"], "关联卡片ID")
    _del(TBL["ifrs_detail"], "关联卡片ID")
    _del(TBL["cost_share"], "关联卡片ID")
    _del(TBL["pay_plan"], "关联卡片ID")
    _del(TBL["vouchers"], "关联卡片ID")
    _del(TBL["approval_tickets"], "关联对象ID")
    _del(TBL["version_snapshots"], "关联卡片ID")
    _del(TBL["oneoff_fees"], "关联卡片ID")


def delete_card(cur, card_id):
    """删除草稿/退回状态的卡片及其关联数据。已提交/已审批不可删。"""
    with _glob:
        card = lc.get_record(TBL["cards"], card_id)
        if not card:
            return {"ok": False, "error": "卡片不存在"}
        st = val(card, "审批状态", "草稿")
        if st not in ("草稿", "退回"):
            return {"ok": False, "error": f"状态[{st}]不可删除，仅草稿/退回可删"}
        if cur.get("role") != ROLE_ADMIN and val(card, "提交人") != cur.get("name"):
            return {"ok": False, "error": "只能删除自己创建的草稿或退回卡片"}
        _purge_card_children(card_id)
        lc.delete(TBL["cards"], [card_id])
        return {"ok": True, "card_id": card_id}


# ---- 复核编辑与联动重算 ------------------------------------------------
_CARD_EDIT_TEXT = ["卡片名称", "承租方", "实际付款方", "实际收款方", "甲方", "租赁地址",
                   "省级", "市级", "区镇", "片区", "业务模块", "备注"]
_CARD_EDIT_DATE = ["租赁起始日", "租赁终止日"]
_CARD_EDIT_NUM = ["建筑面积", "实用面积", "约定付款日期", "IFRS折现率"]
_CARD_EDIT_MONEY = ["预付租金"]
_CARD_EDIT_SELECT = ["费用类型", "计费算法", "结算方式", "提单部门", "是否含税", "是否IFRS", "合同状态"]
# 这些字段影响台账计算：一旦改动，已生成的台账/凭证必须作废重算，不得沿用旧结果
_CALC_FIELDS = ["租赁起始日", "租赁终止日", "是否IFRS", "IFRS折现率", "预付租金", "结算方式", "费用类型"]


def save_card_basic(cur, card_id, b):
    """复核阶段修改卡片基础信息；影响计算的字段变更时作废下游台账（不覆盖已审批数据）。"""
    with _glob:
        card = lc.get_record(TBL["cards"], card_id)
        if not card:
            return {"ok": False, "error": "卡片不存在"}
        st = val(card, "审批状态", "草稿")
        if st not in ("草稿", "退回"):
            return {"ok": False, "error": "状态[%s]不可修改，仅草稿/退回可编辑" % st}
        props, changed, calc_changed = {}, [], []
        for k in _CARD_EDIT_TEXT:
            if k in b:
                props[k] = {"text": str(b.get(k) or "").strip()}
        for k in _CARD_EDIT_DATE:
            if k in b:
                props[k] = {"date": norm_d(b.get(k))}
        for k in _CARD_EDIT_NUM:
            if k in b:
                props[k] = {"number": float(dnum(b.get(k)))}
        for k in _CARD_EDIT_MONEY:
            if k in b:
                props[k] = {"currency": float(dnum(b.get(k)))}
        for k in _CARD_EDIT_SELECT:
            if k in b and str(b.get(k) or "").strip():
                props[k] = {"select": str(b.get(k)).strip()}
        if not props:
            return {"ok": False, "error": "没有可更新的字段"}
        # 起止日先后校验（用改后的值判断）
        s = norm_d(props.get("租赁起始日", {}).get("date") or val(card, "租赁起始日"))
        e = norm_d(props.get("租赁终止日", {}).get("date") or val(card, "租赁终止日"))
        if s and e and e < s:
            return {"ok": False, "error": "租赁终止日不能早于租赁起始日"}
        # 找出真正发生变化的字段
        for k, v in props.items():
            now = val(card, k)
            new = v.get("text", v.get("date", v.get("number", v.get("currency", v.get("select")))))
            if k in _CARD_EDIT_DATE:
                now, new = norm_d(now), norm_d(new)
            if k in _CARD_EDIT_NUM or k in _CARD_EDIT_MONEY:
                now, new = str(dnum(now)), str(dnum(new))
            if str(now if now is not None else "") != str(new if new is not None else ""):
                changed.append(k)
                if k in _CALC_FIELDS:
                    calc_changed.append(k)
        if not changed:
            return {"ok": True, "changed": [], "invalidated": False}
        lc.update_checked(TBL["cards"], [{"record_id": card_id, "properties": props}])
        invalidated = False
        purged = {}
        if calc_changed:
            # 上游变了 → 下游结果失效，删除重算，绝不留旧数据冒充新结果
            for key in ("contract_ledger", "ifrs_detail", "pay_plan", "vouchers"):
                n = _purge_by_card(TBL[key], card_id)
                if n:
                    purged[TBL[key]] = n
            invalidated = bool(purged)
        return {"ok": True, "changed": changed, "calc_changed": calc_changed,
                "invalidated": invalidated, "purged": purged}


def _purge_by_card(db, card_id):
    rows = lc.query(db, filt={"property": {"property": "关联卡片ID", "text": {"equals": card_id}}})
    ids = [r["record_id"] for r in rows]
    for i in range(0, len(ids), 100):
        lc.delete(db, ids[i:i + 100])
    return len(ids)


def save_ledger_adjust(cur, card_id, rows):
    """合同法台账人工复核手调：按月份更新降租金额/未付款/已付款，并联动重算勾稽与 IFRS。"""
    with _glob:
        card = lc.get_record(TBL["cards"], card_id)
        if not card:
            return {"ok": False, "error": "卡片不存在"}
        st = val(card, "审批状态", "草稿")
        if st not in ("草稿", "退回"):
            return {"ok": False, "error": "状态[%s]不可调整台账，仅草稿/退回可手调" % st}
        ledger = sorted(_ledger_by_card(card_id), key=lambda r: (val(r, "月份") or ""))
        if not ledger:
            return {"ok": False, "error": "台账尚未生成，请先生成台账"}
        edits = {}
        for r in (rows or []):
            ym = str(r.get("月份") or "")[:7]
            if not ym:
                continue
            edits[ym] = r
        if not edits:
            return {"ok": False, "error": "没有需要调整的行"}
        unchecked = []
        for r in ledger:
            ym = (val(r, "月份") or "")[:7]
            e = edits.get(ym)
            if e is None:
                unchecked.append({
                    "月份": ym, "合同金额": dnum(r.get("合同金额")),
                    "降租金额": dnum(r.get("降租金额")),
                    "未付款": dnum(r.get("未付款")), "已付款": dnum(r.get("已付款")),
                    "_rid": r["record_id"],
                })
                continue
            for k in ("降租金额", "未付款", "已付款"):
                if k in e and str(e.get(k, "")) not in ("", "None"):
                    # 降租金额是减免额，允许为负（需求：实付金额合计 = 合同金额 + 降租金额）；
                    # 未付款/已付款是实际支付口径，不能为负。
                    if k in ("未付款", "已付款") and dnum(e.get(k)) < 0:
                        return {"ok": False, "error": "%s 的%s不能为负" % (ym, k)}
            unchecked.append({
                "月份": ym, "合同金额": dnum(r.get("合同金额")),
                "降租金额": dnum(e.get("降租金额", r.get("降租金额"))),
                "未付款": dnum(e.get("未付款", r.get("未付款"))),
                "已付款": dnum(e.get("已付款", r.get("已付款"))),
                "_rid": r["record_id"],
            })
        calc = _calc_ledger_balances(unchecked)
        tail = calc[-1]["预付科目余额"]
        if abs(tail) > Decimal("0.01"):
            return {"ok": False,
                    "error": "勾稽不通过：末月预付科目余额为 %s（应为 0）。请检查各月未付款/已付款/降租金额。" % tail}
        # 勾稽通过：写回台账
        for r in calc:
            lc.update_checked(TBL["contract_ledger"], [{"record_id": r["_rid"], "properties": {
                "降租金额": {"currency": float(r["降租金额"])},
                "未付款": {"currency": float(r["未付款"])},
                "已付款": {"currency": float(r["已付款"])},
                "实付金额合计": {"currency": float(r["实付金额合计"])},
                "预付科目余额": {"currency": float(r["预付科目余额"])},
                "勾稽通过": {"checkbox": True},
            }}])
        # 联动重算 IFRS（合同金额变化直接影响租赁付款额与现值）
        ifrs_n = 0
        if val(card, "是否IFRS", "") == "是":
            _purge_by_card(TBL["ifrs_detail"], card_id)
            try:
                ifrs_rows = _calc_ifrs(card, [(r["月份"], r["实付金额合计"]) for r in calc])
            except ValueError as ex:
                return {"ok": False, "error": "台账已保存，但 IFRS 重算失败：%s" % ex}
            if ifrs_rows:
                payload = [{
                    "关联卡片ID": {"text": card_id},
                    "版本ID": {"text": val(card, "版本号", "1")},
                    "月份": {"date": r["月份"]},
                    "行类型": {"select": r["行类型"]},
                    "资产折旧": {"currency": float(r["资产折旧"])},
                    "负债利息": {"currency": float(r["负债利息"])},
                    "租赁付款额": {"currency": float(r["租赁付款额"])},
                    "折现倍数": {"number": float(r["折现倍数"])},
                    "现值": {"currency": float(r["现值"])},
                    "租赁负债余额": {"currency": float(r["租赁负债余额"])},
                    "使用权资产余额": {"currency": float(r["使用权资产余额"])},
                } for r in ifrs_rows]
                for i in range(0, len(payload), 100):
                    lc.add(TBL["ifrs_detail"], payload[i:i + 100])
                ifrs_n = len(payload)
        return {"ok": True, "months": len(calc), "ifrs_months": ifrs_n,
                "tail_balance": float(tail)}


# ---- 审批 --------------------------------------------------------------
def _find_ticket_active(card_id):
    res = lc.query(TBL["approval_tickets"], filt={
        "and": [
            {"property": {"property": "关联对象ID", "text": {"equals": card_id}}},
            {"property": {"property": "状态", "select": {"equals": "待审"}}},
        ]})
    return res[0] if res else None


def _set_card_state(card_id, state):
    """写入卡片审批状态并事后确认真的生效了（避免"显示成功但状态没变"）。"""
    lc.update_checked(TBL["cards"], [{"record_id": card_id,
                                     "properties": {"审批状态": {"select": state}}}])
    chk = lc.get_record(TBL["cards"], card_id)
    if val(chk, "审批状态", "") != state:
        raise RuntimeError("卡片状态写入后仍为[%s]，未能变为[%s]" % (val(chk, "审批状态", ""), state))
    return True


def submit_card(cur, card_id):
    with _glob:
        card = lc.get_record(TBL["cards"], card_id)
        if not card:
            return {"ok": False, "error": "卡片不存在"}
        if val(card, "审批状态", "草稿") not in ("草稿", "退回"):
            return {"ok": False, "error": f"状态[{val(card,'审批状态')}]不可提交"}
        if _ocr_pending(card):
            return {"ok": False, "error": "识别结果尚未人工确认，请核对后再提交"}
        validation = validate_card(card_id, card=card)
        if not validation.get("ok"):
            return {"ok": False, "error": "；".join(
                e["message"] for e in validation.get("errors", [])) or "提交校验失败",
                "validation": validation}
        if not _ledger_by_card(card_id):
            return {"ok": False, "error": "请先生成台账再提交"}
        code = val(card, "合同编码")
        # 写审批工单
        lc.add(TBL["approval_tickets"], [{
            "工单类型": {"select": "台账登记"},
            "关联对象ID": {"text": card_id},
            "合同编码": {"text": code},
            "提交人": {"text": cur["name"]},
            "状态": {"select": "待审"},
            "提交时间": {"date": _now().strftime("%Y-%m-%dT%H:%M:%SZ")},
        }])
        _set_card_state(card_id, "待审")
        return {"ok": True, "card_id": card_id, "code": code, "state": "待审"}


def approve_card(cur, card_id, opinion):
    return _decide(cur, card_id, "通过", opinion)


def reject_card(cur, card_id, opinion):
    if not opinion:
        return {"ok": False, "error": "退回必须填写意见"}
    return _decide(cur, card_id, "退回", opinion)


def withdraw_card(cur, card_id, opinion="提交人撤回"):
    """提交人仅可在待审阶段撤回；保留原审批工单并把卡片退回草稿。"""
    with _glob:
        card = lc.get_record(TBL["cards"], card_id)
        if not card:
            return {"ok": False, "error": "卡片不存在"}
        if val(card, "审批状态", "") != "待审":
            return {"ok": False, "error": f"状态[{val(card,'审批状态')}]不可撤回"}
        if val(card, "提交人", "") != cur.get("name"):
            return {"ok": False, "error": "只有提交人可以撤回"}
        t = _find_ticket_active(card_id)
        if not t:
            return {"ok": False, "error": "审批工单不存在"}
        lc.update_checked(TBL["approval_tickets"], [{
            "record_id": t["record_id"],
            "properties": {
                "状态": {"select": "撤回"},
                "审批人": {"text": cur.get("name", "")},
                "意见": {"text": opinion or "提交人撤回"},
                "审批时间": {"date": _now().strftime("%Y-%m-%dT%H:%M:%SZ")},
            },
        }])
        _set_card_state(card_id, "草稿")
        return {"ok": True, "card_id": card_id, "state": "草稿"}


def _decide(cur, card_id, action, opinion):
    with _glob:
        card = lc.get_record(TBL["cards"], card_id)
        if not card:
            return {"ok": False, "error": "卡片不存在"}
        if val(card, "审批状态", "") != "待审":
            return {"ok": False, "error": f"状态[{val(card,'审批状态')}]不可审批"}
        if val(card, "提交人") and val(card, "提交人") == cur.get("name"):
            return {"ok": False, "error": "提交人与审批人必须分离，不能审批自己提交的卡片"}
        t = _find_ticket_active(card_id)
        if not t:
            return {"ok": False, "error": "审批工单不存在"}
        target = "通过" if action == "通过" else "退回"
        # 工单与卡片状态都要写成功才算审批完成；任一失败都明确报错，不返回假成功
        lc.update_checked(TBL["approval_tickets"], [{
            "record_id": t["record_id"],
            "properties": {
                "状态": {"select": target},
                "审批人": {"text": cur["name"]},
                "意见": {"text": opinion or action},
                "审批时间": {"date": _now().strftime("%Y-%m-%dT%H:%M:%SZ")},
            },
        }])
        _set_card_state(card_id, target)
        auto = {}
        if target == "通过":
            # 审批通过 → 自动生成付款清单与凭证（联动团队空间对应表），并推进任务池状态
            try:
                pr = generate_payments(cur, card_id)
                auto["payments"] = pr.get("payments", 0)
                if not pr.get("ok"):
                    auto["payments_error"] = pr.get("error", "")
            except Exception as e:  # 自动生成失败不阻断审批
                auto["payments_error"] = str(e)
            try:
                vr = generate_vouchers(cur, card_id)
                auto["vouchers"] = vr.get("vouchers", 0)
                if not vr.get("ok"):
                    auto["vouchers_error"] = vr.get("error", "")
            except Exception as e:
                auto["vouchers_error"] = str(e)
            _task_transition(card_id, "处理中")
        else:
            _task_transition(card_id, "待处理")
        return {"ok": True, "card_id": card_id, "state": target,
                "ticket_id": t["record_id"], "auto": auto}


def _task_transition(card_id, state):
    """按关联卡片 ID 推进任务池状态（合同编码绑定）。"""
    card = lc.get_record(TBL["cards"], card_id)
    if not card:
        return
    code = val(card, "合同编码")
    for t in lc.query(TBL["task_pool"], filt={
            "property": {"property": "合同编码", "text": {"equals": code}}}):
        lc.update_checked(TBL["task_pool"], [{
            "record_id": t["record_id"],
            "properties": {"状态": {"select": state}},
        }])


# =======================================================================
# 阶段5：主数据 / 成本分摊 / 付款清单+审批 / 凭证 / 报表 / 导入导出
# =======================================================================

# ---- 结算方式 → 付款月份偏移 -------------------------------------------
# 结算方式规则（依据需求「3、租赁卡片W列未付款金额在不同结算方式下的设置案例」）
# cycle：付款周期（月）；prepay：预付方式的首个付款节点比租赁首月提前 1 个月
SETTLE_RULE = {
    "付当月": {"cycle": 1, "prepay": False},
    "预付下月": {"cycle": 1, "prepay": True},
    "付两月": {"cycle": 2, "prepay": False},
    "预付两月": {"cycle": 2, "prepay": True},
    "付当季": {"cycle": 3, "prepay": False},
    "预付下季": {"cycle": 3, "prepay": True},
    "付半年": {"cycle": 6, "prepay": False},
    "预付半年": {"cycle": 6, "prepay": True},
    "付当年": {"cycle": 12, "prepay": False},
    "预付下年": {"cycle": 12, "prepay": True},
}


def group_payment_nodes(month_amounts, cycle, lead):
    """把逐月金额按付款周期分组为付款节点。

    month_amounts: [("YYYY-MM", Decimal)]，按月份升序
    cycle: 付款周期（月）；lead: 首个付款节点相对首个费用月份的月份偏移（预付为 -1）
    返回 [{"付款月": "YYYY-MM", "费用月份": [...], "金额": Decimal}]
    """
    out = []
    for i in range(0, len(month_amounts), cycle):
        chunk = month_amounts[i:i + cycle]
        months = [m for m, _ in chunk]
        amount = sum((dnum(a) for _, a in chunk), Decimal("0"))
        if amount == 0:
            continue   # 整段为 0（如全免租月）不生成无用付款节点
        out.append({"付款月": _month_add(months[0], lead), "费用月份": months, "金额": amount})
    return out


def _period_label(months):
    """费用所属期间：单月「2026年04月」，同年跨月「2026年4至5月」（对齐需求付款清单报表）。"""
    if not months:
        return ""
    a, b = months[0], months[-1]
    ya, ma = int(a[:4]), int(a[5:7])
    if a == b:
        return "%d年%02d月" % (ya, ma)
    if a[:4] == b[:4]:
        return "%d年%d至%d月" % (ya, ma, int(b[5:7]))
    return "%d年%d月至%d年%d月" % (ya, ma, int(b[:4]), int(b[5:7]))


def _month_add(ym: str, offset: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    idx = (y * 12 + (m - 1)) + offset
    yy, mm = divmod(idx, 12)
    return "%04d-%02d" % (yy, mm + 1)


def _month_str(d: str) -> str:
    return (d or "")[:7]


def _pay_date(month_ym: str, offset: int, day: int) -> str:
    ym = _month_add(month_ym, offset)
    dd = max(1, min(28, int(day) if day else 10))
    return "%s-%02d" % (ym, dd)


# ---- 主数据 ------------------------------------------------------------
_MASTER_TYPES = {
    "suppliers": "md_suppliers", "depts": "md_depts", "rules": "md_account_rules",
    "contacts": "md_contacts", "entities": "md_entities", "sites": "md_sites",
    "params": "md_params",
}


def add_master(cur, mtype, b):
    db = _MASTER_TYPES.get(mtype)
    if not db:
        return {"ok": False, "error": "未知主数据类型"}
    rec = {}
    for k, v in b.items():
        if isinstance(v, bool):
            rec[k] = {"checkbox": v}
        elif isinstance(v, (int, float)):
            rec[k] = {"number": float(v)}
        elif isinstance(v, str) and v.strip():
            rec[k] = {"text": v.strip()}
    if not rec:
        return {"ok": False, "error": "无有效字段"}
    lc.add(db, [rec])
    return {"ok": True}


def import_master(cur, mtype, rows):
    db = _MASTER_TYPES.get(mtype)
    if not db:
        return {"ok": False, "error": "未知主数据类型"}
    if not isinstance(rows, list) or not rows:
        return {"ok": False, "error": "无导入数据"}
    recs = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        rec = {}
        for k, v in row.items():
            if isinstance(v, bool):
                rec[k] = {"checkbox": v}
            elif isinstance(v, (int, float)):
                rec[k] = {"number": float(v)}
            elif v not in (None, ""):
                rec[k] = {"text": str(v)}
        if rec:
            recs.append(rec)
    if recs:
        for i in range(0, len(recs), 100):
            lc.add(db, recs[i:i+100])
    return {"ok": True, "imported": len(recs)}


# ---- 成本分摊 ----------------------------------------------------------
def get_shares(card_id):
    return lc.query(TBL["cost_share"], filt={
        "property": {"property": "关联卡片ID", "text": {"equals": card_id}}})


def save_shares(cur, card_id, shares):
    if not isinstance(shares, list):
        return {"ok": False, "error": "shares 必须为数组"}
    # 校验比例合计
    total = sum(dnum(s.get("分摊比例")) for s in shares)
    if abs(total - Decimal("1")) > Decimal("0.0001") if shares else False:
        return {"ok": False, "error": "分摊比例合计须为 100%（最后一个部门可用倒挤）"}
    with _glob:
        for s in get_shares(card_id):
            lc.delete(TBL["cost_share"], [s["record_id"]])
        payload = [{
            "关联卡片ID": {"text": card_id},
            "成本中心": {"text": str(s.get("成本中心") or "")},
            "分摊比例": {"number": float(dnum(s.get("分摊比例")))},
            "生效日": {"date": norm_d(s.get("生效日"))},
            "倒挤": {"checkbox": bool(s.get("倒挤"))},
            "变更依据": {"text": str(s.get("变更依据") or "")},
        } for s in shares]
        if payload:
            lc.add(TBL["cost_share"], payload)
        return {"ok": True, "shares": len(payload)}


# ---- 一次性费用 --------------------------------------------------------
def get_oneoff(card_id):
    return lc.query(TBL["oneoff_fees"], filt={
        "property": {"property": "关联卡片ID", "text": {"equals": card_id}}})


def save_oneoff(cur, card_id, fees):
    """写入「一次性费用」（押金/保证金/预付租金等）。费用类型为空时只写金额与说明。"""
    if not isinstance(fees, list):
        return {"ok": False, "error": "fees 必须为数组"}
    allowed = {"租赁保证金", "物管保证金", "水电保证金", "其他保证金", "预付租金"}
    with _glob:
        for f in get_oneoff(card_id):
            lc.delete(TBL["oneoff_fees"], [f["record_id"]])
        payload = []
        for f in fees:
            amt = dnum(f.get("金额"))
            if amt is None:
                continue
            rec = {"关联卡片ID": {"text": card_id},
                   "金额": {"currency": float(amt)},
                   "说明": {"text": str(f.get("说明") or "")}}
            ty = str(f.get("费用类型") or "").strip()
            if ty in allowed:
                rec["费用类型"] = {"select": ty}
            payload.append(rec)
        if payload:
            lc.add(TBL["oneoff_fees"], payload)
        return {"ok": True, "fees": len(payload)}


def _share_effective(card_id, month_ym):
    """返回该卡片在给定月份的生效分摊（比例+成本中心），含倒挤归一。"""
    shares = get_shares(card_id)
    if not shares:
        return []
    # 按生效日分组，取 <= month 的最新生效区间
    eff = {}
    for s in shares:
        effd = norm_d(val(s, "生效日"))
        key = effd or "0000-00"
        eff.setdefault(key, []).append(s)
    # 选择生效日 <= month 的最近一组
    chosen = []
    best = ""
    for effd, lst in eff.items():
        if effd <= (month_ym + "-01"):
            if effd > best:
                best = effd
                chosen = lst
    if not chosen:
        return []
    total = sum(dnum(val(s, "分摊比例")) for s in chosen)
    out = []
    for i, s in enumerate(chosen):
        is_last = i == len(chosen) - 1
        val_s = val(s, "倒挤", False)
        if is_last and dnum(val(s, "分摊比例")) > 0 and abs(total - Decimal("1")) > Decimal("0.0001"):
            ratio = (Decimal("1") - (total - dnum(val(s, "分摊比例")))).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        else:
            ratio = (dnum(val(s, "分摊比例")) / total).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) if total else Decimal("0")
        out.append({"成本中心": val(s, "成本中心"), "比例": ratio})
    return out


# ---- 付款清单 ----------------------------------------------------------
def generate_payments(cur, card_id):
    with _glob:
        card = lc.get_record(TBL["cards"], card_id)
        if not card:
            return {"ok": False, "error": "卡片不存在"}
        if val(card, "审批状态", "") != "通过":
            return {"ok": False, "error": "仅审批通过的合同可生成付款清单"}
        ledger = _ledger_by_card(card_id)
        if not ledger:
            return {"ok": False, "error": "合同金额明细为空"}
        settle = val(card, "结算方式", "付当月")
        rule = SETTLE_RULE.get(settle)
        if not rule:
            return {"ok": False, "error": "未知结算方式[%s]，请先修正卡片结算方式" % settle}
        cycle = rule["cycle"]
        lead = -1 if rule["prepay"] else 0   # 预付：首个付款节点比租赁首月提前 1 个月
        payday = int(dnum(val(card, "约定付款日期") or 0))
        code = val(card, "合同编码")
        area = val(card, "片区")
        module = val(card, "业务模块")
        dept = val(card, "提单部门")
        # Existing payment facts/approvals are immutable under regeneration.
        existing = lc.query(TBL["pay_plan"], filt={
                "property": {"property": "关联卡片ID", "text": {"equals": card_id}}})
        if existing:
            return {'ok': True, 'payments': len(existing), 'reused': True, 'code': code}
        # 按付款周期把台账月份分组：一个节点只出一笔（非付款节点不生成行）
        rows = sorted(ledger, key=lambda r: (val(r, "月份") or ""))
        month_amounts = [((val(r, "月份") or "")[:7], dnum(r.get("实付金额合计"))) for r in rows]
        nodes = group_payment_nodes(month_amounts, cycle, lead)
        by_month = {(val(r, "月份") or "")[:7]: r for r in rows}
        payload = []
        for n in nodes:
            first = by_month.get(n["费用月份"][0], {})
            payload.append({
                "关联卡片ID": {"text": card_id},
                "台账明细ID": {"text": first.get("record_id", "")},
                "月份": {"date": n["付款月"] + "-01"},
                "计划付款金额": {"currency": float(n["金额"])},
                "费用所属期间": {"text": _period_label(n["费用月份"])},
                "约定付款日期": {"date": _pay_date(n["付款月"], 0, payday)},
                "结算方式": {"text": settle},
                "物理片区": {"text": area},
                "业务模块": {"text": module},
                "提单部门": {"text": dept},
                "已付": {"checkbox": False},
            })
        if payload:
            for i in range(0, len(payload), 100):
                lc.add(TBL["pay_plan"], payload[i:i+100])
        return {"ok": True, "payments": len(payload), "code": code,
                "cycle": cycle, "settle": settle}


def list_payments(filt=None, sorts=None):
    return lc.query(TBL["pay_plan"], filt=filt)


def submit_payments(cur, record_ids):
    """勾选付款明细，生成付款审批工单，明细进入待审。"""
    if not record_ids:
        return {"ok": False, "error": "请选择付款明细"}
    with _glob:
        rows = []
        for rid in record_ids:
            rec = lc.get_record(TBL["pay_plan"], rid)
            if rec:
                rows.append(rec)
        if not rows:
            return {"ok": False, "error": "未找到付款明细"}
        batch = _now().strftime("ZF%Y%m%d%H%M%S")
        card_id = val(rows[0], "关联卡片ID")
        code = ""
        card = lc.get_record(TBL["cards"], card_id)
        if card:
            code = val(card, "合同编码")
        total = sum(dnum(r.get("计划付款金额")) for r in rows)
        lc.add(TBL["approval_tickets"], [{
            "工单类型": {"select": "付款审批"},
            "关联对象ID": {"text": card_id},
            "合同编码": {"text": code},
            "提交人": {"text": cur["name"]},
            "状态": {"select": "待审"},
            "提交时间": {"date": _now().strftime("%Y-%m-%dT%H:%M:%SZ")},
        }])
        # 明细标记批次
        for r in rows:
            lc.update_checked(TBL["pay_plan"], [{"record_id": r["record_id"], "properties": {
                "付款批次": {"text": batch}}}])
        return {"ok": True, "batch": batch, "count": len(rows),
                "amount": float(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))}


def _find_pay_ticket(batch_id=None, card_id=None):
    ands = [{"property": {"property": "工单类型", "select": {"equals": "付款审批"}}},
            {"property": {"property": "状态", "select": {"equals": "待审"}}}]
    if card_id:
        ands.append({"property": {"property": "关联对象ID", "text": {"equals": card_id}}})
    res = lc.query(TBL["approval_tickets"], filt={"and": ands})
    return res[0] if res else None


def approve_payments(cur, card_id, opinion):
    return _decide_pay(cur, card_id, "通过", opinion)


def reject_payments(cur, card_id, opinion):
    if not opinion:
        return {"ok": False, "error": "退回必须填写意见"}
    return _decide_pay(cur, card_id, "退回", opinion)


def _decide_pay(cur, card_id, action, opinion):
    with _glob:
        t = _find_pay_ticket(card_id=card_id)
        if not t:
            return {"ok": False, "error": "付款审批工单不存在"}
        target = "通过" if action == "通过" else "退回"
        batch = ""
        # 找该卡片的付款批次
        pays = lc.query(TBL["pay_plan"], filt={
            "and": [
                {"property": {"property": "关联卡片ID", "text": {"equals": card_id}}},
                {"property": {"property": "已付", "checkbox": {"equals": False}}},
            ]})
        if pays:
            batch = val(pays[0], "付款批次")
        lc.update_checked(TBL["approval_tickets"], [{"record_id": t["record_id"], "properties": {
            "状态": {"select": target}, "审批人": {"text": cur["name"]},
            "意见": {"text": opinion or action},
            "审批时间": {"date": _now().strftime("%Y-%m-%dT%H:%M:%SZ")}}}])
        return {"ok": True, "state": target, "batch": batch}


def record_payment(cur, payment_id, b):
    """回写实付（自动/模板/手动）。"""
    try:
        amount = Decimal(str(b.get("实付金额", "")))
        if not amount.is_finite() or amount <= 0:
            raise ValueError()
        if amount != amount.quantize(Decimal("0.01")):
            raise ValueError()
    except Exception:
        return {"ok": False, "error": "请填写大于0、最多两位小数的实付金额"}
    try:
        paid_date = date.fromisoformat(str(b.get("实付日期", ""))).isoformat()
    except (ValueError, TypeError):
        return {"ok": False, "error": "请填写有效的实付日期（YYYY-MM-DD）"}
    rec = lc.get_record(TBL["pay_plan"], payment_id)
    if not rec:
        return {"ok": False, "error": "付款明细不存在"}
    props = {"已付": {"checkbox": True},
             "实付金额": {"currency": float(amount)},
             "实付日期": {"date": paid_date}}
    lc.update_checked(TBL["pay_plan"], [{"record_id": payment_id, "properties": props}])
    return {"ok": True}


# ---- 凭证（合同法计提 + IFRS 9类） ------------------------------------
# 真实科目编码（取自需求 5、a/b/c 凭证规则）
_DEFAULT_ACCOUNT = {
    # 合同法
    "损益科目": "6401.27", "应付租赁款": "1123.16",
    # IFRS 初始化/终止（5、c 合同新增/终止）
    "使用权资产原值": "1601.01",
    "使用权资产累计折旧": "1601.02",
    "租赁负债-租赁付款额": "2703.02",
    "租赁负债-未确认融资费用": "2703.01",
    "预付账款-预付租金": "1123.16",
    # IFRS 月度计提
    "折旧费用": "6401.30",        # 主营业务成本_租赁折旧费用（成本费用大类 03/07/09）
    "利息费用": "6603.07",        # 财务费用_租赁利息费用
    "未确认融资费用": "2703.01",  # 租赁负债_未确认融资费用（贷）
    "租赁负债付款额": "2703.02",  # 租赁负债_租赁付款额（借）
    "银行存款": "1002",
    "处置损益": "6711",
}
# 折旧费用按成本费用大类分流（需求 5、c 计提折旧；缺 BPM 大类主数据时默认主营业务成本）
DEPRECIATION_BY_CATEGORY = {
    "03": "6401.30", "07": "6401.30", "09": "6401.30",  # 主营业务成本
    "11": "6405.16",                                      # 医疗业务成本
    "05": "6601.27",                                      # 销售费用
    "02": "6602.13.28",                                   # 管理费用_教研费用
}


def _v(tp, card_id, code, period, summary, dr_acct, cr_acct, amt, neg=False):
    """借方正数行（贷方为 0）。neg=True 表示金额为负（冲销用）。"""
    a = amt if not neg else -amt
    return {
        "凭证类型": {"select": tp},
        "关联卡片ID": {"text": card_id},
        "记账期间": {"text": period},
        "摘要": {"text": summary},
        "科目编码": {"text": dr_acct},
        "科目名称": {"text": ""},
        "借方金额": {"currency": float(a.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))},
        "贷方金额": {"currency": float(Decimal("0"))},
        "辅助核算": {"text": code},
        "凭证状态": {"select": "已生成"},
    }


def _wc(tp, card_id, code, period, summary, cr_acct, amt, neg=False):
    """贷方正数行（借方为 0）。neg=True 表示金额为负。"""
    a = amt if not neg else -amt
    return {
        "凭证类型": {"select": tp},
        "关联卡片ID": {"text": card_id},
        "记账期间": {"text": period},
        "摘要": {"text": summary},
        "科目编码": {"text": cr_acct},
        "科目名称": {"text": ""},
        "借方金额": {"currency": float(Decimal("0"))},
        "贷方金额": {"currency": float(a.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))},
        "辅助核算": {"text": code},
        "凭证状态": {"select": "已生成"},
    }


def _approx_asset_orig(card, ifrs_rows):
    """使用权资产原值 = Σ 现值；未确认融资费用 = Σ(租赁付款额) - Σ 现值 - 预付租金。"""
    pv_sum = sum(dnum(r.get("现值")) for r in ifrs_rows)
    pay_sum = sum(dnum(r.get("租赁付款额")) for r in ifrs_rows)
    prepaid = dnum(val(card, "预付租金"))
    unamortized = (pay_sum - pv_sum).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return pv_sum, unamortized, prepaid


def generate_vouchers(cur, card_id, period=None):
    """生成合同法计提 + IFRS 9类凭证。

    覆盖需求 5、b 合同法 + 5、c IFRS 凭证规则：
      - 合同法计提（借 损益 / 贷 应付）
      - IFRS 合同新增（初始化）：借 使用权资产原值 / 借 未确认融资费用 / 贷 租赁付款额 / 贷 预付
      - IFRS 计提折旧（借 折旧费用[按大类] / 贷 累计折旧）
      - IFRS 计提利息（借 利息费用 / 贷 未确认融资费用）
      - IFRS 计提实付租金（借 租赁负债付款额 / 贷 银行存款）
      - IFRS 冲销合同法计提（负数红冲）
    终止/修改两类由 generate_event_vouchers 单独处理（需事件参数）。
    """
    with _glob:
        card = lc.get_record(TBL["cards"], card_id)
        if not card:
            return {"ok": False, "error": "卡片不存在"}
        if val(card, "审批状态", "") != "通过":
            return {"ok": False, "error": "仅审批通过的合同可生成凭证"}
        code = val(card, "合同编码")
        # 幂等：清旧凭证
        for v in lc.query(TBL["vouchers"], filt={
                "property": {"property": "关联卡片ID", "text": {"equals": card_id}}}):
            lc.delete(TBL["vouchers"], [v["record_id"]])

        ledger = _ledger_by_card(card_id)
        ifrs = _ifrs_by_card(card_id)
        vouchers = []
        target_periods = set()

        # 承债部门大类（简化：无 BPM 主数据时默认 03 主营业务成本）
        dept_cat = val(card, "承担部门类别", "03") or "03"
        dep_acct = DEPRECIATION_BY_CATEGORY.get(dept_cat[:2], _DEFAULT_ACCOUNT["折旧费用"])

        # 1) 合同法计提
        for r in ledger:
            ym = (val(r, "月份") or "")[:7]
            amt = dnum(r.get("实付金额合计"))
            if amt <= 0:
                continue
            target_periods.add(ym)
            vouchers.append(_v("合同法计提", card_id, code, ym, "计提%s租赁费" % ym,
                               _DEFAULT_ACCOUNT["损益科目"], "", amt))
            vouchers.append(_wc("合同法计提", card_id, code, ym, "计提%s租赁费" % ym,
                                _DEFAULT_ACCOUNT["应付租赁款"], amt))

        # 2) IFRS 凭证（若并入）
        if val(card, "是否IFRS", "") == "是" and ifrs:
            # 2a) IFRS 新增初始化（一条，取首月期间）
            pv_sum, unamortized, prepaid = _approx_asset_orig(card, ifrs)
            init_period = (val(ifrs[0], "月份") or "")[:7]
            if pv_sum > 0:
                vouchers.append(_v("IFRS新增", card_id, code, init_period, "IFRS合同新增-使用权资产原值",
                                   _DEFAULT_ACCOUNT["使用权资产原值"], "", pv_sum))
                if unamortized > 0:
                    vouchers.append(_v("IFRS新增", card_id, code, init_period, "IFRS合同新增-未确认融资费用",
                                       _DEFAULT_ACCOUNT["租赁负债-未确认融资费用"], "", unamortized))
                vouchers.append(_wc("IFRS新增", card_id, code, init_period, "IFRS合同新增-租赁付款额",
                                    _DEFAULT_ACCOUNT["租赁负债-租赁付款额"], pv_sum + unamortized))
                if prepaid:
                    vouchers.append(_wc("IFRS新增", card_id, code, init_period, "IFRS合同新增-预付租金",
                                        _DEFAULT_ACCOUNT["预付账款-预付租金"], prepaid))
                target_periods.add(init_period)

            # 2b) 月度计提
            for r in ifrs:
                ym = (val(r, "月份") or "")[:7]
                dep = dnum(r.get("资产折旧"))
                interest = dnum(r.get("负债利息"))
                payment = dnum(r.get("租赁付款额"))
                if dep:
                    vouchers.append(_v("IFRS折旧", card_id, code, ym, "计提%s折旧" % ym, dep_acct, "", dep))
                    vouchers.append(_wc("IFRS折旧", card_id, code, ym, "计提%s折旧" % ym,
                                        _DEFAULT_ACCOUNT["使用权资产累计折旧"], dep))
                    target_periods.add(ym)
                if interest:
                    vouchers.append(_v("IFRS利息", card_id, code, ym, "计提%s利息" % ym,
                                       _DEFAULT_ACCOUNT["利息费用"], "", interest))
                    vouchers.append(_wc("IFRS利息", card_id, code, ym, "计提%s利息" % ym,
                                        _DEFAULT_ACCOUNT["未确认融资费用"], interest))
                    target_periods.add(ym)
                if payment:
                    vouchers.append(_v("IFRS实付租金", card_id, code, ym, "支付%s租金" % ym,
                                       _DEFAULT_ACCOUNT["租赁负债付款额"], "", payment))
                    vouchers.append(_wc("IFRS实付租金", card_id, code, ym, "支付%s租金" % ym,
                                        _DEFAULT_ACCOUNT["银行存款"], payment))
                    # 冲销合同法计提
                    vouchers.append(_v("冲销合同法计提", card_id, code, ym, "冲销%s合同法计提" % ym,
                                       _DEFAULT_ACCOUNT["损益科目"], "", payment, neg=True))
                    vouchers.append(_wc("冲销合同法计提", card_id, code, ym, "冲销%s合同法计提" % ym,
                                        _DEFAULT_ACCOUNT["应付租赁款"], payment, neg=True))
                    target_periods.add(ym)

        # 过滤 period
        if period:
            vouchers = [v for v in vouchers if v["记账期间"]["text"] == period]
        if vouchers:
            for i in range(0, len(vouchers), 100):
                lc.add(TBL["vouchers"], vouchers[i:i+100])
        return {"ok": True, "vouchers": len(vouchers), "periods": sorted(target_periods)}


def generate_event_vouchers(cur, card_id, event_type, params):
    """事件型 IFRS 凭证：终止 / 租赁修改（涨租/减租）。

    event_type ∈ {"终止", "租赁修改"}
    params:
      终止: {"终止日":"YYYY-MM-DD", "处置损益":float(可选), "累计折旧":float, "原值":float,
             "未确认融资费用":float, "租赁付款额余额":float, "期末未付租金":float}
      租赁修改: {"修改日":"YYYY-MM-DD", "负债变动":float, "原值变动":float, "累计折旧变动":float,
                "未确认融资费用变动":float, "租赁付款额变动":float}（变动=修改后-修改前）
    借贷方向按需求 5、c 租赁修改规则实现。
    """
    with _glob:
        card = lc.get_record(TBL["cards"], card_id)
        if not card:
            return {"ok": False, "error": "卡片不存在"}
        code = val(card, "合同编码")
        vouchers = []
        q = Decimal("0.01")
        p = event_type
        d = str(params.get("修改日") or params.get("终止日") or "")[:10] or ""

        def dv(x):
            return dnum(params.get(x))

        if event_type == "租赁修改":
            # 需求规则：借 使用权资产-原值(变动) / 借 累计折旧(变动) / 贷 未确认融资费用(变动) / 贷 租赁付款额(变动)
            orig_delta = dv("原值变动")
            dep_delta = dv("累计折旧变动")
            unamortized_delta = dv("未确认融资费用变动")
            liability_delta = dv("租赁付款额变动")
            if orig_delta:
                vouchers.append(_v("租赁修改", card_id, code, d, "租赁修改-使用权资产原值调整",
                                   _DEFAULT_ACCOUNT["使用权资产原值"], "", orig_delta))
            if dep_delta:
                vouchers.append(_v("租赁修改", card_id, code, d, "租赁修改-累计折旧调整",
                                   _DEFAULT_ACCOUNT["使用权资产累计折旧"], "", dep_delta))
            if unamortized_delta:
                vouchers.append(_wc("租赁修改", card_id, code, d, "租赁修改-未确认融资费用调整",
                                    _DEFAULT_ACCOUNT["租赁负债-未确认融资费用"], unamortized_delta))
            if liability_delta:
                vouchers.append(_wc("租赁修改", card_id, code, d, "租赁修改-租赁付款额调整",
                                    _DEFAULT_ACCOUNT["租赁负债-租赁付款额"], liability_delta))
        elif event_type == "终止":
            # 处置分录（需求 5、c 终止规则）：
            #   借 使用权资产-累计折旧（冲销累计折旧）
            #   借/贷 处置损益（平衡差额）
            #   贷 使用权资产-原值
            #   贷/借 租赁负债-未确认融资费用 / 租赁付款额 等
            orig = dv("原值")
            dep = dv("累计折旧")
            unamortized = dv("未确认融资费用")
            liability = dv("租赁付款额余额")
            gain_loss = dv("处置损益")
            if orig:
                if dep:
                    vouchers.append(_v("终止处置", card_id, code, d, "终止-冲销累计折旧",
                                       _DEFAULT_ACCOUNT["使用权资产累计折旧"], "", dep))
                vouchers.append(_wc("终止处置", card_id, code, d, "终止-冲销使用权资产原值",
                                    _DEFAULT_ACCOUNT["使用权资产原值"], orig))
            if unamortized:
                vouchers.append(_v("终止处置", card_id, code, d, "终止-未确认融资费用",
                                   _DEFAULT_ACCOUNT["租赁负债-未确认融资费用"], "", unamortized))
            if liability:
                vouchers.append(_wc("终止处置", card_id, code, d, "终止-冲销租赁付款额",
                                    _DEFAULT_ACCOUNT["租赁负债-租赁付款额"], liability))
            if gain_loss:
                acct = _DEFAULT_ACCOUNT["处置损益"]
                if gain_loss > 0:
                    vouchers.append(_wc("终止处置", card_id, code, d, "终止-处置收益", acct, gain_loss))
                else:
                    vouchers.append(_v("终止处置", card_id, code, d, "终止-处置损失", acct, "", -gain_loss))
        else:
            return {"ok": False, "error": "未知事件类型"}

        if vouchers:
            for i in range(0, len(vouchers), 100):
                lc.add(TBL["vouchers"], vouchers[i:i+100])
        return {"ok": True, "vouchers": len(vouchers)}


def list_vouchers(filt=None):
    return lc.query(TBL["vouchers"], filt=filt)


# ---- 报表（数据聚合） ---------------------------------------------------
def report_address(cur, cid=None):
    """地址租赁报表：卡片 × 台账月度实付。"""
    cards = _cards_query()
    out = []
    for c in cards:
        cid_ = c["record_id"]
        ledger = _ledger_by_card(cid_)
        for r in ledger:
            out.append({
                "卡片": val(c, "卡片名称") or val(c, "合同编码"),
                "地址": val(c, "租赁地址"), "省级": val(c, "省级"), "市级": val(c, "市级"),
                "费用类型": val(c, "费用类型"), "月份": (val(r, "月份") or "")[:7],
                "实付金额合计": dnum(r.get("实付金额合计")),
            })
    return out


def report_cashflow(cur):
    """现金流：付款清单已付/未付。"""
    pays = list_payments()
    out = []
    for p in pays:
        cid = val(p, "关联卡片ID")
        card = lc.get_record(TBL["cards"], cid) if cid else None
        out.append({
            "卡片": val(card, "合同编码") if card else "",
            "费用所属期间": val(p, "费用所属期间"),
            "计划付款金额": dnum(p.get("计划付款金额")),
            "实付金额": dnum(p.get("实付金额")),
            "已付": bool(val(p, "已付", False)),
        })
    return out


def report_ifrs(cur):
    """IFRS 初始化/折旧/利息汇总。"""
    cards = _cards_query()
    out = []
    for c in cards:
        if val(c, "是否IFRS", "") != "是":
            continue
        ifrs = _ifrs_by_card(c["record_id"])
        for r in ifrs:
            out.append({
                "卡片": val(c, "合同编码"), "月份": (val(r, "月份") or "")[:7],
                "资产折旧": dnum(r.get("资产折旧")), "负债利息": dnum(r.get("负债利息")),
                "租赁付款额": dnum(r.get("租赁付款额")),
            })
    return out


def report_aging(cur, disclosure_date=None):
    """租赁负债到期分析：按披露日将租赁负债余额分桶 1年内/1-2年/2-5年/5年以上。"""
    from datetime import datetime as _dt
    if not disclosure_date:
        disclosure_date = "%s-12-31" % _dt.now().year
    dd = _dt.strptime(disclosure_date[:10], "%Y-%m-%d")
    cards = _cards_query()
    out = []
    for c in cards:
        if val(c, "是否IFRS", "") != "是":
            continue
        ifrs = _ifrs_by_card(c["record_id"])
        for r in ifrs:
            m = (val(r, "月份") or "")[:7]
            bal = dnum(r.get("租赁负债余额"))
            pay = dnum(r.get("租赁付款额"))
            interest = dnum(r.get("负债利息"))
            try:
                md = _dt.strptime(m + "-01", "%Y-%m-%d")
            except Exception:
                continue
            # 剩余月数：披露日之前的期间已经支付/已过账，不属于到期分析范围
            remain_months = (md.year - dd.year) * 12 + (md.month - dd.month)
            if remain_months < 0:
                continue
            if remain_months <= 12:
                bucket = "1年以内"
            elif remain_months <= 24:
                bucket = "1-2年"
            elif remain_months <= 60:
                bucket = "2-5年"
            else:
                bucket = "5年以上"
            out.append({
                "卡片": val(c, "合同编码"), "月份": m,
                "到期年限": bucket, "租赁负债余额": bal,
                "租赁付款额": pay, "负债利息": interest,
            })
    return out


def report_balance(cur):
    cards = _cards_query()
    out = []
    for c in cards:
        if val(c, "是否IFRS", "") != "是":
            continue
        ifrs = _ifrs_by_card(c["record_id"])
        if not ifrs:
            continue
        first = ifrs[0]
        last = ifrs[-1]
        out.append({
            "卡片": val(c, "合同编码"),
            "资产期初余额": dnum(first.get("使用权资产余额")) + dnum(first.get("资产折旧")),
            "资产期末余额": dnum(last.get("使用权资产余额")),
            "负债期初余额": dnum(first.get("租赁负债余额")) - dnum(first.get("负债利息")) + dnum(first.get("租赁付款额")),
            "负债期末余额": dnum(last.get("租赁负债余额")),
            "累计折旧": sum(dnum(r.get("资产折旧")) for r in ifrs),
        })
    return out


def report_sharing(cur):
    """成本分摊明细报表：按卡片×成本中心×生效区间列示分摊比例。"""
    cards = _cards_query()
    out = []
    for c in cards:
        shares = get_shares(c["record_id"])
        for s in shares:
            out.append({
                "卡片": val(c, "合同编码"),
                "成本中心": val(s, "成本中心"),
                "分摊比例": dnum(val(s, "分摊比例")),
                "生效日": val(s, "生效日"),
                "倒挤": bool(val(s, "倒挤", False)),
            })
    return out


# ---- 导出 CSV ----------------------------------------------------------
def _to_csv(rows, headers, keymap):
    import io
    buf = io.StringIO()
    buf.write(",".join(headers) + "\n")
    for r in rows:
        line = []
        for k in keymap:
            v = r.get(k, "") if isinstance(r, dict) else ""
            if isinstance(v, Decimal):
                v = str(v)
            v = str(v if v is not None else "").replace(",", "")
            line.append('"%s"' % v.replace('"', '""'))
        buf.write(",".join(line) + "\n")
    return buf.getvalue()


def export_csv(kind):
    if kind == "cards":
        rows = _cards_query()
        headers = ["合同编码", "卡片名称", "费用类型", "承租方", "租赁地址", "审批状态"]
        keymap = ["合同编码", "卡片名称", "费用类型", "承租方", "租赁地址", "审批状态"]
        return _to_csv([{k: val(r, k) for k in keymap} for r in rows], headers, keymap)
    if kind == "payments":
        rows = list_payments()
        headers = ["费用所属期间", "计划付款金额", "实付金额", "已付"]
        keymap = ["费用所属期间", "计划付款金额", "实付金额", "已付"]
        return _to_csv([{k: val(r, k) for k in keymap} for r in rows], headers, keymap)
    if kind == "vouchers":
        rows = list_vouchers()
        headers = ["凭证类型", "记账期间", "摘要", "科目编码", "借方金额", "贷方金额"]
        keymap = headers
        return _to_csv([{k: val(r, k) for k in keymap} for r in rows], headers, keymap)
    return ""


# ---- OCR / 文件上传 -----------------------------------------------------
def ocr_extract(files: list) -> dict:
    """使用项目内 importer.py；依赖必须安装在实际运行服务器。

    返回 {"fields":{...}, "segments":[...], "text":...}
    """
    if not files:
        return {"fields": {}, "segments": [], "text": "", "error": "无文件"}
    # 路径1：importer.parse（支持图片/PDF/docx/xlsx/csv/txt，云端依赖 requirements.txt）
    try:
        import importer
        for path in files:
            raw = open(path, "rb").read()
            ext = os.path.splitext(path)[1].lower()
            result = importer.parse({"filename": os.path.basename(path), "content": base64.b64encode(raw).decode()})
            if result.get("error"):
                return {"fields": {}, "segments": [], "text": "", "error": result["error"]}
            fields = result.get("fields", {})
            # importer 的字段是英文 key，映射为中文供前端用
            fmap = {
                "contract_no": "合同编号", "oa_no": "OA单号", "lessor": "出租方", "lessee": "承租方",
                "start_date": "租赁起始日", "end_date": "租赁终止日", "address": "租赁地址",
                "monthly_amount": "月租金", "tax_rate": "税率", "department": "提单部门", "applicant": "申请人",
            }
            cn_fields = {fmap.get(k, k): v for k, v in fields.items()}
            # 金额段由项目内规则提取（无则空）
            segs = _extract_segments(result.get("text", ""))
            return {"fields": cn_fields, "segments": segs, "text": result.get("text", "")}
    except Exception as e:
        return {"fields": {}, "segments": [], "text": "",
                "error": "合同解析失败：" + str(e)[:200]}


def _extract_segments(text: str):
    """从识别文本提取付款计划金额段（期次起-期次止:金额）。

    OCR 常把表格拆成独立行：期次（"4-12"）、期间（"第4至12个月"）、金额（"43580.00"）各占一行，
    因此用「期次 → 金额」状态机配对，而不是要求三者同处一行。
    """
    import re

    RE_PERIOD = re.compile(r"^第\s*[\d,]+\s*(?:至|到|~|-|—|–)\s*[\d,]+\s*个?月")
    RE_PERIOD_ONE = re.compile(r"^第\s*[\d,]+\s*个?月")
    RE_AMOUNT = re.compile(r"^[¥￥]?\s*([\d,]+(?:\.\d+)?)\s*(?:元|元/月)?$")
    RE_RANGE = re.compile(r"^[（(]?\s*(\d{1,3})\s*(?:至|到|~|-|—|–)\s*(\d{1,3})\s*[）)]?")
    # 紧凑写法 "期次:金额"，一行可含多段（用 ; ； , 分隔）
    RE_COMPACT = re.compile(r"(\d{1,3})\s*[-~]\s*(\d{1,3})\s*[:：]\s*([\d,]+(?:\.\d+)?)")

    text = text or ""
    # 优先只在「付款计划」区段内配对，避免与合同其他数字误配
    # 付款计划表标题写法多样，尽量覆盖；找不到标题才退化为全文（此时误配风险由人工复核兜底）
    m_sec = re.search(r"(付款计划|付款安排|支付计划|租金支付计划|付款明细|收款计划|交租计划|"
                      r"租金明细|付款节点|支付节点|付款期次|付款周期|租金支付)", text)
    scope = text[m_sec.start():] if m_sec else text

    segs, pending = [], None
    for raw in scope.splitlines():
        line = raw.strip()
        if not line:
            continue
        # 期间行（"第1个月" / "第4至12个月"）只作说明，不当作期次或金额
        if RE_PERIOD.match(line) or RE_PERIOD_ONE.match(line):
            continue
        # 紧凑写法优先：一行内的所有 "期次:金额" 全部取出，避免只取到第一段
        compact = RE_COMPACT.findall(line)
        if compact:
            for a, b, amt in compact:
                segs.append("%d-%d:%s" % (int(a), int(b), amt.replace(",", "")))
            pending = None
            continue
        # 金额行：整行只有金额
        m_amt = RE_AMOUNT.match(line)
        if m_amt:
            if pending:
                segs.append("%d-%d:%s" % (pending[0], pending[1], m_amt.group(1).replace(",", "")))
                pending = None
            continue
        # 期次行：X-Y（同行若还带金额则直接成段）
        m_rng = RE_RANGE.match(line)
        if m_rng:
            a, b = int(m_rng.group(1)), int(m_rng.group(2))
            if a > b:          # 期次倒置视为无效，不生成段
                continue
            rest = line[m_rng.end():]
            m_inline = re.search(r"([\d,]+(?:\.\d+)?)", re.sub(r"第\s*[\d,]+\s*个?月", "", rest))
            if m_inline:
                segs.append("%d-%d:%s" % (a, b, m_inline.group(1).replace(",", "")))
                pending = None
            else:
                pending = (a, b)
            continue
    # 兜底：分号/顿号分隔的 "1-1:43580.00;2-2:0.00" 单行写法
    if not segs:
        for m in re.finditer(r"(\d{1,3})\s*-\s*(\d{1,3})\s*[:：]\s*([\d,]+\.?\d*)", text or ""):
            segs.append("%s-%s:%s" % (m.group(1), m.group(2), m.group(3).replace(",", "")))
    return segs


def ocr_to_overview(card_id, ocr):
    """把 OCR 的金额段（期次-期次:金额）粗转成金额概览分段草稿，供人工复核。"""
    segs = []
    for s in ocr.get("segments", []):
        m = s.split(":")
        if len(m) != 2:
            continue
        rng, amt = m[0].strip(), m[1].strip()
        mm = rng.split("-")
        if len(mm) != 2:
            continue
        try:
            a, b = int(mm[0]), int(mm[1])
            segs.append({"期次起": a, "期次止": b, "金额": amt})
        except ValueError:
            continue
    return segs


# =======================================================================
# 统一业务状态与业务校验
# 状态一律由真实数据推导（卡片字段 + 金额概览 + 台账 + 审批工单），
# 禁止前端用多个字段自行拼凑，避免各处口径不一致。
# =======================================================================

STAGE_META = {
    "待人工核对": ("待人工核对", "continue-review"),
    "待补金额":   ("待补金额",   "fill-amount"),
    "待生成台账": ("待生成台账", "generate-ledger"),
    "待复核":     ("待复核",     "review-ledger"),
    "待提交审批": ("待提交审批", "submit-card"),
    "待审批":     ("待审批",     "approve"),
    "退回待修改": ("退回待修改", "edit-card"),
    "已通过":     ("已通过",     "open-payment"),
}
STEP_FLOW = ["资料进入", "人工核对", "业务校验", "租赁计算", "复核", "提交"]


def _tickets_by_card(card_id):
    return lc.query(TBL["approval_tickets"], filt={
        "property": {"property": "关联对象ID", "text": {"equals": card_id}}})


def _ocr_pending(card):
    """OCR 结果是否仍待人工确认（字段 OCR状态 由识别流程写入）。"""
    return str(val(card, "OCR状态", "") or "").strip() in ("已识别待核对", "核对失败")


def _segments_of(overview):
    """把金额概览记录转成 expand_monthly 需要的分段结构。"""
    out = []
    for r in overview or []:
        out.append({"起始日": val(r, "租赁起始日", ""),
                    "终止日": val(r, "租赁终止日", ""),
                    "月金额": val(r, "月金额", 0)})
    return out


def check_payment_coverage(start, end, segments):
    """付款计划是否覆盖完整租期：返回缺失月份、重复月份与结论。"""
    a, b = norm_d(start)[:10], norm_d(end)[:10]
    if len(a) != 10 or len(b) != 10:
        return {"missing": [], "duplicated": [], "complete": False,
                "error": "起止日期不完整"}
    try:
        s = date.fromisoformat(a); e = date.fromisoformat(b)
    except ValueError:
        return {"missing": [], "duplicated": [], "complete": False, "error": "起止日期非法"}
    if e < s:
        return {"missing": [], "duplicated": [], "complete": False, "error": "终止日早于起始日"}

    expected, cur = [], _month_first(s)
    while cur <= _month_first(e):
        expected.append(cur.strftime("%Y-%m"))
        cur = _next_month_first(cur)

    # 逐段展开并计数，才能发现"两段覆盖同一月份"的重叠
    counter = {}
    for seg in segments or []:
        sa, sb = norm_d(seg.get("起始日"))[:10], norm_d(seg.get("终止日"))[:10]
        if len(sa) != 10 or len(sb) != 10:
            continue
        try:
            c1, c2 = date.fromisoformat(sa), date.fromisoformat(sb)
        except ValueError:
            continue
        if c2 < c1:
            continue
        c = _month_first(c1)
        while c <= _month_first(c2):
            k = c.strftime("%Y-%m")
            counter[k] = counter.get(k, 0) + 1
            c = _next_month_first(c)

    missing = sorted(set(expected) - set(counter))
    duplicated = sorted([m for m, n in counter.items() if n > 1])
    return {"missing": missing, "duplicated": duplicated,
            "complete": (not missing and not duplicated),
            "expected_months": len(expected), "covered_months": len(counter)}


def validate_card(card_id, card=None, overview=None, shares=None, ledger=None):
    """业务校验：只报真实阻塞项，不编造。可传入已取好的数据避免重复查询。"""
    if card is None:
        card = lc.get_record(TBL["cards"], card_id)
    if not card:
        return {"ok": False, "error": "卡片不存在"}
    errors, warnings = [], []

    def err(field, code, message):
        errors.append({"field": field, "code": code, "message": message})

    def warn(field, message):
        warnings.append({"field": field, "message": message})

    code = str(val(card, "合同编码", "") or "").strip()
    if not code:
        err("合同编码", "REQUIRED", "合同编码不能为空")
    else:
        same = lc.query(TBL["cards"], filt={
            "property": {"property": "合同编码", "text": {"equals": code}}})
        same = [r for r in same if val(r, '费用类型') == val(card, '费用类型')]
        if len(same) > 1:
            err("合同编码", "DUPLICATE",
                "合同编码 %s 已存在 %d 张卡片，疑似重复导入" % (code, len(same)))

    for f, label in (("承租方", "承租方"), ("实际收款方", "出租方/实际收款方"),
                     ("租赁地址", "租赁地址")):
        if not str(val(card, f, "") or "").strip():
            err(f, "REQUIRED", "%s不能为空" % label)

    start, end = val(card, "租赁起始日", ""), val(card, "租赁终止日", "")
    if not start:
        err("租赁起始日", "REQUIRED", "租赁起始日不能为空")
    if not end:
        err("租赁终止日", "REQUIRED", "租赁终止日不能为空")
    if start and end and str(start)[:10] >= str(end)[:10]:
        err("租赁终止日", "DATE_ORDER", "租赁起始日必须早于租赁终止日")

    area = dnum(val(card, "计租面积", 0)) or dnum(val(card, "建筑面积", 0))
    if not area or area <= 0:
        err("计租面积", "INVALID", "计租面积必须大于 0")

    if str(val(card, "是否IFRS", "否")) == "是" and not (dnum(val(card, "IFRS折现率", 0)) > 0):
        err("IFRS折现率", "REQUIRED", "「是否IFRS=是」时必须填写折现率（如 0.04）")

    if _ocr_pending(card):
        warn("OCR结果", "识别结果尚未人工确认，请核对后再提交")

    overview = _overview_by_card(card_id) if (overview is None and start and end) else (overview or [])
    if not overview:
        err("付款计划", "MISSING", "尚未录入金额概览（付款计划），无法生成台账")
    else:
        cov = check_payment_coverage(start, end, _segments_of(overview))
        if cov.get("error"):
            err("付款计划", "INVALID", "租期解析失败：" + cov["error"])
        else:
            if cov["missing"]:
                err("付款计划", "COVERAGE_INCOMPLETE",
                    "付款计划未覆盖完整租期，缺失 %d 个月：%s"
                    % (len(cov["missing"]), "、".join(cov["missing"][:6])
                       + ("…" if len(cov["missing"]) > 6 else "")))
            if cov["duplicated"]:
                err("付款计划", "OVERLAP",
                    "付款计划存在重复覆盖月份：" + "、".join(cov["duplicated"][:6]))

    if shares is None:
        shares = get_shares(card_id) if card_id else []
    if shares:
        total = sum((dnum(val(s, "分摊比例", 0)) for s in shares), Decimal("0"))
        # 表内口径为 0-1 小数（与 save_shares 的合计=1 校验、AI 扫描提示词一致），
        # 切勿按百分比校验，否则会把 0.6/0.25/0.15 这类正确数据误判为异常。
        if abs(total - Decimal("1")) > Decimal("0.01"):
            pct = (total * 100).quantize(Decimal("0.01"))
            err("分摊比例", "SUM_NOT_1",
                "分摊比例合计为 %s（0-1 小数口径，应为 1，当前约 %s%%）" % (total, pct))

    if ledger is None:
        ledger = _ledger_by_card(card_id)
    if not ledger:
        warn("合同法台账", "尚未生成合同法台账")

    return {"ok": len(errors) == 0, "valid": len(errors) == 0,
            "errors": errors, "warnings": warnings,
            "stage": get_card_stage(card)["stage"]}


def get_card_stage(card, overview=None, ledger=None):
    """由真实数据推导当前阶段与下一步动作。"""
    card_id = card.get("record_id", "")
    state = str(val(card, "审批状态", "草稿") or "草稿")
    overview = _overview_by_card(card_id) if overview is None else overview
    ledger = _ledger_by_card(card_id) if ledger is None else ledger

    blocking = []
    start, end = val(card, "租赁起始日", ""), val(card, "租赁终止日", "")
    if not str(val(card, "合同编码", "") or "").strip():
        blocking.append("合同编码为空")
    if not str(val(card, "承租方", "") or "").strip():
        blocking.append("承租方为空")
    if not str(val(card, "租赁地址", "") or "").strip():
        blocking.append("租赁地址为空")
    if not start or not end:
        blocking.append("租赁起止日期不完整")
    elif str(start)[:10] >= str(end)[:10]:
        blocking.append("租赁起始日不早于终止日")
    area = dnum(val(card, "计租面积", 0)) or dnum(val(card, "建筑面积", 0))
    if not area or area <= 0:
        blocking.append("计租面积无效")
    if str(val(card, "是否IFRS", "否")) == "是" and not (dnum(val(card, "IFRS折现率", 0)) > 0):
        blocking.append("IFRS=是 但折现率为空")
    if _ocr_pending(card):
        blocking.append("OCR 识别结果待人工确认")

    if state == "通过":
        stage = "已通过"
    elif state == "退回":
        stage = "退回待修改"
    elif state == "待审":
        stage = "待审批"
    elif state == "草稿":
        if blocking:
            # 有明显阻塞 → 先让人工把资料补齐/核对
            stage = "待人工核对"
        elif not overview:
            stage = "待补金额"
        elif not ledger:
            stage = "待生成台账"
        else:
            stage = "待复核"
    else:
        stage = "待人工核对"

    # 业务进度：按已完成的真实事实推进
    done = 1                                   # 资料进入（卡片已存在）
    if not blocking:
        done = 2                               # 人工核对通过
    if overview and not blocking:
        done = max(done, 3)                    # 业务校验（金额依据已具备）
    if ledger and overview and not blocking:
        done = max(done, 4)                    # 租赁计算（台账已生成）
    if state == "通过" and not blocking and overview and ledger:
        done = 6
    elif state == "待审" and not blocking and overview and ledger:
        done = 5

    label, action = STAGE_META.get(stage, (stage, ""))
    return {
        "stage": stage,
        "label": label,
        "next_action": action,
        "blocking_reasons": blocking,
        "completed_steps": STEP_FLOW[:min(done, len(STEP_FLOW))],
        "step_index": min(done, len(STEP_FLOW)),
        "steps": STEP_FLOW,
        "approval_state": state,
        "has_overview": bool(overview),
        "has_ledger": bool(ledger),
    }


def workbench_summary(cur):
    """首页工作台：按角色给出真实任务量、最近处理与风险提示。一次扫描避免 N+1。"""
    role = cur["role"]
    is_approver = role in (ROLE_ADMIN, ROLE_APPROVER)
    cards = _cards_query()
    mine = [c for c in cards if val(c, "提交人", "") == cur["name"]]

    ov_ids, ld_ids = set(), set()
    for row in lc.query_all(TBL["amount_overview"]):
        ov_ids.add(val(row, "关联卡片ID", ""))
    for row in lc.query_all(TBL["contract_ledger"]):
        ld_ids.add(val(row, "关联卡片ID", ""))

    def stage_of(c):
        cid = c.get("record_id", "")
        return get_card_stage(c, overview=(1 if cid in ov_ids else 0) and [1] or [],
                              ledger=(1 if cid in ld_ids else 0) and [1] or [])["stage"]

    scope = cards if is_approver else mine
    buckets = {"待人工核对": 0, "待补金额": 0, "待生成台账": 0,
               "待复核": 0, "待提交审批": 0, "待审批": 0, "退回待修改": 0, "已通过": 0}
    for c in scope:
        st = stage_of(c)
        if st in buckets:
            buckets[st] += 1

    tasks = []
    if is_approver:
        tasks.append({"key": "待审批", "label": "待审批", "count": buckets["待审批"],
                      "view": "todo"})
    tasks.append({"key": "待人工核对", "label": "待人工核对", "count": buckets["待人工核对"],
                  "view": "mine" if not is_approver else "cards"})
    tasks.append({"key": "待补金额", "label": "待补金额", "count": buckets["待补金额"],
                  "view": "mine" if not is_approver else "cards"})
    tasks.append({"key": "待生成台账", "label": "待生成台账", "count": buckets["待生成台账"],
                  "view": "mine" if not is_approver else "cards"})
    tasks.append({"key": "待提交审批", "label": "待提交审批", "count": buckets["待提交审批"],
                  "view": "mine" if not is_approver else "cards"})
    tasks.append({"key": "退回待修改", "label": "退回待修改", "count": buckets["退回待修改"],
                  "view": "mine" if not is_approver else "cards"})

    recent = []
    for c in sorted(scope, key=lambda x: str(val(x, "提交时间", "")), reverse=True)[:5]:
        cid = c.get("record_id", "")
        st = get_card_stage(c, overview=(1 if cid in ov_ids else 0) and [1] or [],
                            ledger=(1 if cid in ld_ids else 0) and [1] or [])
        recent.append({
            "card_id": cid,
            "code": str(val(c, "合同编码", "")),
            "name": str(val(c, "卡片名称", "")),
            "stage": st["stage"],
            "next_action": st["next_action"],
            "updated_at": str(val(c, "提交时间", ""))[:10],
            "approval_state": st["approval_state"],
        })

    alerts = []
    ifrs_missing = [c for c in scope
                    if str(val(c, "是否IFRS", "否")) == "是"
                    and not (dnum(val(c, "IFRS折现率", 0)) > 0)]
    if ifrs_missing:
        alerts.append({"level": "warn", "code": "IFRS_RATE_MISSING",
                       "message": "%d 张卡片标记为 IFRS 但折现率为空，IFRS 台账无法生成"
                                  % len(ifrs_missing)})
    no_amount = [c for c in scope if c.get("record_id", "") not in ov_ids]
    if no_amount:
        alerts.append({"level": "info", "code": "NO_PAY_PLAN",
                       "message": "%d 张卡片尚未录入付款计划" % len(no_amount)})

    return {
        "ok": True,
        "role": role,
        "tasks": tasks,
        "recent": recent,
        "alerts": alerts,
        "links": {
            "team_space": "https://www.workbuddy.cn/space/s/" + str(CONFIG.get("space_id", "")),
        },
    }


def ocr_confirm(cur, card_id, status="人工已确认"):
    """人工核对完成确认：把 OCR状态 落为「人工已确认」，写后回读确认。"""
    if status not in ("人工已确认", "核对失败"):
        return {"ok": False, "error": "状态只能是 人工已确认 或 核对失败"}
    card = lc.get_record(TBL["cards"], card_id)
    if not card:
        return {"ok": False, "error": "卡片不存在"}
    with _glob:
        lc.update_checked(TBL["cards"], [{
            "record_id": card_id, "properties": {"OCR状态": {"select": status}}}])
        chk = lc.get_record(TBL["cards"], card_id)
        got = str(val(chk, "OCR状态", "") or "")
        if got != status:
            return {"ok": False, "error": "OCR状态写入后仍为[%s]，未能变为[%s]" % (got, status)}
        return {"ok": True, "card_id": card_id, "OCR状态": got,
                "write_confirmed": True}


def save_payment_draft(cur, card_id, rows):
    """保存合同原始付款明细快照；不生成付款指令或改变审批状态。"""
    if cur.get('role') not in (ROLE_ADMIN, ROLE_KEEPER):
        return {'ok': False, 'error': '无权维护付款明细'}
    if not isinstance(rows, list) or len(rows) > 600:
        return {'ok': False, 'error': '付款明细格式错误或超过600行'}
    keys = ('period', 'pay_date', 'rent', 'mgmt', 'total', 'tax')
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            return {'ok': False, 'error': '付款明细必须为对象'}
        item = {k: str(row.get(k) if row.get(k) is not None else '').strip() for k in keys}
        for key in ('rent', 'mgmt', 'total'):
            if item[key]:
                try:
                    number = Decimal(item[key].replace(',', ''))
                    if not number.is_finite() or number < 0:
                        raise ValueError()
                except (ValueError, ArithmeticError):
                    return {'ok': False, 'error': '付款明细金额无效：' + key}
        if item['pay_date']:
            try:
                date.fromisoformat(item['pay_date'])
            except ValueError:
                return {'ok': False, 'error': '应付日格式无效'}
        normalized.append(item)
    with _glob:
        card = lc.get_record(TBL['cards'], card_id)
        if not card or val(card, '审批状态', '') not in ('草稿', '退回'):
            return {'ok': False, 'error': '仅草稿或退回卡片可保存付款明细'}
        snapshot = json.dumps({'kind': 'payment-draft-v1', 'rows': normalized,
                               'review_status': '待人工核对'}, ensure_ascii=False, sort_keys=True)
        version = 'payment-' + hashlib.sha256(snapshot.encode()).hexdigest()[:24]
        existing = lc.query(TBL['version_snapshots'], filt={'and': [
            {'property': {'property': '关联卡片ID', 'text': {'equals': card_id}}},
            {'property': {'property': '版本号', 'text': {'equals': version}}}]})
        for record in existing:
            if val(record, '快照JSON', '') == snapshot:
                return {'ok': True, 'write_confirmed': True, 'rows': normalized, 'reused': True}
        result = lc.add(TBL['version_snapshots'], [{
            '关联卡片ID': {'text': card_id}, '版本号': {'text': version},
            '变更日期': {'date': _now().isoformat()},
            '变更原因': {'text': '合同付款明细草稿'},
            '变更依据': {'text': '人工录入或识别候选值，待人工核对'},
            '快照JSON': {'text': snapshot}}])
        rec = result[0] if isinstance(result, list) and result else {}
        rid = rec.get('id') or rec.get('record_id')
        saved = lc.get_record(TBL['version_snapshots'], rid) if rid else None
        confirmed = bool(saved and val(saved, '快照JSON', '') == snapshot
                         and val(saved, '关联卡片ID', '') == card_id)
        return {'ok': confirmed, 'write_confirmed': confirmed,
                'rows': normalized if confirmed else [],
                'error': '' if confirmed else '付款明细写入尚未回读确认，请保留草稿并重试'}


def card_context(card_id):
    """详情页一次取全：卡片 + 阶段 + 校验 + 三台账 + 分摊/一次性费用 + 审批/付款/凭证/版本。
    详情页只需 1 次请求，避免多次串行调用。"""
    card = lc.get_record(TBL["cards"], card_id)
    if not card:
        return {"ok": False, "error": "卡片不存在"}
    overview = _overview_by_card(card_id)
    ledger = _ledger_by_card(card_id)
    ifrs = _ifrs_by_card(card_id)
    shares = get_shares(card_id)
    try:
        oneoff = get_oneoff(card_id)
    except Exception:
        oneoff = []
    tickets = _tickets_by_card(card_id)
    payments = lc.query(TBL["pay_plan"], filt={
        "property": {"property": "关联卡片ID", "text": {"equals": card_id}}})
    voucher_rows = lc.query(TBL["vouchers"], filt={
        "property": {"property": "关联卡片ID", "text": {"equals": card_id}}})
    versions = lc.query(TBL["version_snapshots"], filt={
        "property": {"property": "关联卡片ID", "text": {"equals": card_id}}})

    stage = get_card_stage(card, overview=overview, ledger=ledger)
    vres = validate_card(card_id, card=card, overview=overview, shares=shares, ledger=ledger)

    return {
        "ok": True,
        "card": card,
        "stage": stage,
        "validation": {"valid": vres.get("valid", False),
                       "errors": vres.get("errors", []),
                       "warnings": vres.get("warnings", [])},
        "overview": overview,
        "ledger": ledger,
        "ifrs": ifrs,
        "shares": shares,
        "oneoff_fees": oneoff,
        "tickets": sorted(tickets, key=lambda t: str(val(t, "提交时间", ""))),
        "payments": payments,
        "vouchers": voucher_rows,
        "versions": versions,
        "intake": next(iter(reversed(W.events(sys.modules[__name__], card_id, "intake-v2", records=versions))), None),
        "approval_events": W.events(sys.modules[__name__], card_id, "approval-v2", records=versions),
    }


_legacy_submit_card = submit_card
_legacy_record_payment = record_payment

# ---- HTTP --------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        safe = re.sub(r'(token=)[^&\s\"]+', r'\1[redacted]', fmt % args)
        sys.stderr.write("[zl] " + safe + "\n")

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization")
        self.end_headers()

    def _require(self, roles=None):
        cur = current(self)
        if not cur:
            self._json(401, {"ok": False, "error": "未登录或会话过期"})
            return None
        active = find_user(cur['account'])
        if not active:
            self._json(401, {'ok':False,'error':'账号已停用或不存在'})
            return None
        cur['role'] = val(active, '角色')
        cur['name'] = val(active, '姓名') or cur['account']
        if roles and cur["role"] not in roles:
            self._json(403, {"ok": False, "error": f"无权操作：需要角色 {roles}"})
            return None
        return cur

    def do_GET(self):
        parsed = urlparse(self.path)
        p = parsed.path
        try:
            if p in ("/", "/index.html", "/app"):
                self._serve_index()
                return
            if p == '/ui-workflow.js':
                script = (HERE / 'ui-workflow.js').read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'application/javascript; charset=utf-8')
                self.send_header('Content-Length', str(len(script)))
                self.end_headers()
                self.wfile.write(script)
                return
            if p == '/api/payment-batches':
                user = self._require()
                if user:
                    rows = list(W.batches(sys.modules[__name__]).values())
                    if user['role'] not in (ROLE_ADMIN, '付款审批人', '资金管理处'):
                        rows = [r for r in rows if r['actor'] == user['account']]
                    self._json(200, {'ok':True,'batches':rows})
                return
            if p == "/api/me":
                cur = current(self)
                if not cur:
                    self._json(401, {"ok": False, "error": "未登录"})
                    return
                self._json(200, {"ok": True, "user": {"account": cur["account"],
                                 "name": cur["name"], "role": cur["role"]}})
                return
            if p == "/api/workbench/summary":
                cur = self._require()
                if not cur:
                    return
                self._json(200, cached_response("summary:" + cur["account"], lambda: workbench_summary(cur), 5))
                return
            # 主数据下拉（只读，登录即可）
            if p.startswith("/api/master/"):
                cur = self._require()
                if not cur:
                    return
                mtype = p.split("/api/master/")[1]
                self._get_master(cur, mtype)
                return
            if p.startswith("/api/cards/"):
                cur = self._require()
                if not cur:
                    return
                self._get_card(cur, p)
                return
            if p == "/api/cards":
                cur = self._require()
                if not cur:
                    return
                self._get_cards(cur, parsed)
                return
            if p == "/api/todo":
                cur = self._require()
                if not cur:
                    return
                self._get_todo(cur)
                return
            # ---- 阶段5：付款 / 凭证 / 报表 / 分摊 / 主数据 / 导出 ----
            if p.startswith("/api/shares/"):
                cur = self._require()
                if not cur:
                    return
                card_id = p.split("/api/shares/")[1]
                self._json(200, {"ok": True, "shares": get_shares(card_id)})
                return
            if p == "/api/payments":
                cur = self._require()
                if not cur:
                    return
                self._get_payments(cur, parsed)
                return
            if p.startswith("/api/payments/todo"):
                cur = self._require()
                if not cur:
                    return
                self._get_pay_todo(cur)
                return
            if p == "/api/vouchers":
                cur = self._require()
                if not cur:
                    return
                card_id = parse_qs(parsed.query).get("card", [""])[0]
                filt = {"property": {"property": "关联卡片ID", "text": {"equals": card_id}}} if card_id else None
                self._json(200, {"ok": True, "vouchers": list_vouchers(filt)})
                return
            if p == "/api/admin/requests":
                k = self._require(roles=[ROLE_ADMIN])
                if not k:
                    return
                self._json(200, {"ok": True, "requests": list_requests(k)})
                return
            if p == "/api/reports/address":
                cur = self._require()
                if not cur:
                    return
                self._json(200, {"ok": True, "rows": report_address(cur)})
                return
            if p == "/api/reports/cashflow":
                cur = self._require()
                if not cur:
                    return
                self._json(200, {"ok": True, "rows": report_cashflow(cur)})
                return
            if p == "/api/reports/ifrs":
                cur = self._require()
                if not cur:
                    return
                self._json(200, {"ok": True, "rows": report_ifrs(cur)})
                return
            if p == "/api/reports/aging":
                cur = self._require()
                if not cur:
                    return
                dd = parse_qs(parsed.query).get("date", [""])[0] or None
                self._json(200, {"ok": True, "rows": report_aging(cur, dd)})
                return
            if p == "/api/reports/balance":
                cur = self._require()
                if not cur:
                    return
                self._json(200, {"ok": True, "rows": report_balance(cur)})
                return
            if p == "/api/reports/sharing":
                cur = self._require()
                if not cur:
                    return
                self._json(200, {"ok": True, "rows": report_sharing(cur)})
                return
            if p.startswith("/api/export/"):
                cur = self._require()
                if not cur:
                    return
                self._export(cur, p)
                return
            self._json(404, {"ok": False, "error": "not found"})
        except RuntimeError as e:
            self._json(502, {"ok": False, "error": str(e)[:400]})
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)[:400]})

    def _get_cards(self, cur, parsed):
        q = parse_qs(parsed.query)
        state = q.get("state", [""])[0]
        mine = q.get("mine", [""])[0]
        filt = None
        ands = []
        if state:
            ands.append({"property": {"property": "审批状态", "select": {"equals": state}}})
        if mine:
            ands.append({"property": {"property": "提交人", "text": {"equals": cur["name"]}}})
        if ands:
            filt = {"and": ands}
        cards = _cards_query(filt)
        # 一次读取子表并建立索引，避免列表页对每张卡片发两次查询造成 N+1 卡顿。
        card_ids = {c.get("record_id", "") for c in cards}
        ov_counts = {cid: 0 for cid in card_ids}
        ld_counts = {cid: 0 for cid in card_ids}
        if card_ids:
            for row in lc.query_all(TBL["amount_overview"]):
                cid = val(row, "关联卡片ID", "")
                if cid in ov_counts:
                    ov_counts[cid] += 1
            for row in lc.query_all(TBL["contract_ledger"]):
                cid = val(row, "关联卡片ID", "")
                if cid in ld_counts:
                    ld_counts[cid] += 1
        for c in cards:
            cid = c.get("record_id", "")
            c["_overview_count"] = ov_counts.get(cid, 0)
            c["_ledger_count"] = ld_counts.get(cid, 0)
            c["_stage"] = get_card_stage(c, overview=[True] if ov_counts.get(cid) else [],
                                          ledger=[True] if ld_counts.get(cid) else [])
        self._json(200, {"ok": True, "cards": cards})

    def _get_card(self, cur, p):
        parts = p.split("/")
        card_id = parts[3]
        action = parts[4] if len(parts) > 4 else None
        if action not in ('approve', 'reject', 'withdraw', 'validate'):
            if not W.can_edit(sys.modules[__name__], cur, card_id):
                self._json(403, {'ok':False, 'error':'只能修改本人账号关联的草稿，旧数据需先完成归属迁移'})
                return
        if action == 'delete' and W.events(sys.modules[__name__], card_id, 'approval-v2'):
            self._json(409, {'ok':False, 'error':'有审批历史的卡片不可删除'})
            return
        if action == "context":
            self._json(200, card_context(card_id))
            return
        if action == "overview":
            self._json(200, {"ok": True, "overview": _overview_by_card(card_id)})
            return
        if action == "ledger":
            self._json(200, {"ok": True, "ledger": _ledger_by_card(card_id)})
            return
        if action == "ifrs":
            self._json(200, {"ok": True, "ifrs": _ifrs_by_card(card_id)})
            return
        # 详情
        card = lc.get_record(TBL["cards"], card_id)
        if not card:
            self._json(404, {"ok": False, "error": "卡片不存在"})
            return
        self._json(200, {"ok": True, "card": card})

    def _get_todo(self, cur):
        # 审批人/管理员：待我审批；其他人返回 403（不再伪装成空列表）
        if cur["role"] not in (ROLE_ADMIN, ROLE_APPROVER):
            self._json(403, {"ok": False, "error": "仅台账审批人可查看待办审批"})
            return
        filt = {"property": {"property": "审批状态", "select": {"equals": "待审"}}}
        self._json(200, {"ok": True, "todo": _cards_query(filt)})

    def _get_master(self, cur, mtype):
        m = {
            "suppliers": "md_suppliers", "depts": "md_depts", "rules": "md_account_rules",
            "contacts": "md_contacts", "entities": "md_entities", "sites": "md_sites",
            "params": "md_params",
        }.get(mtype)
        if not m:
            self._json(404, {"ok": False, "error": "未知主数据类型"})
            return
        self._json(200, {"ok": True, "rows": lc.query(TBL[m])})

    def _get_payments(self, cur, parsed):
        q = parse_qs(parsed.query)
        card_id = q.get("card", [""])[0]
        unpaid_only = q.get("unpaid", [""])[0]
        filt = None
        ands = []
        if card_id:
            ands.append({"property": {"property": "关联卡片ID", "text": {"equals": card_id}}})
        if unpaid_only:
            ands.append({"property": {"property": "已付", "checkbox": {"equals": False}}})
        if ands:
            filt = {"and": ands}
        rows = list_payments(filt)
        cards = {r['record_id']: r for r in lc.query(TBL['cards'])}
        batch_index = {rid:b for b in W.batches(sys.modules[__name__]).values() for rid in b['rows']}
        for row in rows:
            card = cards.get(val(row,'关联卡片ID'), {})
            for field in ('合同编码','卡片名称','费用类型','租赁地址'):
                row[field] = val(card,field)
            batch = batch_index.get(row['record_id'], {})
            row['_batch'] = batch.get('batch','')
            row['_approval'] = batch.get('state','未提交')
        self._json(200, {"ok": True, "payments": rows})

    def _get_pay_todo(self, cur):
        if cur["role"] not in (ROLE_ADMIN, "付款审批人"):
            self._json(403, {"ok": False, "error": "仅付款审批人可查看付款待办"})
            return
        tickets = lc.query(TBL["approval_tickets"], filt={
            "and": [
                {"property": {"property": "工单类型", "select": {"equals": "付款审批"}}},
                {"property": {"property": "状态", "select": {"equals": "待审"}}},
            ]})
        self._json(200, {"ok": True, "tickets": tickets})

    def _export(self, cur, p):
        kind = p.split("/api/export/")[1]
        csv = export_csv(kind)
        if not csv:
            self._json(400, {"ok": False, "error": "未知导出类型"})
            return
        body = csv.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="%s.csv"' % kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        clear_response_cache()
        parsed = urlparse(self.path)
        body = self._body()
        self._loaded_body = body
        p = parsed.path
        try:
            if p == "/api/login":
                acct = str(body.get("账号") or body.get("account") or "").strip()
                pwd = str(body.get("密码") or body.get("password") or "").strip()
                u = find_user(acct)
                if not u or not verify_pwd(pwd, val(u, "密码哈希")):
                    self._json(401, {"ok": False, "error": "账号或密码错误"})
                    return
                name = val(u, "姓名") or acct
                role = val(u, "角色") or ROLE_KEEPER
                tok = create_session(acct, name, role)
                self._json(200, {"ok": True, "token": tok,
                                 "user": {"account": acct, "name": name, "role": role}})
                return
            if p == "/api/logout":
                self._json(200, {"ok": True})
                return
            # 账号申请：不要求登录，但开通前账号处于未启用状态，无法登录
            if p == "/api/register":
                self._json(200, register_request(body))
                return

            cur = self._require()
            if not cur:
                return

            if p == '/api/intake':
                self._json(200, W.save_intake(sys.modules[__name__], cur, body))
                return
            if p == '/api/payment-batch/decide':
                action = body.get('action')
                if action not in ('通过', '退回'):
                    self._json(400, {'ok':False, 'error':'审批动作无效'})
                    return
                self._json(200, W.decide_payment(sys.modules[__name__], cur, body.get('batch'), action, body.get('opinion','')))
                return

            if p.startswith("/api/admin/requests/") and p.endswith("/approve"):
                k = self._require(roles=[ROLE_ADMIN])
                if not k:
                    return
                uid = p.split("/api/admin/requests/")[1].split("/approve")[0]
                self._json(200, decide_request(k, uid, True, body.get("角色") or None))
                return
            if p.startswith("/api/admin/requests/") and p.endswith("/reject"):
                k = self._require(roles=[ROLE_ADMIN])
                if not k:
                    return
                uid = p.split("/api/admin/requests/")[1].split("/reject")[0]
                self._json(200, decide_request(k, uid, False))
                return

            if p == "/api/upload":
                k = self._require(roles=[ROLE_ADMIN, ROLE_KEEPER])
                if not k:
                    return
                self._upload(k, body)
                return
            if p == "/api/ai-scan":
                k = self._require(roles=[ROLE_ADMIN, ROLE_KEEPER])
                if not k:
                    return
                self._ai_scan(k, body)
                return

            if p == "/api/cards":
                k = self._require(roles=[ROLE_ADMIN, ROLE_KEEPER])
                if not k:
                    return
                self._json(200, create_card(k, body))
                return
            if p.startswith("/api/cards/"):
                self._post_card(cur, p, body)
                return
            # ---- 阶段5 POST 路由 ----
            if p.startswith("/api/shares/"):
                k = self._require(roles=[ROLE_ADMIN, ROLE_KEEPER])
                if not k:
                    return
                card_id = p.split("/api/shares/")[1]
                self._json(200, save_shares(k, card_id, body.get("shares") or []))
                return
            if p.startswith("/api/oneoff/"):
                k = self._require(roles=[ROLE_ADMIN, ROLE_KEEPER])
                if not k:
                    return
                card_id = p.split("/api/oneoff/")[1]
                self._json(200, save_oneoff(k, card_id, body.get("fees") or []))
                return
            if p.startswith("/api/payments/generate/"):
                k = self._require(roles=[ROLE_ADMIN, ROLE_KEEPER])
                if not k:
                    return
                card_id = p.split("/api/payments/generate/")[1]
                self._json(200, generate_payments(k, card_id))
                return
            if p == "/api/payments/submit":
                k = self._require(roles=[ROLE_ADMIN, "付款审批人", "台账维护人"])
                if not k:
                    return
                self._json(200, W.submit_payment(sys.modules[__name__], k, body.get("record_ids") or []))
                return
            if p.startswith("/api/payments/approve/"):
                k = self._require(roles=[ROLE_ADMIN, "付款审批人"])
                if not k:
                    return
                card_id = p.split("/api/payments/approve/")[1]
                self._json(200, approve_payments(k, card_id, str(body.get("意见") or "").strip()))
                return
            if p.startswith("/api/payments/reject/"):
                k = self._require(roles=[ROLE_ADMIN, "付款审批人"])
                if not k:
                    return
                card_id = p.split("/api/payments/reject/")[1]
                self._json(200, reject_payments(k, card_id, str(body.get("意见") or "").strip()))
                return
            if p.startswith("/api/payments/record/"):
                k = self._require(roles=[ROLE_ADMIN, "资金管理处"])
                if not k:
                    return
                payment_id = p.split("/api/payments/record/")[1]
                self._json(200, W.record_payment(sys.modules[__name__], k, payment_id, body))
                return
            if p.startswith("/api/vouchers/generate/"):
                k = self._require(roles=[ROLE_ADMIN, "年报项目组"])
                if not k:
                    return
                card_id = p.split("/api/vouchers/generate/")[1]
                self._json(200, generate_vouchers(k, card_id, body.get("period")))
                return
            if p.startswith("/api/vouchers/event/"):
                k = self._require(roles=[ROLE_ADMIN, "年报项目组"])
                if not k:
                    return
                card_id = p.split("/api/vouchers/event/")[1]
                self._json(200, generate_event_vouchers(k, card_id, body.get("event_type", ""), body.get("params") or {}))
                return
            if p.startswith("/api/master/") and p.endswith("/import"):
                k = self._require(roles=[ROLE_ADMIN, "IT管理员"])
                if not k:
                    return
                mtype = p.split("/api/master/")[1].split("/import")[0]
                self._json(200, import_master(k, mtype, body.get("rows") or []))
                return
            if p.startswith("/api/master/") and p.endswith("/add"):
                k = self._require(roles=[ROLE_ADMIN, "IT管理员"])
                if not k:
                    return
                mtype = p.split("/api/master/")[1].split("/add")[0]
                self._json(200, add_master(k, mtype, body))
                return
            self._json(404, {"ok": False, "error": "not found"})
        except RuntimeError as e:
            self._json(502, {"ok": False, "error": str(e)[:400]})
        except Exception as e:
            self._json(500, {"ok": False, "error": str(e)[:400]})

    def _post_card(self, cur, p, body):
        parts = p.split("/")
        card_id = parts[3]
        action = parts[4] if len(parts) > 4 else None
        if action == 'payment-draft':
            self._json(200, save_payment_draft(cur, card_id, body.get('rows')))
            return
        if action == "validate":
            k = self._require(roles=[ROLE_ADMIN, ROLE_KEEPER, ROLE_APPROVER])
            if not k:
                return
            self._json(200, validate_card(card_id))
            return
        if action == "ocr-confirm":
            k = self._require(roles=[ROLE_ADMIN, ROLE_KEEPER])
            if not k:
                return
            self._json(200, ocr_confirm(k, card_id,
                                        str(body.get("status") or "人工已确认").strip()))
            return
        if action == "overview":
            k = self._require(roles=[ROLE_ADMIN, ROLE_KEEPER])
            if not k:
                return
            segs = body.get("segments") or []
            self._json(200, save_amount_overview(k, card_id, segs))
            return
        if action == "generate":
            k = self._require(roles=[ROLE_ADMIN, ROLE_KEEPER])
            if not k:
                return
            self._json(200, generate_ledgers(k, card_id))
            return
        if action == "submit":
            k = self._require(roles=[ROLE_ADMIN, ROLE_KEEPER])
            if not k:
                return
            self._json(200, W.submit(sys.modules[__name__], k, card_id))
            return
        if action == "approve":
            k = self._require(roles=[ROLE_ADMIN, ROLE_APPROVER])
            if not k:
                return
            self._json(200, W.decide(sys.modules[__name__], k, card_id, "通过", str(body.get("意见") or "").strip()))
            return
        if action == "reject":
            k = self._require(roles=[ROLE_ADMIN, ROLE_APPROVER])
            if not k:
                return
            self._json(200, W.decide(sys.modules[__name__], k, card_id, "退回", str(body.get("意见") or "").strip()))
            return
        if action == "withdraw":
            k = self._require()
            if not k:
                return
            self._json(200, W.withdraw(sys.modules[__name__], k, card_id, str(body.get("意见") or "提交人撤回").strip()))
            return
        if action == "delete":
            k = self._require(roles=[ROLE_ADMIN, ROLE_KEEPER])
            if not k:
                return
            self._json(200, delete_card(k, card_id))
            return
        if action == "basic":
            k = self._require(roles=[ROLE_ADMIN, ROLE_KEEPER])
            if not k:
                return
            self._json(200, save_card_basic(k, card_id, body))
            return
        if action == "ledger-adjust":
            k = self._require(roles=[ROLE_ADMIN, ROLE_KEEPER])
            if not k:
                return
            self._json(200, save_ledger_adjust(k, card_id, body.get("rows") or []))
            return
        self._json(404, {"ok": False, "error": "not found"})

    def _ai_scan(self, cur, body):
        """OpenAI-compatible multimodal relay proxy; the key is never persisted or logged."""
        endpoint = str(body.get('endpoint') or '').strip()
        model = str(body.get('model') or '').strip()
        api_key = str(body.get('api_key') or '').strip()
        if not endpoint.startswith(('https://', 'http://')):
            return self._json(400, {'ok': False, 'error': '模型接口地址无效'})
        if not model or not api_key:
            return self._json(400, {'ok': False, 'error': '请先填写模型名称和 API Key'})
        if len(api_key) > 300 or len(model) > 120:
            return self._json(400, {'ok': False, 'error': '模型配置长度无效'})
        source_text = str(body.get('text') or '')[:120000]
        image_data = str(body.get('image_data') or '')
        if len(image_data) > 18 * 1024 * 1024:
            return self._json(400, {'ok': False, 'error': '图片内容过大，请使用本地 OCR 或压缩图片'})
        instruction = ('你是财务租赁合同录入助手。只返回一个合法 JSON 对象，不要 Markdown，不要解释。'
                       '字段可包含：合同编码、卡片名称、费用类型、承租方、出租方、实际付款方、实际收款方、'
                       '租赁地址、租赁起始日、租赁终止日、建筑面积、实用面积、payment_table、segments、oneoff。'
                       '无法确认的字段返回空字符串；金额必须是数字；不要猜测。')
        content = [{'type': 'text', 'text': instruction + ('\n合同 OCR 原文：\n' + source_text if source_text else '')}]
        if image_data.startswith('data:image/'):
            content.append({'type': 'image_url', 'image_url': {'url': image_data}})
        payload = json.dumps({'model': model, 'temperature': 0, 'messages': [{'role': 'user', 'content': content}]}, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(endpoint, data=payload, method='POST', headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + api_key})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                upstream = json.loads(resp.read(2 * 1024 * 1024).decode('utf-8', 'replace'))
            msg = (((upstream.get('choices') or [{}])[0].get('message') or {}).get('content'))
            if isinstance(msg, list): msg = ''.join(x.get('text', '') for x in msg if isinstance(x, dict))
            if not isinstance(msg, str) or not msg.strip(): return self._json(502, {'ok': False, 'error': '模型未返回可解析内容'})
            cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', msg.strip(), flags=re.I).strip()
            try: parsed = json.loads(cleaned)
            except Exception: parsed = None
            return self._json(200, {'ok': True, 'model': model, 'result': parsed, 'raw': msg[:120000]})
        except urllib.error.HTTPError as e:
            return self._json(502, {'ok': False, 'error': f'模型接口返回 HTTP {e.code}'})
        except socket.gaierror:
            return self._json(502, {'ok': False, 'error': '模型接口域名无法解析。请确认 WorkBuddy 运行环境已接入公司内网 DNS，或改用可从该环境访问的网关地址'})
        except Exception as e:
            return self._json(502, {'ok': False, 'error': '模型接口调用失败：' + str(e)[:180]})

    def _upload(self, k, body):
        """接收 base64 文件，走 importer 云端 OCR 抽取字段。支持 PDF/DOCX/XLSX/CSV/TXT/图片。"""
        fname = str(body.get("filename") or "scan.png")
        b64 = str(body.get("data") or "")
        if not b64:
            self._json(400, {"ok": False, "error": "缺少文件内容"})
            return
        try:
            raw = base64.b64decode(b64.split(",")[-1])
        except Exception:
            self._json(400, {"ok": False, "error": "文件内容不是合法 base64"})
            return
        max_mb = int(os.environ.get('ZL_UPLOAD_MAX_MB', '50'))
        if len(raw) > max_mb * 1024 * 1024:
            self._json(400, {"ok": False, "error": f"文件超过 {max_mb}MB"})
            return
        ext = os.path.splitext(fname)[1].lower() or ".png"
        if ext not in (".png", ".jpg", ".jpeg", ".pdf", ".docx", ".xlsx", ".csv", ".txt"):
            self._json(400, {"ok": False, "error": "仅支持 PDF/DOCX/XLSX/CSV/TXT/JPG/PNG"})
            return
        # 优先走 importer（云端 OCR，无需落盘）
        try:
            import importer
            result = importer.parse({"filename": fname, "content": base64.b64encode(raw).decode()})
        except Exception as e:
            self._json(400, {"ok": False, "error": "合同解析失败：" + str(e)[:200]})
            return
        if isinstance(result, dict) and result.get("error"):
            self._json(400, {"ok": False, "error": result["error"]})
            return
        fields = result.get("fields", {}) if isinstance(result, dict) else {}
        text = result.get("text", "") if isinstance(result, dict) else ""
        segments = result.get("segments", []) if isinstance(result, dict) else []
        payment_table = result.get("payment_table", []) if isinstance(result, dict) else []
        sharing = result.get("sharing", []) if isinstance(result, dict) else []
        oneoff_fees = result.get("oneoff_fees", []) if isinstance(result, dict) else []
        # importer 字段是英文 key，统一转中文（前端按这些名称回填表单）
        fmap = {
            "contract_no": "合同编号", "oa_no": "OA单号", "lessor": "出租方", "lessee": "承租方",
            "lessor_id": "出租方信用代码", "lessee_id": "承租方信用代码",
            "lessor_bank": "出租方开户行", "lessee_bank": "承租方开户行",
            "lessor_account": "出租方银行账号", "lessee_account": "承租方银行账号",
            "start_date": "租赁起始日", "end_date": "租赁终止日", "address": "租赁地址",
            "property_addr": "租赁物坐落", "sign_place": "签订地点",
            "monthly_amount": "月租金", "monthly_fee": "月管理费", "deposit": "保证金",
            "tax_rate": "税率", "tax_incl": "是否含税", "free_rent": "免租期",
            "department": "提单部门", "applicant": "申请人",
            "area": "计租面积", "escalation": "递增约定", "settlement": "结算方式",
            "province": "省级", "city": "市级", "district": "区镇",
        }
        cn_fields = {fmap.get(k, k): v for k, v in fields.items()}
        if not segments:
            segments = _extract_segments(text)
        segs = ocr_to_overview(None, {"segments": segments})
        # 同一字段出现多个候选值时不擅自选择，回传冲突交前端提示人工确认
        raw_conf = result.get("conflicts", {}) if isinstance(result, dict) else {}
        cn_conf = {}
        for k, vals in (raw_conf or {}).items():
            cn_conf[fmap.get(k, k)] = vals
        self._json(200, {"ok": True, "fields": cn_fields, "segments": segs,
                         "payment_table": payment_table, "sharing": sharing, "oneoff_fees": oneoff_fees,
                         "conflicts": cn_conf, "text": text})

    def _serve_index(self):
        f = HERE / "index.html"
        if not f.exists():
            self._json(404, {"ok": False, "error": "missing index.html"})
            return
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    tok = os.environ.get("ZL_TOKEN") or os.environ.get("RENTALS_LIBRARY_TOKEN") or ""
    if os.environ.get('ZL_STORAGE') == 'local':
        if not os.environ.get('ZL_DATA_DIR') or not os.environ.get('ZL_SESSION_KEY'):
            raise RuntimeError('Local mode requires ZL_DATA_DIR and ZL_SESSION_KEY')
    elif lc.is_sandbox():
        if tok:
            lc.set_token(tok)
        print("沙箱模式：走 auth-proxy 访问资料库", file=sys.stderr)
    else:
        if not tok:
            print("请设置 ZL_TOKEN 环境变量（资料库 token）", file=sys.stderr)
            sys.exit(1)
        lc.set_token(tok)
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", "8700"))
    host = os.environ.get("ZL_HOST", "0.0.0.0")
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"ZL LightApp listening on http://{host}:{port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()


