<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { AlertTriangle, ExternalLink, Plus, Search } from "@lucide/vue";
import { api, jsonBody } from "../api";
import { dateTime, statusPresentation } from "../workspace";
import { openMedia } from "../media";
import { useAppStore } from "../stores/app";
import PageHeader from "../components/PageHeader.vue";
import SubscriptionNavigation from "../components/SubscriptionNavigation.vue";
import FilterToolbar from "../components/FilterToolbar.vue";
import InspectorPanel from "../components/InspectorPanel.vue";
import AuthorAvatar from "../components/AuthorAvatar.vue";
import StateView from "../components/StateView.vue";
import StatusIndicator from "../components/StatusIndicator.vue";
import MoreActions from "../components/MoreActions.vue";
import Pager from "../components/Pager.vue";

type Kind = "posts" | "reels" | "stories" | "highlights";
type Stream = { id: number; content_kind: Kind; scan_mode: string; enabled: boolean; history_complete: boolean; next_check_at: string; last_success_at?: string; deferred_reason?: string; blocked_code?: string; indexed_works?: number; estimated_total?: number | null; estimated_remaining?: number | null };
type Author = { id: number; username: string; nickname: string; external_user_id: string; avatar_url?: string; profile_url: string; is_subscribed: boolean; total_works: number; last_success_at?: string; streams: Stream[] };
type Work = { id: number; title?: string; content_kind: string; work_type?: string; published_at?: string; download_task_id?: number | null; expected_media_count?: number };
type Collection = { id: number; title: string; source_url: string; work_ids: number[] };
type Session = { enabled: boolean; configured: boolean; status: string; last_error?: string | null; cooling?: boolean; cooldown_until?: string | null };
type Tab = "overview" | "works" | "highlights";

const root = "/platform-downloads/instagram";
const kindOrder: Kind[] = ["posts", "reels", "stories", "highlights"];
const kindLabels: Record<string, string> = { posts: "Posts", reels: "Reels", stories: "Stories", highlights: "Highlights", post: "Post", reel: "Reel", story: "Story" };
const workKinds: Record<string, Kind> = { post: "posts", reel: "reels", story: "stories" };
const kindHelp: Record<Kind, string> = { posts: "图文与轮播", reels: "短视频", stories: "仅当前可见的 24 小时内容", highlights: "主页精选合集" };
const store = useAppStore(), route = useRoute(), router = useRouter();
const authors = ref<Author[]>([]), works = ref<Work[]>([]), collections = ref<Collection[]>([]);
const loading = ref(false), busy = ref(false), busyIds = ref<number[]>([]), error = ref(""), detailError = ref(""), updated = ref("");
const page = ref(Number(route.query.page) || 1), pages = ref(1), total = ref(0), search = ref(String(route.query.q || "")), subscribed = ref(String(route.query.subscribed || ""));
const selectedIds = ref<number[]>([]), detail = ref<Author>(), kinds = ref<Kind[]>([]), session = ref<Session>();
const tab = ref<Tab>("overview");
const workPage = ref(1), workPages = ref(1), workTotal = ref(0), collectionPage = ref(1), collectionPages = ref(1), collectionTotal = ref(0);
const selected = computed(() => Number(route.query.author) || 0);
const withZone = (value: string) => /Z$|[+-]\d\d:\d\d$/.test(value) ? value : value + "Z";
const utc = (value?: string | null) => dateTime(value ? withZone(value) : undefined);
const epoch = (value?: string | null) => value ? Date.parse(withZone(value)) : NaN;
let timer: number | undefined, searchTimer: number | undefined, loadSequence = 0, detailSequence = 0;

const sessionIssue = computed(() => {
  const value = session.value;
  if (!value) return null;
  if (!value.enabled) return { title: "Instagram 采集尚未启用", text: "订阅会保留，但不会检查或下载。导入并检测会话后，在账号设置中开启采集。", action: "前往开启" };
  if (!value.configured) return { title: "尚未导入登录会话", text: "Instagram 需要已登录的浏览器会话才能读取作者内容。", action: "导入会话" };
  if (value.status !== "ready") return { title: statusPresentation(value.status).label, text: value.last_error || "会话尚未通过检测，检查已暂停。", action: "检测会话" };
  if (value.cooling) return { title: "保护性冷却中", text: `为避免触发平台验证，请求暂停至 ${utc(value.cooldown_until)}，到期后自动恢复。`, action: "" };
  if (value.last_error) return { title: "最近一次请求异常", text: value.last_error, action: "查看会话" };
  return null;
});
const kindsDirty = computed(() => {
  if (!detail.value) return false;
  if (!detail.value.is_subscribed) return true;
  return enabledKinds(detail.value).join() !== [...kinds.value].sort(byKind).join();
});

function byKind(a: string, b: string) { return kindOrder.indexOf(a as Kind) - kindOrder.indexOf(b as Kind); }
function name(author: Author) { return author.nickname || author.username; }
function enabledKinds(author: Author) {
  return [...new Set(author.streams.filter(s => s.enabled && s.scan_mode !== "history").map(s => s.content_kind))].sort(byKind);
}
function stream(author: Author, kind: Kind, mode: "recent" | "history") {
  return author.streams.find(s => s.content_kind === kind && s.scan_mode === mode);
}
function blockedLabel(code?: string) {
  const label = statusPresentation(code).label;
  return label === "状态未确认" ? "检查受阻" : label;
}
function historyPercent(item?: Stream) {
  if (!item) return null;
  if (item.history_complete) return 100;
  if (!item.estimated_total) return null;
  return Math.min(99, Math.round(((item.indexed_works ?? 0) / item.estimated_total) * 100));
}
function nextCheck(author: Author) {
  return author.streams
    .filter(s => s.enabled && s.scan_mode !== "history" && s.next_check_at)
    .map(s => s.next_check_at)
    .sort((a, b) => epoch(a) - epoch(b))[0];
}
function relative(value?: string) {
  const at = epoch(value);
  if (Number.isNaN(at)) return "未安排";
  const minutes = Math.round((at - Date.now()) / 60000);
  if (minutes < -1) return `已到期 ${-minutes} 分钟`;
  if (minutes <= 1) return "即将开始";
  if (minutes < 60) return `${minutes} 分钟后`;
  if (minutes < 1440) return `${Math.round(minutes / 60)} 小时后`;
  return utc(value);
}
function authorState(author: Author): { status: string; label: string; hint: string } {
  if (!author.is_subscribed) return { status: "paused", label: "已暂停", hint: author.total_works ? "保留已收录作品" : "" };
  const blocked = author.streams.find(s => s.enabled && s.blocked_code);
  if (blocked) return { status: "warning", label: "检查受阻", hint: `${kindLabels[blocked.content_kind]} · ${blockedLabel(blocked.blocked_code)}` };
  const history = author.streams.filter(s => s.enabled && s.scan_mode === "history" && !s.history_complete);
  if (history.length) {
    const current = history.find(s => s.content_kind === "posts") || history[0];
    const percent = historyPercent(current);
    return { status: "running", label: "补齐历史中", hint: percent == null ? `已收录 ${current.indexed_works ?? 0} 条` : `${kindLabels[current.content_kind]} ${percent}% · ${current.indexed_works ?? 0}/${current.estimated_total}` };
  }
  const next = nextCheck(author);
  return { status: "success", label: "已是最新", hint: next ? `下次检查 ${relative(next)}` : "" };
}
function recentLabel(item?: Stream) {
  if (!item || !item.enabled) return "未采集";
  if (item.blocked_code) return blockedLabel(item.blocked_code);
  return relative(item.next_check_at);
}
function historyLabel(item?: Stream) {
  if (item?.history_complete) return "已补齐";
  if (!item || !item.enabled) return "未补齐";
  if (item.blocked_code) return blockedLabel(item.blocked_code);
  return item.estimated_remaining != null ? `剩余约 ${item.estimated_remaining} 条` : `已收录 ${item.indexed_works ?? 0} 条`;
}

function syncQuery() {
  void router.replace({ query: { ...route.query, page: page.value > 1 ? String(page.value) : undefined, q: search.value || undefined, subscribed: subscribed.value || undefined } });
}
async function load() {
  const sequence = ++loadSequence;
  loading.value = true; error.value = "";
  try {
    const query = new URLSearchParams({ page: String(page.value), page_size: "20" });
    if (search.value.trim()) query.set("q", search.value.trim());
    if (subscribed.value) query.set("subscribed", subscribed.value);
    const [data, account] = await Promise.all([api(`${root}/authors?${query}`), api<Session>(`${root}/session`)]);
    if (sequence !== loadSequence) return;
    authors.value = data.items;
    const refreshed = authors.value.find(a => a.id === detail.value?.id);
    if (refreshed && detail.value) detail.value = { ...detail.value, ...refreshed };
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
function openAuthor(id: number, view: Tab = "overview") { tab.value = view; void router.replace({ query: { ...route.query, author: String(id) } }); }
function closeAuthor() { void router.replace({ query: { ...route.query, author: undefined } }); }
async function loadDetail() {
  const id = selected.value, sequence = ++detailSequence;
  detail.value = undefined; detailError.value = "";
  if (!id) return;
  workPage.value = collectionPage.value = 1;
  try {
    const author = await api<Author>(`${root}/authors/${id}`);
    if (sequence !== detailSequence) return;
    detail.value = author;
    const current = enabledKinds(author);
    kinds.value = current.length ? current : ["posts", "reels"];
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
async function mutate(path: string, body?: object, fallback = "已保存") {
  busy.value = true; detailError.value = error.value = "";
  try {
    const result = await api(root + path, { method: "POST", ...(body ? jsonBody(body) : {}) });
    store.notify(result.message || fallback);
    selectedIds.value = [];
    await load();
    if (selected.value) await loadDetail();
  } catch (e: any) { if (selected.value) detailError.value = e.message; else error.value = e.message; }
  finally { busy.value = false; }
}
async function toggleSubscription(author: Author) {
  if (busyIds.value.includes(author.id)) return;
  busyIds.value = [...busyIds.value, author.id];
  try {
    const next = await api<Author>(`${root}/authors/${author.id}/subscription`, { method: "POST", ...jsonBody({ is_subscribed: !author.is_subscribed }) });
    authors.value = authors.value.map(a => a.id === author.id ? { ...a, ...next } : a);
    if (detail.value?.id === author.id) {
      const unsaved = detail.value.is_subscribed && kindsDirty.value;
      detail.value = { ...detail.value, ...next };
      const current = enabledKinds(detail.value);
      // 详情面板中尚未保存的选择不被列表开关覆盖。
      if (current.length && !unsaved) kinds.value = current;
    }
    store.notify(next.is_subscribed ? `已恢复订阅 ${name(author)}` : `已暂停订阅 ${name(author)}`);
  } catch (e: any) { store.notify(e.message || "订阅状态更新失败", "error"); }
  finally { busyIds.value = busyIds.value.filter(id => id !== author.id); }
}
function scan(author: Author) { void mutate(`/authors/${author.id}/scan`, undefined, "检查已排入调度"); }
async function preview(taskId: number) {
  try {
    const result = await api(`${root}/tasks/${taskId}/media?page=1&page_size=100`);
    const items = result.items || result;
    if (!items.length) { detailError.value = "该作品尚无可预览的本地媒体，请查看下载任务。"; return; }
    openMedia(items.map((item: any) => ({ type: item.media_type === "video" ? "video" : "image", url: item.preview_url, title: item.title || item.filename, downloadUrl: item.download_url })));
  } catch (e: any) { detailError.value = e.message; }
}
function toggleKind(kind: Kind) {
  kinds.value = kinds.value.includes(kind) ? kinds.value.filter(k => k !== kind) : [...kinds.value, kind].sort(byKind);
}
watch(selected, () => void loadDetail(), { immediate: true });
function authorsChanged() { void load(); }
onMounted(() => { if (route.query.view === "reports") { void router.replace("/automation"); return; } void load(); window.addEventListener("app:authors-changed", authorsChanged); timer = window.setInterval(() => { if (!document.hidden && !busy.value) void load(); }, 15000); });
onBeforeUnmount(() => { ++loadSequence; ++detailSequence; window.clearInterval(timer); window.clearTimeout(searchTimer); window.removeEventListener("app:authors-changed", authorsChanged); });
</script>
<template>
  <section class="workspace-page authors-page">
    <PageHeader title="订阅" description="关注作者，持续收集值得留下的内容。" :busy="loading" :updated-at="updated" refreshable @refresh="load">
      <MoreActions label="Instagram 操作"><RouterLink to="/settings/account-instagram">账号与采集节奏</RouterLink><RouterLink to="/automation">查看运行记录</RouterLink></MoreActions>
      <button class="btn primary" @click="openAdd"><Plus :size="15" />订阅作者</button>
    </PageHeader>
    <SubscriptionNavigation />
    <div v-if="sessionIssue" class="instagram-feedback" data-tone="error" role="status">
      <AlertTriangle :size="18" />
      <div><strong>{{ sessionIssue.title }}</strong><small>{{ sessionIssue.text }}</small></div>
      <RouterLink v-if="sessionIssue.action" class="btn ghost" to="/settings/account-instagram">{{ sessionIssue.action }}</RouterLink>
    </div>
    <div v-if="error && authors.length" class="load-error-banner" role="alert"><span>{{ error }}</span><button class="text-button" @click="load">重试</button></div>
    <FilterToolbar :active="subscribed ? (subscribed === 'true' ? '已订阅' : '已暂停') : ''" @clear="subscribed=''; filters()">
      <template #search><label class="search"><Search :size="16" /><input v-model="search" placeholder="搜索用户名、名称或 ID" aria-label="搜索 Instagram 作者" @input="searchInput" /></label></template>
      <select v-model="subscribed" aria-label="订阅状态" @change="filters"><option value="">全部订阅</option><option value="true">已订阅</option><option value="false">已暂停</option></select>
    </FilterToolbar>
    <div v-if="selectedIds.length" class="selection-bar" role="region" aria-label="批量操作">
      <span>已选择 {{ selectedIds.length }} 位作者</span>
      <button class="btn ghost" :disabled="busy" @click="mutate('/authors/batch', {ids:selectedIds,is_subscribed:false}, '已暂停所选作者')">暂停订阅</button>
      <button class="text-button" @click="selectedIds = []">取消选择</button>
    </div>
    <StateView v-if="!authors.length" :loading="loading" :error="error" :title="search || subscribed ? '没有匹配的作者' : '还没有订阅 Instagram 作者'" description="粘贴作者主页或用户名即可订阅；身份确认后会按请求预算分批补齐历史。" @retry="load" />
    <div v-else class="table-shell">
      <table class="data-table author-table ig-author-table">
        <thead><tr>
          <th class="select-col"><input type="checkbox" aria-label="选择本页全部作者" :checked="authors.length > 0 && authors.every(a => selectedIds.includes(a.id))" @change="selectedIds = ($event.target as HTMLInputElement).checked ? authors.map(a=>a.id) : []" /></th>
          <th>作者</th><th>采集内容</th><th>作品</th><th>状态</th><th>订阅</th><th class="actions-col">操作</th>
        </tr></thead>
        <tbody><tr v-for="author in authors" :key="author.id" :class="{selected:selected===author.id}">
          <td class="select-col"><input v-model="selectedIds" type="checkbox" :value="author.id" :aria-label="'选择 '+name(author)" /></td>
          <td data-label="作者"><div class="author-cell"><AuthorAvatar :id="author.id" platform="instagram" :url="author.avatar_url" /><div><button class="record-link" @click="openAuthor(author.id)">{{ name(author) }}</button><span>@{{ author.username }}</span></div></div></td>
          <td data-label="采集内容"><div class="ig-kind-list"><span v-for="kind in enabledKinds(author)" :key="kind" class="ig-kind" :data-kind="kind">{{ kindLabels[kind] }}</span><span v-if="!enabledKinds(author).length" class="muted">—</span></div></td>
          <td data-label="作品"><strong>{{ author.total_works.toLocaleString() }}</strong></td>
          <td data-label="状态"><div class="ig-state"><StatusIndicator :status="authorState(author).status" :label="authorState(author).label" /><small v-if="authorState(author).hint">{{ authorState(author).hint }}</small></div></td>
          <td data-label="订阅"><button class="switch" :class="{ on: author.is_subscribed }" role="switch" :aria-checked="author.is_subscribed" :aria-label="(author.is_subscribed ? '暂停订阅 ' : '恢复订阅 ') + name(author)" :disabled="busyIds.includes(author.id)" @click="toggleSubscription(author)"><i /></button></td>
          <td class="actions-col"><MoreActions :label="name(author)">
            <button @click="openAuthor(author.id)">查看作者详情</button>
            <button @click="openAuthor(author.id, 'works')">浏览作品</button>
            <button :disabled="busy || !author.is_subscribed" @click="scan(author)">立即检查</button>
            <a :href="author.profile_url" target="_blank" rel="noreferrer">打开 Instagram 主页</a>
          </MoreActions></td>
        </tr></tbody>
      </table>
    </div>
    <Pager :page="page" :pages="pages" :total="total" @change="changePage" />
    <InspectorPanel :open="Boolean(selected)" :title="detail ? name(detail) : 'Instagram 作者'" :subtitle="detail ? '@' + detail.username + ' · Instagram' : ''" @close="closeAuthor">
      <StateView v-if="!detail" :loading="!detailError" :error="detailError" @retry="loadDetail" />
      <template v-else>
        <nav class="view-tabs" aria-label="作者详情">
          <button :class="{ active: tab === 'overview' }" @click="tab = 'overview'">概览</button>
          <button :class="{ active: tab === 'works' }" @click="tab = 'works'">作品<span v-if="workTotal">{{ workTotal }}</span></button>
          <button :class="{ active: tab === 'highlights' }" @click="tab = 'highlights'">Highlights<span v-if="collectionTotal">{{ collectionTotal }}</span></button>
        </nav>
        <p v-if="detailError" role="alert" class="load-error-banner">{{ detailError }}</p>
        <template v-if="tab === 'overview'">
          <dl class="detail-grid ig-summary">
            <div><dt>订阅</dt><dd><StatusIndicator :status="authorState(detail).status" :label="authorState(detail).label" /></dd></div>
            <div><dt>已收录作品</dt><dd>{{ detail.total_works.toLocaleString() }}</dd></div>
            <div><dt>最近成功</dt><dd class="ig-small-value">{{ utc(detail.last_success_at) }}</dd></div>
            <div><dt>下次检查</dt><dd class="ig-small-value">{{ detail.is_subscribed ? relative(nextCheck(detail)) : '已暂停' }}</dd></div>
          </dl>
          <section class="ig-section">
            <header><h3>采集内容</h3><p>每种内容按各自周期检查；Stories 过期后无法补回。</p></header>
            <div class="ig-kind-picker" role="group" aria-label="采集内容">
              <button v-for="kind in kindOrder" :key="kind" type="button" class="ig-kind-option" :aria-pressed="kinds.includes(kind)" @click="toggleKind(kind)"><strong>{{ kindLabels[kind] }}</strong><small>{{ kindHelp[kind] }}</small></button>
            </div>
            <div class="detail-links">
              <button v-if="kindsDirty" class="btn primary" :disabled="busy || !kinds.length" @click="mutate(`/authors/${detail.id}/subscription`, {is_subscribed:true,kinds}, detail.is_subscribed ? '采集内容已更新' : '已恢复订阅')">{{ detail.is_subscribed ? '保存采集内容' : '恢复订阅' }}</button>
              <button class="btn ghost" :disabled="busy || !detail.is_subscribed" @click="scan(detail)">立即检查</button>
              <button v-if="detail.is_subscribed" class="text-button" :disabled="busy" @click="mutate(`/authors/${detail.id}/subscription`, {is_subscribed:false}, '已暂停订阅')">暂停订阅</button>
            </div>
          </section>
          <section class="ig-section">
            <header><h3>检查进度</h3><p>时间为计划值；请求预算或冷却可能让检查顺延。</p></header>
            <table class="ig-progress">
              <thead><tr><th>内容</th><th>新增检查</th><th>历史补齐</th></tr></thead>
              <tbody>
                <tr v-for="kind in kindOrder" :key="kind" :class="{ off: !stream(detail, kind, 'recent')?.enabled }">
                  <th scope="row">{{ kindLabels[kind] }}</th>
                  <td><span :class="{ danger: stream(detail, kind, 'recent')?.blocked_code }">{{ recentLabel(stream(detail, kind, 'recent')) }}</span><small v-if="stream(detail, kind, 'recent')?.enabled && stream(detail, kind, 'recent')?.deferred_reason">{{ stream(detail, kind, 'recent')?.deferred_reason }}</small></td>
                  <td v-if="kind === 'stories'"><span class="muted">不适用</span></td>
                  <td v-else>
                    <span :class="{ danger: stream(detail, kind, 'history')?.blocked_code }">{{ historyLabel(stream(detail, kind, 'history')) }}</span>
                    <progress v-if="stream(detail, kind, 'history')?.enabled && historyPercent(stream(detail, kind, 'history')) != null" :max="100" :value="historyPercent(stream(detail, kind, 'history')) ?? 0" :aria-label="kindLabels[kind] + ' 历史补齐进度'" />
                    <small v-if="stream(detail, kind, 'history')?.enabled && stream(detail, kind, 'history')?.deferred_reason">{{ stream(detail, kind, 'history')?.deferred_reason }}</small>
                  </td>
                </tr>
              </tbody>
            </table>
            <div class="detail-links">
              <button class="btn ghost" :disabled="busy || !detail.is_subscribed" @click="mutate(`/authors/${detail.id}/history`,{enabled:true}, '历史补齐已排入调度')">继续 / 重新补齐历史</button>
              <button class="text-button" :disabled="busy" @click="mutate(`/authors/${detail.id}/history`,{enabled:false}, '已暂停历史补齐')">暂停历史补齐</button>
            </div>
          </section>
          <div class="detail-links ig-links">
            <a class="text-button" :href="detail.profile_url" target="_blank" rel="noreferrer"><ExternalLink :size="14" />打开 Instagram 主页</a>
            <RouterLink class="text-button" :to="{path:'/library',query:{platform:'instagram',author:detail.nickname || detail.username}}">查看本地媒体</RouterLink>
          </div>
        </template>
        <template v-else-if="tab === 'works'">
          <StateView v-if="!works.length" title="尚未发现作品" description="首次检查完成后，作品会出现在这里。" />
          <ul v-else class="ig-work-list">
            <li v-for="work in works" :key="work.id">
              <span class="ig-kind" :data-kind="workKinds[work.content_kind] || work.content_kind">{{ kindLabels[work.content_kind] || work.content_kind }}</span>
              <div class="ig-work-copy"><strong :title="work.title || ''">{{ work.title || '无标题' }}</strong><small>{{ utc(work.published_at) }}<template v-if="work.expected_media_count"> · {{ work.expected_media_count }} 个媒体</template></small></div>
              <div class="ig-work-actions">
                <template v-if="work.download_task_id">
                  <button class="text-button" @click="preview(work.download_task_id)">预览</button>
                  <RouterLink class="text-button" :to="{path:'/operations/tasks',query:{task_key:`instagram:${work.download_task_id}`}}">任务</RouterLink>
                </template>
                <span v-else class="muted">待下载</span>
              </div>
            </li>
          </ul>
          <Pager v-if="workTotal" :page="workPage" :pages="workPages" :total="workTotal" @change="workPage=$event; loadWorks().catch(e=>detailError=e.message)" />
        </template>
        <template v-else>
          <StateView v-if="!collections.length" title="尚未采集 Highlights" :description="kinds.includes('highlights') ? '下次检查 Highlights 时会自动收录。' : '在概览中选择 Highlights 并保存后开始收录。'" />
          <ul v-else class="ig-work-list">
            <li v-for="collection in collections" :key="collection.id">
              <div class="ig-work-copy"><strong>{{ collection.title || '未命名精选' }}</strong><small>已发现 {{ collection.work_ids.length }} 条 Story</small></div>
              <div class="ig-work-actions"><a class="text-button" :href="collection.source_url" target="_blank" rel="noreferrer">查看原精选</a></div>
            </li>
          </ul>
          <Pager v-if="collectionTotal" :page="collectionPage" :pages="collectionPages" :total="collectionTotal" @change="collectionPage=$event; loadCollections().catch(e=>detailError=e.message)" />
        </template>
      </template>
    </InspectorPanel>
  </section>
</template>
