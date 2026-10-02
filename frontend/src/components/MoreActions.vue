<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, useId } from 'vue'
import { Ellipsis, X } from '@lucide/vue'
import { focusFirst, restoreFocus, trapFocus } from '../focus'
withDefaults(defineProps<{ label?: string }>(), { label: '更多操作' })
const open = ref(false), trigger = ref<HTMLElement | null>(null), panel = ref<HTMLElement | null>(null), id = useId()
const position = ref({ top: '0px', left: '0px' })
function close() { const previousPanel = panel.value; open.value = false; document.removeEventListener('keydown', keydown); void nextTick(() => { if (document.activeElement === document.body || previousPanel?.contains(document.activeElement)) restoreFocus(trigger.value) }) }
function keydown(event: KeyboardEvent) { if (event.key === 'Escape') { event.preventDefault(); close() } else trapFocus(event, panel.value) }
async function show() {
  const rect = trigger.value?.getBoundingClientRect()
  if (!rect) return
  position.value = { top: Math.min(rect.bottom + 6, Math.max(12, window.innerHeight - 360)) + 'px', left: Math.max(12, Math.min(rect.right - 240, window.innerWidth - 252)) + 'px' }
  open.value = true; document.addEventListener('keydown', keydown)
  await nextTick(); focusFirst(panel.value)
}
function choose(event: MouseEvent) { if ((event.target as HTMLElement).closest('button:not(:disabled),a')) close() }
onBeforeUnmount(() => document.removeEventListener('keydown', keydown))
</script>
<template>
  <button ref="trigger" class="icon-btn" type="button" :aria-label="label" :title="label" aria-haspopup="dialog" :aria-expanded="open" :aria-controls="id" @click="show"><Ellipsis :size="18" /></button>
  <Teleport to="body"><div v-if="open" class="action-popover-backdrop" @click.self="close"><section :id="id" ref="panel" class="action-popover" :style="position" role="dialog" aria-modal="true" :aria-label="label" tabindex="-1"><header><strong>{{ label }}</strong><button class="icon-btn" type="button" aria-label="关闭操作菜单" @click="close"><X :size="16" /></button></header><div @click="choose"><slot /></div></section></div></Teleport>
</template>
