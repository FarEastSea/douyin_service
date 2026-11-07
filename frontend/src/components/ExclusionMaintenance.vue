<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { RotateCcw, Search } from "@lucide/vue";
import { api, jsonBody } from "../api";
import { useAppStore } from "../stores/app";
import { confirmAction, dateTime, platformNames } from "../workspace";
import StateView from "./StateView.vue";
import Pager from "./Pager.vue";

interface ExclusionItem {
  key: string;
  platform: string;
  kind: "work" | "file" | "media";
  title?: string | null;
  author_name?: string | null;
  detail?: string | null;
  excluded_at?: string | null;
  restorable: boolean;
}
interface RestoreResult {
  succeeded: { key: string; message: string }[];
  failed: { key: string; message: string }[];
}

const store = useAppStore();
const items = ref<ExclusionItem[]>([]), total = ref(0), page = ref(1), pages = ref(1);
const platform = ref(""), search = ref(""), loading = ref(false), loaded = ref(false), error = ref("");
const selected = ref<string[]>([]), busy = ref(false), failures = ref<RestoreResult["failed"]>([]);
const failureLabels = ref<Record<string, string>>({});
const restorableKeys = computed(() => items.value.filter((item) => item.restorable).map((item) => item.key));
const kindLabels: Record<ExclusionItem["kind"], string> = { work: "整个作品", file: "单张图片", media: "单个媒体" };
let searchTimer: number | undefined;

async function load() {
  loading.value = true;
  const params = new URLSearchParams({ page: String(page.value), page_size: "50" });
  if (platform.value) params.set("platform", platform.value);
  if (search.value.trim()) params.set("q", search.value.trim());
  try {
    const data = await api<{ items: ExclusionItem[]; total: number; page: number; pages: number }>(`/operations/exclusions?${params}`);
    items.value = data.items; total.value = data.total; pages.value = data.pages; page.value = data.page;
    selected.value = selected.value.filter((key) => data.items.some((item) => item.key === key));
    loaded.value = true; error.value = "";
  } catch (reason: any) {
    error.value = reason.message || "排除项目读取失败";
  } finally {
    loading.value = false;
  }
}
function filters() { page.value = 1; selected.value = []; void load(); }
function queueSearch() { window.clearTimeout(searchTimer); searchTimer = window.setTimeout(filters, 300); }
function changePage(value: number) { page.value = value; selected.value = []; void load(); }
async function restore(keys: string[]) {
  if (!keys.length || busy.value) return;
  const ok = await confirmAction(
    `恢复 ${keys.length} 个排除项目？\n抖音会立即重新下载；Instagram 会立即排入下载；其他平台在作者处于订阅状态时，于下一次订阅检查下载。
恢复失败的项目会保留在列表中。`,
    "恢复下载",
    "恢复下载",
    false,
  );
  if (!ok) return;
  busy.value = true;
  try {
    const result = await api<{ message?: string; data?: RestoreResult }>("/operations/exclusions/restore", {
      method: "POST",
      ...jsonBody({ keys }),
    });
    failureLabels.value = Object.fromEntries(keys.map((key) => [key, label(key)]));
    failures.value = result.data?.failed || [];
    store.notify(result.message || "已恢复", failures.value.length ? "info" : "success");
    selected.value = [];
    await load();
  } catch (reason: any) {
    store.notify(reason.message || "恢复失败", "error");
  } finally {
    busy.value = false;
  }
}
function label(key: string) {
  const item = items.value.find((entry) => entry.key === key);
  return item ? item.title || item.detail || key : key;
}
onMounted(() => { void load(); window.addEventListener("app:exclusions-refresh", load); });
onBeforeUnmount(() => { window.clearTimeout(searchTimer); window.removeEventListener("app:exclusions-refresh", load); });
</script>

<template>
  <section class="maintenance-page">
    <section class="setting-section">
      <header>
        <div>
          <h2>排除列表</h2>
          <p>在媒体库或作品页删除时选择“不再下载”的项目。恢复后会重新下载，原文件不会自动找回。</p>
        </div>
      </header>
      <div class="exclusion-toolbar">
        <label class="search"><Search :size="16" /><input v-model="search" aria-label="搜索排除项目" placeholder="搜索标题、作者或文件名" @input="queueSearch" /></label>
        <select v-model="platform" aria-label="筛选平台" @change="filters">
          <option value="">全部平台</option>
          <option v-for="(label, key) in platformNames" :key="key" :value="key">{{ label }}</option>
        </select>
        <button class="btn primary" :disabled="busy || !selected.length" @click="restore(selected)"><RotateCcw :size="15" />恢复所选 {{ selected.length || "" }}</button>
      </div>
      <div v-if="failures.length" class="load-error-banner" role="alert">
        <span>{{ failures.length }} 项未恢复：{{ failures.map((item) => `${failureLabels[item.key] || item.key}（${item.message}）`).join("；") }}</span>
        <button class="text-button" @click="failures = []">知道了</button>
      </div>
      <StateView v-if="!items.length" :loading="loading" :error="error" :title="search || platform ? '没有匹配的排除项目' : '没有排除项目'" description="删除媒体时勾选“阻止订阅重新下载”的项目会出现在这里。" @retry="load" />
      <div v-else class="table-shell">
        <table class="data-table exclusion-table">
          <thead>
            <tr>
              <th class="select-col"><input type="checkbox" aria-label="选择本页全部可恢复项目" :checked="restorableKeys.length > 0 && restorableKeys.every((key) => selected.includes(key))" @change="selected = ($event.target as HTMLInputElement).checked ? [...restorableKeys] : []" /></th>
              <th>项目</th><th>平台</th><th>范围</th><th>排除时间</th><th class="actions-col">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in items" :key="item.key">
              <td class="select-col"><input v-model="selected" type="checkbox" :value="item.key" :disabled="!item.restorable" :aria-label="'选择 ' + (item.title || item.detail)" /></td>
              <td data-label="项目"><div class="exclusion-title"><strong :title="item.title || ''">{{ item.title || item.detail || "未命名" }}</strong><small>{{ item.author_name || "未提供作者" }}<template v-if="item.title && item.detail"> · {{ item.detail }}</template></small></div></td>
              <td data-label="平台">{{ platformNames[item.platform] || item.platform }}</td>
              <td data-label="范围">{{ kindLabels[item.kind] }}</td>
              <td data-label="排除时间">{{ item.excluded_at ? dateTime(item.excluded_at) : "—" }}</td>
              <td class="actions-col">
                <button v-if="item.restorable" class="text-button" :disabled="busy" @click="restore([item.key])">恢复下载</button>
                <span v-else class="muted exclusion-unavailable" title="删除时未能记录下载归档位置">无法自动恢复</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <Pager v-if="loaded && total" :page="page" :pages="pages" :total="total" @change="changePage" />
    </section>
  </section>
</template>
