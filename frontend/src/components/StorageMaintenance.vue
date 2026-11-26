<script setup lang="ts">
import type { SettingsController } from "../composables/useSettingsController";
import { HardDrive } from "@lucide/vue";
const props = defineProps<{ controller: SettingsController }>();
const {
  storageAudit,
  storageAuditState,
  lastStorageRepair,
  storageJournals,
  storageRestorePreview,
  storageRestoreBusy,
  storageRepairAllState,
  storageAuditBusy,
  storageRepairBusy,
  storageRepairAllBusy,
  storageRepairPlan,
  diagnostic,
  storageRepairTargets,
  eligibleRepairTargets,
  storageIssueCount,
  displayDateTime,
  startStorageAudit,
  previewStorageRepair,
  applyStorageRepair,
  applyAllStorageRepairs,
  loadStorageJournals,
  restoreStorage,
} = props.controller;
</script>
<template>
  <section class="maintenance-page">
    <section class="setting-section">
      <header>
        <div>
          <h2>巡检状态</h2>
          <p v-if="storageRepairAllBusy">
            后台全部维护进行中，已处理
            {{ storageRepairAllState.progress?.applied || 0 }} 项。
          </p>
          <p v-else-if="storageAuditBusy">
            正在扫描
            {{ storageAuditState.progress?.scanned_records || 0 }} 条记录、{{
              storageAuditState.progress?.scanned_files || 0
            }}
            个文件；离开页面后仍会继续。
          </p>
          <p v-else-if="storageAudit">
            已扫描 {{ storageAudit.scanned_records }} 条记录、{{
              storageAudit.scanned_files
            }}
            个文件 · {{ displayDateTime(storageAudit.checked_at) }}
          </p>
          <p v-else>尚未扫描；任务会在后台执行，刷新页面不会丢失。</p>
        </div>
        <button
          class="btn primary"
          type="button"
          :disabled="storageAuditBusy || storageRepairAllBusy"
          @click="startStorageAudit"
        >
          <HardDrive :size="15" />
          {{ storageAuditBusy ? "扫描中…" : "扫描存储" }}
        </button>
      </header>
      <div
        v-if="storageAudit"
        class="storage-metrics"
      >
        <div>
          <b>{{ storageAudit.disk.used_percent }}%</b>
          <span>磁盘已用</span>
        </div>
        <div>
          <b>
            {{
              storageAudit.issue_counts?.relinkable_records ??
              storageAudit.relinkable_records?.length ??
              0
            }}
          </b>
          <span>可修复旧路径</span>
        </div>
        <div>
          <b>
            {{ storageAudit.issue_counts?.missing_records ?? storageAudit.missing_records.length }}
          </b>
          <span>记录缺文件</span>
        </div>
        <div>
          <b>{{ storageAudit.issue_counts?.partial_files ?? storageAudit.partial_files.length }}</b>
          <span>陈旧临时文件</span>
        </div>
        <div>
          <b>
            {{ storageAudit.issue_counts?.zero_byte_files ?? storageAudit.zero_byte_files.length }}
          </b>
          <span>空文件</span>
        </div>
        <div>
          <b>{{ storageAudit.issue_counts?.orphan_files ?? storageAudit.orphan_files.length }}</b>
          <span>未登记媒体</span>
        </div>
      </div>
      <div
        v-if="storageAudit"
        class="storage-actions"
      >
        <button
          class="btn ghost"
          type="button"
          :disabled="storageRepairBusy || storageRepairAllBusy || !storageRepairTargets.length"
          @click="previewStorageRepair"
        >
          {{ storageRepairBusy ? "核验中…" : "预演当前批次" }}
        </button>
        <button
          v-if="storageRepairPlan"
          class="btn ghost"
          type="button"
          :disabled="storageRepairBusy || storageRepairAllBusy || !eligibleRepairTargets.length"
          @click="applyStorageRepair"
        >
          处理当前批次 {{ eligibleRepairTargets.length }} 项
        </button>
        <button
          class="btn"
          type="button"
          :disabled="storageRepairBusy || storageRepairAllBusy || !storageIssueCount"
          @click="applyAllStorageRepairs"
        >
          后台处理全部 {{ storageIssueCount }} 项
        </button>
      </div>
      <p
        v-if="lastStorageRepair"
        class="maintenance-note"
      >
        上次处理：{{ displayDateTime(lastStorageRepair.applied_at) }} · 成功
        {{ lastStorageRepair.applied }} 项 · 跳过 {{ lastStorageRepair.skipped || 0 }} 项
      </p>
      <p
        v-if="lastStorageRepair?.status === 'partial'"
        class="config-alert"
        role="status"
      >
        维护部分完成：{{ lastStorageRepair.errors || 0 }} 项处理失败。{{
          lastStorageRepair.verification_error
            ? "复检失败：" + lastStorageRepair.verification_error
            : "请查看剩余原因和维护清单。"
        }}
      </p>
      <details
        v-if="
          lastStorageRepair?.failure_details?.length ||
          lastStorageRepair?.apply_errors?.length ||
          lastStorageRepair?.skipped_reasons
        "
        class="diagnostic-box"
      >
        <summary>处理失败与剩余原因</summary>
        <pre>{{
          JSON.stringify(
            {
              failures: lastStorageRepair.failure_details || lastStorageRepair.apply_errors,
              skipped: lastStorageRepair.skipped_reasons,
              remaining: lastStorageRepair.remaining_counts,
            },
            null,
            2,
          )
        }}</pre>
      </details>
      <section class="setting-section">
        <header>
          <div>
            <h2>可恢复维护清单</h2>
            <p>逐批记录隔离位置与结果；恢复前必须预演，不覆盖已有文件。</p>
          </div>
          <button
            class="btn ghost compact"
            @click="loadStorageJournals"
          >
            刷新清单
          </button>
        </header>
        <details
          v-for="journal in storageJournals"
          :key="journal.id"
          class="journal-row"
        >
          <summary>
            {{ journal.id }} ·
            {{ journal.read_error ? "清单不可读取" : journal.state || "历史维护记录（旧版）" }}
            · {{ journal.moved?.length || 0 }} 个隔离项
          </summary>
          <div class="journal-body">
            <div>
              <p>
                {{
                  journal.path || `${journal.root}/.quarantine/storage-maintenance/${journal.id}`
                }}
              </p>
              <p
                v-if="journal.read_error"
                role="alert"
              >
                {{ journal.message }}（{{ journal.read_error }}）
              </p>
              <details class="diagnostic-box">
                <summary>查看逐项结果</summary>
                <pre>{{
                  JSON.stringify(
                    {
                      read_error: journal.read_error,
                      message: journal.message,
                      plan: journal.plan,
                      moved: journal.moved,
                      errors: journal.apply_errors,
                      restored: journal.restore_result,
                    },
                    null,
                    2,
                  )
                }}</pre>
              </details>
            </div>
            <button
              class="btn ghost compact"
              :disabled="storageRestoreBusy || !journal.moved?.length"
              @click="restoreStorage(journal.id)"
            >
              预演恢复
            </button>
          </div>
        </details>
        <div
          v-if="storageRestorePreview"
          class="maintenance-note"
        >
          <strong>
            允许恢复
            {{ storageRestorePreview.items.filter((item: any) => item.eligible).length }}
            / {{ storageRestorePreview.items.length }} 项
          </strong>
          <pre>{{ JSON.stringify(storageRestorePreview.items, null, 2) }}</pre>
          <button
            class="btn ghost"
            :disabled="
              storageRestoreBusy ||
              storageRestorePreview.dry_run === false ||
              !storageRestorePreview.items.some((item: any) => item.eligible)
            "
            @click="restoreStorage(storageRestorePreview.journalId, true)"
          >
            确认恢复
          </button>
        </div>
        <p v-if="!storageJournals.length">暂无维护清单。</p>
      </section>
      <details
        v-if="storageAudit && (storageRepairTargets.length || storageAudit.orphan_files?.length)"
        class="diagnostic-box"
      >
        <summary>查看问题样本</summary>
        <pre>{{
          JSON.stringify(
            {
              relinkable_records: storageAudit.relinkable_records,
              missing_records: storageAudit.missing_records,
              zero_byte_files: storageAudit.zero_byte_files,
              partial_files: storageAudit.partial_files,
              orphan_files: storageAudit.orphan_files,
            },
            null,
            2,
          )
        }}</pre>
      </details>
      <div
        v-if="storageRepairPlan"
        class="maintenance-note"
      >
        <strong>
          预演结果：{{ storageRepairPlan.eligible }}/{{ storageRepairPlan.planned }} 项可处理。
        </strong>
        未登记的媒体会归入媒体库的“无索引媒体”，文件保持原位；临时文件只移动到可恢复隔离区，真正缺失的媒体任务会标记为失败以便重试。
      </div>
    </section>
  </section>
</template>
