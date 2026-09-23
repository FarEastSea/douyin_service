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

const maintenanceTabs = new Set(['process', 'operations', 'logs', 'about'])
function legacySettingsRedirect(query: Record<string, unknown>) {
  const tab = String(query.tab || 'general')
  return { path: maintenanceTabs.has(tab) ? '/operations/maintenance' : '/settings', query: { ...query, tab } }
}

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/dashboard' },
    { path: '/dashboard', component: DashboardView },
    { path: '/operations/tasks', component: UnifiedTasksView },
    { path: '/operations/maintenance', component: SettingsView, props: { mode: 'maintenance' } },
    { path: '/settings', component: SettingsView, props: { mode: 'settings' } },
    { path: '/douyin/tasks', component: TasksView },
    { path: '/douyin/authors', component: AuthorsView },
    { path: '/douyin/authors/:id/works', component: WorksView },
    { path: '/douyin/updates', component: UpdatesView },
    { path: '/douyin/settings', redirect: to => legacySettingsRedirect(to.query) },
    { path: '/x/tasks', component: XTasksView },
    { path: '/x/authors', component: XAuthorsView },
    { path: '/x/settings', redirect: to => legacySettingsRedirect(to.query) },
    { path: '/tiktok/tasks', component: PlatformTasksView, props: { platform: 'tiktok' } },
    { path: '/tiktok/settings', redirect: to => legacySettingsRedirect(to.query) },
    { path: '/weibo/tasks', component: PlatformTasksView, props: { platform: 'weibo' } },
    { path: '/weibo/settings', redirect: to => legacySettingsRedirect(to.query) },
    { path: '/bilibili/tasks', component: PlatformTasksView, props: { platform: 'bilibili' } },
    { path: '/bilibili/settings', redirect: to => legacySettingsRedirect(to.query) },
    { path: '/xhs/tasks', component: PlatformTasksView, props: { platform: 'xhs' } },
    { path: '/xhs/settings', redirect: to => legacySettingsRedirect(to.query) },
    { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
  ],
})
