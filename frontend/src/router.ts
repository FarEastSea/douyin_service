import { createRouter, createWebHashHistory } from 'vue-router'
import TasksView from './views/TasksView.vue'
import AuthorsView from './views/AuthorsView.vue'
import WorksView from './views/WorksView.vue'
import UpdatesView from './views/UpdatesView.vue'
import SettingsView from './views/SettingsView.vue'
import XTasksView from './views/XTasksView.vue'
import XAuthorsView from './views/XAuthorsView.vue'
import PlatformTasksView from './views/PlatformTasksView.vue'
import UnifiedTasksView from './views/UnifiedTasksView.vue'
import DashboardView from './views/DashboardView.vue'

const legacySections: Record<string, string> = {
  general: '/settings/application',
  account: '/settings/account-douyin',
  runtime: '/settings/downloads',
  archive: '/settings/archive',
  process: '/operations/maintenance/services',
  operations: '/operations/maintenance/platforms',
  logs: '/operations/maintenance/logs',
  about: '/operations/maintenance/update',
}

function legacySettingsRedirect(query: Record<string, unknown>, platform?: string) {
  const requestedPlatform = platform || String(query.platform || '')
  if (['douyin', 'x', 'tiktok', 'weibo', 'bilibili', 'xhs'].includes(requestedPlatform)) {
    return { path: `/settings/account-${requestedPlatform}`, query: {} }
  }
  const tab = String(query.tab || '')
  if (tab && legacySections[tab]) return { path: legacySections[tab], query: {} }
  return { path: '/settings/application', query: {} }
}

function legacyMaintenanceRedirect(query: Record<string, unknown>) {
  const tab = String(query.tab || '')
  return { path: legacySections[tab] || '/operations/maintenance/services', query: {} }
}

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/dashboard' },
    { path: '/dashboard', component: DashboardView },
    { path: '/operations/tasks', component: UnifiedTasksView },
    { path: '/operations/maintenance', redirect: to => legacyMaintenanceRedirect(to.query) },
    { path: '/operations/maintenance/:section', component: SettingsView, props: route => ({ mode: 'maintenance', section: String(route.params.section) }) },
    { path: '/settings', redirect: to => legacySettingsRedirect(to.query) },
    { path: '/settings/:section', component: SettingsView, props: route => ({ mode: 'settings', section: String(route.params.section) }) },
    { path: '/douyin/tasks', component: TasksView },
    { path: '/douyin/authors', component: AuthorsView },
    { path: '/douyin/authors/:id/works', component: WorksView },
    { path: '/douyin/updates', component: UpdatesView },
    { path: '/douyin/settings', redirect: to => legacySettingsRedirect(to.query, 'douyin') },
    { path: '/x/tasks', component: XTasksView },
    { path: '/x/authors', component: XAuthorsView },
    { path: '/x/settings', redirect: to => legacySettingsRedirect(to.query, 'x') },
    { path: '/tiktok/tasks', component: PlatformTasksView, props: { platform: 'tiktok' } },
    { path: '/tiktok/settings', redirect: to => legacySettingsRedirect(to.query, 'tiktok') },
    { path: '/weibo/tasks', component: PlatformTasksView, props: { platform: 'weibo' } },
    { path: '/weibo/settings', redirect: to => legacySettingsRedirect(to.query, 'weibo') },
    { path: '/bilibili/tasks', component: PlatformTasksView, props: { platform: 'bilibili' } },
    { path: '/bilibili/settings', redirect: to => legacySettingsRedirect(to.query, 'bilibili') },
    { path: '/xhs/tasks', component: PlatformTasksView, props: { platform: 'xhs' } },
    { path: '/xhs/settings', redirect: to => legacySettingsRedirect(to.query, 'xhs') },
    { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
  ],
})
