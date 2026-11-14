<template>
  <v-card variant="outlined" class="pa-4">
    <div class="text-subtitle-1 mb-2">网盘直链频道</div>
    <div class="text-body-2 text-medium-emphasis mb-3">
      渠道在左侧「渠道管理」中维护；这里只展示运行概况。
    </div>
    <v-row dense>
      <v-col cols="6" sm="3">
        <v-chip size="small" variant="tonal" color="primary">渠道 {{ summary.total }}</v-chip>
      </v-col>
      <v-col cols="6" sm="3">
        <v-chip size="small" variant="tonal" color="success">启用 {{ summary.enabled }}</v-chip>
      </v-col>
      <v-col cols="6" sm="3">
        <v-chip size="small" variant="tonal">索引 {{ summary.tg_entries }}</v-chip>
      </v-col>
      <v-col cols="6" sm="3">
        <v-chip size="small" variant="tonal" prepend-icon="mdi-clock-outline">
          {{ lastPollText }}
        </v-chip>
      </v-col>
    </v-row>
  </v-card>
</template>

<script setup>
import { computed, onMounted, reactive } from "vue"

const props = defineProps({
  api: { type: [Object, Function], required: true },
  pluginId: { type: String, default: "QuarkChannel" },
})

const summary = reactive({ total: 0, enabled: 0, tg_entries: 0, last_poll: 0 })

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

onMounted(async () => {
  try {
    const raw = await props.api.get(`plugin/${props.pluginId}/channels/summary`)
    const body = unwrap(raw)
    Object.assign(summary, {
      total: body?.total || 0,
      enabled: body?.enabled || 0,
      tg_entries: body?.tg_entries || 0,
      last_poll: body?.last_poll || 0,
    })
  } catch (error) {
    /* 概况失败不影响页面显示 */
  }
})
</script>
