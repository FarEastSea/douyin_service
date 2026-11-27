<script setup lang="ts">
import SelectField from "./SelectField.vue";
import DateField from "./DateField.vue";
import { durationHint } from "../settings/duration";
import { BellRing } from "@lucide/vue";
import { useAppStore } from "../stores/app";
import SubscriptionBackup from "./SubscriptionBackup.vue";
import AccountSettings from "./AccountSettings.vue";
import type { SettingsController } from "../composables/useSettingsController";

const appearance = useAppStore();

const channelNames: Record<string, string> = {
  webhook: "Webhook",
  bark: "Bark",
  email: "邮件",
  gotify: "Gotify",
  telegram: "Telegram",
  wecom: "企业微信",
  dingtalk: "钉钉",
  feishu: "飞书",
};
const componentProps = defineProps<{ controller: SettingsController }>();
const {
  advancedBusy,
  testDatabase,
  archiveRules,
  resourceState,
  fieldErrors,
  fieldDirty,
  notificationTestBusy,
  notificationTestChannel,
  notificationTestResult,
  activePage,
  resolvedSections,
  activePlatform,
  fieldValue,
  setFieldValue,
  fieldEnabled,
  toggleField,
  saveAndTestNotification,
  digestBusy,
  digestPreview,
  sendDigestNow,
  toggleArchiveValue,
} = componentProps.controller;
</script>
<template>
  <div class="settings-sections">
    <section
      v-if="activePage.id === 'application'"
      class="setting-section"
    >
      <div class="setting-list">
        <label class="setting-row">
          <span class="setting-copy">
            <strong>界面主题</strong>
            <small>立即应用于当前浏览器</small>
          </span>
          <SelectField
            :model-value="appearance.theme"
            aria-label="界面主题"
            @update:model-value="appearance.setTheme($event)"
            :options="{ auto: '跟随系统', light: '浅色', dark: '深色' }"
          />
        </label>
      </div>
    </section>
    <template v-if="resolvedSections.length && !(activePage.id === 'account-x' || activePlatform)">
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
                :id="'setting-' + field.key"
                :aria-describedby="
                  fieldErrors[field.key] ? 'setting-error-' + field.key : undefined
                "
                @click.prevent="toggleField(field)"
              >
                <span class="switch-track"><i /></span>
                <span>{{ fieldEnabled(field) ? "已开启" : "已关闭" }}</span>
              </button>
              <input
                v-else
                :id="'setting-' + field.key"
                :aria-invalid="Boolean(fieldErrors[field.key]) || undefined"
                :aria-describedby="
                  fieldErrors[field.key] ? 'setting-error-' + field.key : undefined
                "
                :type="field.secret ? 'password' : field.kind"
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
    </template>

    <section
      v-if="activePage.id === 'notifications'"
      class="setting-section"
    >
      <header>
        <div>
          <h2>发送测试</h2>
          <p>先保存当前子页，再使用服务端实际配置发送一条测试通知。</p>
        </div>
      </header>
      <div class="setting-list">
        <div class="setting-row">
          <span class="setting-copy">
            <strong>测试渠道</strong>
            <small>测试结果不会包含密钥或带 Token 的完整地址。</small>
          </span>
          <span class="setting-control inline-control">
            <SelectField
              v-model="notificationTestChannel"
              aria-label="通知测试渠道"
              :options="{
                all: '全部渠道',
                webhook: 'Webhook',
                bark: 'Bark',
                email: '邮件',
                gotify: 'Gotify',
                telegram: 'Telegram',
                wecom: '企业微信',
                dingtalk: '钉钉',
                feishu: '飞书',
              }"
            />
            <button
              class="btn ghost"
              type="button"
              :disabled="notificationTestBusy"
              @click="saveAndTestNotification"
            >
              <BellRing :size="15" />
              {{ notificationTestBusy ? "测试中…" : "保存并测试" }}
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
          >
            <strong>{{ channelNames[String(channel)] || channel }}</strong>
            {{ result.success ? "成功" : result.message || "失败" }}
          </span>
        </div>
        <div class="setting-row">
          <span class="setting-copy">
            <strong>每日摘要</strong>
            <small>
              立即生成昨天的摘要并发送到已启用的渠道，用来核对内容；不受摘要开关和发送时间限制。
            </small>
          </span>
          <span class="setting-control inline-control">
            <button
              class="btn ghost"
              type="button"
              :disabled="digestBusy"
              @click="sendDigestNow"
            >
              <BellRing :size="15" />
              {{ digestBusy ? "发送中…" : "保存并发送摘要" }}
            </button>
          </span>
        </div>
        <div
          v-if="digestPreview"
          class="digest-preview"
          role="status"
        >
          <strong>{{ digestPreview.title }}</strong>
          <pre>{{ digestPreview.body }}</pre>
        </div>
      </div>
    </section>
    <SubscriptionBackup v-if="activePage.id === 'subscriptions'" />

    <AccountSettings
      v-if="activePage.id.startsWith('account-')"
      :controller="controller"
    />
    <section
      v-if="activePage.id === 'archive'"
      class="archive-page"
    >
      <section class="setting-section">
        <header>
          <div>
            <h2>路径与文件名</h2>
            <p>规则在创建任务时固化，不影响已排队任务。</p>
          </div>
        </header>
        <div class="setting-list">
          <label class="setting-row">
            <span class="setting-copy">
              <strong>目录模板</strong>
              <small>可用：{author} {published_date} {year} {month} {work_type}</small>
            </span>
            <span class="setting-control">
              <input
                id="setting-directory_template"
                :aria-invalid="Boolean(fieldErrors.directory_template) || undefined"
                :aria-describedby="
                  fieldErrors.directory_template ? 'setting-error-directory_template' : undefined
                "
                @input="delete fieldErrors.directory_template"
                v-model="archiveRules.directory_template"
              />
              <span
                v-if="fieldErrors.directory_template"
                id="setting-error-directory_template"
                class="field-error"
              >
                {{ fieldErrors.directory_template }}
              </span>
            </span>
          </label>
          <label class="setting-row">
            <span class="setting-copy">
              <strong>文件名模板</strong>
              <small>必须包含作品 ID、序号后缀和扩展名变量。</small>
            </span>
            <span class="setting-control">
              <input
                id="setting-filename_template"
                :aria-invalid="Boolean(fieldErrors.filename_template) || undefined"
                :aria-describedby="
                  fieldErrors.filename_template ? 'setting-error-filename_template' : undefined
                "
                @input="delete fieldErrors.filename_template"
                v-model="archiveRules.filename_template"
              />
              <span
                v-if="fieldErrors.filename_template"
                id="setting-error-filename_template"
                class="field-error"
              >
                {{ fieldErrors.filename_template }}
              </span>
            </span>
          </label>
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
            <span class="setting-copy">
              <strong>作品类型</strong>
              <small>至少保留一种需要下载的媒体类型。</small>
            </span>
            <span class="setting-control inline-control">
              <button
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
              </button>
            </span>
          </div>
          <div class="setting-row">
            <span class="setting-copy">
              <strong>发布时间</strong>
              <small>留空表示不限制。</small>
            </span>
            <span class="setting-control paired-control">
              <DateField
                id="setting-published_from"
                :aria-invalid="Boolean(fieldErrors.published_from) || undefined"
                :aria-describedby="
                  fieldErrors.published_from ? 'setting-error-published_from' : undefined
                "
                @change="delete fieldErrors.published_from"
                v-model="archiveRules.published_from"
                :max="archiveRules.published_to || undefined"
                aria-label="发布时间起"
              />
              <span
                v-if="fieldErrors.published_from"
                id="setting-error-published_from"
                class="field-error"
              >
                {{ fieldErrors.published_from }}
              </span>
              <DateField
                id="setting-published_to"
                :aria-invalid="Boolean(fieldErrors.published_to) || undefined"
                :aria-describedby="
                  fieldErrors.published_to ? 'setting-error-published_to' : undefined
                "
                @change="delete fieldErrors.published_to"
                v-model="archiveRules.published_to"
                :min="archiveRules.published_from || undefined"
                aria-label="发布时间止"
              />
              <span
                v-if="fieldErrors.published_to"
                id="setting-error-published_to"
                class="field-error"
              >
                {{ fieldErrors.published_to }}
              </span>
            </span>
          </div>
          <div class="setting-row">
            <span class="setting-copy">
              <strong>文件大小（MB）</strong>
              <small>0 表示不限制。</small>
            </span>
            <span class="setting-control paired-control">
              <input
                id="setting-min_file_size_mb"
                :aria-invalid="Boolean(fieldErrors.min_file_size_mb) || undefined"
                :aria-describedby="
                  fieldErrors.min_file_size_mb ? 'setting-error-min_file_size_mb' : undefined
                "
                @input="delete fieldErrors.min_file_size_mb"
                v-model.number="archiveRules.min_file_size_mb"
                type="number"
                min="0"
                step="0.1"
                aria-label="最小文件大小"
              />
              <span
                v-if="fieldErrors.min_file_size_mb"
                id="setting-error-min_file_size_mb"
                class="field-error"
              >
                {{ fieldErrors.min_file_size_mb }}
              </span>
              <input
                id="setting-max_file_size_mb"
                :aria-invalid="Boolean(fieldErrors.max_file_size_mb) || undefined"
                :aria-describedby="
                  fieldErrors.max_file_size_mb ? 'setting-error-max_file_size_mb' : undefined
                "
                @input="delete fieldErrors.max_file_size_mb"
                v-model.number="archiveRules.max_file_size_mb"
                type="number"
                min="0"
                step="0.1"
                aria-label="最大文件大小"
              />
              <span
                v-if="fieldErrors.max_file_size_mb"
                id="setting-error-max_file_size_mb"
                class="field-error"
              >
                {{ fieldErrors.max_file_size_mb }}
              </span>
            </span>
          </div>
          <div class="setting-row">
            <span class="setting-copy">
              <strong>同目录元数据</strong>
              <small>在每个媒体文件旁生成独立元数据文件。</small>
            </span>
            <span class="setting-control inline-control">
              <button
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
              </button>
            </span>
          </div>
        </div>
      </section>
    </section>
    <section
      v-if="activePage.id === 'connections'"
      class="setting-section"
    >
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
