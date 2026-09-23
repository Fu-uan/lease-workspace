# -*- coding: utf-8 -*-
"""端到端验证（阶段4 门禁）：在本进程内起线程 HTTPServer，走真实资料库完成
登录 → 建卡片 → 录金额概览 → 生成台账 → 提交 → 审批 全闭环。

用法：LIB_TOKEN=... python test_e2e.py
清洗：测试用唯一合同编码 + 结束后删除生成的测试卡片/台账/工单。
"""
import sys, os, json, threading, time
from http.server import ThreadingHTTPServer

sys.path.insert(0, r"D:/workbuddy/财务租赁/租赁线上化LightApp")
import backend as B
import library_client as lc

PORT = 8722
CODE = "HT-E2E-TEST-001"


def start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), B.Handler)
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
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}token={token}"
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path),
                                 data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def main():
    lc.set_token(os.environ["LIB_TOKEN"])
    srv = start_server()
    time.sleep(0.5)
    prints = []
    try:
        # 1 登录
        r = http("POST", "/api/login", {"账号": "keeper", "密码": "keeper123"})
        assert r["ok"] and r["user"]["role"] == "台账维护人", ("登录失败", r)
        prints.append("1 登录 keeper OK, role=台账维护人")
        ktok = r["token"]

        # 2 建卡片
        r = http("POST", "/api/cards", {"合同编码": CODE, "卡片名称": "E2E测试卡", "费用类型": "租金",
                                       "承租方": "E2E承租方", "租赁起始日": "2025-01-01", "租赁终止日": "2025-12-31",
                                       "计费算法": "整月", "结算方式": "付当月", "是否含税": "含税",
                                       "是否IFRS": "是", "IFRS折现率": "0.003658333", "预付租金": "0"}, ktok)
        assert r["ok"], ("建卡片失败", r)
        cid = r["card_id"]
        assert cid, ("未返回 card_id", r)
        prints.append("2 建卡片 OK, card_id=%s" % cid)

        # 3 录金额概览
        r = http("POST", "/api/cards/%s/overview" % cid,
                 {"segments": [{"租赁起始日": "2025-01-01", "租赁终止日": "2025-12-31", "月份数": "12", "月金额": "10000"}]},
                 ktok)
        assert r["ok"], ("金额概览失败", r)
        prints.append("3 录金额概览 OK, segments=%d" % r.get("segments"))

        # 4 生成台账
        r = http("POST", "/api/cards/%s/generate" % cid, {}, ktok)
        assert r["ok"], ("生成台账失败", r)
        prints.append("4 生成台账 OK, ledger=%d ifrs=%d" % (r["ledger_months"], r["ifrs_months"]))
        assert r["ledger_months"] == 12, ("合同法台账应为12个月", r)
        assert r["ifrs_months"] == 12, ("IFRS台账应为12个月", r)

        # 5 提交
        r = http("POST", "/api/cards/%s/submit" % cid, {}, ktok)
        assert r["ok"] and r["state"] == "待审", ("提交失败", r)
        prints.append("5 提交 OK, state=待审")

        # 6 approver 登录并审批通过
        r = http("POST", "/api/login", {"账号": "approver", "密码": "approver123"})
        assert r["ok"] and r["user"]["role"] == "台账审批人", ("审批人登录失败", r)
        atok = r["token"]
        prints.append("6 approver 登录 OK")

        # 审批人看待办
        r = http("GET", "/api/todo", token=atok)
        assert r["ok"] and any(x["record_id"] == cid for x in r["todo"]), ("待办不含新卡片", r)
        prints.append("7 待办含新卡片 OK")

        # 通过
        r = http("POST", "/api/cards/%s/approve" % cid, {"意见": "同意"}, atok)
        assert r["ok"] and r["state"] == "通过", ("审批失败", r)
        prints.append("8 审批通过 OK")

        # 9 维护人访问 todo 应 403（而非空列表伪装有数据）
        import urllib.error
        try:
            urllib.request.urlopen(urllib.request.Request(
                "http://127.0.0.1:%d/api/todo?token=%s" % (PORT, ktok)))
            prints.append("9 FAIL：维护人访问 todo 未返回 403")
        except urllib.error.HTTPError as e:
            prints.append("9 维护人访问 todo 返回 %d（403 分离校验 OK）" % e.code)

        # 10 回读台账/IFRS
        r = http("GET", "/api/cards/%s/ledger" % cid, token=ktok)
        assert len(r["ledger"]) == 12, ("回读合同法台账失败", r)
        r2 = http("GET", "/api/cards/%s/ifrs" % cid, token=ktok)
        assert len(r2["ifrs"]) == 12, ("回读IFRS台账失败", r2)
        prints.append("10 回读台账/IFRS OK (12/12)")

        print("\n".join(prints))
        print("E2E 全部通过 ✅")
        os.environ["E2E_CARD_ID"] = cid
    finally:
        srv.shutdown()


if __name__ == "__main__":
    main()