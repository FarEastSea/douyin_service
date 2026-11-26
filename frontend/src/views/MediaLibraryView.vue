<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  ArrowUpRight,
  Check,
  Download,
  Image,
  ListChecks,
  Play,
  Plus,
  Search,
  Trash2,
} from "@lucide/vue";
import { api, jsonBody } from "../api";
import { useAppStore } from "../stores/app";
import { isVideoMedia, mediaPreviewType, openMedia } from "../media";
import {
  bytes,
  confirmAction,
  confirmWithOption,
  dateTime,
  usePlatformNames,
  useContextActions,
} from "../workspace";
import type { LibraryMedia, MediaItem, PageData } from "../types";
import PageHeader from "../components/PageHeader.vue";
import FilterToolbar from "../components/FilterToolbar.vue";
import MediaThumbnail from "../components/MediaThumbnail.vue";
import StateView from "../components/StateView.vue";
import Pager from "../components/Pager.vue";
import InspectorPanel from "../components/InspectorPanel.vue";
import IssueDetail from "../components/IssueDetail.vue";
const platformNames = usePlatformNames();

interface BatchDeleteResult {
  message?: string;
  data?: {
    succeeded: string[];
    failed: Array<{ key: string; message: string }>;
    removed_files: number;
  };
}

const SORTS: Record<string, string> = {
  created_desc: "最近入库",
  created_asc: "最早入库",
  size_desc: "体积最大",
  size_asc: "体积最小",
  published_desc: "最近发布",
};
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const route = useRoute(),
  router = useRouter(),
  store = useAppStore();
const text = (value: unknown) => (typeof value === "string" ? value : "");
const sortOf = (value: unknown) => (text(value) in SORTS ? text(value) : "created_desc");
const dateOf = (value: unknown) => (DATE_PATTERN.test(text(value)) ? text(value) : "");
const categoryOf = (value: unknown) =>
  ["indexed", "orphaned"].includes(text(value)) ? text(value) : "";
const queryPage = () => Math.max(1, Math.floor(Number(route.query.page) || 1));
const items = ref<LibraryMedia[]>([]),
  total = ref(0),
  pages = ref(1),
  page = ref(queryPage());
const platform = ref(text(route.query.platform)),
  type = ref(text(route.query.type)),
  search = ref(text(route.query.q)),
  author = ref(text(route.query.author));
const authorId = ref(text(route.query.author_id));
const sort = ref(sortOf(route.query.sort)),
  category = ref(categoryOf(route.query.category));
const dateFrom = ref(dateOf(route.query.from)),
  dateTo = ref(dateOf(route.query.to));
const orphanedTotal = ref(0),
  deleting = ref(false),
  serverToday = ref("");
const selecting = ref(false),
  selected = ref<string[]>([]);
const loading = ref(true),
  loaded = ref(false),
  error = ref(""),
  updatedAt = ref("");
const inspected = computed(() => items.value.find((item) => item.key === text(route.query.media)));
const detailOpen = computed(() => Boolean(route.query.media));
let sequence = 0,
  controller: AbortController | undefined,
  searchTimer: number | undefined;
const endpoint = (value?: string) =>
  value ? (value.startsWith("/api/") ? value : `/api${value}`) : "";
const previewItems = computed<MediaItem[]>(() =>
  items.value
    .filter((item) => item.available !== false)
    .map((item) => ({
      url: endpoint(item.preview_endpoint),
      type: mediaPreviewType(item.media_type),
      title: item.title || item.filename,
    })),
);
const dateRangeInvalid = computed(() =>
  Boolean(dateFrom.value && dateTo.value && dateFrom.value > dateTo.value),
);
const dateLabel = computed(() =>
  dateFrom.value && dateTo.value
    ? `${dateFrom.value} 至 ${dateTo.value}`
    : dateFrom.value
      ? `${dateFrom.value} 起`
      : dateTo.value
        ? `截至 ${dateTo.value}`
        : "",
);
const hasFilters = computed(() =>
  Boolean(
    search.value ||
    platform.value ||
    type.value ||
    category.value ||
    authorId.value ||
    author.value ||
    dateFrom.value ||
    dateTo.value ||
    sort.value !== "created_desc",
  ),
);
// 只有按入库时间排序时，相邻项目才属于同一天，分组标题才有意义。
const timeline = computed(() => sort.value === "created_desc" || sort.value === "created_asc");
const priorityItems = computed(() => items.value.slice(0, 4));
const groups = computed(() => {
  if (!timeline.value) return [{ key: "all", label: "", items: items.value }];
  const result: Array<{ key: string; label: string; items: LibraryMedia[] }> = [];
  for (const item of items.value) {
    const key = dayKey(item.completed_at || item.created_at);
    const last = result[result.length - 1];
    if (last?.key === key) last.items.push(item);
    else result.push({ key, label: dayLabel(key), items: [item] });
  }
  return result;
});
const selectedItems = computed(() =>
  items.value.filter((item) => selected.value.includes(item.key)),
);
const selectedBytes = computed(() =>
  selectedItems.value.reduce((sum, item) => sum + (item.file_size || 0), 0),
);
const allSelected = computed(
  () => items.value.length > 0 && selectedItems.value.length === items.value.length,
);

// 接口返回的是服务器本地时间（不带时区）。分组直接取其中的日期部分，并与服务器给出的“今天”比较，
// 这样分组、日期筛选和洞察页的日期口径一致，不随浏览器所在时区变化。
function dayKey(value?: string) {
  return value && /^\d{4}-\d{2}-\d{2}/.test(value) ? value.slice(0, 10) : "";
}
function shiftDay(key: string, days: number) {
  const [year, month, day] = key.split("-").map(Number);
  return new Date(Date.UTC(year!, month! - 1, day! + days)).toISOString().slice(0, 10);
}
function dayLabel(key: string) {
  if (!key) return "时间未确认";
  if (serverToday.value) {
    if (key === serverToday.value) return "今天";
    if (key === shiftDay(serverToday.value, -1)) return "昨天";
  }
  const [year, month, day] = key.split("-").map(Number);
  return serverToday.value.startsWith(year + "-")
    ? `${month} 月 ${day} 日`
    : `${year} 年 ${month} 月 ${day} 日`;
}
// keepDetail 为 false 时关闭详情：翻页或切换到另一位作者后，原来查看的媒体通常已不在当前页。
function syncQuery(keepDetail = true) {
  void router.replace({
    query: {
      category: category.value || undefined,
      platform: platform.value || undefined,
      type: type.value || undefined,
      q: search.value.trim() || undefined,
      author: author.value.trim() || undefined,
      author_id: authorId.value || undefined,
      sort: sort.value === "created_desc" ? undefined : sort.value,
      from: dateFrom.value || undefined,
      to: dateTo.value || undefined,
      page: page.value > 1 ? String(page.value) : undefined,
      media: keepDetail ? text(route.query.media) || undefined : undefined,
    },
  });
}
async function load() {
  if (dateRangeInvalid.value) {
    error.value = "开始日期不能晚于结束日期";
    loading.value = false;
    return;
  }
  const current = ++sequence;
  controller?.abort();
  controller = new AbortController();
  loading.value = true;
  const params = new URLSearchParams({
    page: String(page.value),
    page_size: "30",
    sort_by: sort.value,
  });
  if (category.value) params.set("library_status", category.value);
  if (platform.value) params.set("platform", platform.value);
  if (type.value) params.set("media_type", type.value);
  if (search.value.trim()) params.set("q", search.value.trim());
  if (author.value.trim()) params.set("author", author.value.trim());
  if (authorId.value) params.set("author_id", authorId.value);
  if (dateFrom.value) params.set("date_from", dateFrom.value);
  if (dateTo.value) params.set("date_to", dateTo.value);
  try {
    const data = await api<PageData<LibraryMedia> & { orphaned_total?: number; today?: string }>(
      `/operations/media?${params}`,
      { signal: controller.signal },
    );
    if (current !== sequence) return;
    if (page.value > Math.max(1, data.pages)) {
      page.value = Math.max(1, data.pages);
      syncQuery();
      void load();
      return;
    }
    items.value = data.items;
    total.value = data.total;
    pages.value = data.pages;
    orphanedTotal.value = data.orphaned_total ?? 0;
    serverToday.value = data.today || "";
    // 选择只对当前页有效；翻页或筛选后丢弃已不在页面上的项，避免删除看不见的内容。
    selected.value = selected.value.filter((key) => data.items.some((item) => item.key === key));
    loaded.value = true;
    error.value = "";
    updatedAt.value = new Date().toISOString();
  } catch (reason: any) {
    if (current === sequence && reason.name !== "AbortError")
      error.value = reason.message || "媒体库读取失败";
  } finally {
    if (current === sequence) loading.value = false;
  }
}
function filters(keepDetail = true) {
  clearTimeout(searchTimer);
  searchTimer = undefined;
  page.value = 1;
  syncQuery(keepDetail);
  void load();
}
function queueSearch() {
  clearTimeout(searchTimer);
  searchTimer = window.setTimeout(() => filters(), 350);
}
function changePage(value: number) {
  page.value = value;
  syncQuery(false);
  void load();
}
function clearFilters() {
  sort.value = "created_desc";
  category.value = "";
  platform.value = "";
  author.value = "";
  authorId.value = "";
  type.value = "";
  search.value = "";
  dateFrom.value = "";
  dateTo.value = "";
  filters();
}
function inspect(item: LibraryMedia) {
  void router.replace({ query: { ...route.query, media: item.key } });
}
function closeDetail() {
  void router.replace({ query: { ...route.query, media: undefined } });
}
function preview(item: LibraryMedia) {
  const index = previewItems.value.findIndex(
    (entry) => entry.url === endpoint(item.preview_endpoint),
  );
  if (index >= 0) openMedia(previewItems.value, index);
}
function toggleSelecting() {
  selecting.value = !selecting.value;
  if (!selecting.value) selected.value = [];
}
function toggleSelected(item: LibraryMedia) {
  selected.value = selected.value.includes(item.key)
    ? selected.value.filter((key) => key !== item.key)
    : [...selected.value, item.key];
}
function toggleAll() {
  selected.value = allSelected.value ? [] : items.value.map((item) => item.key);
}
function activate(item: LibraryMedia) {
  if (selecting.value) toggleSelected(item);
  else preview(item);
}
async function removeSelected() {
  if (deleting.value || !selectedItems.value.length) return;
  const targets = selectedItems.value;
  const indexed = targets.some((item) => item.library_status !== "orphaned");
  const message = `删除选中的 ${targets.length} 项媒体（共 ${bytes(selectedBytes.value)}）？
文件会从下载目录中删除，不可撤销。`;
  let block = false;
  if (indexed) {
    const choice = await confirmWithOption(
      message,
      {
        label: "阻止订阅重新下载这些媒体",
        help: "默认不勾选：作者处于订阅状态时，下一次订阅检查会重新下载。勾选后订阅会跳过它们，之后可在“系统 → 排除项目”中恢复下载。",
      },
      "批量删除媒体",
      `删除 ${targets.length} 项`,
    );
    if (!choice.confirmed) return;
    block = choice.checked;
  } else if (!(await confirmAction(message, "批量删除媒体", `删除 ${targets.length} 项`))) return;
  deleting.value = true;
  try {
    const result = await api<BatchDeleteResult>("/operations/media/batch-delete", {
      method: "POST",
      timeoutMs: 120000,
      ...jsonBody({ keys: targets.map((item) => item.key), block_redownload: block }),
    });
    const failed = result.data?.failed || [];
    if (failed.length)
      store.notify(
        `${result.message || "部分媒体未删除"}。首个原因：${failed[0]!.message}`,
        "error",
      );
    else store.notify(result.message || "已删除选中的媒体");
    selected.value = failed.map((item) => item.key);
    if (!failed.length) selecting.value = false;
    if (
      inspected.value &&
      !failed.some((item) => item.key === inspected.value?.key) &&
      targets.some((item) => item.key === inspected.value?.key)
    )
      closeDetail();
    await load();
  } catch (reason: any) {
    store.notify(reason.message || "批量删除失败", "error");
    await load();
  } finally {
    deleting.value = false;
  }
}
async function removeIndexed(item: LibraryMedia) {
  if (deleting.value) return;
  const choice = await confirmWithOption(
    `删除“${item.title || item.filename}”？
文件会从下载目录中删除，不可撤销。`,
    {
      label: "阻止订阅重新下载此媒体",
      help: "默认不勾选：作者处于订阅状态时，下一次订阅检查会重新下载。勾选后订阅会跳过它，之后可在“系统 → 排除项目”中恢复下载。",
    },
    "删除媒体文件",
    "删除文件",
  );
  if (!choice.confirmed) return;
  deleting.value = true;
  try {
    const result = await api<{ message?: string }>(
      `/operations/media/${encodeURIComponent(item.platform)}/${item.id}?block_redownload=${choice.checked}`,
      { method: "DELETE" },
    );
    store.notify(result.message || "已删除文件");
    closeDetail();
    await load();
  } catch (reason: any) {
    store.notify(reason.message || "删除失败", "error");
  } finally {
    deleting.value = false;
  }
}
function removeMedia(item: LibraryMedia) {
  return item.library_status === "orphaned" ? removeOrphan(item) : removeIndexed(item);
}
async function removeOrphan(item: LibraryMedia) {
  if (deleting.value || item.library_status !== "orphaned") return;
  const missing = item.available === false;
  const message = missing
    ? `移除无索引媒体“${item.title || item.filename}”的登记？
文件已不在下载目录中，只删除这条登记。`
    : `删除无索引媒体“${item.title || item.filename}”？
文件会从下载目录中删除，不可撤销。`;
  if (
    !(await confirmAction(
      message,
      missing ? "删除登记" : "删除媒体文件",
      missing ? "删除登记" : "删除文件",
    ))
  )
    return;
  deleting.value = true;
  try {
    const result = await api<{ message?: string }>(`/operations/media/orphan/${item.id}`, {
      method: "DELETE",
    });
    store.notify(result.message || "已删除文件");
    closeDetail();
    await load();
  } catch (reason: any) {
    store.notify(reason.message || "删除失败", "error");
  } finally {
    deleting.value = false;
  }
}
function newDownload() {
  window.dispatchEvent(new CustomEvent("app:new-download"));
}
function duration(item: LibraryMedia) {
  if (!item.duration_ms) return "";
  const seconds = Math.round(item.duration_ms / 1000);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
function kindLabel(item: LibraryMedia) {
  return ["live", "live_photo"].includes(item.media_type)
    ? "实况"
    : item.media_type === "video"
      ? duration(item) || "视频"
      : "图片";
}
useContextActions(() => [
  { id: "refresh-library", label: "刷新媒体库", run: load },
  {
    id: "select-media",
    label: selecting.value ? "退出媒体多选" : "多选媒体",
    run: toggleSelecting,
  },
  { id: "new-media", label: "新建下载", run: newDownload },
]);
watch(
  () => route.query,
  () => {
    // 搜索框仍在防抖等待时，以用户正在输入的内容为准，不让路由里的旧值覆盖它。
    if (searchTimer !== undefined) return;
    const next = [
      categoryOf(route.query.category),
      text(route.query.platform),
      text(route.query.type),
      text(route.query.q),
      text(route.query.author),
      text(route.query.author_id),
      sortOf(route.query.sort),
      dateOf(route.query.from),
      dateOf(route.query.to),
      queryPage(),
    ] as const;
    if (
      next.join("|") ===
      [
        category.value,
        platform.value,
        type.value,
        search.value.trim(),
        author.value.trim(),
        authorId.value,
        sort.value,
        dateFrom.value,
        dateTo.value,
        page.value,
      ].join("|")
    )
      return;
    category.value = next[0];
    platform.value = next[1];
    type.value = next[2];
    search.value = next[3];
    author.value = next[4];
    authorId.value = next[5];
    sort.value = next[6];
    dateFrom.value = next[7];
    dateTo.value = next[8];
    page.value = next[9];
    void load();
  },
);
onMounted(load);
onBeforeUnmount(() => {
  sequence++;
  controller?.abort();
  clearTimeout(searchTimer);
});
</script>

<template>
  <section class="workspace-page library-workspace">
    <PageHeader
      title="媒体库"
      description="保存下来的图片、视频与实况。"
      :busy="loading"
      :updated-at="updatedAt"
      refreshable
      @refresh="load"
    >
      <button
        class="btn"
        :aria-pressed="selecting"
        :disabled="!items.length && !selecting"
        @click="toggleSelecting"
      >
        <ListChecks :size="17" />
        {{ selecting ? "完成选择" : "多选" }}
      </button>
      <button
        class="btn primary workspace-create"
        @click="newDownload"
      >
        <Plus :size="17" />
        新建下载
      </button>
    </PageHeader>
    <nav
      class="view-tabs"
      aria-label="媒体类型"
    >
      <button
        v-for="option in [
          ['', '全部媒体'],
          ['video', '视频'],
          ['image', '图片'],
          ['live', '实况'],
        ]"
        :key="option[0]"
        :class="{ active: type === option[0] }"
        :aria-pressed="type === option[0]"
        @click="
          type = option[0]!;
          filters();
        "
      >
        {{ option[1] }}
      </button>
    </nav>
    <nav
      class="view-tabs library-category"
      aria-label="媒体分类"
    >
      <button
        v-for="option in [
          ['', '全部'],
          ['indexed', '已索引'],
          ['orphaned', '无索引媒体'],
        ]"
        :key="option[0]"
        :class="{ active: category === option[0] }"
        :aria-pressed="category === option[0]"
        @click="
          category = option[0]!;
          filters();
        "
      >
        {{ option[1] }}
        <span v-if="option[0] === 'orphaned' && orphanedTotal">
          {{ orphanedTotal.toLocaleString() }}
        </span>
      </button>
    </nav>
    <p
      v-if="category === 'orphaned'"
      class="inline-note"
    >
      无索引媒体是任务记录已删除、但文件仍保留在下载目录中的媒体，以及存储审计发现的未登记文件。可以照常预览、下载和删除。
    </p>
    <FilterToolbar
      :active="[
        platform && platformNames[platform],
        (author || authorId) && '作者：' + (author || authorId),
        dateLabel && '入库：' + dateLabel,
        sort !== 'created_desc' && SORTS[sort],
      ]"
      @clear="clearFilters"
    >
      <template #search>
        <label class="search">
          <Search :size="17" />
          <input
            v-model="search"
            aria-label="搜索已保存媒体"
            placeholder="搜索标题、作者或文件名"
            @input="queueSearch"
          />
        </label>
      </template>
      <select
        v-model="platform"
        aria-label="筛选媒体平台"
        @change="
          authorId = '';
          filters();
        "
      >
        <option value="">全部平台</option>
        <option
          v-for="(label, key) in platformNames"
          :key="key"
          :value="key"
        >
          {{ label }}
        </option>
      </select>
      <input
        v-model="author"
        class="author-filter"
        aria-label="筛选作者名称"
        placeholder="作者名称"
        @input="
          authorId = '';
          queueSearch();
        "
      />
      <label class="date-filter">
        <span>入库自</span>
        <input
          v-model="dateFrom"
          type="date"
          aria-label="入库开始日期"
          :max="dateTo || undefined"
          :aria-invalid="dateRangeInvalid"
          @change="filters()"
        />
      </label>
      <label class="date-filter">
        <span>至</span>
        <input
          v-model="dateTo"
          type="date"
          aria-label="入库结束日期"
          :min="dateFrom || undefined"
          :aria-invalid="dateRangeInvalid"
          @change="filters()"
        />
      </label>
      <select
        v-model="sort"
        aria-label="媒体排序"
        @change="filters()"
      >
        <option
          v-for="(label, key) in SORTS"
          :key="key"
          :value="key"
        >
          {{ label }}
        </option>
      </select>
    </FilterToolbar>
    <div class="library-context">
      <h2>
        {{
          type === "video"
            ? "视频"
            : type === "image"
              ? "图片"
              : type === "live"
                ? "实况"
                : "全部媒体"
        }}
        <span>{{ loaded ? total.toLocaleString() : "-" }}</span>
      </h2>
      <span>
        按{{ SORTS[sort] }}排序
        <span
          v-if="!selecting"
          class="library-preview-hint"
        >
          ，可连续预览当前页
        </span>
      </span>
    </div>
    <div
      v-if="selecting"
      class="selection-bar"
      role="region"
      aria-label="媒体多选"
    >
      <span>
        已选 {{ selectedItems.length }} 项
        <template v-if="selectedItems.length">· {{ bytes(selectedBytes) }}</template>
        <template v-else>，点击封面选择</template>
      </span>
      <div>
        <button
          class="btn compact"
          :disabled="!items.length"
          @click="toggleAll"
        >
          {{ allSelected ? "取消全选" : "全选本页" }}
        </button>
        <button
          class="btn compact danger"
          :disabled="!selectedItems.length || deleting"
          @click="removeSelected"
        >
          <Trash2 :size="15" />
          {{ deleting ? "删除中…" : "删除所选" }}
        </button>
        <button
          class="btn compact"
          @click="toggleSelecting"
        >
          退出
        </button>
      </div>
    </div>
    <div
      v-if="error && items.length"
      class="load-error-banner"
      role="alert"
    >
      <IssueDetail
        :message="error"
        impact="显示上次读取的媒体。"
      />
      <button
        class="btn ghost compact"
        @click="load"
      >
        重试
      </button>
    </div>
    <StateView
      v-if="!items.length"
      layout="grid"
      :loading="loading"
      :error="error"
      :title="hasFilters ? '没有找到匹配的媒体' : '媒体库等待第一份内容'"
      description="下载完成的媒体会出现在这里。也可以调整筛选后重新查找。"
      @retry="load"
    >
      <button
        v-if="hasFilters"
        class="btn"
        @click="clearFilters"
      >
        清除筛选
      </button>
      <button
        class="btn primary"
        @click="newDownload"
      >
        添加下载
      </button>
    </StateView>
    <div
      v-else
      class="library-grid"
      :class="{ selecting }"
      :aria-busy="loading"
    >
      <template
        v-for="group in groups"
        :key="group.key"
      >
        <h3
          v-if="group.label"
          class="library-day"
        >
          {{ group.label }}
          <span>{{ group.items.length }} 项</span>
        </h3>
        <article
          v-for="item in group.items"
          :key="item.key"
          class="library-item"
          :class="{ selected: inspected?.key === item.key, picked: selected.includes(item.key) }"
        >
          <button
            class="library-cover"
            :aria-label="
              (selecting ? (selected.includes(item.key) ? '取消选择 ' : '选择 ') : '预览 ') +
              (item.title || item.filename)
            "
            :aria-pressed="selecting ? selected.includes(item.key) : undefined"
            :disabled="!selecting && item.available === false"
            @click="activate(item)"
          >
            <MediaThumbnail
              :src="
                endpoint(item.thumbnail_endpoint) ||
                (item.media_type === 'image' ? endpoint(item.preview_endpoint) : '')
              "
              :width="item.width"
              :height="item.height"
              :priority="priorityItems.includes(item)"
              :type="item.media_type"
              natural
            />
            <span
              v-if="item.available === false"
              class="media-unavailable"
            >
              文件不可用
            </span>
            <span
              v-if="selecting"
              class="library-check"
              aria-hidden="true"
            >
              <Check
                v-if="selected.includes(item.key)"
                :size="15"
              />
            </span>
          </button>
          <div class="library-caption">
            <div class="library-file-meta">
              <span class="library-media-kind">
                <Play
                  v-if="isVideoMedia(item.media_type)"
                  :size="13"
                  aria-hidden="true"
                />
                <Image
                  v-else
                  :size="13"
                  aria-hidden="true"
                />
                {{ kindLabel(item) }}
              </span>
              <small>
                {{ platformNames[item.platform] || item.platform }}
                <template v-if="item.library_status === 'orphaned'">
                  ·
                  <span class="orphan-badge">无索引</span>
                </template>
              </small>
            </div>
            <button
              class="record-link"
              :title="item.title || item.filename"
              @click="inspect(item)"
            >
              {{ item.title || item.filename }}
            </button>
            <div class="library-author">
              <span>{{ item.author_name || "未提供作者" }}</span>
              <button
                class="icon-btn"
                :aria-label="'查看 ' + (item.title || item.filename) + ' 的详情'"
                @click="inspect(item)"
              >
                <ArrowUpRight :size="17" />
              </button>
            </div>
            <small>{{ bytes(item.file_size) }}</small>
          </div>
        </article>
      </template>
    </div>
    <Pager
      v-if="loaded"
      :page="page"
      :pages="pages"
      :total="total"
      @change="changePage"
    />
    <InspectorPanel
      :open="detailOpen"
      :title="inspected?.title || inspected?.filename || '媒体详情'"
      :subtitle="inspected ? platformNames[inspected.platform] : ''"
      @close="closeDetail"
    >
      <template v-if="inspected">
        <button
          class="inspector-media-preview"
          :disabled="inspected.available === false"
          aria-label="预览当前媒体"
          @click="preview(inspected)"
        >
          <MediaThumbnail
            :src="
              endpoint(inspected.thumbnail_endpoint) ||
              (inspected.media_type === 'image' ? endpoint(inspected.preview_endpoint) : '')
            "
            :type="inspected.media_type"
          />
        </button>
        <dl class="detail-fields">
          <div>
            <dt>作者</dt>
            <dd>{{ inspected.author_name || "未提供" }}</dd>
          </div>
          <div>
            <dt>文件</dt>
            <dd>{{ inspected.filename }}</dd>
          </div>
          <div>
            <dt>类型</dt>
            <dd>
              {{ kindLabel(inspected) }}
              <template v-if="inspected.width && inspected.height">
                · {{ inspected.width }} × {{ inspected.height }}
              </template>
            </dd>
          </div>
          <div>
            <dt>大小</dt>
            <dd>{{ bytes(inspected.file_size) }}</dd>
          </div>
          <div>
            <dt>入库时间</dt>
            <dd>{{ dateTime(inspected.completed_at || inspected.created_at) }}</dd>
          </div>
          <div v-if="inspected.published_at">
            <dt>发布时间</dt>
            <dd>{{ dateTime(inspected.published_at) }}</dd>
          </div>
          <div>
            <dt>分类</dt>
            <dd>
              {{
                inspected.library_status === "orphaned"
                  ? "无索引媒体：原任务记录已删除，文件仍由媒体库管理"
                  : "已索引"
              }}
            </dd>
          </div>
          <div>
            <dt>可用状态</dt>
            <dd>
              {{
                inspected.available === false
                  ? inspected.library_status === "orphaned"
                    ? "文件已不在下载目录中，可删除这条登记"
                    : "文件不可用，请核对关联任务"
                  : "已入库"
              }}
            </dd>
          </div>
        </dl>
        <div class="detail-links">
          <RouterLink
            v-if="inspected.task_key"
            class="btn ghost"
            :to="{ path: '/operations/tasks', query: { task_key: inspected.task_key } }"
          >
            查看关联任务
          </RouterLink>
          <RouterLink
            v-if="inspected.author_id && ['douyin', 'x', 'instagram'].includes(inspected.platform)"
            class="btn ghost"
            :to="{
              path: '/authors/' + inspected.platform,
              query: { author: String(inspected.author_id) },
            }"
          >
            查看作者订阅
          </RouterLink>
          <RouterLink
            v-if="inspected.work_id && inspected.author_id && inspected.platform === 'douyin'"
            class="btn ghost"
            :to="{
              path: '/authors/douyin/' + inspected.author_id + '/works',
              query: { work: String(inspected.work_id) },
            }"
          >
            管理作品与文件
          </RouterLink>
          <button
            v-if="inspected.author_name"
            class="btn ghost"
            @click="
              platform = inspected.platform;
              author = inspected.author_name;
              authorId = inspected.author_id ? String(inspected.author_id) : '';
              filters(false);
            "
          >
            查看该作者的媒体
          </button>
        </div>
      </template>
      <StateView
        v-else
        :loading="loading"
        :error="error"
        title="当前页面没有这份媒体"
        description="关闭详情后调整筛选或返回原来的分页。"
        @retry="load"
      />
      <template
        v-if="inspected"
        #footer
      >
        <button
          class="btn primary"
          :disabled="inspected.available === false"
          @click="preview(inspected)"
        >
          <Play :size="16" />
          预览
        </button>
        <a
          v-if="inspected.download_endpoint && inspected.available !== false"
          class="btn"
          :href="endpoint(inspected.download_endpoint)"
          download
        >
          <Download :size="16" />
          下载文件
        </a>
        <button
          class="btn danger"
          :disabled="deleting"
          @click="removeMedia(inspected)"
        >
          <Trash2 :size="16" />
          {{
            inspected.library_status === "orphaned" && inspected.available === false
              ? "删除登记"
              : "删除文件"
          }}
        </button>
      </template>
    </InspectorPanel>
  </section>
</template>
