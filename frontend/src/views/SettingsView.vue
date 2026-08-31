<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Activity, Archive, BellRing, Clipboard, Cookie, Database, Play, RefreshCw, Save, Server, Settings2, Square, Trash2 } from '@lucide/vue'
import { useRoute } from 'vue-router'
import { api, jsonBody } from '../api'
import { useAppStore } from '../stores/app'
import { riskTypeLabel } from '../localization'

const store = useAppStore(), route = useRoute()
const tab = ref(String(route.query.tab || 'general'))
const runtime = ref<any>({}), limits = ref<any>({}), cookieValue = ref(''), xCookieValue = ref('')
const douyinAccount = ref<any>({ user_agent: '', proxy_enabled: false }), douyinProxyValue = ref('')
const logs = ref<any[]>([]), logLevels = ref(['info', 'warning', 'error']), live = ref(true), process = ref<any>({})
const readiness = ref<any>({ components: {} })
const allFields = ref<any[]>([]), allValues = ref<any>({}), timer = ref<number>()
const secretValues = ref<Record<string, string>>({})
const updateInfo = ref<any>({}), diagnostic = ref<any>(null), updateBusy = ref(false)
const notificationTestBusy = ref(false)
const notificationTestChannel = ref<'all' | 'webhook' | 'bark' | 'email' | 'gotify'>('all')
const notificationTestResult = ref<Record<string, any>>({})
const archiveRules = ref<any>({
  directory_template: '{author}', filename_template: '{title}_{aweme_id}{index_suffix}.{ext}',
  work_types: ['video', 'images'], published_from: null, published_to: null,
  min_file_size_mb: 0, max_file_size_mb: 0, metadata_formats: [],
})
const filteredLogs = computed(() => logs.value.filter(item => logLevels.value.includes(item.level)))
const accountOnlyKeys = new Set(['DOUYIN_COOKIE', 'X_COOKIE', 'X_COOKIE_FILE'])
const generalFields = computed(() => allFields.value.filter(field => !accountOnlyKeys.has(field.key)))
const generalGroup = ref('')
const generalGroups = computed(() => [...new Set(generalFields.value.map(field => String(field.group || '其他')))])
const visibleGeneralFields = computed(() => generalFields.value.filter(field => String(field.group || '其他') === generalGroup.value))
const xCookieFile = ref('')
const tabs = [['general', '常规设置'], ['account', '平台账号'], ['runtime', '下载与风控'], ['archive', '归档与导出'], ['process', '服务进程'], ['logs', '活动日志'], ['about', '诊断与关于']]
const isBooleanField = (field: any) => ['true', 'false'].includes(String(field.default).toLowerCase())
const booleanFieldValue = (field: any) => String(allValues.value[field.key]?.value).toLowerCase() === 'true'
const readinessLabel = (name: string) => ({ configuration: '应用配置', database: '数据库', redis: 'Redis', worker: 'Celery Worker', beat: 'Celery Beat' }[name] || name)
function toggleBooleanField(field: any) { allValues.value[field.key].value = booleanFieldValue(field) ? 'false' : 'true' }

async function loadRuntime() { const data = await api<any>('/config/runtime'); runtime.value = data.config; limits.value = data.limits }
async function loadArchiveRules() { const data = await api<any>('/config/archive-rules'); archiveRules.value = data.rules }
async function loadDouyinAccount() { const data = await api<any>('/config/douyin-account'); douyinAccount.value = data.account || {} }
async function loadAll() { const data = await api<any>('/config/all'); allFields.value = data.fields; allValues.value = data.values; xCookieFile.value = data.values?.X_COOKIE_FILE?.value || ''; if (!generalGroups.value.includes(generalGroup.value)) generalGroup.value = generalGroups.value[0] || '' }
async function loadLogs(silent: boolean | Event = false) { try { logs.value = (await api<any>('/logs?start=0&count=500')).logs || [] } catch (e: any) { if (silent !== true) store.notify(e.message, 'error') } }
async function loadProcess() { process.value = await api('/process/status') }
async function loadReadiness() { readiness.value = await api('/status/readiness') }
async function loadUpdateInfo() { updateInfo.value = await api<any>('/update/info') }
async function init() {
  try { await Promise.all([loadRuntime(), loadArchiveRules(), loadDouyinAccount(), loadAll(), loadProcess(), loadReadiness(), loadLogs(), loadUpdateInfo()]) }
  catch (error: any) { store.notify(error.message || '加载设置失败', 'error') }
  if (timer.value != null) window.clearInterval(timer.value)
  timer.value = window.setInterval(() => { if (live.value && tab.value === 'logs') loadLogs(true) }, 3000)
}
async function saveRuntime() {
  try { const result = await api<any>('/config/runtime', { method: 'POST', ...jsonBody(runtime.value) }); runtime.value = result.data.config; store.notify('运行配置已保存'); await store.refreshStatus() }
  catch (error: any) { store.notify(error.message || '保存失败', 'error') }
}
function toggleArchiveValue(key: 'work_types' | 'metadata_formats', value: string) {
  const values = archiveRules.value[key] || []
  archiveRules.value[key] = values.includes(value) ? values.filter((item: string) => item !== value) : [...values, value]
}
async function saveArchiveRules() {
  try {
    const result = await api<any>('/config/archive-rules', { method: 'POST', ...jsonBody(archiveRules.value) })
    archiveRules.value = result.rules; store.notify(result.message || '归档规则已保存')
  } catch (error: any) { store.notify(error.message || '保存归档规则失败', 'error') }
}
async function saveAll() {
  const values: Record<string, any> = {}
  for (const field of allFields.value) {
    if (field.secret) {
      if (secretValues.value[field.key]?.trim()) values[field.key] = secretValues.value[field.key].trim()
    } else if (allValues.value[field.key]?.value !== undefined) values[field.key] = allValues.value[field.key].value
  }
  try { const result = await api<any>('/config/all', { method: 'POST', ...jsonBody({ values }) }); secretValues.value = {}; store.notify(result.message || '设置已保存'); await loadAll(); return true }
  catch (error: any) { store.notify(error.message || '保存失败', 'error'); return false }
}
async function testNotification() {
  notificationTestBusy.value = true
  try {
    if (!await saveAll()) return
    const result = await api<any>('/notifications/test', { method: 'POST', ...jsonBody({ channel: notificationTestChannel.value }) })
    notificationTestResult.value = result.data?.channels || {}
    store.notify(result.message || '通知测试完成', result.success ? 'success' : 'error')
  } catch (error: any) { store.notify(error.message || '通知测试失败', 'error') }
  finally { notificationTestBusy.value = false }
}
async function saveDouyinAccount() {
  const payload: Record<string, any> = { user_agent: douyinAccount.value.user_agent, proxy_enabled: Boolean(douyinAccount.value.proxy_enabled) }
  if (cookieValue.value.trim()) payload.cookie = cookieValue.value.trim()
  if (douyinProxyValue.value.trim()) payload.proxy_url = douyinProxyValue.value.trim()
  try { const result = await api<any>('/config/douyin-account', { method: 'POST', ...jsonBody(payload) }); douyinAccount.value = result.data; cookieValue.value = ''; douyinProxyValue.value = ''; store.notify(result.message); await store.refreshRisk() }
  catch (error: any) { store.notify(error.message || '保存抖音账号档案失败', 'error') }
}
async function saveXCookie() {
  const value = xCookieValue.value
  if (!value.trim()) return store.notify('Cookie 内容不能为空', 'error')
  try { const result = await api<any>('/x/config/cookie', { method: 'POST', ...jsonBody({ cookie: value.trim() }) }); store.notify(result.message); xCookieValue.value = '' }
  catch (error: any) { store.notify(error.message || '保存 Cookie 失败', 'error') }
}
async function saveXCookieFile() {
  try { const result = await api<any>('/config/all', { method: 'POST', ...jsonBody({ values: { X_COOKIE_FILE: xCookieFile.value.trim() } }) }); store.notify(result.message || 'X Cookie 文件路径已保存'); await loadAll() }
  catch (error: any) { store.notify(error.message || '保存 X Cookie 文件路径失败', 'error') }
}
async function processAction(target: 'worker' | 'beat', action: 'start' | 'stop') { try { const result = await api<any>(`/process/${target}/${action}`, { method: 'POST' }); store.notify(result.message || '操作完成'); await Promise.all([loadProcess(), loadReadiness()]) } catch (e: any) { store.notify(e.message, 'error') } }
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

    <div v-if="tab === 'general'" class="settings-panel"><header><Settings2 /><div><h3>基础配置</h3><p>按用途分类管理应用、目录、数据库、后台任务与通知配置</p></div><div class="header-actions"><select v-if="generalGroup === '通知'" v-model="notificationTestChannel" aria-label="通知测试渠道"><option value="all">全部渠道</option><option value="webhook">Webhook</option><option value="bark">Bark</option><option value="email">邮件</option><option value="gotify">Gotify</option></select><button v-if="generalGroup === '通知'" class="btn ghost" :disabled="notificationTestBusy" @click="testNotification"><BellRing :size="16" />保存并测试</button><button class="btn primary" @click="saveAll"><Save :size="16" />保存</button></div></header><nav class="settings-subtabs"><button v-for="group in generalGroups" :key="group" :class="{ active: generalGroup === group }" @click="generalGroup = group">{{ group }}</button></nav><div class="form-grid">
      <label v-for="field in visibleGeneralFields" :key="field.key"><span>{{ field.label }}</span><input v-if="field.secret" v-model="secretValues[field.key]" type="password" placeholder="留空保持当前值" /><button v-else-if="isBooleanField(field)" type="button" class="setting-switch" :class="{ on: booleanFieldValue(field) }" role="switch" :aria-checked="booleanFieldValue(field)" @click="toggleBooleanField(field)"><span class="switch-track"><i /></span><span>{{ booleanFieldValue(field) ? '已开启' : '已关闭' }}</span></button><input v-else v-model="allValues[field.key].value" :placeholder="field.default" /><small>{{ field.help || (field.secret ? '敏感值不会回显' : field.group) }}</small></label>
    </div><div v-if="generalGroup === '通知' && Object.keys(notificationTestResult).length" class="notification-test-result"><article v-for="(result, channel) in notificationTestResult" :key="channel" :data-success="result.success"><strong>{{ channel }}</strong><span>{{ result.message }}</span></article></div></div>

    <div v-else-if="tab === 'account'" class="settings-panel"><header><Cookie /><div><h3>平台登录凭据</h3><p>敏感内容加密保存且不会回显，抖音仅使用一个低并发请求上下文</p></div><button class="btn ghost" @click="loadDouyinAccount"><RefreshCw :size="15" />刷新健康状态</button></header><div class="credential-grid"><article class="douyin-account-card"><div class="account-health"><strong>抖音默认账号</strong><span :data-status="douyinAccount.status">{{ douyinAccount.status_label || '未配置' }}</span></div><p>Cookie、UIFID、User-Agent 与代理绑定使用；连续鉴权或风控异常会自动隔离，不会自动轮换账号。</p><div class="account-facts"><span>Cookie：{{ douyinAccount.configured ? `已加密 · ${douyinAccount.cookie_fingerprint}` : '未配置' }}</span><span>UIFID：{{ douyinAccount.has_uifid ? `已绑定 · ${douyinAccount.uifid_fingerprint}` : '缺失' }}</span><span>最近成功：{{ douyinAccount.last_success_at ? new Date(douyinAccount.last_success_at).toLocaleString() : '暂无' }}</span><span v-if="douyinAccount.last_failure_code">最近失败：{{ riskTypeLabel(douyinAccount.last_failure_code) }} · {{ douyinAccount.last_failure_at ? new Date(douyinAccount.last_failure_at).toLocaleString() : '时间未知' }} · 连续 {{ douyinAccount.consecutive_failures }} 次</span></div><textarea v-model="cookieValue" placeholder="留空保持当前 Cookie；粘贴新值时必须包含 UIFID" /><label class="account-field"><span>User-Agent</span><input v-model="douyinAccount.user_agent" placeholder="浏览器 User-Agent" /></label><label class="account-proxy"><button type="button" class="setting-switch" :class="{ on: douyinAccount.proxy_enabled }" role="switch" :aria-checked="Boolean(douyinAccount.proxy_enabled)" @click="douyinAccount.proxy_enabled = !douyinAccount.proxy_enabled"><span class="switch-track"><i /></span><span>{{ douyinAccount.proxy_enabled ? '使用代理' : '不使用代理' }}</span></button><input v-if="douyinAccount.proxy_enabled" v-model="douyinProxyValue" :placeholder="douyinAccount.proxy_label ? `留空保持 ${douyinAccount.proxy_label}` : 'http://user:password@host:port'" /></label><button class="btn primary" @click="saveDouyinAccount"><Save :size="16" />保存账号请求上下文</button></article><article><strong>X Cookie</strong><p>用于 gallery-dl 访问需要登录的内容。</p><textarea v-model="xCookieValue" placeholder="粘贴最新 X Cookie" /><small class="cookie-format-hint">格式要求：支持 Cookie Header String（例如 name=value; name2=value2）或 Netscape 格式，不支持 JSON。</small><button class="btn primary" @click="saveXCookie"><Save :size="16" />更新 X Cookie</button></article><article class="credential-path"><strong>X Cookie 文件</strong><p>可选：填写服务器上的 Cookie 文件路径，留空则使用上方保存的 Cookie。</p><div><input v-model="xCookieFile" placeholder="例如：/data/cookies/x.txt" /><button class="btn ghost" @click="saveXCookieFile"><Save :size="16" />保存路径</button></div></article></div></div>

    <div v-else-if="tab === 'runtime'" class="settings-panel"><header><Activity /><div><h3>下载、订阅与风控</h3><p>修改后下一次请求或调度周期生效</p></div><button class="btn primary" @click="saveRuntime"><Save :size="16" />保存运行配置</button></header><div class="form-grid">
      <label v-for="(spec, key) in limits" :key="key"><span>{{ spec.label }}</span><input v-if="spec.type !== 'bool'" v-model.number="runtime[key]" type="number" :min="spec.min" :max="spec.max" /><button v-else type="button" class="setting-switch" :class="{ on: runtime[key] }" role="switch" :aria-checked="Boolean(runtime[key])" @click="runtime[key] = !runtime[key]"><span class="switch-track"><i /></span><span>{{ runtime[key] ? '已开启' : '已关闭' }}</span></button><small>{{ spec.min != null ? `${spec.min}–${spec.max} ${spec.unit || ''}` : '向右为开启，向左为关闭' }}</small></label>
    </div></div>

    <div v-else-if="tab === 'archive'" class="settings-panel archive-panel"><header><Archive /><div><h3>归档与导出规则</h3><p>规则在创建任务时固化，修改不会影响已排队任务</p></div><button class="btn primary" @click="saveArchiveRules"><Save :size="16" />保存归档规则</button></header><div class="archive-grid">
      <label><span>目录模板</span><input v-model="archiveRules.directory_template" placeholder="{author}/{year}/{month}" /><small>可用：{author} {published_date} {year} {month} {work_type}</small></label>
      <label><span>文件名模板</span><input v-model="archiveRules.filename_template" placeholder="{title}_{aweme_id}{index_suffix}.{ext}" /><small>必须包含 {aweme_id}、{index} 或 {index_suffix}、{ext}</small></label>
      <fieldset><legend>作品类型</legend><button type="button" class="setting-switch" :class="{ on: archiveRules.work_types?.includes('video') }" @click="toggleArchiveValue('work_types', 'video')"><span class="switch-track"><i /></span><span>视频</span></button><button type="button" class="setting-switch" :class="{ on: archiveRules.work_types?.includes('images') }" @click="toggleArchiveValue('work_types', 'images')"><span class="switch-track"><i /></span><span>图集</span></button></fieldset>
      <div class="archive-pair"><label><span>发布时间起</span><input v-model="archiveRules.published_from" type="date" /></label><label><span>发布时间止</span><input v-model="archiveRules.published_to" type="date" /></label></div>
      <div class="archive-pair"><label><span>最小文件（MB）</span><input v-model.number="archiveRules.min_file_size_mb" type="number" min="0" step="0.1" /><small>0 表示不限制</small></label><label><span>最大文件（MB）</span><input v-model.number="archiveRules.max_file_size_mb" type="number" min="0" step="0.1" /><small>0 表示不限制</small></label></div>
      <fieldset><legend>同目录元数据</legend><button type="button" class="setting-switch" :class="{ on: archiveRules.metadata_formats?.includes('json') }" @click="toggleArchiveValue('metadata_formats', 'json')"><span class="switch-track"><i /></span><span>JSON</span></button><button type="button" class="setting-switch" :class="{ on: archiveRules.metadata_formats?.includes('csv') }" @click="toggleArchiveValue('metadata_formats', 'csv')"><span class="switch-track"><i /></span><span>CSV</span></button><small>每个媒体文件旁生成独立元数据文件；默认关闭。</small></fieldset>
    </div></div>

    <div v-else-if="tab === 'process'" class="settings-panel"><header><Server /><div><h3>服务进程</h3><p>管理 Celery Worker 与定时调度器</p></div><button class="btn ghost" @click="loadReadiness"><RefreshCw :size="15" />检查依赖</button></header><div class="readiness-grid"><article v-for="(component, name) in readiness.components" :key="name" :data-ready="component.ok"><span class="health-dot" :class="{ online: component.ok }" /><div><strong>{{ readinessLabel(String(name)) }}</strong><small>{{ component.message }}</small></div></article></div><div class="process-grid"><article v-for="target in ['worker','beat']" :key="target"><div><span class="health-dot" :class="{ online: process[target]?.running }" /><strong>{{ target === 'worker' ? '下载 Worker' : '定时调度 Beat' }}</strong></div><p>{{ process[target]?.running ? `运行中 · PID ${process[target]?.pid || '—'}` : '当前已停止' }}</p><footer><button class="btn ghost" @click="processAction(target as any, 'start')"><Play :size="15" />启动</button><button class="btn ghost" @click="processAction(target as any, 'stop')"><Square :size="15" />停止</button></footer></article></div></div>

    <div v-else-if="tab === 'logs'" class="settings-panel log-panel"><header><Activity /><div><h3>活动日志</h3><p>实时查看最近 500 条系统与任务事件</p></div><div class="header-actions"><button type="button" class="setting-switch" :class="{ on: live }" role="switch" :aria-checked="live" @click="live = !live"><span class="switch-track"><i /></span><span>实时刷新</span></button><button class="btn ghost compact" @click="copyLogs"><Clipboard :size="15" />复制</button><button class="btn ghost compact" @click="clearLogs"><Trash2 :size="15" />清空</button><button class="btn ghost compact" @click="loadLogs"><RefreshCw :size="15" />刷新</button></div></header><div class="log-filters"><button v-for="level in ['info','warning','error']" :key="level" :class="{ active: logLevels.includes(level) }" @click="toggleLevel(level)">{{ level }}</button></div><div class="log-console"><article v-for="(item, index) in filteredLogs" :key="`${item.ts}-${index}`" :data-level="item.level"><time>{{ new Date(item.ts * 1000).toLocaleString() }}</time><b>[{{ item.source }}]</b><span>{{ item.msg }}</span><small v-if="item.detail">{{ item.detail }}</small></article><div v-if="!filteredLogs.length" class="empty-state">暂无符合筛选条件的日志</div></div></div>

    <div v-else class="settings-panel"><header><Database /><div><h3>诊断与关于</h3><p>版本检查、更新诊断与运维入口</p></div></header><div class="about-grid"><article><strong>当前版本 {{ updateInfo.current?.short || '—' }}</strong><p>{{ updateInfo.message || '正在读取本地版本信息' }}<br />分支：{{ updateInfo.branch || '—' }}</p><div class="header-actions"><button class="btn ghost" :disabled="updateBusy" @click="checkUpdate">检查更新</button><button class="btn ghost" @click="diagnoseUpdate">复制诊断</button><button v-if="updateInfo.has_update" class="btn primary" :disabled="updateBusy || !updateInfo.update_supported" @click="applyUpdate">安装更新</button></div></article><article><strong>媒体下载管理系统</strong><p>FastAPI · PostgreSQL · Redis · Celery · Vue 3</p><div class="header-actions"><a class="btn ghost" href="/docs" target="_blank">API 文档</a><a class="btn ghost" href="/legacy">旧版界面</a></div></article></div><pre v-if="diagnostic" class="diagnostic-preview">{{ JSON.stringify(diagnostic, null, 2) }}</pre></div>
  </section>
</template>

<style scoped>
.settings-subtabs { margin: -4px 0 16px; padding-bottom: 12px; display: flex; gap: 6px; overflow-x: auto; border-bottom: 1px solid var(--line); }
.settings-subtabs button { padding: 7px 11px; white-space: nowrap; border: 1px solid var(--line); border-radius: 8px; background: var(--surface-2); color: var(--muted); cursor: pointer; }
.settings-subtabs button.active { border-color: var(--accent); background: var(--accent-soft); color: var(--accent); }
.notification-test-result { margin-top: 14px; display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
.notification-test-result article { padding: 10px 12px; display: grid; gap: 3px; border: 1px solid var(--line); border-radius: 9px; background: var(--surface-2); }
.notification-test-result article[data-success="true"] { border-color: color-mix(in srgb, var(--green) 45%, var(--line)); }
.notification-test-result strong { text-transform: capitalize; font-size: 11px; }
.notification-test-result span { color: var(--muted); font-size: 9px; }
.douyin-account-card { grid-row: span 2; }
.account-health { display:flex; align-items:center; justify-content:space-between; gap:10px; }
.account-health>span { padding:4px 7px; border-radius:6px; background:var(--surface-3); color:var(--muted); font-size:9px; }
.account-health>span[data-status="healthy"] { color:var(--green); }
.account-health>span[data-status="isolated"],.account-health>span[data-status="degraded"] { color:var(--red); }
.account-facts { margin:10px 0; display:grid; gap:4px; color:var(--muted); font-size:9px; }
.account-field,.account-proxy { margin:8px 0; display:grid; gap:6px; color:var(--muted); font-size:10px; }
.account-field input,.account-proxy input { width:100%; min-height:36px; padding:0 10px; }
.archive-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; }
.archive-grid>label,.archive-pair label { display:grid; gap:6px; }
.archive-grid input { width:100%; min-height:40px; padding:0 11px; }
.archive-grid small { color:var(--muted); font-size:9px; line-height:1.5; }
.archive-grid fieldset { margin:0; padding:12px; display:flex; flex-wrap:wrap; align-items:center; gap:12px; border:1px solid var(--line); border-radius:10px; }
.archive-grid legend { padding:0 5px; color:var(--muted); font-size:10px; }
.archive-grid fieldset small { flex-basis:100%; }
.archive-pair { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }
.readiness-grid { margin-bottom: 16px; display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; }
.readiness-grid article { padding: 11px 12px; display: flex; align-items: center; gap: 9px; border: 1px solid var(--line); border-radius: 9px; background: var(--surface-2); }
.readiness-grid article[data-ready="false"] { border-color: color-mix(in srgb, var(--red) 45%, var(--line)); }
.readiness-grid article div { min-width: 0; display: grid; gap: 2px; }
.readiness-grid article strong { font-size: 11px; }
.readiness-grid article small { overflow: hidden; color: var(--muted); font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }
@media(max-width:900px){.notification-test-result{grid-template-columns:repeat(2,1fr)}}
@media(max-width:900px){.readiness-grid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:720px){.archive-grid{grid-template-columns:1fr}.archive-pair{grid-template-columns:1fr}}
</style>
