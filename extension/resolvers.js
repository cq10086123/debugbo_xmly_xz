// resolvers.js — 音源解析器框架（v0.5.0 起第三方音源插件化）
//
// 结构：
//   本文件          = 框架层：内置官方源 + 注册表 + registerSource 入口 + 共享工具
//   sources/x.js    = 第三方音源：一个音源一个文件，IIFE 包裹，registerSource 自注册
//   sources.config.js = 音源文件名注册表，background.js 启动时按表动态加载
//
// 新增一个第三方音源，只需两步（不用改本文件）：
//   1. 把后端「Python 脚本接口」的 audio 逻辑用 JS 重写为 sources/xxx.js（参考 sources/_template.js）
//   2. 在 sources.config.js 的 PLUGIN_SOURCES 里加一行 'xxx.js'，刷新插件即可
//
// 解析器契约（与后端 parse 契约对应）：
//   registerSource('接口名', async (chapter, ctx) => 直链字符串 | { url, headers })
//     chapter = { track_id, episode_num, title, fmt }
//     ctx     = { serverUrl, token, quality, fmt, albumId }
//   「接口名」必须与后端接口管理里的接口 name 完全一致（任务按这个名字路由）。
//
// 音源文件内可直接使用的全局工具（crypto.js / 本文件提供）：
//   md5(str)                       纯 JS MD5 → hex
//   aesEcbEncryptB64(data, keyStr) AES-128-ECB 加密（PKCS7）→ base64
//   sha256Bytes(u8)                WebCrypto SHA-256 → Uint8Array
//   aesGcmEncrypt(key, nonce, pt)  WebCrypto AES-GCM → ct||tag
//   xchacha20poly1305Decrypt(key, nonce24, ct, tag)  纯 JS XChaCha20-Poly1305 解密
//   bytesToHex / hexToBytes / u8ToB64 / decryptUrl
//   installSourceCookies(origin, cookieStr)  Cookie 注入（MV3 fetch 禁设 Cookie 头，必须用它）
//
// 注意：chrome.downloads 只允许白名单请求头（Authorization/Cookie/Referer/Origin/User-Agent 等），
// 自定义头无法透传，因此涉及自定义头的音源请把鉴权信息编码进 URL，或用 fetch 自行拉流。

const XM_UA = 'Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/102.0.5005.167 Electron/19.1.1 Safari/537.36'

// ── 官方接口（内置源，逻辑固化，不走插件化）──
// 需 xm-sign（由 offscreen 用专属浏览器指纹计算）；最终 CDN 直链无需自定义请求头。
// 官方账号登录态由 background.js 通过 chrome.cookies.set 注入浏览器 cookie 商店，
// 此处的 fetch 访问 mobile.ximalaya.com 时浏览器会自动带上 Cookie（MV3 禁止手动设 Cookie 头）。
async function resolveOfficial(trackId, quality, sign) {
  const ts = Date.now()
  const url = `https://mobile.ximalaya.com/mobile/download/v2/track/${trackId}/ts-${ts}`
  const params = new URLSearchParams({ trackId, device: 'win32', trackQualityLevel: quality })
  const headers = {
    'User-Agent': XM_UA,
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN',
    'Origin': 'https://mobile.ximalaya.com',
    'Referer': 'https://mobile.ximalaya.com',
    'xm-sign': sign,
  }
  let resp
  try {
    resp = await fetch(url + '?' + params.toString(), { headers })
  } catch (e) {
    throw new Error('网络请求失败: ' + e.message)
  }
  const text = await resp.text()
  if (!resp.ok) {
    throw new Error(`HTTP ${resp.status}: ${text.slice(0, 200)}`)
  }
  let data
  try {
    data = JSON.parse(text)
  } catch (e) {
    throw new Error('响应非JSON: ' + text.slice(0, 200))
  }
  if (data.ret !== 0) throw new Error(`请求失败(ret=${data.ret}): ${data.msg || ''}`)
  const enc = data.data && data.data.downloadAacUrl
  if (!enc) throw new Error('未找到 downloadAacUrl（' + text.slice(0, 120) + '）')
  return await globalThis.decryptUrl(enc)
}

// ── 音源注册表 ──
const RESOLVERS = { official: resolveOfficial }

// 注册一个第三方音源。name 必须与后端接口 name 一致；official 为内置源，禁止覆盖。
// 形式一：registerSource('name', resolveFn)
// 形式二：registerSource({ name, displayName }, resolveFn)
function registerSource(nameOrMeta, resolveFn) {
  const meta = typeof nameOrMeta === 'string' ? { name: nameOrMeta } : (nameOrMeta || {})
  const name = meta.name
  if (!name || typeof resolveFn !== 'function') {
    throw new Error('registerSource(name, resolveFn) 参数错误')
  }
  if (name === 'official') throw new Error('official 为内置源，禁止覆盖')
  if (RESOLVERS[name]) console.warn('[plugin] 音源重复注册，后者覆盖前者:', name)
  RESOLVERS[name] = resolveFn
  console.log('[plugin] 第三方音源已注册:', name, meta.displayName || '')
}
globalThis.RESOLVERS = RESOLVERS
globalThis.registerSource = registerSource

// ── 通用 Cookie 注入工具 ──
// MV3 fetch 禁止手动设置 Cookie 头（浏览器静默丢弃），音源若依赖 Cookie 鉴权，
// 必须改用本函数把 cookie 写入浏览器 cookie 商店，后续 fetch 访问该域时浏览器自动携带。
//   origin    例: 'https://azybk.tingyou8.vip/'
//   cookieStr 例: 'dfp=xxx; session=yyy'
async function installSourceCookies(origin, cookieStr) {
  const u = new URL(origin)
  const domain = '.' + u.hostname
  const expiry = Math.floor(Date.now() / 1000) + 24 * 3600
  const pairs = String(cookieStr || '').split(';').map(s => s.trim()).filter(Boolean)
  let ok = 0
  for (const p of pairs) {
    const idx = p.indexOf('=')
    if (idx < 0) continue
    try {
      await chrome.cookies.set({
        url: origin,
        name: p.slice(0, idx).trim(),
        value: p.slice(idx + 1).trim(),
        domain,
        path: '/',
        secure: u.protocol === 'https:',
        httpOnly: false,
        sameSite: 'no_restriction',
        expirationDate: expiry,
      })
      ok++
    } catch (e) {
      console.warn('[plugin] cookie 注入失败:', p.slice(0, idx), e.message)
    }
  }
  return ok
}
globalThis.installSourceCookies = installSourceCookies
