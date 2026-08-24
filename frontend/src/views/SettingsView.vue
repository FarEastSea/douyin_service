<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Activity, Clipboard, Cookie, Database, Play, RefreshCw, Save, Server, Settings2, Square, Trash2 } from '@lucide/vue'
import { useRoute } from 'vue-router'
import { api, jsonBody } from '../api'
import { useAppStore } from '../stores/app'

const store = useAppStore(), route = useRoute()
const tab = ref(String(route.query.tab || 'general'))
const runtime = ref<any>({}), limits = ref<any>({}), cookieValue = ref(''), xCookieValue = ref('')
const logs = ref<any[]>([]), logLevels = ref(['info', 'warning', 'error']), live = ref(true), process = ref<any>({})
const allFields = ref<any[]>([]), allValues = ref<any>({}), timer = ref<number>()
const secretValues = ref<Record<string, string>>({})
const updateInfo = ref<any>({}), diagnostic = ref<any>(null), updateBusy = ref(false)
const filteredLogs = computed(() => logs.value.filter(item => logLevels.value.includes(item.level)))
const accountOnlyKeys = new Set(['DOUYIN_COOKIE', 'X_COOKIE', 'X_COOKIE_FILE'])
const generalFields = computed(() => allFields.value.filter(field => !accountOnlyKeys.has(field.key)))
const generalGroup = ref('')
const generalGroups = computed(() => [...new Set(generalFields.value.map(field => String(field.group || '其他')))])
const visibleGeneralFields = computed(() => generalFields.value.filter(field => String(field.group || '其他') === generalGroup.value))
const xCookieFile = ref('')
const tabs = [['general', '常规设置'], ['account', '平台账号'], ['runtime', '下载与风控'], ['process', '服务进程'], ['logs', '活动日志'], ['about', '诊断与关于']]

async function loadRuntime() { const data = await api<any>('/config/runtime'); runtime.value = data.config; limits.value = data.limits }
async function loadAll() { const data = await api<any>('/config/all'); allFields.value = data.fields; allValues.value = data.values; xCookieFile.value = data.values?.X_COOKIE_FILE?.value || ''; if (!generalGroups.value.includes(generalGroup.value)) generalGroup.value = generalGroups.value[0] || '' }
async function loadLogs(silent: boolean | Event = false) { try { logs.value = (await api<any>('/logs?start=0&count=500')).logs || [] } catch (e: any) { if (silent !== true) store.notify(e.message, 'error') } }
async function loadProcess() { process.value = await api('/process/status') }
async function loadUpdateInfo() { updateInfo.value = await api<any>('/update/info') }
async function init() {
  try { await Promise.all([loadRuntime(), loadAll(), loadProcess(), loadLogs(), loadUpdateInfo()]) }
  catch (error: any) { store.notify(error.message || '加载设置失败', 'error') }
  if (timer.value != null) window.clearInterval(timer.value)
  timer.value = window.setInterval(() => { if (live.value && tab.value === 'logs') loadLogs(true) }, 3000)
}
async function saveRuntime() {
  try { const result = await api<any>('/config/runtime', { method: 'POST', ...jsonBody(runtime.value) }); runtime.value = result.data.config; store.notify('运行配置已保存'); await store.refreshStatus() }
  catch (error: any) { store.notify(error.message || '保存失败', 'error') }
}
async function saveAll() {
  const values: Record<string, any> = {}
  for (const field of allFields.value) {
    if (field.secret) {
      if (secretValues.value[field.key]?.trim()) values[field.key] = secretValues.value[field.key].trim()
    } else if (allValues.value[field.key]?.value !== undefined) values[field.key] = allValues.value[field.key].value
  }
  try { const result = await api<any>('/config/all', { method: 'POST', ...jsonBody({ values }) }); store.notify(result.message || '设置已保存') }
  catch (error: any) { store.notify(error.message || '保存失败', 'error') }
}
async function saveCookie(platform: 'douyin' | 'x') {
  const value = platform === 'douyin' ? cookieValue.value : xCookieValue.value
  if (!value.trim()) return store.notify('Cookie 内容不能为空', 'error')
  try { const result = await api<any>(platform === 'douyin' ? '/config/cookie' : '/x/config/cookie', { method: 'POST', ...jsonBody({ cookie: value.trim() }) }); store.notify(result.message); if (platform === 'douyin') { cookieValue.value = ''; await store.refreshRisk() } else xCookieValue.value = '' }
  catch (error: any) { store.notify(error.message || '保存 Cookie 失败', 'error') }
}
async function saveXCookieFile() {
  try { const result = await api<any>('/config/all', { method: 'POST', ...jsonBody({ values: { X_COOKIE_FILE: xCookieFile.value.trim() } }) }); store.notify(result.message || 'X Cookie 文件路径已保存'); await loadAll() }
  catch (error: any) { store.notify(error.message || '保存 X Cookie 文件路径失败', 'error') }
}
async function processAction(target: 'worker' | 'beat', action: 'start' | 'stop') { try { const result = await api<any>(`/process/${target}/${action}`, { method: 'POST' }); store.notify(result.message || '操作完成'); await loadProcess() } catch (e: any) { store.notify(e.message, 'error') } }
async function clearLogs() { if (!confirm('确定清空活动日志？')) return; await api('/logs', { method: 'DELETE' }); logs.value = []; store.notify('日志已清空') }
async function copyLogs() { await navigator.clipboard.writeText(filteredLogs.value.map(item => `${new Date(item.ts * 1000).toLocaleString()} [${item.level}] [${item.source}] ${item.msg}\n${item.detail || ''}`).join('\n')); store.notify('日志已复制') }
async function checkUpdate() { updateBusy.value = true; try { updateInfo.value = await api<any>('/update/check'); store.notify(updateInfo.value.message || '检查完成') } catch (e: any) { store.notify(e.message, 'error') } finally { updateBusy.value = false } }
async function diagnoseUpdate() { try { diagnostic.value = await api<any>('/update/diagnose'); await navigator.clipboard.writeText(JSON.stringify(diagnostic.value, null, 2)); store.notify('诊断信息已复制') } catch (e: any) { store.notify(e.message, 'error') } }
async function applyUpdate() { if (!confirm('确定拉取远程更新并重启 Worker/Beat？')) return; updateBusy.value = true; try { const result = await api<any>('/update/apply', { method: 'POST' }); store.notify(result.message || '更新完成'); await loadUpdateInfo() } catch (e: any) { store.notify(e.message, 'error') } finally { updateBusy.value = false } }
function toggleLevel(level: string) { logLevels.value = logLevels.value.includes(level) ? logLevels.value.filter(v => v !== level) : [...logLevels.value, level] }
onMounted(init); onBeforeUnmount(() => clearInterval(timer.value))
</script>

<template>
  <section class="workspace-card settings-workspace">
    <header class="workspace-header"><div><p class="eyebrow">SYSTEM CONTROL</p><h2>设置与诊断</h2><span>所有网页配置在保存后动态生效</span></div><button class="btn ghost" @click="init"><RefreshCw :size="16" />刷新状态</button></header>
    <nav class="settings-tabs"><button v-for="item in tabs" :key="item[0]" :class="{ active: tab === item[0] }" @click="tab = item[0]">{{ item[1] }}</button></nav>

    <div v-if="tab === 'general'" class="settings-panel"><header><Settings2 /><div><h3>基础配置</h3><p>按用途分类管理应用、目录、数据库与后台任务配置</p></div><button class="btn primary" @click="saveAll"><Save :size="16" />保存</button></header><nav class="settings-subtabs"><button v-for="group in generalGroups" :key="group" :class="{ active: generalGroup === group }" @click="generalGroup = group">{{ group }}</button></nav><div class="form-grid">
      <label v-for="field in visibleGeneralFields" :key="field.key"><span>{{ field.label }}</span><input v-if="field.secret" v-model="secretValues[field.key]" type="password" placeholder="留空保持当前值" /><input v-else v-model="allValues[field.key].value" :placeholder="field.default" /><small>{{ field.help || (field.secret ? '敏感值不会回显' : field.group) }}</small></label>
    </div></div>

    <div v-else-if="tab === 'account'" class="settings-panel"><header><Cookie /><div><h3>平台登录凭据</h3><p>Cookie 相关配置统一在此管理，敏感内容不会回显</p></div></header><div class="credential-grid"><article><strong>抖音 Cookie</strong><p>用于作者资料、作品列表和链接刷新。</p><textarea v-model="cookieValue" placeholder="粘贴最新抖音 Cookie" /><small class="cookie-format-hint">格式要求：从已登录抖音页面的网络请求头复制完整 Cookie Header String，必须包含 UIFID；不支持 JSON 或 Netscape 格式。</small><button class="btn primary" @click="saveCookie('douyin')"><Save :size="16" />更新抖音 Cookie</button></article><article><strong>X Cookie</strong><p>用于 gallery-dl 访问需要登录的内容。</p><textarea v-model="xCookieValue" placeholder="粘贴最新 X Cookie" /><small class="cookie-format-hint">格式要求：支持 Cookie Header String（例如 name=value; name2=value2）或 Netscape 格式，不支持 JSON。</small><button class="btn primary" @click="saveCookie('x')"><Save :size="16" />更新 X Cookie</button></article><article class="credential-path"><strong>X Cookie 文件</strong><p>可选：填写服务器上的 Cookie 文件路径，留空则使用上方保存的 Cookie。</p><div><input v-model="xCookieFile" placeholder="例如：/data/cookies/x.txt" /><button class="btn ghost" @click="saveXCookieFile"><Save :size="16" />保存路径</button></div></article></div></div>

    <div v-else-if="tab === 'runtime'" class="settings-panel"><header><Activity /><div><h3>下载、订阅与风控</h3><p>修改后下一次请求或调度周期生效</p></div><button class="btn primary" @click="saveRuntime"><Save :size="16" />保存运行配置</button></header><div class="form-grid">
      <label v-for="(spec, key) in limits" :key="key"><span>{{ spec.label }}</span><input v-if="spec.type !== 'bool'" v-model.number="runtime[key]" type="number" :min="spec.min" :max="spec.max" /><button v-else type="button" class="setting-switch" :class="{ on: runtime[key] }" role="switch" :aria-checked="Boolean(runtime[key])" @click="runtime[key] = !runtime[key]"><span class="switch-track"><i /></span><span>{{ runtime[key] ? '已开启' : '已关闭' }}</span></button><small>{{ spec.min != null ? `${spec.min}–${spec.max} ${spec.unit || ''}` : '向右为开启，向左为关闭' }}</small></label>
    </div></div>

    <div v-else-if="tab === 'process'" class="settings-panel"><header><Server /><div><h3>服务进程</h3><p>管理 Celery Worker 与定时调度器</p></div></header><div class="process-grid"><article v-for="target in ['worker','beat']" :key="target"><div><span class="health-dot" :class="{ online: process[target]?.running }" /><strong>{{ target === 'worker' ? '下载 Worker' : '定时调度 Beat' }}</strong></div><p>{{ process[target]?.running ? `运行中 · PID ${process[target]?.pid || '—'}` : '当前已停止' }}</p><footer><button class="btn ghost" @click="processAction(target as any, 'start')"><Play :size="15" />启动</button><button class="btn ghost" @click="processAction(target as any, 'stop')"><Square :size="15" />停止</button></footer></article></div></div>

    <div v-else-if="tab === 'logs'" class="settings-panel log-panel"><header><Activity /><div><h3>活动日志</h3><p>实时查看最近 500 条系统与任务事件</p></div><div class="header-actions"><button type="button" class="setting-switch" :class="{ on: live }" role="switch" :aria-checked="live" @click="live = !live"><span class="switch-track"><i /></span><span>实时刷新</span></button><button class="btn ghost compact" @click="copyLogs"><Clipboard :size="15" />复制</button><button class="btn ghost compact" @click="clearLogs"><Trash2 :size="15" />清空</button><button class="btn ghost compact" @click="loadLogs"><RefreshCw :size="15" />刷新</button></div></header><div class="log-filters"><button v-for="level in ['info','warning','error']" :key="level" :class="{ active: logLevels.includes(level) }" @click="toggleLevel(level)">{{ level }}</button></div><div class="log-console"><article v-for="(item, index) in filteredLogs" :key="`${item.ts}-${index}`" :data-level="item.level"><time>{{ new Date(item.ts * 1000).toLocaleString() }}</time><b>[{{ item.source }}]</b><span>{{ item.msg }}</span><small v-if="item.detail">{{ item.detail }}</small></article><div v-if="!filteredLogs.length" class="empty-state">暂无符合筛选条件的日志</div></div></div>

    <div v-else class="settings-panel"><header><Database /><div><h3>诊断与关于</h3><p>版本检查、更新诊断与运维入口</p></div></header><div class="about-grid"><article><strong>当前版本 {{ updateInfo.current?.short || '—' }}</strong><p>{{ updateInfo.message || '正在读取本地版本信息' }}<br />分支：{{ updateInfo.branch || '—' }}</p><div class="header-actions"><button class="btn ghost" :disabled="updateBusy" @click="checkUpdate">检查更新</button><button class="btn ghost" @click="diagnoseUpdate">复制诊断</button><button v-if="updateInfo.has_update" class="btn primary" :disabled="updateBusy || !updateInfo.update_supported" @click="applyUpdate">安装更新</button></div></article><article><strong>媒体下载管理系统</strong><p>FastAPI · PostgreSQL · Redis · Celery · Vue 3</p><div class="header-actions"><a class="btn ghost" href="/docs" target="_blank">API 文档</a><a class="btn ghost" href="/legacy">旧版界面</a></div></article></div><pre v-if="diagnostic" class="diagnostic-preview">{{ JSON.stringify(diagnostic, null, 2) }}</pre></div>
  </section>
</template>

<style scoped>
.settings-subtabs { margin: -4px 0 16px; padding-bottom: 12px; display: flex; gap: 6px; overflow-x: auto; border-bottom: 1px solid var(--line); }
.settings-subtabs button { padding: 7px 11px; white-space: nowrap; border: 1px solid var(--line); border-radius: 8px; background: var(--surface-2); color: var(--muted); cursor: pointer; }
.settings-subtabs button.active { border-color: var(--accent); background: var(--accent-soft); color: var(--accent); }
</style>
