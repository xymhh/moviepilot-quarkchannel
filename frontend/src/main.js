import { createApp, h, ref } from "vue"
import { createVuetify } from "vuetify"
import { aliases, mdi } from "vuetify/iconsets/mdi"
import "vuetify/styles"
import * as components from "vuetify/components"
import * as directives from "vuetify/directives"

const { VApp, VContainer, VMain, VTab, VTabs, VWindow, VWindowItem } = components
import AppPage from "./components/AppPage.vue"
import Config from "./components/Config.vue"

const previewWidth = Number(new URLSearchParams(location.search).get("w") || 0)
if (previewWidth > 0) {
  window.__QC_PREVIEW_WIDTH__ = previewWidth
}

const vuetify = createVuetify({
  components,
  directives,
  theme: { defaultTheme: "dark" },
  icons: { defaultSet: "mdi", aliases, sets: { mdi } },
})

const now = Math.floor(Date.now() / 1000)
const channels = ref([
  {
    id: "ch-pansou",
    name: "主盘搜",
    type: "pansou",
    enabled: true,
    priority: 50,
    config: { base_url: "http://pansou:8888", src: "all", plugins: [], cloud_types: ["quark"] },
    stats: { last_scan: now - 120, last_error: "", hits: 128 },
  },
  {
    id: "ch-tg",
    name: "追更频道",
    type: "tg_channel",
    enabled: true,
    priority: 30,
    config: { channel: "@demo_channel", interval_minutes: 10, scan_limit: 100, backfill: false },
    stats: { last_scan: now - 60, last_error: "", hits: 46 },
  },
  {
    id: "ch-tg2",
    name: "备用频道",
    type: "tg_channel",
    enabled: false,
    priority: 20,
    config: { channel: "@backup_channel", interval_minutes: 15, scan_limit: 50, backfill: true },
    stats: { last_scan: 0, last_error: "", hits: 0 },
  },
])

let sequence = channels.value.length
let config = {
  enabled: true,
  downloader_name: "网盘直链",
  download_root: "/downloads",
  max_tasks: 3,
  only_whitelist: false,
  keyword_whitelist: "",
  mp_base_url: "http://127.0.0.1:3000",
  aria2_enabled: true,
  aria2_connections: 64,
  aria2_split: 64,
  keep_saved: false,
  tg_api_id: "",
  tg_api_hash: "",
  tg_session: "",
}

function channelId(url) {
  const match = url.match(/\/channels\/([^/]+)/)
  return match ? decodeURIComponent(match[1]) : ""
}

function summary() {
  return {
    total: channels.value.length,
    enabled: channels.value.filter((item) => item.enabled).length,
    tg_entries: 320,
    last_poll: Math.max(0, ...channels.value.map((item) => item.stats?.last_scan || 0)),
  }
}

const api = {
  async get(url) {
    if (url.endsWith("/channels/summary")) {
      return { success: true, data: summary() }
    }
    if (url.endsWith("/channels")) {
      return { success: true, data: { items: channels.value } }
    }
    throw new Error("预览接口未实现")
  },
  async post(url, body) {
    if (url.includes("/test")) {
      const item = channels.value.find((row) => row.id === channelId(url))
      return {
        success: true,
        message: item ? `「${item.name}」连接正常` : "连接正常",
      }
    }
    if (url.includes("/scan")) {
      const item = channels.value.find((row) => row.id === channelId(url))
      if (item) {
        item.stats.last_scan = Math.floor(Date.now() / 1000)
        item.stats.hits += 3
      }
      return { success: true, message: "扫描完成，新增 3 条", data: { added: 3 } }
    }
    if (url.endsWith("/channels")) {
      const item = {
        ...body,
        id: `ch-preview-${++sequence}`,
        stats: { last_scan: 0, last_error: "", hits: 0 },
      }
      channels.value.push(item)
      return { success: true, data: { id: item.id } }
    }
    throw new Error("预览接口未实现")
  },
  async put(url, body) {
    const item = channels.value.find((row) => row.id === channelId(url))
    if (!item) return { success: false, message: "渠道不存在" }
    if (body.config) {
      item.config = { ...body.config }
      item.name = body.name
      item.priority = body.priority
      item.enabled = body.enabled
    } else {
      Object.assign(item, body)
    }
    return { success: true, data: item }
  },
  async delete(url) {
    channels.value = channels.value.filter((row) => row.id !== channelId(url))
    return { success: true }
  },
}

const Root = {
  setup() {
    const view = ref("channels")
    return () =>
      h(
        VApp,
        {},
        {
          default: () =>
            h(
              VMain,
              {},
              {
                default: () =>
                  h(
                    VContainer,
                    {
                      class: "py-4",
                      style: previewWidth > 0
                        ? `max-width: ${previewWidth}px; width: 100%; padding: 0 8px`
                        : "max-width: 1100px",
                    },
                    {
                      default: () => [
                        h(
                          VTabs,
                          {
                            modelValue: view.value,
                            "onUpdate:modelValue": (value) => (view.value = value),
                            class: "mb-4",
                          },
                          {
                            default: () => [
                              h(VTab, { value: "channels" }, { default: () => "渠道管理" }),
                              h(VTab, { value: "config" }, { default: () => "插件设置" }),
                            ],
                          },
                        ),
                        h(
                          VWindow,
                          { modelValue: view.value, "onUpdate:modelValue": (value) => (view.value = value) },
                          {
                            default: () => [
                              h(
                                VWindowItem,
                                { value: "channels" },
                                { default: () => h(AppPage, { api, pluginId: "QuarkChannel" }) },
                              ),
                              h(
                                VWindowItem,
                                { value: "config" },
                                {
                                  default: () =>
                                    h(Config, {
                                      api,
                                      initialConfig: config,
                                      pluginId: "QuarkChannel",
                                      onSave: (payload) => {
                                        config = payload
                                      },
                                    }),
                                },
                              ),
                            ],
                          },
                        ),
                      ],
                    },
                  ),
              },
            ),
        },
      )
  },
}

createApp(Root).use(vuetify).mount("#app")
