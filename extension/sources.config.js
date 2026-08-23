// sources.config.js — 第三方音源注册表
//
// 新增音源两步走：
//   1. 在 sources/ 目录新建你的音源文件（如 ting.js），内部用 registerSource 自注册
//      （参考 sources/_template.js 模板 / sources/A.js 实例）
//   2. 在下面数组里加一行文件名
// 然后到 chrome://extensions 刷新插件即可生效，无需改动其他任何文件。
//
// 注意：
//   - 文件按数组顺序加载；单个文件损坏/不存在只会跳过该源并打日志，不影响其他音源和官方源。
//   - 文件名 '_' 开头的视为模板/草稿，不要加进列表。
globalThis.PLUGIN_SOURCES = [
  'A.js',
  'B.js',
]
