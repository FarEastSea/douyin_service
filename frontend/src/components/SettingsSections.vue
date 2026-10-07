<script setup lang="ts">
import {
  BellRing,
  ChevronDown,
  Clipboard,
  Cookie,
  HardDrive,
  Menu,
  Play,
  RefreshCw,
  Save,
  ShieldCheck,
  Square,
  Trash2,
  X,
} from "@lucide/vue";
import StatusIndicator from "./StatusIndicator.vue";
import IssueDetail from "./IssueDetail.vue";
import type { SettingsController } from "../composables/useSettingsController";
const componentProps = defineProps<{ controller: SettingsController }>();
const {
  advancedBusy,
  testDatabase,
  archiveRules,
  douyinAccount,
  douyinCookieValue,
  douyinProxyValue,
  xCookieValue,
  platformCookieValues,
  platformCredentialStatus,
  resourceState,
  notificationTestBusy,
  notificationTestChannel,
  notificationTestResult,
  activePage,
  resolvedSections,
  activePlatform,
  activeAccountFacts,
  fieldValue,
  setFieldValue,
  fieldEnabled,
  toggleField,
  displayDateTime,
  saveAndTestNotification,
  toggleArchiveValue,
} = componentProps.controller;
</script>
<template>
  <div class="settings-sections">
    <template
      v-if="
        resolvedSections.length &&
        !(activePage.id === 'account-x' || activePlatform)
      "
    >
      <section
        v-for="section in resolvedSections"
        :key="section.title"
        class="setting-section"
      >
        <header>
          <div>
            <h2>{{ section.title }}</h2>
            <p>{{ section.description }}</p>
          </div>
        </header>
        <div class="setting-list">
          <label
            v-for="field in section.fields"
            :key="field.source + '-' + field.key"
            class="setting-row"
          >
            <span class="setting-copy">
              <strong
                >{{ field.label
                }}<i v-if="field.required" aria-label="必填">必填</i></strong
              >
              <small>{{ field.help || field.key }}</small>
            </span>
            <span class="setting-control">
              <button
                v-if="field.kind === 'boolean'"
                class="setting-switch"
                :class="{ on: fieldEnabled(field) }"
                type="button"
                role="switch"
                :aria-checked="fieldEnabled(field)"
                @click.prevent="toggleField(field)"
              >
                <span class="switch-track"><i /></span>
                <span>{{ fieldEnabled(field) ? "已开启" : "已关闭" }}</span>
              </button>
              <input
                v-else
                :type="field.secret ? 'password' : field.kind"
                :value="fieldValue(field)"
                :min="field.min"
                :max="field.max"
                :placeholder="field.secret ? '留空保持当前值' : undefined"
                :autocomplete="field.secret ? 'new-password' : 'off'"
                @input="
                  setFieldValue(
                    field,
                    ($event.target as HTMLInputElement).value,
                  )
                "
              />
            </span>
          </label>
        </div>
      </section>
    </template>

    <section v-if="activePage.id === 'notifications'" class="setting-section">
      <header>
        <div>
          <h2>发送测试</h2>
          <p>先保存当前子页，再使用服务端实际配置发送一条测试通知。</p>
        </div>
      </header>
      <div class="setting-list">
        <div class="setting-row">
          <span class="setting-copy"
            ><strong>测试渠道</strong
            ><small>测试结果不会包含密钥或带 Token 的完整地址。</small></span
          >
          <span class="setting-control inline-control">
            <select v-model="notificationTestChannel" aria-label="通知测试渠道">
              <option value="all">全部渠道</option>
              <option value="webhook">Webhook</option>
              <option value="bark">Bark</option>
              <option value="email">邮件</option>
              <option value="gotify">Gotify</option>
            </select>
            <button
              class="btn ghost"
              type="button"
              :disabled="notificationTestBusy"
              @click="saveAndTestNotification"
            >
              <BellRing :size="15" />{{
                notificationTestBusy ? "测试中…" : "保存并测试"
              }}
            </button>
          </span>
        </div>
        <div
          v-if="Object.keys(notificationTestResult).length"
          class="notification-result"
        >
          <span
            v-for="(result, channel) in notificationTestResult"
            :key="String(channel)"
            ><strong>{{ channel }}</strong
            >{{ result.success ? "成功" : result.message || "失败" }}</span
          >
        </div>
      </div>
    </section>

    <section v-if="activePage.id === 'account-douyin'" class="account-page">
      <div class="account-summary">
        <span
          class="health-dot"
          :class="{
            online: douyinAccount.configured && douyinAccount.has_uifid,
          }"
        />
        <div>
          <strong>{{ douyinAccount.status_label || "账号状态未确认" }}</strong>
          <p>
            {{
              douyinAccount.cookie_fingerprint
                ? "Cookie 指纹 " + douyinAccount.cookie_fingerprint
                : "Cookie 不回显，更新时粘贴完整值。"
            }}
          </p>
        </div>
        <StatusIndicator
          :status="
            resourceState.douyinAccount.loaded
              ? douyinAccount.configured
                ? 'success'
                : 'warning'
              : undefined
          "
          :label="
            resourceState.douyinAccount.loaded
              ? douyinAccount.configured
                ? '已配置'
                : '未配置'
              : undefined
          "
        />
      </div>
      <section class="setting-section">
        <header>
          <div>
            <h2>账号事实</h2>
            <p>用于判断当前请求上下文是否具备抖音网页接口需要的浏览器身份。</p>
          </div>
        </header>
        <dl class="fact-list">
          <div>
            <dt>UIFID</dt>
            <dd>
              {{
                !resourceState.douyinAccount.loaded
                  ? "未确认"
                  : douyinAccount.has_uifid
                    ? "已包含"
                    : "缺失"
              }}
            </dd>
          </div>
          <div>
            <dt>最近成功</dt>
            <dd>{{ displayDateTime(douyinAccount.last_success_at) }}</dd>
          </div>
          <div>
            <dt>代理</dt>
            <dd>
              {{
                !resourceState.douyinAccount.loaded
                  ? "未确认"
                  : douyinAccount.proxy_enabled
                    ? douyinAccount.proxy_label || "已启用"
                    : "未启用"
              }}
            </dd>
          </div>
        </dl>
      </section>
      <section class="setting-section">
        <header>
          <div>
            <h2>请求凭据</h2>
            <p>Cookie 和代理地址留空表示不修改已保存的加密值。</p>
          </div>
        </header>
        <div class="setting-list">
          <label class="setting-row"
            ><span class="setting-copy"
              ><strong>抖音 Cookie</strong
              ><small>粘贴浏览器请求头中的完整 Cookie 字符串。</small></span
            ><span class="setting-control">
              <textarea
                v-model="douyinCookieValue"
                rows="4"
                placeholder="留空保持当前 Cookie"
              /></span
          ></label>
          <label class="setting-row"
            ><span class="setting-copy"
              ><strong>User-Agent</strong
              ><small>应与获取 Cookie 的浏览器保持一致。</small></span
            ><span class="setting-control"
              ><input
                v-model="douyinAccount.user_agent"
                autocomplete="off" /></span
          ></label>
          <div class="setting-row">
            <span class="setting-copy"
              ><strong>使用代理</strong
              ><small
                >Cookie、UIFID、User-Agent 和代理共同组成请求上下文。</small
              ></span
            ><span class="setting-control"
              ><button
                class="setting-switch"
                :class="{ on: douyinAccount.proxy_enabled }"
                type="button"
                role="switch"
                :aria-checked="Boolean(douyinAccount.proxy_enabled)"
                @click="
                  douyinAccount.proxy_enabled = !douyinAccount.proxy_enabled
                "
              >
                <span class="switch-track"><i /></span
                ><span>{{
                  douyinAccount.proxy_enabled ? "已开启" : "已关闭"
                }}</span>
              </button></span
            >
          </div>
          <label v-if="douyinAccount.proxy_enabled" class="setting-row"
            ><span class="setting-copy"
              ><strong>代理地址</strong
              ><small>留空保持当前加密代理地址。</small></span
            ><span class="setting-control"
              ><input
                v-model="douyinProxyValue"
                type="password"
                autocomplete="new-password"
                placeholder="留空保持当前值" /></span
          ></label>
        </div>
      </section>
    </section>

    <section
      v-if="activePage.id === 'account-x' || activePlatform"
      class="account-page"
    >
      <div class="account-summary">
        <Cookie :size="19" />
        <div>
          <strong>{{ activePage.title }}</strong>
          <p v-if="activePage.id === 'account-x'">
            Cookie 用于 gallery-dl 访问需要登录的 X 内容。
          </p>
          <p v-else>
            {{
              !resourceState.platformCredentials.loaded
                ? "凭据状态未确认"
                : platformCredentialStatus[activePlatform?.id || ""]?.configured
                  ? "网页加密 Cookie 已配置。"
                  : "当前尚未配置网页加密 Cookie。"
            }}
          </p>
        </div>
        <StatusIndicator
          :status="
            activePage.id === 'account-x'
              ? resourceState.all.loaded
                ? 'neutral'
                : undefined
              : resourceState.platformCredentials.loaded
                ? platformCredentialStatus[activePlatform?.id || '']?.configured
                  ? 'success'
                  : 'warning'
                : undefined
          "
          :label="
            activePage.id === 'account-x' && resourceState.all.loaded
              ? '独立凭据'
              : resourceState.platformCredentials.loaded
                ? platformCredentialStatus[activePlatform?.id || '']?.configured
                  ? '已配置'
                  : '未配置'
                : undefined
          "
        />
      </div>
      <section class="setting-section">
        <header>
          <div>
            <h2>账号事实</h2>
            <p>核对当前下载引擎、文件凭据和网页凭据状态。</p>
          </div>
        </header>
        <dl class="fact-list">
          <div v-for="fact in activeAccountFacts" :key="fact.label">
            <dt>{{ fact.label }}</dt>
            <dd>{{ fact.value }}</dd>
          </div>
        </dl>
      </section>
      <section
        v-for="section in resolvedSections"
        :key="section.title"
        class="setting-section"
      >
        <header>
          <div>
            <h2>{{ section.title }}</h2>
            <p>{{ section.description }}</p>
          </div>
        </header>
        <div class="setting-list">
          <label
            v-for="field in section.fields"
            :key="field.source + '-' + field.key"
            class="setting-row"
          >
            <span class="setting-copy"
              ><strong
                >{{ field.label
                }}<i v-if="field.required" aria-label="必填">必填</i></strong
              ><small>{{ field.help || field.key }}</small></span
            >
            <span class="setting-control">
              <button
                v-if="field.kind === 'boolean'"
                class="setting-switch"
                :class="{ on: fieldEnabled(field) }"
                type="button"
                role="switch"
                :aria-checked="fieldEnabled(field)"
                @click.prevent="toggleField(field)"
              >
                <span class="switch-track"><i /></span
                ><span>{{ fieldEnabled(field) ? "已开启" : "已关闭" }}</span>
              </button>
              <input
                v-else
                :type="field.secret ? 'password' : field.kind"
                :value="fieldValue(field)"
                :min="field.min"
                :max="field.max"
                :placeholder="field.secret ? '留空保持当前值' : undefined"
                :autocomplete="field.secret ? 'new-password' : 'off'"
                @input="
                  setFieldValue(
                    field,
                    ($event.target as HTMLInputElement).value,
                  )
                "
              />
            </span>
          </label>
        </div>
      </section>
      <section class="setting-section">
        <header>
          <div>
            <h2>登录凭据</h2>
            <p>敏感值单独加密保存，不会通过配置接口回显。</p>
          </div>
        </header>
        <div class="setting-list">
          <label class="setting-row">
            <span class="setting-copy"
              ><strong>{{
                activePage.id === "account-x"
                  ? "X Cookie"
                  : activePlatform?.name + " Cookie"
              }}</strong
              ><small
                >支持 Cookie Header String；留空表示保持现有值。</small
              ></span
            >
            <span class="setting-control">
              <textarea
                v-if="activePage.id === 'account-x'"
                v-model="xCookieValue"
                rows="5"
                placeholder="留空保持当前 Cookie"
              /><textarea
                v-else
                v-model="platformCookieValues[activePlatform!.id]"
                rows="5"
                placeholder="留空保持当前 Cookie"
              />
            </span>
          </label>
        </div>
      </section>
      <section
        v-if="activePage.id === 'account-x' || activePlatform?.id === 'xhs'"
        class="capability-note"
      >
        <ShieldCheck :size="18" />
        <div>
          <strong>{{
            activePlatform?.id === "xhs"
              ? "仅支持单条笔记"
              : "支持作者主页与单条作品"
          }}</strong>
          <p>
            {{
              activePlatform?.id === "xhs"
                ? "小红书作者主页批量采集已搁置；这里的凭据仅用于单条图文、视频和实况笔记。"
                : "X 的作者主页与单条作品沿用同一套下载引擎和凭据。"
            }}
          </p>
        </div>
      </section>
    </section>

    <section v-if="activePage.id === 'archive'" class="archive-page">
      <section class="setting-section">
        <header>
          <div>
            <h2>路径与文件名</h2>
            <p>规则在创建任务时固化，不影响已排队任务。</p>
          </div>
        </header>
        <div class="setting-list">
          <label class="setting-row"
            ><span class="setting-copy"
              ><strong>目录模板</strong
              ><small
                >可用：{author} {published_date} {year} {month}
                {work_type}</small
              ></span
            ><span class="setting-control"
              ><input v-model="archiveRules.directory_template" /></span
          ></label>
          <label class="setting-row"
            ><span class="setting-copy"
              ><strong>文件名模板</strong
              ><small>必须包含作品 ID、序号后缀和扩展名变量。</small></span
            ><span class="setting-control"
              ><input v-model="archiveRules.filename_template" /></span
          ></label>
        </div>
      </section>
      <section class="setting-section">
        <header>
          <div>
            <h2>内容范围</h2>
            <p>筛选新任务允许归档的作品类型、发布时间和文件大小。</p>
          </div>
        </header>
        <div class="setting-list">
          <div class="setting-row">
            <span class="setting-copy"
              ><strong>作品类型</strong
              ><small>至少保留一种需要下载的媒体类型。</small></span
            ><span class="setting-control inline-control"
              ><button
                v-for="item in [
                  { id: 'video', label: '视频' },
                  { id: 'images', label: '图集' },
                ]"
                :key="item.id"
                class="choice-button"
                :class="{ active: archiveRules.work_types?.includes(item.id) }"
                type="button"
                :aria-pressed="archiveRules.work_types?.includes(item.id)"
                @click="toggleArchiveValue('work_types', item.id)"
              >
                {{ item.label }}
              </button></span
            >
          </div>
          <div class="setting-row">
            <span class="setting-copy"
              ><strong>发布时间</strong><small>留空表示不限制。</small></span
            ><span class="setting-control paired-control"
              ><input
                v-model="archiveRules.published_from"
                type="date"
                aria-label="发布时间起" /><input
                v-model="archiveRules.published_to"
                type="date"
                aria-label="发布时间止"
            /></span>
          </div>
          <div class="setting-row">
            <span class="setting-copy"
              ><strong>文件大小（MB）</strong
              ><small>0 表示不限制。</small></span
            ><span class="setting-control paired-control"
              ><input
                v-model.number="archiveRules.min_file_size_mb"
                type="number"
                min="0"
                step="0.1"
                aria-label="最小文件大小" /><input
                v-model.number="archiveRules.max_file_size_mb"
                type="number"
                min="0"
                step="0.1"
                aria-label="最大文件大小"
            /></span>
          </div>
          <div class="setting-row">
            <span class="setting-copy"
              ><strong>同目录元数据</strong
              ><small>在每个媒体文件旁生成独立元数据文件。</small></span
            ><span class="setting-control inline-control"
              ><button
                v-for="item in ['json', 'csv']"
                :key="item"
                class="choice-button"
                :class="{
                  active: archiveRules.metadata_formats?.includes(item),
                }"
                type="button"
                :aria-pressed="archiveRules.metadata_formats?.includes(item)"
                @click="toggleArchiveValue('metadata_formats', item)"
              >
                {{ item.toUpperCase() }}
              </button></span
            >
          </div>
        </div>
      </section>
    </section>
    <section v-if="activePage.id === 'connections'" class="setting-section">
      <header><h2>连接验证</h2></header>
      <p class="inline-note">
        使用当前表单连接参数验证数据库。测试不会保存修改；密码留空时使用已有值。
      </p>
      <button
        class="btn"
        :disabled="advancedBusy || !resourceState.all.loaded"
        @click="testDatabase"
      >
        测试数据库连接
      </button>
    </section>
  </div>
</template>
