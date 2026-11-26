import { computed, ref, onBeforeUnmount } from "vue";
import { api, jsonBody } from "../api";
import { useAppStore } from "../stores/app";
import { confirmAction } from "../workspace";

export function useStorageMaintenance() {
  const store = useAppStore();
  let disposed = false;
  let storageAuditTimer: number | undefined;
  const storageAudit = ref<any>(null);
  const storageAuditState = ref<any>({ status: "idle", progress: {} });
  const lastStorageRepair = ref<any>(null);
  const storageJournals = ref<any[]>([]);
  const storageRestorePreview = ref<any>(null);
  const storageRestoreBusy = ref(false);
  const storageRepairAllState = ref<any>({ status: "idle", progress: {} });
  const storageAuditBusy = ref(false);
  const storageRepairBusy = ref(false);
  const storageRepairAllBusy = ref(false);
  const storageRepairPlan = ref<any>(null);
  const storageRepairTargets = computed(() => {
    const report = storageAudit.value;
    if (!report) return [];
    return [
      ...(report.relinkable_records || []).map((item: any) => ({
        issue_type: "stale_record_path",
        record_kind: item.kind,
        record_id: item.id,
        path: item.path,
      })),
      ...(report.missing_records || []).map((item: any) => ({
        issue_type: "missing_record",
        record_kind: item.kind,
        record_id: item.id,
        path: item.path,
      })),
      ...(report.zero_byte_files || []).map((item: any) => ({
        issue_type: "zero_byte_file",
        record_kind: item.kind,
        record_id: item.id,
        path: item.path,
      })),
      ...(report.partial_files || []).map((item: any) => ({
        issue_type: "partial_file",
        path: item.path,
      })),
      ...(report.orphan_files || []).map((path: string) => ({
        issue_type: "orphan_file",
        path,
      })),
    ].slice(0, 200);
  });
  const eligibleRepairTargets = computed(() =>
    (storageRepairPlan.value?.items || [])
      .filter((item: any) => item.eligible)
      .map((item: any) => ({
        issue_type: item.issue_type,
        record_kind: item.record_kind,
        record_id: item.record_id,
        path: item.path,
      })),
  );
  const storageIssueCount = computed(() => {
    const counts = storageAudit.value?.issue_counts;
    if (!counts) return storageRepairTargets.value.length;
    return Object.values(counts).reduce(
      (total: number, value: any) => total + Number(value || 0),
      0,
    );
  });
  function stopStorageAuditPolling() {
    if (storageAuditTimer != null) window.clearInterval(storageAuditTimer);
    storageAuditTimer = undefined;
  }
  function startStorageAuditPolling() {
    if (!disposed && storageAuditTimer == null)
      storageAuditTimer = window.setInterval(() => refreshStorageAudit(true), 2000);
  }
  async function refreshStorageAudit(silent = false) {
    const previousStatus = storageAuditState.value?.status;
    const previousRepairAllStatus = storageRepairAllState.value?.status;
    try {
      const state = await api<any>("/operations/storage-audit");
      storageAuditState.value = state;
      lastStorageRepair.value = state.last_repair || null;
      storageRepairAllState.value = state.repair_all || {
        status: "idle",
        progress: {},
      };
      storageAuditBusy.value = ["queued", "running"].includes(state.status);
      storageRepairAllBusy.value = ["queued", "running"].includes(
        storageRepairAllState.value.status,
      );
      if (state.result) storageAudit.value = state.result;
      if (storageAuditBusy.value || storageRepairAllBusy.value) startStorageAuditPolling();
      else stopStorageAuditPolling();
      if (
        state.status === "completed" &&
        previousStatus &&
        !["completed", "idle"].includes(previousStatus)
      )
        store.notify("存储巡检完成");
      if (state.status === "failed" && previousStatus !== "failed" && !silent)
        store.notify(state.error || "存储巡检失败", "error");
      if (
        storageRepairAllState.value.status === "completed" &&
        ["queued", "running"].includes(previousRepairAllStatus)
      ) {
        store.notify(
          "存储全部维护完成：已处理 " + (storageRepairAllState.value.result?.applied || 0) + " 项",
        );
      }
      if (
        ["completed", "partial"].includes(storageRepairAllState.value.status) &&
        ["queued", "running"].includes(previousRepairAllStatus)
      )
        void loadStorageJournals();
      if (storageRepairAllState.value.status === "partial" && previousRepairAllStatus !== "partial")
        store.notify("存储维护部分完成，请查看失败项和复检结果", "error");
      if (
        storageRepairAllState.value.status === "failed" &&
        previousRepairAllStatus !== "failed" &&
        !silent
      ) {
        store.notify(storageRepairAllState.value.error || "存储全部维护失败", "error");
      }
    } catch (error: any) {
      storageAuditBusy.value = false;
      stopStorageAuditPolling();
      if (!silent) store.notify(error.message || "读取存储巡检状态失败", "error");
    }
  }
  async function startStorageAudit() {
    storageAuditBusy.value = true;
    storageRepairPlan.value = null;
    try {
      storageAuditState.value = await api<any>("/operations/storage-audit", {
        method: "POST",
      });
      startStorageAuditPolling();
    } catch (error: any) {
      storageAuditBusy.value = false;
      store.notify(error.message || "提交存储巡检失败", "error");
    }
  }
  async function previewStorageRepair() {
    if (!storageRepairTargets.value.length)
      return store.notify("当前没有需要处理的存储问题", "info");
    storageRepairBusy.value = true;
    try {
      storageRepairPlan.value = await api("/operations/storage-repair", {
        method: "POST",
        ...jsonBody({ dry_run: true, targets: storageRepairTargets.value }),
      });
    } catch (error: any) {
      store.notify(error.message || "生成修复方案失败", "error");
    } finally {
      storageRepairBusy.value = false;
    }
  }
  async function applyStorageRepair() {
    const targets = eligibleRepairTargets.value;
    if (
      !targets.length ||
      !(await confirmAction(
        "确认处理 " + targets.length + " 项？异常文件只移动到可恢复隔离区，不会直接删除。",
      ))
    )
      return;
    storageRepairBusy.value = true;
    try {
      const result = await api<any>("/operations/storage-repair", {
        method: "POST",
        ...jsonBody({ dry_run: false, targets }),
      });
      lastStorageRepair.value = result.repair_state || null;
      store.notify("本批已处理 " + result.applied + " 项，正在重新扫描剩余问题");
      await startStorageAudit();
    } catch (error: any) {
      store.notify(error.message || "执行存储维护失败", "error");
    } finally {
      storageRepairBusy.value = false;
    }
  }
  async function applyAllStorageRepairs() {
    const count = storageIssueCount.value;
    if (!count || !(await confirmAction("确认在后台处理本次扫描发现的全部 " + count + " 项问题？")))
      return;
    storageRepairAllBusy.value = true;
    storageRepairPlan.value = null;
    try {
      storageRepairAllState.value = await api<any>("/operations/storage-repair-all", {
        method: "POST",
      });
      store.notify("存储全部维护已进入后台队列，可以离开页面");
      startStorageAuditPolling();
    } catch (error: any) {
      storageRepairAllBusy.value = false;
      store.notify(error.message || "提交存储全部维护失败", "error");
    }
  }

  async function loadStorageJournals() {
    try {
      storageJournals.value = (await api<any>("/operations/storage-journals")).items || [];
    } catch (error: any) {
      store.notify(error.message || "存储恢复记录读取失败", "error");
    }
  }
  async function restoreStorage(journalId: string, apply = false) {
    if (
      apply &&
      (!storageRestorePreview.value ||
        storageRestorePreview.value.journalId !== journalId ||
        !(await confirmAction(
          "确认恢复预演中允许的隔离文件？不会覆盖已有文件，也不会自动恢复任务完成状态。",
        )))
    )
      return;
    storageRestoreBusy.value = true;
    try {
      const result = await api<any>("/operations/storage-restore", {
        method: "POST",
        ...jsonBody({ journal_id: journalId, dry_run: !apply }),
      });
      storageRestorePreview.value = { ...result, journalId };
      if (apply) {
        store.notify(
          result.status === "partial"
            ? "恢复部分完成，请查看逐项失败原因"
            : "隔离文件恢复完成，请重新巡检并按需重试任务",
          result.status === "partial" ? "error" : "success",
        );
        await loadStorageJournals();
        await startStorageAudit();
      }
    } catch (error: any) {
      store.notify(error.message || "隔离恢复失败，原文件不会被覆盖", "error");
    } finally {
      storageRestoreBusy.value = false;
    }
  }

  onBeforeUnmount(() => {
    disposed = true;
    stopStorageAuditPolling();
  });
  return {
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
    storageRepairTargets,
    eligibleRepairTargets,
    storageIssueCount,
    refreshStorageAudit,
    startStorageAudit,
    previewStorageRepair,
    applyStorageRepair,
    applyAllStorageRepairs,
    loadStorageJournals,
    restoreStorage,
  };
}
