<template>
  <div class="channel-page">
    <div class="page-head">
      <div class="page-title">
        <div class="title-row">
          <v-icon icon="mdi-rss" color="primary" size="26" />
          <h2 class="text-h6 font-weight-bold mb-0">渠道管理</h2>
        </div>
        <p class="text-body-2 text-medium-emphasis mt-1 mb-0">
          统一维护盘搜与 Telegram 频道，启用后自动参与搜索和增量同步。
        </p>
      </div>
      <v-btn
        class="add-button"
        color="primary"
        variant="flat"
        prepend-icon="mdi-plus"
        @click="openCreate">
        新增渠道
      </v-btn>
    </div>

    <v-progress-linear v-if="loading" indeterminate color="primary" rounded />

    <v-alert v-if="loadError" type="error" variant="tonal" class="mb-1">
      <div class="alert-row">
        <span>{{ loadError }}</span>
        <v-btn variant="text" color="error" size="small" @click="loadData()">重试</v-btn>
      </div>
    </v-alert>

    <v-row dense class="stats-row">
      <v-col v-for="card in statCards" :key="card.label" cols="6" md="3">
        <v-card variant="tonal" :color="card.color" class="stat-card">
          <v-card-text class="stat-content">
            <v-icon :icon="card.icon" size="22" />
            <div class="stat-copy">
              <div class="stat-label">{{ card.label }}</div>
              <div class="stat-value" :class="{ 'stat-value-small': card.small }">{{ card.value }}</div>
            </div>
          </v-card-text>
        </v-card>
      </v-col>
    </v-row>

    <v-card v-if="!loading && !rows.length" variant="outlined" class="empty-card">
      <v-card-text class="empty-content">
        <v-icon icon="mdi-rss-box" size="42" color="primary" />
        <div class="text-subtitle-1 font-weight-medium">还没有渠道</div>
        <div class="text-body-2 text-medium-emphasis">新增盘搜服务或 Telegram 频道后即可参与搜索。</div>
        <v-btn color="primary" variant="tonal" prepend-icon="mdi-plus" @click="openCreate">
          新增第一个渠道
        </v-btn>
      </v-card-text>
    </v-card>

    <v-data-table
      v-if="!isMobile && rows.length"
      :headers="headers"
      :items="rows"
      item-value="id"
      :items-per-page="-1"
      hide-default-footer
      density="compact"
      class="channels-table">
      <template #item.name="{ item }">
        <div class="name-cell">
          <v-avatar :color="typeColor(item.type)" variant="tonal" size="34">
            <v-icon :icon="typeIcon(item.type)" size="18" />
          </v-avatar>
          <div class="name-copy">
            <div class="text-body-2 font-weight-medium text-truncate">{{ item.name }}</div>
            <div class="text-caption text-medium-emphasis text-truncate">{{ channelSubtitle(item) }}</div>
          </div>
        </div>
      </template>
      <template #item.type="{ item }">
        <v-chip :color="typeColor(item.type)" variant="tonal" size="small">
          {{ typeLabel(item.type) }}
        </v-chip>
      </template>
      <template #item.priority="{ item }">
        <span class="text-body-2">{{ item.priority }}</span>
      </template>
      <template #item.enabled="{ item }">
        <v-switch
          :model-value="item.enabled"
          :loading="Boolean(toggling[item.id])"
          color="primary"
          density="compact"
          hide-details
          @update:model-value="toggleEnabled(item, $event)" />
      </template>
      <template #item.last_scan="{ item }">
        <span class="text-body-2">{{ formatTime(item.last_scan) }}</span>
      </template>
      <template #item.hits="{ item }">
        <span class="text-body-2">{{ item.hits }}</span>
      </template>
      <template #item.actions="{ item }">
        <div class="table-actions">
          <v-btn
            icon="mdi-access-point-check"
            size="small"
            variant="text"
            color="primary"
            title="测试渠道"
            :loading="testingId === item.id"
            @click="testChannel(item)" />
          <v-btn
            v-if="item.type === 'tg_channel'"
            icon="mdi-radar"
            size="small"
            variant="text"
            color="info"
            title="立即扫描"
            :loading="scanningId === item.id"
            @click="scanChannel(item)" />
          <v-btn
            icon="mdi-pencil-outline"
            size="small"
            variant="text"
            title="编辑渠道"
            @click="openEdit(item)" />
          <v-btn
            icon="mdi-delete-outline"
            size="small"
            variant="text"
            color="error"
            title="删除渠道"
            @click="askDelete(item)" />
        </div>
      </template>
    </v-data-table>

    <div v-if="isMobile && rows.length" class="mobile-list">
      <v-card v-for="item in rows" :key="item.id" variant="outlined" class="mobile-card">
        <div class="mobile-card-head">
          <div class="name-cell">
            <v-avatar :color="typeColor(item.type)" variant="tonal" size="36">
              <v-icon :icon="typeIcon(item.type)" size="19" />
            </v-avatar>
            <div class="name-copy">
              <div class="mobile-name">{{ item.name }}</div>
              <div class="text-caption text-medium-emphasis">{{ channelSubtitle(item) }}</div>
            </div>
          </div>
          <v-switch
            :model-value="item.enabled"
            :loading="Boolean(toggling[item.id])"
            color="primary"
            density="compact"
            hide-details
            @update:model-value="toggleEnabled(item, $event)" />
        </div>

        <div class="mobile-meta">
          <div class="meta-item">
            <div class="meta-label">类型</div>
            <div class="meta-value">
              <v-chip :color="typeColor(item.type)" variant="tonal" size="small">
                {{ typeLabel(item.type) }}
              </v-chip>
            </div>
          </div>
          <div class="meta-item">
            <div class="meta-label">优先级</div>
            <div class="meta-value">{{ item.priority }}</div>
          </div>
          <div class="meta-item">
            <div class="meta-label">最近扫描</div>
            <div class="meta-value">{{ formatTime(item.last_scan) }}</div>
          </div>
          <div class="meta-item">
            <div class="meta-label">命中</div>
            <div class="meta-value">{{ item.hits }}</div>
          </div>
        </div>

        <v-alert
          v-if="item.last_error"
          type="error"
          density="compact"
          variant="tonal"
          class="mt-3">
          {{ item.last_error }}
        </v-alert>

        <div class="mobile-actions">
          <v-btn
            variant="tonal"
            color="primary"
            size="small"
            prepend-icon="mdi-access-point-check"
            :loading="testingId === item.id"
            @click="testChannel(item)">
            测试
          </v-btn>
          <v-btn
            v-if="item.type === 'tg_channel'"
            variant="tonal"
            color="info"
            size="small"
            prepend-icon="mdi-radar"
            :loading="scanningId === item.id"
            @click="scanChannel(item)">
            扫描
          </v-btn>
          <v-btn
            variant="tonal"
            size="small"
            prepend-icon="mdi-pencil-outline"
            @click="openEdit(item)">
            编辑
          </v-btn>
          <v-btn
            variant="tonal"
            color="error"
            size="small"
            prepend-icon="mdi-delete-outline"
            @click="askDelete(item)">
            删除
          </v-btn>
        </div>
      </v-card>
    </div>

    <v-dialog v-model="dialogVisible" :fullscreen="isMobile" max-width="760" scrollable>
      <v-card class="dialog-card">
        <v-form @submit.prevent="submitChannel">
          <v-card-title class="dialog-title">
            <span>{{ editingId ? "编辑渠道" : "新增渠道" }}</span>
            <v-btn icon="mdi-close" variant="text" size="small" title="关闭" @click="closeDialog" />
          </v-card-title>
          <v-card-subtitle class="dialog-subtitle">
            渠道配置保存后立即刷新运行时，启用状态会同步影响搜索与监控。
          </v-card-subtitle>
          <v-card-text class="dialog-body">
            <v-row dense>
              <v-col cols="12" md="7">
                <v-text-field
                  v-model="form.name"
                  label="渠道名称"
                  placeholder="例如：主盘搜、追更频道"
                  variant="outlined"
                  density="comfortable"
                  hide-details="auto"
                  autocomplete="off" />
              </v-col>
              <v-col cols="12" md="5">
                <v-select
                  v-model="form.type"
                  :items="typeOptions"
                  item-title="title"
                  item-value="value"
                  label="渠道类型"
                  variant="outlined"
                  density="comfortable"
                  hide-details="auto"
                  :disabled="Boolean(editingId)" />
              </v-col>
              <v-col cols="12" md="6">
                <v-text-field
                  v-model.number="form.priority"
                  type="number"
                  label="优先级"
                  hint="数字越大越优先，同优先级按创建顺序排列"
                  variant="outlined"
                  density="comfortable"
                  hide-details="auto"
                  inputmode="numeric" />
              </v-col>
              <v-col cols="12" md="6">
                <v-switch
                  v-model="form.enabled"
                  label="启用渠道"
                  color="primary"
                  density="comfortable"
                  hide-details="auto" />
              </v-col>
            </v-row>

            <template v-if="form.type === 'pansou'">
              <v-divider class="my-4" />
              <div class="form-section-title">
                <v-icon icon="mdi-magnify" size="18" color="primary" />
                <span>盘搜配置</span>
              </div>
              <v-row dense>
                <v-col cols="12">
                  <v-text-field
                    v-model="form.base_url"
                    label="服务地址"
                    placeholder="例如：http://pansou:8888"
                    variant="outlined"
                    density="comfortable"
                    hide-details="auto"
                    autocomplete="off" />
                </v-col>
                <v-col cols="12" md="6">
                  <v-text-field
                    v-model="form.username"
                    label="账号（可选）"
                    variant="outlined"
                    density="comfortable"
                    hide-details="auto"
                    autocomplete="off" />
                </v-col>
                <v-col cols="12" md="6">
                  <v-text-field
                    v-model="form.password"
                    type="password"
                    label="密码（可选）"
                    variant="outlined"
                    density="comfortable"
                    hide-details="auto"
                    autocomplete="new-password" />
                </v-col>
                <v-col cols="12" md="6">
                  <v-text-field
                    v-model="form.token"
                    type="password"
                    label="访问令牌（可选）"
                    variant="outlined"
                    density="comfortable"
                    hide-details="auto"
                    autocomplete="new-password" />
                </v-col>
                <v-col cols="12" md="6">
                  <v-select
                    v-model="form.src"
                    :items="sourceOptions"
                    item-title="title"
                    item-value="value"
                    label="搜索来源"
                    variant="outlined"
                    density="comfortable"
                    hide-details="auto" />
                </v-col>
                <v-col cols="12" md="6">
                  <v-text-field
                    v-model="form.pluginsText"
                    label="指定插件"
                    placeholder="多个插件用逗号分隔"
                    hint="留空表示使用全部插件"
                    variant="outlined"
                    density="comfortable"
                    hide-details="auto" />
                </v-col>
                <v-col cols="12" md="6">
                  <v-text-field
                    v-model="form.cloudTypesText"
                    label="网盘类型"
                    placeholder="例如：quark,115"
                    hint="多个类型用逗号分隔，留空表示全部"
                    variant="outlined"
                    density="comfortable"
                    hide-details="auto" />
                </v-col>
              </v-row>
            </template>

            <template v-else>
              <v-divider class="my-4" />
              <div class="form-section-title">
                <v-icon icon="mdi-television-guide" size="18" color="info" />
                <span>Telegram 频道配置</span>
              </div>
              <v-row dense>
                <v-col cols="12">
                  <v-text-field
                    v-model="form.channel"
                    label="频道标识"
                    placeholder="例如：@channel 或 t.me 链接"
                    variant="outlined"
                    density="comfortable"
                    hide-details="auto"
                    autocomplete="off" />
                </v-col>
                <v-col cols="12" md="6">
                  <v-text-field
                    v-model.number="form.interval_minutes"
                    type="number"
                    min="1"
                    max="1440"
                    label="轮询间隔（分钟）"
                    variant="outlined"
                    density="comfortable"
                    hide-details="auto"
                    inputmode="numeric" />
                </v-col>
                <v-col cols="12" md="6">
                  <v-text-field
                    v-model.number="form.scan_limit"
                    type="number"
                    min="1"
                    max="1000"
                    label="单次扫描条数"
                    variant="outlined"
                    density="comfortable"
                    hide-details="auto"
                    inputmode="numeric" />
                </v-col>
                <v-col cols="12">
                  <v-switch
                    v-model="form.backfill"
                    label="首次接入回补历史"
                    hint="开启后首次同步会把最近消息加入索引"
                    color="primary"
                    density="comfortable"
                    hide-details="auto" />
                </v-col>
              </v-row>
            </template>
          </v-card-text>
          <v-card-actions class="dialog-actions">
            <v-btn variant="text" @click="closeDialog">取消</v-btn>
            <v-btn
              type="submit"
              color="primary"
              variant="flat"
              :loading="submitting"
              :disabled="submitting">
              {{ editingId ? "保存修改" : "新增渠道" }}
            </v-btn>
          </v-card-actions>
        </v-form>
      </v-card>
    </v-dialog>

    <v-dialog v-model="deleteVisible" max-width="420">
      <v-card>
        <v-card-title>删除渠道</v-card-title>
        <v-card-text>
          确认删除「{{ deletingChannel?.name || "该渠道" }}」？删除后不再参与搜索与监控，此操作不可撤销。
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="deleteVisible = false">取消</v-btn>
          <v-btn color="error" variant="flat" :loading="deleting" @click="confirmDelete">删除</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-snackbar v-model="snackbar.visible" :color="snackbar.color" location="top" timeout="3500">
      {{ snackbar.text }}
    </v-snackbar>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from "vue"
import { useViewport } from "../composables/useViewport"

defineOptions({ name: "QuarkChannelAppPage" })

const props = defineProps({
  api: { type: [Object, Function], required: true },
  pluginId: { type: String, default: "QuarkChannel" },
  navKey: { type: String, default: "channels" },
})

const { isMobile } = useViewport(600)
const loading = ref(false)
const loadError = ref("")
const items = ref([])
const summary = reactive({ total: 0, enabled: 0, tg_entries: 0, last_poll: 0 })
const toggling = reactive({})
const testingId = ref("")
const scanningId = ref("")
const dialogVisible = ref(false)
const editingId = ref("")
const submitting = ref(false)
const deleteVisible = ref(false)
const deletingChannel = ref(null)
const deleting = ref(false)
const snackbar = reactive({ visible: false, text: "", color: "success" })

const headers = [
  { title: "名称", key: "name", sortable: true },
  { title: "类型", key: "type", sortable: true },
  { title: "优先级", key: "priority", sortable: true, align: "start" },
  { title: "启用", key: "enabled", sortable: false, align: "center" },
  { title: "最近扫描", key: "last_scan", sortable: false },
  { title: "命中", key: "hits", sortable: false, align: "end" },
  { title: "操作", key: "actions", sortable: false, align: "end" },
]

const typeOptions = [
  { title: "盘搜", value: "pansou" },
  { title: "Telegram 频道", value: "tg_channel" },
]

const sourceOptions = [
  { title: "全部来源", value: "all" },
  { title: "仅 Telegram 频道", value: "tg" },
  { title: "仅插件源", value: "plugin" },
]

const defaultForm = () => ({
  name: "",
  type: "pansou",
  enabled: true,
  priority: 50,
  base_url: "",
  token: "",
  username: "",
  password: "",
  src: "all",
  pluginsText: "",
  cloudTypesText: "",
  channel: "",
  interval_minutes: 10,
  scan_limit: 100,
  backfill: false,
})

const form = reactive(defaultForm())

const rows = computed(() =>
  items.value.map((item) => {
    const stats = item.stats || {}
    return {
      ...item,
      last_scan: Number(stats.last_scan || 0),
      hits: Number(stats.hits || 0),
      last_error: String(stats.last_error || ""),
    }
  }),
)

const statCards = computed(() => [
  { label: "渠道总数", value: summary.total, icon: "mdi-rss", color: "primary" },
  { label: "已启用", value: summary.enabled, icon: "mdi-toggle-switch", color: "success" },
  { label: "TG 索引条数", value: summary.tg_entries, icon: "mdi-database-eye", color: "info" },
  {
    label: "最近同步",
    value: formatTime(summary.last_poll),
    icon: "mdi-clock-check-outline",
    color: "secondary",
    small: true,
  },
])

function notify(text, color = "success") {
  snackbar.text = String(text || "")
  snackbar.color = color
  snackbar.visible = true
}

function unwrap(raw) {
  if (raw?.data && typeof raw.data === "object" && "success" in raw.data) {
    return raw.data
  }
  return raw || {}
}

function request(method, path, data) {
  const fn = props.api?.[method]
  if (typeof fn !== "function") {
    throw new Error("当前宿主不支持该操作")
  }
  return fn.call(props.api, `plugin/${props.pluginId}${path}`, data)
}

function applySummary(data) {
  if (!data || typeof data !== "object") return
  summary.total = Number(data.total ?? summary.total) || 0
  summary.enabled = Number(data.enabled ?? summary.enabled) || 0
  summary.tg_entries = Number(data.tg_entries ?? summary.tg_entries) || 0
  summary.last_poll = Number(data.last_poll ?? summary.last_poll) || 0
}

function syncSummaryFromItems() {
  summary.total = items.value.length
  summary.enabled = items.value.filter((item) => item.enabled).length
}

async function loadData(options = {}) {
  const silent = Boolean(options.silent)
  if (!silent) loading.value = true
  loadError.value = ""
  try {
    const [listResult, summaryResult] = await Promise.allSettled([
      request("get", "/channels"),
      request("get", "/channels/summary"),
    ])
    if (listResult.status !== "fulfilled") {
      throw listResult.reason
    }
    const body = unwrap(listResult.value)
    if (body.success === false) {
      throw new Error(body.message || "读取渠道失败")
    }
    const data = body.data || body
    items.value = Array.isArray(data?.items) ? data.items : []
    if (summaryResult.status === "fulfilled") {
      const summaryBody = unwrap(summaryResult.value)
      applySummary(summaryBody.data || summaryBody)
    } else {
      syncSummaryFromItems()
    }
  } catch (error) {
    loadError.value = `读取渠道失败：${error?.message || error}`
    if (!silent) notify(loadError.value, "error")
  } finally {
    if (!silent) loading.value = false
  }
}

async function refreshSummary() {
  try {
    const body = unwrap(await request("get", "/channels/summary"))
    applySummary(body.data || body)
  } catch (error) {
    syncSummaryFromItems()
  }
}

function typeLabel(type) {
  return type === "tg_channel" ? "TG 频道" : "盘搜"
}

function typeIcon(type) {
  return type === "tg_channel" ? "mdi-television-guide" : "mdi-magnify"
}

function typeColor(type) {
  return type === "tg_channel" ? "info" : "primary"
}

function channelSubtitle(item) {
  if (item.type === "tg_channel") {
    return item.config?.channel || "未配置频道"
  }
  return item.config?.base_url || "未配置服务地址"
}

function formatTime(value) {
  const timestamp = Number(value || 0)
  if (!timestamp) return "尚未同步"
  const date = new Date(timestamp * 1000)
  if (Number.isNaN(date.getTime())) return "尚未同步"
  const pad = (number) => String(number).padStart(2, "0")
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function resetForm() {
  Object.assign(form, defaultForm())
}

function openCreate() {
  editingId.value = ""
  resetForm()
  dialogVisible.value = true
}

function openEdit(item) {
  const channel = items.value.find((row) => row.id === item.id) || item
  editingId.value = channel.id
  const config = channel.config || {}
  Object.assign(form, {
    name: channel.name || "",
    type: channel.type || "pansou",
    enabled: Boolean(channel.enabled),
    priority: Number(channel.priority || 0),
    base_url: config.base_url || "",
    token: config.token || "",
    username: config.username || "",
    password: config.password || "",
    src: config.src || "all",
    pluginsText: Array.isArray(config.plugins) ? config.plugins.join("，") : String(config.plugins || ""),
    cloudTypesText: Array.isArray(config.cloud_types)
      ? config.cloud_types.join("，")
      : String(config.cloud_types || ""),
    channel: config.channel || "",
    interval_minutes: Number(config.interval_minutes || 10),
    scan_limit: Number(config.scan_limit || 100),
    backfill: Boolean(config.backfill),
  })
  dialogVisible.value = true
}

function closeDialog() {
  dialogVisible.value = false
  editingId.value = ""
}

function splitList(value) {
  return String(value || "")
    .split(/[,，\n|]+/)
    .map((item) => item.trim())
    .filter(Boolean)
}

function validateForm() {
  if (!String(form.name || "").trim()) return "请填写渠道名称"
  if (form.type === "pansou" && !String(form.base_url || "").trim()) {
    return "请填写盘搜服务地址"
  }
  if (form.type === "tg_channel" && !String(form.channel || "").trim()) {
    return "请填写 Telegram 频道标识"
  }
  return ""
}

function buildPayload() {
  const config =
    form.type === "pansou"
      ? {
          base_url: String(form.base_url || "").trim(),
          token: String(form.token || ""),
          username: String(form.username || ""),
          password: String(form.password || ""),
          src: form.src || "all",
          plugins: splitList(form.pluginsText),
          cloud_types: splitList(form.cloudTypesText),
        }
      : {
          channel: String(form.channel || "").trim(),
          interval_minutes: Number(form.interval_minutes || 10),
          scan_limit: Number(form.scan_limit || 100),
          backfill: Boolean(form.backfill),
        }
  return {
    name: String(form.name || "").trim(),
    type: form.type,
    enabled: Boolean(form.enabled),
    priority: Number(form.priority || 0),
    config,
  }
}

async function submitChannel() {
  if (submitting.value) return
  const message = validateForm()
  if (message) {
    notify(message, "warning")
    return
  }
  submitting.value = true
  const isEditing = Boolean(editingId.value)
  const path = isEditing ? `/channels/${encodeURIComponent(editingId.value)}` : "/channels"
  try {
    const body = unwrap(await request(isEditing ? "put" : "post", path, buildPayload()))
    if (body.success === false) {
      throw new Error(body.message || "保存失败")
    }
    dialogVisible.value = false
    editingId.value = ""
    notify(isEditing ? "渠道已更新" : "渠道已新增")
    await loadData({ silent: true })
  } catch (error) {
    notify(`保存失败：${error?.message || error}`, "error")
  } finally {
    submitting.value = false
  }
}

function setLocalEnabled(channelId, enabled) {
  const channel = items.value.find((item) => item.id === channelId)
  if (channel) channel.enabled = Boolean(enabled)
}

async function toggleEnabled(item, enabled) {
  const channelId = item.id
  if (toggling[channelId]) return
  const next = Boolean(enabled)
  const previous = Boolean(item.enabled)
  toggling[channelId] = true
  setLocalEnabled(channelId, next)
  try {
    const body = unwrap(
      await request("put", `/channels/${encodeURIComponent(channelId)}`, { enabled: next }),
    )
    if (body.success === false) {
      throw new Error(body.message || "更新失败")
    }
    notify(next ? "渠道已启用" : "渠道已停用")
    await refreshSummary()
  } catch (error) {
    setLocalEnabled(channelId, previous)
    notify(`更新失败：${error?.message || error}`, "error")
  } finally {
    delete toggling[channelId]
  }
}

async function testChannel(item) {
  if (testingId.value) return
  testingId.value = item.id
  try {
    const body = unwrap(await request("post", `/channels/${encodeURIComponent(item.id)}/test`))
    const ok = body.success !== false
    notify(body.message || (ok ? "渠道测试成功" : "渠道测试失败"), ok ? "success" : "error")
  } catch (error) {
    notify(`测试失败：${error?.message || error}`, "error")
  } finally {
    testingId.value = ""
  }
}

async function scanChannel(item) {
  if (scanningId.value || item.type !== "tg_channel") return
  scanningId.value = item.id
  try {
    const body = unwrap(await request("post", `/channels/${encodeURIComponent(item.id)}/scan`))
    const ok = body.success !== false
    notify(body.message || (ok ? "扫描完成" : "扫描失败"), ok ? "success" : "error")
    if (ok) {
      await loadData({ silent: true })
    }
  } catch (error) {
    notify(`扫描失败：${error?.message || error}`, "error")
  } finally {
    scanningId.value = ""
  }
}

function askDelete(item) {
  deletingChannel.value = item
  deleteVisible.value = true
}

async function confirmDelete() {
  const channel = deletingChannel.value
  if (!channel || deleting.value) return
  deleting.value = true
  try {
    const body = unwrap(
      await request("delete", `/channels/${encodeURIComponent(channel.id)}`),
    )
    if (body.success === false) {
      throw new Error(body.message || "删除失败")
    }
    deleteVisible.value = false
    deletingChannel.value = null
    notify("渠道已删除")
    await loadData({ silent: true })
  } catch (error) {
    notify(`删除失败：${error?.message || error}`, "error")
  } finally {
    deleting.value = false
  }
}

watch(
  () => props.pluginId,
  () => loadData(),
)

onMounted(loadData)
</script>

<style scoped>
.channel-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding-bottom: 24px;
}

.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.page-title {
  min-width: 0;
}

.title-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.stats-row {
  margin-top: -4px;
}

.stat-card {
  height: 100%;
}

.stat-content {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px;
}

.stat-copy {
  min-width: 0;
}

.stat-label {
  font-size: 12px;
  opacity: 0.72;
}

.stat-value {
  font-size: 22px;
  line-height: 1.25;
  font-weight: 700;
}

.stat-value-small {
  font-size: 13px;
  font-weight: 600;
  word-break: break-all;
}

.alert-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.empty-card {
  border-style: dashed;
}

.empty-content {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  padding: 40px 18px;
  text-align: center;
}

.channels-table :deep(.v-data-table__td) {
  vertical-align: middle;
}

.name-cell {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.name-copy {
  min-width: 0;
}

.table-actions {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 2px;
  flex-wrap: nowrap;
}

.mobile-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.mobile-card {
  padding: 14px;
}

.mobile-card-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}

.mobile-name {
  max-width: 62vw;
  font-size: 15px;
  font-weight: 600;
  word-break: break-all;
}

.mobile-meta {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin-top: 12px;
}

.meta-item {
  min-width: 0;
  padding: 8px 10px;
  border-radius: 8px;
  background: rgba(var(--v-theme-on-surface), 0.04);
}

.meta-label {
  font-size: 12px;
  color: rgba(var(--v-theme-on-surface), 0.62);
}

.meta-value {
  margin-top: 2px;
  font-size: 13px;
  word-break: break-all;
}

.mobile-actions {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin-top: 14px;
}

.dialog-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 4px;
}

.dialog-subtitle {
  padding-top: 0;
  line-height: 1.5;
  white-space: normal;
}

.dialog-body {
  padding-top: 10px;
}

.form-section-title {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 10px;
  font-size: 14px;
  font-weight: 600;
}

.dialog-actions {
  padding: 12px 24px 20px;
}

@media (max-width: 599px) {
  .page-head {
    flex-direction: column;
    align-items: stretch;
  }

  .add-button {
    width: 100%;
  }

  .dialog-body {
    padding-left: 16px;
    padding-right: 16px;
  }

  .dialog-actions {
    padding-left: 16px;
    padding-right: 16px;
  }
}
</style>
