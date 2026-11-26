import { maintenancePages, settingsPages } from "./settings-registry";
import { createRouter, createWebHashHistory } from "vue-router";
const AuthorsView = () => import("./views/AuthorsView.vue");
const InstagramAuthorsView = () => import("./views/InstagramAuthorsView.vue");
const WorksView = () => import("./views/WorksView.vue");
const UpdatesView = () => import("./views/UpdatesView.vue");
const SettingsView = () => import("./views/SettingsView.vue");
const UnifiedTasksView = () => import("./views/UnifiedTasksView.vue");
const DashboardView = () => import("./views/DashboardView.vue");
const MediaLibraryView = () => import("./views/MediaLibraryView.vue");
const InsightsView = () => import("./views/InsightsView.vue");

const legacySections: Record<string, string> = {
  general: "/settings/application",
  account: "/settings/account-douyin",
  runtime: "/settings/downloads",
  archive: "/settings/archive",
  process: "/operations/maintenance/services",
  operations: "/operations/maintenance/platforms",
  logs: "/operations/maintenance/logs",
  about: "/operations/maintenance/update",
};

function legacySettingsRedirect(query: Record<string, unknown>, platform?: string) {
  const requestedPlatform = platform || String(query.platform || "");
  if (
    ["douyin", "x", "tiktok", "weibo", "bilibili", "xhs", "instagram"].includes(requestedPlatform)
  ) {
    return { path: `/settings/account-${requestedPlatform}`, query: {} };
  }
  const tab = String(query.tab || "");
  if (tab && legacySections[tab]) return { path: legacySections[tab], query: {} };
  return { path: "/settings/application", query: {} };
}

function legacyMaintenanceRedirect(query: Record<string, unknown>) {
  const tab = String(query.tab || "");
  return {
    path: legacySections[tab] || "/operations/maintenance/services",
    query: {},
  };
}

export const router = createRouter({
  history: createWebHashHistory(),
  scrollBehavior(to, from, savedPosition) {
    if (savedPosition) return savedPosition;
    if (to.path !== from.path) return { top: 0 };
    return false;
  },
  routes: [
    { path: "/", redirect: "/dashboard" },
    { path: "/dashboard", meta: { title: "总览" }, component: DashboardView },
    { path: "/library", meta: { title: "媒体库" }, component: MediaLibraryView },
    { path: "/insights", meta: { title: "洞察" }, component: InsightsView },
    { path: "/subscriptions", redirect: "/authors/douyin" },
    {
      path: "/authors/instagram",
      meta: { title: "Instagram 订阅" },
      component: InstagramAuthorsView,
    },
    { path: "/operations/tasks", meta: { title: "下载任务" }, component: UnifiedTasksView },
    {
      path: "/operations/maintenance",
      redirect: (to) => legacyMaintenanceRedirect(to.query),
    },
    {
      path: "/operations/maintenance/:section",
      meta: { title: "系统维护" },
      component: SettingsView,
      props: (route) => ({
        mode: "maintenance",
        section: String(route.params.section),
      }),
    },
    { path: "/settings", redirect: (to) => legacySettingsRedirect(to.query) },
    {
      path: "/settings/:section",
      meta: { title: "设置" },
      component: SettingsView,
      props: (route) => ({
        mode: "settings",
        section: String(route.params.section),
      }),
    },
    {
      path: "/douyin/tasks",
      redirect: (to) => ({
        path: "/operations/tasks",
        query: { ...to.query, platform: "douyin" },
      }),
    },
    {
      path: "/authors/:platform(douyin|x)",
      meta: { title: "作者订阅" },
      component: AuthorsView,
      props: true,
    },
    {
      path: "/douyin/authors",
      redirect: (to) => ({ path: "/authors/douyin", query: to.query }),
    },
    { path: "/authors/douyin/:id/works", meta: { title: "作者作品" }, component: WorksView },
    {
      path: "/douyin/authors/:id/works",
      redirect: (to) => ({
        path: `/authors/douyin/${to.params.id}/works`,
        query: to.query,
      }),
    },
    { path: "/automation", meta: { title: "运行记录" }, component: UpdatesView },
    {
      path: "/douyin/updates",
      redirect: (to) => ({ path: "/automation", query: to.query }),
    },
    {
      path: "/douyin/settings",
      redirect: (to) => legacySettingsRedirect(to.query, "douyin"),
    },
    {
      path: "/x/tasks",
      redirect: (to) => ({
        path: "/operations/tasks",
        query: { ...to.query, platform: "x" },
      }),
    },
    {
      path: "/x/authors",
      redirect: (to) => ({ path: "/authors/x", query: to.query }),
    },
    {
      path: "/x/settings",
      redirect: (to) => legacySettingsRedirect(to.query, "x"),
    },
    {
      path: "/tiktok/tasks",
      redirect: (to) => ({
        path: "/operations/tasks",
        query: { ...to.query, platform: "tiktok" },
      }),
    },
    {
      path: "/tiktok/settings",
      redirect: (to) => legacySettingsRedirect(to.query, "tiktok"),
    },
    {
      path: "/weibo/tasks",
      redirect: (to) => ({
        path: "/operations/tasks",
        query: { ...to.query, platform: "weibo" },
      }),
    },
    {
      path: "/weibo/settings",
      redirect: (to) => legacySettingsRedirect(to.query, "weibo"),
    },
    {
      path: "/bilibili/tasks",
      redirect: (to) => ({
        path: "/operations/tasks",
        query: { ...to.query, platform: "bilibili" },
      }),
    },
    {
      path: "/bilibili/settings",
      redirect: (to) => legacySettingsRedirect(to.query, "bilibili"),
    },
    {
      path: "/xhs/tasks",
      redirect: (to) => ({
        path: "/operations/tasks",
        query: { ...to.query, platform: "xhs" },
      }),
    },
    {
      path: "/xhs/settings",
      redirect: (to) => legacySettingsRedirect(to.query, "xhs"),
    },
    { path: "/:pathMatch(.*)*", redirect: "/dashboard" },
  ],
});

router.afterEach((to, _from, failure) => {
  if (failure) return;
  const pages = to.path.startsWith("/settings/") ? settingsPages : maintenancePages;
  const section = pages[String(to.params.section)]?.title;
  document.title = (section || to.meta.title || "总览") + " · 媒体工作台";
});
