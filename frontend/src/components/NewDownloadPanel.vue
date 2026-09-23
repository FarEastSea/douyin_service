<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowRight, CircleCheck, Link2, Search, X } from '@lucide/vue'
import { api, jsonBody } from '../api'
import { focusFirst, trapFocus } from '../focus'
import { useAppStore } from '../stores/app'
import type { MediaPlatform } from '../types'

const emit = defineEmits<{ close: [] }>()
const router = useRouter()
const store = useAppStore()
const input = ref('')
const inputElement = ref<HTMLInputElement | null>(null)
const panelElement = ref<HTMLElement | null>(null)
const platformId = ref('')
const inputKind = ref<'author' | 'work' | 'unknown'>('unknown')
const identified = ref(false)
const detecting = ref(false)
const submitting = ref(false)
const error = ref('')
const options = computed(() => store.platforms.length ? store.platforms : [
  { id: 'douyin', name: '抖音' }, { id: 'x', name: 'X' }, { id: 'tiktok', name: 'TikTok' },
  { id: 'weibo', name: '微博' }, { id: 'bilibili', name: 'B站' }, { id: 'xhs', name: '小红书' },
] as MediaPlatform[])
const selected = computed(() => options.value.find(item => item.id === platformId.value))

function close() { if (!submitting.value) emit('close') }
function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') { close(); return }
  trapFocus(event, panelElement.value)
}
function selectPlatform() { identified.value = false; inputKind.value = 'unknown'; error.value = '' }
async function identify() {
  if (!input.value.trim()) { error.value = '先粘贴作品链接、作者主页或平台支持的用户标识。'; return }
  detecting.value = true; error.value = ''
  try {
    const found = await api<{ platform_id: string; input_kind: 'author' | 'work' | 'unknown' }>('/platforms/detect', { method: 'POST', ...jsonBody({ value: input.value.trim() }) })
    platformId.value = found.platform_id
    inputKind.value = found.input_kind
    if (found.platform_id === 'xhs' && found.input_kind === 'author') {
      identified.value = false
      error.value = '小红书目前仅支持单条笔记；作者主页批量采集已搁置。'
      return
    }
    identified.value = true
  } catch {
    identified.value = false
    error.value = '未能从链接识别平台。若输入的是用户名或短文本，请手动选择平台。'
    await nextTick()
  } finally { detecting.value = false }
}
function confirmPlatform() {
  if (!input.value.trim()) { error.value = '先填写下载来源。'; return }
  if (!platformId.value) { error.value = '先选择平台。'; return }
  if (platformId.value === 'xhs' && /\/(?:user\/profile)\/[a-z0-9]+\/?(?:\?.*)?$/i.test(input.value.trim())) {
    error.value = '小红书目前仅支持单条笔记；作者主页批量采集已搁置。'; return
  }
  identified.value = true; error.value = ''
}
async function submit() {
  if (!identified.value || !platformId.value || submitting.value) return
  if (platformId.value === 'xhs' && inputKind.value === 'author') { error.value = '小红书目前仅支持单条笔记。'; return }
  submitting.value = true; error.value = ''
  const source = input.value.trim()
  try {
    if (platformId.value === 'douyin') {
      const result = await api<{ url_type?: string; author_already_exists?: boolean; author_id?: number; author_position?: number }>('/tasks/download', { method: 'POST', ...jsonBody({ share_url: source, start_index: 1, wait_time: 1 }) })
      if (result.url_type === 'author' && result.author_already_exists && result.author_id) {
        emit('close')
        store.notify('作者已存在，已定位到对应记录', 'info')
        await router.push({ path: '/douyin/authors', query: { focus: String(result.author_id), position: String(result.author_position || 0) } })
        return
      }
    }
    else if (platformId.value === 'x') await api('/x/download', { method: 'POST', ...jsonBody({ profile_url: source }) })
    else await api(`/platform-downloads/${platformId.value}/download`, { method: 'POST', ...jsonBody({ source }) })
    const destination = `/operations/tasks?platform=${encodeURIComponent(platformId.value)}`
    store.notify('下载任务已提交，可以在全部任务中查看进度。')
    emit('close')
    await router.push(destination)
  } catch (reason: any) { error.value = reason.message || '提交失败，请检查链接和平台后重试。' }
  finally { submitting.value = false }
}
onMounted(() => { document.body.classList.add('modal-open'); document.addEventListener('keydown', onKeydown); void nextTick(() => focusFirst(panelElement.value, inputElement.value)) })
onBeforeUnmount(() => { document.body.classList.remove('modal-open'); document.removeEventListener('keydown', onKeydown) })
</script>

<template>
  <div class="download-backdrop" @click.self="close">
    <section ref="panelElement" class="download-panel" role="dialog" aria-modal="true" aria-labelledby="new-download-title">
      <header><div><h2 id="new-download-title">新建下载</h2><p>先确认来源，再提交到后台队列。</p></div><button class="icon-btn" aria-label="关闭新建下载" :disabled="submitting" @click="close"><X :size="19" /></button></header>
      <div class="download-panel-body">
        <label for="download-source">作品链接、作者主页或用户标识</label>
        <div class="source-input"><Link2 :size="19" /><input id="download-source" ref="inputElement" v-model="input" autocomplete="off" placeholder="粘贴分享链接或输入用户标识" @input="identified = false; inputKind = 'unknown'; error = ''" @keyup.enter="identify" /></div>
        <p class="field-help">识别只检查链接所属平台，不会发起下载。单条作品和作者主页由各平台按已有能力处理。</p>
        <button class="btn ghost" :disabled="detecting || !input.trim()" @click="identify"><Search :size="17" />{{ detecting ? '识别中…' : '识别来源' }}</button>

        <div class="download-platform-choice">
          <label for="download-platform">平台</label>
          <select id="download-platform" v-model="platformId" @change="selectPlatform"><option value="">选择平台</option><option v-for="item in options" :key="item.id" :value="item.id">{{ item.name }}</option></select>
          <button v-if="platformId && !identified" class="text-button" @click="confirmPlatform">确认选择 {{ selected?.name || '该平台' }}</button>
        </div>
        <div v-if="identified && selected" class="source-confirmed" role="status"><CircleCheck :size="19" /><span><strong>来源已确认：{{ selected.name }}{{ inputKind === 'author' ? ' · 作者主页' : inputKind === 'work' ? ' · 单条作品' : '' }}</strong><small>{{ platformId === 'xhs' ? '小红书当前仅支持单条笔记，不支持作者主页批量采集。' : '提交后会显示在全部任务中；如需调整来源，请重新识别。' }}</small></span></div>
        <p v-if="error" class="form-error" role="alert">{{ error }}</p>
      </div>
      <footer><button class="btn ghost" :disabled="submitting" @click="close">取消</button><button class="btn primary" :disabled="!identified || submitting || (platformId === 'xhs' && inputKind === 'author')" @click="submit">{{ submitting ? '正在提交…' : '确认提交' }}<ArrowRight :size="17" /></button></footer>
    </section>
  </div>
</template>
