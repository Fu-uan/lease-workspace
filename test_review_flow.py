# -*- coding: utf-8 -*-
"""复核编辑 + 联动重算 + 结算方式分组的真实库端到端验证。

覆盖：
  1 登录 → 建卡（付当季 + 并入IFRS）→ 录金额概览 → 生成台账
  2 台账勾稽破坏时拒绝保存（末月预付科目余额≠0）
  3 台账手调（降租为负 + 同步调整未付款）保存成功并联动重算 IFRS
  4 基础信息改动"结算方式"→ 下游台账作废（联动重算的失效链路）
  5 重新生成 → 提交 → 审批通过 → 生成付款清单（付当季应合并为 4 笔）
  6 账龄报表不包含披露日之前的期间
  7 清理测试数据

需要环境变量 LIB_TOKEN（资料库 token）。运行：
    LIB_TOKEN=op_xxx python test_review_flow.py
"""
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backend as B            # noqa: E402
import library_client as lc    # noqa: E402

PORT = 8797
CODE = "HT-REVIEW-TEST"
LOGS = []


def req(method, path, body=None, tok=None):
    data = None
    if body is not None:
        body = dict(body)
        body["_token"] = tok
        data = json.dumps(body, ensure_ascii=False).encode()
    elif tok:
        path += ("&" if "?" in path else "?") + "token=" + tok
    r = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), data=data,
                               method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=120) as x:
        return json.loads(x.read().decode())


def log(msg):
    LOGS.append(msg)
    print(msg, flush=True)      # 立即输出，便于中途失败时也能看到进度


def purge(card_id):
    """清理该卡片的全部关联数据。"""
    for db in ("amount_overview", "contract_ledger", "ifrs_detail", "cost_share",
               "pay_plan", "vouchers", "version_snapshots"):
        B._purge_by_card(B.TBL[db], card_id)
    for t in lc.query(B.TBL["approval_tickets"], filt={
            "property": {"property": "关联对象ID", "text": {"equals": card_id}}}):
        lc.delete(B.TBL["approval_tickets"], [t["record_id"]])
    lc.delete(B.TBL["cards"], [card_id])


def main():
    tok = os.environ.get("LIB_TOKEN", "")
    if not tok:
        print("缺少 LIB_TOKEN"); return 1
    lc.set_token(tok)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), B.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)
    card_id = None
    try:
        # 预清理
        for c in lc.query(B.TBL["cards"], filt={
                "property": {"property": "合同编码", "text": {"equals": CODE}}}):
            purge(c["record_id"])

        ktok = req("POST", "/api/login", {"账号": "keeper", "密码": "keeper123"})["token"]
        atok = req("POST", "/api/login", {"账号": "approver", "密码": "approver123"})["token"]
        log("1 登录 keeper/approver OK")

        c = req("POST", "/api/cards", {"合同编码": CODE, "卡片名称": "复核流程测试", "费用类型": "租金",
                                       "承租方": "测试承租方", "租赁地址": "湖南省长沙市岳麓区某路1号",
                                       "租赁起始日": "2025-01-01", "租赁终止日": "2025-12-31",
                                       "计费算法": "整月", "结算方式": "付当季",
                                       "约定付款日期": "10", "是否含税": "含税",
                                       "是否IFRS": "是", "IFRS折现率": "0.003658333"}, ktok)
        assert c["ok"], c
        card_id = c["card_id"]
        log("1 建卡 card_id=%s 结算方式=付当季" % card_id)

        ov = req("POST", "/api/cards/%s/overview" % card_id, {"segments": [
            {"租赁起始日": "2025-01-01", "租赁终止日": "2025-12-31", "月份数": 12,
             "月金额": 5000, "金额小计": 60000}]}, ktok)
        assert ov["ok"], ov
        g = req("POST", "/api/cards/%s/generate" % card_id, {}, ktok)
        assert g["ok"] and g["ledger_months"] == 12, g
        log("1 生成台账 合同法 %d 月 / IFRS %d 月" % (g["ledger_months"], g["ifrs_months"]))

        # 2 勾稽破坏应被拒绝（只降租、不同步调未付款 → 末月余额 -21790）
        bad = req("POST", "/api/cards/%s/ledger-adjust" % card_id,
                  {"rows": [{"月份": "2025-03", "降租金额": -21790, "未付款": 5000, "已付款": 0}]}, ktok)
        assert not bad["ok"] and "勾稽不通过" in bad["error"], bad
        log("2 勾稽破坏被拒绝：%s" % bad["error"][:60])

        # 3 合法手调：降租 -1000 且未付款同步减 1000 → 末月归零
        rows = [{"月份": "2025-03", "降租金额": -1000, "未付款": 4000, "已付款": 0}]
        ok = req("POST", "/api/cards/%s/ledger-adjust" % card_id, {"rows": rows}, ktok)
        assert ok["ok"] and abs(ok["tail_balance"]) < 0.01, ok
        assert ok["ifrs_months"] == 12, ok
        log("3 台账手调保存 OK，末月余额=%s，IFRS 重算 %d 月" % (ok["tail_balance"], ok["ifrs_months"]))
        led = lc.query(B.TBL["contract_ledger"], filt={
            "property": {"property": "关联卡片ID", "text": {"equals": card_id}}})
        m3 = [r for r in led if (B.val(r, "月份") or "")[:7] == "2025-03"][0]
        assert B.dnum(m3.get("降租金额")) == -1000 and B.dnum(m3.get("实付金额合计")) == 4000, m3
        log("3 2025-03 降租=-1000 实付金额合计=%s（=合同5000+降租-1000）" % B.dnum(m3.get("实付金额合计")))

        # 4 改结算方式 → 台账作废
        b = req("POST", "/api/cards/%s/basic" % card_id, {"结算方式": "付当季", "租赁地址": "湖南省长沙市岳麓区某路2号"}, ktok)
        assert b["ok"] and b["changed"] == ["租赁地址"], b
        log("4 非计算字段变更不作废：" + str(b["changed"]))
        b2 = req("POST", "/api/cards/%s/basic" % card_id, {"结算方式": "付半年"}, ktok)
        assert b2["ok"] and b2["invalidated"], b2
        left = lc.query(B.TBL["contract_ledger"], filt={
            "property": {"property": "关联卡片ID", "text": {"equals": card_id}}})
        assert len(left) == 0, "台账应已作废"
        log("4 结算方式变更 → 台账/IFRS 已作废（剩余台账 %d 行）" % len(left))

        # 5 重算并走完审批 + 付款清单分组
        req("POST", "/api/cards/%s/basic" % card_id, {"结算方式": "付当季"}, ktok)
        assert not req("POST", "/api/cards/%s/submit" % card_id, {}, ktok)["ok"], "无台账不应可提交"
        g2 = req("POST", "/api/cards/%s/generate" % card_id, {}, ktok)
        assert g2["ok"], g2
        s = req("POST", "/api/cards/%s/submit" % card_id, {}, ktok)
        assert s["ok"], s
        a = req("POST", "/api/cards/%s/approve" % card_id, {"意见": "同意"}, atok)
        assert a["ok"], a
        log("5 重新生成 → 提交 → 审批通过 OK")

        # 审批人不能审批自己提交的（分离校验）
        pay = req("POST", "/api/payments/generate/%s" % card_id, {}, ktok)
        assert pay["ok"], pay
        log("5 付款清单 %d 笔（付当季，12 个月应为 4 笔），周期=%s" % (pay["payments"], pay.get("cycle")))
        assert pay["payments"] == 4, pay
        plist = lc.query(B.TBL["pay_plan"], filt={
            "property": {"property": "关联卡片ID", "text": {"equals": card_id}}})
        periods = sorted(B.val(r, "费用所属期间") for r in plist)
        log("5 费用所属期间：" + " / ".join(periods))
        assert set(periods) == {"2025年1至3月", "2025年4至6月", "2025年7至9月", "2025年10至12月"}, periods

        # 6 账龄报表不含披露日之前的期间
        ag = req("GET", "/api/reports/aging?date=2025-12-31", tok=ktok)["rows"]
        mine = [r for r in ag if r["卡片"] == CODE]
        months = sorted(r["月份"] for r in mine)
        log("6 账龄（披露日 2025-12-31）包含期间：%s" % (months or "无"))
        assert all(m >= "2025-12" for m in months), months

        # 凭证生成限「年报项目组/管理员」——用维护人应被拒，用管理员应成功
        adm = req("POST", "/api/login", {"账号": "admin", "密码": "admin123"})["token"]
        try:
            req("POST", "/api/vouchers/generate/%s" % card_id, {}, ktok)
            raise AssertionError("维护人不应能生成凭证")
        except urllib.error.HTTPError as e:
            assert e.code == 403, e.code
        log("6 维护人生成凭证被拒（403）——角色隔离正确")
        v = req("POST", "/api/vouchers/generate/%s" % card_id, {}, adm)
        log("6 管理员生成凭证 %d 行" % v.get("vouchers", 0))
        assert v.get("vouchers", 0) > 0, v
    finally:
        if card_id:
            purge(card_id)
            log("7 测试数据已清理，剩余卡片 %d" % len(lc.query(B.TBL["cards"])))
        srv.shutdown()
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
