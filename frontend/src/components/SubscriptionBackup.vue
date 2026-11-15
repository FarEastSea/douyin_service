<script setup lang="ts">
import { computed, ref } from "vue";
import { Download, Upload } from "@lucide/vue";
import { api, jsonBody } from "../api";
import { useAppStore } from "../stores/app";
import { platformNames } from "../workspace";

interface ImportCounts { created: number; existing: number; invalid: number }
interface ImportResult {
  dry_run: boolean;
  douyin: ImportCounts;
  x: ImportCounts;
  unsupported: Record<string, number>;
}
interface BackupFile { format?: unknown; exported_at?: unknown; [key: string]: unknown }

const MAX_FILE_BYTES = 5 * 1024 * 1024;
const store = useAppStore();
const fileInput = ref<HTMLInputElement | null>(null);
const exporting = ref(false), importing = ref(false);
const backup = ref<BackupFile | null>(null), fileName = ref("");
const preview = ref<ImportResult | null>(null);
const restoreSubscriptions = ref(true);
const pending = computed(() => (preview.value ? preview.value.douyin.created + preview.value.x.created : 0));
const unsupported = computed(() =>
  Object.entries(preview.value?.unsupported || {})
    .map(([platform, count]) => `${platformNames[platform] || platform} ${count} 位`)
    .join("、"),
);
const message = (reason: unknown, fallback: string) => (reason instanceof Error ? reason.message : fallback);

async function exportBackup() {
  if (exporting.value) return;
  exporting.value = true;
  try {
    const data = await api<Record<string, unknown>>("/operations/subscriptions/export", { timeoutMs: 60000 });
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `subscriptions-${new Date().toISOString().slice(0, 10)}.json`;
    link.click();
    URL.revokeObjectURL(url);
    const authors = ["douyin", "x", "platform_authors"].reduce(
      (sum, key) => sum + (Array.isArray(data[key]) ? (data[key] as unknown[]).length : 0), 0,
    );
    store.notify(`已导出 ${authors} 位作者的订阅清单`);
  } catch (reason: unknown) {
    store.notify(message(reason, "导出失败"), "error");
  } finally {
    exporting.value = false;
  }
}
function reset() {
  backup.value = null;
  preview.value = null;
  fileName.value = "";
  if (fileInput.value) fileInput.value.value = "";
}
async function submit(dryRun: boolean): Promise<ImportResult | null> {
  const result = await api<{ message?: string; data?: ImportResult }>("/operations/subscriptions/import", {
    method: "POST", timeoutMs: 120000,
    ...jsonBody({ backup: backup.value, restore_subscriptions: restoreSubscriptions.value, dry_run: dryRun }),
  });
  if (!dryRun) store.notify(result.message || "订阅清单已导入");
  return result.data || null;
}
async function chooseFile(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0];
  if (!file || importing.value) return;
  reset();
  if (file.size > MAX_FILE_BYTES) {
    store.notify("备份文件超过 5 MB，请确认选择的是订阅备份", "error");
    return;
  }
  importing.value = true;
  try {
    const parsed: unknown = JSON.parse(await file.text());
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("文件内容不是订阅备份");
    backup.value = parsed as BackupFile;
    fileName.value = file.name;
    preview.value = await submit(true);
  } catch (reason: unknown) {
    reset();
    store.notify(reason instanceof SyntaxError ? "文件不是有效的 JSON" : message(reason, "备份读取失败"), "error");
  } finally {
    importing.value = false;
  }
}
async function applyImport() {
  if (!backup.value || importing.value || !pending.value) return;
  importing.value = true;
  try {
    await submit(false);
    reset();
  } catch (reason: unknown) {
    store.notify(message(reason, "导入失败，未写入任何作者"), "error");
  } finally {
    importing.value = false;
  }
}
</script>

<template>
  <section class="setting-section">
    <header>
      <div>
        <h2>订阅备份</h2>
        <p>导出全部平台的作者清单，用于迁移或重建实例。备份只含作者标识、名称、订阅开关和检查间隔，不含 Cookie 等凭据。</p>
      </div>
    </header>
    <div class="setting-list">
      <div class="setting-row">
        <span class="setting-copy"><strong>导出订阅清单</strong><small>下载一份 JSON 文件，包含抖音、X 与其他平台的作者。</small></span>
        <span class="setting-control inline-control"><button class="btn ghost" type="button" :disabled="exporting" @click="exportBackup"><Download :size="15" />{{ exporting ? "导出中…" : "导出" }}</button></span>
      </div>
      <div class="setting-row">
        <span class="setting-copy"><strong>导入订阅清单</strong><small>补建备份中缺失的抖音与 X 作者，已有作者保持不变；不请求外部平台。Instagram 等平台的订阅依赖各自的会话与断点，需要在对应页面重新添加。</small></span>
        <span class="setting-control inline-control">
          <input ref="fileInput" class="sr-only" type="file" accept="application/json,.json" aria-label="选择订阅备份文件" @change="chooseFile" />
          <button class="btn ghost" type="button" :disabled="importing" @click="fileInput?.click()"><Upload :size="15" />{{ importing && !preview ? "读取中…" : "选择备份文件" }}</button>
        </span>
      </div>
      <div v-if="preview" class="backup-preview" role="status">
        <strong>{{ fileName }} · 预演结果</strong>
        <dl>
          <div><dt>抖音</dt><dd>新增 {{ preview.douyin.created }} · 已存在 {{ preview.douyin.existing }}<template v-if="preview.douyin.invalid"> · 无效 {{ preview.douyin.invalid }}</template></dd></div>
          <div><dt>X</dt><dd>新增 {{ preview.x.created }} · 已存在 {{ preview.x.existing }}<template v-if="preview.x.invalid"> · 无效 {{ preview.x.invalid }}</template></dd></div>
          <div v-if="unsupported"><dt>不导入</dt><dd>{{ unsupported }}</dd></div>
        </dl>
        <label class="backup-option"><input v-model="restoreSubscriptions" type="checkbox" /><span>恢复备份中的订阅开关<small>不勾选时新增的作者一律不订阅，可稍后逐个开启。勾选后，下一轮订阅检查会为这些作者排队下载作品；作者较多时建议分批导入。</small></span></label>
        <div class="backup-actions">
          <button class="btn primary" type="button" :disabled="importing || !pending" @click="applyImport">{{ importing ? "导入中…" : pending ? `导入 ${pending} 位作者` : "没有需要新增的作者" }}</button>
          <button class="btn" type="button" :disabled="importing" @click="reset">取消</button>
        </div>
      </div>
    </div>
  </section>
</template>
