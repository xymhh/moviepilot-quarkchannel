<template>
  <div class="qc-detail">
    <v-row dense class="mb-3">
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

    <v-row dense class="mb-3">
      <v-col cols="12" sm="6">
        <v-btn block color="primary" prepend-icon="mdi-open-in-new" @click="openChannels">
          打开渠道管理
        </v-btn>
      </v-col>
      <v-col cols="12" sm="6">
        <v-btn block variant="tonal" prepend-icon="mdi-cog" @click="openSettings">
          插件设置
        </v-btn>
      </v-col>
    </v-row>

    <v-card variant="outlined">
      <v-card-title class="text-subtitle-2 py-2">渠道概览</v-card-title>
      <v-divider />
      <v-list v-if="channels.length" density="compact">
        <v-list-item v-for="item in channels.slice(0, 6)" :key="item.id">
          <template #prepend>
            <v-icon
              :icon="item.type === 'tg_channel' ? 'mdi-send' : 'mdi-cloud-search'"
              size="18"
              class="mr-3"
            />
          </template>
          <v-list-item-title class="text-body-2 text-truncate">
            {{ item.name }}
          </v-list-item-title>
          <v-list-item-subtitle class="text-caption">
            {{ item.type === "tg_channel" ? "TG 频道" : "盘搜" }} ·
            {{ item.enabled ? "已启用" : "已停用" }} · 命中 {{ item.stats?.hits || 0 }}
          </v-list-item-subtitle>
          <template #append>
            <v-chip size="x-small" :color="item.enabled ? 'success' : 'default'" variant="tonal">
              {{ item.enabled ? "运行中" : "已停用" }}
            </v-chip>
          </template>
        </v-list-item>
      </v-list>
      <v-card-text v-else class="text-body-2 text-medium-emphasis">
        还没有渠道，点击上方按钮新增。
      </v-card-text>
    </v-card>

    <div class="text-caption text-medium-emphasis mt-3">
      详细参数在「设置 → 插件 → 网盘直链频道」；渠道增删改在「渠道管理」。
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from "vue"

const props = defineProps({
  api: { type: [Object, Function], required: true },
  pluginId: { type: String, default: "QuarkChannel" },
  navKey: { type: String, default: "main" },
})

const summary = reactive({ total: 0, enabled: 0, tg_entries: 0, last_poll: 0 })
const channels = ref([])

function unwrap(payload) {
  if (payload && typeof payload === "object" && "success" in payload) {
    return payload.data ?? payload
  }
  if (payload && typeof payload === "object" && payload.data && typeof payload.data === "object") {
    return payload.data
  }
  return payload
}

const lastPollText = computed(() => {
  const stamp = Number(summary.last_poll || 0)
  if (!stamp) return "未同步"
  const diff = Math.max(0, Math.floor(Date.now() / 1000 - stamp))
  if (diff < 60) return "刚刚同步"
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return `${Math.floor(diff / 86400)} 天前`
})

function openChannels() {
  const pluginId = props.pluginId || "QuarkChannel"
  window.location.hash = `#/plugin-app/${pluginId}/channels`
}

function openSettings() {
  const pluginId = props.pluginId || "QuarkChannel"
  window.location.hash = `#/plugin-app/${pluginId}/settings`
}

onMounted(async () => {
  try {
    const rawSummary = await props.api.get(`plugin/${props.pluginId}/channels/summary`)
    const body = unwrap(rawSummary)
    Object.assign(summary, {
      total: body?.total || 0,
      enabled: body?.enabled || 0,
      tg_entries: body?.tg_entries || 0,
      last_poll: body?.last_poll || 0,
    })
  } catch (error) {
    /* 概况失败不影响页面 */
  }
  try {
    const rawList = await props.api.get(`plugin/${props.pluginId}/channels`)
    const body = unwrap(rawList)
    channels.value = body?.items || []
  } catch (error) {
    channels.value = []
  }
})
</script>

<style scoped>
.qc-detail {
  padding: 4px 0 8px;
}
</style>
