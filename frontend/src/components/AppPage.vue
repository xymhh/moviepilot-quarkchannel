<template>
  <div class="qc-page">
    <v-tabs v-model="tab" density="compact" class="mb-2">
      <v-tab value="channels">渠道</v-tab>
      <v-tab value="search">搜索下载</v-tab>
    </v-tabs>

    <div v-if="tab === 'channels'">
    <v-row dense class="mb-2">
      <v-col cols="6" sm="3">
        <v-chip size="small" variant="tonal" color="primary" prepend-icon="mdi-rss">
          渠道 {{ summary.total }}
        </v-chip>
      </v-col>
      <v-col cols="6" sm="3">
        <v-chip size="small" variant="tonal" color="success" prepend-icon="mdi-check-circle">
          启用 {{ summary.enabled }}
        </v-chip>
      </v-col>
      <v-col cols="6" sm="3">
        <v-chip size="small" variant="tonal" prepend-icon="mdi-database">
          索引 {{ summary.tg_entries }}
        </v-chip>
      </v-col>
      <v-col cols="6" sm="3">
        <v-chip size="small" variant="tonal" prepend-icon="mdi-clock-outline">
          {{ lastPollText }}
        </v-chip>
      </v-col>
    </v-row>

    <v-row dense align="center" class="mb-2">
      <v-col cols="12" md="6">
        <v-text-field
          v-model="search"
          density="compact"
          variant="outlined"
          hide-details
          clearable
          prepend-inner-icon="mdi-magnify"
          placeholder="搜索名称、类型、频道或地址"
        />
      </v-col>
      <v-col cols="12" md="6">
        <div class="qc-toolbar">
          <v-btn-toggle v-model="statusFilter" density="compact" variant="outlined" divided>
            <v-btn value="all" size="small">全部</v-btn>
            <v-btn value="enabled" size="small">已启用</v-btn>
            <v-btn value="disabled" size="small">已停用</v-btn>
          </v-btn-toggle>
          <v-btn color="primary" size="small" prepend-icon="mdi-plus" @click="openCreate">
            新增
          </v-btn>
        </div>
      </v-col>
    </v-row>

    <v-expand-transition>
      <v-card v-if="selected.size" variant="tonal" color="primary" class="mb-2 pa-2">
        <div class="d-flex align-center flex-wrap ga-2">
          <span class="text-caption">已选 {{ selected.size }} 项</span>
          <v-spacer />
          <v-btn size="small" variant="text" @click="bulkToggle(true)">启用</v-btn>
          <v-btn size="small" variant="text" @click="bulkToggle(false)">停用</v-btn>
          <v-btn size="small" variant="text" @click="selected.clear()">清空</v-btn>
        </div>
      </v-card>
    </v-expand-transition>

    <v-row v-if="filtered.length" dense>
      <v-col v-for="item in filtered" :key="item.id" cols="12" sm="6" md="4" lg="3">
        <v-card variant="outlined" class="qc-card">
          <v-card-item class="pb-1">
            <template #prepend>
              <v-checkbox-btn
                :model-value="selected.has(item.id)"
                density="compact"
                hide-details
                @update:model-value="toggleSelect(item.id, $event)"
              />
            </template>
            <v-card-title class="qc-title" :title="item.name" @click="openDetail(item)">
              {{ item.name }}
            </v-card-title>
            <template #append>
              <v-chip size="x-small" :color="typeColor(item.type)" variant="flat">
                {{ typeLabel(item.type) }}
              </v-chip>
            </template>
          </v-card-item>
          <v-card-text class="py-1 qc-body" @click="openDetail(item)">
            <div class="text-caption text-truncate">{{ channelLine(item) }}</div>
            <div class="text-caption text-medium-emphasis">优先级 {{ item.priority }}</div>
          </v-card-text>
          <v-card-text class="py-1">
            <div class="d-flex flex-wrap ga-1">
              <v-chip size="x-small" :color="item.enabled ? 'success' : 'default'" variant="tonal">
                {{ item.enabled ? "已启用" : "已停用" }}
              </v-chip>
              <v-chip size="x-small" variant="tonal" prepend-icon="mdi-clock-outline">
                {{ relativeTime(item.stats?.last_scan) }}
              </v-chip>
              <v-chip size="x-small" variant="tonal">命中 {{ item.stats?.hits || 0 }}</v-chip>
              <v-chip
                v-if="item.stats?.last_error"
                size="x-small"
                color="error"
                variant="tonal"
                :title="item.stats.last_error"
              >
                异常
              </v-chip>
            </div>
          </v-card-text>
          <v-card-actions class="pt-0">
            <v-switch
              :model-value="item.enabled"
              density="compact"
              hide-details
              color="success"
              class="qc-switch"
              @update:model-value="setEnabled(item, $event)"
            />
            <v-spacer />
            <v-btn
              icon="mdi-access-point"
              size="small"
              variant="text"
              title="测试"
              @click="testChannel(item)"
            />
            <v-btn
              icon="mdi-radar"
              size="small"
              variant="text"
              title="立即扫描"
              :disabled="item.type !== 'tg_channel'"
              @click="scanChannel(item)"
            />
            <v-btn icon="mdi-pencil" size="small" variant="text" title="编辑" @click="openEdit(item)" />
            <v-btn
              icon="mdi-delete"
              size="small"
              variant="text"
              color="error"
              title="删除"
              @click="askDelete(item)"
            />
          </v-card-actions>
        </v-card>
      </v-col>
    </v-row>

    <v-card v-else variant="tonal" class="pa-6 text-center">
      <v-icon icon="mdi-rss" size="40" class="mb-2" />
      <div class="text-body-2 mb-3">
        {{ channels.length ? "没有匹配的渠道" : "还没有渠道，先新增一个" }}
      </div>
      <v-btn color="primary" size="small" prepend-icon="mdi-plus" @click="openCreate">新增渠道</v-btn>
    </v-card>
    </div>

    <div v-else-if="tab === 'search'">
      <v-row dense align="center" class="mb-2">
        <v-col cols="12" md="9">
          <v-text-field
            v-model="downloadSearch"
            density="compact"
            variant="outlined"
            hide-details
            clearable
            prepend-inner-icon="mdi-magnify"
            placeholder="输入片名，搜索所有已启用渠道"
            @keyup.enter="searchShares"
          />
        </v-col>
        <v-col cols="12" md="3">
          <v-btn
            block
            color="primary"
            prepend-icon="mdi-magnify"
            :loading="searching"
            @click="searchShares"
          >
            搜索
          </v-btn>
        </v-col>
      </v-row>

      <div v-if="searching" class="py-10 text-center">
        <v-progress-circular indeterminate color="primary" />
        <div class="text-caption mt-3">正在搜索各渠道…</div>
      </div>

      <v-row v-else-if="shares.length" dense>
        <v-col v-for="share in shares" :key="share.sid" cols="12" sm="6" md="4" lg="3">
          <v-card variant="outlined" class="qc-share-card">
            <v-card-item class="pb-1">
              <v-card-title class="qc-title" :title="share.title">
                {{ share.title || "未命名分享" }}
              </v-card-title>
              <template #append>
                <v-chip size="x-small" variant="tonal">{{ share.site_name || "渠道" }}</v-chip>
              </template>
            </v-card-item>
            <v-card-text class="py-1">
              <div class="d-flex flex-wrap ga-1">
                <v-chip size="x-small" variant="tonal" prepend-icon="mdi-folder-outline">
                  {{ share.files || 0 }} 个文件
                </v-chip>
                <v-chip size="x-small" variant="tonal" prepend-icon="mdi-harddisk">
                  {{ formatSize(share.size) }}
                </v-chip>
              </div>
            </v-card-text>
            <v-spacer />
            <v-card-actions class="pt-1">
              <v-btn
                size="small"
                variant="tonal"
                color="primary"
                prepend-icon="mdi-format-list-bulleted"
                @click="openEpisodes(share)"
              >
                选集
              </v-btn>
              <v-spacer />
              <v-btn
                size="small"
                variant="text"
                prepend-icon="mdi-download"
                :loading="downloadingSid === share.sid"
                @click="downloadWhole(share)"
              >
                整包
              </v-btn>
            </v-card-actions>
          </v-card>
        </v-col>
      </v-row>

      <v-card v-else-if="searchDone" variant="tonal" class="pa-6 text-center">
        <v-icon icon="mdi-magnify-close" size="38" class="mb-2" />
        <div class="text-body-2">没有找到分享，换个关键词试试</div>
      </v-card>

      <v-card v-else variant="tonal" class="pa-6 text-center">
        <v-icon icon="mdi-cloud-search-outline" size="38" class="mb-2" />
        <div class="text-body-2 mb-1">输入片名后搜索</div>
        <div class="text-caption text-medium-emphasis">
          一个分享只显示一条，点「选集」再勾选需要的集
        </div>
      </v-card>
    </div>

    <v-navigation-drawer
      v-if="detail"
      :model-value="detailOpen"
      temporary
      location="right"
      :width="isMobile ? undefined : 380"
      @update:model-value="detailOpen = $event"
    >
      <div class="d-flex align-center pa-3">
        <div class="text-subtitle-1">渠道详情</div>
        <v-spacer />
        <v-btn icon="mdi-close" size="small" variant="text" @click="detailOpen = false" />
      </div>
      <v-divider />
      <v-list density="compact">
        <v-list-item title="名称" :subtitle="detail.name" />
        <v-list-item title="类型" :subtitle="typeLabel(detail.type)" />
        <v-list-item title="状态" :subtitle="detail.enabled ? '已启用' : '已停用'" />
        <v-list-item title="优先级" :subtitle="String(detail.priority)" />
        <v-list-item
          v-if="detail.type === 'pansou'"
          title="服务地址"
          :subtitle="detail.config?.base_url || '-'"
        />
        <v-list-item
          v-if="detail.type === 'pansou'"
          title="搜索来源"
          :subtitle="detail.config?.src || 'all'"
        />
        <v-list-item v-if="detail.type === 'tg_channel'" title="频道" :subtitle="detail.config?.channel || '-'" />
        <v-list-item
          v-if="detail.type === 'tg_channel'"
          title="轮询间隔"
          :subtitle="`${detail.config?.interval_minutes || 10} 分钟`"
        />
        <v-list-item
          v-if="detail.type === 'tg_channel'"
          title="单次扫描"
          :subtitle="`${detail.config?.scan_limit || 100} 条`"
        />
        <v-list-item title="命中" :subtitle="String(detail.stats?.hits || 0)" />
        <v-list-item title="最近扫描" :subtitle="relativeTime(detail.stats?.last_scan)" />
        <v-list-item v-if="detail.stats?.last_error" title="最近错误" :subtitle="detail.stats.last_error" />
      </v-list>
      <div class="pa-3 d-flex ga-2">
        <v-btn variant="text" @click="detailOpen = false">关闭</v-btn>
        <v-spacer />
        <v-btn color="primary" @click="editFromDetail">编辑</v-btn>
      </div>
    </v-navigation-drawer>

    <v-navigation-drawer
      v-if="episodeShare"
      :model-value="episodeOpen"
      temporary
      location="right"
      :width="isMobile ? '100%' : 420"
      @update:model-value="episodeOpen = $event"
    >
      <div class="d-flex align-center pa-3">
        <div class="qc-drawer-heading">
          <div class="text-subtitle-1">选集下载</div>
          <div class="text-caption text-medium-emphasis qc-drawer-subtitle" :title="episodeShare.title">
            {{ episodeShare.title }}
          </div>
        </div>
        <v-spacer />
        <v-btn icon="mdi-close" size="small" variant="text" @click="episodeOpen = false" />
      </div>
      <v-divider />
      <div class="px-3 py-1 d-flex align-center flex-wrap ga-1">
        <v-btn size="x-small" variant="text" @click="selectAllEpisodes">全选</v-btn>
        <v-btn size="x-small" variant="text" @click="invertEpisodes">反选</v-btn>
        <v-btn size="x-small" variant="text" @click="clearEpisodes">清空</v-btn>
        <v-spacer />
        <span class="text-caption">已选 {{ episodeChecked.size }} 项</span>
      </div>
      <v-divider />
      <div v-if="episodesLoading" class="py-10 text-center">
        <v-progress-circular indeterminate color="primary" />
        <div class="text-caption mt-2">正在读取文件…</div>
      </div>
      <v-list v-else-if="episodes.length" density="compact" class="qc-episode-list">
        <v-list-item v-for="file in episodes" :key="fileKey(file)">
          <template #prepend>
            <v-checkbox-btn
              :model-value="episodeChecked.has(fileKey(file))"
              density="compact"
              hide-details
              @update:model-value="toggleEpisode(file, $event)"
            />
          </template>
          <v-list-item-title class="qc-episode-name" :title="file.name">
            <v-chip
              v-if="file.episode"
              size="x-small"
              variant="tonal"
              color="primary"
              class="flex-shrink-0"
            >
              {{ episodeLabel(file.episode) }}
            </v-chip>
            <span class="text-truncate">{{ file.name }}</span>
          </v-list-item-title>
          <v-list-item-subtitle>{{ formatSize(file.size) }}</v-list-item-subtitle>
        </v-list-item>
      </v-list>
      <v-card v-else variant="tonal" class="ma-3 pa-4 text-center">
        <div class="text-body-2">没有可下载的文件</div>
      </v-card>
      <div class="qc-drawer-footer">
        <v-btn
          block
          color="primary"
          prepend-icon="mdi-download"
          :loading="downloading"
          :disabled="!episodeChecked.size"
          @click="downloadSelected"
        >
          下载已选（{{ episodeChecked.size }} 项 · {{ formatSize(selectedEpisodeSize) }}）
        </v-btn>
      </div>
    </v-navigation-drawer>

    <v-dialog v-model="dialogVisible" :fullscreen="isMobile" max-width="720" scrollable>
      <v-card>
        <v-card-title>{{ editing ? "编辑渠道" : "新增渠道" }}</v-card-title>
        <v-card-subtitle>渠道保存后立即生效，参与搜索与监控</v-card-subtitle>
        <v-card-text>
          <v-row dense>
            <v-col cols="12" sm="6">
              <v-text-field v-model="form.name" label="名称" density="compact" variant="outlined" />
            </v-col>
            <v-col cols="12" sm="6">
              <v-select
                v-model="form.type"
                :items="typeOptions"
                item-title="label"
                item-value="value"
                label="类型"
                density="compact"
                variant="outlined"
                :disabled="!!editing"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-text-field
                v-model.number="form.priority"
                label="优先级（越大越优先）"
                type="number"
                density="compact"
                variant="outlined"
              />
            </v-col>
            <v-col cols="12" sm="6" class="d-flex align-center">
              <v-switch v-model="form.enabled" label="启用" color="success" hide-details />
            </v-col>

            <template v-if="form.type === 'pansou'">
              <v-col cols="12">
                <v-text-field
                  v-model="form.config.base_url"
                  label="服务地址"
                  placeholder="http://pansou:8888"
                  density="compact"
                  variant="outlined"
                />
              </v-col>
              <v-col cols="12" sm="6">
                <v-text-field
                  v-model="form.config.username"
                  label="账号"
                  density="compact"
                  variant="outlined"
                />
              </v-col>
              <v-col cols="12" sm="6">
                <v-text-field
                  v-model="form.config.password"
                  label="密码"
                  type="password"
                  density="compact"
                  variant="outlined"
                />
              </v-col>
              <v-col cols="12">
                <v-text-field
                  v-model="form.config.token"
                  label="访问令牌（可选）"
                  type="password"
                  density="compact"
                  variant="outlined"
                />
              </v-col>
              <v-col cols="12" sm="6">
                <v-select
                  v-model="form.config.src"
                  :items="srcOptions"
                  item-title="label"
                  item-value="value"
                  label="搜索来源"
                  density="compact"
                  variant="outlined"
                />
              </v-col>
              <v-col cols="12" sm="6">
                <v-text-field
                  v-model="form.config.pluginsText"
                  label="指定插件（逗号分隔）"
                  density="compact"
                  variant="outlined"
                />
              </v-col>
              <v-col cols="12">
                <v-text-field
                  v-model="form.config.cloudTypesText"
                  label="网盘类型（逗号分隔）"
                  placeholder="quark,115"
                  density="compact"
                  variant="outlined"
                />
              </v-col>
            </template>

            <template v-else>
              <v-col cols="12">
                <v-text-field
                  v-model="form.config.channel"
                  label="频道标识"
                  placeholder="@channel 或 https://t.me/xxx"
                  density="compact"
                  variant="outlined"
                />
              </v-col>
              <v-col cols="12" sm="6">
                <v-text-field
                  v-model.number="form.config.interval_minutes"
                  label="轮询间隔（分钟）"
                  type="number"
                  density="compact"
                  variant="outlined"
                />
              </v-col>
              <v-col cols="12" sm="6">
                <v-text-field
                  v-model.number="form.config.scan_limit"
                  label="单次扫描条数"
                  type="number"
                  density="compact"
                  variant="outlined"
                />
              </v-col>
              <v-col cols="12">
                <v-switch v-model="form.config.backfill" label="首次接入回补历史" hide-details />
              </v-col>
            </template>
          </v-row>
        </v-card-text>
        <v-card-actions>
          <v-btn variant="text" @click="dialogVisible = false">取消</v-btn>
          <v-spacer />
          <v-btn color="primary" :loading="saving" @click="submit">保存</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog v-model="deleteVisible" max-width="420">
      <v-card>
        <v-card-title>删除渠道</v-card-title>
        <v-card-text>确认删除「{{ deleting?.name }}」？删除后不再参与搜索与监控。</v-card-text>
        <v-card-actions>
          <v-btn variant="text" @click="deleteVisible = false">取消</v-btn>
          <v-spacer />
          <v-btn color="error" @click="confirmDelete">删除</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-snackbar v-model="snack.show" :timeout="3000" :color="snack.color" location="bottom">
      {{ snack.text }}
    </v-snackbar>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from "vue"
import { useViewport } from "../composables/useViewport"

const props = defineProps({
  api: { type: [Object, Function], required: true },
  pluginId: { type: String, default: "QuarkChannel" },
  navKey: { type: String, default: "channels" },
})

const { isMobile } = useViewport(600)

const channels = ref([])
const summary = reactive({ total: 0, enabled: 0, tg_entries: 0, last_poll: 0 })
const search = ref("")
const statusFilter = ref("all")
const selected = ref(new Set())
const loading = ref(false)

const dialogVisible = ref(false)
const editing = ref(null)
const saving = ref(false)
const form = reactive(defaultForm())

const deleteVisible = ref(false)
const deleting = ref(null)

const detailOpen = ref(false)
const detail = ref(null)

const tab = ref("channels")
const downloadSearch = ref("")
const shares = ref([])
const searching = ref(false)
const searchDone = ref(false)
const downloadingSid = ref("")

const episodeShare = ref(null)
const episodeOpen = ref(false)
const episodes = ref([])
const episodesLoading = ref(false)
const episodeChecked = ref(new Set())
const downloading = ref(false)

const snack = reactive({ show: false, text: "", color: "success" })

const typeOptions = [
  { label: "盘搜", value: "pansou" },
  { label: "TG 频道", value: "tg_channel" },
]
const srcOptions = [
  { label: "全部来源", value: "all" },
  { label: "仅 TG 频道", value: "tg" },
  { label: "仅插件源", value: "plugin" },
]

function defaultForm() {
  return {
    name: "",
    type: "pansou",
    priority: 50,
    enabled: true,
    config: {
      base_url: "",
      username: "",
      password: "",
      token: "",
      src: "all",
      pluginsText: "",
      cloudTypesText: "quark",
      channel: "",
      interval_minutes: 10,
      scan_limit: 100,
      backfill: false,
    },
  }
}

function unwrap(payload) {
  if (payload && typeof payload === "object" && "success" in payload) {
    return payload.data ?? payload
  }
  if (payload && typeof payload === "object" && payload.data && typeof payload.data === "object") {
    return payload.data
  }
  return payload
}

async function request(method, url, body) {
  const handler = props.api?.[method]
  if (typeof handler !== "function") {
    throw new Error(`宿主缺少 api.${method}`)
  }
  const raw = await handler.call(props.api, url, body)
  return unwrap(raw)
}

function notify(text, color = "success") {
  snack.text = text
  snack.color = color
  snack.show = true
}

const filtered = computed(() => {
  const keyword = search.value.trim().toLowerCase()
  return channels.value.filter((item) => {
    if (statusFilter.value === "enabled" && !item.enabled) return false
    if (statusFilter.value === "disabled" && item.enabled) return false
    if (!keyword) return true
    const haystack = [
      item.name,
      item.type,
      typeLabel(item.type),
      item.config?.base_url,
      item.config?.channel,
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase()
    return haystack.includes(keyword)
  })
})

const lastPollText = computed(() => {
  if (!summary.last_poll) return "未同步"
  return `同步 ${relativeTime(summary.last_poll)}`
})

const selectedEpisodeSize = computed(() => episodes.value.reduce((total, file) => {
  if (!episodeChecked.value.has(fileKey(file))) return total
  return total + Number(file.size || 0)
}, 0))

function typeLabel(type) {
  return type === "tg_channel" ? "TG 频道" : "盘搜"
}

function typeColor(type) {
  return type === "tg_channel" ? "info" : "primary"
}

function channelLine(item) {
  if (item.type === "tg_channel") return item.config?.channel || "未设置频道"
  return item.config?.base_url || "未设置地址"
}

function relativeTime(value) {
  const stamp = Number(value || 0)
  if (!stamp) return "从未"
  const diff = Math.max(0, Math.floor(Date.now() / 1000 - stamp))
  if (diff < 60) return "刚刚"
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return `${Math.floor(diff / 86400)} 天前`
}

function toggleSelect(id, value) {
  const next = new Set(selected.value)
  if (value) next.add(id)
  else next.delete(id)
  selected.value = next
}

async function load() {
  loading.value = true
  try {
    const body = await request("get", `plugin/${props.pluginId}/channels`)
    channels.value = body?.items || []
  } catch (error) {
    notify(`加载渠道失败：${error.message}`, "error")
  } finally {
    loading.value = false
  }
}

async function loadSummary() {
  try {
    const body = await request("get", `plugin/${props.pluginId}/channels/summary`)
    Object.assign(summary, {
      total: body?.total || 0,
      enabled: body?.enabled || 0,
      tg_entries: body?.tg_entries || 0,
      last_poll: body?.last_poll || 0,
    })
  } catch (error) {
    /* 汇总失败不影响列表 */
  }
}

function fileKey(file) {
  return String(file?.fid || file?.name || "")
}

function formatSize(value) {
  const size = Number(value || 0)
  if (!size) return "0 B"
  const units = ["B", "KB", "MB", "GB", "TB"]
  let index = 0
  let amount = size
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024
    index += 1
  }
  const digits = amount >= 100 || index === 0 ? 0 : 1
  return `${amount.toFixed(digits)} ${units[index]}`
}

function episodeLabel(episode) {
  return `第 ${episode} 集`
}

async function searchShares() {
  const keyword = downloadSearch.value.trim()
  if (!keyword) {
    notify("请输入要搜索的片名", "warning")
    return
  }
  searching.value = true
  searchDone.value = false
  shares.value = []
  try {
    const query = `keyword=${encodeURIComponent(keyword)}&limit=50`
    const body = await request("get", `plugin/${props.pluginId}/shares?${query}`)
    if (body?.success === false) {
      notify(body?.message || "搜索失败", "warning")
      return
    }
    shares.value = body?.items || []
    searchDone.value = true
    if (shares.value.length) await loadSummary()
  } catch (error) {
    notify(`搜索失败：${error.message}`, "error")
  } finally {
    searching.value = false
  }
}

async function openEpisodes(share) {
  episodeShare.value = share
  episodeOpen.value = true
  episodes.value = []
  episodeChecked.value = new Set()
  episodesLoading.value = true
  try {
    const sid = encodeURIComponent(share.sid)
    const body = await request("get", `plugin/${props.pluginId}/shares/${sid}/episodes`)
    if (body?.success === false) {
      notify(body?.message || "加载集数失败", "warning")
      return
    }
    episodes.value = body?.items || []
  } catch (error) {
    notify(`加载集数失败：${error.message}`, "error")
  } finally {
    episodesLoading.value = false
  }
}

function toggleEpisode(file, value) {
  const next = new Set(episodeChecked.value)
  const key = fileKey(file)
  if (value) next.add(key)
  else next.delete(key)
  episodeChecked.value = next
}

function selectAllEpisodes() {
  episodeChecked.value = new Set(episodes.value.map(fileKey))
}

function clearEpisodes() {
  episodeChecked.value = new Set()
}

function invertEpisodes() {
  const next = new Set()
  for (const file of episodes.value) {
    const key = fileKey(file)
    if (!episodeChecked.value.has(key)) next.add(key)
  }
  episodeChecked.value = next
}

function selectedFiles() {
  return episodes.value.filter((file) => episodeChecked.value.has(fileKey(file)))
}

async function submitDownload(share, payload, label) {
  downloading.value = true
  try {
    const body = await request("post", `plugin/${props.pluginId}/download_episodes`, {
      sid: share.sid,
      ...payload,
    })
    if (body?.success === false) {
      notify(body?.message || "下载提交失败", "warning")
      return false
    }
    notify(`已提交${label} ${body?.count || 0} 个文件`)
    await loadSummary()
    return true
  } catch (error) {
    notify(`下载提交失败：${error.message}`, "error")
    return false
  } finally {
    downloading.value = false
  }
}

async function downloadWhole(share) {
  downloadingSid.value = share.sid
  try {
    await submitDownload(share, {}, "整包")
  } finally {
    downloadingSid.value = ""
  }
}

async function downloadSelected() {
  if (!episodeShare.value) return
  const files = selectedFiles()
  if (!files.length) {
    notify("请先勾选要下载的文件", "warning")
    return
  }
  const fids = files.map((file) => String(file.fid || "")).filter(Boolean)
  const wantedEpisodes = files
    .map((file) => Number(file.episode || 0))
    .filter((episode) => episode > 0)
  let payload
  if (fids.length === files.length) {
    payload = { fids }
  } else if (wantedEpisodes.length === files.length) {
    payload = { episodes: wantedEpisodes }
  } else {
    notify("部分文件缺少可用的集号或文件标识", "warning")
    return
  }
  const ok = await submitDownload(episodeShare.value, payload, "选集")
  if (ok) episodeOpen.value = false
}

function openCreate() {
  editing.value = null
  Object.assign(form, defaultForm())
  dialogVisible.value = true
}

function openEdit(item) {
  editing.value = item
  Object.assign(form, {
    name: item.name,
    type: item.type,
    priority: item.priority ?? 0,
    enabled: !!item.enabled,
    config: {
      base_url: item.config?.base_url || "",
      username: item.config?.username || "",
      password: item.config?.password || "",
      token: item.config?.token || "",
      src: item.config?.src || "all",
      pluginsText: (item.config?.plugins || []).join(","),
      cloudTypesText: (item.config?.cloud_types || []).join(","),
      channel: item.config?.channel || "",
      interval_minutes: item.config?.interval_minutes || 10,
      scan_limit: item.config?.scan_limit || 100,
      backfill: !!item.config?.backfill,
    },
  })
  dialogVisible.value = true
}

function buildPayload() {
  const config = {}
  if (form.type === "pansou") {
    config.base_url = form.config.base_url.trim()
    config.username = form.config.username.trim()
    config.password = form.config.password
    config.token = form.config.token
    config.src = form.config.src || "all"
    config.plugins = splitList(form.config.pluginsText)
    config.cloud_types = splitList(form.config.cloudTypesText)
  } else {
    config.channel = form.config.channel.trim()
    config.interval_minutes = Number(form.config.interval_minutes || 10)
    config.scan_limit = Number(form.config.scan_limit || 100)
    config.backfill = !!form.config.backfill
  }
  return {
    name: form.name.trim(),
    type: form.type,
    priority: Number(form.priority || 0),
    enabled: !!form.enabled,
    config,
  }
}

function splitList(text) {
  return String(text || "")
    .split(/[,，\s]+/)
    .map((item) => item.trim())
    .filter(Boolean)
}

async function submit() {
  const payload = buildPayload()
  if (!payload.name) {
    notify("请填写渠道名称", "warning")
    return
  }
  if (payload.type === "pansou" && !payload.config.base_url) {
    notify("请填写盘搜服务地址", "warning")
    return
  }
  if (payload.type === "tg_channel" && !payload.config.channel) {
    notify("请填写频道标识", "warning")
    return
  }
  saving.value = true
  try {
    if (editing.value) {
      await request("put", `plugin/${props.pluginId}/channels/${editing.value.id}`, payload)
      notify("渠道已更新")
    } else {
      await request("post", `plugin/${props.pluginId}/channels`, payload)
      notify("渠道已创建")
    }
    dialogVisible.value = false
    await load()
    await loadSummary()
  } catch (error) {
    notify(`保存失败：${error.message}`, "error")
  } finally {
    saving.value = false
  }
}

async function setEnabled(item, value) {
  try {
    await request("put", `plugin/${props.pluginId}/channels/${item.id}`, { enabled: !!value })
    item.enabled = !!value
    await loadSummary()
  } catch (error) {
    notify(`更新失败：${error.message}`, "error")
  }
}

async function bulkToggle(enabled) {
  const ids = Array.from(selected.value)
  if (!ids.length) return
  let ok = 0
  let fail = 0
  for (const id of ids) {
    try {
      await request("put", `plugin/${props.pluginId}/channels/${id}`, { enabled })
      ok += 1
    } catch (error) {
      fail += 1
    }
  }
  selected.value = new Set()
  notify(`成功 ${ok} / 失败 ${fail}`, fail ? "warning" : "success")
  await load()
  await loadSummary()
}

async function testChannel(item) {
  try {
    const body = await request("post", `plugin/${props.pluginId}/channels/${item.id}/test`)
    notify(body?.message || "测试完成", body?.success === false ? "warning" : "success")
  } catch (error) {
    notify(`测试失败：${error.message}`, "error")
  }
}

async function scanChannel(item) {
  try {
    const body = await request("post", `plugin/${props.pluginId}/channels/${item.id}/scan`)
    notify(body?.message || "扫描完成", body?.success === false ? "warning" : "success")
    await load()
    await loadSummary()
  } catch (error) {
    notify(`扫描失败：${error.message}`, "error")
  }
}

function askDelete(item) {
  deleting.value = item
  deleteVisible.value = true
}

async function confirmDelete() {
  if (!deleting.value) return
  try {
    await request("delete", `plugin/${props.pluginId}/channels/${deleting.value.id}`)
    notify("渠道已删除")
    deleteVisible.value = false
    deleting.value = null
    await load()
    await loadSummary()
  } catch (error) {
    notify(`删除失败：${error.message}`, "error")
  }
}

function openDetail(item) {
  detail.value = item
  detailOpen.value = true
}

function editFromDetail() {
  const item = detail.value
  detailOpen.value = false
  if (item) openEdit(item)
}

onMounted(async () => {
  await load()
  await loadSummary()
})
</script>

<style scoped>
.qc-page {
  padding: 14px 16px 28px;
}

.qc-toolbar {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  flex-wrap: wrap;
}

@media (max-width: 599px) {
  .qc-page {
    padding: 12px 12px calc(104px + env(safe-area-inset-bottom, 0px));
  }

  .qc-toolbar {
    justify-content: space-between;
  }
}

.qc-card {
  min-height: 150px;
}

.qc-title {
  font-size: 14px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  cursor: pointer;
}

.qc-body {
  cursor: pointer;
}

.qc-switch {
  flex: 0 0 auto;
}

.qc-share-card {
  min-height: 142px;
  display: flex;
  flex-direction: column;
}

.qc-drawer-heading {
  min-width: 0;
}

.qc-drawer-subtitle {
  max-width: 280px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.qc-episode-list {
  padding-bottom: 84px;
}

.qc-episode-name {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.qc-drawer-footer {
  position: sticky;
  bottom: 0;
  padding: 12px;
  border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  background: rgb(var(--v-theme-surface));
}
</style>
