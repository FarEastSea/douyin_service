<script setup lang="ts">
import { Clipboard, Cookie, Play, Square, Trash2 } from "@lucide/vue";
import ExclusionMaintenance from "./ExclusionMaintenance.vue";
import StorageMaintenance from "./StorageMaintenance.vue";
import type { SettingsController } from "../composables/useSettingsController";
const componentProps = defineProps<{ controller: SettingsController }>();
const {
  advancedDiagnostics,
  advancedBusy,
  advancedError,
  loadAdvanced,
  advancedAction,
  resourceState,
  logLevels,
  live,
  process,
  readiness,
  platformReadiness,
  updateInfo,
  diagnostic,
  updateBusy,
  activePage,
  filteredLogs,
  readinessLabel,
  sourceLabel,
  displayDateTime,
  processAction,
  clearLogs,
  copyLogs,
  checkUpdate,
  diagnoseUpdate,
  toggleLevel,
} = componentProps.controller;
</script>
<template>
  <div class="settings-sections">
    <section
      v-if="activePage.id === 'services'"
      class="maintenance-page"
    >
      <section class="setting-section">
        <header>
          <div>
            <h2>依赖状态</h2>
            <p>Web、数据库、Redis 和后台任务的当前可用性。</p>
          </div>
        </header>
        <div class="status-list">
          <div
            v-for="(component, name) in readiness.components"
            :key="String(name)"
            class="status-row"
          >
            <span
              class="health-dot"
              :class="{ online: component.ok }"
            />
            <span>
              <strong>{{ readinessLabel(String(name)) }}</strong>
              <small>{{ component.message }}</small>
            </span>
            <b :class="{ good: component.ok }">{{ component.ok ? "正常" : "异常" }}</b>
          </div>
        </div>
      </section>
      <section class="setting-section">
        <header>
          <div>
            <h2>后台进程</h2>
            <p>启动和停止动作继续使用现有进程管理方式。</p>
          </div>
        </header>
        <div class="status-list">
          <div
            v-for="target in ['worker', 'beat']"
            :key="target"
            class="status-row"
          >
            <span
              class="health-dot"
              :class="{ online: process?.[target]?.running }"
            />
            <span>
              <strong>{{ target === "worker" ? "下载 Worker" : "定时调度 Beat" }}</strong>
              <small>
                {{
                  !process?.[target]
                    ? "状态未确认"
                    : process[target].running
                      ? "运行中 · PID " + (process[target].pid || "—")
                      : "已停止"
                }}
              </small>
            </span>
            <span class="row-buttons">
              <button
                class="btn ghost compact"
                type="button"
                :disabled="!process?.[target]"
                @click="processAction(target as any, 'start')"
              >
                <Play :size="14" />
                启动
              </button>
              <button
                class="btn ghost compact"
                type="button"
                :disabled="!process?.[target]"
                @click="processAction(target as any, 'stop')"
              >
                <Square :size="14" />
                停止
              </button>
            </span>
          </div>
        </div>
      </section>
    </section>

    <section
      v-else-if="activePage.id === 'platforms'"
      class="maintenance-page"
    >
      <section class="setting-section">
        <header>
          <div>
            <h2>平台就绪状态</h2>
            <p>
              当前运行版本
              {{
                platformReadiness.revision
                  ? String(platformReadiness.revision).slice(0, 12)
                  : "未确认"
              }}；真实成功记录必须同时通过本地文件核验。
            </p>
          </div>
        </header>
        <div
          class="platform-table"
          role="table"
          aria-label="平台就绪状态"
        >
          <div
            class="platform-table-head"
            role="row"
          >
            <span role="columnheader">平台</span>
            <span role="columnheader">能力与引擎</span>
            <span role="columnheader">依赖</span>
            <span role="columnheader">验收</span>
          </div>
          <div
            v-for="item in platformReadiness.items"
            :key="item.platform"
            class="platform-table-row"
            role="row"
          >
            <span role="cell">
              <strong>{{ item.name }}</strong>
              <small>
                {{
                  item.status === "ready"
                    ? "本地就绪"
                    : item.status === "degraded"
                      ? "可用但待完善"
                      : "阻塞"
                }}
              </small>
            </span>
            <span role="cell">
              <strong>{{ item.engine }}</strong>
              <small>
                {{ item.supported_sources.map(sourceLabel).join(" / ") || "未开放下载" }}
              </small>
            </span>
            <span role="cell">
              <small>
                Cookie {{ item.cookie_configured ? "已配置" : "未配置" }} · FFmpeg
                {{ item.ffmpeg_ready ? "可用" : "未安装" }} · 目录{{
                  item.download_root.writable ? "可写" : "不可写"
                }}
              </small>
            </span>
            <span role="cell">
              <strong>
                {{
                  item.external_validation_complete
                    ? "已完成真实验收"
                    : item.external_tested
                      ? "已有记录，待补齐"
                      : "尚无真实成功记录"
                }}
              </strong>
              <small>{{ displayDateTime(item.last_external_success_at) }}</small>
            </span>
            <ul v-if="item.blockers.length || item.warnings.length">
              <li
                v-for="message in [...item.blockers, ...item.warnings]"
                :key="message"
              >
                {{ message }}
              </li>
            </ul>
          </div>
        </div>
      </section>
    </section>

    <StorageMaintenance
      v-else-if="activePage.id === 'storage'"
      :controller="controller"
    />
    <ExclusionMaintenance v-else-if="activePage.id === 'exclusions'" />
    <section
      v-else-if="activePage.id === 'logs'"
      class="maintenance-page"
    >
      <section class="setting-section">
        <header>
          <div>
            <h2>活动日志</h2>
            <p>保留可操作事件、失败证据和关联请求，高频周期任务不会逐次刷屏。</p>
          </div>
          <div class="row-buttons">
            <button
              class="setting-switch"
              :class="{ on: live }"
              type="button"
              role="switch"
              :aria-checked="live"
              @click="live = !live"
            >
              <span class="switch-track"><i /></span>
              <span>实时刷新</span>
            </button>
            <button
              class="btn ghost compact"
              type="button"
              :disabled="!resourceState.logs.loaded"
              @click="copyLogs"
            >
              <Clipboard :size="14" />
              复制
            </button>
            <button
              class="btn ghost compact"
              type="button"
              :disabled="!resourceState.logs.loaded"
              @click="clearLogs"
            >
              <Trash2 :size="14" />
              清空
            </button>
          </div>
        </header>
        <div
          class="log-filters"
          aria-label="日志级别筛选"
        >
          <button
            v-for="level in ['info', 'warning', 'error']"
            :key="level"
            type="button"
            :class="{ active: logLevels.includes(level) }"
            :aria-pressed="logLevels.includes(level)"
            @click="toggleLevel(level)"
          >
            {{ level }}
          </button>
        </div>
        <div class="log-console">
          <article
            v-for="(item, index) in filteredLogs"
            :key="String(item.ts) + '-' + index"
            :data-level="item.level"
          >
            <time>{{ new Date(item.ts * 1000).toLocaleString() }}</time>
            <b>
              [{{ item.source }}]
              <template v-if="item.event_code">[{{ item.event_code }}]</template>
            </b>
            <span>{{ item.msg }}</span>
            <small v-if="item.detail">{{ item.detail }}</small>
            <small v-if="item.correlation_id || Object.keys(item.context || {}).length">
              {{ item.correlation_id ? "请求 " + item.correlation_id : ""
              }}{{
                Object.keys(item.context || {}).length ? " · " + JSON.stringify(item.context) : ""
              }}
            </small>
          </article>
          <div
            v-if="resourceState.logs.loaded && !filteredLogs.length"
            class="empty-state"
          >
            暂无符合筛选条件的日志
          </div>
        </div>
      </section>
    </section>

    <section
      v-else-if="activePage.id === 'update'"
      class="maintenance-page"
    >
      <section class="setting-section">
        <header>
          <div>
            <h2>版本与更新</h2>
            <p>{{ updateInfo.message || "正在读取本地版本信息" }}</p>
          </div>
        </header>
        <dl class="fact-list">
          <div>
            <dt>当前版本</dt>
            <dd>{{ updateInfo.current?.short || "—" }}</dd>
          </div>
          <div>
            <dt>分支</dt>
            <dd>{{ updateInfo.branch || "—" }}</dd>
          </div>
          <div>
            <dt>更新状态</dt>
            <dd>
              {{ updateInfo.has_update ? "有可用更新" : "当前已是最新或尚未检查" }}
            </dd>
          </div>
        </dl>
        <div class="storage-actions">
          <button
            class="btn ghost"
            type="button"
            :disabled="updateBusy"
            @click="checkUpdate"
          >
            检查更新
          </button>
          <button
            class="btn ghost"
            type="button"
            @click="diagnoseUpdate"
          >
            复制诊断
          </button>
        </div>
        <p class="inline-note">
          版本安装统一通过现有 Jenkins
          发布流程执行，完成后刷新页面即可。发布会先排空任务、备份数据库，再切换版本。
        </p>
      </section>
      <section class="setting-section">
        <header>
          <div>
            <h2>系统入口</h2>
            <p>FastAPI · PostgreSQL · Redis · Celery · Vue 3</p>
          </div>
        </header>
        <div class="storage-actions">
          <a
            class="btn ghost"
            href="/docs"
            target="_blank"
            rel="noopener"
          >
            API 文档
          </a>
          <a
            class="btn ghost"
            href="/legacy"
          >
            旧版界面
          </a>
        </div>
        <pre
          v-if="diagnostic"
          class="diagnostic-box"
          >{{ JSON.stringify(diagnostic, null, 2) }}</pre>
      </section>
    </section>
    <details
      v-if="activePage.id === 'services'"
      class="diagnostic-box"
      @toggle="($event.target as HTMLDetailsElement).open && !advancedDiagnostics && loadAdvanced()"
    >
      <summary>高级诊断与恢复</summary>
      <p class="inline-note">查看后台队列、Worker 日志或使用服务恢复操作。</p>
      <div class="storage-actions">
        <button
          class="btn"
          :disabled="advancedBusy"
          @click="loadAdvanced"
        >
          刷新诊断
        </button>
        <button
          class="btn"
          :disabled="advancedBusy"
          @click="advancedAction('celery-test')"
        >
          投递测试任务
        </button>
        <button
          class="btn danger"
          :disabled="advancedBusy"
          @click="advancedAction('celery-purge-old')"
        >
          清理旧队列
        </button>
        <button
          class="btn"
          :disabled="advancedBusy"
          @click="advancedAction('service/restart')"
        >
          重启 Web 服务
        </button>
      </div>
      <p
        v-if="advancedError"
        role="alert"
      >
        {{ advancedError }}
      </p>
      <pre v-if="advancedDiagnostics">{{ JSON.stringify(advancedDiagnostics, null, 2) }}</pre>
    </details>
  </div>
</template>
