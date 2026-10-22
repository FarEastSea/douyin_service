<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from "vue";
import { useRouter } from "vue-router";
import {
  ArrowRight,
  Check,
  ClipboardPaste,
  Download,
  Link2,
  LoaderCircle,
  Users,
  X,
} from "@lucide/vue";
import { modalDepth } from "../workspace";
import { api, jsonBody } from "../api";
import {
  captureFocusOrigin,
  focusFirst,
  restoreFocus,
  trapFocus,
} from "../focus";
import { useAppStore } from "../stores/app";
import type { MediaPlatform } from "../types";
const props = withDefaults(
  defineProps<{
    intent?: "download" | "subscribe";
    platform?: string;
    returnFocus?: HTMLElement | null;
  }>(),
  { intent: "download", platform: "" },
);
const emit = defineEmits<{ close: [] }>();
const router = useRouter(),
  store = useAppStore();
const mode = ref(props.intent),
  input = ref(""),
  manualPlatform = ref(props.platform),
  manualOpen = ref(false);
const field = ref<HTMLTextAreaElement | null>(null),
  panel = ref<HTMLElement | null>(null);
type Detection = {
  platform_id: string;
  input_kind: "author" | "work" | "unknown";
};
const found = ref<Detection | null>(null),
  detecting = ref(false),
  submitting = ref(false),
  pasting = ref(false);
const error = ref(""),
  pasteHint = ref("");
const success = ref<{
  message: string;
  path: string;
  query?: Record<string, string>;
} | null>(null);
let disposed = false;
let sequence = 0,
  timer: number | undefined,
  controller: AbortController | undefined;
let pending: { source: string; promise: Promise<Detection | null> } | null =
  null;
let origin: HTMLElement | null = null;
const viewportHeight = ref(0),
  viewportTop = ref(0);
function updateViewport() {
  viewportHeight.value = window.visualViewport?.height || window.innerHeight;
  viewportTop.value = window.visualViewport?.offsetTop || 0;
}
const options = computed(() =>
  store.platforms.length
    ? store.platforms
    : ([
        { id: "douyin", name: "抖音" },
        { id: "x", name: "X" },
        { id: "tiktok", name: "TikTok" },
        { id: "weibo", name: "微博" },
        { id: "bilibili", name: "B站" },
        { id: "xhs", name: "小红书" },
      ] as MediaPlatform[]),
);
const platform = computed(
  () =>
    found.value?.platform_id ||
    manualPlatform.value ||
    (mode.value === "subscribe" ? "douyin" : ""),
);
const selected = computed(() =>
  options.value.find((item) => item.id === platform.value),
);
const kind = computed(() => found.value?.input_kind || "unknown");
const blocked = computed(() => {
  if (
    mode.value === "subscribe" &&
    platform.value &&
    !["douyin", "x"].includes(platform.value)
  )
    return "目前支持订阅抖音和 X 作者，请粘贴对应的作者主页。";
  if (mode.value === "subscribe" && kind.value === "work")
    return "这是作品链接。订阅请使用作者主页，或切换到下载作品。";
  if (platform.value === "xhs" && kind.value === "author")
    return "小红书目前仅支持单条笔记，请粘贴笔记链接。";
  return "";
});
const submitLabel = computed(() =>
  submitting.value
    ? "正在提交…"
    : mode.value === "subscribe"
      ? "添加并订阅"
      : kind.value === "author"
        ? "下载作者作品"
        : "开始下载",
);
function close() {
  if (!submitting.value) emit("close");
}
function keydown(event: KeyboardEvent) {
  if (event.key === "Escape") {
    event.preventDefault();
    close();
  } else trapFocus(event, panel.value);
}
function invalidate() {
  ++sequence;
  controller?.abort();
  pending = null;
  found.value = null;
  detecting.value = false;
  clearTimeout(timer);
  error.value = "";
  pasteHint.value = "";
}
watch(
  input,
  () => {
    invalidate();
    if (input.value.trim())
      timer = window.setTimeout(() => void identify(input.value.trim()), 250);
  },
  { flush: "sync" },
);
watch(mode, () => {
  error.value = "";
  success.value = null;
});
function identify(source: string): Promise<Detection | null> {
  if (found.value && source === input.value.trim())
    return Promise.resolve(found.value);
  if (pending?.source === source) return pending.promise;
  const current = ++sequence;
  controller?.abort();
  controller = new AbortController();
  detecting.value = true;
  const promise = api<Detection>("/platforms/detect", {
    method: "POST",
    signal: controller.signal,
    ...jsonBody({ value: source }),
  })
    .then((value) => {
      if (current === sequence && input.value.trim() === source)
        found.value = value;
      return current === sequence ? value : null;
    })
    .catch(() => null)
    .finally(() => {
      if (current === sequence) {
        detecting.value = false;
        pending = null;
      }
    });
  pending = { source, promise };
  return promise;
}
async function paste() {
  if (pasting.value || submitting.value) return;
  pasting.value = true;
  pasteHint.value = "";
  try {
    const text = await navigator.clipboard.readText();
    if (text.trim()) input.value = text.slice(0, 4096);
    else pasteHint.value = "剪贴板为空，也可以长按输入框粘贴。";
  } catch {
    pasteHint.value = "浏览器未允许读取剪贴板，请长按输入框粘贴。";
  } finally {
    pasting.value = false;
    await nextTick();
    field.value?.focus();
  }
}
async function submit() {
  if (submitting.value || pasting.value || disposed) return;
  const source = input.value.trim();
  if (!source) {
    field.value?.focus();
    return;
  }
  submitting.value = true;
  error.value = "";
  success.value = null;
  clearTimeout(timer);
  try {
    await identify(source);
    if (disposed || source !== input.value.trim()) return;
    if (blocked.value) {
      error.value = blocked.value;
      return;
    }
    const target = platform.value;
    if (!target) {
      manualOpen.value = true;
      error.value = "未能识别平台，请在下方选择平台后提交。";
      return;
    }
    if (mode.value === "subscribe") {
      const author = await api<any>(
        target === "x" ? "/x/authors/" : "/authors/",
        {
          method: "POST",
          ...jsonBody({
            [target === "x" ? "profile_url" : "share_url"]: source,
            is_subscribed: true,
          }),
        },
      );
      if (!author.id || author.is_subscribed !== true)
        throw new Error("订阅状态尚未确认，请在作者页检查后重试。");
      success.value = {
        message: `已订阅 ${author.nickname || author.display_name || author.username || "作者"}`,
        path: `/authors/${target}`,
        query: { author: String(author.id) },
      };
      window.dispatchEvent(
        new CustomEvent("app:authors-changed", {
          detail: { platform: target },
        }),
      );
    } else if (target === "douyin") {
      const result = await api<any>("/tasks/download", {
        method: "POST",
        ...jsonBody({
          share_url: source,
          allow_author_scan: kind.value === "author",
        }),
      });
      success.value =
        result.url_type === "author" && result.author_id
          ? {
              message: "作者作品扫描已提交，扫描后创建下载任务",
              path: "/authors/douyin",
              query: { author: String(result.author_id) },
            }
          : {
              message: result.task_ids?.length
                ? `已加入下载队列 · ${result.task_ids.length} 个文件`
                : "下载任务已提交",
              path: "/operations/tasks",
              query: {
                platform: target,
                ...(result.task_ids?.length
                  ? { task_key: `douyin:${result.task_ids[0]}` }
                  : {}),
              },
            };
      window.dispatchEvent(new CustomEvent("app:tasks-changed"));
    } else {
      const result = await api<any>(
        target === "x"
          ? "/x/download"
          : `/platform-downloads/${target}/download`,
        {
          method: "POST",
          ...jsonBody(target === "x" ? { profile_url: source } : { source }),
        },
      );
      success.value = {
        message: "下载任务已提交",
        path: "/operations/tasks",
        query: {
          platform: target,
          ...(result.id ? { task_key: `${target}:${result.id}` } : {}),
        },
      };
      window.dispatchEvent(new CustomEvent("app:tasks-changed"));
    }
    input.value = "";
    void store.refreshStats();
  } catch (reason: any) {
    error.value = reason.message || "提交失败，请稍后重试。";
    if (reason.code === "author_link_requires_confirmation") {
      found.value = { platform_id: "douyin", input_kind: "author" };
    }
  } finally {
    submitting.value = false;
    await nextTick();
    field.value?.focus();
  }
}
function viewResult() {
  if (success.value) {
    emit("close");
    void router.push({ path: success.value.path, query: success.value.query });
  }
}
function submitFromKeyboard(event: KeyboardEvent) {
  if (event.isComposing) return;
  event.preventDefault();
  void submit();
}
onMounted(() => {
  origin = props.returnFocus || captureFocusOrigin();
  modalDepth.value++;
  document.addEventListener("keydown", keydown);
  updateViewport();
  window.visualViewport?.addEventListener("resize", updateViewport);
  window.visualViewport?.addEventListener("scroll", updateViewport);
  void nextTick(() => focusFirst(panel.value, field.value));
});
onBeforeUnmount(() => {
  disposed = true;
  window.visualViewport?.removeEventListener("resize", updateViewport);
  window.visualViewport?.removeEventListener("scroll", updateViewport);
  invalidate();
  modalDepth.value--;
  document.removeEventListener("keydown", keydown);
  void nextTick(() => restoreFocus(origin));
});
</script>

<template>
  <div
    class="download-backdrop"
    :style="
      viewportHeight
        ? {
            '--capture-height': viewportHeight + 'px',
            '--capture-top': viewportTop + 'px',
          }
        : undefined
    "
    @click.self="close"
  >
    <form
      ref="panel"
      class="download-panel quick-capture"
      role="dialog"
      aria-modal="true"
      aria-labelledby="new-download-title"
      @submit.prevent="submit"
    >
      <header>
        <div>
          <h2 id="new-download-title">
            {{ mode === "subscribe" ? "订阅作者" : "下载作品" }}
          </h2>
          <p>粘贴分享内容，一次提交。</p>
        </div>
        <button
          type="button"
          class="icon-btn"
          aria-label="关闭添加面板"
          :disabled="submitting"
          @click="close"
        >
          <X :size="19" />
        </button>
      </header>
      <div class="capture-tabs" aria-label="添加内容类型">
        <button
          type="button"
          :aria-pressed="mode === 'download'"
          :disabled="submitting"
          @click="mode = 'download'"
        >
          <Download :size="17" />下载作品
        </button>
        <button
          type="button"
          :aria-pressed="mode === 'subscribe'"
          :disabled="submitting"
          @click="mode = 'subscribe'"
        >
          <Users :size="17" />订阅作者
        </button>
      </div>
      <div class="download-panel-body">
        <div class="capture-input-label">
          <label for="download-source">{{
            mode === "subscribe" ? "作者主页链接" : "作品链接或分享内容"
          }}</label
          ><button
            type="button"
            class="text-button"
            :disabled="submitting || pasting"
            @click="paste"
          >
            <ClipboardPaste :size="15" />{{ pasting ? "正在读取…" : "粘贴" }}
          </button>
        </div>
        <div class="source-input">
          <Link2 :size="18" /><textarea
            id="download-source"
            ref="field"
            v-model="input"
            :disabled="submitting"
            rows="3"
            maxlength="4096"
            inputmode="url"
            enterkeyhint="go"
            autocomplete="off"
            autocapitalize="off"
            :spellcheck="false"
            :aria-invalid="Boolean(error || blocked)"
            :aria-describedby="
              error || blocked ? 'capture-error' : 'capture-help'
            "
            :placeholder="
              mode === 'subscribe'
                ? '粘贴作者分享链接，也支持 X 用户名'
                : '直接粘贴复制的整段分享内容'
            "
            @keydown.enter.exact="submitFromKeyboard"
          />
        </div>
        <p id="capture-help" class="field-help">
          {{
            mode === "subscribe"
              ? "添加后直接开启自动更新，无需再去列表打开订阅。"
              : "视频、单图、实况、图集自动识别；图集会创建对应文件任务。"
          }}
        </p>
        <p v-if="pasteHint" class="field-help" role="status">{{ pasteHint }}</p>
        <div v-if="input.trim()" class="capture-detection" aria-live="polite">
          <LoaderCircle v-if="detecting" :size="15" class="spinning" /><Check
            v-else-if="found"
            :size="15"
          /><span>{{
            detecting
              ? "正在识别链接…"
              : selected
                ? `${selected.name}${kind === "author" ? " · 作者主页" : kind === "work" ? " · 单条作品" : ""}`
                : "选择平台后即可提交"
          }}</span
          ><button
            v-if="!found"
            type="button"
            class="text-button"
            :disabled="submitting"
            @click="manualOpen = !manualOpen"
          >
            选择平台
          </button>
        </div>
        <div v-if="manualOpen && !found" class="download-platform-choice">
          <label for="download-platform">平台</label
          ><select
            id="download-platform"
            v-model="manualPlatform"
            :disabled="submitting"
          >
            <option value="">自动识别</option>
            <option
              v-for="item in options.filter(
                (item) =>
                  mode !== 'subscribe' || ['douyin', 'x'].includes(item.id),
              )"
              :key="item.id"
              :value="item.id"
            >
              {{ item.name }}
            </option>
          </select>
        </div>
        <p
          v-if="mode === 'download' && kind === 'author' && !blocked"
          class="capture-profile-note"
        >
          这是作者主页，提交会扫描该作者作品并创建下载任务。<button
            type="button"
            class="text-button"
            :disabled="submitting"
            @click="mode = 'subscribe'"
          >
            只订阅此作者
          </button>
        </p>
        <p
          v-if="error || blocked"
          id="capture-error"
          class="form-error"
          role="alert"
        >
          {{ error || blocked }}
        </p>
        <div v-if="success" class="capture-success" role="status">
          <Check :size="18" />
          <div>
            <strong>{{ success.message }}</strong
            ><span>可继续粘贴下一条链接</span>
          </div>
          <button type="button" class="text-button" @click="viewResult">
            查看<ArrowRight :size="14" />
          </button>
        </div>
      </div>
      <footer>
        <button
          type="button"
          class="btn ghost"
          :disabled="submitting"
          @click="close"
        >
          关闭</button
        ><button
          class="btn primary"
          :disabled="!input.trim() || submitting || pasting || Boolean(blocked)"
        >
          <LoaderCircle v-if="submitting" :size="17" class="spinning" /><span>{{
            submitLabel
          }}</span
          ><ArrowRight v-if="!submitting" :size="17" />
        </button>
      </footer>
    </form>
  </div>
</template>
