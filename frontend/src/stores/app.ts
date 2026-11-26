import { defineStore } from "pinia";
import { api } from "../api";
import type { MediaPlatform } from "../types";

type Theme = "auto" | "light" | "dark";

declare global {
  interface Window {
    applyMediaTheme(preference: Theme): "dark" | "light";
  }
}
function savedTheme(): Theme {
  try {
    const value = localStorage.getItem("media-theme");
    if (value === "light" || value === "dark") return value;
  } catch {
    /* Storage can be disabled. */
  }
  return "auto";
}
let session = new AbortController();
let riskRequest: Promise<void> | null = null;
let healthRequest: Promise<void> | null = null;
export const useAppStore = defineStore("app", {
  state: () => ({
    stats: {
      total_authors: 0,
      subscribed_authors: 0,
      pending_tasks: 0,
      downloading_tasks: 0,
      total_downloads: 0,
    },
    platforms: null as MediaPlatform[] | null,
    risk: {
      active: false,
      retry_after: 0,
      error_type: null as string | null,
      reason: null as string | null,
      last_seen_at: null as string | null,
      error_type_label: null as string | null,
      reason_label: null as string | null,
      requires_cookie_update: false,
      requires_account_update: false,
    },
    theme: savedTheme(),
    effectiveTheme: document.documentElement.dataset.theme === "light" ? "light" : "dark",
    authenticated: false,
    statsKnown: false,
    readiness: "unknown" as "ready" | "not_ready" | "unknown",
    riskState: "pending" as "pending" | "ready" | "failed",
    riskUntil: 0,
    nextHealthAt: 0,
    resumedAt: 0,
    pollTick: 0,
    pollTimer: null as number | null,
    toast: null as null | { message: string; tone: "success" | "error" | "info" },
    sidebarOpen: false,
  }),
  actions: {
    async refreshStatus() {
      await Promise.allSettled([this.refreshPlatforms(), this.refreshStats(), this.refreshRisk()]);
    },
    async refreshPlatforms() {
      const { signal } = session;
      try {
        const { items } = await api<{ items: MediaPlatform[] }>("/platforms", { signal });
        if (!signal.aborted && Array.isArray(items)) this.platforms = items;
      } catch {
        /* Keep the last catalog available offline. */
      }
    },
    async refreshStats() {
      const { signal } = session;
      try {
        const stats = await api<typeof this.stats>("/status/stats", { signal });
        if (!signal.aborted) {
          this.stats = stats;
          this.statsKnown = true;
        }
      } catch {
        /* Keep the last summary. */
      }
    },
    refreshRisk() {
      if (riskRequest) return riskRequest;
      const { signal } = session;
      riskRequest = api<typeof this.risk>("/system/douyin-risk-state", { signal })
        .then((risk) => {
          if (signal.aborted) return;
          this.risk = risk;
          this.riskUntil =
            risk.active && risk.retry_after > 0 ? Date.now() + risk.retry_after * 1000 : 0;
          this.riskState = "ready";
        })
        .catch(() => {
          if (!signal.aborted) this.riskState = "failed";
        })
        .finally(() => {
          if (!signal.aborted) riskRequest = null;
        });
      return riskRequest;
    },
    async refreshReadiness() {
      const { signal } = session;
      try {
        const data = await api<{ ready: boolean }>("/status/readiness", { signal });
        if (!signal.aborted) this.readiness = data.ready ? "ready" : "not_ready";
      } catch {
        if (!signal.aborted) this.readiness = "unknown";
      }
    },
    refreshHealth() {
      if (healthRequest) return healthRequest;
      const { signal } = session;
      this.nextHealthAt = Date.now() + 30_000;
      healthRequest = Promise.all([
        this.refreshRisk(),
        this.refreshStats(),
        this.refreshReadiness(),
      ])
        .then(() => undefined)
        .finally(() => {
          if (!signal.aborted) healthRequest = null;
        });
      return healthRequest;
    },
    poll(resumed = false) {
      if (document.hidden || !this.authenticated) return;
      const now = Date.now();
      this.pollTick = now;
      if (resumed) this.resumedAt = now;
      if (this.riskUntil) {
        this.risk.retry_after = Math.max(0, Math.ceil((this.riskUntil - now) / 1000));
        if (!this.risk.retry_after) {
          this.riskUntil = 0;
          void this.refreshRisk();
        }
      }
      if (resumed || now >= this.nextHealthAt) void this.refreshHealth();
    },
    startPolling() {
      if (this.pollTimer !== null) return;
      this.pollTimer = window.setInterval(() => this.poll(), 1000);
      void this.refreshHealth();
      void this.refreshPlatforms();
    },
    stopPolling() {
      if (this.pollTimer !== null) window.clearInterval(this.pollTimer);
      this.pollTimer = null;
      session.abort();
      session = new AbortController();
      healthRequest = riskRequest = null;
    },
    notify(message: string, tone: "success" | "error" | "info" = "success") {
      this.toast = { message, tone };
      const notification = this.toast;
      if (tone !== "error")
        window.setTimeout(() => {
          if (this.toast === notification) this.toast = null;
        }, 6000);
    },
    applyTheme() {
      this.effectiveTheme = window.applyMediaTheme(this.theme);
    },
    setTheme(theme: Theme) {
      this.theme = theme;
      this.applyTheme();
      try {
        localStorage.setItem("media-theme", theme);
      } catch {
        /* Use the preference for this session. */
      }
    },
  },
});
