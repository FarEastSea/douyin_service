<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { ArrowRight, Search } from "@lucide/vue";
import { useRouter, type RouteLocationRaw } from "vue-router";
import { api } from "../api";
import { contextActions, modalDepth, platformNames, type WorkspaceAction } from "../workspace";
import {
  focusFirst,
  restoreFocus,
  trapFocus,
  captureFocusOrigin,
} from "../focus";
import type { LibraryMedia, PageData, UnifiedTask } from "../types";

interface PaletteItem extends WorkspaceAction {
  group: string;
  hint?: string;
}
interface NamedAuthor { id: number; nickname?: string; username?: string; display_name?: string }

const SEARCH_DELAY = 280;
const MIN_QUERY = 2;
const emit = defineEmits<{ close: [] }>(),
  router = useRouter();
const query = ref(""),
  active = ref(0),
  panel = ref<HTMLElement | null>(null),
  input = ref<HTMLInputElement | null>(null);
const remote = ref<PaletteItem[]>([]),
  searching = ref(false),
  searchFailed = ref(false);
let origin: HTMLElement | null = null;
let timer: number | undefined,
  controller: AbortController | undefined,
  sequence = 0;
const pages = [
  ["总览", "/dashboard"],
  ["任务", "/operations/tasks"],
  ["媒体库", "/library"],
  ["洞察", "/insights"],
  ["抖音作者", "/authors/douyin"],
  ["X 用户", "/authors/x"],
  ["Instagram 作者", "/authors/instagram"],
  ["订阅运行记录", "/automation"],
  ["系统状态", "/operations/maintenance/services"],
  ["设置", "/settings/application"],
];
function go(to: RouteLocationRaw) {
  return async () => {
    await router.push(to);
  };
}
const commands = computed<PaletteItem[]>(() =>
  [
    {
      id: "new-download",
      label: "新建下载",
      group: "命令",
      run: () => {
        window.dispatchEvent(new CustomEvent("app:new-download"));
      },
    },
    ...contextActions.value.map((item) => ({ ...item, group: "命令" })),
    ...pages.map(([label, path]) => ({
      id: path!,
      label: `前往${label}`,
      group: "页面",
      run: go(path!),
    })),
  ].filter(
    (item) =>
      !query.value ||
      item.label.toLowerCase().includes(query.value.trim().toLowerCase()),
  ),
);
const actions = computed<PaletteItem[]>(() => [...commands.value, ...remote.value]);
const groups = computed(() => {
  const result: Array<{ name: string; items: Array<{ item: PaletteItem; index: number }> }> = [];
  actions.value.forEach((item, index) => {
    const last = result[result.length - 1];
    if (last?.name === item.group) last.items.push({ item, index });
    else result.push({ name: item.group, items: [{ item, index }] });
  });
  return result;
});

async function settled<T>(request: Promise<T>): Promise<T | null> {
  try {
    return await request;
  } catch (reason: unknown) {
    if (!(reason instanceof DOMException && reason.name === "AbortError")) searchFailed.value = true;
    return null;
  }
}
async function search(term: string) {
  const current = ++sequence;
  controller?.abort();
  controller = new AbortController();
  const signal = controller.signal;
  const q = encodeURIComponent(term);
  searching.value = true;
  searchFailed.value = false;
  const [tasks, media, douyin, x] = await Promise.all([
    settled(api<PageData<UnifiedTask>>(`/operations/tasks?q=${q}&page=1&page_size=4`, { signal })),
    settled(api<PageData<LibraryMedia>>(`/operations/media?q=${q}&page=1&page_size=4&with_counts=false`, { signal })),
    settled(api<PageData<NamedAuthor>>(`/authors/?q=${q}&page=1&page_size=4`, { signal })),
    settled(api<PageData<NamedAuthor>>(`/x/authors/?q=${q}&page=1&page_size=3`, { signal })),
  ]);
  if (current !== sequence) return;
  searching.value = false;
  remote.value = [
    ...(tasks?.items || []).map((task) => ({
      id: `task:${task.key}`,
      group: "任务",
      label: task.source_label || `任务 ${task.id}`,
      hint: [platformNames[task.platform] || task.platform, task.author_name].filter(Boolean).join(" · "),
      run: go({ path: "/operations/tasks", query: { task_key: task.key } }),
    })),
    ...(media?.items || []).map((item) => ({
      id: `media:${item.key}`,
      group: "媒体",
      label: item.title || item.filename,
      hint: [platformNames[item.platform] || item.platform, item.author_name].filter(Boolean).join(" · "),
      run: go({ path: "/library", query: { q: term, media: item.key } }),
    })),
    ...(douyin?.items || []).map((author) => ({
      id: `douyin-author:${author.id}`,
      group: "作者",
      label: author.nickname || `作者 ${author.id}`,
      hint: "抖音",
      run: go({ path: "/authors/douyin", query: { author: String(author.id) } }),
    })),
    ...(x?.items || []).map((author) => ({
      id: `x-author:${author.id}`,
      group: "作者",
      label: author.display_name || `@${author.username}`,
      hint: "X",
      run: go({ path: "/authors/x", query: { author: String(author.id) } }),
    })),
  ];
}
watch(query, (value) => {
  active.value = 0;
  window.clearTimeout(timer);
  const term = value.trim();
  if (term.length < MIN_QUERY) {
    sequence++;
    controller?.abort();
    remote.value = [];
    searching.value = false;
    searchFailed.value = false;
    return;
  }
  searching.value = true;
  timer = window.setTimeout(() => void search(term), SEARCH_DELAY);
});
async function choose(item?: WorkspaceAction) {
  if (!item || item.disabled) return;
  emit("close");
  await nextTick();
  await item.run();
}
function keydown(event: KeyboardEvent) {
  if (event.key === "Escape") {
    event.preventDefault();
    emit("close");
  } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    active.value =
      (active.value +
        (event.key === "ArrowDown" ? 1 : -1) +
        actions.value.length) %
      Math.max(1, actions.value.length);
    void nextTick(() =>
      panel.value?.querySelector(".command-results button.active")?.scrollIntoView({ block: "nearest" }),
    );
  } else if (event.key === "Enter") {
    event.preventDefault();
    void choose(actions.value[active.value]);
  } else trapFocus(event, panel.value);
}
onMounted(() => {
  origin = captureFocusOrigin();
  modalDepth.value++;
  document.addEventListener("keydown", keydown);
  void nextTick(() => focusFirst(panel.value, input.value));
});
onBeforeUnmount(() => {
  modalDepth.value--;
  sequence++;
  controller?.abort();
  window.clearTimeout(timer);
  document.removeEventListener("keydown", keydown);
  void nextTick(() => restoreFocus(origin));
});
</script>
<template>
  <Teleport to="body"
    ><div class="command-backdrop" @click.self="emit('close')">
      <section
        ref="panel"
        class="command-palette"
        role="dialog"
        aria-modal="true"
        aria-label="搜索与命令"
      >
        <label class="command-search"
          ><Search :size="18" /><input
            ref="input"
            v-model="query"
            aria-label="搜索任务、媒体、作者、命令和页面"
            placeholder="搜索任务、媒体、作者，或输入命令…"
          /><kbd>Esc</kbd></label
        >
        <div class="command-results" :aria-busy="searching">
          <template v-for="group in groups" :key="group.name">
            <p class="command-group">{{ group.name }}</p>
            <button
              v-for="entry in group.items"
              :key="entry.item.id"
              :class="{ active: active === entry.index, danger: entry.item.danger }"
              :disabled="entry.item.disabled"
              @pointermove="active = entry.index"
              @click="choose(entry.item)"
            >
              <span class="command-label">{{ entry.item.label }}<small v-if="entry.item.hint">{{ entry.item.hint }}</small></span
              ><ArrowRight :size="14" />
            </button>
          </template>
          <p v-if="searching" class="command-empty" role="status">正在搜索…</p>
          <p v-else-if="searchFailed" class="command-empty" role="status">部分内容暂时无法搜索，显示的是已读取到的结果。</p>
          <p v-else-if="!actions.length" class="command-empty">
            {{ query.trim().length < MIN_QUERY ? '没有匹配的命令。输入至少两个字可搜索任务、媒体和作者。' : '没有找到匹配的任务、媒体、作者或命令。' }}
          </p>
        </div>
        <footer>↑ ↓ 选择 · Enter 打开<span>命令、页面与内容</span></footer>
      </section>
    </div></Teleport
  >
</template>
