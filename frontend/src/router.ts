import { createRouter, createWebHashHistory } from "vue-router";
const AuthorsView = () => import("./views/AuthorsView.vue");
const WorksView = () => import("./views/WorksView.vue");
const UpdatesView = () => import("./views/UpdatesView.vue");
const SettingsView = () => import("./views/SettingsView.vue");
const UnifiedTasksView = () => import("./views/UnifiedTasksView.vue");
const DashboardView = () => import("./views/DashboardView.vue");

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

function legacySettingsRedirect(
  query: Record<string, unknown>,
  platform?: string,
) {
  const requestedPlatform = platform || String(query.platform || "");
  if (
    ["douyin", "x", "tiktok", "weibo", "bilibili", "xhs"].includes(
      requestedPlatform,
    )
  ) {
    return { path: `/settings/account-${requestedPlatform}`, query: {} };
  }
  const tab = String(query.tab || "");
  if (tab && legacySections[tab])
    return { path: legacySections[tab], query: {} };
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
  routes: [
    { path: "/", redirect: "/dashboard" },
    { path: "/dashboard", component: DashboardView },
    { path: "/operations/tasks", component: UnifiedTasksView },
    {
      path: "/operations/maintenance",
      redirect: (to) => legacyMaintenanceRedirect(to.query),
    },
    {
      path: "/operations/maintenance/:section",
      component: SettingsView,
      props: (route) => ({
        mode: "maintenance",
        section: String(route.params.section),
      }),
    },
    { path: "/settings", redirect: (to) => legacySettingsRedirect(to.query) },
    {
      path: "/settings/:section",
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
      component: AuthorsView,
      props: true,
    },
    {
      path: "/douyin/authors",
      redirect: (to) => ({ path: "/authors/douyin", query: to.query }),
    },
    { path: "/authors/douyin/:id/works", component: WorksView },
    {
      path: "/douyin/authors/:id/works",
      redirect: (to) => ({
        path: `/authors/douyin/${to.params.id}/works`,
        query: to.query,
      }),
    },
    { path: "/automation", component: UpdatesView },
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
