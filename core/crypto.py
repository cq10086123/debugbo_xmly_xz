"""加解密模块"""

import base64
import binascii
import re
from Crypto.Cipher import AES


def decrypt_download_url(ciphertext: str) -> str:
    """
    解密喜马拉雅 downloadAacUrl
    加密方式: AES-128-ECB
    密钥: aaad3e4fd540b0f79dca95606e72bf93

    如果传入的是明文 URL（免费音频），直接返回原文。
    """
    if not ciphertext:
        return ciphertext

    # 免费音频返回的是明文 URL，无需解密
    if ciphertext.startswith(("http://", "https://")):
        return ciphertext

    # VIP 音频返回的是 base64 编码的 AES 密文，需要解密
    try:
        key = binascii.unhexlify("aaad3e4fd540b0f79dca95606e72bf93")
        ciphertext_bytes = base64.urlsafe_b64decode(
            ciphertext + "=" * (4 - len(ciphertext) % 4)
        )
        cipher = AES.new(key, AES.MODE_ECB)
        plaintext = cipher.decrypt(ciphertext_bytes)
        plaintext = re.sub(r"[^\x20-\x7E]", "", plaintext.decode("utf-8", errors="ignore"))
        return plaintext
    except Exception:
        # 解密失败，原样返回（可能是格式变化的明文 URL）
        return ciphertext
