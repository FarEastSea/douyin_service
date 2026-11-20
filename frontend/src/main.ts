import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { router } from './router'
import { useAppStore } from './stores/app'
import './styles.css'

const pinia = createPinia()

if (import.meta.env.PROD) {
  const entry = document.querySelector<HTMLScriptElement>('script[type="module"][src]')
  if (entry) {
    const build = new URL(entry.src).pathname
    const refreshKey = '_workspace_refresh'
    const preloadErrors = new Set<unknown>()

    window.addEventListener('vite:preloadError', (event) => {
      preloadErrors.add((event as Event & { payload: unknown }).payload)
    })

    router.onError((error, to) => {
      if (!preloadErrors.delete(error)) return

      const url = new URL(window.location.href)
      if (url.searchParams.get(refreshKey) === build) {
        useAppStore(pinia).notify('页面资源加载失败，请检查网络后刷新页面。', 'error')
        return
      }
      // 请求新的入口页，并保留未能完成的导航。
      url.searchParams.set(refreshKey, build)
      url.hash = to.fullPath
      window.location.replace(url)
    })

    router.afterEach((_to, _from, failure) => {
      if (failure) return
      const url = new URL(window.location.href)
      if (!url.searchParams.has(refreshKey)) return
      url.searchParams.delete(refreshKey)
      window.history.replaceState(window.history.state, '', url)
    })
  }
}

createApp(App).use(pinia).use(router).mount('#app')
