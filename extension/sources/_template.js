// ══════════════════════════════════════════════════════════════
//  新音源开发模板 —— 复制本文件为 sources/你的接口名.js，改完在
//  sources.config.js 的 PLUGIN_SOURCES 里登记文件名即可被插件自动加载。
//  （本文件以 _ 开头，仅作模板，不会被加载）
// ══════════════════════════════════════════════════════════════
//
// 【契约】
//   registerSource({ name, displayName }, async (chapter, ctx) => 直链字符串 | { url, headers })
//     name        : 必须与后端「接口管理」里的接口 name 完全一致（任务按它路由，卡密绑定也按它校验）
//     displayName : 仅备注用
//     chapter     : { track_id, episode_num, title, fmt }  —— 章节元数据（服务器下发）
//     ctx         : { serverUrl, token, quality, fmt, albumId } —— albumId 即书籍/专辑 ID
//     返回        : 音频直链字符串（http/https 开头）
//
// 【可用全局工具】（crypto.js / resolvers.js 提供，直接用）
//   md5(str)                         纯 JS MD5 → hex 字符串
//   aesEcbEncryptB64(data, keyStr)   AES-128-ECB 加密（PKCS7）→ base64
//   sha256Bytes(u8)                  WebCrypto SHA-256 → Uint8Array（async）
//   aesGcmEncrypt(key, nonce, pt)    WebCrypto AES-GCM → ct||tag（async）
//   xchacha20poly1305Decrypt(key, nonce24, ct, tag)  纯 JS XChaCha20-Poly1305 解密
//   bytesToHex(u8) / hexToBytes(hex) / u8ToB64(u8) / decryptUrl(x)
//   installSourceCookies(origin, cookieStr)          Cookie 注入（async）
//
// 【常见坑】
//   1. MV3 fetch 禁止手动设置 Cookie / Origin / Referer 头（浏览器静默丢弃）。
//      需要 Cookie 鉴权的音源：用 installSourceCookies('https://音源域名/', 'k1=v1; k2=v2')
//      写入浏览器 cookie 商店，fetch 时浏览器自动携带（建议 fetch 加 credentials: 'include'）。
//   2. 响应里的 Set-Cookie 可以用 chrome.cookies.get({ url, name }) 读回（能读 httpOnly），
//      再用 installSourceCookies 按 sameSite:'no_restriction' 重写一遍确保跨站携带。
//   3. chrome.downloads 创建下载时不支持自定义请求头 —— 直链必须是无鉴权裸链，
//      或把鉴权信息编码进 URL query。
//   4. 务必将整个文件包在 IIFE 里（;(() => { ... })()），避免常量名与其他音源冲突。
//   5. 改完文件后到 chrome://extensions 点插件「刷新」才生效。
// ══════════════════════════════════════════════════════════════
;(() => {
  // ── 配置区：常量、密钥、UA 等 ──
  const HOST = 'https://api.example.com'
  const UA = 'your-app-ua'

  // ── 内部辅助函数（凭证缓存、签名、加解密封装等）──
  let cachedToken = null
  async function getToken(force) {
    if (cachedToken && !force) return cachedToken
    // ... 认证流程 ...
    cachedToken = 'xxx'
    return cachedToken
  }

  registerSource({ name: 'your_source_name', displayName: '你的音源显示名' }, async (chapter, ctx) => {
    const bookId = String(ctx.albumId || '')
    const chapterId = String(chapter.track_id || '')
    if (!bookId || !chapterId) throw new Error('缺少 bookId/chapterId')

    const token = await getToken(false)
    const resp = await fetch(`${HOST}/audio?book=${bookId}&chapter=${chapterId}`, {
      headers: { 'User-Agent': UA, 'Authorization': 'Bearer ' + token },
      credentials: 'include',
    })
    if (!resp.ok) throw new Error('HTTP ' + resp.status)
    const data = await resp.json()
    const url = data && data.data && data.data.url
    if (!url) throw new Error('未找到音频URL: ' + JSON.stringify(data).slice(0, 120))
    return url
  })
})()
