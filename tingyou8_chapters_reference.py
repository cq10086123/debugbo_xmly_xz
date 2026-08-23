# ═══════════════════════════════════════════════════════════════
# 【章节】听友FM (tingyou8) —— 整段复制粘贴到「章节」tab 即可
# 引擎契约（与 C:\Users\11754\Desktop\1\py 一致）：def parse(params)
#   params 含: bookId / page / page0 / size / count 及搜索结果透传字段
#   返回: list[dict]，每项必含 chapter_id、title（可选 order/duration 等）
#
# ⚠️ 注意：下方 BASE + "/album/detail" 的接口路径为占位推测，
#   搜索为 /apk/search 真实可用；章节/音频真实路径需你抓包确认后替换。
# ═══════════════════════════════════════════════════════════════
import requests
import json
import secrets
import base64
import hashlib
import struct
import warnings
from datetime import datetime

warnings.filterwarnings("ignore")

from Crypto.Cipher import AES, ChaCha20_Poly1305

# ── 加密配置（听友FM 固定）────────────────────────────────────
KEY = bytes.fromhex("ea9d9d4f9a983fe6f6382f29c7b46b8d6dc47abc6da36662e6ddff8c78902f65")
UA = "azybk_1.0.8(HBP-AL00,Android32)"
BASE = "https://azybk.tingyou8.vip/apk"

_MASK = 0xFFFFFFFF
_SBOX = [214, 144, 233, 254, 204, 225, 61, 183, 22, 182, 20, 194, 40, 251, 44, 5, 43, 103, 154, 118, 42, 190, 4, 195, 170, 68, 19, 38, 73, 134, 6, 153, 156, 66, 80, 244, 145, 239, 152, 122, 51, 84, 11, 67, 237, 207, 172, 98, 228, 179, 28, 169, 201, 8, 232, 149, 128, 223, 148, 250, 117, 143, 63, 166, 71, 7, 167, 252, 243, 115, 23, 186, 131, 89, 60, 25, 230, 133, 79, 168, 104, 107, 129, 178, 113, 100, 218, 139, 248, 235, 15, 75, 112, 86, 157, 53, 30, 36, 14, 94, 99, 88, 209, 162, 37, 34, 124, 59, 1, 33, 120, 135, 212, 0, 70, 87, 159, 211, 39, 82, 76, 54, 2, 231, 160, 196, 200, 158, 234, 191, 138, 210, 64, 199, 56, 181, 163, 247, 242, 206, 249, 97, 21, 161, 224, 174, 93, 164, 155, 52, 26, 85, 173, 147, 50, 48, 245, 140, 177, 227, 29, 246, 226, 46, 130, 102, 202, 96, 192, 41, 35, 171, 13, 83, 78, 111, 213, 219, 55, 69, 222, 253, 142, 47, 3, 255, 106, 114, 109, 108, 91, 81, 141, 27, 175, 146, 187, 221, 188, 127, 17, 217, 92, 65, 31, 16, 90, 216, 10, 193, 49, 136, 165, 205, 123, 189, 45, 116, 208, 18, 184, 229, 180, 176, 137, 105, 151, 74, 12, 150, 119, 126, 101, 185, 241, 9, 197, 110, 198, 132, 24, 240, 125, 236, 58, 220, 77, 32, 121, 238, 95, 62, 215, 203, 57, 72]
_RK = [21, 14, 7, 0, 49, 42, 35, 28, 77, 70, 63, 56, 105, 98, 91, 84, 133, 126, 119, 112, 161, 154, 147, 140, 189, 182, 175, 168, 217, 210, 203, 196, 245, 238, 231, 224, 17, 10, 3, 252, 45, 38, 31, 24, 73, 66, 59, 52, 101, 94, 87, 80, 129, 122, 115, 108, 157, 150, 143, 136, 185, 178, 171, 164, 213, 206, 199, 192, 241, 234, 227, 220, 13, 6, 255, 248, 41, 34, 27, 20, 69, 62, 55, 48, 97, 90, 83, 76, 125, 118, 111, 104, 153, 146, 139, 132, 181, 174, 167, 160, 209, 202, 195, 188, 237, 230, 223, 216, 9, 2, 251, 244, 37, 30, 23, 16, 65, 58, 51, 44, 93, 86, 79, 72, 121, 114, 107, 100]
_DEVICE_INPUT = "HONOR|HBP-AL00|HBP-AL00|1234567890|12|fp|"


def _seed_key(ts):
    seed = hashlib.sha256(b"fa317cd29b|" + ts.encode()).digest()
    return (struct.unpack_from(">I", seed, i * 4)[0] for i in range(4))


def _ext_key(d0, d1, d2, d3):
    v62, v63 = d1 ^ 0x56AA3350, d0 ^ 0xA3B1BAC6
    v65, v64 = d3 ^ 0xB27022DC, d2 ^ 0x677D9197
    ext = bytearray()
    for i in range(0, 128, 4):
        v67 = v65
        rk = struct.unpack_from("<I", bytes(_RK[i:i + 4]), 0)[0]
        v68 = v62 ^ v65 ^ v64 ^ rk
        hi, b2, b1 = v68 >> 24, (v68 >> 16) & 0xFF, (v68 >> 8) & 0xFF
        lo = _SBOX[v68 & 0xFF]
        b2n = (_SBOX[hi] << 24) | (_SBOX[b2] << 16) | (_SBOX[b1] << 8) | lo
        v65 = (v63 ^ ((lo << 24) & _MASK) ^ ((b2n << 10) & _MASK)
               ^ ((4 * b2n) & _MASK) ^ ((b2n << 18) & _MASK) ^ b2n) & _MASK
        ext += struct.pack("<I", v65)
        v63, v62, v64 = v62, v64, v67
    return bytes(ext)


def _enc_block(block, extkey):
    b = block
    v74 = (b[8] << 24) | (b[9] << 16) | (b[10] << 8) | b[11]
    v75 = (b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3]
    v76 = (b[4] << 24) | (b[5] << 16) | (b[6] << 8) | b[7]
    v77 = (b[12] << 24) | (b[13] << 16) | (b[14] << 8) | b[15]
    for i in range(0, 128, 4):
        v78, v79 = v76, v77
        v80 = struct.unpack_from("<I", extkey, i)[0]
        v81 = v74
        v82 = v76 ^ v74 ^ v79 ^ v80
        hi, b2, b1 = v82 >> 24, (v82 >> 16) & 0xFF, (v82 >> 8) & 0xFF
        lo = _SBOX[v82 & 0xFF]
        v86 = v75 ^ ((lo << 24) & _MASK)
        b2n = (_SBOX[hi] << 24) | (_SBOX[b2] << 16) | (_SBOX[b1] << 8) | lo
        v74, v76 = v79, v81
        v77 = (v86 ^ ((b2n << 10) & _MASK) ^ ((4 * b2n) & _MASK)
               ^ ((b2n << 18) & _MASK) ^ b2n) & _MASK
        v75 = v78
    out = bytearray(16)
    for j, v in enumerate((v77, v79, v81, v78)):
        out[j * 4] = (v >> 24) & 0xFF
        out[j * 4 + 1] = (v >> 16) & 0xFF
        out[j * 4 + 2] = (v >> 8) & 0xFF
        out[j * 4 + 3] = v & 0xFF
    return bytes(out)


def _gen_dfp():
    ts = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    raw = _DEVICE_INPUT.encode()
    n1 = (len(raw) + 15) & ~0xF
    pad = n1 - len(raw)
    padded = raw + bytes([pad]) * pad
    d0, d1, d2, d3 = _seed_key(ts)
    extkey = _ext_key(d0, d1, d2, d3)
    out = bytearray()
    for i in range(0, n1, 16):
        out += _enc_block(padded[i:i + 16], extkey)
    return "dfp=f-c29cd:f-" + base64.b64encode(bytes(out)).decode()


def _encrypt(plaintext: bytes) -> bytes:
    nonce = secrets.token_bytes(12)
    c = AES.new(KEY, AES.MODE_GCM, nonce=nonce)
    ct, tag = c.encrypt_and_digest(plaintext)
    payload = ct + tag
    return bytes([2]) + nonce + payload[::-1]


def _decrypt(payload: bytes) -> bytes:
    ver = payload[0]
    nonce = payload[1:25]
    body = payload[25:]
    if ver == 2:
        body = body[::-1]
    ct = body[:-16]
    tag = body[-16:]
    c = ChaCha20_Poly1305.new(key=KEY, nonce=nonce)
    return c.decrypt_and_verify(ct, tag)


def _guest() -> dict:
    dfp = _gen_dfp()
    H = {
        "User-Agent": UA,
        "X-VERSION": "1.0.8",
        "X-Payload-Version": "2",
        "Content-Type": "text/plain; charset=utf-8",
        "x-skip-error-prompt": "true",
        "x-guest-auth-request": "true",
        "x-skip-session": "true",
        "Cookie": dfp,
    }
    r = requests.post(f"{BASE}/auth/guest", data=_encrypt(b"{}").hex(),
                      headers=H, timeout=15, verify=False)
    r.raise_for_status()
    data = json.loads(_decrypt(bytes.fromhex(r.json()["payload"])).decode("utf-8"))
    session = ""
    for part in r.headers.get("set-cookie", "").split("; "):
        if part.startswith("session="):
            session = part.split("=", 1)[1]
    return {"token": data["auth_token"], "session": session, "dfp": dfp}


_CRED = None

def _get_credentials(force=False) -> dict:
    global _CRED
    if _CRED and not force:
        return _CRED
    cred = _guest()
    _CRED = cred
    return cred


def parse(params):
    """获取专辑章节列表（引擎契约：def parse(params)，返回 list[{chapter_id, title, ...}]）"""
    book_id = params.get("bookId", "")   # 即搜索返回的 id
    page = int(params.get("page", 1))
    size = int(params.get("size", 200)) or 200
    base_url = f"{BASE}/album/detail"   # TODO: 路径为推测，需抓包核实（搜索为 /apk/search）
    try:
        cred = _get_credentials()
        headers = {
            "User-Agent": UA,
            "X-VERSION": "1.0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "X-Session-Type": "guest",
            "Authorization": "Bearer " + cred["token"],
            "X-Payload-Version": "2",
            "Content-Type": "text/plain; charset=utf-8",
            "Accept-Encoding": "gzip",
            "Cookie": cred.get("dfp", _gen_dfp()) + "; session=" + cred.get("session", ""),
        }
        body = json.dumps({"albumId": book_id, "page": page, "size": size},
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response = requests.post(base_url, data=_encrypt(body).hex(),
                                 headers=headers, timeout=15, verify=False)
        response.raise_for_status()
        data = json.loads(_decrypt(bytes.fromhex(response.json()["payload"])).decode("utf-8"))
    except Exception as e:
        print(f"获取章节失败: {e}")
        return []

    result = []
    for idx, item in enumerate(data.get("tracks", []) or data.get("chapters", []) or []):
        result.append({
            "chapter_id": str(item.get("id", "")),   # 必填：作为 chapter_id / trackId
            "title": item.get("name", "") or item.get("title", ""),
            "order": idx + 1,
            # "trackId": item.get("id"),              # 如需自定义字段透传给音频脚本，可加
        })
    print(f"共获取到 {len(result)} 个章节")
    return result
