<template>
  <div class="config-page">
    <div class="config-head">
      <div>
        <div class="title-row">
          <v-icon icon="mdi-cog-outline" color="primary" size="26" />
          <h2 class="text-h6 font-weight-bold mb-0">插件设置</h2>
        </div>
        <p class="text-body-2 text-medium-emphasis mt-1 mb-0">
          这里只保留全局下载与 Telegram 凭据；盘搜和频道请到「渠道管理」维护。
        </p>
      </div>
      <v-btn
        class="save-button"
        color="primary"
        variant="flat"
        prepend-icon="mdi-content-save-outline"
        @click="save">
        保存设置
      </v-btn>
    </div>

    <v-tabs
      v-model="tab"
      density="comfortable"
      show-arrows
      align-tabs="start"
      class="config-tabs">
      <v-tab value="basic">基础</v-tab>
      <v-tab value="download">下载</v-tab>
      <v-tab value="telegram">Telegram</v-tab>
      <v-tab value="advanced">高级</v-tab>
    </v-tabs>

    <v-window v-model="tab" eager class="config-window">
      <v-window-item value="basic">
        <v-card variant="outlined" class="config-card">
          <v-card-title class="card-title">
            <v-icon icon="mdi-tune-variant" size="20" color="primary" />
            <span>基础</span>
          </v-card-title>
          <v-card-subtitle class="card-subtitle">
            控制插件总开关、下载器展示信息与白名单过滤。
          </v-card-subtitle>
          <v-card-text>
            <v-row dense>
              <v-col :cols="isMobile ? 12 : 6">
                <v-switch
                  v-model="form.enabled"
                  label="启用插件"
                  hint="关闭后不参与搜索，MoviePilot 回到原生 BT/PT"
                  color="primary"
                  density="comfortable"
                  hide-details="auto" />
              </v-col>
              <v-col :cols="isMobile ? 12 : 6">
                <v-switch
                  v-model="form.only_whitelist"
                  label="仅白名单关键词生效"
                  hint="开启后只处理命中的关键词"
                  color="primary"
                  density="comfortable"
                  hide-details="auto" />
              </v-col>
              <v-col :cols="isMobile ? 12 : 6">
                <v-text-field
                  v-model="form.downloader_name"
                  label="下载器名称"
                  placeholder="网盘直链"
                  hint="下载页里显示的标签名"
                  variant="outlined"
                  density="comfortable"
                  hide-details="auto" />
              </v-col>
              <v-col :cols="isMobile ? 12 : 6">
                <v-text-field
                  v-model="form.download_root"
                  label="下载根目录"
                  placeholder="/downloads"
                  variant="outlined"
                  density="comfortable"
                  hide-details="auto" />
              </v-col>
              <v-col :cols="isMobile ? 12 : 6">
                <v-text-field
                  v-model.number="form.max_tasks"
                  type="number"
                  min="1"
                  max="6"
                  label="并发下载任务数"
                  hint="建议 1-6，插件会按上下限收敛"
                  variant="outlined"
                  density="comfortable"
                  hide-details="auto"
                  inputmode="numeric" />
              </v-col>
              <v-col :cols="isMobile ? 12 : 6">
                <v-text-field
                  v-model="form.keyword_whitelist"
                  label="关键词白名单"
                  placeholder="多个关键词用英文逗号分隔"
                  variant="outlined"
                  density="comfortable"
                  hide-details="auto" />
              </v-col>
            </v-row>
          </v-card-text>
        </v-card>
      </v-window-item>

      <v-window-item value="download">
        <v-card variant="outlined" class="config-card">
          <v-card-title class="card-title">
            <v-icon icon="mdi-download-network-outline" size="20" color="success" />
            <span>下载</span>
          </v-card-title>
          <v-card-subtitle class="card-subtitle">
            aria2 不可用时会自动回落到内置分片下载。
          </v-card-subtitle>
          <v-card-text>
            <v-row dense>
              <v-col :cols="isMobile ? 12 : 4">
                <v-switch
                  v-model="form.aria2_enabled"
                  label="启用 aria2"
                  hint="使用容器内置 aria2c 多线程下载"
                  color="primary"
                  density="comfortable"
                  hide-details="auto" />
              </v-col>
              <v-col :cols="isMobile ? 12 : 4">
                <v-text-field
                  v-model.number="form.aria2_connections"
                  type="number"
                  min="1"
                  max="128"
                  label="连接数"
                  variant="outlined"
                  density="comfortable"
                  hide-details="auto"
                  inputmode="numeric" />
              </v-col>
              <v-col :cols="isMobile ? 12 : 4">
                <v-text-field
                  v-model.number="form.aria2_split"
                  type="number"
                  min="1"
                  max="128"
                  label="分片数"
                  variant="outlined"
                  density="comfortable"
                  hide-details="auto"
                  inputmode="numeric" />
              </v-col>
              <v-col cols="12">
                <v-switch
                  v-model="form.keep_saved"
                  label="保留网盘副本"
                  hint="关闭时下载成功后清理转存副本，失败时保留"
                  color="primary"
                  density="comfortable"
                  hide-details="auto" />
              </v-col>
            </v-row>
          </v-card-text>
        </v-card>
      </v-window-item>

      <v-window-item value="telegram">
        <v-card variant="outlined" class="config-card">
          <v-card-title class="card-title">
            <v-icon icon="mdi-telegram" size="20" color="info" />
            <span>Telegram 凭据</span>
          </v-card-title>
          <v-card-subtitle class="card-subtitle">
            使用 Telethon 用户会话读取私有频道，凭据不会写入日志。
          </v-card-subtitle>
          <v-card-text>
            <v-row dense>
              <v-col :cols="isMobile ? 12 : 6">
                <v-text-field
                  v-model="form.tg_api_id"
                  label="api_id"
                  placeholder="例如 12345678"
                  variant="outlined"
                  density="comfortable"
                  hide-details="auto"
                  autocomplete="off" />
              </v-col>
              <v-col :cols="isMobile ? 12 : 6">
                <v-text-field
                  v-model="form.tg_api_hash"
                  type="password"
                  label="api_hash"
                  variant="outlined"
                  density="comfortable"
                  hide-details="auto"
                  autocomplete="new-password" />
              </v-col>
              <v-col cols="12">
                <v-text-field
                  v-model="form.tg_session"
                  type="password"
                  label="StringSession"
                  hint="与 api_id / api_hash 配套，留空则不会启动频道监控"
                  variant="outlined"
                  density="comfortable"
                  hide-details="auto"
                  autocomplete="new-password" />
              </v-col>
            </v-row>
          </v-card-text>
        </v-card>
      </v-window-item>

      <v-window-item value="advanced">
        <v-card variant="outlined" class="config-card">
          <v-card-title class="card-title">
            <v-icon icon="mdi-cog-box" size="20" color="primary" />
            <span>高级</span>
          </v-card-title>
          <v-card-subtitle class="card-subtitle">
            一般无需修改；改动后需要保存并重载插件。
          </v-card-subtitle>
          <v-card-text>
            <v-row dense>
              <v-col cols="12">
                <v-text-field
                  v-model="form.mp_base_url"
                  label="MoviePilot 内部地址"
                  placeholder="http://127.0.0.1:3000"
                  hint="用于生成合成种子 URL，一般无需修改"
                  variant="outlined"
                  density="comfortable"
                  hide-details="auto" />
              </v-col>
              <v-col cols="12">
                <v-alert
                  type="info"
                  variant="tonal"
                  density="comfortable"
                  text="盘搜服务地址、Telegram 频道等按渠道配置，请到左侧「渠道管理」维护。"
                />
              </v-col>
            </v-row>
          </v-card-text>
        </v-card>
      </v-window-item>
    </v-window>

    <div class="config-actions">
      <v-btn
        type="submit"
        color="primary"
        variant="flat"
        size="large"
        block
        prepend-icon="mdi-content-save-outline"
        @click="save">
        保存设置
      </v-btn>
    </div>

    <v-snackbar v-model="snackbar.visible" :color="snackbar.color" location="top" timeout="3000">
      {{ snackbar.text }}
    </v-snackbar>
  </div>
</template>

<script setup>
import { reactive, ref, watch } from "vue"
import { useViewport } from "../composables/useViewport"

defineOptions({ name: "QuarkChannelConfig" })

const props = defineProps({
  api: { type: [Object, Function], default: null },
  initialConfig: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: "QuarkChannel" },
})

const emit = defineEmits(["save"])
const { isMobile } = useViewport(600)
const tab = ref("basic")

const defaults = () => ({
  enabled: false,
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
})

const form = reactive(defaults())
const snackbar = reactive({ visible: false, text: "", color: "success" })

function applyConfig(config) {
  const source = config && typeof config === "object" ? config : {}
  Object.assign(form, defaults(), source)
  form.enabled = Boolean(form.enabled)
  form.only_whitelist = Boolean(form.only_whitelist)
  form.aria2_enabled = Boolean(form.aria2_enabled)
  form.keep_saved = Boolean(form.keep_saved)
  form.max_tasks = Number(form.max_tasks || 3)
  form.aria2_connections = Number(form.aria2_connections || 64)
  form.aria2_split = Number(form.aria2_split || 64)
  form.tg_api_id = String(form.tg_api_id ?? "")
  form.tg_api_hash = String(form.tg_api_hash ?? "")
  form.tg_session = String(form.tg_session ?? "")
}

function clamp(value, min, max, fallback) {
  const number = Number(value)
  if (!Number.isFinite(number)) return fallback
  return Math.min(max, Math.max(min, Math.round(number)))
}

function save() {
  const payload = {
    ...props.initialConfig,
    ...form,
    max_tasks: clamp(form.max_tasks, 1, 6, 3),
    aria2_connections: clamp(form.aria2_connections, 1, 128, 64),
    aria2_split: clamp(form.aria_split, 1, 128, 64),
  }
  emit("save", payload)
  snackbar.text = "设置已提交，保存后生效"
  snackbar.color = "success"
  snackbar.visible = true
}

watch(
  () => props.initialConfig,
  (config) => applyConfig(config),
  { immediate: true, deep: true },
)
</script>

<style scoped>
.config-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 12px 16px 24px;
}

.config-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 0 4px;
}

.title-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.config-tabs {
  margin: 0 4px;
}

.config-window {
  margin: 0 4px;
}

.config-card {
  border-radius: 10px;
}

.card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-bottom: 4px;
  font-size: 16px;
}

.card-subtitle {
  padding-top: 0;
  line-height: 1.5;
  white-space: normal;
}

.config-actions {
  margin-top: 18px;
  padding: 0 4px;
}

@media (max-width: 599px) {
  .config-page {
    padding: 12px 12px 24px;
  }

  .config-head {
    flex-direction: column;
    align-items: stretch;
  }

  .save-button {
    width: 100%;
  }
}
</style>
