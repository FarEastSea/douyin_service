import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from "vue";

import { onBeforeRouteLeave, onBeforeRouteUpdate, useRouter } from "vue-router";
import { api, jsonBody, saveToken } from "../api";

import { useAppStore } from "../stores/app";

import {
  assignedEnvKeys,
  maintenanceNavigation,
  maintenancePages,
  platformCredentials,
  sectionPath,
  settingsNavigation,
  settingsPages,
  type SettingsFieldSection,
  type SettingsMode,
} from "../settings-registry";
import { confirmAction } from "../workspace";
export function useSettingsController(props: {
  mode: SettingsMode;
  section: string;
}) {
  const router = useRouter();
  const store = useAppStore();
  type ResourceKey =
    | "all"
    | "runtime"
    | "archive"
    | "douyinAccount"
    | "platformCredentials"
    | "logs";
  type ResolvedField = {
    source: "env" | "runtime";
    key: string;
    label: string;
    help: string;
    secret: boolean;
    required: boolean;
    kind: "text" | "number" | "boolean";
    min?: number;
    max?: number;
  };
  const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value ?? null));
  const same = (left: unknown, right: unknown) =>
    JSON.stringify(left) === JSON.stringify(right);

  const runtime = ref<Record<string, any>>({});
  const limits = ref<Record<string, any>>({});
  const allFields = ref<any[]>([]);
  const allValues = ref<Record<string, any>>({});
  const runtimeEnvKeys = ref<string[]>([]);
  const secretValues = ref<Record<string, string>>({});
  const baselineRuntime = ref<Record<string, any>>({});
  const baselineAll = ref<Record<string, any>>({});
  const archiveRules = ref<any>({
    directory_template: "{author}",
    filename_template: "{title}_{aweme_id}{index_suffix}.{ext}",
    work_types: ["video", "images"],
    published_from: null,
    published_to: null,
    min_file_size_mb: 0,
    max_file_size_mb: 0,
    metadata_formats: [],
  });
  const baselineArchive = ref<any>({});
  const douyinAccount = ref<any>({ user_agent: "", proxy_enabled: false });
  const baselineDouyinAccount = ref<any>({});
  const douyinCookieValue = ref("");
  const douyinProxyValue = ref("");
  const xCookieValue = ref("");
  const platformCookieValues = ref<Record<string, string>>({});
  const platformCredentialStatus = ref<Record<string, any>>({});
  const resourceState = ref<
    Record<ResourceKey, { loaded: boolean; error: string }>
  >({
    all: { loaded: false, error: "" },
    runtime: { loaded: false, error: "" },
    archive: { loaded: false, error: "" },
    douyinAccount: { loaded: false, error: "" },
    platformCredentials: { loaded: false, error: "" },
    logs: { loaded: false, error: "" },
  });
  const saving = ref(false);
  const pageLoading = ref(false);
  const saveError = ref("");
  const configurationFeedback = ref("");
  const queueChange = ref<any>({ state: "applied", pending: false });
  let queueChangeTimer: number | undefined;
  async function refreshQueueChange() {
    try {
      queueChange.value = await api("/config/queue-change");
    } catch {
      /* 其他配置仍可使用 */
    }
  }
  async function cancelQueueChange() {
    if (!(await confirmAction("撤回待生效的后台连接变更，继续使用当前配置？")))
      return;
    try {
      await api("/config/queue-change/cancel", { method: "POST" });
      await refreshQueueChange();
    } catch (error: any) {
      store.notify(error.message || "撤回失败", "error");
    }
  }

  const logs = ref<any[]>([]);
  const logLevels = ref(["info", "warning", "error"]);
  const live = ref(true);
  const process = ref<any>(null);
  const readiness = ref<any>({ components: {} });
  const platformReadiness = ref<any>({ items: [] });
  const storageAudit = ref<any>(null);
  const storageAuditState = ref<any>({ status: "idle", progress: {} });
  const lastStorageRepair = ref<any>(null);
  const storageJournals = ref<any[]>([]);
  const storageRestorePreview = ref<any>(null);
  const storageRestoreBusy = ref(false);
  const storageRepairAllState = ref<any>({ status: "idle", progress: {} });
  const platformAuditBusy = ref(false);
  const storageAuditBusy = ref(false);
  const storageRepairBusy = ref(false);
  const storageRepairAllBusy = ref(false);
  const storageRepairPlan = ref<any>(null);
  const updateInfo = ref<any>({});
  const diagnostic = ref<any>(null);
  const updateBusy = ref(false);
  const notificationTestBusy = ref(false);
  const notificationTestChannel = ref<
    "all" | "webhook" | "bark" | "email" | "gotify"
  >("all");
  const notificationTestResult = ref<Record<string, any>>({});
  let logTimer: number | undefined;
  let storageAuditTimer: number | undefined;

  const pageMap = computed(() =>
    props.mode === "maintenance" ? maintenancePages : settingsPages,
  );
  const activePage = computed(
    () =>
      pageMap.value[props.section] ||
      pageMap.value[props.mode === "maintenance" ? "services" : "application"],
  );
  const unknownFields = computed(() => {
    const runtimeKeys = new Set(runtimeEnvKeys.value);
    return allFields.value.filter(
      (field) => !assignedEnvKeys.has(field.key) && !runtimeKeys.has(field.key),
    );
  });

  const activeFieldSections = computed<SettingsFieldSection[]>(() => {
    if (activePage.value.id === "other") {
      return [
        {
          title: "未分类配置",
          description:
            "这些字段来自较新的服务端版本，保存方式与其他基础配置一致。",
          envKeys: unknownFields.value.map((field) => field.key),
        },
      ];
    }
    return activePage.value.fieldSections || [];
  });
  const allFieldMap = computed(() =>
    Object.fromEntries(allFields.value.map((field) => [field.key, field])),
  );
  const resolvedSections = computed(() =>
    activeFieldSections.value
      .map((section) => ({
        ...section,
        fields: [
          ...(section.envKeys || [])
            .map((key) => resolveEnvField(key))
            .filter(Boolean),
          ...(section.runtimeKeys || [])
            .map((key) => resolveRuntimeField(key))
            .filter(Boolean),
        ] as ResolvedField[],
      }))
      .filter((section) => section.fields.length),
  );
  const activeEnvKeys = computed(() => [
    ...new Set(
      activeFieldSections.value.flatMap((section) => section.envKeys || []),
    ),
  ]);
  const activeRuntimeKeys = computed(() => [
    ...new Set(
      activeFieldSections.value.flatMap((section) => section.runtimeKeys || []),
    ),
  ]);
  const activePlatform = computed(() =>
    platformCredentials.find((item) => item.pageId === activePage.value.id),
  );
  const activeAccountPrefix = computed(
    () =>
      (
        ({
          "account-x": "X",
          "account-tiktok": "TIKTOK",
          "account-weibo": "WEIBO",
          "account-bilibili": "BILIBILI",
          "account-xhs": "XHS",
        }) as Record<string, string>
      )[activePage.value.id] || "",
  );
  const activeAccountFacts = computed(() => {
    const prefix = activeAccountPrefix.value;
    if (!prefix) return [];
    return [
      {
        label: "下载引擎",
        value: resourceState.value.all.loaded
          ? allValues.value[`${prefix}_DOWNLOAD_ENGINE`]?.value || "未配置"
          : "未确认",
      },
      {
        label: "Cookie 文件",
        value: resourceState.value.all.loaded
          ? allValues.value[`${prefix}_COOKIE_FILE`]?.value || "未配置"
          : "未确认",
      },
      {
        label: "网页 Cookie",
        value:
          activePage.value.id === "account-x"
            ? "独立加密保存，不回显"
            : !resourceState.value.platformCredentials.loaded
              ? "未确认"
              : platformCredentialStatus.value[activePlatform.value?.id || ""]
                    ?.configured
                ? "已配置"
                : "未配置",
      },
    ];
  });
  const filteredLogs = computed(() =>
    logs.value.filter((item) => logLevels.value.includes(item.level)),
  );

  function resolveEnvField(key: string): ResolvedField | null {
    const field = allFieldMap.value[key];
    if (!field) return null;
    const defaultValue = String(field.default ?? "");
    const isBoolean = ["true", "false"].includes(defaultValue.toLowerCase());
    const isNumber =
      !isBoolean &&
      defaultValue !== "" &&
      Number.isFinite(Number(defaultValue));
    return {
      source: "env",
      key,
      label: field.label || key,
      help:
        field.help ||
        (field.secret ? "敏感值不会回显；留空表示保持现有值。" : ""),
      secret: Boolean(field.secret),
      required: Boolean(field.required),
      kind: isBoolean ? "boolean" : isNumber ? "number" : "text",
    };
  }
  function resolveRuntimeField(key: string): ResolvedField | null {
    const spec = limits.value[key];
    if (!spec) return null;
    return {
      source: "runtime",
      key,
      label: spec.label || key,
      help:
        spec.type === "bool"
          ? "修改后在下一次任务调用或调度周期生效。"
          : [
              spec.min != null && spec.max != null
                ? String(spec.min) + "–" + String(spec.max)
                : "",
              spec.unit || "",
            ]
              .filter(Boolean)
              .join(" "),
      secret: false,
      required: false,
      kind: spec.type === "bool" ? "boolean" : "number",
      min: spec.min,
      max: spec.max,
    };
  }
  function fieldValue(field: ResolvedField) {
    if (field.secret) return secretValues.value[field.key] || "";
    return field.source === "runtime"
      ? runtime.value[field.key]
      : (allValues.value[field.key]?.value ?? "");
  }
  function setFieldValue(field: ResolvedField, value: any) {
    if (field.secret) {
      secretValues.value[field.key] = String(value);
    } else if (field.source === "runtime") {
      runtime.value[field.key] =
        field.kind === "number" ? Number(value) : value;
    } else {
      if (!allValues.value[field.key])
        allValues.value[field.key] = { value: "" };
      allValues.value[field.key].value =
        field.kind === "boolean" ? String(Boolean(value)) : String(value);
    }
  }
  function fieldEnabled(field: ResolvedField) {
    return field.source === "runtime"
      ? Boolean(runtime.value[field.key])
      : String(allValues.value[field.key]?.value).toLowerCase() === "true";
  }
  function toggleField(field: ResolvedField) {
    setFieldValue(field, !fieldEnabled(field));
  }
  function envKeyDirty(key: string) {
    const field = allFieldMap.value[key];
    if (!field) return false;
    if (field.secret) return Boolean(secretValues.value[key]?.trim());
    return !same(allValues.value[key]?.value, baselineAll.value[key]);
  }
  function runtimeKeyDirty(key: string) {
    return !same(runtime.value[key], baselineRuntime.value[key]);
  }
  const dirtyEnvKeys = computed(() => activeEnvKeys.value.filter(envKeyDirty));
  const dirtyRuntimeKeys = computed(() =>
    activeRuntimeKeys.value.filter(runtimeKeyDirty),
  );
  const archiveDirty = computed(
    () =>
      activePage.value.id === "archive" &&
      !same(archiveRules.value, baselineArchive.value),
  );
  const douyinDirtyCount = computed(() => {
    if (activePage.value.id !== "account-douyin") return 0;
    let count = 0;
    if (douyinCookieValue.value.trim()) count += 1;
    if (douyinProxyValue.value.trim()) count += 1;
    if (
      !same(
        douyinAccount.value.user_agent || "",
        baselineDouyinAccount.value.user_agent || "",
      )
    )
      count += 1;
    if (
      !same(
        Boolean(douyinAccount.value.proxy_enabled),
        Boolean(baselineDouyinAccount.value.proxy_enabled),
      )
    )
      count += 1;
    return count;
  });
  const credentialDirty = computed(() => {
    if (activePage.value.id === "account-x")
      return Boolean(xCookieValue.value.trim());
    if (activePlatform.value)
      return Boolean(
        platformCookieValues.value[activePlatform.value.id]?.trim(),
      );
    return false;
  });
  const activeDirtyCount = computed(
    () =>
      dirtyEnvKeys.value.length +
      dirtyRuntimeKeys.value.length +
      (archiveDirty.value ? 1 : 0) +
      douyinDirtyCount.value +
      (credentialDirty.value ? 1 : 0),
  );
  const hasActiveChanges = computed(() => activeDirtyCount.value > 0);

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
  const readinessLabel = (name: string) =>
    (
      ({
        configuration: "应用配置",
        database: "数据库",
        redis: "Redis",
        worker: "Celery Worker",
        beat: "Celery Beat",
        xhs_collector: "小红书下载服务",
      }) as Record<string, string>
    )[name] || name;
  const sourceLabel = (name: string) =>
    (({ profile: "作者主页", work: "单条作品" }) as Record<string, string>)[
      name
    ] || name;
  function displayDateTime(value?: string) {
    if (!value) return "暂无成功记录";
    const date = new Date(value);
    return Number.isNaN(date.getTime())
      ? "时间未知"
      : date.toLocaleString("zh-CN");
  }

  let pageLoadSequence = 0;
  const resourceRevision: Partial<Record<ResourceKey, number>> = {};
  async function trackedLoad(
    key: ResourceKey,
    loader: (isCurrent: () => boolean) => Promise<void>,
  ) {
    const revision = (resourceRevision[key] || 0) + 1;
    resourceRevision[key] = revision;
    const isCurrent = () => resourceRevision[key] === revision;
    try {
      await loader(isCurrent);
      if (!isCurrent()) return false;
      resourceState.value[key] = { loaded: true, error: "" };
      return true;
    } catch (error: any) {
      if (!isCurrent()) return false;
      resourceState.value[key] = {
        loaded: false,
        error: error.message || "读取失败",
      };
      return false;
    }
  }
  async function loadAll() {
    return trackedLoad("all", async (isCurrent) => {
      const data = await api<any>("/config/all");
      if (!isCurrent()) return;
      allFields.value = data.fields || [];
      allValues.value = data.values || {};
      runtimeEnvKeys.value = data.runtime_keys || [];
      baselineAll.value = Object.fromEntries(
        Object.entries(allValues.value).map(([key, item]: [string, any]) => [
          key,
          clone(item?.value),
        ]),
      );
      secretValues.value = {};
    });
  }
  async function loadRuntime() {
    return trackedLoad("runtime", async (isCurrent) => {
      const data = await api<any>("/config/runtime");
      if (!isCurrent()) return;
      runtime.value = data.config || {};
      limits.value = data.limits || {};
      baselineRuntime.value = clone(runtime.value);
    });
  }
  async function loadArchiveRules() {
    return trackedLoad("archive", async (isCurrent) => {
      const data = await api<any>("/config/archive-rules");
      if (!isCurrent()) return;
      archiveRules.value = data.rules;
      baselineArchive.value = clone(data.rules);
    });
  }
  async function loadDouyinAccount() {
    return trackedLoad("douyinAccount", async (isCurrent) => {
      const data = await api<any>("/config/douyin-account");
      if (!isCurrent()) return;
      douyinAccount.value = data.account || {};
      baselineDouyinAccount.value = clone(douyinAccount.value);
      douyinCookieValue.value = "";
      douyinProxyValue.value = "";
    });
  }
  async function loadPlatformCredentialStatus() {
    return trackedLoad("platformCredentials", async (isCurrent) => {
      const rows = await Promise.all(
        platformCredentials.map(
          async (item) =>
            [
              item.id,
              await api<any>(
                "/platform-downloads/" + item.id + "/config/cookie",
              ),
            ] as const,
        ),
      );
      if (!isCurrent()) return;
      platformCredentialStatus.value = Object.fromEntries(rows);
    });
  }
  async function loadLogs(silent: boolean | Event = false) {
    const ok = await trackedLoad("logs", async (isCurrent) => {
      const data = await api<any>("/logs?start=0&count=500");
      if (isCurrent()) logs.value = data.logs || [];
    });
    if (!ok && silent !== true)
      store.notify(
        resourceState.value.logs.error || "活动日志读取失败",
        "error",
      );
    return ok;
  }
  async function loadProcess() {
    process.value = await api("/process/status");
  }
  async function loadReadiness() {
    readiness.value = await api("/status/readiness");
  }
  async function loadPlatformReadiness() {
    platformAuditBusy.value = true;
    try {
      platformReadiness.value = await api("/operations/platform-readiness");
    } catch (error: any) {
      store.notify(error.message || "平台验收状态加载失败", "error");
    } finally {
      platformAuditBusy.value = false;
    }
  }
  async function loadUpdateInfo() {
    updateInfo.value = await api<any>("/update/info");
  }
  function stopStorageAuditPolling() {
    if (storageAuditTimer != null) window.clearInterval(storageAuditTimer);
    storageAuditTimer = undefined;
  }
  function startStorageAuditPolling() {
    if (storageAuditTimer == null)
      storageAuditTimer = window.setInterval(
        () => refreshStorageAudit(true),
        2000,
      );
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
      if (storageAuditBusy.value || storageRepairAllBusy.value)
        startStorageAuditPolling();
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
          "存储全部维护完成：已处理 " +
            (storageRepairAllState.value.result?.applied || 0) +
            " 项",
        );
      }
      if (
        ["completed", "partial"].includes(storageRepairAllState.value.status) &&
        ["queued", "running"].includes(previousRepairAllStatus)
      )
        void loadStorageJournals();
      if (
        storageRepairAllState.value.status === "partial" &&
        previousRepairAllStatus !== "partial"
      )
        store.notify("存储维护部分完成，请查看失败项和复检结果", "error");
      if (
        storageRepairAllState.value.status === "failed" &&
        previousRepairAllStatus !== "failed" &&
        !silent
      ) {
        store.notify(
          storageRepairAllState.value.error || "存储全部维护失败",
          "error",
        );
      }
    } catch (error: any) {
      storageAuditBusy.value = false;
      stopStorageAuditPolling();
      if (!silent)
        store.notify(error.message || "读取存储巡检状态失败", "error");
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
        "确认处理 " +
          targets.length +
          " 项？异常文件只移动到可恢复隔离区，不会直接删除。",
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
      store.notify(
        "本批已处理 " + result.applied + " 项，正在重新扫描剩余问题",
      );
      await startStorageAudit();
    } catch (error: any) {
      store.notify(error.message || "执行存储维护失败", "error");
    } finally {
      storageRepairBusy.value = false;
    }
  }
  async function applyAllStorageRepairs() {
    const count = storageIssueCount.value;
    if (
      !count ||
      !(await confirmAction(
        "确认在后台处理本次扫描发现的全部 " + count + " 项问题？",
      ))
    )
      return;
    storageRepairAllBusy.value = true;
    storageRepairPlan.value = null;
    try {
      storageRepairAllState.value = await api<any>(
        "/operations/storage-repair-all",
        { method: "POST" },
      );
      store.notify("存储全部维护已进入后台队列，可以离开页面");
      startStorageAuditPolling();
    } catch (error: any) {
      storageRepairAllBusy.value = false;
      store.notify(error.message || "提交存储全部维护失败", "error");
    }
  }

  async function loadStorageJournals() {
    storageJournals.value =
      (await api<any>("/operations/storage-journals")).items || [];
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

  async function loadActivePage() {
    const current = ++pageLoadSequence;
    pageLoading.value = true;
    saveError.value = "";
    const jobs: Promise<any>[] = [];
    if (props.mode === "settings") {
      if (activePage.value.id === "archive") jobs.push(loadArchiveRules());
      else if (activePage.value.id === "account-douyin")
        jobs.push(loadDouyinAccount());
      else {
        jobs.push(loadAll());
        if (activeRuntimeKeys.value.length) jobs.push(loadRuntime());
        if (activePlatform.value) jobs.push(loadPlatformCredentialStatus());
      }
    } else if (activePage.value.id === "services") {
      jobs.push(loadProcess(), loadReadiness());
    } else if (activePage.value.id === "platforms") {
      jobs.push(loadPlatformReadiness());
    } else if (activePage.value.id === "storage") {
      jobs.push(refreshStorageAudit(true), loadStorageJournals());
    } else if (activePage.value.id === "logs") {
      jobs.push(loadLogs());
    } else if (activePage.value.id === "update") {
      jobs.push(loadUpdateInfo());
    }
    const results = await Promise.allSettled(jobs);
    if (current !== pageLoadSequence) return;
    const failures = results.filter(
      (result) =>
        result.status === "rejected" ||
        (result.status === "fulfilled" && result.value === false),
    );
    if (failures.length)
      saveError.value =
        "有 " +
        failures.length +
        " 项状态读取失败；为避免覆盖服务端值，相关保存已禁用。";
    pageLoading.value = false;
    configureLogPolling();
  }
  function configureLogPolling() {
    if (logTimer != null) window.clearInterval(logTimer);
    logTimer = undefined;
    if (props.mode === "maintenance" && activePage.value.id === "logs") {
      logTimer = window.setInterval(() => {
        if (live.value) void loadLogs(true);
      }, 3000);
    }
  }
  function discardActiveChanges() {
    for (const key of activeEnvKeys.value) {
      if (allValues.value[key])
        allValues.value[key].value = clone(baselineAll.value[key]);
      delete secretValues.value[key];
    }
    for (const key of activeRuntimeKeys.value)
      runtime.value[key] = clone(baselineRuntime.value[key]);
    if (activePage.value.id === "archive")
      archiveRules.value = clone(baselineArchive.value);
    if (activePage.value.id === "account-douyin") {
      douyinAccount.value = clone(baselineDouyinAccount.value);
      douyinCookieValue.value = "";
      douyinProxyValue.value = "";
    }
    if (activePage.value.id === "account-x") xCookieValue.value = "";
    if (activePlatform.value)
      platformCookieValues.value[activePlatform.value.id] = "";
    saveError.value = "";
  }
  async function confirmDiscard() {
    if (saving.value) {
      store.notify("正在保存，请等待完成后离开", "info");
      return false;
    }
    return (
      !hasActiveChanges.value ||
      (await confirmAction("当前子页有未保存修改，确定放弃并离开吗？"))
    );
  }
  async function refreshActivePage() {
    if (!(await confirmDiscard())) return;
    discardActiveChanges();
    await loadActivePage();
  }

  async function saveEnvBlock(keys: string[]) {
    const dirtyKeys = keys.filter(envKeyDirty);
    if (!dirtyKeys.length) return false;
    if (!resourceState.value.all.loaded)
      throw new Error("基础配置尚未成功读取");
    const values: Record<string, any> = {};
    for (const key of dirtyKeys) {
      const field = allFieldMap.value[key];
      values[key] = field?.secret
        ? secretValues.value[key].trim()
        : allValues.value[key]?.value;
    }
    if (
      dirtyKeys.some(
        (key) => key === "DOWNLOAD_ROOT" || key.endsWith("_DOWNLOAD_SUBDIR"),
      )
    ) {
      const preview = await api<any>("/config/all?preview=true", {
        method: "POST",
        ...jsonBody({ values }),
      });
      const counts = preview.data?.migrated_paths || {};
      const count = Object.values(counts)
        .filter((value) => typeof value === "number")
        .reduce<number>((sum, value) => sum + Number(value), 0);
      if (
        !(await confirmAction(
          `目录预演通过，将更新 ${count} 条文件关联。不搬移或删除文件，是否保存？`,
        ))
      )
        return false;
    }
    const result = await api<any>("/config/all", {
      method: "POST",
      ...jsonBody({ values }),
    });
    configurationFeedback.value = result.message || "配置已生效";
    if (result.data?.admin_token) saveToken(result.data.admin_token);
    for (const key of dirtyKeys) {
      const field = allFieldMap.value[key];
      if (field?.secret) delete secretValues.value[key];
      else baselineAll.value[key] = clone(allValues.value[key]?.value);
    }
    return true;
  }
  async function saveRuntimeBlock(keys: string[]) {
    const dirtyKeys = keys.filter(runtimeKeyDirty);
    if (!dirtyKeys.length) return false;
    if (!resourceState.value.runtime.loaded)
      throw new Error("运行配置尚未成功读取");
    const payload = Object.fromEntries(
      dirtyKeys.map((key) => [key, runtime.value[key]]),
    );
    const result = await api<any>("/config/runtime", {
      method: "POST",
      ...jsonBody(payload),
    });
    for (const key of dirtyKeys) {
      if (result.data?.config && key in result.data.config)
        runtime.value[key] = result.data.config[key];
      baselineRuntime.value[key] = clone(runtime.value[key]);
    }
    await store.refreshStatus();
    return true;
  }
  async function saveArchiveBlock() {
    if (!archiveDirty.value) return false;
    if (!resourceState.value.archive.loaded)
      throw new Error("归档规则尚未成功读取");
    const result = await api<any>("/config/archive-rules", {
      method: "POST",
      ...jsonBody(archiveRules.value),
    });
    archiveRules.value = result.rules;
    baselineArchive.value = clone(result.rules);
    return true;
  }
  async function saveDouyinBlock() {
    if (!douyinDirtyCount.value) return false;
    if (!resourceState.value.douyinAccount.loaded)
      throw new Error("抖音账号状态尚未成功读取");
    const payload: Record<string, any> = {};
    if (douyinCookieValue.value.trim())
      payload.cookie = douyinCookieValue.value.trim();
    if (douyinProxyValue.value.trim())
      payload.proxy_url = douyinProxyValue.value.trim();
    if (
      !same(
        douyinAccount.value.user_agent || "",
        baselineDouyinAccount.value.user_agent || "",
      )
    )
      payload.user_agent = douyinAccount.value.user_agent;
    if (
      !same(
        Boolean(douyinAccount.value.proxy_enabled),
        Boolean(baselineDouyinAccount.value.proxy_enabled),
      )
    )
      payload.proxy_enabled = Boolean(douyinAccount.value.proxy_enabled);
    const result = await api<any>("/config/douyin-account", {
      method: "POST",
      ...jsonBody(payload),
    });
    douyinAccount.value = result.data || {};
    baselineDouyinAccount.value = clone(douyinAccount.value);
    douyinCookieValue.value = "";
    douyinProxyValue.value = "";
    await store.refreshRisk();
    return true;
  }
  async function saveCredentialBlock() {
    if (!credentialDirty.value) return false;
    if (activePage.value.id === "account-x") {
      await api<any>("/x/config/cookie", {
        method: "POST",
        ...jsonBody({ cookie: xCookieValue.value.trim() }),
      });
      xCookieValue.value = "";
      return true;
    }
    const platform = activePlatform.value;
    if (!platform) return false;
    await api<any>("/platform-downloads/" + platform.id + "/config/cookie", {
      method: "POST",
      ...jsonBody({ cookie: platformCookieValues.value[platform.id].trim() }),
    });
    platformCookieValues.value[platform.id] = "";
    await loadPlatformCredentialStatus();
    return true;
  }
  async function saveCurrentPage(options: { silentSuccess?: boolean } = {}) {
    if (saving.value || pageLoading.value) return false;
    if (!hasActiveChanges.value) {
      if (!options.silentSuccess)
        store.notify("当前子页没有需要保存的修改", "info");
      return true;
    }
    saving.value = true;
    saveError.value = "";
    const errors: string[] = [];
    let savedBlocks = 0;
    const run = async (label: string, operation: () => Promise<boolean>) => {
      try {
        if (await operation()) savedBlocks += 1;
      } catch (error: any) {
        errors.push(label + "：" + (error.message || "保存失败"));
      }
    };
    if (activePage.value.id === "archive")
      await run("归档规则", saveArchiveBlock);
    else if (activePage.value.id === "account-douyin")
      await run("抖音账号", saveDouyinBlock);
    else {
      await run("基础配置", () => saveEnvBlock(activeEnvKeys.value));
      await run("运行配置", () => saveRuntimeBlock(activeRuntimeKeys.value));
      if (activePage.value.id === "account-x" || activePlatform.value)
        await run("平台凭据", saveCredentialBlock);
    }
    saving.value = false;
    if (errors.length) {
      saveError.value = errors.join("；");
      store.notify(
        savedBlocks ? "部分配置已保存，其余修改已保留" : "当前子页保存失败",
        "error",
      );
      return false;
    }
    if (!options.silentSuccess)
      store.notify("“" + activePage.value.title + "”已保存");
    return true;
  }
  async function saveAndTestNotification() {
    notificationTestBusy.value = true;
    try {
      if (!(await saveCurrentPage({ silentSuccess: true }))) return;
      const result = await api<any>("/notifications/test", {
        method: "POST",
        ...jsonBody({ channel: notificationTestChannel.value }),
      });
      notificationTestResult.value = result.data?.channels || {};
      store.notify(
        result.message || "通知测试完成",
        result.success ? "success" : "error",
      );
    } catch (error: any) {
      store.notify(error.message || "通知测试失败", "error");
    } finally {
      notificationTestBusy.value = false;
    }
  }
  function toggleArchiveValue(
    key: "work_types" | "metadata_formats",
    value: string,
  ) {
    const values = archiveRules.value[key] || [];
    archiveRules.value[key] = values.includes(value)
      ? values.filter((item: string) => item !== value)
      : [...values, value];
  }
  async function processAction(
    target: "worker" | "beat",
    action: "start" | "stop",
  ) {
    try {
      const result = await api<any>("/process/" + target + "/" + action, {
        method: "POST",
      });
      store.notify(result.message || "操作完成");
      await Promise.all([loadProcess(), loadReadiness()]);
    } catch (error: any) {
      store.notify(error.message || "进程操作失败", "error");
    }
  }
  async function clearLogs() {
    if (
      !resourceState.value.logs.loaded ||
      !(await confirmAction("确定清空活动日志？"))
    )
      return;
    await api("/logs", { method: "DELETE" });
    logs.value = [];
    store.notify("日志已清空");
  }
  async function copyLogs() {
    if (!resourceState.value.logs.loaded) return;
    const text = filteredLogs.value
      .map(
        (item) =>
          new Date(item.ts * 1000).toLocaleString() +
          " [" +
          item.level +
          "] [" +
          item.source +
          "]" +
          (item.event_code ? " [" + item.event_code + "]" : "") +
          (item.correlation_id ? " [请求 " + item.correlation_id + "]" : "") +
          " " +
          item.msg +
          (item.detail ? "\n" + item.detail : "") +
          (Object.keys(item.context || {}).length
            ? "\n上下文: " + JSON.stringify(item.context)
            : ""),
      )
      .join("\n\n");
    await navigator.clipboard.writeText(text);
    store.notify("日志已复制（敏感值已脱敏）");
  }
  async function checkUpdate() {
    updateBusy.value = true;
    try {
      updateInfo.value = await api<any>("/update/check");
      store.notify(updateInfo.value.message || "检查完成");
    } catch (error: any) {
      store.notify(error.message || "检查更新失败", "error");
    } finally {
      updateBusy.value = false;
    }
  }
  async function diagnoseUpdate() {
    try {
      diagnostic.value = await api<any>("/update/diagnose");
      await navigator.clipboard.writeText(
        JSON.stringify(diagnostic.value, null, 2),
      );
      store.notify("诊断信息已复制");
    } catch (error: any) {
      store.notify(error.message || "诊断失败", "error");
    }
  }
  async function applyUpdate() {
    if (!(await confirmAction("确定拉取远程更新并重启 Worker/Beat？"))) return;
    updateBusy.value = true;
    try {
      const result = await api<any>("/update/apply", { method: "POST" });
      store.notify(result.message || "更新完成");
      await loadUpdateInfo();
    } catch (error: any) {
      store.notify(error.message || "更新失败", "error");
    } finally {
      updateBusy.value = false;
    }
  }
  function toggleLevel(level: string) {
    logLevels.value = logLevels.value.includes(level)
      ? logLevels.value.filter((value) => value !== level)
      : [...logLevels.value, level];
  }
  function beforeUnload(event: BeforeUnloadEvent) {
    if (!hasActiveChanges.value) return;
    event.preventDefault();
    event.returnValue = "";
  }
  onBeforeRouteLeave(() => confirmDiscard());
  onBeforeRouteUpdate(() => confirmDiscard());
  watch(
    () => [props.mode, props.section] as const,
    async () => {
      const defaultSection =
        props.mode === "maintenance" ? "services" : "application";
      if (!pageMap.value[props.section]) {
        await router.replace(sectionPath(props.mode, defaultSection));
        return;
      }
      await loadActivePage();
    },
    { immediate: true },
  );
  onMounted(() => {
    void refreshQueueChange();
    queueChangeTimer = window.setInterval(() => {
      if (!document.hidden) void refreshQueueChange();
    }, 10_000);
    window.addEventListener("beforeunload", beforeUnload);
  });
  onBeforeUnmount(() => {
    pageLoadSequence++;
    for (const key of Object.keys(resourceState.value) as ResourceKey[])
      resourceRevision[key] = (resourceRevision[key] || 0) + 1;
    window.clearInterval(queueChangeTimer);
    if (logTimer != null) window.clearInterval(logTimer);
    stopStorageAuditPolling();
    window.removeEventListener("beforeunload", beforeUnload);
  });

  const advancedDiagnostics = ref<any>(),
    advancedBusy = ref(false),
    advancedError = ref("");
  async function loadAdvanced() {
    advancedBusy.value = true;
    advancedError.value = "";
    const results = await Promise.allSettled([
      api("/celery-debug"),
      api("/worker-log?lines=80"),
    ]);
    advancedDiagnostics.value = {
      queue: results[0].status === "fulfilled" ? results[0].value : "未确认",
      worker_log:
        results[1].status === "fulfilled" ? results[1].value : "未确认",
    };
    if (results.some((item) => item.status === "rejected"))
      advancedError.value = "部分诊断未读取成功";
    advancedBusy.value = false;
  }
  async function advancedAction(endpoint: string) {
    if (advancedBusy.value) return;
    if (
      endpoint === "celery-purge-old" &&
      !(await confirmAction(
        "清空旧 download / scheduler 队列中的全部积压消息？消息删除不可撤销；数据库任务和媒体文件保留。",
        "清理旧队列",
        "清空旧队列",
      ))
    )
      return;
    if (
      endpoint === "service/restart" &&
      !(await confirmAction(
        "重启 Web 服务？当前网页会短暂失去连接，请等待服务恢复。",
        "重启 Web 服务",
        "重启服务",
      ))
    )
      return;
    advancedBusy.value = true;
    try {
      const result = await api<any>("/" + endpoint, { method: "POST" });
      if (result.success === false)
        throw new Error(result.error || result.message || "操作失败");
      store.notify(result.message || "操作已提交");
    } catch (e: any) {
      store.notify(e.message, "error");
    } finally {
      advancedBusy.value = false;
    }
  }
  async function testDatabase() {
    if (!resourceState.value.all.loaded || advancedBusy.value) return;
    advancedBusy.value = true;
    const values = Object.fromEntries(
      ["DB_TYPE", "DB_HOST", "DB_PORT", "DB_USER", "DB_NAME"].map((key) => [
        key.toLowerCase(),
        allValues.value[key]?.value,
      ]),
    );
    values.db_port = Number(values.db_port);
    try {
      const result = await api<any>("/config/database/test", {
        method: "POST",
        ...jsonBody({
          ...values,
          db_password: secretValues.value.DB_PASSWORD || "",
        }),
      });
      if (result.success === false)
        throw new Error(result.message || "连接失败");
      store.notify(result.message || "连接成功");
    } catch (e: any) {
      store.notify(e.message, "error");
    } finally {
      advancedBusy.value = false;
    }
  }
  return {
    advancedDiagnostics,
    advancedBusy,
    advancedError,
    loadAdvanced,
    advancedAction,
    testDatabase,
    archiveRules,
    douyinAccount,
    douyinCookieValue,
    douyinProxyValue,
    xCookieValue,
    platformCookieValues,
    platformCredentialStatus,
    resourceState,
    saving,
    pageLoading,
    saveError,
    configurationFeedback,
    queueChange,
    cancelQueueChange,
    logLevels,
    live,
    process,
    readiness,
    platformReadiness,
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
    updateInfo,
    diagnostic,
    updateBusy,
    notificationTestBusy,
    notificationTestChannel,
    notificationTestResult,
    activePage,
    resolvedSections,
    activePlatform,
    activeAccountFacts,
    filteredLogs,
    fieldValue,
    setFieldValue,
    fieldEnabled,
    toggleField,
    activeDirtyCount,
    hasActiveChanges,
    storageRepairTargets,
    eligibleRepairTargets,
    storageIssueCount,
    readinessLabel,
    sourceLabel,
    displayDateTime,
    startStorageAudit,
    previewStorageRepair,
    applyStorageRepair,
    applyAllStorageRepairs,
    loadStorageJournals,
    restoreStorage,
    discardActiveChanges,
    refreshActivePage,
    saveCurrentPage,
    saveAndTestNotification,
    toggleArchiveValue,
    processAction,
    clearLogs,
    copyLogs,
    checkUpdate,
    diagnoseUpdate,
    applyUpdate,
    toggleLevel,
  };
}
export type SettingsController = ReturnType<typeof useSettingsController>;
