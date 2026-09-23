# -*- coding: utf-8 -*-
"""阶段5 E2E：付款清单生成/审批、凭证生成、报表、分摊（真实库）。"""
import sys, os, json, time
from http.server import ThreadingHTTPServer

sys.path.insert(0, r"D:/workbuddy/财务租赁/租赁线上化LightApp")
import backend as B
import library_client as lc

PORT = 8733
CODE = "HT-E2E-S5-001"


def start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), B.Handler)
    import threading
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def http(method, path, body=None, token=None):
    import urllib.request
    if body is not None:
        body = dict(body)
        if token:
            body["_token"] = token
        data = json.dumps(body, ensure_ascii=False).encode()
    else:
        data = None
    if token and body is None:
        path = path + ("&" if "?" in path else "?") + "token=" + token
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path),
                                 data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def main():
    lc.set_token(os.environ["LIB_TOKEN"])
    srv = start_server()
    time.sleep(0.5)
    logs = []
    cid = None
    try:
        # keeper 登录
        r = http("POST", "/api/login", {"账号": "keeper", "密码": "keeper123"})
        ktok = r["token"]
        # 建卡 + 概览 + 生成 + 提交
        r = http("POST", "/api/cards", {"合同编码": CODE, "卡片名称": "S5测试卡", "费用类型": "租金",
                                        "承租方": "S5承租方", "租赁起始日": "2025-01-01", "租赁终止日": "2025-12-31",
                                        "计费算法": "整月", "结算方式": "预付下月", "是否含税": "含税",
                                        "是否IFRS": "是", "IFRS折现率": "0.003658333"}, ktok)
        cid = r["card_id"]
        http("POST", "/api/cards/%s/overview" % cid, {"segments": [
            {"租赁起始日": "2025-01-01", "租赁终止日": "2025-12-31", "月份数": "12", "月金额": "5000"}]}, ktok)
        http("POST", "/api/cards/%s/generate" % cid, {}, ktok)
        http("POST", "/api/cards/%s/submit" % cid, {}, ktok)
        # approver 通过
        r = http("POST", "/api/login", {"账号": "approver", "密码": "approver123"})
        atok = r["token"]
        r = http("POST", "/api/cards/%s/approve" % cid, {"意见": "同意"}, atok)
        assert r["state"] == "通过"
        logs.append("1 卡片审批通过 OK")

        # 2 成本分摊（倒挤）
        r = http("POST", "/api/shares/" + cid, {"shares": [
            {"成本中心": "A部门", "分摊比例": 0.6, "生效日": "2025-01-01", "倒挤": False},
            {"成本中心": "B部门", "分摊比例": 0.4, "生效日": "2025-01-01", "倒挤": True}]}, ktok)
        assert r["ok"], ("分摊失败", r)
        logs.append("2 成本分摊保存 OK")

        # 3 生成付款清单
        r = http("POST", "/api/payments/generate/" + cid, {}, ktok)
        assert r["ok"] and r["payments"] == 12, ("付款清单生成失败", r)
        logs.append("3 付款清单生成 OK, 12笔")

        # 4 勾选付款明细提交审批
        pays = http("GET", "/api/payments?card=%s&unpaid=1" % cid, token=ktok)["payments"]
        rids = [p["record_id"] for p in pays]
        r = http("POST", "/api/payments/submit", {"record_ids": rids}, ktok)
        assert r["ok"], ("付款提交失败", r)
        logs.append("4 付款批次提交 OK, batch=%s" % r["batch"])

        # 5 付款审批（用管理员，或付款审批人角色）
        r = http("POST", "/api/login", {"账号": "admin", "密码": "admin123"})
        adm_tok = r["token"]
        r = http("POST", "/api/payments/approve/" + cid, {"意见": "同意付款"}, adm_tok)
        assert r["ok"] and r["state"] == "通过", ("付款审批失败", r)
        logs.append("5 付款审批通过 OK (admin)")

        # 6 凭证生成（年报项目组或管理员）
        r = http("POST", "/api/vouchers/generate/" + cid, {}, adm_tok)
        assert r["ok"] and r["vouchers"] > 0, ("凭证生成失败", r)
        logs.append("6 凭证生成 OK, %d 行" % r["vouchers"])

        # 7 报表
        r = http("GET", "/api/reports/address", token=adm_tok)
        assert len(r["rows"]) == 12, ("地址报表失败", len(r["rows"]))
        r = http("GET", "/api/reports/cashflow", token=adm_tok)
        assert len(r["rows"]) == 12, ("现金流报表失败", len(r["rows"]))
        r = http("GET", "/api/reports/ifrs", token=adm_tok)
        assert len(r["rows"]) == 12, ("IFRS报表失败", len(r["rows"]))
        logs.append("7 三类报表 OK (12/12/12)")

        print("\n".join(logs))
        print("阶段5 E2E 全部通过 ✅")
    finally:
        srv.shutdown()


if __name__ == "__main__":
    main()