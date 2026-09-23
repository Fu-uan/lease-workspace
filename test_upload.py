# -*- coding: utf-8 -*-
"""阶段5.5 E2E：上传扫描件 → OCR → 字段草稿回填（真实库登录后调用 /api/upload）。"""
import sys, os, base64, json, time
from http.server import ThreadingHTTPServer

sys.path.insert(0, r"D:/workbuddy/财务租赁/租赁线上化LightApp")
import backend as B
import library_client as lc

PORT = 8744


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
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def main():
    lc.set_token(os.environ["LIB_TOKEN"])
    srv = start_server()
    time.sleep(0.5)
    try:
        r = http("POST", "/api/login", {"账号": "keeper", "密码": "keeper123"})
        ktok = r["token"]
        # 读示例扫描件
        scan = r"D:/workbuddy/财务租赁/示例合同扫描件.png"
        data = base64.b64encode(open(scan, "rb").read()).decode()
        r = http("POST", "/api/upload", {"filename": "示例合同扫描件.png", "data": data}, ktok)
        print("upload ok:", r.get("ok"), "| error:", r.get("error", ""))
        print("fields:", json.dumps(r.get("fields", {}), ensure_ascii=False))
        print("segments:", json.dumps(r.get("segments", []), ensure_ascii=False))
        assert r.get("ok") and r.get("fields", {}).get("合同编号") == "HT202500855", "OCR字段抽取不完整"
        print("阶段5.5 上传OCR E2E 通过 ✅")
    finally:
        srv.shutdown()


if __name__ == "__main__":
    main()