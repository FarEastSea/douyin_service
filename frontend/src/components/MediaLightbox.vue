<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ChevronLeft, ChevronRight, Download, X } from '@lucide/vue'
import type { MediaItem } from '../types'

const props = defineProps<{ open: boolean; items: MediaItem[]; start?: number }>()
const emit = defineEmits<{ close: [] }>()
const index = ref(0)
const failed = ref(false)
const current = computed(() => props.items[index.value])

function move(delta: number) {
  if (!props.items.length) return
  index.value = (index.value + delta + props.items.length) % props.items.length
  failed.value = false
}
function close() { emit('close') }
function keydown(event: KeyboardEvent) {
  if (!props.open) return
  if (event.key === 'Escape') close()
  if (event.key === 'ArrowLeft') move(-1)
  if (event.key === 'ArrowRight') move(1)
}
watch(() => props.open, async open => {
  document.body.classList.toggle('modal-open', open)
  if (open) {
    index.value = Math.min(Math.max(props.start || 0, 0), Math.max(0, props.items.length - 1))
    failed.value = false
    await nextTick()
    document.querySelector<HTMLElement>('.lightbox-close')?.focus()
  }
})
onMounted(() => window.addEventListener('keydown', keydown))
onBeforeUnmount(() => { window.removeEventListener('keydown', keydown); document.body.classList.remove('modal-open') })
</script>

<template>
  <Teleport to="body">
    <Transition name="fade">
      <div v-if="open" class="lightbox" role="dialog" aria-modal="true" aria-label="媒体预览" @click.self="close">
        <header class="lightbox-bar">
          <div><strong>{{ current?.title || '媒体预览' }}</strong><span v-if="items.length > 1">{{ index + 1 }} / {{ items.length }}</span></div>
          <div>
            <a v-if="current" class="icon-btn" :href="current.url" download title="下载"><Download :size="18" /></a>
            <button class="icon-btn lightbox-close" title="关闭" @click="close"><X :size="20" /></button>
          </div>
        </header>
        <main class="lightbox-stage">
          <div v-if="failed || !current" class="media-fallback">媒体加载失败，请关闭后重试</div>
          <video v-else-if="current.type === 'video'" :key="current.url" controls playsinline preload="metadata" :src="current.url" @error="failed = true" />
          <img v-else :key="current.url" :src="current.url" :alt="current.title || '图片预览'" @error="failed = true" />
          <button v-if="items.length > 1" class="lightbox-nav prev" aria-label="上一项" @click="move(-1)"><ChevronLeft /></button>
          <button v-if="items.length > 1" class="lightbox-nav next" aria-label="下一项" @click="move(1)"><ChevronRight /></button>
        </main>
      </div>
    </Transition>
  </Teleport>
</template>
