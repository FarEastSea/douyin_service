<script setup lang="ts">
import { durationHint } from "../settings/duration";
import type { SettingsController } from "../composables/useSettingsController";
import { Cookie, ShieldCheck, X } from "@lucide/vue";
import StatusIndicator from "./StatusIndicator.vue";
import InstagramConnectionPanel from "./InstagramConnectionPanel.vue";
const props = defineProps<{ controller: SettingsController }>();
const {
  douyinAccount,
  douyinCookieValue,
  douyinProxyValue,
  xCookieValue,
  platformCookieValues,
  platformCredentialStatus,
  resourceState,
  fieldErrors,
  fieldDirty,
  activePage,
  resolvedSections,
  activePlatform,
  activeAccountFacts,
  fieldValue,
  setFieldValue,
  fieldEnabled,
  toggleField,
  displayDateTime,
} = props.controller;
</script>
<template>
  <section
    v-if="activePage.id === 'account-douyin'"
    class="account-page"
  >
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
        <label class="setting-row">
          <span class="setting-copy">
            <strong>抖音 Cookie</strong>
            <small>粘贴浏览器请求头中的完整 Cookie 字符串。</small>
          </span>
          <span class="setting-control">
            <textarea
              id="setting-cookie"
              :aria-invalid="Boolean(fieldErrors.cookie) || undefined"
              :aria-describedby="fieldErrors.cookie ? 'setting-error-cookie' : undefined"
              @input="delete fieldErrors.cookie"
              v-model="douyinCookieValue"
              rows="4"
              placeholder="留空保持当前 Cookie"
            />
            <span
              v-if="fieldErrors.cookie"
              id="setting-error-cookie"
              class="field-error"
            >
              {{ fieldErrors.cookie }}
            </span>
          </span>
        </label>
        <label class="setting-row">
          <span class="setting-copy">
            <strong>User-Agent</strong>
            <small>应与获取 Cookie 的浏览器保持一致。</small>
          </span>
          <span class="setting-control">
            <input
              id="setting-user_agent"
              :aria-invalid="Boolean(fieldErrors.user_agent) || undefined"
              :aria-describedby="fieldErrors.user_agent ? 'setting-error-user_agent' : undefined"
              @input="delete fieldErrors.user_agent"
              v-model="douyinAccount.user_agent"
              autocomplete="off"
            />
            <span
              v-if="fieldErrors.user_agent"
              id="setting-error-user_agent"
              class="field-error"
            >
              {{ fieldErrors.user_agent }}
            </span>
          </span>
        </label>
        <div class="setting-row">
          <span class="setting-copy">
            <strong>使用代理</strong>
            <small>Cookie、UIFID、User-Agent 和代理共同组成请求上下文。</small>
          </span>
          <span class="setting-control">
            <button
              class="setting-switch"
              :class="{ on: douyinAccount.proxy_enabled }"
              type="button"
              role="switch"
              :aria-checked="Boolean(douyinAccount.proxy_enabled)"
              @click="douyinAccount.proxy_enabled = !douyinAccount.proxy_enabled"
            >
              <span class="switch-track"><i /></span>
              <span>{{ douyinAccount.proxy_enabled ? "已开启" : "已关闭" }}</span>
            </button>
          </span>
        </div>
        <label
          v-if="douyinAccount.proxy_enabled"
          class="setting-row"
        >
          <span class="setting-copy">
            <strong>代理地址</strong>
            <small>留空保持当前加密代理地址。</small>
          </span>
          <span class="setting-control">
            <input
              id="setting-proxy_url"
              :aria-invalid="Boolean(fieldErrors.proxy_url) || undefined"
              :aria-describedby="fieldErrors.proxy_url ? 'setting-error-proxy_url' : undefined"
              @input="delete fieldErrors.proxy_url"
              v-model="douyinProxyValue"
              type="password"
              autocomplete="new-password"
              placeholder="留空保持当前值"
            />
            <span
              v-if="fieldErrors.proxy_url"
              id="setting-error-proxy_url"
              class="field-error"
            >
              {{ fieldErrors.proxy_url }}
            </span>
          </span>
        </label>
      </div>
    </section>
  </section>

  <InstagramConnectionPanel
    v-if="activePlatform?.id === 'instagram'"
    :cookie="platformCookieValues.instagram || ''"
    :saving="controller.saving.value"
    @update:cookie="platformCookieValues.instagram = $event"
    @saved="controller.refreshCredentialStatus()"
  />
  <section
    v-if="activePage.id === 'account-x' || activePlatform"
    class="account-page"
  >
    <div
      v-if="activePlatform?.id !== 'instagram'"
      class="account-summary"
    >
      <Cookie :size="19" />
      <div>
        <strong>{{ activePage.title }}</strong>
        <p v-if="activePage.id === 'account-x'">Cookie 用于 gallery-dl 访问需要登录的 X 内容。</p>
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
    <section
      v-if="activePlatform?.id !== 'instagram'"
      class="setting-section"
    >
      <header>
        <div>
          <h2>账号事实</h2>
          <p>核对当前下载引擎、文件凭据和网页凭据状态。</p>
        </div>
      </header>
      <dl class="fact-list">
        <div
          v-for="fact in activeAccountFacts"
          :key="fact.label"
        >
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
          <span class="setting-copy">
            <strong>
              {{ field.label }}
              <i
                v-if="field.required"
                aria-label="必填"
              >
                必填
              </i>
              <span
                v-if="fieldDirty(field)"
                class="field-dirty"
              >
                已修改
              </span>
            </strong>
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
              :id="'setting-' + field.key"
              :aria-invalid="Boolean(fieldErrors[field.key]) || undefined"
              :aria-describedby="fieldErrors[field.key] ? 'setting-error-' + field.key : undefined"
              :value="fieldValue(field)"
              :min="field.min"
              :max="field.max"
              :placeholder="field.secret ? '留空保持当前值' : undefined"
              :autocomplete="field.secret ? 'new-password' : 'off'"
              @input="setFieldValue(field, ($event.target as HTMLInputElement).value)"
            />
            <small
              v-if="field.kind === 'number' && durationHint(field.label, fieldValue(field))"
              class="field-help"
            >
              {{ durationHint(field.label, fieldValue(field)) }}
            </small>
            <span
              v-if="fieldErrors[field.key]"
              :id="'setting-error-' + field.key"
              class="field-error"
            >
              {{ fieldErrors[field.key] }}
            </span>
          </span>
        </label>
      </div>
    </section>
    <section
      v-if="activePlatform?.id !== 'instagram'"
      class="setting-section"
    >
      <header>
        <div>
          <h2>登录凭据</h2>
          <p>敏感值单独加密保存，不会通过配置接口回显。</p>
        </div>
      </header>
      <div class="setting-list">
        <label class="setting-row">
          <span class="setting-copy">
            <strong>
              {{ activePage.id === "account-x" ? "X Cookie" : activePlatform?.name + " Cookie" }}
            </strong>
            <small>支持 Cookie Header String；留空表示保持现有值。</small>
          </span>
          <span class="setting-control">
            <textarea
              id="setting-cookie"
              :aria-invalid="Boolean(fieldErrors.cookie) || undefined"
              :aria-describedby="fieldErrors.cookie ? 'setting-error-cookie' : undefined"
              @input="delete fieldErrors.cookie"
              v-if="activePage.id === 'account-x'"
              v-model="xCookieValue"
              rows="5"
              placeholder="留空保持当前 Cookie"
            />
            <textarea
              id="setting-cookie"
              :aria-invalid="Boolean(fieldErrors.cookie) || undefined"
              :aria-describedby="fieldErrors.cookie ? 'setting-error-cookie' : undefined"
              @input="delete fieldErrors.cookie"
              v-else
              v-model="platformCookieValues[activePlatform!.id]"
              rows="5"
              placeholder="留空保持当前 Cookie"
            />
            <span
              v-if="fieldErrors.cookie"
              id="setting-error-cookie"
              class="field-error"
            >
              {{ fieldErrors.cookie }}
            </span>
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
        <strong>
          {{ activePlatform?.id === "xhs" ? "仅支持单条笔记" : "支持作者主页与单条作品" }}
        </strong>
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
</template>
