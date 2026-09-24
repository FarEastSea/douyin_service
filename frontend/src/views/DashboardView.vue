<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { AlertCircle, ArrowRight, CircleCheck, Clock3, Download, HardDrive, RefreshCw, ShieldAlert, Users } from '@lucide/vue'
import { api } from '../api'
import { useAppStore } from '../stores/app'
import type { UnifiedTask, UnifiedTaskPage } from '../types'

interface SubscriptionReport {
  status: string
  summary?: string
  checked_authors: number
  failed_authors: number
  warning_authors: number
  new_works: number
  remaining_authors: number
  started_at?: string
}
interface SubscriptionReports { items: SubscriptionReport[]; cycle?: Record<string, unknown> }
interface StorageState { status: string; error?: string; updated_at?: string; result?: { issue_counts?: Record<string, number>; scanned_at?: string } }

const router = useRouter()
const store = useAppStore()
const tasks = ref<UnifiedTask[]>([])
const failedTasks = ref<UnifiedTask[]>([])
const counts = ref<Record<string, number> | null>(null)
const report = ref<SubscriptionReport | null>(null)
const storage = ref<StorageState | null>(null)
const risk = ref<any>(null)
const errors = ref<string[]>([])
const loading = ref(true)
const refreshedAt = ref<Date | null>(null)
let timer: number | undefined

const activeCount = computed(() => Number(counts.value?.downloading || 0))
const failedCount = computed(() => Number(counts.value?.failed || 0))
const queuedCount = computed(() => Number(counts.value?.pending || 0))
const storageIssues = computed(() => Object.values(storage.value?.result?.issue_counts || {}).reduce((sum, item) => sum + Number(item || 0), 0))
const attention = computed(() => {
  const items: Array<{ title: string; detail: string; to: string; tone: 'critical' | 'warning' }> = []
  if (risk.value?.active) items.push({
    title: '抖音请求已暂停',
    detail: risk.value.requires_account_update ? '账号请求上下文需要检查；新抖音请求暂不可用。' : '正在保护性冷却，等待后再检查。',
    to: '/settings/account-douyin', tone: 'critical',
  })
  if (failedCount.value) items.push({ title: `${failedCount.value.toLocaleString()} 个下载任务失败`, detail: '查看失败原因，按任务决定是否重试。', to: '/operations/tasks?status=failed', tone: 'critical' })
  if (report.value && ['failed', 'interrupted', 'partial_upstream', 'partial_authentication'].includes(report.value.status)) items.push({
    title: ['failed', 'interrupted'].includes(report.value.status) ? '最近一次订阅检查异常终止' : '订阅检查需要核对',
    detail: report.value.summary || '查看本轮失败证据和续检状态。', to: '/douyin/updates', tone: 'warning',
  })
  if (storage.value?.status === 'failed') items.push({ title: '存储巡检未完成', detail: storage.value.error || '可在运行维护中查看原因并重新扫描。', to: '/operations/maintenance/storage', tone: 'warning' })
  else if (storageIssues.value) items.push({ title: `上次存储巡检发现 ${storageIssues.value.toLocaleString()} 项问题`, detail: '先查看样本与预演，再决定是否处理。', to: '/operations/maintenance/storage', tone: 'warning' })
  return items
})

const statusNames: Record<string, string> = { pending: '等待中', downloading: '下载中', paused: '已暂停', completed: '已完成', skipped: '已跳过', failed: '失败', cancelled: '已取消' }
const platformNames: Record<string, string> = { douyin: '抖音', x: 'X', tiktok: 'TikTok', weibo: '微博', bilibili: 'B站', xhs: '小红书' }
function formatTime(value?: string) {
  if (!value) return '暂无记录'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
function newDownload() { window.dispatchEvent(new Event('app:new-download')) }
function onVisibilityChange() { if (!document.hidden) void load() }

async function load() {
  loading.value = true
  const results = await Promise.allSettled([
    api<UnifiedTaskPage>('/operations/tasks?page=1&page_size=6'),
    api<UnifiedTaskPage>('/operations/tasks?page=1&page_size=4&status=failed'),
    api<SubscriptionReports>('/authors/reports/subscriptions?limit=1'),
    api<StorageState>('/operations/storage-audit'),
    api<any>('/system/douyin-risk-state'),
  ])
  const messages = ['任务汇总', '失败任务', '订阅检查', '存储巡检', '抖音风控']
  errors.value = results.flatMap((item, index) => item.status === 'rejected' ? [messages[index]] : [])
  tasks.value = results[0].status === 'fulfilled' ? results[0].value.items || [] : []
  counts.value = results[0].status === 'fulfilled' ? results[0].value.status_summary || {} : null
  failedTasks.value = results[1].status === 'fulfilled' ? results[1].value.items || [] : []
  report.value = results[2].status === 'fulfilled' ? results[2].value.items?.[0] || null : null
  storage.value = results[3].status === 'fulfilled' ? results[3].value : null
  risk.value = results[4].status === 'fulfilled' ? results[4].value : null
  refreshedAt.value = new Date()
  loading.value = false
}

onMounted(() => {
  void load()
  timer = window.setInterval(() => { if (!document.hidden) void load() }, 30_000)
  document.addEventListener('visibilitychange', onVisibilityChange)
})
onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
  document.removeEventListener('visibilitychange', onVisibilityChange)
})
</script>

<template>
  <div class="dashboard-page">
    <header class="dashboard-heading">
      <div>
        <h1>工作台</h1>
        <p>从这里提交下载，处理异常，再回到日常巡检。</p>
      </div>
      <div class="dashboard-heading-actions">
        <span v-if="refreshedAt" class="freshness">更新于 {{ formatTime(refreshedAt.toISOString()) }}</span>
        <button class="btn ghost" :disabled="loading" @click="load"><RefreshCw :size="17" />刷新</button>
      </div>
    </header>

    <section class="dashboard-intake" aria-labelledby="intake-title">
      <div><h2 id="intake-title">开始一次下载</h2><p>粘贴作品或作者链接，先识别平台和可用能力，再确认提交。</p></div>
      <button class="btn primary" @click="newDownload"><Download :size="18" />新建下载</button>
    </section>

    <div v-if="errors.length" class="dashboard-fetch-error" role="status"><AlertCircle :size="18" /><span>{{ errors.join('、') }}暂不可用；已显示能够读取的状态，未把未知值当作零。</span><button @click="load">重试</button></div>

    <section class="queue-overview" aria-label="全部平台任务概览">
      <div><span>全部平台 · 下载中</span><strong>{{ counts === null ? '—' : activeCount.toLocaleString() }}</strong><RouterLink to="/operations/tasks?status=downloading">查看任务 <ArrowRight :size="15" /></RouterLink></div>
      <div><span>全部平台 · 等待中</span><strong>{{ counts === null ? '—' : queuedCount.toLocaleString() }}</strong><RouterLink to="/operations/tasks?status=pending">查看队列 <ArrowRight :size="15" /></RouterLink></div>
      <div :data-alert="failedCount > 0"><span>全部平台 · 失败</span><strong>{{ counts === null ? '—' : failedCount.toLocaleString() }}</strong><RouterLink to="/operations/tasks?status=failed">处理失败 <ArrowRight :size="15" /></RouterLink></div>
    </section>

    <div class="dashboard-columns">
      <section class="dashboard-section attention-section" aria-labelledby="attention-title">
        <header><div><h2 id="attention-title">需要你处理</h2><p>只列出失败与异常；普通排队任务不在这里。</p></div></header>
        <div v-if="loading && !refreshedAt" class="dashboard-skeleton" aria-label="正在读取异常状态"><i /><i /><i /></div>
        <div v-else-if="attention.length" class="attention-list">
          <RouterLink v-for="item in attention" :key="item.title" :to="item.to" :data-tone="item.tone">
            <ShieldAlert v-if="item.tone === 'critical'" :size="20" /><AlertCircle v-else :size="20" />
            <span><strong>{{ item.title }}</strong><small>{{ item.detail }}</small></span><ArrowRight :size="17" />
          </RouterLink>
          <div v-if="failedTasks.length" class="attention-examples"><strong>最近失败</strong><RouterLink v-for="task in failedTasks.slice(0, 3)" :key="task.key" :to="`/operations/tasks?platform=${task.platform}&status=failed`"><span>{{ task.source_label || `任务 ${task.id}` }}</span><small>{{ task.error_message || '查看失败原因' }}</small><ArrowRight :size="15" /></RouterLink></div>
        </div>
        <div v-else-if="errors.length" class="dashboard-empty"><AlertCircle :size="20" /><strong>部分状态尚未确认</strong><span>刷新后再确认是否存在需要处理的问题。</span></div>
        <div v-else class="dashboard-empty"><CircleCheck :size="21" /><strong>当前没有待处理异常</strong><span>下载任务与订阅检查会继续在后台运行。</span></div>
      </section>

      <section class="dashboard-section cycle-section" aria-labelledby="cycle-title">
        <header><div><h2 id="cycle-title">最近订阅检查</h2><p>抖音作者 · 最近一次检查结果</p></div><RouterLink to="/douyin/updates">查看记录 <ArrowRight :size="15" /></RouterLink></header>
        <div v-if="report" class="cycle-summary">
          <span class="status" :data-tone="report.status">{{ report.status === 'running' ? '执行中' : report.status === 'completed' ? '已完成' : report.status === 'interrupted' ? '异常终止' : report.status === 'failed' ? '失败' : '部分完成' }}</span>
          <strong>{{ report.summary || '订阅检查' }}</strong>
          <div><span><Users :size="16" />检查 {{ report.checked_authors }} 位</span><span><AlertCircle :size="16" />异常 {{ report.failed_authors + report.warning_authors }} 位</span></div>
          <small><Clock3 :size="15" />{{ formatTime(report.started_at) }} · 新作品 {{ report.new_works }} 个 · 待续检 {{ report.remaining_authors }} 位</small>
        </div>
        <div v-else-if="errors.includes('订阅检查')" class="dashboard-empty"><AlertCircle :size="20" /><strong>订阅检查状态未确认</strong><span>稍后重试，或前往自动更新查看。</span></div>
        <div v-else class="dashboard-empty"><Users :size="20" /><strong>暂无订阅检查记录</strong><span>订阅作者后，检查结果会显示在这里。</span></div>
      </section>
    </div>

    <section class="dashboard-section recent-section" aria-labelledby="recent-title">
      <header><div><h2 id="recent-title">最近任务</h2><p>全部平台的最新任务与状态</p></div><RouterLink to="/operations/tasks">查看全部 <ArrowRight :size="15" /></RouterLink></header>
      <div v-if="tasks.length" class="dashboard-task-list">
        <RouterLink v-for="task in tasks" :key="task.key" :to="`/operations/tasks?platform=${task.platform}`">
          <span class="task-platform">{{ platformNames[task.platform] || task.platform }}</span>
          <span class="task-title"><strong>{{ task.source_label || `任务 ${task.id}` }}</strong><small>{{ task.author_name || task.source_type || '媒体任务' }}</small></span>
          <span class="status" :data-tone="task.status">{{ statusNames[task.status] || task.status }}</span>
          <time>{{ formatTime(task.created_at) }}</time><ArrowRight :size="16" />
        </RouterLink>
      </div>
      <div v-else-if="errors.includes('任务汇总')" class="dashboard-empty"><AlertCircle :size="20" /><strong>最近任务状态未确认</strong><span>任务接口恢复后会自动刷新。</span></div>
      <div v-else class="dashboard-empty"><Download :size="20" /><strong>还没有下载任务</strong><span>使用上方“新建下载”开始。</span></div>
    </section>

    <footer class="dashboard-footer"><HardDrive :size="16" /><span>存储巡检{{ errors.includes('存储巡检') ? '状态未确认' : storage?.status === 'completed' ? `更新于 ${formatTime(storage.updated_at)}` : storage?.status === 'running' ? '正在运行' : storage?.status === 'failed' ? '需要检查' : '尚无完成记录' }}</span><button @click="router.push('/operations/maintenance/storage')">打开存储维护 <ArrowRight :size="15" /></button></footer>
  </div>
</template>
