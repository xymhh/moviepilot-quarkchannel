import { computed, onMounted, onUnmounted, ref } from "vue"

export function useViewport(breakpoint = 600) {
  const width = ref(typeof window !== "undefined" ? window.innerWidth : 1200)
  const isMobile = computed(() => width.value < breakpoint)

  function update() {
    if (typeof window !== "undefined") {
      width.value = window.innerWidth
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
