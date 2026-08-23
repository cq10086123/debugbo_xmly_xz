// offscreen.js — 在 Offscreen Document 中运行 du_web_sdk，计算 xm-sign
// 每个插件安装有独立的浏览器指纹（browserID），即"用户维度"的请求身份。
const CID = 't6pfoml9679z52kqw93uqu75eflqdg1bykhl'
const KFP = 'h5_goyxvzyohd'

function whenSdkReady() {
  return new Promise((resolve, reject) => {
    if (window.du_web_sdk) return resolve()
    let n = 0
    const t = setInterval(() => {
      if (window.du_web_sdk) { clearInterval(t); resolve() }
      else if (++n > 60) { clearInterval(t); reject(new Error('du_web_sdk 超时未加载')) }
    }, 50)
  })
}

async function getXmSign() {
  await whenSdkReady()
  return await new Promise((resolve, reject) => {
    let browserID = ''
    const p1 = new Promise((res) => window.du_web_sdk.getBrowserID(CID, KFP, '', (t) => { browserID = t || ''; res(t || '') }))
    const p2 = new Promise((res) => window.du_web_sdk.getSessionID(CID, KFP, '', (t) => res(t || '')))
    Promise.all([p1, p2])
      .then(([b, s]) => resolve((b || '') + '&&' + (s || '')))
      .catch(reject)
  })
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.target !== 'offscreen') return false
  if (msg.type === 'sign') {
    getXmSign()
      .then((sign) => sendResponse(sign))
      .catch((e) => sendResponse({ error: e.message }))
    return true // 异步响应
  }
  return false
})
