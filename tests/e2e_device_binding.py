"""端到端验证：网络绑定全链路（TestClient 模拟用户/管理员，XFF 模拟不同家庭网络）

场景覆盖（trust_proxy_header=1 + X-Forwarded-For 模拟出口 IP）：
 1. 管理员生成卡密（max_devices=1）并开启 device_binding_enabled
 2. 家里登录 → 绑定家庭网络；同 /24 的第二台设备/浏览器登录 → 共存不顶号
 3. token 拿到别的网络用 → 401「网络环境已变更」；回原网络自动恢复
 4. 别人家登录 → 顶掉家庭网络全部会话（401「已在其他网络登录」）
 5. 管理端绑定列表 / 解绑 → 被解绑网络的 token 401
 6. 关闭开关 → 行为回到历史版本：登录不落网络键，会话不校验
"""

import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="dvb_e2e_")
os.environ["DATA_DIR"] = _TMP

from fastapi.testclient import TestClient  # noqa: E402
from app import app  # noqa: E402


def parse_captcha(svg_uri: str) -> str:
    """从验证码 SVG（data URI）里抠出 4 位字符（<text ...>CH</text>，按写入顺序）"""
    import base64
    svg = base64.b64decode(svg_uri.split(",", 1)[1]).decode("utf-8")
    chars = re.findall(r'<text [^>]*>([A-Za-z0-9])</text>', svg)
    assert len(chars) == 4, f"captcha parse failed: {chars}"
    return "".join(chars)


def do_login(client, code, client_type="web", headers=None):
    """走完整登录（含验证码），返回 (status, json)"""
    cap = client.get("/api/auth/captcha", headers=headers or {}).json()
    body = {"code": code, "captchaId": cap["captchaId"], "captcha": parse_captcha(cap["svg"]), "client": client_type}
    r = client.post("/api/auth/login", json=body, headers=headers or {})
    return r.status_code, r.json()


def me(client, token, extra_headers=None):
    headers = {"Authorization": f"Bearer {token}"}
    if extra_headers:
        headers.update(extra_headers)
    r = client.get("/api/auth/me", headers=headers)
    return r.status_code, (r.json() if r.content else {})


def main():
    # client 指定局域网来源 IP：admin_lan_only 中间件默认仅放行私网 IP
    with TestClient(app, client=("192.168.1.100", 54321)) as client:
        # ── 管理员登录 + 生成卡密 ──
        r = client.post("/api/admin/login", json={"username": "admin", "password": "admin123"})
        assert r.json().get("success"), r.text
        admin_token = {"Authorization": "Bearer " + r.json()["token"]}

        r = client.post("/api/admin/cards/generate", headers=admin_token, json={
            "count": 1, "expiry_type": "fixed", "expires_at": "2099-01-01T00:00:00+00:00", "max_devices": 1,
        })
        assert r.json().get("success"), r.text
        card_code = r.json()["codes"][0]

        # ── 开启网络绑定 + 信任 XFF（模拟公网来源）──
        r = client.put("/api/admin/config", headers=admin_token, json={"config": {
            "device_binding_enabled": "1", "trust_proxy_header": "1",
        }})
        assert r.json().get("success"), r.text

        home = {"X-Forwarded-For": "23.5.6.10"}       # 家里（公网 /24: 23.5.6.0/24）
        home2 = {"X-Forwarded-For": "23.5.6.200"}     # 家里另一台设备（同 /24 → 同一网络）
        other = {"X-Forwarded-For": "98.7.6.5"}       # 别人家（另一个公网网段）

        # 1) 家里 Chrome 网页登录
        s1, j1 = do_login(client, card_code, "web", headers=home)
        assert s1 == 200 and j1["success"], (s1, j1)
        tok_home1 = j1["token"]

        # 2) 家里插件 / Edge 再登录（同 /24）→ 不顶号，会话共存
        s2, j2 = do_login(client, card_code, "extension", headers=home2)
        assert s2 == 200 and j2["success"], (s2, j2)
        tok_home2 = j2["token"]
        assert me(client, tok_home1, home)[0] == 200      # 第一个浏览器仍在线
        assert me(client, tok_home2, home2)[0] == 200     # 第二个也在线
        # 插件轮询接口同样可用
        rb = client.get("/api/extension/tasks", headers={"Authorization": f"Bearer {tok_home2}", **home2})
        assert rb.status_code == 200 and rb.json()["success"], rb.text

        # 3) token 拿到别的网络用 → 401 网络环境已变更（防外带）；回家自动恢复
        code_t, body_t = me(client, tok_home1, other)
        assert code_t == 401 and "网络环境已变更" in body_t.get("detail", ""), (code_t, body_t)
        assert me(client, tok_home1, home)[0] == 200

        # 4) 别人家登录 → 顶掉家庭网络的全部会话
        s3, j3 = do_login(client, card_code, "web", headers=other)
        assert s3 == 200 and j3["success"], (s3, j3)
        tok_other = j3["token"]
        code_h1, body_h1 = me(client, tok_home1, home)
        assert code_h1 == 401 and "其他网络" in body_h1.get("detail", ""), (code_h1, body_h1)
        code_h2, _ = me(client, tok_home2, home2)
        assert code_h2 == 401
        assert me(client, tok_other, other)[0] == 200     # 新网络正常

        # 5) 管理端绑定列表 / 解绑
        r = client.get("/api/admin/cards/1/devices", headers=admin_token)
        j = r.json()
        assert j["success"] and j["binding_enabled"] is True, j
        nets = [b["device_id"] for b in j["bindings"]]
        assert nets == ["98.7.6.0/24"], nets              # 家庭网段已被顶掉，只剩别人家

        r = client.post("/api/admin/cards/1/devices/unbind", headers=admin_token,
                        json={"device_id": "98.7.6.0/24"})
        assert r.json().get("success"), r.text
        code_o, body_o = me(client, tok_other, other)
        assert code_o == 401 and "其他网络" in body_o.get("detail", ""), (code_o, body_o)

        # 6) 关闭总开关 → 完全恢复历史行为：登录不落网络键、会话不校验
        r = client.put("/api/admin/config", headers=admin_token, json={"config": {
            "device_binding_enabled": "0", "trust_proxy_header": "0",
        }})
        assert r.json().get("success"), r.text
        sg, jg = do_login(client, card_code, "web")
        assert sg == 200 and jg["success"], (sg, jg)
        assert me(client, jg["token"])[0] == 200

        # ── 平滑过渡回归：关闭期登录 → 重新开启 → 存量会话不受影响 ──
        # （关闭期不落网络键 → 开启后按历史免绑定会话放行，至自然重登录）
        r = client.put("/api/admin/config", headers=admin_token, json={"config": {
            "device_binding_enabled": "1", "trust_proxy_header": "1",
        }})
        assert r.json().get("success")
        assert me(client, jg["token"], home)[0] == 200    # 免绑定会话正常使用
        # 免绑定会话短窗口内跨公网网段使用 → 触发 token 多网段风控兜底强制下线
        code_r, body_r = me(client, jg["token"], other)
        assert code_r == 401 and "多个网络环境" in body_r.get("detail", ""), (code_r, body_r)
        # 新登录正常走绑定
        si, ji = do_login(client, card_code, "web", headers=home)
        assert si == 200 and ji["success"], (si, ji)
        assert me(client, ji["token"], home)[0] == 200

        # 7) WebSocket 旁路（扫码轮询 /ws/poll）同样受网络校验约束
        with client.websocket_connect(f"/api/accounts/ws/poll?token={ji['token']}", headers=other) as ws:
            msg = ws.receive_json()
            assert msg.get("success") is False and "网络环境已变更" in msg.get("error", ""), msg
        with client.websocket_connect(f"/api/accounts/ws/poll?token={ji['token']}", headers=home) as ws:
            ws.send_json({"qr_id": ""})
            msg = ws.receive_json()   # 缺 qr_id 的业务报错 = 已通过鉴权与网络校验
            assert "qr_id" in msg.get("error", ""), msg

        print("E2E ALL PASS")


if __name__ == "__main__":
    main()
