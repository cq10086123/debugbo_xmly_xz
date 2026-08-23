"""端到端验证：设备绑定全链路（TestClient 模拟网页/插件/管理员三端）

场景覆盖：
 1. 管理员生成卡密（max_devices=1）
 2. 开启 device_binding_enabled 后：
    a. 网页端设备A登录 → 成功
    b. 插件端设备B登录 → 成功（双席位共存，同机正常形态）
    c. 网页端设备C登录 → 成功且自动顶掉设备A
    d. 设备A的旧 token → 401「已在其他设备登录」
    e. 设备C的 token 换个设备头 → 401（device_mismatch 防拷贝）
    f. 未携带 deviceId 的旧客户端登录 → 403 提示升级
    g. 管理员解绑设备C → 其 token 401
 3. 关闭开关 → 行为回到历史版本：无 deviceId 也能登录，旧会话可用
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


def do_login(client, code, device_id=None, client_type="web"):
    """走完整登录（含验证码），返回 (status, json)"""
    cap = client.get("/api/auth/captcha").json()
    body = {"code": code, "captchaId": cap["captchaId"], "captcha": parse_captcha(cap["svg"]), "client": client_type}
    if device_id is not None:
        body["deviceId"] = device_id
    r = client.post("/api/auth/login", json=body)
    return r.status_code, r.json()


def me(client, token, device_id=None):
    headers = {"Authorization": f"Bearer {token}"}
    if device_id:
        headers["X-Device-Id"] = device_id
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

        # ── 开启设备绑定（保存后即时生效）──
        r = client.put("/api/admin/config", headers=admin_token, json={"config": {"device_binding_enabled": "1"}})
        assert r.json().get("success"), r.text

        # a. 网页端设备A登录
        sa, ja = do_login(client, card_code, "dev-AAAA", "web")
        assert sa == 200 and ja["success"], (sa, ja)
        tok_a = ja["token"]

        # b. 插件端设备B登录（另一席位，不冲突）
        sb, jb = do_login(client, card_code, "dev-BBBB", "extension")
        assert sb == 200 and jb["success"], (sb, jb)
        tok_b = jb["token"]
        # B 的插件轮询接口可用（带设备头）
        rb = client.get("/api/extension/tasks", headers={"Authorization": f"Bearer {tok_b}", "X-Device-Id": "dev-BBBB"})
        assert rb.status_code == 200 and rb.json()["success"], rb.text

        # c. 网页端设备C登录 → 顶掉设备A
        sc, jc = do_login(client, card_code, "dev-CCCC", "web")
        assert sc == 200 and jc["success"], (sc, jc)
        tok_c = jc["token"]

        # d. 设备A旧 token → 401 已在其他设备登录
        code_a, body_a = me(client, tok_a, "dev-AAAA")
        assert code_a == 401 and "其他设备" in body_a.get("detail", ""), (code_a, body_a)

        # 插件席 B 不受影响
        code_b, _ = me(client, tok_b, "dev-BBBB")
        assert code_b == 200

        # e. 设备C token 配错误设备头 → 401（防 token 拷贝）
        code_x, body_x = me(client, tok_c, "dev-FAKE")
        assert code_x == 401 and "登录环境" in body_x.get("detail", ""), (code_x, body_x)
        # 不带设备头同样拒绝（防删头绕过）
        code_y, _ = me(client, tok_c)
        assert code_y == 401

        # f. 旧客户端（不带 deviceId）登录 → 403 提示升级
        sf, jf = do_login(client, card_code, None, "web")
        assert sf == 403 and "更新" in jf.get("detail", ""), (sf, jf)

        # ── 管理端设备列表 / 解绑设备C ──
        r = client.get(f"/api/admin/cards/1/devices", headers=admin_token)
        j = r.json()
        assert j["success"] and j["binding_enabled"] is True, j
        web_devs = [d["device_id"] for d in j["devices"]["web"]]
        ext_devs = [d["device_id"] for d in j["devices"]["extension"]]
        assert web_devs == ["dev-CCCC"], web_devs          # A 已被顶掉
        assert ext_devs == ["dev-BBBB"], ext_devs

        r = client.post(f"/api/admin/cards/1/devices/unbind", headers=admin_token,
                        json={"device_id": "dev-CCCC", "client_type": "web"})
        assert r.json().get("success"), r.text
        code_c, body_c = me(client, tok_c, "dev-CCCC")
        assert code_c == 401 and "其他设备" in body_c.get("detail", ""), (code_c, body_c)

        # ── 关闭总开关 → 完全恢复历史行为 ──
        r = client.put("/api/admin/config", headers=admin_token, json={"config": {"device_binding_enabled": "0"}})
        assert r.json().get("success"), r.text
        sg, jg = do_login(client, card_code, None, "web")     # 不带 deviceId 也能登录
        assert sg == 200 and jg["success"], (sg, jg)
        code_g, _ = me(client, jg["token"])                    # 不带设备头也能用
        assert code_g == 200

        print("E2E ALL PASS")


if __name__ == "__main__":
    main()
