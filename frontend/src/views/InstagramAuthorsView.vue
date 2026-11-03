<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Plus, Search } from "@lucide/vue";
import { api, jsonBody } from "../api";
import { dateTime } from "../workspace";
import { openMedia } from "../media";
import PageHeader from "../components/PageHeader.vue";
import SubscriptionNavigation from "../components/SubscriptionNavigation.vue";
import FilterToolbar from "../components/FilterToolbar.vue";
import InspectorPanel from "../components/InspectorPanel.vue";
import AuthorAvatar from "../components/AuthorAvatar.vue";
import StateView from "../components/StateView.vue";
import StatusIndicator from "../components/StatusIndicator.vue";
import Pager from "../components/Pager.vue";

type Stream = { id: number; content_kind: string; scan_mode: string; enabled: boolean; history_complete: boolean; next_check_at: string; last_success_at?: string; deferred_reason?: string; indexed_works?: number; estimated_remaining?: number | null };
type Author = { id: number; username: string; nickname: string; external_user_id: string; avatar_url?: string; profile_url: string; is_subscribed: boolean; total_works: number; streams: Stream[] };
const root = "/platform-downloads/instagram";
const route = useRoute(), router = useRouter();
const authors = ref<Author[]>([]), reports = ref<any[]>([]), works = ref<any[]>([]), collections = ref<any[]>([]);
const loading = ref(false), busy = ref(false), error = ref(""), detailError = ref(""), notice = ref(""), updated = ref("");
const page = ref(Number(route.query.page) || 1), pages = ref(1), total = ref(0), search = ref(String(route.query.q || "")), subscribed = ref(String(route.query.subscribed || ""));
const selectedIds = ref<number[]>([]), detail = ref<Author>(), kinds = ref<string[]>([]), session = ref<any>();
const workPage = ref(1), workPages = ref(1), workTotal = ref(0), collectionPage = ref(1), collectionPages = ref(1), collectionTotal = ref(0);
const selected = computed(() => Number(route.query.author) || 0), reportsMode = computed(() => route.query.view === "reports");
const contentLabels: Record<string, string> = { posts: "Posts", reels: "Reels", stories: "Stories", highlights: "Highlights" };
const utc = (value?: string) => dateTime(value ? /Z$|[+-]\d\d:\d\d$/.test(value) ? value : value + "Z" : undefined);
let timer: number | undefined, searchTimer: number | undefined, loadSequence = 0, detailSequence = 0;
function syncQuery() {
  void router.replace({ query: { ...route.query, page: page.value > 1 ? String(page.value) : undefined, q: search.value || undefined, subscribed: subscribed.value || undefined } });
}
async function load() {
  const sequence = ++loadSequence;
  loading.value = true; error.value = "";
  try {
    const query = new URLSearchParams({ page: String(page.value), page_size: "20" });
    if (search.value.trim() && !reportsMode.value) query.set("q", search.value.trim());
    if (subscribed.value && !reportsMode.value) query.set("subscribed", subscribed.value);
    const [data, account] = await Promise.all([api(`${root}/${reportsMode.value ? 'reports' : 'authors'}?${query}`), api(`${root}/session`)]);
    if (sequence !== loadSequence) return;
    if (reportsMode.value) reports.value = data.items; else {
      authors.value = data.items;
      const refreshed = authors.value.find(a => a.id === detail.value?.id);
      if (refreshed && detail.value) detail.value = { ...detail.value, ...refreshed };
    }
    total.value = data.total; pages.value = data.pages; session.value = account;
    updated.value = new Date().toISOString();
    if (page.value > pages.value) { page.value = pages.value; syncQuery(); void load(); }
  } catch (e: any) { if (sequence === loadSequence) error.value = e.message; }
  finally { if (sequence === loadSequence) loading.value = false; }
}
function filters() { page.value = 1; selectedIds.value = []; syncQuery(); void load(); }
function searchInput() { window.clearTimeout(searchTimer); searchTimer = window.setTimeout(filters, 300); }
function changePage(value: number) { page.value = value; selectedIds.value = []; syncQuery(); void load(); }
function openAdd() { window.dispatchEvent(new CustomEvent("app:new-download", { detail: { intent: "subscribe", platform: "instagram" } })); }
function openAuthor(id: number) { void router.replace({ query: { ...route.query, author: String(id), view: undefined } }); }
function closeAuthor() { void router.replace({ query: { ...route.query, author: undefined } }); }
function switchView() { page.value = 1; void router.replace({ query: { view: reportsMode.value ? undefined : "reports" } }); }
async function loadDetail() {
  const id = selected.value, sequence = ++detailSequence;
  detail.value = undefined; detailError.value = "";
  if (!id) return;
  workPage.value = collectionPage.value = 1;
  try {
    const author = await api<Author>(`${root}/authors/${id}`);
    if (sequence !== detailSequence) return;
    detail.value = author;
    kinds.value = [...new Set(author.streams.filter(s => s.enabled).map(s => s.content_kind))];
    await Promise.all([loadWorks(), loadCollections()]);
  } catch (e: any) { if (sequence === detailSequence) detailError.value = e.message; }
}
async function loadWorks() {
  const id = selected.value;
  const result = await api(`${root}/authors/${id}/works?page=${workPage.value}`);
  if (id !== selected.value) return;
  works.value = result.items; workPages.value = result.pages; workTotal.value = result.total;
}
async function loadCollections() {
  const id = selected.value;
  const result = await api(`${root}/authors/${id}/collections?page=${collectionPage.value}`);
  if (id !== selected.value) return;
  collections.value = result.items; collectionPages.value = result.pages; collectionTotal.value = result.total;
}
async function mutate(path: string, body?: object) {
  busy.value = true; detailError.value = error.value = ""; notice.value = "";
  try {
    const result = await api(root + path, { method: "POST", ...(body ? jsonBody(body) : {}) });
    notice.value = result.message || "已保存";
    selectedIds.value = [];
    await load();
    if (selected.value) await loadDetail();
  } catch (e: any) { if (selected.value) detailError.value = e.message; else error.value = e.message; }
  finally { busy.value = false; }
}
async function preview(taskId: number) {
  try {
    const result = await api(`${root}/tasks/${taskId}/media?page=1&page_size=100`);
    const items = result.items || result;
    if (!items.length) { detailError.value = "该作品尚无可预览的本地媒体，请查看下载任务。"; return; }
    openMedia(items.map((item: any) => ({ type: item.media_type === "video" ? "video" : "image", url: item.preview_url, title: item.title || item.filename, downloadUrl: item.download_url })));
  } catch (e: any) { detailError.value = e.message; }
}
function delayLabel(stream: Stream) {
  if (stream.scan_mode === "history" && stream.history_complete) return "历史已补齐";
  if (!stream.enabled) return "已暂停";
  const minutes = Math.floor((Date.now() - Date.parse(stream.next_check_at + "Z")) / 60000);
  return minutes > 1 ? `到期延迟 ${minutes} 分钟` : "下次 " + utc(stream.next_check_at);
}
watch(selected, () => void loadDetail(), { immediate: true });
watch(reportsMode, () => { page.value = 1; selectedIds.value = []; void load(); });
function authorsChanged() { void load(); }
onMounted(() => { void load(); window.addEventListener("app:authors-changed", authorsChanged); timer = window.setInterval(() => { if (!document.hidden && !busy.value) void load(); }, 15000); });
onBeforeUnmount(() => { ++loadSequence; ++detailSequence; window.clearInterval(timer); window.clearTimeout(searchTimer); window.removeEventListener("app:authors-changed", authorsChanged); });
</script>
<template>
  <div class="workspace-page authors-page">
    <PageHeader title="Instagram 订阅" description="分批收集作品，查看实际检查进度与历史补采。" :busy="loading" :updated-at="updated" refreshable @refresh="load">
      <button class="btn ghost" @click="switchView">{{ reportsMode ? '作者列表' : '运行记录' }}</button>
      <button class="btn primary" @click="openAdd"><Plus :size="15" />订阅作者</button>
    </PageHeader>
    <SubscriptionNavigation />
    <div v-if="session && (!session.enabled || session.status !== 'ready' || session.last_error)" class="load-error-banner" role="status">
      <span>{{ !session.enabled ? 'Instagram 采集尚未启用。' : session.last_error || '请先检测 Instagram 会话。' }}</span>
      <RouterLink to="/settings/account-instagram">前往账号设置</RouterLink>
    </div>
    <p v-if="notice" role="status">{{ notice }}</p>
    <div v-if="error" class="load-error-banner" role="alert">{{ error }}</div>
    <FilterToolbar v-if="!reportsMode" :active="subscribed ? '订阅状态' : ''" @clear="subscribed=''; search=''; filters()">
      <template #search><label class="search"><Search :size="16" /><input v-model="search" placeholder="搜索用户名、名称或 ID" aria-label="搜索 Instagram 作者" @input="searchInput" /></label></template>
      <select v-model="subscribed" aria-label="订阅状态" @change="filters"><option value="">全部作者</option><option value="true">已订阅</option><option value="false">已暂停</option></select>
      <button v-if="selectedIds.length" class="btn ghost" :disabled="busy" @click="mutate('/authors/batch', {ids:selectedIds,is_subscribed:false})">暂停所选 {{ selectedIds.length }} 位作者</button>
    </FilterToolbar>
    <StateView v-if="reportsMode ? !reports.length : !authors.length" :loading="loading" :error="error" :title="reportsMode ? '尚无运行记录' : '尚无匹配的 Instagram 作者'" description="添加订阅后，身份解析、检查和历史补采会按预算分批执行。" @retry="load" />
    <div v-else class="table-shell">
      <table v-if="!reportsMode" class="data-table author-table">
        <thead><tr><th><input type="checkbox" aria-label="选择本页全部作者" :checked="authors.length > 0 && authors.every(a => selectedIds.includes(a.id))" @change="selectedIds = ($event.target as HTMLInputElement).checked ? authors.map(a=>a.id) : []" /></th><th>作者</th><th>作品</th><th>订阅与进度</th><th class="actions-col">操作</th></tr></thead>
        <tbody><tr v-for="author in authors" :key="author.id" :class="{selected:selected===author.id}">
          <td><input v-model="selectedIds" type="checkbox" :value="author.id" :aria-label="'选择 '+author.username" /></td>
          <td data-label="作者"><div class="author-cell"><AuthorAvatar :id="author.id" platform="instagram" :url="author.avatar_url" /><div><button class="record-link" @click="openAuthor(author.id)">{{ author.nickname || author.username }}</button><span>@{{ author.username }}</span></div></div></td>
          <td data-label="作品">{{ author.total_works }}</td>
          <td data-label="订阅与进度"><StatusIndicator :status="author.is_subscribed ? 'success' : 'paused'" :label="author.is_subscribed ? '已订阅' : '已暂停'" /><p class="field-help">{{ author.streams.filter(s=>s.enabled && s.scan_mode==='history' && !s.history_complete).length ? '历史补采中' : '查看内容检查进度' }}</p></td>
          <td><button class="text-button" @click="openAuthor(author.id)">查看详情</button></td>
        </tr></tbody>
      </table>
      <table v-else class="data-table"><thead><tr><th>时间</th><th>内容</th><th>新增 / 保存文件</th><th>结果</th><th>任务</th></tr></thead><tbody><tr v-for="run in reports" :key="run.id">
        <td>{{ utc(run.started_at) }}</td><td>{{ contentLabels[run.content_kind] || run.content_kind }} · {{ run.scan_mode==='history' ? '历史补采' : '新增检查' }}</td><td>{{ run.discovered }} / {{ run.saved }}</td><td><StatusIndicator :status="run.status" /><p class="field-help">{{ run.message }}</p></td><td><RouterLink v-if="run.task_id" :to="{path:'/operations/tasks',query:{task_key:`instagram:${run.task_id}`}}">查看任务</RouterLink></td>
      </tr></tbody></table>
    </div>
    <Pager :page="page" :pages="pages" :total="total" @change="changePage" />
    <InspectorPanel :open="Boolean(selected)" :title="detail?.nickname || 'Instagram 作者'" :subtitle="detail ? '@'+detail.username : ''" @close="closeAuthor">
      <StateView v-if="!detail" :loading="!detailError" :error="detailError" @retry="loadDetail" />
      <div v-else class="settings-sections">
        <p v-if="detailError" role="alert" class="load-error-banner">{{ detailError }}</p>
        <div class="detail-links"><a :href="detail.profile_url" target="_blank" rel="noreferrer">打开作者主页</a><RouterLink :to="{path:'/library',query:{platform:'instagram',author:detail.nickname || detail.username}}">查看本地媒体</RouterLink></div>
        <section class="setting-section"><header><div><h2>采集内容</h2><p>Stories 仅能收取当前仍可访问的内容；每种内容按自己的周期检查。</p></div></header>
          <div class="detail-links"><label v-for="(label, key) in contentLabels" :key="key"><input v-model="kinds" type="checkbox" :value="key" /> {{ label }}</label></div>
          <div class="detail-links"><button class="btn primary" :disabled="busy || !kinds.length" @click="mutate(`/authors/${detail.id}/subscription`, {is_subscribed:true,kinds})">保存并订阅</button><button class="btn ghost" :disabled="busy || !detail.is_subscribed" @click="mutate(`/authors/${detail.id}/subscription`, {is_subscribed:false})">暂停订阅</button><button class="btn ghost" :disabled="busy" @click="mutate(`/authors/${detail.id}/scan`)">立即检查</button></div>
        </section>
        <section class="setting-section"><header><div><h2>检查与历史进度</h2><p>到期时间是计划值；请求预算或冷却可能使检查延期。</p></div></header>
          <dl class="fact-list"><div v-for="stream in detail.streams" :key="stream.id"><dt>{{ contentLabels[stream.content_kind] }} · {{ stream.scan_mode==='history' ? '历史' : '新增' }}</dt><dd>{{ delayLabel(stream) }}<p v-if="stream.scan_mode==='history' && stream.enabled" class="field-help">已索引 {{ stream.indexed_works ?? 0 }} 条 · {{ stream.history_complete ? '扫描已结束' : stream.estimated_remaining == null ? '剩余数量待分页发现' : `预计待发现 ${stream.estimated_remaining} 条` }}</p><p v-if="stream.deferred_reason" class="field-help">{{ stream.deferred_reason }}</p><small v-if="stream.last_success_at">最近成功 {{ utc(stream.last_success_at) }}</small></dd></div></dl>
          <div class="detail-links"><button class="btn ghost" :disabled="busy" @click="mutate(`/authors/${detail.id}/history`,{enabled:true})">继续 / 重新补齐历史</button><button class="btn ghost" :disabled="busy" @click="mutate(`/authors/${detail.id}/history`,{enabled:false})">暂停历史补采</button></div>
        </section>
        <section class="setting-section"><header><h2>作品</h2></header>
          <StateView v-if="!works.length" title="尚未发现作品" />
          <div v-for="work in works" :key="work.id" class="setting-row"><span class="setting-copy"><strong>{{ work.title || work.content_kind }}</strong><small>{{ utc(work.published_at) }} · {{ work.work_type }} · {{ work.expected_media_count }} 个媒体</small></span><span class="setting-control"><button v-if="work.download_task_id" class="text-button" @click="preview(work.download_task_id)">预览</button><RouterLink v-if="work.download_task_id" :to="{path:'/operations/tasks',query:{task_key:`instagram:${work.download_task_id}`}}">任务</RouterLink></span></div>
          <Pager v-if="workTotal" :page="workPage" :pages="workPages" :total="workTotal" @change="workPage=$event; loadWorks().catch(e=>detailError=e.message)" />
        </section>
        <section class="setting-section"><header><h2>Highlights</h2></header><StateView v-if="!collections.length" title="尚未采集精选" />
          <div v-for="collection in collections" :key="collection.id" class="setting-row"><span class="setting-copy"><strong>{{ collection.title }}</strong><small>{{ collection.work_ids.length }} 条已发现的 Story，共用作品与媒体身份</small></span><a :href="collection.source_url" target="_blank" rel="noreferrer">查看原精选</a></div>
          <Pager v-if="collectionTotal" :page="collectionPage" :pages="collectionPages" :total="collectionTotal" @change="collectionPage=$event; loadCollections().catch(e=>detailError=e.message)" />
        </section>
      </div>
    </InspectorPanel>
  </div>
</template>
