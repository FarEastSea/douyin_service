<script setup lang="ts">
import IssueDetail from "./components/IssueDetail.vue";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import {
  Aperture,
  Download,
  HardDrive,
  KeyRound,
  LayoutDashboard,
  Layers3,
  Menu,
  MoonStar,
  Settings,
  Sun,
  LogOut,
  Users,
  X,
  Search,
  Plus,
  ArrowLeft,
  Images,
  ChartColumn,
  CircleCheck,
  Info,
  CircleAlert,
} from "@lucide/vue";
import { api, ApiError, saveToken, getToken } from "./api";
import MediaLightbox from "./components/MediaLightbox.vue";
import NewDownloadPanel from "./components/NewDownloadPanel.vue";
import CommandPalette from "./components/CommandPalette.vue";
import MoreActions from "./components/MoreActions.vue";
import ConfirmDialog from "./components/ConfirmDialog.vue";
import { modalDepth, inspectorDepth } from "./workspace";
import { settingsNavigation, maintenanceNavigation, sectionPath } from "./settings-registry";
import RiskBanner from "./components/RiskBanner.vue";
import { captureFocusOrigin, focusFirst, restoreFocus, trapFocus } from "./focus";
import { useAppStore } from "./stores/app";
import type { MediaItem } from "./types";
import BootstrapView from "./views/BootstrapView.vue";

const store = useAppStore(),
  route = useRoute();
const connectionError = ref("");
const initialized = ref(false),
  bootstrap = ref<any>({ ready: true });
const commandOpen = ref(false);
const authOpen = ref(false),
  token = ref("");
const previewOpen = ref(false),
  previewItems = ref<MediaItem[]>([]),
  previewStart = ref(0);
const newDownloadOpen = ref(false),
  newDownloadButton = ref<HTMLButtonElement | null>(null);
const captureIntent = ref<"download" | "subscribe">("download"),
  capturePlatform = ref("");
const captureOrigin = ref<HTMLElement | null>(null);
const sidebarElement = ref<HTMLElement | null>(null),
  mobileMenuButton = ref<HTMLButtonElement | null>(null);
const authDialog = ref<HTMLElement | null>(null),
  authInput = ref<HTMLInputElement | null>(null);
const mobileViewport = ref(false);
const readiness = computed(() => store.readiness);
const authBusy = ref(false),
  authError = ref("");
const announcement = ref("");
watch(
  () => store.toast,
  async (toast) => {
    announcement.value = "";
    await nextTick();
    if (toast && toast === store.toast) announcement.value = toast.message;
  },
);
const mainContent = ref<HTMLElement | null>(null);
let authReturnFocus: HTMLElement | null = null;
let mobileQuery: MediaQueryList | null = null;
let themeQuery: MediaQueryList | null = null;
const blocking = computed(
  () => authOpen.value || newDownloadOpen.value || previewOpen.value || modalDepth.value > 0,
);
watch(authOpen, (value) => {
  modalDepth.value += value ? 1 : -1;
});
watch(blocking, (value) => {
  document.body.classList.toggle("modal-open", value);
});
function themeChanged() {
  if (store.theme === "auto") store.applyTheme();
}

const directoryMode = computed(() =>
  route.path.startsWith("/settings")
    ? "settings"
    : route.path.startsWith("/operations/maintenance")
      ? "maintenance"
      : null,
);
const directory = computed(() =>
  directoryMode.value === "settings" ? settingsNavigation : maintenanceNavigation,
);
const pageTitle = computed(() => String(route.meta.title || "总览"));
const themeIcon = computed(() => (store.effectiveTheme === "dark" ? Sun : MoonStar));
const themeTarget = computed(() => (store.effectiveTheme === "dark" ? "light" : "dark"));
const themeLabel = computed(() => (themeTarget.value === "light" ? "切换为浅色" : "切换为深色"));
const readinessLabel = computed(() =>
  readiness.value === "ready"
    ? "服务就绪"
    : readiness.value === "not_ready"
      ? "服务需检查"
      : "状态未确认",
);
const commandShortcut = /Mac|iPhone|iPad/.test(navigator.userAgent) ? "⌘K" : "Ctrl K";
function openCommand() {
  if (!store.authenticated) {
    closeSidebar();
    openAuth();
    return;
  }
  closeSidebar();
  commandOpen.value = true;
}
const mobileNavigationOpen = computed(() => mobileViewport.value && store.sidebarOpen);

function toggleSidebar() {
  dismissSidebar();
}
function closeSidebar() {
  store.sidebarOpen = false;
}
function openSidebar() {
  store.sidebarOpen = true;
  void nextTick(() => focusFirst(sidebarElement.value));
}
function dismissSidebar() {
  store.sidebarOpen = false;
  void nextTick(() => restoreFocus(mobileMenuButton.value));
}
function preview(event: Event) {
  const detail = (event as CustomEvent).detail;
  previewItems.value = detail.items;
  previewStart.value = detail.start || 0;
  previewOpen.value = true;
}
function openNewDownload(event?: Event) {
  if (!store.authenticated) {
    openAuth(event);
    return;
  }
  captureOrigin.value =
    event?.currentTarget instanceof HTMLElement ? event.currentTarget : captureFocusOrigin();
  const detail = (event as CustomEvent | undefined)?.detail;
  captureIntent.value = detail?.intent === "subscribe" ? "subscribe" : "download";
  capturePlatform.value = typeof detail?.platform === "string" ? detail.platform : "";
  newDownloadOpen.value = true;
  closeSidebar();
}
function openSubscription() {
  openNewDownload(new CustomEvent("app:new-download", { detail: { intent: "subscribe" } }));
}
function closeNewDownload() {
  newDownloadOpen.value = false;
}
function handleVisibilityChange() {
  store.poll(true);
}
async function init() {
  store.applyTheme();
  connectionError.value = "";
  const initialToken = getToken();
  try {
    const status = await api("/bootstrap/status");
    if (initialToken !== getToken()) return;
    bootstrap.value = status;
    store.authenticated = Boolean(initialToken);
    if (bootstrap.value.ready && store.authenticated) store.startPolling();
  } catch (error) {
    if (!(error instanceof ApiError && error.status === 401))
      connectionError.value = error instanceof Error ? error.message : "无法连接服务";
  } finally {
    initialized.value = true;
    if (!connectionError.value && bootstrap.value.ready && !store.authenticated && !authOpen.value)
      showAuth();
  }
}
function showAuth(returnFocus?: HTMLElement | null) {
  authReturnFocus =
    returnFocus || (document.activeElement instanceof HTMLElement ? document.activeElement : null);
  authOpen.value = true;
  void nextTick(() => focusFirst(authDialog.value, authInput.value));
}
function openAuth(event?: Event) {
  showAuth(event?.currentTarget instanceof HTMLElement ? event.currentTarget : null);
}
function closeAuth() {
  if (authBusy.value) return;
  token.value = "";
  authError.value = "";
  authOpen.value = false;
  const target = authReturnFocus;
  authReturnFocus = null;
  void nextTick(() => restoreFocus(target));
}
function requireAuth() {
  store.stopPolling();
  store.$reset();
  store.applyTheme();
  previewOpen.value = false;
  previewItems.value = [];
  commandOpen.value = false;
  if (authOpen.value) return;
  newDownloadOpen.value = false;
  void nextTick(() => showAuth(captureOrigin.value || newDownloadButton.value));
}
function onGlobalKeydown(event: KeyboardEvent) {
  if (
    store.authenticated &&
    (event.ctrlKey || event.metaKey) &&
    event.key.toLowerCase() === "k" &&
    (modalDepth.value === inspectorDepth.value || commandOpen.value)
  ) {
    event.preventDefault();
    closeSidebar();
    commandOpen.value = !commandOpen.value;
    return;
  }
  if (authOpen.value) {
    if (event.key === "Escape") {
      event.preventDefault();
      closeAuth();
    } else trapFocus(event, authDialog.value);
    return;
  }
  if (mobileNavigationOpen.value) {
    if (event.key === "Escape") {
      event.preventDefault();
      dismissSidebar();
    } else trapFocus(event, sidebarElement.value);
  }
}
async function login() {
  const candidate = token.value.trim();
  if (!candidate || authBusy.value) return;
  authBusy.value = true;
  authError.value = "";
  try {
    const status = await api<{ ready: boolean }>("/bootstrap/status", {
      headers: { Authorization: `Bearer ${candidate}` },
    });
    saveToken(candidate);
    bootstrap.value = status;
    store.authenticated = true;
    if (status.ready) store.startPolling();
    authBusy.value = false;
    closeAuth();
  } catch (error) {
    authError.value = error instanceof Error ? error.message : "连接失败";
  } finally {
    authBusy.value = false;
  }
}
async function logout() {
  if (authBusy.value) return;
  authBusy.value = true;
  try {
    await api("/auth/logout", { method: "POST" });
    saveToken("");
    requireAuth();
  } catch (error) {
    store.notify(error instanceof Error ? error.message : "退出失败，请重试", "error");
  } finally {
    authBusy.value = false;
  }
}
function credentialsChanged(event: StorageEvent) {
  if (event.key === "douyinAdminToken" || event.key === null) {
    requireAuth();
    if (getToken())
      void init().then(() => {
        if (store.authenticated) closeAuth();
      });
  }
}
function updateMobileViewport(event: MediaQueryListEvent | MediaQueryList) {
  mobileViewport.value = event.matches;
  if (!event.matches) store.sidebarOpen = false;
}
onMounted(() => {
  mobileQuery = window.matchMedia("(max-width: 899px)");
  updateMobileViewport(mobileQuery);
  mobileQuery.addEventListener("change", updateMobileViewport);
  themeQuery = matchMedia("(prefers-color-scheme: light)");
  themeQuery.addEventListener("change", themeChanged);
  window.addEventListener("storage", credentialsChanged);
  void init();
  window.addEventListener("app:auth-required", requireAuth);
  window.addEventListener("app:preview", preview);
  window.addEventListener("app:new-download", openNewDownload);
  document.addEventListener("keydown", onGlobalKeydown);
  document.addEventListener("visibilitychange", handleVisibilityChange);
});
onBeforeUnmount(() => {
  window.removeEventListener("app:auth-required", requireAuth);
  window.removeEventListener("app:preview", preview);
  window.removeEventListener("app:new-download", openNewDownload);
  document.removeEventListener("keydown", onGlobalKeydown);
  document.removeEventListener("visibilitychange", handleVisibilityChange);
  themeQuery?.removeEventListener("change", themeChanged);
  mobileQuery?.removeEventListener("change", updateMobileViewport);
  window.removeEventListener("storage", credentialsChanged);
  store.stopPolling();
});
</script>

<template>
  <div
    v-if="!initialized"
    class="app-loading"
  >
    <span class="brand-mark"><Aperture :size="20" /></span>
    <span>正在载入控制台…</span>
  </div>
  <div
    v-else-if="connectionError"
    class="resource-error"
    role="alert"
  >
    <CircleAlert :size="18" />
    <span>{{ connectionError }}</span>
    <button
      class="btn"
      @click="init"
    >
      重新连接
    </button>
  </div>
  <BootstrapView
    v-else-if="!bootstrap.ready"
    :status="bootstrap"
    :inert="authOpen"
  />
  <div
    v-else
    class="app-shell"
    :class="{
      'directory-mode': directoryMode,
      'mobile-open': store.sidebarOpen,
      'has-capture-dock': !directoryMode,
    }"
    :inert="blocking"
  >
    <a
      class="skip-link"
      href="#main-content"
      @click.prevent="mainContent?.focus()"
    >
      跳转到主要内容
    </a>
    <header
      class="workspace-masthead"
      :inert="mobileNavigationOpen"
    >
      <button
        ref="mobileMenuButton"
        class="icon-btn mobile-menu"
        aria-label="打开导航"
        @click="openSidebar"
      >
        <Menu :size="20" />
      </button>
      <RouterLink
        class="workspace-brand"
        to="/dashboard"
        aria-label="媒体工作台总览"
      >
        <span class="brand-mark"><Aperture :size="16" /></span>
        <strong>媒体工作台</strong>
      </RouterLink>
      <div class="masthead-tools">
        <button
          class="icon-btn"
          :title="themeLabel"
          :aria-label="themeLabel"
          @click="store.setTheme(themeTarget)"
        >
          <component
            :is="themeIcon"
            :size="17"
          />
        </button>
        <button
          class="icon-btn"
          aria-label="打开命令面板"
          @click="openCommand"
        >
          <Search :size="18" />
        </button>
        <button
          v-if="directoryMode && store.authenticated"
          class="icon-btn masthead-add"
          aria-label="新建下载"
          @click="openNewDownload"
        >
          <Plus :size="18" />
        </button>
      </div>
    </header>
    <aside
      ref="sidebarElement"
      class="sidebar"
      :inert="mobileViewport && !store.sidebarOpen"
      :aria-hidden="mobileViewport && !store.sidebarOpen ? 'true' : undefined"
      aria-label="主导航"
      :role="mobileNavigationOpen ? 'dialog' : undefined"
      :aria-modal="mobileNavigationOpen ? 'true' : undefined"
      :tabindex="mobileNavigationOpen ? -1 : undefined"
    >
      <header class="brand">
        <span class="brand-mark"><Aperture :size="18" /></span>
        <div class="brand-copy">
          <strong>媒体工作台</strong>
          <small>Media Studio</small>
        </div>
        <button
          class="icon-btn collapse-btn"
          aria-label="关闭导航"
          @click="toggleSidebar"
        >
          <X :size="18" />
        </button>
      </header>
      <button
        class="sidebar-search"
        type="button"
        @click="openCommand"
      >
        <Search :size="15" />
        <span>搜索与命令</span>
        <kbd>{{ commandShortcut }}</kbd>
      </button>
      <template v-if="directoryMode">
        <RouterLink
          class="back-workspace"
          to="/dashboard"
          @click="closeSidebar"
        >
          <ArrowLeft :size="15" />
          <span>返回工作区</span>
        </RouterLink>
        <nav
          class="main-nav directory-nav"
          :aria-label="pageTitle + '目录'"
        >
          <div
            v-for="group in directory"
            :key="group.id"
            class="nav-group"
          >
            <span class="nav-label">{{ group.label }}</span>
            <RouterLink
              v-for="item in group.items"
              :key="item.id"
              :to="sectionPath(directoryMode, item.id)"
              @click="closeSidebar"
            >
              <span>{{ item.label }}</span>
            </RouterLink>
          </div>
        </nav>
      </template>
      <template v-else>
        <button
          v-if="store.authenticated"
          ref="newDownloadButton"
          class="btn primary sidebar-create"
          type="button"
          @click="openNewDownload"
        >
          <Plus :size="16" />
          新建下载
        </button>
        <nav
          class="main-nav"
          aria-label="工作区"
        >
          <span class="nav-label">工作区</span>
          <RouterLink
            to="/dashboard"
            @click="closeSidebar"
          >
            <LayoutDashboard :size="17" />
            <span>总览</span>
          </RouterLink>
          <RouterLink
            to="/operations/tasks"
            @click="closeSidebar"
          >
            <Layers3 :size="17" />
            <span>任务</span>
            <small
              v-if="store.stats.downloading_tasks"
              class="nav-count"
            >
              {{ store.stats.downloading_tasks }}
            </small>
          </RouterLink>
          <RouterLink
            to="/library"
            @click="closeSidebar"
          >
            <Images :size="17" />
            <span>媒体库</span>
          </RouterLink>
          <RouterLink
            to="/authors/douyin"
            :class="{
              'router-link-active':
                route.path.startsWith('/authors') || route.path === '/automation',
            }"
            @click="closeSidebar"
          >
            <Users :size="17" />
            <span>订阅</span>
          </RouterLink>
          <RouterLink
            to="/insights"
            @click="closeSidebar"
          >
            <ChartColumn :size="17" />
            <span>洞察</span>
          </RouterLink>
        </nav>
        <nav
          class="main-nav utility-nav"
          aria-label="系统"
        >
          <span class="nav-label">系统</span>
          <RouterLink
            to="/operations/maintenance/services"
            @click="closeSidebar"
          >
            <HardDrive :size="17" />
            <span>系统</span>
          </RouterLink>
          <RouterLink
            to="/settings/application"
            @click="closeSidebar"
          >
            <Settings :size="17" />
            <span>设置</span>
          </RouterLink>
        </nav>
      </template>
      <footer class="sidebar-footer">
        <RouterLink
          to="/operations/maintenance/services"
          class="service-state"
          :data-state="readiness"
          @click="closeSidebar"
        >
          <i aria-hidden="true" />
          <span>{{ readinessLabel }}</span>
        </RouterLink>
        <div class="sidebar-tools">
          <button
            v-if="!mobileViewport"
            class="icon-btn"
            :title="themeLabel"
            :aria-label="themeLabel"
            @click="store.setTheme(themeTarget)"
          >
            <component
              :is="themeIcon"
              :size="17"
            />
          </button>
          <button
            v-if="store.authenticated"
            class="connection-control"
            :disabled="authBusy"
            title="退出当前浏览器"
            @click="logout"
          >
            <LogOut :size="16" />
            已连接 · 退出
          </button>
          <button
            v-else
            class="connection-control"
            @click="openAuth"
          >
            <KeyRound :size="16" />
            连接凭据
          </button>
        </div>
      </footer>
    </aside>
    <button
      class="mobile-backdrop"
      aria-label="关闭导航"
      @click="dismissSidebar"
    />

    <main
      ref="mainContent"
      id="main-content"
      class="main-area"
      tabindex="-1"
      :inert="mobileNavigationOpen"
      :aria-hidden="mobileNavigationOpen ? 'true' : undefined"
    >
      <RiskBanner v-if="store.authenticated && route.path !== '/dashboard'" />
      <RouterView v-if="store.authenticated" />
      <div
        v-else
        class="empty-state"
      >
        <KeyRound :size="22" />
        <strong>连接后查看工作区</strong>
        <button
          class="btn primary"
          @click="openAuth"
        >
          连接凭据
        </button>
      </div>
    </main>
    <nav
      v-if="!directoryMode && store.authenticated"
      class="mobile-capture-dock"
      aria-label="快捷添加"
      :inert="mobileNavigationOpen"
    >
      <button
        class="btn primary"
        @click="openNewDownload"
      >
        <Download :size="18" />
        下载作品
      </button>
      <MoreActions label="快捷添加">
        <button @click="openSubscription">
          <Users :size="18" />
          订阅作者
        </button>
      </MoreActions>
    </nav>

    <Teleport to="body">
      <div
        class="sr-only"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        {{ announcement }}
      </div>
      <Transition name="toast">
        <div
          v-if="store.toast"
          class="toast"
          :class="{ 'above-capture-dock': !directoryMode }"
          :data-tone="store.toast.tone === 'error' ? 'danger' : store.toast.tone"
        >
          <component
            :is="
              store.toast.tone === 'error'
                ? CircleAlert
                : store.toast.tone === 'info'
                  ? Info
                  : CircleCheck
            "
            :size="17"
            aria-hidden="true"
          />
          <IssueDetail
            v-if="store.toast.tone === 'error'"
            :message="store.toast.message"
          />
          <span v-else>{{ store.toast.message }}</span>
          <button
            class="icon-btn"
            aria-label="关闭通知"
            @click="store.toast = null"
          >
            <X :size="16" />
          </button>
        </div>
      </Transition>
    </Teleport>
    <MediaLightbox
      :open="previewOpen"
      :items="previewItems"
      :start="previewStart"
      @close="
        previewOpen = false;
        previewItems = [];
      "
    />
    <CommandPalette
      v-if="commandOpen"
      @close="commandOpen = false"
    />
    <ConfirmDialog />
    <Teleport to="body">
      <NewDownloadPanel
        v-if="newDownloadOpen"
        :intent="captureIntent"
        :platform="capturePlatform"
        :return-focus="captureOrigin"
        @close="closeNewDownload"
      />
    </Teleport>
  </div>
  <Teleport to="body">
    <div
      v-if="authOpen"
      class="auth-overlay"
      @click.self="closeAuth"
    >
      <form
        ref="authDialog"
        class="auth-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="auth-title"
        tabindex="-1"
        @submit.prevent="login"
      >
        <button
          type="button"
          class="icon-btn close"
          aria-label="关闭"
          @click="closeAuth"
        >
          <X :size="18" />
        </button>
        <span class="brand-mark"><KeyRound :size="20" /></span>
        <h2 id="auth-title">输入管理 Token</h2>
        <p>Token 只保存在当前浏览器，并通过 Authorization 请求头发送。</p>
        <label for="admin-token">管理 Token</label>
        <input
          id="admin-token"
          ref="authInput"
          v-model="token"
          type="password"
          autocomplete="off"
          placeholder="Bearer Token"
        />
        <p
          v-if="authError"
          class="danger-text"
          role="alert"
        >
          {{ authError }}
        </p>
        <button
          class="btn primary"
          :disabled="authBusy"
        >
          {{ authBusy ? "正在连接…" : "连接并继续" }}
        </button>
      </form>
    </div>
  </Teleport>
</template>
