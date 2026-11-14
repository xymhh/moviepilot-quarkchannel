<template>
  <div class="channel-page" :class="{ 'channel-page--batch-mobile': isMobile && selectedIds.length }">
    <div class="toolbar" :class="{ 'toolbar--mobile': isMobile }">
      <div class="toolbar-filters">
        <v-text-field
          v-model="search"
          class="search-field"
          density="compact"
          variant="outlined"
          hide-details
          clearable
          prepend-inner-icon="mdi-magnify"
          placeholder="搜索名称、类型、频道或地址"
          aria-label="搜索渠道" />
        <v-btn-toggle
          v-model="statusFilter"
          mandatory
          divided
          color="primary"
          density="comfortable"
          variant="outlined"
          class="status-toggle">
          <v-btn value="all">全部</v-btn>
          <v-btn value="enabled">已启用</v-btn>
          <v-btn value="disabled">已停用</v-btn>
        </v-btn-toggle>
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

    <v-slide-y-transition>
      <div
        v-if="selectedIds.length"
        class="batch-bar"
        :class="{ 'batch-bar--mobile': isMobile }">
        <div class="batch-count">已选 {{ selectedIds.length }} 项</div>
        <div class="batch-actions">
          <v-btn
            size="small"
            variant="tonal"
            color="success"
            prepend-icon="mdi-check-circle-outline"
            :loading="batchBusy && batchAction === 'enable'"
            :disabled="batchBusy"
            @click="batchSetEnabled(true)">
            启用
          </v-btn>
          <v-btn
            size="small"
            variant="tonal"
            color="warning"
            prepend-icon="mdi-stop-circle-outline"
            :loading="batchBusy && batchAction === 'disable'"
            :disabled="batchBusy"
            @click="batchSetEnabled(false)">
            停用
          </v-btn>
          <v-btn size="small" variant="text" :disabled="batchBusy" @click="clearSelection">
            清空选择
          </v-btn>
        </div>
      </div>
    </v-slide-y-transition>

    <div v-if="!loading && !filteredRows.length" class="empty-state">
      <v-icon :icon="rows.length ? 'mdi-filter-off-outline' : 'mdi-rss'" size="46" color="primary" />
      <div class="empty-title">
        {{ rows.length ? "没有匹配的渠道" : "还没有渠道，先新增一个" }}
      </div>
      <div class="empty-hint">
        {{ rows.length ? "试试调整搜索关键词或状态筛选。" : "新增盘搜服务或 Telegram 频道后即可参与搜索。" }}
      </div>
      <v-btn
        v-if="!rows.length"
        color="primary"
        variant="tonal"
        prepend-icon="mdi-plus"
        @click="openCreate">
        新增渠道
      </v-btn>
      <v-btn v-else variant="text" @click="resetFilters">清空筛选</v-btn>
    </div>

    <v-row v-else dense class="channel-grid">
      <v-col v-for="item in filteredRows" :key="item.id" cols="12" sm="6" md="4" lg="3">
        <v-card
          variant="outlined"
          class="channel-card"
          :class="{
            'channel-card--selected': isSelected(item.id),
            'channel-card--disabled': !item.enabled,
          }">
          <div class="card-head">
            <v-checkbox-btn
              :model-value="isSelected(item.id)"
              class="card-checkbox"
              color="primary"
              density="compact"
              hide-details
              :aria-label="`选择渠道 ${item.name}`"
              @click.stop
              @update:model-value="toggleSelect(item.id, $event)" />
            <div class="card-heading" @click="openDetail(item)">
              <div class="card-title-line">
                <v-tooltip :text="item.name" location="top" open-delay="400" max-width="280">
                  <template #activator="{ props: tooltipProps }">
                    <span v-bind="tooltipProps" class="card-title">{{ item.name }}</span>
                  </template>
                </v-tooltip>
                <v-chip :color="typeColor(item.type)" variant="tonal" size="x-small" class="type-chip">
                  {{ typeLabel(item.type) }}
                </v-chip>
              </div>
            </div>
          </div>

          <div class="card-body" @click="openDetail(item)">
            <div class="endpoint" :title="endpointFull(item)">{{ endpointText(item) }}</div>
            <div class="card-meta-line">优先级 {{ item.priority }}</div>
          </div>

          <div class="status-row">
            <v-chip
              :color="item.enabled ? 'success' : undefined"
              :variant="item.enabled ? 'tonal' : 'outlined'"
              size="small">
              {{ item.enabled ? "已启用" : "已停用" }}
            </v-chip>
            <span class="status-item">
              <v-icon icon="mdi-clock-outline" size="13" />
              {{ formatRelativeTime(item.last_scan) }}
            </span>
            <span class="status-item">
              <v-icon icon="mdi-target" size="13" />
              命中 {{ item.hits }}
            </span>
            <v-tooltip
              v-if="item.last_error"
              :text="item.last_error"
              location="top"
              max-width="300">
              <template #activator="{ props: tooltipProps }">
                <v-chip
                  v-bind="tooltipProps"
                  color="error"
                  variant="tonal"
                  size="small"
                  prepend-icon="mdi-alert-circle-outline"
                  class="error-chip">
                  异常
                </v-chip>
              </template>
            </v-tooltip>
          </div>

          <v-divider class="card-divider" />

          <div class="card-actions">
            <v-tooltip text="测试渠道" location="top">
              <template #activator="{ props: tooltipProps }">
                <v-btn
                  v-bind="tooltipProps"
                  icon="mdi-access-point"
                  size="small"
                  variant="text"
                  color="primary"
                  :loading="testingId === item.id"
                  @click.stop="testChannel(item)" />
              </template>
            </v-tooltip>
            <v-tooltip
              :text="item.type === 'tg_channel' ? '立即扫描' : '盘搜渠道不支持扫描'"
              location="top">
              <template #activator="{ props: tooltipProps }">
                <span v-bind="tooltipProps" class="action-slot">
                  <v-btn
                    icon="mdi-radar"
                    size="small"
                    variant="text"
                    color="info"
                    :disabled="item.type !== 'tg_channel'"
                    :loading="scanningId === item.id"
                    @click.stop="scanChannel(item)" />
                </span>
              </template>
            </v-tooltip>
            <v-tooltip text="编辑渠道" location="top">
              <template #activator="{ props: tooltipProps }">
                <v-btn
                  v-bind="tooltipProps"
                  icon="mdi-pencil"
                  size="small"
                  variant="text"
                  @click.stop="openEdit(item)" />
              </template>
            </v-tooltip>
            <v-tooltip text="删除渠道" location="top">
              <template #activator="{ props: tooltipProps }">
                <v-btn
                  v-bind="tooltipProps"
                  icon="mdi-delete"
                  size="small"
                  variant="text"
                  color="error"
                  @click.stop="askDelete(item)" />
              </template>
            </v-tooltip>
          </div>
        </v-card>
      </v-col>
    </v-row>

    <v-navigation-drawer
      v-model="detailOpen"
      temporary
      location="right"
      :width="isMobile ? '88vw' : 380"
      class="detail-drawer">
      <div class="detail-drawer-inner">
        <div class="drawer-head">
          <div class="drawer-heading">
            <div class="drawer-kicker">渠道详情</div>
            <div class="drawer-title">{{ detailChannel?.name || "渠道" }}</div>
          </div>
          <v-btn icon="mdi-close" variant="text" size="small" title="关闭" @click="detailOpen = false" />
        </div>
        <v-divider />
        <div class="drawer-body">
          <div v-if="detailChannel" class="drawer-chips">
            <v-chip :color="typeColor(detailChannel.type)" variant="tonal" size="small">
              {{ typeLabel(detailChannel.type) }}
            </v-chip>
            <v-chip
              :color="detailChannel.enabled ? 'success' : undefined"
              :variant="detailChannel.enabled ? 'tonal' : 'outlined'"
              size="small">
              {{ detailChannel.enabled ? "已启用" : "已停用" }}
            </v-chip>
          </div>
          <div v-if="detailChannel" class="detail-list">
            <div v-for="row in detailRows" :key="row.label" class="detail-item">
              <div class="detail-label">{{ row.label }}</div>
              <div class="detail-value">{{ row.value }}</div>
            </div>
          </div>
        </div>
        <v-divider />
        <div class="drawer-actions">
          <v-btn variant="text" @click="detailOpen = false">关闭</v-btn>
          <v-btn color="primary" variant="flat" prepend-icon="mdi-pencil" @click="editFromDetail">
            编辑
          </v-btn>
        </div>
      </div>
    </v-navigation-drawer>

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
const search = ref("")
const statusFilter = ref("all")
const selectedIds = ref([])
const batchBusy = ref(false)
const batchAction = ref("")
const detailOpen = ref(false)
const detailChannel = ref(null)
const testingId = ref("")
const scanningId = ref("")
const dialogVisible = ref(false)
const editingId = ref("")
const submitting = ref(false)
const deleteVisible = ref(false)
const deletingChannel = ref(null)
const deleting = ref(false)
const snackbar = reactive({ visible: false, text: "", color: "success" })

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

const filteredRows = computed(() => {
  const keyword = search.value.trim().toLowerCase()
  return rows.value.filter((item) => {
    if (statusFilter.value === "enabled" && !item.enabled) return false
    if (statusFilter.value === "disabled" && item.enabled) return false
    if (!keyword) return true
    const config = item.config || {}
    return [
      item.name,
      item.type,
      typeLabel(item.type),
      config.channel,
      config.base_url,
    ].some((value) => String(value || "").toLowerCase().includes(keyword))
  })
})

const detailRows = computed(() => {
  const item = detailChannel.value
  if (!item) return []
  const config = item.config || {}
  const result = [
    { label: "名称", value: item.name || "未命名" },
    { label: "类型", value: typeLabel(item.type) },
    { label: "状态", value: item.enabled ? "已启用" : "已停用" },
    { label: "优先级", value: String(item.priority ?? 0) },
  ]
  if (item.type === "pansou") {
    result.push(
      { label: "服务地址", value: config.base_url || "未配置" },
      { label: "搜索来源", value: sourceLabel(config.src) },
      { label: "账号", value: config.username || "未配置" },
      { label: "访问令牌", value: config.token ? "已配置" : "未配置" },
      { label: "指定插件", value: listText(config.plugins) },
      { label: "网盘类型", value: listText(config.cloud_types) },
    )
  } else {
    result.push(
      { label: "频道标识", value: config.channel || "未配置" },
      { label: "轮询间隔", value: `${Number(config.interval_minutes || 10)} 分钟` },
      { label: "单次扫描", value: `${Number(config.scan_limit || 100)} 条` },
      { label: "回补历史", value: config.backfill ? "已开启" : "已关闭" },
    )
  }
  result.push(
    { label: "最近扫描", value: formatRelativeTime(item.last_scan) },
    { label: "命中次数", value: String(item.hits) },
  )
  if (item.last_error) {
    result.push({ label: "最近错误", value: item.last_error })
  }
  return result
})

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

function pruneSelection() {
  const valid = new Set(items.value.map((item) => item.id))
  selectedIds.value = selectedIds.value.filter((id) => valid.has(id))
}

async function loadData(options = {}) {
  const silent = Boolean(options.silent)
  if (!silent) loading.value = true
  loadError.value = ""
  try {
    const body = unwrap(await request("get", "/channels"))
    if (body.success === false) {
      throw new Error(body.message || "读取渠道失败")
    }
    const data = body.data || body
    items.value = Array.isArray(data?.items) ? data.items : []
    pruneSelection()
    if (detailChannel.value) {
      const fresh = rows.value.find((item) => item.id === detailChannel.value.id)
      if (fresh) {
        detailChannel.value = fresh
      } else {
        detailOpen.value = false
        detailChannel.value = null
      }
    }
  } catch (error) {
    loadError.value = `读取渠道失败：${error?.message || error}`
    if (!silent) notify(loadError.value, "error")
  } finally {
    if (!silent) loading.value = false
  }
}

function typeLabel(type) {
  return type === "tg_channel" ? "TG 频道" : "盘搜"
}

function typeColor(type) {
  return type === "tg_channel" ? "info" : "primary"
}

function sourceLabel(value) {
  return sourceOptions.find((option) => option.value === value)?.title || "全部来源"
}

function listText(value) {
  const list = Array.isArray(value) ? value.filter(Boolean) : []
  return list.length ? list.join("，") : "全部"
}

function endpointFull(item) {
  if (item.type === "tg_channel") {
    return item.config?.channel || "未配置频道"
  }
  return item.config?.base_url || "未配置服务地址"
}

function ellipsisMiddle(value, maxLength = 30) {
  const text = String(value || "")
  if (text.length <= maxLength) return text
  const keep = Math.max(1, Math.floor((maxLength - 1) / 2))
  return `${text.slice(0, keep)}…${text.slice(-keep)}`
}

function endpointText(item) {
  const text = endpointFull(item)
  return item.type === "tg_channel" ? ellipsisMiddle(text) : text
}

function formatRelativeTime(value) {
  const timestamp = Number(value || 0)
  if (!timestamp) return "从未"
  const diff = Math.max(0, Math.floor(Date.now() / 1000 - timestamp))
  if (diff < 60) return "刚刚"
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  return `${Math.floor(diff / 3600)} 小时前`
}

function isSelected(channelId) {
  return selectedIds.value.includes(channelId)
}

function toggleSelect(channelId, selected) {
  if (selected) {
    if (!selectedIds.value.includes(channelId)) {
      selectedIds.value = [...selectedIds.value, channelId]
    }
    return
  }
  selectedIds.value = selectedIds.value.filter((id) => id !== channelId)
}

function clearSelection() {
  selectedIds.value = []
}

function resetFilters() {
  search.value = ""
  statusFilter.value = "all"
}

function openDetail(item) {
  detailChannel.value = item
  detailOpen.value = true
}

function editFromDetail() {
  const item = detailChannel.value
  detailOpen.value = false
  if (item) openEdit(item)
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

async function batchSetEnabled(enabled) {
  if (batchBusy.value || !selectedIds.value.length) return
  batchBusy.value = true
  batchAction.value = enabled ? "enable" : "disable"
  const ids = [...selectedIds.value]
  let success = 0
  let failed = 0
  for (const channelId of ids) {
    try {
      const body = unwrap(
        await request("put", `/channels/${encodeURIComponent(channelId)}`, { enabled }),
      )
      if (body.success === false) {
        throw new Error(body.message || "更新失败")
      }
      setLocalEnabled(channelId, enabled)
      success += 1
    } catch (error) {
      failed += 1
    }
  }
  batchBusy.value = false
  batchAction.value = ""
  notify(
    `批量${enabled ? "启用" : "停用"}完成：成功 ${success} / 失败 ${failed}`,
    failed ? "warning" : "success",
  )
  await loadData({ silent: true })
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
    if (detailChannel.value?.id === channel.id) {
      detailOpen.value = false
      detailChannel.value = null
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
  gap: 14px;
  padding-bottom: 24px;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
}

.toolbar-filters {
  display: flex;
  align-items: center;
  gap: 12px;
  flex: 1;
  min-width: 0;
}

.search-field {
  flex: 1 1 280px;
  max-width: 380px;
  min-width: 200px;
}

.status-toggle {
  flex: 0 0 auto;
}

.add-button {
  flex: 0 0 auto;
}

.alert-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.batch-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  border: 1px solid rgba(var(--v-theme-primary), 0.22);
  border-radius: 10px;
  background: rgba(var(--v-theme-primary), 0.08);
}

.batch-count {
  font-size: 14px;
  font-weight: 600;
}

.batch-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  padding: 54px 18px;
  border: 1px dashed rgba(var(--v-theme-on-surface), 0.24);
  border-radius: 12px;
  text-align: center;
}

.empty-title {
  font-size: 16px;
  font-weight: 600;
}

.empty-hint {
  max-width: 360px;
  font-size: 13px;
  line-height: 1.6;
  color: rgba(var(--v-theme-on-surface), 0.64);
}

.channel-grid {
  margin-top: -4px;
}

.channel-card {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
  transition:
    transform 0.18s ease,
    box-shadow 0.18s ease,
    border-color 0.18s ease;
}

.channel-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 8px 22px rgba(0, 0, 0, 0.09);
}

.channel-card--selected {
  border-color: rgb(var(--v-theme-primary)) !important;
  box-shadow: 0 0 0 1px rgba(var(--v-theme-primary), 0.4);
}

.channel-card--disabled .card-body,
.channel-card--disabled .status-row {
  opacity: 0.78;
}

.card-head {
  display: flex;
  align-items: flex-start;
  gap: 2px;
  padding: 8px 8px 0;
}

.card-checkbox {
  flex: 0 0 auto;
  margin-top: -2px;
}

.card-heading {
  flex: 1;
  min-width: 0;
  padding: 6px 8px 0 0;
  cursor: pointer;
}

.card-title-line {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.card-title {
  display: block;
  flex: 1;
  min-width: 0;
  overflow: hidden;
  font-size: 15px;
  font-weight: 600;
  line-height: 1.4;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.type-chip {
  flex: 0 0 auto;
}

.card-body {
  min-height: 42px;
  padding: 6px 14px 0 42px;
  cursor: pointer;
}

.endpoint {
  overflow: hidden;
  font-size: 12px;
  line-height: 1.45;
  color: rgba(var(--v-theme-on-surface), 0.7);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.card-meta-line {
  margin-top: 4px;
  font-size: 12px;
  color: rgba(var(--v-theme-on-surface), 0.55);
}

.status-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  padding: 10px 14px 10px 42px;
}

.status-item {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 12px;
  color: rgba(var(--v-theme-on-surface), 0.64);
}

.error-chip {
  cursor: help;
}

.card-divider {
  margin-top: auto;
}

.card-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 2px;
  padding: 2px 6px;
}

.action-slot {
  display: inline-flex;
}

.detail-drawer-inner {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.drawer-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 16px;
}

.drawer-heading {
  min-width: 0;
}

.drawer-kicker {
  font-size: 12px;
  color: rgba(var(--v-theme-on-surface), 0.6);
}

.drawer-title {
  margin-top: 2px;
  overflow: hidden;
  font-size: 17px;
  font-weight: 700;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.drawer-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
}

.drawer-chips {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}

.detail-list {
  border-top: 1px solid rgba(var(--v-theme-on-surface), 0.1);
}

.detail-item {
  display: grid;
  grid-template-columns: 92px minmax(0, 1fr);
  gap: 12px;
  padding: 10px 0;
  border-bottom: 1px solid rgba(var(--v-theme-on-surface), 0.08);
}

.detail-label {
  font-size: 12px;
  color: rgba(var(--v-theme-on-surface), 0.6);
}

.detail-value {
  font-size: 13px;
  line-height: 1.5;
  word-break: break-word;
}

.drawer-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 16px calc(12px + env(safe-area-inset-bottom, 0px));
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
  .toolbar--mobile {
    flex-direction: column;
    align-items: stretch;
  }

  .toolbar--mobile .toolbar-filters {
    flex-direction: column;
    align-items: stretch;
  }

  .toolbar--mobile .search-field {
    max-width: none;
    min-width: 0;
    width: 100%;
  }

  .toolbar--mobile .status-toggle {
    width: 100%;
  }

  .toolbar--mobile .status-toggle :deep(.v-btn) {
    flex: 1;
  }

  .toolbar--mobile .add-button {
    width: 100%;
  }

  .batch-bar--mobile {
    position: fixed;
    right: 0;
    bottom: 0;
    left: 0;
    z-index: 6;
    margin: 0 8px;
    padding: 12px 14px calc(12px + env(safe-area-inset-bottom, 0px));
    border-radius: 14px 14px 0 0;
    box-shadow: 0 -6px 24px rgba(0, 0, 0, 0.18);
  }

  .batch-bar--mobile .batch-actions {
    justify-content: flex-end;
  }

  .channel-page--batch-mobile {
    padding-bottom: 96px;
  }

  .detail-item {
    grid-template-columns: 82px minmax(0, 1fr);
  }

  .dialog-body {
    padding-right: 16px;
    padding-left: 16px;
  }

  .dialog-actions {
    padding-right: 16px;
    padding-left: 16px;
  }
}
</style>
