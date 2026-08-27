import { defineStore } from 'pinia'
import { api } from '../api'

type Theme = 'auto' | 'light' | 'dark'

export const useAppStore = defineStore('app', {
  state: () => ({
    stats: { total_authors: 0, subscribed_authors: 0, pending_tasks: 0, downloading_tasks: 0, total_downloads: 0 },
    risk: { active: false, retry_after: 0, error_type: null as string | null, reason: null as string | null, last_seen_at: null as string | null, error_type_label: null as string | null, reason_label: null as string | null, requires_cookie_update: false, requires_account_update: false },
    theme: (localStorage.getItem('media-theme') || 'auto') as Theme,
    toast: null as null | { message: string; tone: 'success' | 'error' | 'info' },
    sidebarOpen: false,
    riskTimer: null as number | null,
  }),
  actions: {
    async refreshStatus() {
      await Promise.allSettled([this.refreshStats(), this.refreshRisk()])
    },
    async refreshStats() {
      try { this.stats = await api('/status/stats') } catch { /* auth gate handles it */ }
    },
    async refreshRisk() {
      try { this.risk = await api<any>('/system/douyin-risk-state') } catch { /* keep last known state */ }
    },
    startRiskClock() {
      if (this.riskTimer != null) return
      this.riskTimer = window.setInterval(() => {
        if (this.risk.active && this.risk.retry_after > 0) {
          this.risk.retry_after--
          if (this.risk.retry_after <= 0) void this.refreshRisk()
        }
      }, 1000)
    },
    stopRiskClock() {
      if (this.riskTimer != null) window.clearInterval(this.riskTimer)
      this.riskTimer = null
    },
    notify(message: string, tone: 'success' | 'error' | 'info' = 'success') {
      this.toast = { message, tone }
      window.setTimeout(() => { if (this.toast?.message === message) this.toast = null }, 3600)
    },
    applyTheme() {
      const effective = this.theme === 'auto'
        ? (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark') : this.theme
      document.documentElement.dataset.theme = effective
      localStorage.setItem('media-theme', this.theme)
    },
    cycleTheme() {
      const values: Theme[] = ['auto', 'light', 'dark']
      this.theme = values[(values.indexOf(this.theme) + 1) % values.length]
      this.applyTheme()
    },
  },
})
