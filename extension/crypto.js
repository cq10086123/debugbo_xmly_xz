// crypto.js — 喜马拉雅 downloadAacUrl 解密（AES-128-ECB）
// 密钥为公开常量（与后端 core/crypto.py 完全一致）。免费音频返回的是明文 URL，无需解密。
//
// 注意：浏览器 Web Crypto 不支持 ECB，且 AES-CBC 解密会强制 PKCS7 去填充，无法用它逐块模拟 ECB，
// 因此这里用一段纯 JS 的 AES-128-ECB 实现（仅解密短 URL，性能足够）。

const XM_AES_KEY = 'aaad3e4fd540b0f79dca95606e72bf93'

// ── GF(2^8) 乘法 ──
function gmul(a, b) {
  let p = 0
  for (let i = 0; i < 8; i++) {
    if (b & 1) p ^= a
    const hi = a & 0x80
    a = (a << 1) & 0xff
    if (hi) a ^= 0x1b
    b >>= 1
  }
  return p & 0xff
}
function rotl8(x, n) { return ((x << n) | (x >> (8 - n))) & 0xff }

// 运行期生成 SBOX / INV_SBOX（避免写 256 常量）
const SBOX = new Uint8Array(256)
const INV_SBOX = new Uint8Array(256)
for (let i = 0; i < 256; i++) {
  let s = i ? (function inv(x) { for (let j = 1; j < 256; j++) if (gmul(x, j) === 1) return j; return 0 })(i) : 0
  const afs = s ^ rotl8(s, 1) ^ rotl8(s, 2) ^ rotl8(s, 3) ^ rotl8(s, 4) ^ 0x63
  SBOX[i] = afs & 0xff
}
for (let i = 0; i < 256; i++) INV_SBOX[SBOX[i]] = i

// ── 密钥扩展（AES-128：11 轮密钥，176 字节）──
function keyExpansion(key) {
  const w = new Uint8Array(176)
  for (let i = 0; i < 16; i++) w[i] = key[i]
  let rcon = 1
  for (let i = 16; i < 176; i += 4) {
    let t0 = w[i - 4], t1 = w[i - 3], t2 = w[i - 2], t3 = w[i - 1]
    if (i % 16 === 0) {
      const a = t0; t0 = t1; t1 = t2; t2 = t3; t3 = a // 循环左移
      t0 = SBOX[t0]; t1 = SBOX[t1]; t2 = SBOX[t2]; t3 = SBOX[t3]
      t0 ^= rcon; rcon = gmul(rcon, 2)
    }
    w[i] = w[i - 16] ^ t0
    w[i + 1] = w[i - 15] ^ t1
    w[i + 2] = w[i - 14] ^ t2
    w[i + 3] = w[i - 13] ^ t3
  }
  return w
}

function addRoundKey(s, w, off) {
  for (let i = 0; i < 16; i++) s[i] ^= w[off + i]
}
function invShiftRows(s) {
  const o = new Uint8Array(16)
  for (let r = 0; r < 4; r++) for (let c = 0; c < 4; c++) o[r + 4 * ((c + r) % 4)] = s[r + 4 * c]
  s.set(o)
}
function invSubBytes(s) { for (let i = 0; i < 16; i++) s[i] = INV_SBOX[s[i]] }
function invMixColumns(s) {
  for (let c = 0; c < 4; c++) {
    const i = 4 * c
    const a0 = s[i], a1 = s[i + 1], a2 = s[i + 2], a3 = s[i + 3]
    s[i]     = gmul(a0, 0x0e) ^ gmul(a1, 0x0b) ^ gmul(a2, 0x0d) ^ gmul(a3, 0x09)
    s[i + 1] = gmul(a0, 0x09) ^ gmul(a1, 0x0e) ^ gmul(a2, 0x0b) ^ gmul(a3, 0x0d)
    s[i + 2] = gmul(a0, 0x0d) ^ gmul(a1, 0x09) ^ gmul(a2, 0x0e) ^ gmul(a3, 0x0b)
    s[i + 3] = gmul(a0, 0x0b) ^ gmul(a1, 0x0d) ^ gmul(a2, 0x09) ^ gmul(a3, 0x0e)
  }
}

function aes128DecryptBlock(inp, w) {
  const s = new Uint8Array(16)
  for (let i = 0; i < 16; i++) s[i] = inp[i]
  // 轮密钥 k 占据字节 [16k, 16k+15]（4 字 × 4 字节）
  addRoundKey(s, w, 160) // Nr=10 → 初始白化用第 10 轮密钥
  for (let r = 9; r >= 1; r--) {
    invShiftRows(s)
    invSubBytes(s)
    addRoundKey(s, w, 16 * r)
    invMixColumns(s)
  }
  invShiftRows(s)
  invSubBytes(s)
  addRoundKey(s, w, 0)
  return s
}

function hexToBytes(hex) {
  const out = new Uint8Array(hex.length / 2)
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16)
  return out
}
function b64urlToBytes(s) {
  const pad = s + '='.repeat((4 - (s.length % 4)) % 4)
  const b64 = pad.replace(/-/g, '+').replace(/_/g, '/')
  const bin = atob(b64)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}

// 解密入口：密文为 base64url 编码；明文 URL 直接返回。结果与后端 core/crypto.py 行为一致（去非打印字符）。
function decryptUrl(ciphertext) {
  if (!ciphertext) return ciphertext
  if (ciphertext.startsWith('http://') || ciphertext.startsWith('https://')) return ciphertext
  try {
    const w = keyExpansion(hexToBytes(XM_AES_KEY))
    const bytes = b64urlToBytes(ciphertext)
    const out = new Uint8Array(bytes.length)
    for (let i = 0; i < bytes.length; i += 16) {
      const blk = aes128DecryptBlock(bytes.subarray(i, i + 16), w)
      out.set(blk, i)
    }
    // 与后端一致：去除非打印 ASCII（含 PKCS7 填充）
    let s = ''
    for (const b of out) if (b >= 0x20 && b <= 0x7e) s += String.fromCharCode(b)
    return s.trim()
  } catch (e) {
    return ciphertext
  }
}

globalThis.decryptUrl = decryptUrl

// ══════════════════════════════════════════════════════════════
//  以下为本地下载第三方音源（ating / tingyou8）新增的加密原语。
//  与后端 Python 脚本（pycryptodome / hashlib）行为逐一对应，勿随意改动。
// ══════════════════════════════════════════════════════════════

function rotl32(x, n) { return ((x << n) | (x >>> (32 - n))) >>> 0 }

function bytesToHex(u8) {
  let s = ''
  for (const b of u8) s += b.toString(16).padStart(2, '0')
  return s
}
function u8ToB64(u8) {
  let bin = ''
  for (const b of u8) bin += String.fromCharCode(b)
  return btoa(bin)
}
function u32le(a, o) { return (a[o] | (a[o + 1] << 8) | (a[o + 2] << 16) | (a[o + 3] << 24)) >>> 0 }
function u32be(a, o) { return ((a[o] << 24) | (a[o + 1] << 16) | (a[o + 2] << 8) | a[o + 3]) >>> 0 }

// ── MD5（纯 JS；ating 的 token / 内部签名用）──
function md5(input) {
  const bytes = typeof input === 'string' ? new TextEncoder().encode(input) : input
  const bitLen = bytes.length * 8
  const totalLen = (((bytes.length + 1 + 8) + 63) >> 6) << 6
  const buf = new Uint8Array(totalLen)
  buf.set(bytes)
  buf[bytes.length] = 0x80
  const dv = new DataView(buf.buffer)
  dv.setUint32(totalLen - 8, bitLen >>> 0, true)
  dv.setUint32(totalLen - 4, Math.floor(bitLen / 0x100000000) >>> 0, true)

  let a0 = 0x67452301, b0 = 0xefcdab89, c0 = 0x98badcfe, d0 = 0x10325476
  const S = [7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,
             5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20,
             4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
             6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21]
  const K = new Int32Array(64)
  for (let i = 0; i < 64; i++) K[i] = Math.floor(Math.abs(Math.sin(i + 1)) * 4294967296) | 0

  const M = new Int32Array(16)
  for (let off = 0; off < totalLen; off += 64) {
    for (let i = 0; i < 16; i++) M[i] = dv.getInt32(off + i * 4, true)
    let A = a0, B = b0, C = c0, D = d0
    for (let i = 0; i < 64; i++) {
      let F, g
      if (i < 16) { F = (B & C) | (~B & D); g = i }
      else if (i < 32) { F = (D & B) | (~D & C); g = (5 * i + 1) % 16 }
      else if (i < 48) { F = B ^ C ^ D; g = (3 * i + 5) % 16 }
      else { F = C ^ (B | ~D); g = (7 * i) % 16 }
      const tmp = D
      D = C
      C = B
      B = (B + rotl32((A + F + K[i] + M[g]) | 0, S[i])) | 0
      A = tmp
    }
    a0 = (a0 + A) | 0
    b0 = (b0 + B) | 0
    c0 = (c0 + C) | 0
    d0 = (d0 + D) | 0
  }
  const out = new Uint8Array(16)
  const dvo = new DataView(out.buffer)
  dvo.setInt32(0, a0, true); dvo.setInt32(4, b0, true); dvo.setInt32(8, c0, true); dvo.setInt32(12, d0, true)
  return bytesToHex(out)
}

// ── AES-128-ECB 加密（ating 签名/请求体用；复用上方 SBOX/keyExpansion）──
function subBytes(s) { for (let i = 0; i < 16; i++) s[i] = SBOX[s[i]] }
function shiftRows(s) {
  const o = new Uint8Array(16)
  for (let r = 0; r < 4; r++) for (let c = 0; c < 4; c++) o[r + 4 * c] = s[r + 4 * ((c + r) % 4)]
  s.set(o)
}
function mixColumns(s) {
  for (let c = 0; c < 4; c++) {
    const i = 4 * c
    const a0 = s[i], a1 = s[i + 1], a2 = s[i + 2], a3 = s[i + 3]
    s[i]     = gmul(a0, 2) ^ gmul(a1, 3) ^ a2 ^ a3
    s[i + 1] = a0 ^ gmul(a1, 2) ^ gmul(a2, 3) ^ a3
    s[i + 2] = a0 ^ a1 ^ gmul(a2, 2) ^ gmul(a3, 3)
    s[i + 3] = gmul(a0, 3) ^ a1 ^ a2 ^ gmul(a3, 2)
  }
}
function aes128EncryptBlock(inp, w) {
  const s = new Uint8Array(16)
  for (let i = 0; i < 16; i++) s[i] = inp[i]
  addRoundKey(s, w, 0)
  for (let r = 1; r <= 9; r++) {
    subBytes(s)
    shiftRows(s)
    mixColumns(s)
    addRoundKey(s, w, 16 * r)
  }
  subBytes(s)
  shiftRows(s)
  addRoundKey(s, w, 160)
  return s
}
// data: string | Uint8Array；keyStr: 16 字节 ASCII 密钥；PKCS7 填充，返回 base64
function aesEcbEncryptB64(data, keyStr) {
  const key = new TextEncoder().encode(keyStr)
  const w = keyExpansion(key)
  const plain = typeof data === 'string' ? new TextEncoder().encode(data) : data
  const padLen = 16 - (plain.length % 16)
  const padded = new Uint8Array(plain.length + padLen)
  padded.set(plain)
  padded.fill(padLen, plain.length)
  const out = new Uint8Array(padded.length)
  for (let i = 0; i < padded.length; i += 16) {
    out.set(aes128EncryptBlock(padded.subarray(i, i + 16), w), i)
  }
  return u8ToB64(out)
}

// ── WebCrypto 封装：SHA-256 / AES-GCM 加密（tingyou8 用）──
async function sha256Bytes(u8) {
  const d = await crypto.subtle.digest('SHA-256', u8)
  return new Uint8Array(d)
}
async function aesGcmEncrypt(keyU8, nonceU8, plainU8) {
  const key = await crypto.subtle.importKey('raw', keyU8, { name: 'AES-GCM' }, false, ['encrypt'])
  const buf = await crypto.subtle.encrypt({ name: 'AES-GCM', iv: nonceU8, tagLength: 128 }, key, plainU8)
  return new Uint8Array(buf) // ct || tag（与 pycryptodome encrypt_and_digest 拼接顺序一致）
}

// ── ChaCha20 / HChaCha20 / XChaCha20-Poly1305（tingyou8 解密用；WebCrypto 不支持，纯 JS）──
function _chachaQR(s, a, b, c, d) {
  s[a] = (s[a] + s[b]) >>> 0; s[d] = rotl32(s[d] ^ s[a], 16)
  s[c] = (s[c] + s[d]) >>> 0; s[b] = rotl32(s[b] ^ s[c], 12)
  s[a] = (s[a] + s[b]) >>> 0; s[d] = rotl32(s[d] ^ s[a], 8)
  s[c] = (s[c] + s[d]) >>> 0; s[b] = rotl32(s[b] ^ s[c], 7)
}
function _chachaRounds(w) {
  for (let i = 0; i < 10; i++) {
    _chachaQR(w, 0, 4, 8, 12); _chachaQR(w, 1, 5, 9, 13); _chachaQR(w, 2, 6, 10, 14); _chachaQR(w, 3, 7, 11, 15)
    _chachaQR(w, 0, 5, 10, 15); _chachaQR(w, 1, 6, 11, 12); _chachaQR(w, 2, 7, 8, 13); _chachaQR(w, 3, 4, 9, 14)
  }
}
// RFC 8439 ChaCha20 单块（counter 32bit，nonce 12 字节）
function chacha20Block(key, counter, nonce) {
  const s = new Uint32Array(16)
  s[0] = 0x61707865; s[1] = 0x3320646e; s[2] = 0x79622d32; s[3] = 0x6b206574
  for (let i = 0; i < 8; i++) s[4 + i] = u32le(key, i * 4)
  s[12] = counter >>> 0
  s[13] = u32le(nonce, 0); s[14] = u32le(nonce, 4); s[15] = u32le(nonce, 8)
  const w = Uint32Array.from(s)
  _chachaRounds(w)
  const out = new Uint8Array(64)
  for (let i = 0; i < 16; i++) {
    const v = (w[i] + s[i]) >>> 0
    out[i * 4] = v & 0xff; out[i * 4 + 1] = (v >>> 8) & 0xff; out[i * 4 + 2] = (v >>> 16) & 0xff; out[i * 4 + 3] = (v >>> 24) & 0xff
  }
  return out
}
// HChaCha20（XChaCha20 子密钥派生；nonce 16 字节，输出 state[0..3]||state[12..15]，不加回原状态）
function hchacha20(key, nonce16) {
  const s = new Uint32Array(16)
  s[0] = 0x61707865; s[1] = 0x3320646e; s[2] = 0x79622d32; s[3] = 0x6b206574
  for (let i = 0; i < 8; i++) s[4 + i] = u32le(key, i * 4)
  for (let i = 0; i < 4; i++) s[12 + i] = u32le(nonce16, i * 4)
  _chachaRounds(s)
  const out = new Uint8Array(32)
  const idx = [0, 1, 2, 3, 12, 13, 14, 15]
  for (let j = 0; j < 8; j++) {
    const v = s[idx[j]]
    out[j * 4] = v & 0xff; out[j * 4 + 1] = (v >>> 8) & 0xff; out[j * 4 + 2] = (v >>> 16) & 0xff; out[j * 4 + 3] = (v >>> 24) & 0xff
  }
  return out
}
// Poly1305（BigInt 版，消息短、正确性优先）
function poly1305Tag(key, msg) {
  const P = (1n << 130n) - 5n
  let r = 0n, s = 0n
  for (let i = 0; i < 16; i++) r |= BigInt(key[i]) << BigInt(8 * i)
  r &= 0x0ffffffc0ffffffc0ffffffc0fffffffn
  for (let i = 0; i < 16; i++) s |= BigInt(key[16 + i]) << BigInt(8 * i)
  let acc = 0n
  for (let off = 0; off < msg.length; off += 16) {
    const n = Math.min(16, msg.length - off)
    let block = 0n
    for (let i = 0; i < n; i++) block |= BigInt(msg[off + i]) << BigInt(8 * i)
    block |= 1n << BigInt(8 * n)
    acc = ((acc + block) * r) % P
  }
  acc = (acc + s) % (1n << 128n)
  const out = new Uint8Array(16)
  for (let i = 0; i < 16; i++) out[i] = Number((acc >> BigInt(8 * i)) & 0xffn)
  return out
}
// XChaCha20-Poly1305 解密（pycryptodome ChaCha20_Poly1305 + 24 字节 nonce 等价，无 AAD）
function xchacha20poly1305Decrypt(key, nonce24, ct, tag) {
  const subkey = hchacha20(key, nonce24.subarray(0, 16))
  const nonce12 = new Uint8Array(12)
  nonce12.set(nonce24.subarray(16, 24), 4)
  const otk = chacha20Block(subkey, 0, nonce12).subarray(0, 32)
  const padLen = (16 - (ct.length % 16)) % 16
  const mac = new Uint8Array(ct.length + padLen + 16)
  mac.set(ct, 0)
  // mac 后 16 字节：aad 长度(0) 8B LE + ct 长度 8B LE（左侧 pad 与 aad 长度已是 0）
  const L = ct.length
  const base = mac.length - 8
  mac[base] = L & 0xff; mac[base + 1] = (L >>> 8) & 0xff; mac[base + 2] = (L >>> 16) & 0xff; mac[base + 3] = (L >>> 24) & 0xff
  const expected = poly1305Tag(otk, mac)
  let diff = 0
  for (let i = 0; i < 16; i++) diff |= expected[i] ^ tag[i]
  if (diff !== 0) throw new Error('ChaCha20-Poly1305 校验失败（tag mismatch）')
  const out = new Uint8Array(ct.length)
  for (let off = 0; off < ct.length; off += 64) {
    const ks = chacha20Block(subkey, 1 + Math.floor(off / 64), nonce12)
    const n = Math.min(64, ct.length - off)
    for (let j = 0; j < n; j++) out[off + j] = ct[off + j] ^ ks[j]
  }
  return out
}

globalThis.md5 = md5
globalThis.aesEcbEncryptB64 = aesEcbEncryptB64
globalThis.sha256Bytes = sha256Bytes
globalThis.aesGcmEncrypt = aesGcmEncrypt
globalThis.xchacha20poly1305Decrypt = xchacha20poly1305Decrypt
globalThis.bytesToHex = bytesToHex
globalThis.u8ToB64 = u8ToB64
