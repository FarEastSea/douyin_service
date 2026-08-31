import { createRouter, createWebHashHistory } from 'vue-router'
import TasksView from './views/TasksView.vue'
import AuthorsView from './views/AuthorsView.vue'
import WorksView from './views/WorksView.vue'
import UpdatesView from './views/UpdatesView.vue'
import SettingsView from './views/SettingsView.vue'
import XTasksView from './views/XTasksView.vue'
import XAuthorsView from './views/XAuthorsView.vue'
import PlatformTasksView from './views/PlatformTasksView.vue'

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/douyin/tasks' },
    { path: '/douyin/tasks', component: TasksView },
    { path: '/douyin/authors', component: AuthorsView },
    { path: '/douyin/authors/:id/works', component: WorksView },
    { path: '/douyin/updates', component: UpdatesView },
    { path: '/douyin/settings', component: SettingsView },
    { path: '/x/tasks', component: XTasksView },
    { path: '/x/authors', component: XAuthorsView },
    { path: '/x/settings', component: SettingsView },
    { path: '/tiktok/tasks', component: PlatformTasksView, props: { platform: 'tiktok' } },
    { path: '/tiktok/settings', component: SettingsView },
    { path: '/:pathMatch(.*)*', redirect: '/douyin/tasks' },
  ],
})
