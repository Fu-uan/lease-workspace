# -*- coding: utf-8 -*-
"""
HTTP 层端到端验证：启动真实服务，用三角色登录走完整审批闭环，并验证越权拦截(403)。
用法：ZL_TOKEN=<token> python test_http.py
"""
import json
import os
import threading
import time
import urllib.request

import library_client as lc
TOK = os.environ.get("ZL_TOKEN") or ""
lc.set_token(TOK)

# 起真实服务
import backend as B
PORT = 8799
B.lc.set_token(TOK)
srv = B.ThreadingHTTPServer(("127.0.0.1", PORT), B.Handler)
th = threading.Thread(target=srv.serve_forever, daemon=True)
th.start()
BASE = f"http://127.0.0.1:{PORT}"


def api(method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def login(acct, pwd):
    s, j = api("POST", "/api/login", {"账号": acct, "密码": pwd})
    assert s == 200 and j.get("ok"), f"登录失败 {acct}: {j}"
    return j["token"]


print("=" * 70)
print("登录三角色")
tk_keeper = login("keeper", "keeper123")
tk_approver = login("approver", "approver123")
tk_admin = login("admin", "admin123")
print("  keeper(维护人) ✓  approver(审批人) ✓  admin(管理员) ✓")

print("\n场景1：维护人新建合同 -> 草稿")
s, j = api("POST", "/api/contracts", {"合同编号": "HT-HTTP-001", "承租方": "深圳星辰科技",
    "房产单元": "A座-101", "开始日期": "2026-10-01", "结束日期": "2027-09-30",
    "月租金": 45000, "押金": 90000, "付款日": 5}, token=tk_keeper)
assert s == 200 and j["ok"] and j["state"] == "草稿", j
print("  ✓ 草稿")

print("\n场景2：维护人提交 -> 待审")
s, j = api("POST", "/api/contracts/submit", {"合同编号": "HT-HTTP-001"}, token=tk_keeper)
assert s == 200 and j["ok"] and j["state"] == "待审", j
print("  ✓ 待审")

print("\n场景3：越权拦截 —— 维护人调审批接口(应403)")
s, j = api("POST", "/api/contracts/approve", {"合同编号": "HT-HTTP-001", "意见": "越权"},
           token=tk_keeper)
assert s == 403, f"维护人应被403，实际 {s}: {j}"
print(f"  ✓ 403 {j['error']}")

print("\n场景4：审批人通过 -> 通过")
s, j = api("POST", "/api/contracts/approve", {"合同编号": "HT-HTTP-001", "意见": "租金合理，同意"},
           token=tk_approver)
assert s == 200 and j["ok"] and j["state"] == "通过", j
print("  ✓ 通过")

print("\n场景5：防重复 —— 已通过再审批(应被状态机拒绝)")
s, j = api("POST", "/api/contracts/approve", {"合同编号": "HT-HTTP-001", "意见": ""},
           token=tk_approver)
assert not j.get("ok"), "已通过不应再通过"
print(f"  ✓ 拒绝: {j['error']}")

print("\n场景6：待办列表(审批人视角)应含已通过合同(提交后不再待审)")
s, j = api("GET", "/api/todo", token=tk_approver)
codes = [c["合同编号"] for c in j.get("todo", [])]
assert "HT-HTTP-001" not in codes, "已通过合同不应在待审中"
print("  ✓ 待审列表不含已通过合同")

print("\n场景7：管理员退回 -> 退回；维护人重提 -> 待审")
# 先造一条待审合同
api("POST", "/api/contracts", {"合同编号": "HT-HTTP-002", "承租方": "北京云帆", "房产单元": "B-201",
    "月租金": 30000, "押金": 60000, "付款日": 10}, token=tk_keeper)
api("POST", "/api/contracts/submit", {"合同编号": "HT-HTTP-002"}, token=tk_keeper)
s, j = api("POST", "/api/contracts/reject", {"合同编号": "HT-HTTP-002", "意见": "押金比例不符"},
           token=tk_admin)
assert s == 200 and j["ok"] and j["state"] == "退回", j
print("  ✓ 退回")
s, j = api("POST", "/api/contracts/resubmit", {"合同编号": "HT-HTTP-002", "意见": "已修正"},
           token=tk_keeper)
assert s == 200 and j["ok"] and j["state"] == "待审", j
print("  ✓ 重提 -> 待审")

print("\n场景8：审批记录已写库")
s, j = api("GET", "/api/approvals?code=HT-HTTP-001", token=tk_admin)
for x in j.get("approvals", []):
    print("   ", x.get("操作时间"), x.get("动作"), x.get("操作人"), "|", x.get("意见"))
assert len(j.get("approvals", [])) >= 2, "应有提交+通过两条记录"

print("\n✅ HTTP 层全链路 + 越权拦截 + 防重复 验证通过")

# 清理
srv.shutdown()
def delrec(db, ids):
    if not ids: return
    import urllib.request
    req = urllib.request.Request(lc.BASE + "/space/api/agent/v1/batch-delete-records",
        data=json.dumps({"databaseId": db, "recordIds": ids}).encode(),
        headers={"Content-Type": "application/json", "X-Skill-Token": lc.get_token()})
    urllib.request.urlopen(req, timeout=25)
def q(db, code):
    res = lc.query(db, filt={"property": {"property": "合同编号", "text": {"equals": code}}})
    return [x["record_id"] for x in res]
for code in ("HT-HTTP-001", "HT-HTTP-002"):
    delrec(B.APPROVALS_DB, q(B.APPROVALS_DB, code))
    delrec(B.CONTRACTS_DB, q(B.CONTRACTS_DB, code))
print("清理完成")
