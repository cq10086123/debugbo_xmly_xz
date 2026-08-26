<script setup>
import { getBizToken } from '../utils/request'


function downloadSkills() {
  const token = getBizToken()
  if (!token) {
    alert('请先登录卡密后再下载 Skills 配置')
    return
  }
  const a = document.createElement('a')
  a.href = `/api/skills/export_openapi?token=${encodeURIComponent(token)}`
  a.download = `ai_skills_config_${token.substring(0,4)}.json`
  document.body.appendChild(a)
  a.click()
  a.remove()
}

function downloadExtension() {
  const token = getBizToken()
  if (!token) {
    alert('请先登录卡密后再下载插件')
    return
  }
  const a = document.createElement('a')
  a.href = `/api/files/extension-zip?token=${encodeURIComponent(token)}`
  a.download = 'yousheng_extension.zip'
  document.body.appendChild(a)
  a.click()
  a.remove()
}
</script>

<template>
  <div class="guide">
    <div class="card-panel pad hero">
      <h1>使用说明</h1>
      <p class="muted">卡密登录后即可搜索、批量下载有声书；支持「离线下载到服务器」和「本地下载到浏览器」两种模式。</p>
    </div>

    <div class="warning-banner">
      <span class="warn-icon">⚠️</span>
      <div class="warn-text">
        <b>请勿开启多并发下载，极易触发官方限流。</b>
        如果触发限流（任务大面积失败、提示 429 / 签名错误），请立即切换到<b>「本地下载」</b>模式：音频直接走本机 IP 拉到你电脑，不受服务器公网 IP 限流影响，也不占用服务器磁盘。
      </div>
    </div>

    <section class="card-panel pad">
      <h2>快速开始</h2>
      <ol class="steps">
        <li>在「搜索 / 下载」页输入书名并搜索。</li>
        <li>点击专辑查看章节列表，确认范围。</li>
        <li>选择下载模式：
          <ul>
            <li><b>服务器下载</b>：音频保存在服务器，之后到「文件管理」打包下载。</li>
            <li><b>本地下载</b>（推荐）：需要安装下方浏览器插件，音频直接下载到你电脑，不占服务器空间、且走本机 IP 更不易被限流。</li>
          </ul>
        </li>
        <li>点击「开始下载」，在「下载任务」查看进度。</li>
        <li>服务器下载完成后，到「文件管理」按专辑折叠/展开，打包 ZIP 或单集下载。</li>
      </ol>
    </section>

    <section class="card-panel pad">
      <h2>服务器下载 vs 本地下载</h2>
      <table class="diff-table">
        <thead>
          <tr><th>对比项</th><th>服务器下载</th><th>本地下载</th></tr>
        </thead>
        <tbody>
          <tr>
            <td>存储位置</td>
            <td>保存在服务器磁盘</td>
            <td>直接下载到你的电脑</td>
          </tr>
          <tr>
            <td>是否需要插件</td>
            <td>不需要</td>
            <td>需要安装浏览器插件</td>
          </tr>
          <tr>
            <td>适合场景</td>
            <td>挂机批量、多账号、VIP 解密</td>
            <td>省去服务器空间、走本机 IP</td>
          </tr>
          <tr>
            <td>取回方式</td>
            <td>「文件管理」→ 下载 ZIP / 单集</td>
            <td>浏览器默认下载目录</td>
          </tr>
          <tr>
            <td>对服务器要求</td>
            <td>需要一定磁盘/带宽</td>
            <td>服务器只跑解析，不占空间</td>
          </tr>
        <tr>
          <td>IP 风控</td>
          <td>走服务器公网 IP，高频易触发官方限流</td>
          <td>走你本机 IP，风险分散、更稳</td>
        </tr>
        </tbody>
      </table>
    </section>

    <section class="card-panel pad">
      <h2>限流与风控说明（重要）</h2>
      <p class="muted">官方音源（如喜马拉雅）对单一 IP 的请求频率有风控限制。服务器下载模式下，解析与下载请求都走<b>同一台服务器的公网 IP</b>，一旦短时间内大量请求，极易被官方临时限流，表现为任务频繁失败、账号被短暂封禁。</p>
      <div class="tips">
        <p><b>强烈建议优先使用「本地下载」</b>：音频解析与下载都在你的浏览器里执行，走你自己的网络 IP，既能绕开服务器 IP 的集中限流，又不占用服务器磁盘。</p>
        <p>若必须用服务器下载，请<b>降低并发数</b>（如 1~3），避免短时间高频请求。</p>
        <p>多账号可按需在「我的账号」中添加并轮换，进一步分散单账号压力。</p>
        <p>遇到任务大面积失败、提示签名错误/429/风控时，先暂停、降低并发，或切换到本地下载。</p>
      </div>
    </section>


    <section class="card-panel pad">
      <h2>🤖 AI 助手 Skills 配置下载</h2>
      <p class="muted">如果你想在微信、Coze 或 Dify 等 AI 助手中使用当前卡密通过对话来控制下载，请点击下方下载你的专属 Skills 配置文件。将其导入到 AI 平台中即可使用，里面<b>已自动内置你当前的卡密</b>。</p>
      <button class="success big" @click="downloadSkills">📥 下载专属 AI Skills 配置</button>
    </section>

    <section class="card-panel pad">
      <h2>一键下载浏览器插件</h2>
      <p class="muted">本地下载模式必须安装插件。插件会把音频解析和下载放到你的浏览器里执行，走本机 IP，服务器不存文件。</p>
      <button class="success big" @click="downloadExtension">⬇ 下载插件压缩包</button>
    </section>

    <section class="card-panel pad">
      <h2>浏览器插件安装教程（Chrome / Edge）</h2>
      <ol class="steps">
        <li>点击上方按钮，下载 <code>yousheng_extension.zip</code>。</li>
        <li>解压到一个固定目录（例如 <code>D:\yousheng_extension</code>），<b>不要删除</b>，插件会从这里读取。</li>
        <li>打开浏览器扩展管理页：
          <ul>
            <li>Chrome：地址栏输入 <code>chrome://extensions</code> 并回车。</li>
            <li>Edge：地址栏输入 <code>edge://extensions</code> 并回车。</li>
          </ul>
        </li>
        <li>打开右上角「开发者模式」开关。</li>
        <li>点击「加载已解压的扩展程序」，选择刚才解压的文件夹。</li>
        <li>浏览器工具栏出现 🎧 图标即表示安装成功。</li>
        <li>首次使用时点击插件图标，填写服务器地址（本机一般为 <code>http://localhost:6500</code>）并保存。</li>
      </ol>
    </section>

    <section class="card-panel pad">
      <h2>常见问题</h2>
      <dl class="faq">
        <dt>下载 ZIP 或单集没反应？</dt>
        <dd>大文件是浏览器原生流式下载，请按 <kbd>Ctrl+J</kbd>（Mac <kbd>⌘+J</kbd>）打开下载管理器查看进度。若长时间没出现，请刷新页面或强制刷新 <kbd>Ctrl+Shift+R</kbd>。</dd>
        <dt>文件管理里集数太多？</dt>
        <dd>文件管理页默认按专辑折叠，点击专辑标题可展开/收起，顶部还有「展开全部」「折叠全部」按钮。</dd>
        <dt>插件安装后无法使用？</dt>
        <dd>确认插件里填写的服务器地址能访问；若本机运行，请使用 <code>http://localhost:6500</code>，并保持后端程序处于运行状态。</dd>
        <dt>任务大面积失败 / 提示 429、签名错误、风控？</dt>
        <dd>多为触发官方限流。先暂停任务，降低服务器下载并发（1~3），或改用「本地下载」走自己 IP；等待一段时间后再试。</dd>
        <dt>服务器下载的音频是 .m4a 而不是 .mp3？</dt>
        <dd>属正常——官方接口返回的是原始音频格式，并非损坏。播放器（如 VLC、PotPlayer）都能正常播放。</dd>
        <dt>下载的 ZIP 解压报错 / 体积异常小？</dt>
        <dd>可能是打包中途网络中断。回到「文件管理」重新点击「下载 ZIP」即可；单集也可用「单集下载」单独获取。</dd>
        <dt>提示「卡密已过期」或「卡密已被禁用」？</dt>
        <dd>卡密有效期到或被停用，请联系发卡方/管理员续期或更换。</dd>
      </dl>
    </section>

    <section class="card-panel pad">
      <h2>反馈与联系</h2>
      <p class="muted">使用中遇到问题、建议或想反馈 bug，欢迎邮件联系：</p>
      <p class="mail">✉️ lm@12311111.xyz</p>
    </section>
  </div>
</template>

<style scoped>
.guide { display: flex; flex-direction: column; gap: 18px; }
.hero { text-align: center; }
.hero h1 { margin: 10px 0 6px; font-size: 22px; }
.hero p { max-width: 640px; margin: 0 auto; line-height: 1.6; }

h2 { font-size: 16px; margin: 0 0 14px; color: var(--text); }
.steps, .faq { margin: 0; padding-left: 18px; color: var(--text-dim); line-height: 1.8; }
.steps li { margin-bottom: 8px; }
.steps ul { margin: 4px 0 0; padding-left: 18px; }

.diff-table {
  width: 100%; border-collapse: collapse; font-size: 13px;
  color: var(--text-dim);
}
.diff-table th, .diff-table td {
  padding: 10px 12px; border: 1px solid var(--border); text-align: left;
}
.diff-table th { background: var(--panel-2); color: var(--text); font-weight: 600; }
.diff-table td:first-child { color: var(--text); font-weight: 600; width: 120px; }

button.big { padding: 12px 22px; font-size: 15px; border-radius: 10px; margin-top: 8px; }
code {
  background: var(--panel-2); padding: 2px 6px; border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px;
}
kbd {
  background: var(--panel-2); border: 1px solid var(--border);
  padding: 1px 5px; border-radius: 4px; font-size: 12px;
}
.faq dt { color: var(--text); font-weight: 600; margin-top: 12px; }
.faq dd { margin: 4px 0 0; }

.warning-banner {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 14px 18px;
  border-radius: 10px;
  border: 1px solid rgba(255, 165, 0, 0.35);
  background: linear-gradient(180deg, rgba(255, 165, 0, 0.10), rgba(255, 165, 0, 0.05));
  color: var(--text-dim);
  line-height: 1.7;
}
.warning-banner .warn-icon {
  font-size: 22px;
  line-height: 1.2;
  flex-shrink: 0;
}
.warning-banner .warn-text { flex: 1; }
.warning-banner .warn-text b { color: var(--text); }

.tips { display: flex; flex-direction: column; gap: 10px; margin: 0; padding: 0; }
.tips p { margin: 0; color: var(--text-dim); line-height: 1.7; }
.tips b { color: var(--text); }

.mail {
  font-size: 16px; font-weight: 700; color: var(--text);
  background: var(--panel-2); display: inline-block;
  padding: 10px 16px; border-radius: 10px; letter-spacing: .5px;
}
</style>
