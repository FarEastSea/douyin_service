<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  BellRing, ChevronDown, Clipboard, Cookie, HardDrive, Menu, Play, RefreshCw,
  Save, ShieldCheck, Square, Trash2, X,
} from '@lucide/vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRouter } from 'vue-router'
import { api, jsonBody, saveToken } from '../api'
import { focusFirst, restoreFocus, trapFocus } from '../focus'
import { useAppStore } from '../stores/app'
import {
  assignedEnvKeys, maintenanceNavigation, maintenancePages, platformCredentials,
  sectionPath, settingsNavigation, settingsPages, type SettingsFieldSection,
  type SettingsMode,
} from '../settings-registry'

const props = withDefaults(defineProps<{ mode?: SettingsMode; section?: string }>(), {
  mode: 'settings',
  section: 'application',
})
const router = useRouter()
const store = useAppStore()
type ResourceKey = 'all' | 'runtime' | 'archive' | 'douyinAccount' | 'platformCredentials' | 'logs'
type ResolvedField = {
  source: 'env' | 'runtime'
  key: string
  label: string
  help: string
  secret: boolean
  required: boolean
  kind: 'text' | 'number' | 'boolean'
  min?: number
  max?: number
}
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value ?? null))
const same = (left: unknown, right: unknown) => JSON.stringify(left) === JSON.stringify(right)

const runtime = ref<Record<string, any>>({})
const limits = ref<Record<string, any>>({})
const allFields = ref<any[]>([])
const allValues = ref<Record<string, any>>({})
const runtimeEnvKeys = ref<string[]>([])
const secretValues = ref<Record<string, string>>({})
const baselineRuntime = ref<Record<string, any>>({})
const baselineAll = ref<Record<string, any>>({})
const archiveRules = ref<any>({
  directory_template: '{author}',
  filename_template: '{title}_{aweme_id}{index_suffix}.{ext}',
  work_types: ['video', 'images'],
  published_from: null,
  published_to: null,
  min_file_size_mb: 0,
  max_file_size_mb: 0,
  metadata_formats: [],
})
const baselineArchive = ref<any>({})
const douyinAccount = ref<any>({ user_agent: '', proxy_enabled: false })
const baselineDouyinAccount = ref<any>({})
const douyinCookieValue = ref('')
const douyinProxyValue = ref('')
const xCookieValue = ref('')
const platformCookieValues = ref<Record<string, string>>({})
const platformCredentialStatus = ref<Record<string, any>>({})
const resourceState = ref<Record<ResourceKey, { loaded: boolean; error: string }>>({
  all: { loaded: false, error: '' },
  runtime: { loaded: false, error: '' },
  archive: { loaded: false, error: '' },
  douyinAccount: { loaded: false, error: '' },
  platformCredentials: { loaded: false, error: '' },
  logs: { loaded: false, error: '' },
})
const saving = ref(false)
const pageLoading = ref(false)
const saveError = ref('')
const expandedGroups = ref<string[]>([])
const directoryOpen = ref(false)
const mobileViewport = ref(false)
const directoryElement = ref<HTMLElement | null>(null)
const directoryTrigger = ref<HTMLElement | null>(null)
let mobileQuery: MediaQueryList | null = null

const logs = ref<any[]>([])
const logLevels = ref(['info', 'warning', 'error'])
const live = ref(true)
const process = ref<any>(null)
const readiness = ref<any>({ components: {} })
const platformReadiness = ref<any>({ items: [] })
const storageAudit = ref<any>(null)
const storageAuditState = ref<any>({ status: 'idle', progress: {} })
const lastStorageRepair = ref<any>(null)
const storageRepairAllState = ref<any>({ status: 'idle', progress: {} })
const platformAuditBusy = ref(false)
const storageAuditBusy = ref(false)
const storageRepairBusy = ref(false)
const storageRepairAllBusy = ref(false)
const storageRepairPlan = ref<any>(null)
const updateInfo = ref<any>({})
const diagnostic = ref<any>(null)
const updateBusy = ref(false)
const notificationTestBusy = ref(false)
const notificationTestChannel = ref<'all' | 'webhook' | 'bark' | 'email' | 'gotify'>('all')
const notificationTestResult = ref<Record<string, any>>({})
let logTimer: number | undefined
let storageAuditTimer: number | undefined

const pageMap = computed(() => props.mode === 'maintenance' ? maintenancePages : settingsPages)
const activePage = computed(() => pageMap.value[props.section] || pageMap.value[props.mode === 'maintenance' ? 'services' : 'application'])
const unknownFields = computed(() => {
  const runtimeKeys = new Set(runtimeEnvKeys.value)
  return allFields.value.filter(field => !assignedEnvKeys.has(field.key) && !runtimeKeys.has(field.key))
})
const navigation = computed(() => {
  const source = props.mode === 'maintenance' ? maintenanceNavigation : settingsNavigation
  const groups = source.map(group => ({ ...group, items: [...group.items] }))
  if (props.mode === 'settings' && unknownFields.value.length) {
    groups.push({ id: 'other', label: '兼容配置', items: [{ id: 'other', label: '其他配置' }] })
  }
  return groups
})
const activeGroupId = computed(() => navigation.value.find(group => group.items.some(item => item.id === activePage.value.id))?.id || '')
const activeFieldSections = computed<SettingsFieldSection[]>(() => {
  if (activePage.value.id === 'other') {
    return [{ title: '未分类配置', description: '这些字段来自较新的服务端版本，保存方式与其他基础配置一致。', envKeys: unknownFields.value.map(field => field.key) }]
  }
  return activePage.value.fieldSections || []
})
const allFieldMap = computed(() => Object.fromEntries(allFields.value.map(field => [field.key, field])))
const resolvedSections = computed(() => activeFieldSections.value.map(section => ({
  ...section,
  fields: [
    ...(section.envKeys || []).map(key => resolveEnvField(key)).filter(Boolean),
    ...(section.runtimeKeys || []).map(key => resolveRuntimeField(key)).filter(Boolean),
  ] as ResolvedField[],
})).filter(section => section.fields.length))
const activeEnvKeys = computed(() => [...new Set(activeFieldSections.value.flatMap(section => section.envKeys || []))])
const activeRuntimeKeys = computed(() => [...new Set(activeFieldSections.value.flatMap(section => section.runtimeKeys || []))])
const activePlatform = computed(() => platformCredentials.find(item => item.pageId === activePage.value.id))
const activeAccountPrefix = computed(() => ({
  'account-x': 'X',
  'account-tiktok': 'TIKTOK',
  'account-weibo': 'WEIBO',
  'account-bilibili': 'BILIBILI',
  'account-xhs': 'XHS',
} as Record<string, string>)[activePage.value.id] || '')
const activeAccountFacts = computed(() => {
  const prefix = activeAccountPrefix.value
  if (!prefix) return []
  return [
    { label: '下载引擎', value: allValues.value[`${prefix}_DOWNLOAD_ENGINE`]?.value || '未配置' },
    { label: 'Cookie 文件', value: allValues.value[`${prefix}_COOKIE_FILE`]?.value || '未配置' },
    {
      label: '网页 Cookie',
      value: activePage.value.id === 'account-x'
        ? '独立加密保存，不回显'
        : platformCredentialStatus.value[activePlatform.value?.id || '']?.configured ? '已配置' : '未配置',
    },
  ]
})
const filteredLogs = computed(() => logs.value.filter(item => logLevels.value.includes(item.level)))

function resolveEnvField(key: string): ResolvedField | null {
  const field = allFieldMap.value[key]
  if (!field) return null
  const defaultValue = String(field.default ?? '')
  const isBoolean = ['true', 'false'].includes(defaultValue.toLowerCase())
  const isNumber = !isBoolean && defaultValue !== '' && Number.isFinite(Number(defaultValue))
  return {
    source: 'env',
    key,
    label: field.label || key,
    help: field.help || (field.secret ? '敏感值不会回显；留空表示保持现有值。' : ''),
    secret: Boolean(field.secret),
    required: Boolean(field.required),
    kind: isBoolean ? 'boolean' : isNumber ? 'number' : 'text',
  }
}
function resolveRuntimeField(key: string): ResolvedField | null {
  const spec = limits.value[key]
  if (!spec) return null
  return {
    source: 'runtime',
    key,
    label: spec.label || key,
    help: spec.type === 'bool'
      ? '修改后在下一次任务调用或调度周期生效。'
      : [spec.min != null && spec.max != null ? String(spec.min) + '–' + String(spec.max) : '', spec.unit || ''].filter(Boolean).join(' '),
    secret: false,
    required: false,
    kind: spec.type === 'bool' ? 'boolean' : 'number',
    min: spec.min,
    max: spec.max,
  }
}
function fieldValue(field: ResolvedField) {
  if (field.secret) return secretValues.value[field.key] || ''
  return field.source === 'runtime' ? runtime.value[field.key] : allValues.value[field.key]?.value ?? ''
}
function setFieldValue(field: ResolvedField, value: any) {
  if (field.secret) {
    secretValues.value[field.key] = String(value)
  } else if (field.source === 'runtime') {
    runtime.value[field.key] = field.kind === 'number' ? Number(value) : value
  } else {
    if (!allValues.value[field.key]) allValues.value[field.key] = { value: '' }
    allValues.value[field.key].value = field.kind === 'boolean' ? String(Boolean(value)) : String(value)
  }
}
function fieldEnabled(field: ResolvedField) {
  return field.source === 'runtime'
    ? Boolean(runtime.value[field.key])
    : String(allValues.value[field.key]?.value).toLowerCase() === 'true'
}
function toggleField(field: ResolvedField) { setFieldValue(field, !fieldEnabled(field)) }
function envKeyDirty(key: string) {
  const field = allFieldMap.value[key]
  if (!field) return false
  if (field.secret) return Boolean(secretValues.value[key]?.trim())
  return !same(allValues.value[key]?.value, baselineAll.value[key])
}
function runtimeKeyDirty(key: string) { return !same(runtime.value[key], baselineRuntime.value[key]) }
const dirtyEnvKeys = computed(() => activeEnvKeys.value.filter(envKeyDirty))
const dirtyRuntimeKeys = computed(() => activeRuntimeKeys.value.filter(runtimeKeyDirty))
const archiveDirty = computed(() => activePage.value.id === 'archive' && !same(archiveRules.value, baselineArchive.value))
const douyinDirtyCount = computed(() => {
  if (activePage.value.id !== 'account-douyin') return 0
  let count = 0
  if (douyinCookieValue.value.trim()) count += 1
  if (douyinProxyValue.value.trim()) count += 1
  if (!same(douyinAccount.value.user_agent || '', baselineDouyinAccount.value.user_agent || '')) count += 1
  if (!same(Boolean(douyinAccount.value.proxy_enabled), Boolean(baselineDouyinAccount.value.proxy_enabled))) count += 1
  return count
})
const credentialDirty = computed(() => {
  if (activePage.value.id === 'account-x') return Boolean(xCookieValue.value.trim())
  if (activePlatform.value) return Boolean(platformCookieValues.value[activePlatform.value.id]?.trim())
  return false
})
const activeDirtyCount = computed(() =>
  dirtyEnvKeys.value.length + dirtyRuntimeKeys.value.length + (archiveDirty.value ? 1 : 0) +
  douyinDirtyCount.value + (credentialDirty.value ? 1 : 0)
)
const hasActiveChanges = computed(() => activeDirtyCount.value > 0)

const storageRepairTargets = computed(() => {
  const report = storageAudit.value
  if (!report) return []
  return [
    ...(report.relinkable_records || []).map((item: any) => ({ issue_type: 'stale_record_path', record_kind: item.kind, record_id: item.id, path: item.path })),
    ...(report.missing_records || []).map((item: any) => ({ issue_type: 'missing_record', record_kind: item.kind, record_id: item.id, path: item.path })),
    ...(report.zero_byte_files || []).map((item: any) => ({ issue_type: 'zero_byte_file', record_kind: item.kind, record_id: item.id, path: item.path })),
    ...(report.partial_files || []).map((item: any) => ({ issue_type: 'partial_file', path: item.path })),
    ...(report.orphan_files || []).map((path: string) => ({ issue_type: 'orphan_file', path })),
  ].slice(0, 200)
})
const eligibleRepairTargets = computed(() => (storageRepairPlan.value?.items || [])
  .filter((item: any) => item.eligible)
  .map((item: any) => ({ issue_type: item.issue_type, record_kind: item.record_kind, record_id: item.record_id, path: item.path })))
const storageIssueCount = computed(() => {
  const counts = storageAudit.value?.issue_counts
  if (!counts) return storageRepairTargets.value.length
  return Object.values(counts).reduce((total: number, value: any) => total + Number(value || 0), 0)
})
const readinessLabel = (name: string) => ({
  configuration: '应用配置', database: '数据库', redis: 'Redis',
  worker: 'Celery Worker', beat: 'Celery Beat', xhs_collector: '小红书下载服务',
} as Record<string, string>)[name] || name
const sourceLabel = (name: string) => ({ profile: '作者主页', work: '单条作品' } as Record<string, string>)[name] || name
function displayDateTime(value?: string) {
  if (!value) return '暂无成功记录'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN')
}

async function trackedLoad(key: ResourceKey, loader: () => Promise<void>) {
  try {
    await loader()
    resourceState.value[key] = { loaded: true, error: '' }
    return true
  } catch (error: any) {
    resourceState.value[key] = { loaded: false, error: error.message || '读取失败' }
    return false
  }
}
async function loadAll() {
  return trackedLoad('all', async () => {
    const data = await api<any>('/config/all')
    allFields.value = data.fields || []
    allValues.value = data.values || {}
    runtimeEnvKeys.value = data.runtime_keys || []
    baselineAll.value = Object.fromEntries(Object.entries(allValues.value).map(([key, item]: [string, any]) => [key, clone(item?.value)]))
    secretValues.value = {}
  })
}
async function loadRuntime() {
  return trackedLoad('runtime', async () => {
    const data = await api<any>('/config/runtime')
    runtime.value = data.config || {}
    limits.value = data.limits || {}
    baselineRuntime.value = clone(runtime.value)
  })
}
async function loadArchiveRules() {
  return trackedLoad('archive', async () => {
    const data = await api<any>('/config/archive-rules')
    archiveRules.value = data.rules
    baselineArchive.value = clone(data.rules)
  })
}
async function loadDouyinAccount() {
  return trackedLoad('douyinAccount', async () => {
    const data = await api<any>('/config/douyin-account')
    douyinAccount.value = data.account || {}
    baselineDouyinAccount.value = clone(douyinAccount.value)
    douyinCookieValue.value = ''
    douyinProxyValue.value = ''
  })
}
async function loadPlatformCredentialStatus() {
  return trackedLoad('platformCredentials', async () => {
    const rows = await Promise.all(platformCredentials.map(async item => [item.id, await api<any>('/platform-downloads/' + item.id + '/config/cookie')] as const))
    platformCredentialStatus.value = Object.fromEntries(rows)
  })
}
async function loadLogs(silent: boolean | Event = false) {
  const ok = await trackedLoad('logs', async () => { logs.value = (await api<any>('/logs?start=0&count=500')).logs || [] })
  if (!ok && silent !== true) store.notify(resourceState.value.logs.error || '活动日志读取失败', 'error')
  return ok
}
async function loadProcess() { process.value = await api('/process/status') }
async function loadReadiness() { readiness.value = await api('/status/readiness') }
async function loadPlatformReadiness() {
  platformAuditBusy.value = true
  try { platformReadiness.value = await api('/operations/platform-readiness') }
  catch (error: any) { store.notify(error.message || '平台验收状态加载失败', 'error') }
  finally { platformAuditBusy.value = false }
}
async function loadUpdateInfo() { updateInfo.value = await api<any>('/update/info') }
function stopStorageAuditPolling() {
  if (storageAuditTimer != null) window.clearInterval(storageAuditTimer)
  storageAuditTimer = undefined
}
function startStorageAuditPolling() {
  if (storageAuditTimer == null) storageAuditTimer = window.setInterval(() => refreshStorageAudit(true), 2000)
}
async function refreshStorageAudit(silent = false) {
  const previousStatus = storageAuditState.value?.status
  const previousRepairAllStatus = storageRepairAllState.value?.status
  try {
    const state = await api<any>('/operations/storage-audit')
    storageAuditState.value = state
    lastStorageRepair.value = state.last_repair || null
    storageRepairAllState.value = state.repair_all || { status: 'idle', progress: {} }
    storageAuditBusy.value = ['queued', 'running'].includes(state.status)
    storageRepairAllBusy.value = ['queued', 'running'].includes(storageRepairAllState.value.status)
    if (state.result) storageAudit.value = state.result
    if (storageAuditBusy.value || storageRepairAllBusy.value) startStorageAuditPolling()
    else stopStorageAuditPolling()
    if (state.status === 'completed' && previousStatus && !['completed', 'idle'].includes(previousStatus)) store.notify('存储巡检完成')
    if (state.status === 'failed' && previousStatus !== 'failed' && !silent) store.notify(state.error || '存储巡检失败', 'error')
    if (storageRepairAllState.value.status === 'completed' && ['queued', 'running'].includes(previousRepairAllStatus)) {
      store.notify('存储全部维护完成：已处理 ' + (storageRepairAllState.value.result?.applied || 0) + ' 项')
    }
    if (storageRepairAllState.value.status === 'failed' && previousRepairAllStatus !== 'failed' && !silent) {
      store.notify(storageRepairAllState.value.error || '存储全部维护失败', 'error')
    }
  } catch (error: any) {
    storageAuditBusy.value = false
    stopStorageAuditPolling()
    if (!silent) store.notify(error.message || '读取存储巡检状态失败', 'error')
  }
}
async function startStorageAudit() {
  storageAuditBusy.value = true
  storageRepairPlan.value = null
  try {
    storageAuditState.value = await api<any>('/operations/storage-audit', { method: 'POST' })
    startStorageAuditPolling()
  } catch (error: any) {
    storageAuditBusy.value = false
    store.notify(error.message || '提交存储巡检失败', 'error')
  }
}
async function previewStorageRepair() {
  if (!storageRepairTargets.value.length) return store.notify('当前没有需要处理的存储问题', 'info')
  storageRepairBusy.value = true
  try {
    storageRepairPlan.value = await api('/operations/storage-repair', {
      method: 'POST', ...jsonBody({ dry_run: true, targets: storageRepairTargets.value }),
    })
  } catch (error: any) { store.notify(error.message || '生成修复方案失败', 'error') }
  finally { storageRepairBusy.value = false }
}
async function applyStorageRepair() {
  const targets = eligibleRepairTargets.value
  if (!targets.length || !confirm('确认处理 ' + targets.length + ' 项？异常文件只移动到可恢复隔离区，不会直接删除。')) return
  storageRepairBusy.value = true
  try {
    const result = await api<any>('/operations/storage-repair', { method: 'POST', ...jsonBody({ dry_run: false, targets }) })
    lastStorageRepair.value = result.repair_state || null
    store.notify('本批已处理 ' + result.applied + ' 项，正在重新扫描剩余问题')
    await startStorageAudit()
  } catch (error: any) { store.notify(error.message || '执行存储维护失败', 'error') }
  finally { storageRepairBusy.value = false }
}
async function applyAllStorageRepairs() {
  const count = storageIssueCount.value
  if (!count || !confirm('确认在后台处理本次扫描发现的全部 ' + count + ' 项问题？')) return
  storageRepairAllBusy.value = true
  storageRepairPlan.value = null
  try {
    storageRepairAllState.value = await api<any>('/operations/storage-repair-all', { method: 'POST' })
    store.notify('存储全部维护已进入后台队列，可以离开页面')
    startStorageAuditPolling()
  } catch (error: any) {
    storageRepairAllBusy.value = false
    store.notify(error.message || '提交存储全部维护失败', 'error')
  }
}

async function loadActivePage() {
  pageLoading.value = true
  saveError.value = ''
  const jobs: Promise<any>[] = []
  if (props.mode === 'settings') {
    if (activePage.value.id === 'archive') jobs.push(loadArchiveRules())
    else if (activePage.value.id === 'account-douyin') jobs.push(loadDouyinAccount())
    else {
      jobs.push(loadAll())
      if (activeRuntimeKeys.value.length) jobs.push(loadRuntime())
      if (activePlatform.value) jobs.push(loadPlatformCredentialStatus())
    }
  } else if (activePage.value.id === 'services') {
    jobs.push(loadProcess(), loadReadiness())
  } else if (activePage.value.id === 'platforms') {
    jobs.push(loadPlatformReadiness())
  } else if (activePage.value.id === 'storage') {
    jobs.push(refreshStorageAudit(true))
  } else if (activePage.value.id === 'logs') {
    jobs.push(loadLogs())
  } else if (activePage.value.id === 'update') {
    jobs.push(loadUpdateInfo())
  }
  const results = await Promise.allSettled(jobs)
  const failures = results.filter(result => result.status === 'rejected' || (result.status === 'fulfilled' && result.value === false))
  if (failures.length) saveError.value = '有 ' + failures.length + ' 项状态读取失败；为避免覆盖服务端值，相关保存已禁用。'
  pageLoading.value = false
  configureLogPolling()
}
function configureLogPolling() {
  if (logTimer != null) window.clearInterval(logTimer)
  logTimer = undefined
  if (props.mode === 'maintenance' && activePage.value.id === 'logs') {
    logTimer = window.setInterval(() => { if (live.value) void loadLogs(true) }, 3000)
  }
}
function discardActiveChanges() {
  for (const key of activeEnvKeys.value) {
    if (allValues.value[key]) allValues.value[key].value = clone(baselineAll.value[key])
    delete secretValues.value[key]
  }
  for (const key of activeRuntimeKeys.value) runtime.value[key] = clone(baselineRuntime.value[key])
  if (activePage.value.id === 'archive') archiveRules.value = clone(baselineArchive.value)
  if (activePage.value.id === 'account-douyin') {
    douyinAccount.value = clone(baselineDouyinAccount.value)
    douyinCookieValue.value = ''
    douyinProxyValue.value = ''
  }
  if (activePage.value.id === 'account-x') xCookieValue.value = ''
  if (activePlatform.value) platformCookieValues.value[activePlatform.value.id] = ''
  saveError.value = ''
}
function confirmDiscard() {
  return !hasActiveChanges.value || confirm('当前子页有未保存修改，确定放弃并离开吗？')
}
async function navigateToSection(section: string) {
  if (section === activePage.value.id) return closeDirectory()
  if (!confirmDiscard()) return
  discardActiveChanges()
  closeDirectory()
  await router.push(sectionPath(props.mode, section))
}
async function refreshActivePage() {
  if (!confirmDiscard()) return
  discardActiveChanges()
  await loadActivePage()
}

async function saveEnvBlock(keys: string[]) {
  const dirtyKeys = keys.filter(envKeyDirty)
  if (!dirtyKeys.length) return false
  if (!resourceState.value.all.loaded) throw new Error('基础配置尚未成功读取')
  const values: Record<string, any> = {}
  for (const key of dirtyKeys) {
    const field = allFieldMap.value[key]
    values[key] = field?.secret ? secretValues.value[key].trim() : allValues.value[key]?.value
  }
  const result = await api<any>('/config/all', { method: 'POST', ...jsonBody({ values }) })
  if (result.data?.admin_token) saveToken(result.data.admin_token)
  for (const key of dirtyKeys) {
    const field = allFieldMap.value[key]
    if (field?.secret) delete secretValues.value[key]
    else baselineAll.value[key] = clone(allValues.value[key]?.value)
  }
  return true
}
async function saveRuntimeBlock(keys: string[]) {
  const dirtyKeys = keys.filter(runtimeKeyDirty)
  if (!dirtyKeys.length) return false
  if (!resourceState.value.runtime.loaded) throw new Error('运行配置尚未成功读取')
  const payload = Object.fromEntries(dirtyKeys.map(key => [key, runtime.value[key]]))
  const result = await api<any>('/config/runtime', { method: 'POST', ...jsonBody(payload) })
  for (const key of dirtyKeys) {
    if (result.data?.config && key in result.data.config) runtime.value[key] = result.data.config[key]
    baselineRuntime.value[key] = clone(runtime.value[key])
  }
  await store.refreshStatus()
  return true
}
async function saveArchiveBlock() {
  if (!archiveDirty.value) return false
  if (!resourceState.value.archive.loaded) throw new Error('归档规则尚未成功读取')
  const result = await api<any>('/config/archive-rules', { method: 'POST', ...jsonBody(archiveRules.value) })
  archiveRules.value = result.rules
  baselineArchive.value = clone(result.rules)
  return true
}
async function saveDouyinBlock() {
  if (!douyinDirtyCount.value) return false
  if (!resourceState.value.douyinAccount.loaded) throw new Error('抖音账号状态尚未成功读取')
  const payload: Record<string, any> = {}
  if (douyinCookieValue.value.trim()) payload.cookie = douyinCookieValue.value.trim()
  if (douyinProxyValue.value.trim()) payload.proxy_url = douyinProxyValue.value.trim()
  if (!same(douyinAccount.value.user_agent || '', baselineDouyinAccount.value.user_agent || '')) payload.user_agent = douyinAccount.value.user_agent
  if (!same(Boolean(douyinAccount.value.proxy_enabled), Boolean(baselineDouyinAccount.value.proxy_enabled))) payload.proxy_enabled = Boolean(douyinAccount.value.proxy_enabled)
  const result = await api<any>('/config/douyin-account', { method: 'POST', ...jsonBody(payload) })
  douyinAccount.value = result.data || {}
  baselineDouyinAccount.value = clone(douyinAccount.value)
  douyinCookieValue.value = ''
  douyinProxyValue.value = ''
  await store.refreshRisk()
  return true
}
async function saveCredentialBlock() {
  if (!credentialDirty.value) return false
  if (activePage.value.id === 'account-x') {
    await api<any>('/x/config/cookie', { method: 'POST', ...jsonBody({ cookie: xCookieValue.value.trim() }) })
    xCookieValue.value = ''
    return true
  }
  const platform = activePlatform.value
  if (!platform) return false
  await api<any>('/platform-downloads/' + platform.id + '/config/cookie', {
    method: 'POST',
    ...jsonBody({ cookie: platformCookieValues.value[platform.id].trim() }),
  })
  platformCookieValues.value[platform.id] = ''
  await loadPlatformCredentialStatus()
  return true
}
async function saveCurrentPage(options: { silentSuccess?: boolean } = {}) {
  if (!hasActiveChanges.value) {
    if (!options.silentSuccess) store.notify('当前子页没有需要保存的修改', 'info')
    return true
  }
  saving.value = true
  saveError.value = ''
  const errors: string[] = []
  let savedBlocks = 0
  const run = async (label: string, operation: () => Promise<boolean>) => {
    try { if (await operation()) savedBlocks += 1 }
    catch (error: any) { errors.push(label + '：' + (error.message || '保存失败')) }
  }
  if (activePage.value.id === 'archive') await run('归档规则', saveArchiveBlock)
  else if (activePage.value.id === 'account-douyin') await run('抖音账号', saveDouyinBlock)
  else {
    await run('基础配置', () => saveEnvBlock(activeEnvKeys.value))
    await run('运行配置', () => saveRuntimeBlock(activeRuntimeKeys.value))
    if (activePage.value.id === 'account-x' || activePlatform.value) await run('平台凭据', saveCredentialBlock)
  }
  saving.value = false
  if (errors.length) {
    saveError.value = errors.join('；')
    store.notify(savedBlocks ? '部分配置已保存，其余修改已保留' : '当前子页保存失败', 'error')
    return false
  }
  if (!options.silentSuccess) store.notify('“' + activePage.value.title + '”已保存')
  return true
}
async function saveAndTestNotification() {
  notificationTestBusy.value = true
  try {
    if (!await saveCurrentPage({ silentSuccess: true })) return
    const result = await api<any>('/notifications/test', {
      method: 'POST', ...jsonBody({ channel: notificationTestChannel.value }),
    })
    notificationTestResult.value = result.data?.channels || {}
    store.notify(result.message || '通知测试完成', result.success ? 'success' : 'error')
  } catch (error: any) { store.notify(error.message || '通知测试失败', 'error') }
  finally { notificationTestBusy.value = false }
}
function toggleArchiveValue(key: 'work_types' | 'metadata_formats', value: string) {
  const values = archiveRules.value[key] || []
  archiveRules.value[key] = values.includes(value) ? values.filter((item: string) => item !== value) : [...values, value]
}
async function processAction(target: 'worker' | 'beat', action: 'start' | 'stop') {
  try {
    const result = await api<any>('/process/' + target + '/' + action, { method: 'POST' })
    store.notify(result.message || '操作完成')
    await Promise.all([loadProcess(), loadReadiness()])
  } catch (error: any) { store.notify(error.message || '进程操作失败', 'error') }
}
async function clearLogs() {
  if (!resourceState.value.logs.loaded || !confirm('确定清空活动日志？')) return
  await api('/logs', { method: 'DELETE' })
  logs.value = []
  store.notify('日志已清空')
}
async function copyLogs() {
  if (!resourceState.value.logs.loaded) return
  const text = filteredLogs.value.map(item =>
    new Date(item.ts * 1000).toLocaleString() + ' [' + item.level + '] [' + item.source + ']' +
    (item.event_code ? ' [' + item.event_code + ']' : '') +
    (item.correlation_id ? ' [请求 ' + item.correlation_id + ']' : '') + ' ' + item.msg +
    (item.detail ? '\n' + item.detail : '') +
    (Object.keys(item.context || {}).length ? '\n上下文: ' + JSON.stringify(item.context) : '')
  ).join('\n\n')
  await navigator.clipboard.writeText(text)
  store.notify('日志已复制（敏感值已脱敏）')
}
async function checkUpdate() {
  updateBusy.value = true
  try { updateInfo.value = await api<any>('/update/check'); store.notify(updateInfo.value.message || '检查完成') }
  catch (error: any) { store.notify(error.message || '检查更新失败', 'error') }
  finally { updateBusy.value = false }
}
async function diagnoseUpdate() {
  try {
    diagnostic.value = await api<any>('/update/diagnose')
    await navigator.clipboard.writeText(JSON.stringify(diagnostic.value, null, 2))
    store.notify('诊断信息已复制')
  } catch (error: any) { store.notify(error.message || '诊断失败', 'error') }
}
async function applyUpdate() {
  if (!confirm('确定拉取远程更新并重启 Worker/Beat？')) return
  updateBusy.value = true
  try { const result = await api<any>('/update/apply', { method: 'POST' }); store.notify(result.message || '更新完成'); await loadUpdateInfo() }
  catch (error: any) { store.notify(error.message || '更新失败', 'error') }
  finally { updateBusy.value = false }
}
function toggleLevel(level: string) {
  logLevels.value = logLevels.value.includes(level) ? logLevels.value.filter(value => value !== level) : [...logLevels.value, level]
}
function toggleGroup(groupId: string) {
  expandedGroups.value = expandedGroups.value.includes(groupId)
    ? expandedGroups.value.filter(id => id !== groupId)
    : [...expandedGroups.value, groupId]
}
function openDirectory() { directoryOpen.value = true; void nextTick(() => focusFirst(directoryElement.value)) }
function closeDirectory() {
  if (!directoryOpen.value) return
  directoryOpen.value = false
  void nextTick(() => restoreFocus(directoryTrigger.value))
}
function handleGlobalKeydown(event: KeyboardEvent) {
  if (!directoryOpen.value || !mobileViewport.value) return
  if (event.key === 'Escape') { event.preventDefault(); closeDirectory() }
  else trapFocus(event, directoryElement.value)
}
function beforeUnload(event: BeforeUnloadEvent) {
  if (!hasActiveChanges.value) return
  event.preventDefault()
  event.returnValue = ''
}
function updateViewport(event?: MediaQueryListEvent) {
  mobileViewport.value = event ? event.matches : Boolean(mobileQuery?.matches)
  if (!mobileViewport.value) directoryOpen.value = false
}

onBeforeRouteLeave(() => confirmDiscard())
onBeforeRouteUpdate(() => confirmDiscard())
watch(() => [props.mode, props.section] as const, async () => {
  const defaultSection = props.mode === 'maintenance' ? 'services' : 'application'
  if (!pageMap.value[props.section]) {
    await router.replace(sectionPath(props.mode, defaultSection))
    return
  }
  if (activeGroupId.value && !expandedGroups.value.includes(activeGroupId.value)) {
    expandedGroups.value = [...expandedGroups.value, activeGroupId.value]
  }
  closeDirectory()
  await loadActivePage()
}, { immediate: true })
watch(activeGroupId, groupId => {
  if (groupId && !expandedGroups.value.includes(groupId) && groupId !== 'archive') {
    expandedGroups.value = [...expandedGroups.value, groupId]
  }
})
onMounted(() => {
  mobileQuery = window.matchMedia('(max-width: 900px)')
  updateViewport()
  mobileQuery.addEventListener('change', updateViewport)
  window.addEventListener('keydown', handleGlobalKeydown)
  window.addEventListener('beforeunload', beforeUnload)
})
onBeforeUnmount(() => {
  if (logTimer != null) window.clearInterval(logTimer)
  stopStorageAuditPolling()
  mobileQuery?.removeEventListener('change', updateViewport)
  window.removeEventListener('keydown', handleGlobalKeydown)
  window.removeEventListener('beforeunload', beforeUnload)
})
</script>

<template>
  <section class="workspace-card config-workspace">
    <header class="workspace-header config-shell-header">
      <div>
        <h2>{{ props.mode === 'maintenance' ? '运行维护' : '设置' }}</h2>
        <span>{{ props.mode === 'maintenance' ? '核对服务状态、存储与诊断证据；维护动作始终由你确认。' : '所有网页配置保存后动态生效，敏感值留空表示保持不变。' }}</span>
      </div>
      <button ref="directoryTrigger" class="btn ghost directory-trigger" type="button" aria-haspopup="dialog" :aria-expanded="directoryOpen" @click="openDirectory">
        <Menu :size="16" />{{ props.mode === 'maintenance' ? '维护目录' : '设置目录' }}
      </button>
    </header>

    <div class="config-layout">
      <button v-if="directoryOpen && mobileViewport" class="directory-backdrop" type="button" aria-label="关闭目录" @click="closeDirectory" />
      <aside
        v-if="!mobileViewport || directoryOpen"
        ref="directoryElement"
        class="config-directory"
        :class="{ open: directoryOpen }"
        :role="mobileViewport ? 'dialog' : undefined"
        :aria-modal="mobileViewport ? 'true' : undefined"
        :aria-label="props.mode === 'maintenance' ? '运行维护目录' : '设置目录'"
        tabindex="-1"
      >
        <div class="directory-mobile-header">
          <strong>{{ props.mode === 'maintenance' ? '运行维护目录' : '设置目录' }}</strong>
          <button class="icon-btn" type="button" aria-label="关闭目录" @click="closeDirectory"><X :size="17" /></button>
        </div>
        <nav>
          <section v-for="group in navigation" :key="group.id" class="directory-group">
            <button
              v-if="group.id === 'archive'"
              class="directory-group-button directory-direct"
              :class="{ active: activePage.id === 'archive' }"
              type="button"
              :aria-current="activePage.id === 'archive' ? 'page' : undefined"
              @click="navigateToSection('archive')"
            >{{ group.label }}</button>
            <button
              v-else
              class="directory-group-button"
              type="button"
              :aria-expanded="expandedGroups.includes(group.id)"
              :aria-controls="'directory-' + group.id"
              @click="toggleGroup(group.id)"
            >
              <span>{{ group.label }}</span>
              <ChevronDown :size="15" :class="{ rotated: expandedGroups.includes(group.id) }" />
            </button>
            <div v-if="group.id !== 'archive'" v-show="expandedGroups.includes(group.id)" :id="'directory-' + group.id" class="directory-children">
              <button
                v-for="item in group.items"
                :key="item.id"
                type="button"
                :class="{ active: activePage.id === item.id }"
                :aria-current="activePage.id === item.id ? 'page' : undefined"
                @click="navigateToSection(item.id)"
              >{{ item.label }}</button>
            </div>
          </section>
        </nav>
      </aside>

      <main class="config-content">
        <header class="config-page-bar">
          <div>
            <span>{{ navigation.find(group => group.id === activeGroupId)?.label }}</span>
            <strong>{{ activePage.title }}</strong>
          </div>
          <div class="page-actions">
            <span v-if="hasActiveChanges" class="dirty-indicator" role="status">{{ activeDirtyCount }} 项未保存</span>
            <button v-if="hasActiveChanges" class="btn ghost" type="button" @click="discardActiveChanges">放弃修改</button>
            <button class="btn ghost" type="button" :disabled="pageLoading || saving" @click="refreshActivePage">
              <RefreshCw :size="15" />{{ pageLoading ? '刷新中…' : '刷新' }}
            </button>
            <button v-if="props.mode === 'settings'" class="btn primary" type="button" :disabled="saving || !hasActiveChanges" @click="saveCurrentPage()">
              <Save :size="15" />{{ saving ? '保存中…' : '保存当前子页' }}
            </button>
          </div>
        </header>

        <div class="config-page">
          <header class="config-page-intro">
            <h3>{{ activePage.title }}</h3>
            <p>{{ activePage.description }}</p>
          </header>
          <p v-if="saveError" class="config-alert" role="alert">{{ saveError }}</p>

          <template v-if="props.mode === 'settings'">
            <template v-if="resolvedSections.length && !(activePage.id === 'account-x' || activePlatform)">
            <section v-for="section in resolvedSections" :key="section.title" class="setting-section">
              <header>
                <div><h4>{{ section.title }}</h4><p>{{ section.description }}</p></div>
              </header>
              <div class="setting-list">
                <label v-for="field in section.fields" :key="field.source + '-' + field.key" class="setting-row">
                  <span class="setting-copy">
                    <strong>{{ field.label }}<i v-if="field.required" aria-label="必填">必填</i></strong>
                    <small>{{ field.help || field.key }}</small>
                  </span>
                  <span class="setting-control">
                    <button
                      v-if="field.kind === 'boolean'"
                      class="setting-switch"
                      :class="{ on: fieldEnabled(field) }"
                      type="button"
                      role="switch"
                      :aria-checked="fieldEnabled(field)"
                      @click.prevent="toggleField(field)"
                    >
                      <span class="switch-track"><i /></span>
                      <span>{{ fieldEnabled(field) ? '已开启' : '已关闭' }}</span>
                    </button>
                    <input
                      v-else
                      :type="field.secret ? 'password' : field.kind"
                      :value="fieldValue(field)"
                      :min="field.min"
                      :max="field.max"
                      :placeholder="field.secret ? '留空保持当前值' : undefined"
                      :autocomplete="field.secret ? 'new-password' : 'off'"
                      @input="setFieldValue(field, ($event.target as HTMLInputElement).value)"
                    />
                  </span>
                </label>
              </div>
            </section>

            </template>

            <section v-if="activePage.id === 'notifications'" class="setting-section">
              <header><div><h4>发送测试</h4><p>先保存当前子页，再使用服务端实际配置发送一条测试通知。</p></div></header>
              <div class="setting-list">
                <div class="setting-row">
                  <span class="setting-copy"><strong>测试渠道</strong><small>测试结果不会包含密钥或带 Token 的完整地址。</small></span>
                  <span class="setting-control inline-control">
                    <select v-model="notificationTestChannel" aria-label="通知测试渠道">
                      <option value="all">全部渠道</option><option value="webhook">Webhook</option>
                      <option value="bark">Bark</option><option value="email">邮件</option><option value="gotify">Gotify</option>
                    </select>
                    <button class="btn ghost" type="button" :disabled="notificationTestBusy" @click="saveAndTestNotification">
                      <BellRing :size="15" />{{ notificationTestBusy ? '测试中…' : '保存并测试' }}
                    </button>
                  </span>
                </div>
                <div v-if="Object.keys(notificationTestResult).length" class="notification-result">
                  <span v-for="(result, channel) in notificationTestResult" :key="String(channel)"><strong>{{ channel }}</strong>{{ result.success ? '成功' : result.message || '失败' }}</span>
                </div>
              </div>
            </section>

            <section v-if="activePage.id === 'account-douyin'" class="account-page">
              <div class="account-summary">
                <span class="health-dot" :class="{ online: douyinAccount.configured && douyinAccount.has_uifid }" />
                <div><strong>{{ douyinAccount.status_label || '账号状态未确认' }}</strong><p>{{ douyinAccount.cookie_fingerprint ? 'Cookie 指纹 ' + douyinAccount.cookie_fingerprint : 'Cookie 不回显，更新时粘贴完整值。' }}</p></div>
                <span class="status" :data-tone="douyinAccount.configured ? 'success' : 'warning'">{{ douyinAccount.configured ? '已配置' : '未配置' }}</span>
              </div>
              <section class="setting-section">
                <header><div><h4>账号事实</h4><p>用于判断当前请求上下文是否具备抖音网页接口需要的浏览器身份。</p></div></header>
                <dl class="fact-list">
                  <div><dt>UIFID</dt><dd>{{ douyinAccount.has_uifid ? '已包含' : '缺失' }}</dd></div>
                  <div><dt>最近成功</dt><dd>{{ displayDateTime(douyinAccount.last_success_at) }}</dd></div>
                  <div><dt>代理</dt><dd>{{ douyinAccount.proxy_enabled ? (douyinAccount.proxy_label || '已启用') : '未启用' }}</dd></div>
                </dl>
              </section>
              <section class="setting-section">
                <header><div><h4>请求凭据</h4><p>Cookie 和代理地址留空表示不修改已保存的加密值。</p></div></header>
                <div class="setting-list">
                  <label class="setting-row"><span class="setting-copy"><strong>抖音 Cookie</strong><small>粘贴浏览器请求头中的完整 Cookie 字符串。</small></span><span class="setting-control"><textarea v-model="douyinCookieValue" rows="4" placeholder="留空保持当前 Cookie" /></span></label>
                  <label class="setting-row"><span class="setting-copy"><strong>User-Agent</strong><small>应与获取 Cookie 的浏览器保持一致。</small></span><span class="setting-control"><input v-model="douyinAccount.user_agent" autocomplete="off" /></span></label>
                  <div class="setting-row"><span class="setting-copy"><strong>使用代理</strong><small>Cookie、UIFID、User-Agent 和代理共同组成请求上下文。</small></span><span class="setting-control"><button class="setting-switch" :class="{ on: douyinAccount.proxy_enabled }" type="button" role="switch" :aria-checked="Boolean(douyinAccount.proxy_enabled)" @click="douyinAccount.proxy_enabled = !douyinAccount.proxy_enabled"><span class="switch-track"><i /></span><span>{{ douyinAccount.proxy_enabled ? '已开启' : '已关闭' }}</span></button></span></div>
                  <label v-if="douyinAccount.proxy_enabled" class="setting-row"><span class="setting-copy"><strong>代理地址</strong><small>留空保持当前加密代理地址。</small></span><span class="setting-control"><input v-model="douyinProxyValue" type="password" autocomplete="new-password" placeholder="留空保持当前值" /></span></label>
                </div>
              </section>
            </section>

            <section v-if="activePage.id === 'account-x' || activePlatform" class="account-page">
              <div class="account-summary">
                <Cookie :size="19" />
                <div><strong>{{ activePage.title }}</strong><p v-if="activePage.id === 'account-x'">Cookie 用于 gallery-dl 访问需要登录的 X 内容。</p><p v-else>{{ platformCredentialStatus[activePlatform?.id || '']?.configured ? '网页加密 Cookie 已配置。' : '当前尚未配置网页加密 Cookie。' }}</p></div>
                <span class="status" :data-tone="activePage.id === 'account-x' || platformCredentialStatus[activePlatform?.id || '']?.configured ? 'success' : 'warning'">{{ activePage.id === 'account-x' ? '按需更新' : platformCredentialStatus[activePlatform?.id || '']?.configured ? '已配置' : '未配置' }}</span>
              </div>
              <section class="setting-section">
                <header><div><h4>账号事实</h4><p>核对当前下载引擎、文件凭据和网页凭据状态。</p></div></header>
                <dl class="fact-list">
                  <div v-for="fact in activeAccountFacts" :key="fact.label"><dt>{{ fact.label }}</dt><dd>{{ fact.value }}</dd></div>
                </dl>
              </section>
              <section v-for="section in resolvedSections" :key="section.title" class="setting-section">
                <header><div><h4>{{ section.title }}</h4><p>{{ section.description }}</p></div></header>
                <div class="setting-list">
                  <label v-for="field in section.fields" :key="field.source + '-' + field.key" class="setting-row">
                    <span class="setting-copy"><strong>{{ field.label }}<i v-if="field.required" aria-label="必填">必填</i></strong><small>{{ field.help || field.key }}</small></span>
                    <span class="setting-control">
                      <button v-if="field.kind === 'boolean'" class="setting-switch" :class="{ on: fieldEnabled(field) }" type="button" role="switch" :aria-checked="fieldEnabled(field)" @click.prevent="toggleField(field)"><span class="switch-track"><i /></span><span>{{ fieldEnabled(field) ? '已开启' : '已关闭' }}</span></button>
                      <input v-else :type="field.secret ? 'password' : field.kind" :value="fieldValue(field)" :min="field.min" :max="field.max" :placeholder="field.secret ? '留空保持当前值' : undefined" :autocomplete="field.secret ? 'new-password' : 'off'" @input="setFieldValue(field, ($event.target as HTMLInputElement).value)" />
                    </span>
                  </label>
                </div>
              </section>
              <section class="setting-section">
                <header><div><h4>登录凭据</h4><p>敏感值单独加密保存，不会通过配置接口回显。</p></div></header>
                <div class="setting-list">
                  <label class="setting-row">
                    <span class="setting-copy"><strong>{{ activePage.id === 'account-x' ? 'X Cookie' : activePlatform?.name + ' Cookie' }}</strong><small>支持 Cookie Header String；留空表示保持现有值。</small></span>
                    <span class="setting-control"><textarea v-if="activePage.id === 'account-x'" v-model="xCookieValue" rows="5" placeholder="留空保持当前 Cookie" /><textarea v-else v-model="platformCookieValues[activePlatform!.id]" rows="5" placeholder="留空保持当前 Cookie" /></span>
                  </label>
                </div>
              </section>
              <section v-if="activePage.id === 'account-x' || activePlatform?.id === 'xhs'" class="capability-note">
                <ShieldCheck :size="18" />
                <div><strong>{{ activePlatform?.id === 'xhs' ? '仅支持单条笔记' : '支持作者主页与单条作品' }}</strong><p>{{ activePlatform?.id === 'xhs' ? '小红书作者主页批量采集已搁置；这里的凭据仅用于单条图文、视频和实况笔记。' : 'X 的作者主页与单条作品沿用同一套下载引擎和凭据。' }}</p></div>
              </section>
            </section>

            <section v-if="activePage.id === 'archive'" class="archive-page">
              <section class="setting-section">
                <header><div><h4>路径与文件名</h4><p>规则在创建任务时固化，不影响已排队任务。</p></div></header>
                <div class="setting-list">
                  <label class="setting-row"><span class="setting-copy"><strong>目录模板</strong><small>可用：{author} {published_date} {year} {month} {work_type}</small></span><span class="setting-control"><input v-model="archiveRules.directory_template" /></span></label>
                  <label class="setting-row"><span class="setting-copy"><strong>文件名模板</strong><small>必须包含作品 ID、序号后缀和扩展名变量。</small></span><span class="setting-control"><input v-model="archiveRules.filename_template" /></span></label>
                </div>
              </section>
              <section class="setting-section">
                <header><div><h4>内容范围</h4><p>筛选新任务允许归档的作品类型、发布时间和文件大小。</p></div></header>
                <div class="setting-list">
                  <div class="setting-row"><span class="setting-copy"><strong>作品类型</strong><small>至少保留一种需要下载的媒体类型。</small></span><span class="setting-control inline-control"><button v-for="item in [{ id: 'video', label: '视频' }, { id: 'images', label: '图集' }]" :key="item.id" class="choice-button" :class="{ active: archiveRules.work_types?.includes(item.id) }" type="button" :aria-pressed="archiveRules.work_types?.includes(item.id)" @click="toggleArchiveValue('work_types', item.id)">{{ item.label }}</button></span></div>
                  <div class="setting-row"><span class="setting-copy"><strong>发布时间</strong><small>留空表示不限制。</small></span><span class="setting-control paired-control"><input v-model="archiveRules.published_from" type="date" aria-label="发布时间起" /><input v-model="archiveRules.published_to" type="date" aria-label="发布时间止" /></span></div>
                  <div class="setting-row"><span class="setting-copy"><strong>文件大小（MB）</strong><small>0 表示不限制。</small></span><span class="setting-control paired-control"><input v-model.number="archiveRules.min_file_size_mb" type="number" min="0" step="0.1" aria-label="最小文件大小" /><input v-model.number="archiveRules.max_file_size_mb" type="number" min="0" step="0.1" aria-label="最大文件大小" /></span></div>
                  <div class="setting-row"><span class="setting-copy"><strong>同目录元数据</strong><small>在每个媒体文件旁生成独立元数据文件。</small></span><span class="setting-control inline-control"><button v-for="item in ['json', 'csv']" :key="item" class="choice-button" :class="{ active: archiveRules.metadata_formats?.includes(item) }" type="button" :aria-pressed="archiveRules.metadata_formats?.includes(item)" @click="toggleArchiveValue('metadata_formats', item)">{{ item.toUpperCase() }}</button></span></div>
                </div>
              </section>
            </section>
          </template>

          <template v-else-if="props.mode === 'maintenance'">
            <section v-if="activePage.id === 'services'" class="maintenance-page">
              <section class="setting-section">
                <header><div><h4>依赖状态</h4><p>Web、数据库、Redis 和后台任务的当前可用性。</p></div></header>
                <div class="status-list">
                  <div v-for="(component, name) in readiness.components" :key="String(name)" class="status-row"><span class="health-dot" :class="{ online: component.ok }" /><span><strong>{{ readinessLabel(String(name)) }}</strong><small>{{ component.message }}</small></span><b :class="{ good: component.ok }">{{ component.ok ? '正常' : '异常' }}</b></div>
                </div>
              </section>
              <section class="setting-section">
                <header><div><h4>后台进程</h4><p>启动和停止动作继续使用现有进程管理方式。</p></div></header>
                <div class="status-list">
                  <div v-for="target in ['worker', 'beat']" :key="target" class="status-row"><span class="health-dot" :class="{ online: process?.[target]?.running }" /><span><strong>{{ target === 'worker' ? '下载 Worker' : '定时调度 Beat' }}</strong><small>{{ process?.[target]?.running ? '运行中 · PID ' + (process[target].pid || '—') : '当前已停止' }}</small></span><span class="row-buttons"><button class="btn ghost compact" type="button" :disabled="!process?.[target]" @click="processAction(target as any, 'start')"><Play :size="14" />启动</button><button class="btn ghost compact" type="button" :disabled="!process?.[target]" @click="processAction(target as any, 'stop')"><Square :size="14" />停止</button></span></div>
                </div>
              </section>
            </section>

            <section v-else-if="activePage.id === 'platforms'" class="maintenance-page">
              <section class="setting-section">
                <header><div><h4>平台就绪状态</h4><p>当前运行版本 {{ platformReadiness.revision ? String(platformReadiness.revision).slice(0, 12) : '未确认' }}；真实成功记录必须同时通过本地文件核验。</p></div></header>
                <div class="platform-table" role="table" aria-label="平台就绪状态">
                  <div class="platform-table-head" role="row"><span role="columnheader">平台</span><span role="columnheader">能力与引擎</span><span role="columnheader">依赖</span><span role="columnheader">验收</span></div>
                  <div v-for="item in platformReadiness.items" :key="item.platform" class="platform-table-row" role="row">
                    <span role="cell"><strong>{{ item.name }}</strong><small>{{ item.status === 'ready' ? '本地就绪' : item.status === 'degraded' ? '可用但待完善' : '阻塞' }}</small></span>
                    <span role="cell"><strong>{{ item.engine }}</strong><small>{{ item.supported_sources.map(sourceLabel).join(' / ') || '未开放下载' }}</small></span>
                    <span role="cell"><small>Cookie {{ item.cookie_configured ? '已配置' : '未配置' }} · FFmpeg {{ item.ffmpeg_ready ? '可用' : '未安装' }} · 目录{{ item.download_root.writable ? '可写' : '不可写' }}</small></span>
                    <span role="cell"><strong>{{ item.external_validation_complete ? '已完成真实验收' : item.external_tested ? '已有记录，待补齐' : '尚无真实成功记录' }}</strong><small>{{ displayDateTime(item.last_external_success_at) }}</small></span>
                    <ul v-if="item.blockers.length || item.warnings.length"><li v-for="message in [...item.blockers, ...item.warnings]" :key="message">{{ message }}</li></ul>
                  </div>
                </div>
              </section>
            </section>

            <section v-else-if="activePage.id === 'storage'" class="maintenance-page">
              <section class="setting-section">
                <header>
                  <div><h4>巡检状态</h4><p v-if="storageRepairAllBusy">后台全部维护进行中，已处理 {{ storageRepairAllState.progress?.applied || 0 }} 项。</p><p v-else-if="storageAuditBusy">正在扫描 {{ storageAuditState.progress?.scanned_records || 0 }} 条记录、{{ storageAuditState.progress?.scanned_files || 0 }} 个文件；离开页面后仍会继续。</p><p v-else-if="storageAudit">已扫描 {{ storageAudit.scanned_records }} 条记录、{{ storageAudit.scanned_files }} 个文件 · {{ displayDateTime(storageAudit.checked_at) }}</p><p v-else>尚未扫描；任务会在后台执行，刷新页面不会丢失。</p></div>
                  <button class="btn primary" type="button" :disabled="storageAuditBusy || storageRepairAllBusy" @click="startStorageAudit"><HardDrive :size="15" />{{ storageAuditBusy ? '扫描中…' : '扫描存储' }}</button>
                </header>
                <div v-if="storageAudit" class="storage-metrics">
                  <div><b>{{ storageAudit.disk.used_percent }}%</b><span>磁盘已用</span></div>
                  <div><b>{{ storageAudit.issue_counts?.relinkable_records ?? storageAudit.relinkable_records?.length ?? 0 }}</b><span>可修复旧路径</span></div>
                  <div><b>{{ storageAudit.issue_counts?.missing_records ?? storageAudit.missing_records.length }}</b><span>记录缺文件</span></div>
                  <div><b>{{ storageAudit.issue_counts?.partial_files ?? storageAudit.partial_files.length }}</b><span>陈旧临时文件</span></div>
                  <div><b>{{ storageAudit.issue_counts?.zero_byte_files ?? storageAudit.zero_byte_files.length }}</b><span>空文件</span></div>
                  <div><b>{{ storageAudit.issue_counts?.orphan_files ?? storageAudit.orphan_files.length }}</b><span>未关联媒体</span></div>
                </div>
                <div v-if="storageAudit" class="storage-actions">
                  <button class="btn ghost" type="button" :disabled="storageRepairBusy || storageRepairAllBusy || !storageRepairTargets.length" @click="previewStorageRepair">{{ storageRepairBusy ? '核验中…' : '预演当前批次' }}</button>
                  <button v-if="storageRepairPlan" class="btn ghost" type="button" :disabled="storageRepairBusy || storageRepairAllBusy || !eligibleRepairTargets.length" @click="applyStorageRepair">处理当前批次 {{ eligibleRepairTargets.length }} 项</button>
                  <button class="btn primary" type="button" :disabled="storageRepairBusy || storageRepairAllBusy || !storageIssueCount" @click="applyAllStorageRepairs">后台处理全部 {{ storageIssueCount }} 项</button>
                </div>
                <p v-if="lastStorageRepair" class="maintenance-note">上次处理：{{ displayDateTime(lastStorageRepair.applied_at) }} · 成功 {{ lastStorageRepair.applied }} 项 · 跳过 {{ lastStorageRepair.skipped || 0 }} 项</p>
                <details v-if="storageAudit && (storageRepairTargets.length || storageAudit.orphan_files?.length)" class="diagnostic-box"><summary>查看问题样本</summary><pre>{{ JSON.stringify({ relinkable_records: storageAudit.relinkable_records, missing_records: storageAudit.missing_records, zero_byte_files: storageAudit.zero_byte_files, partial_files: storageAudit.partial_files, orphan_files: storageAudit.orphan_files }, null, 2) }}</pre></details>
                <div v-if="storageRepairPlan" class="maintenance-note"><strong>预演结果：{{ storageRepairPlan.eligible }}/{{ storageRepairPlan.planned }} 项可处理。</strong> 文件只移动到可恢复隔离区，真正缺失的媒体任务会标记为失败以便重试。</div>
              </section>
            </section>

            <section v-else-if="activePage.id === 'logs'" class="maintenance-page">
              <section class="setting-section">
                <header>
                  <div><h4>活动日志</h4><p>保留可操作事件、失败证据和关联请求，高频周期任务不会逐次刷屏。</p></div>
                  <div class="row-buttons">
                    <button class="setting-switch" :class="{ on: live }" type="button" role="switch" :aria-checked="live" @click="live = !live"><span class="switch-track"><i /></span><span>实时刷新</span></button>
                    <button class="btn ghost compact" type="button" :disabled="!resourceState.logs.loaded" @click="copyLogs"><Clipboard :size="14" />复制</button>
                    <button class="btn ghost compact" type="button" :disabled="!resourceState.logs.loaded" @click="clearLogs"><Trash2 :size="14" />清空</button>
                  </div>
                </header>
                <div class="log-filters" aria-label="日志级别筛选"><button v-for="level in ['info', 'warning', 'error']" :key="level" type="button" :class="{ active: logLevels.includes(level) }" :aria-pressed="logLevels.includes(level)" @click="toggleLevel(level)">{{ level }}</button></div>
                <div class="log-console">
                  <article v-for="(item, index) in filteredLogs" :key="String(item.ts) + '-' + index" :data-level="item.level">
                    <time>{{ new Date(item.ts * 1000).toLocaleString() }}</time>
                    <b>[{{ item.source }}]<template v-if="item.event_code"> [{{ item.event_code }}]</template></b>
                    <span>{{ item.msg }}</span>
                    <small v-if="item.detail">{{ item.detail }}</small>
                    <small v-if="item.correlation_id || Object.keys(item.context || {}).length">{{ item.correlation_id ? '请求 ' + item.correlation_id : '' }}{{ Object.keys(item.context || {}).length ? ' · ' + JSON.stringify(item.context) : '' }}</small>
                  </article>
                  <div v-if="resourceState.logs.loaded && !filteredLogs.length" class="empty-state">暂无符合筛选条件的日志</div>
                </div>
              </section>
            </section>

            <section v-else-if="activePage.id === 'update'" class="maintenance-page">
              <section class="setting-section">
                <header><div><h4>版本与更新</h4><p>{{ updateInfo.message || '正在读取本地版本信息' }}</p></div></header>
                <dl class="fact-list">
                  <div><dt>当前版本</dt><dd>{{ updateInfo.current?.short || '—' }}</dd></div>
                  <div><dt>分支</dt><dd>{{ updateInfo.branch || '—' }}</dd></div>
                  <div><dt>更新状态</dt><dd>{{ updateInfo.has_update ? '有可用更新' : '当前已是最新或尚未检查' }}</dd></div>
                </dl>
                <div class="storage-actions"><button class="btn ghost" type="button" :disabled="updateBusy" @click="checkUpdate">检查更新</button><button class="btn ghost" type="button" @click="diagnoseUpdate">复制诊断</button><button v-if="updateInfo.has_update" class="btn primary" type="button" :disabled="updateBusy || !updateInfo.update_supported" @click="applyUpdate">安装更新</button></div>
              </section>
              <section class="setting-section">
                <header><div><h4>系统入口</h4><p>FastAPI · PostgreSQL · Redis · Celery · Vue 3</p></div></header>
                <div class="storage-actions"><a class="btn ghost" href="/docs" target="_blank" rel="noopener">API 文档</a><a class="btn ghost" href="/legacy">旧版界面</a></div>
                <pre v-if="diagnostic" class="diagnostic-box">{{ JSON.stringify(diagnostic, null, 2) }}</pre>
              </section>
            </section>
          </template>
        </div>
      </main>
    </div>
  </section>
</template>

<style scoped>
.config-workspace { min-height: calc(100dvh - 102px); }
.config-shell-header { min-height: 96px; }
.directory-trigger { display: none; margin-left: auto; }
.config-layout { min-height: 0; flex: 1; display: grid; grid-template-columns: 232px minmax(0, 1fr); }
.config-directory { min-width: 0; padding: 19px 12px; border-right: 1px solid var(--line); background: var(--surface-2); }
.directory-mobile-header { display: none; }
.config-directory nav { display: grid; gap: 13px; }
.directory-group { display: grid; gap: 3px; }
.directory-group-button { width: 100%; height: 33px; padding: 0 9px; display: flex; align-items: center; justify-content: space-between; border: 0; border-radius: 6px; background: transparent; color: var(--muted); font-size: 12px; font-weight: 720; cursor: pointer; }
.directory-group-button:hover { color: var(--text); background: var(--surface-3); }
.directory-group-button.directory-direct.active { color: var(--accent); background: var(--accent-soft); }
.directory-group-button svg { transition: transform .18s ease; }
.directory-group-button svg.rotated { transform: rotate(180deg); }
.directory-children { margin-left: 10px; padding: 2px 0 2px 10px; display: grid; gap: 2px; border-left: 1px solid var(--line-strong); }
.directory-children button { min-height: 35px; padding: 7px 10px; border: 0; border-radius: 6px; background: transparent; color: var(--muted); font-size: 12px; font-weight: 580; text-align: left; cursor: pointer; }
.directory-children button:hover { color: var(--text); background: var(--surface-3); }
.directory-children button.active { color: var(--accent); background: var(--accent-soft); font-weight: 700; }
.config-content { min-width: 0; background: var(--surface); }
.config-page-bar { position: sticky; z-index: 12; top: 0; min-height: 70px; padding: 12px 24px; display: flex; align-items: center; gap: 20px; border-bottom: 1px solid var(--line); background: color-mix(in srgb, var(--surface) 94%, transparent); backdrop-filter: blur(12px); }
.config-page-bar > div:first-child { display: grid; gap: 3px; }
.config-page-bar > div:first-child span { color: var(--muted); font-size: 11px; }
.config-page-bar > div:first-child strong { font-size: 15px; }
.page-actions { margin-left: auto; display: flex; align-items: center; justify-content: flex-end; gap: 8px; flex-wrap: wrap; }
.dirty-indicator { color: var(--amber); font-size: 11px; font-weight: 700; }
.config-page { width: min(100%, 1120px); padding: 30px 34px 52px; }
.config-page-intro { margin-bottom: 30px; }
.config-page-intro h3 { margin: 0 0 7px; font-size: 24px; letter-spacing: -.025em; }
.config-page-intro p { max-width: 720px; margin: 0; color: var(--muted); font-size: 13px; line-height: 1.65; }
.config-alert { margin: -12px 0 22px; padding: 10px 12px; border-left: 3px solid var(--red); background: color-mix(in srgb, var(--red) 8%, transparent); color: var(--red); font-size: 12px; line-height: 1.55; }
.setting-section { border-top: 1px solid var(--line-strong); }
.setting-section + .setting-section { margin-top: 34px; }
.setting-section > header { min-height: 78px; padding: 18px 0 15px; display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; }
.setting-section > header h4 { margin: 0 0 5px; font-size: 15px; }
.setting-section > header p { max-width: 670px; margin: 0; color: var(--muted); font-size: 12px; line-height: 1.55; }
.setting-list { border-bottom: 1px solid var(--line); }
.setting-row { min-height: 82px; padding: 15px 0; display: grid; grid-template-columns: minmax(220px, .9fr) minmax(310px, 1.1fr); align-items: center; gap: 34px; border-top: 1px solid var(--line); }
.setting-copy { min-width: 0; display: grid; gap: 5px; }
.setting-copy strong { display: flex; align-items: center; gap: 7px; font-size: 13px; }
.setting-copy strong i { padding: 2px 5px; border-radius: 4px; background: var(--surface-3); color: var(--faint); font-size: 9px; font-style: normal; font-weight: 700; }
.setting-copy small { color: var(--muted); font-size: 11px; line-height: 1.5; }
.setting-control { min-width: 0; display: flex; justify-content: flex-end; }
.setting-control > input, .setting-control > textarea, .setting-control > select { width: min(100%, 540px); }
.setting-control input, .setting-control textarea { min-width: 0; padding: 10px 11px; outline: 0; font-size: 13px; }
.setting-control input { height: 40px; }
.setting-control textarea { resize: vertical; line-height: 1.55; }
.setting-control input:focus, .setting-control textarea:focus, .setting-control select:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
.inline-control { align-items: center; gap: 8px; flex-wrap: wrap; }
.paired-control { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.choice-button { min-height: 36px; padding: 7px 12px; border: 1px solid var(--line); border-radius: 7px; background: transparent; color: var(--muted); font-size: 12px; font-weight: 680; cursor: pointer; }
.choice-button.active { border-color: color-mix(in srgb, var(--accent) 45%, var(--line)); background: var(--accent-soft); color: var(--accent); }
.notification-result { padding: 12px 0; display: flex; gap: 8px; flex-wrap: wrap; border-top: 1px solid var(--line); }
.notification-result span { padding: 6px 8px; border: 1px solid var(--line); border-radius: 6px; color: var(--muted); font-size: 11px; }
.notification-result strong { margin-right: 6px; color: var(--text); }
.account-page, .archive-page, .maintenance-page { display: grid; gap: 34px; }
.account-summary { min-height: 82px; padding: 15px 0; display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 13px; border-top: 1px solid var(--line-strong); border-bottom: 1px solid var(--line); }
.account-summary > div { display: grid; gap: 4px; }
.account-summary strong { font-size: 14px; }
.account-summary p { margin: 0; color: var(--muted); font-size: 11px; }
.capability-note { padding: 14px 0; display: flex; align-items: flex-start; gap: 12px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); color: var(--accent); }
.capability-note p { margin: 4px 0 0; color: var(--muted); font-size: 12px; line-height: 1.55; }
.fact-list { margin: 0; border-bottom: 1px solid var(--line); }
.fact-list > div { min-height: 58px; padding: 12px 0; display: grid; grid-template-columns: minmax(160px, .6fr) 1fr; align-items: center; gap: 25px; border-top: 1px solid var(--line); }
.fact-list dt { color: var(--muted); font-size: 12px; }
.fact-list dd { margin: 0; font-size: 13px; overflow-wrap: anywhere; }
.status-list { border-bottom: 1px solid var(--line); }
.status-row { min-height: 66px; padding: 12px 0; display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 13px; border-top: 1px solid var(--line); }
.status-row > span:nth-child(2) { display: grid; gap: 4px; }
.status-row strong { font-size: 13px; }
.status-row small { color: var(--muted); font-size: 11px; }
.status-row > b { color: var(--red); font-size: 11px; }
.status-row > b.good { color: var(--green); }
.row-buttons, .storage-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.platform-table { border-top: 1px solid var(--line); }
.platform-table-head, .platform-table-row { display: grid; grid-template-columns: .6fr 1fr 1.15fr 1.15fr; gap: 18px; }
.platform-table-head { min-height: 42px; align-items: center; color: var(--faint); border-bottom: 1px solid var(--line); font-size: 10px; font-weight: 700; letter-spacing: .04em; }
.platform-table-row { min-height: 86px; padding: 15px 0; align-items: start; border-bottom: 1px solid var(--line); }
.platform-table-row > span { display: grid; gap: 5px; }
.platform-table-row strong { font-size: 12px; }
.platform-table-row small { color: var(--muted); font-size: 10px; line-height: 1.5; }
.platform-table-row ul { grid-column: 2 / -1; margin: -2px 0 0; padding-left: 18px; color: var(--amber); font-size: 10px; }
.storage-metrics { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.storage-metrics > div { min-height: 85px; padding: 15px 13px; display: grid; align-content: center; gap: 6px; }
.storage-metrics > div + div { border-left: 1px solid var(--line); }
.storage-metrics b { font-size: 20px; letter-spacing: -.03em; }
.storage-metrics span { color: var(--muted); font-size: 10px; }
.storage-actions { padding: 15px 0; border-bottom: 1px solid var(--line); }
.maintenance-note { margin: 12px 0 0; color: var(--muted); font-size: 11px; line-height: 1.55; }
.diagnostic-box { margin-top: 14px; border: 1px solid var(--line); border-radius: 8px; background: #0b0e11; color: #c5cbd0; }
.diagnostic-box summary { padding: 11px 13px; cursor: pointer; font-size: 11px; font-weight: 700; }
.diagnostic-box pre, pre.diagnostic-box { max-height: 420px; margin: 0; padding: 13px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 10px; line-height: 1.55; }
.log-filters { padding: 10px 0; display: flex; gap: 5px; border-top: 1px solid var(--line); }
.log-filters button { min-height: 31px; padding: 5px 10px; border: 1px solid var(--line); border-radius: 6px; background: transparent; color: var(--muted); font-size: 10px; cursor: pointer; }
.log-filters button.active { background: var(--accent-soft); color: var(--accent); border-color: color-mix(in srgb, var(--accent) 35%, var(--line)); }
.log-console { max-height: 640px; padding: 10px; overflow: auto; border: 1px solid var(--line); border-radius: 8px; background: #0b0e11; color: #c5cbd0; }
.log-console article { padding: 8px 9px; display: grid; grid-template-columns: 156px 110px minmax(0, 1fr); gap: 8px; border-left: 2px solid var(--blue); font-size: 10px; }
.log-console article + article { margin-top: 4px; }
.log-console article[data-level="warning"] { border-color: var(--amber); }
.log-console article[data-level="error"] { border-color: var(--red); }
.log-console time { color: #778189; }
.log-console b { color: #93a0ff; }
.log-console small { grid-column: 3; color: #89939a; white-space: pre-wrap; overflow-wrap: anywhere; }
.directory-backdrop { display: none; }

@media (max-width: 1120px) {
  .config-layout { grid-template-columns: 208px minmax(0, 1fr); }
  .config-page { padding-inline: 25px; }
  .setting-row { grid-template-columns: minmax(190px, .8fr) minmax(280px, 1.2fr); gap: 24px; }
  .storage-metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .storage-metrics > div:nth-child(4) { border-left: 0; border-top: 1px solid var(--line); }
  .storage-metrics > div:nth-child(5), .storage-metrics > div:nth-child(6) { border-top: 1px solid var(--line); }
}
@media (max-width: 900px) {
  .directory-trigger { display: inline-flex; }
  .config-layout { display: block; }
  .config-directory { position: fixed; z-index: 410; inset: 0 auto 0 0; width: min(320px, calc(100vw - 48px)); padding: 0 13px 24px; overflow-y: auto; border-right: 1px solid var(--line-strong); background: var(--surface); box-shadow: var(--shadow); transform: translateX(-104%); transition: transform .2s ease; }
  .config-directory.open { transform: translateX(0); }
  .directory-mobile-header { position: sticky; z-index: 2; top: 0; min-height: 66px; margin-bottom: 10px; display: flex; align-items: center; justify-content: space-between; background: var(--surface); border-bottom: 1px solid var(--line); }
  .directory-backdrop { position: fixed; z-index: 400; inset: 0; display: block; border: 0; background: rgba(4, 7, 10, .56); backdrop-filter: blur(2px); }
  .config-page-bar { top: 0; }
  .platform-table-head { display: none; }
  .platform-table-row { grid-template-columns: 1fr 1fr; }
  .platform-table-row ul { grid-column: 1 / -1; }
}
@media (max-width: 680px) {
  .config-shell-header { min-height: 86px; align-items: center; flex-direction: row; }
  .config-shell-header > div { min-width: 0; }
  .config-shell-header .directory-trigger { width: auto; margin-left: auto; }
  .config-page-bar { padding: 11px 14px; align-items: flex-start; flex-direction: column; gap: 10px; }
  .page-actions { width: 100%; margin-left: 0; justify-content: flex-start; }
  .page-actions .dirty-indicator { width: 100%; }
  .page-actions .btn { flex: 1; }
  .config-page { padding: 24px 16px 40px; }
  .config-page-intro { margin-bottom: 23px; }
  .config-page-intro h3 { font-size: 21px; }
  .setting-section + .setting-section, .account-page, .archive-page, .maintenance-page { gap: 28px; }
  .setting-section > header { min-height: 0; padding: 16px 0 13px; flex-direction: column; }
  .setting-row { min-height: 0; padding: 14px 0 16px; grid-template-columns: 1fr; gap: 11px; }
  .setting-control { justify-content: flex-start; }
  .setting-control > input, .setting-control > textarea, .setting-control > select { width: 100%; }
  .paired-control { width: 100%; }
  .account-summary { grid-template-columns: auto 1fr; }
  .account-summary .status { grid-column: 2; }
  .fact-list > div { grid-template-columns: 110px minmax(0, 1fr); }
  .status-row { grid-template-columns: auto minmax(0, 1fr); }
  .status-row > b, .status-row .row-buttons { grid-column: 2; justify-self: start; }
  .platform-table-row { grid-template-columns: 1fr; gap: 11px; }
  .platform-table-row ul { grid-column: 1; }
  .storage-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .storage-metrics > div:nth-child(3), .storage-metrics > div:nth-child(5) { border-left: 0; }
  .storage-metrics > div:nth-child(3) { border-top: 1px solid var(--line); }
  .storage-metrics > div:nth-child(4) { border-left: 1px solid var(--line); }
  .log-console article { grid-template-columns: 1fr; }
  .log-console small { grid-column: 1; }
}
</style>
