import { computed, onMounted, onUnmounted, ref } from "vue"

export function useViewport(breakpoint = 600) {
  const width = ref(typeof window !== "undefined" ? window.innerWidth : 1200)
  const isMobile = computed(() => width.value < breakpoint)

  function update() {
    if (typeof window !== "undefined") {
      // 预览模式：允许用 ?w=390 模拟窄屏，便于本地截图核对移动端布局
      const forced = Number(window.__QC_PREVIEW_WIDTH__ || 0)
      width.value = forced > 0 ? forced : window.innerWidth
    }
  }

  onMounted(() => {
    if (typeof window !== "undefined") {
      window.addEventListener("resize", update, { passive: true })
      update()
    }
  })

  onUnmounted(() => {
    if (typeof window !== "undefined") {
      window.removeEventListener("resize", update)
    }
  })

  return { width, isMobile }
}
