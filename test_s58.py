# -*- coding: utf-8 -*-
"""阶段5.5-5.8 综合E2E：凭证初始化+终止/修改事件 + 账龄/余额/分摊报表。"""
import sys, os, json, time
from http.server import ThreadingHTTPServer

sys.path.insert(0, r"D:/workbuddy/财务租赁/租赁线上化LightApp")
import backend as B
import library_client as lc

PORT = 8755
CODE = "HT-E2E-S58-001"


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
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode())


def main():
    lc.set_token(os.environ["LIB_TOKEN"])
    srv = start_server()
    time.sleep(0.5)
    logs = []
    try:
        r = http("POST", "/api/login", {"账号": "keeper", "密码": "keeper123"})
        ktok = r["token"]
        r = http("POST", "/api/login", {"账号": "admin", "密码": "admin123"})
        atok = r["token"]

        # 建卡+概览+生成+提交+通过
        r = http("POST", "/api/cards", {"合同编码": CODE, "卡片名称": "S58测试卡", "费用类型": "租金",
                                        "承租方": "S58", "租赁起始日": "2025-01-01", "租赁终止日": "2025-12-31",
                                        "计费算法": "整月", "结算方式": "预付下月", "是否含税": "含税",
                                        "是否IFRS": "是", "IFRS折现率": "0.003658333"}, ktok)
        cid = r["card_id"]
        http("POST", "/api/cards/%s/overview" % cid, {"segments": [
            {"租赁起始日": "2025-01-01", "租赁终止日": "2025-12-31", "月份数": "12", "月金额": "8000"}]}, ktok)
        http("POST", "/api/cards/%s/generate" % cid, {}, ktok)
        http("POST", "/api/cards/%s/submit" % cid, {}, ktok)
        http("POST", "/api/cards/%s/approve" % cid, {"意见": "同意"}, atok)
        logs.append("1 卡片审批通过")

        # 2 成本分摊
        http("POST", "/api/shares/" + cid, {"shares": [
            {"成本中心": "A部门", "分摊比例": 0.6, "生效日": "2025-01-01", "倒挤": False},
            {"成本中心": "B部门", "分摊比例": 0.4, "生效日": "2025-01-01", "倒挤": True}]}, ktok)
        logs.append("2 成本分摊 OK")

        # 3 凭证生成（含IFRS新增初始化）
        r = http("POST", "/api/vouchers/generate/" + cid, {}, atok)
        assert r["ok"] and r["vouchers"] > 0, ("凭证生成失败", r)
        # 验证含 IFRS新增 类型
        vs = http("GET", "/api/vouchers?card=" + cid, token=atok)["vouchers"]
        types = set(v["凭证类型"] for v in vs)
        assert "IFRS新增" in types, ("缺IFRS新增凭证", types)
        assert "IFRS折旧" in types and "IFRS利息" in types and "IFRS实付租金" in types and "冲销合同法计提" in types, types
        logs.append("3 凭证生成含 IFRS新增/折旧/利息/实付/冲销 共 %d 行" % len(vs))

        # 4 租赁修改事件凭证
        r = http("POST", "/api/vouchers/event/" + cid, {"event_type": "租赁修改",
            "params": {"修改日": "2025-06-01", "原值变动": 1000, "累计折旧变动": 200,
                       "未确认融资费用变动": 300, "租赁付款额变动": 1100}}, atok)
        assert r["ok"], ("租赁修改凭证失败", r)
        logs.append("4 租赁修改事件凭证 OK (%d 行)" % r["vouchers"])

        # 5 终止事件凭证
        r = http("POST", "/api/vouchers/event/" + cid, {"event_type": "终止",
            "params": {"终止日": "2025-12-31", "原值": 96000, "累计折旧": 96000,
                       "未确认融资费用": 0, "租赁付款额余额": 0, "处置损益": 500}}, atok)
        assert r["ok"], ("终止凭证失败", r)
        logs.append("5 终止处置事件凭证 OK (%d 行)" % r["vouchers"])

        # 6 账龄报表
        r = http("GET", "/api/reports/aging", token=atok)
        assert len(r["rows"]) > 0, "账龄报表空"
        assert all("到期年限" in x for x in r["rows"]), "账龄缺分桶"
        logs.append("6 账龄分析报表 OK (%d 行)" % len(r["rows"]))

        # 7 余额报表
        r = http("GET", "/api/reports/balance", token=atok)
        assert len(r["rows"]) == 1 and "资产期末余额" in r["rows"][0], "余额报表失败"
        logs.append("7 余额明细报表 OK")

        # 8 分摊报表
        r = http("GET", "/api/reports/sharing", token=atok)
        assert len(r["rows"]) == 2, "分摊报表失败"
        logs.append("8 分摊明细报表 OK (2 行)")

        print("\n".join(logs))
        print("阶段5.5-5.8 综合 E2E 全部通过 ✅")
    finally:
        srv.shutdown()


if __name__ == "__main__":
    main()