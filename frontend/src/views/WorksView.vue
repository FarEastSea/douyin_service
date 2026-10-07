<script setup lang="ts">
import PageHeader from "../components/PageHeader.vue";
import StatusIndicator from "../components/StatusIndicator.vue";
import StateView from "../components/StateView.vue";
import InspectorPanel from "../components/InspectorPanel.vue";
import TrendHistory from "../components/TrendHistory.vue";
import { confirmAction, dateTime, useContextActions } from "../workspace";
import IssueDetail from "../components/IssueDetail.vue";
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from "vue";
import {
  ArrowLeft,
  CheckSquare,
  Download,
  Eye,
  Image,
  RefreshCw,
  Search,
  TrendingUp,
  Trash2,
  X,
} from "@lucide/vue";
import { useRoute, useRouter } from "vue-router";
import { api } from "../api";
import { openMedia } from "../media";
import { focusFirst, restoreFocus, trapFocus } from "../focus";
import { useAppStore } from "../stores/app";
import Pager from "../components/Pager.vue";
import MoreActions from "../components/MoreActions.vue";
import FilterToolbar from "../components/FilterToolbar.vue";
import type { Author, MediaItem, PageData, Work } from "../types";

const route = useRoute(),
  router = useRouter(),
  store = useAppStore();
type WorkSort =
  "published_desc" | "published_asc" | "discovered_desc" | "discovered_asc";
type DownloadFilter =
  "all" | "completed" | "incomplete" | "active" | "failed" | "not_started";
type WorkTypeFilter = "all" | "video" | "images";
const author = ref<Author>(),
  works = ref<Work[]>([]),
  loading = ref(false),
  filter = ref<DownloadFilter>("all"),
  search = ref(""),
  selected = ref<number[]>([]);
const loadError = ref("");
const failedCovers = ref<Set<number>>(new Set());
const failedVideoPreviews = ref<Set<number>>(new Set());
const workType = ref<WorkTypeFilter>("all"),
  publishedFrom = ref(""),
  publishedTo = ref("");
const sort = ref<WorkSort>("published_desc");
const page = ref(1),
  pages = ref(1),
  total = ref(0),
  pageSize = 30;
const searchTimer = ref<number>();
const id = computed(() => Number(route.params.id));
const detailId = computed(() => Number(route.query.work) || 0),
  detail = ref<Work>(),
  detailBusy = ref(false),
  detailError = ref(""),
  tab = ref("files"),
  layout = ref("grid");
let sequence = 0,
  detailSequence = 0;
const loaded = ref(false);
page.value = Number(route.query.page) || 1;
search.value = String(route.query.q || "");
filter.value = (route.query.status || "all") as DownloadFilter;
workType.value = (route.query.type || "all") as WorkTypeFilter;
sort.value = (route.query.sort || "published_desc") as WorkSort;
publishedFrom.value = String(route.query.from || "");
publishedTo.value = String(route.query.to || "");
async function load() {
  const current = ++sequence;
  loading.value = true;
  const params = new URLSearchParams({
    paginated: "true",
    page: String(page.value),
    page_size: String(pageSize),
    sort_by: sort.value,
  });
  if (filter.value !== "all") params.set("download_status", filter.value);
  if (workType.value !== "all") params.set("work_type", workType.value);
  if (publishedFrom.value) params.set("published_from", publishedFrom.value);
  if (publishedTo.value) params.set("published_to", publishedTo.value);
  if (search.value.trim()) params.set("q", search.value.trim());
  try {
    const [authorData, data] = await Promise.all([
      api<Author>(`/authors/${id.value}`),
      api<PageData<Work>>(`/authors/${id.value}/works?${params}`),
    ]);
    if (current !== sequence) return;
    loaded.value = true;
    author.value = authorData;
    works.value = data.items;
    total.value = data.total;
    pages.value = data.pages;
    selected.value = [];
    failedCovers.value = new Set();
    failedVideoPreviews.value = new Set();
    loadError.value = "";
  } catch (error: any) {
    if (current !== sequence) return;
    loadError.value = error.message || "加载作品失败";
    store.notify(loadError.value, "error");
  } finally {
    if (current === sequence) loading.value = false;
  }
}
function changeFilters() {
  page.value = 1;
  syncQuery();
  void load();
}
function changeSort() {
  changeFilters();
}
function changePage(value: number) {
  page.value = value;
  syncQuery();
  void load();
}
function queueSearch() {
  if (searchTimer.value != null) window.clearTimeout(searchTimer.value);
  searchTimer.value = window.setTimeout(() => {
    changeFilters();
  }, 350);
}
function formatWorkTime(value?: string) {
  if (!value) return "未知";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "未知";
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}
function formatCount(value?: number) {
  if (value == null) return "—";
  if (value >= 100000000)
    return `${(value / 100000000).toFixed(value >= 1000000000 ? 0 : 1)}亿`;
  if (value >= 10000)
    return `${(value / 10000).toFixed(value >= 100000 ? 0 : 1)}万`;
  return String(value);
}
function formatDuration(value?: number) {
  if (value == null) return "";
  const seconds = Math.max(0, Math.round(value / 1000));
  const minutes = Math.floor(seconds / 60);
  return `${minutes ? `${minutes}:` : ""}${String(seconds % 60).padStart(minutes ? 2 : 1, "0")} 秒`;
}
function workSpecs(work: Work) {
  return [
    work.width && work.height ? `${work.width}×${work.height}` : "",
    formatDuration(work.duration_ms),
  ]
    .filter(Boolean)
    .join(" · ");
}
function hasStats(work: Work) {
  return [
    work.digg_count,
    work.comment_count,
    work.collect_count,
    work.share_count,
    work.play_count,
  ].some((value) => value != null);
}
function media(work: Work): MediaItem[] {
  if (work.work_type === "video") {
    const file = work.files.find(
      (item) => item.local_available && item.preview_url,
    );
    return file?.preview_url
      ? [{ url: file.preview_url, type: "video", title: work.title }]
      : [];
  }
  const local = work.files
    .filter((file) => file.preview_url)
    .map(
      (file) =>
        ({
          url: file.preview_url!,
          type: file.media_type === "video" ? "video" : "image",
          title: work.title,
        }) as MediaItem,
    );
  if (local.length) return local;
  return work.image_urls.map((url) => ({
    url,
    type: "image",
    title: work.title,
  }));
}
function preview(work: Work) {
  const items = media(work);
  if (items.length) openMedia(items);
  else store.notify("当前作品暂无可用预览", "info");
}
function markCoverFailed(workId: number) {
  failedCovers.value.add(workId);
}
function markVideoPreviewFailed(workId: number) {
  failedVideoPreviews.value.add(workId);
}
function primeVideoPreview(event: Event) {
  const video = event.currentTarget as HTMLVideoElement;
  if (Number.isFinite(video.duration) && video.duration > 0)
    video.currentTime = Math.min(0.1, video.duration / 2);
}
async function workAction(work: Work, endpoint: string, method = "POST") {
  try {
    const result = await api<any>(`/works/${work.id}/${endpoint}`, { method });
    store.notify(result.message || "操作成功");
    await load();
    if (detailId.value === work.id) await loadDetail();
  } catch (error: any) {
    store.notify(error.message || "操作失败", "error");
  }
}
async function remove(work: Work) {
  if (!(await confirmAction("确定删除该作品记录及已下载文件？"))) return;
  try {
    await api(`/works/${work.id}`, { method: "DELETE" });
    store.notify("作品已删除");
    closeDetail();
    await load();
  } catch (error: any) {
    store.notify(error.message || "删除失败", "error");
  }
}
async function batchDelete() {
  if (
    !selected.value.length ||
    !(await confirmAction(
      `确定删除选中的 ${selected.value.length} 个作品、关联任务和已下载文件？订阅不会自动重新下载这些作品。`,
    ))
  )
    return;
  try {
    const result = await api<any>("/works/batch-delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ work_ids: selected.value }),
    });
    store.notify(result.message || "批量删除完成");
    selected.value = [];
    await load();
  } catch (error: any) {
    store.notify(error.message || "批量删除失败", "error");
  }
}
function syncQuery() {
  void router.replace({
    query: {
      ...route.query,
      page: page.value > 1 ? String(page.value) : undefined,
      q: search.value || undefined,
      status: filter.value !== "all" ? filter.value : undefined,
      type: workType.value !== "all" ? workType.value : undefined,
      sort: sort.value !== "published_desc" ? sort.value : undefined,
      from: publishedFrom.value || undefined,
      to: publishedTo.value || undefined,
    },
  });
}
function openDetail(work: Work) {
  tab.value = "files";
  void router.replace({ query: { ...route.query, work: String(work.id) } });
}
function closeDetail() {
  void router.replace({ query: { ...route.query, work: undefined } });
}
async function loadDetail() {
  const current = ++detailSequence;
  detail.value = undefined;
  detailError.value = "";
  if (!detailId.value) return;
  detailBusy.value = true;
  try {
    const data = await api<Work>(`/works/${detailId.value}`);
    if (current === detailSequence) {
      if (data.author_id !== id.value) throw new Error("该作品不属于当前作者");
      detail.value = data;
    }
  } catch (e: any) {
    if (current === detailSequence) detailError.value = e.message;
  } finally {
    if (current === detailSequence) detailBusy.value = false;
  }
}
async function removeFile(index: number) {
  if (!detail.value) return;
  const workId = detail.value.id;
  if (
    !(await confirmAction(
      `删除作品“${detail.value.title || detail.value.aweme_id}”的第 ${index + 1} 个文件？只删除该文件及关联任务，其他文件保留。后续订阅不会自动重新下载这一项。`,
      "删除单个文件",
      "删除文件",
    ))
  )
    return;
  try {
    const data = await api<any>(`/works/${workId}/files/${index}`, {
      method: "DELETE",
    });
    store.notify(data.message);
    await load();
    await loadDetail();
  } catch (e: any) {
    store.notify(e.message, "error");
  }
}
useContextActions(() =>
  detail.value
    ? [
        {
          id: "preview-current-work",
          label: "预览当前作品",
          run: () => preview(detail.value!),
        },
        {
          id: "download-current-work",
          label: "重新下载当前作品",
          run: () => workAction(detail.value!, "redownload"),
          disabled: store.risk.active,
        },
      ]
    : [],
);
watch(detailId, loadDetail);
watch(id, () => {
  sequence++;
  detailSequence++;
  works.value = [];
  author.value = undefined;
  selected.value = [];
  page.value = 1;
  void load();
  void loadDetail();
});
watch(
  () => route.query,
  () => {
    const next = {
      page: Number(route.query.page) || 1,
      q: String(route.query.q || ""),
      status: String(route.query.status || "all"),
      type: String(route.query.type || "all"),
      sort: String(route.query.sort || "published_desc"),
      from: String(route.query.from || ""),
      to: String(route.query.to || ""),
    };
    if (
      next.page === page.value &&
      next.q === search.value &&
      next.status === filter.value &&
      next.type === workType.value &&
      next.sort === sort.value &&
      next.from === publishedFrom.value &&
      next.to === publishedTo.value
    )
      return;
    page.value = next.page;
    search.value = next.q;
    filter.value = next.status as DownloadFilter;
    workType.value = next.type as WorkTypeFilter;
    sort.value = next.sort as WorkSort;
    publishedFrom.value = next.from;
    publishedTo.value = next.to;
    void load();
  },
);
onMounted(() => {
  void load();
  void loadDetail();
});
onBeforeUnmount(() => {
  sequence++;
  detailSequence++;
  clearTimeout(searchTimer.value);
});
</script>

<template>
  <section class="workspace-page works-workspace">
    <RouterLink class="back-link" to="/authors/douyin"
      ><ArrowLeft :size="14" />作者</RouterLink
    ><PageHeader
      :title="author?.nickname || '作者作品'"
      :description="
        (loaded ? total.toLocaleString() : '—') + ' 个作品 · 完整浏览与文件管理'
      "
      :busy="loading"
      refreshable
      @refresh="load"
      ><div class="segmented-control" aria-label="作品布局">
        <button :aria-pressed="layout === 'grid'" @click="layout = 'grid'">
          网格</button
        ><button :aria-pressed="layout === 'list'" @click="layout = 'list'">
          列表
        </button>
      </div></PageHeader
    >
    <div v-if="loadError && works.length" class="load-error-banner">
      <IssueDetail :message="loadError" impact="显示上次读取的数据。" />
    </div>
    <FilterToolbar
      :active="
        [
          filter !== 'all' && '状态已筛选',
          workType !== 'all' && '类型已筛选',
          publishedFrom && '起始日期',
          publishedTo && '结束日期',
          sort !== 'published_desc' && '排序已修改',
        ]
          .filter(Boolean)
          .join(' · ')
      "
      @clear="
        filter = 'all';
        workType = 'all';
        publishedFrom = '';
        publishedTo = '';
        sort = 'published_desc';
        changeFilters();
      "
      ><template #search
        ><label class="search"
          ><Search :size="16" /><input
            v-model="search"
            aria-label="搜索全部作品"
            placeholder="搜索全部作品"
            @input="queueSearch" /></label></template
      ><label class="work-sort"
        ><span>状态</span
        ><select v-model="filter" aria-label="下载状态" @change="changeFilters">
          <option value="all">全部状态</option>
          <option value="completed">已下载</option>
          <option value="incomplete">未完成</option>
          <option value="active">处理中</option>
          <option value="failed">失败/取消</option>
          <option value="not_started">未创建任务</option>
        </select></label
      ><label class="work-sort"
        ><span>类型</span
        ><select
          v-model="workType"
          aria-label="作品类型"
          @change="changeFilters"
        >
          <option value="all">全部类型</option>
          <option value="video">视频</option>
          <option value="images">图集</option>
        </select></label
      ><label class="work-sort work-date"
        ><span>从</span
        ><input
          v-model="publishedFrom"
          type="date"
          aria-label="发布日期起始"
          @change="changeFilters" /></label
      ><label class="work-sort work-date"
        ><span>至</span
        ><input
          v-model="publishedTo"
          type="date"
          aria-label="发布日期结束"
          @change="changeFilters" /></label
      ><label class="work-sort"
        ><span>排序</span
        ><select v-model="sort" aria-label="作品排序方式" @change="changeSort">
          <option value="published_desc">作品时间：最新</option>
          <option value="published_asc">作品时间：最早</option>
          <option value="discovered_desc">收录时间：最新</option>
          <option value="discovered_asc">收录时间：最早</option>
        </select></label
      ></FilterToolbar
    >
    <div v-if="selected.length" class="selection-bar">
      <span>已选择 {{ selected.length }} 个当前页作品</span
      ><button class="btn compact danger" @click="batchDelete">
        删除作品及文件</button
      ><button class="text-button" @click="selected = []">取消选择</button>
    </div>
    <StateView
      v-if="!works.length"
      :loading="loading"
      :error="loadError"
      title="没有符合条件的作品"
      @retry="load"
    />
    <div v-else class="work-grid" :class="{ 'list-layout': layout === 'list' }">
      <article
        v-for="work in works"
        :key="work.id"
        class="work-card"
        :class="{ selected: detailId === work.id }"
      >
        <div class="work-cover">
          <button
            class="cover-preview"
            :aria-label="'预览 ' + (work.title || work.aweme_id)"
            @click="preview(work)"
          >
            <img
              v-if="work.primary_preview_url && !failedCovers.has(work.id)"
              :src="work.primary_preview_url"
              alt=""
              loading="lazy"
              @error="markCoverFailed(work.id)"
            />
            <div v-else class="cover-placeholder">
              <Image :size="24" /><small>封面暂不可用</small>
            </div></button
          ><span>{{
            work.work_type === "images" ? work.image_count + " 张" : "视频"
          }}</span
          ><label class="select-box"
            ><input
              type="checkbox"
              :aria-label="'选择作品 ' + work.aweme_id"
              :checked="selected.includes(work.id)"
              @change="
                selected = selected.includes(work.id)
                  ? selected.filter((value) => value !== work.id)
                  : [...selected, work.id]
              "
          /></label>
        </div>
        <div class="work-copy">
          <button
            class="record-link"
            :title="work.title"
            @click="openDetail(work)"
          >
            {{ work.title || "作品 " + work.aweme_id }}</button
          ><time>{{ dateTime(work.published_at) }}</time
          ><StatusIndicator
            :status="work.download_status"
            :label="work.is_downloaded ? '已下载' : undefined"
          /><span
            >{{ work.completed_task_count }}/{{
              work.total_task_count
            }}
            个文件</span
          >
        </div>
        <footer>
          <button class="text-button" @click="openDetail(work)">详情</button
          ><MoreActions :label="'作品 ' + work.aweme_id"
            ><button @click="preview(work)">预览</button
            ><button
              @click="
                openDetail(work);
                tab = 'stats';
              "
            >
              互动趋势</button
            ><button
              :disabled="store.risk.active"
              @click="workAction(work, 'redownload')"
            >
              重新下载</button
            ><button
              v-if="work.download_status === 'failed'"
              :disabled="store.risk.active"
              @click="workAction(work, 'retry-failed')"
            >
              重试失败文件</button
            ><button class="danger" @click="remove(work)">
              删除作品及文件
            </button></MoreActions
          >
        </footer>
      </article>
    </div>
    <Pager
      v-if="loaded"
      :page="page"
      :pages="pages"
      :total="total"
      @change="changePage"
    />
    <InspectorPanel
      :open="!!detailId"
      :title="detail?.title || '作品详情'"
      :subtitle="detail?.aweme_id"
      @close="closeDetail"
      ><StateView
        v-if="!detail"
        :loading="detailBusy"
        :error="detailError"
        @retry="loadDetail"
      /><template v-else
        ><nav class="view-tabs" aria-label="作品详情">
          <button
            v-for="item in [
              ['files', '文件'],
              ['metadata', '元数据'],
              ['stats', '趋势'],
            ]"
            :key="item[0]"
            :class="{ active: tab === item[0] }"
            :aria-pressed="tab === item[0]"
            @click="tab = item[0]"
          >
            {{ item[1] }}
          </button>
        </nav>
        <template v-if="tab === 'files'"
          ><button class="btn" @click="preview(detail)">预览作品</button>
          <div class="file-list">
            <article v-for="file in detail.files" :key="file.task_id">
              <div>
                <strong>{{
                  file.file_name || "文件 " + (file.file_index + 1)
                }}</strong
                ><StatusIndicator :status="file.status" />
              </div>
              <button
                v-if="file.preview_url"
                class="text-button"
                @click="
                  openMedia([
                    {
                      url: file.preview_url,
                      type: file.media_type === 'video' ? 'video' : 'image',
                      title: detail.title,
                    },
                  ])
                "
              >
                预览</button
              ><button
                v-if="detail.work_type === 'images'"
                class="text-button danger"
                @click="removeFile(file.file_index)"
              >
                删除此文件
              </button>
            </article>
          </div>
          <p v-if="!detail.files.length" class="inline-note">
            尚无下载文件。
          </p></template
        >
        <dl v-else-if="tab === 'metadata'" class="detail-fields">
          <div>
            <dt>发布时间</dt>
            <dd>{{ formatWorkTime(detail.published_at) }}</dd>
          </div>
          <div>
            <dt>收录时间</dt>
            <dd>{{ formatWorkTime(detail.discovered_at) }}</dd>
          </div>
          <div>
            <dt>规格</dt>
            <dd>{{ workSpecs(detail) || "未提供" }}</dd>
          </div>
          <div>
            <dt>标签</dt>
            <dd>{{ detail.hashtags?.join(" · ") || "未提供" }}</dd>
          </div>
          <div>
            <dt>音乐</dt>
            <dd>
              {{ detail.music_title || "未提供" }} {{ detail.music_author }}
            </dd>
          </div>
          <div>
            <dt>数据版本</dt>
            <dd>
              {{ detail.metadata_schema_version }} /
              {{ detail.raw_data_version }}
            </dd>
          </div>
        </dl>
        <TrendHistory
          v-else
          :endpoint="'/works/' + detail.id + '/stats'"
          work /></template
      ><template #footer
        ><template v-if="detail"
          ><button
            class="btn"
            :disabled="store.risk.active"
            @click="workAction(detail, 'redownload')"
          >
            重新下载</button
          ><button class="btn danger" @click="remove(detail)">
            删除作品及文件
          </button></template
        ></template
      ></InspectorPanel
    >
  </section>
</template>
