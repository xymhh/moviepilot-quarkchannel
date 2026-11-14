/**
 * 清理生产产物里只供本地预览使用的入口文件。
 *
 * MoviePilot 只需要 remoteEntry.js 与被 expose 的组件；index.html / index-*.js
 * 是本地预览页（含完整 Vuetify），不该随插件分发。
 */
import fs from "node:fs"
import path from "node:path"

const dir = path.resolve(process.cwd(), "../plugins.v3/quarkchannel/dist/assets")
if (!fs.existsSync(dir)) process.exit(0)

for (const name of fs.readdirSync(dir)) {
  const isPreviewEntry = name === "index.html" || /^index-[A-Za-z0-9_-]+\.(js|css)$/.test(name)
  if (isPreviewEntry) {
    fs.unlinkSync(path.join(dir, name))
    console.log("removed", name)
  }
}
