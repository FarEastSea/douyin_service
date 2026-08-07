<script setup lang="ts">
import { reactive, ref } from 'vue'
import { CheckCircle2, Database, RefreshCw, Save, ShieldCheck } from '@lucide/vue'
import { api, jsonBody, saveToken } from '../api'

defineProps<{ status: any }>()
const form = reactive({
  ADMIN_TOKEN: '', DOWNLOAD_ROOT: '/downloads', DOUYIN_DOWNLOAD_SUBDIR: 'douyin', X_DOWNLOAD_SUBDIR: 'X',
  DB_TYPE: 'postgresql', DB_HOST: '', DB_PORT: '5432', DB_USER: '', DB_PASSWORD: '', DB_NAME: '',
  REDIS_URL: 'redis://localhost:6379/0', REDIS_PASSWORD: '',
})
const busy = ref(false), message = ref('')
async function save() {
  busy.value = true
  try {
    const data = await api<any>('/bootstrap/config', { method: 'POST', ...jsonBody({ values: form }) })
    if (form.ADMIN_TOKEN) saveToken(form.ADMIN_TOKEN)
    message.value = data.message || '配置已保存'
  } catch (error: any) { message.value = error.message || '保存失败' }
  finally { busy.value = false }
}
async function restart() { try { const data = await api<any>('/service/restart', { method: 'POST' }); message.value = data.message; window.setTimeout(() => location.reload(), 2500) } catch (e: any) { message.value = e.message } }
</script>

<template><main class="bootstrap-page"><section class="bootstrap-card"><header><div class="brand-mark"><ShieldCheck /></div><div><p class="eyebrow">FIRST RUN SETUP</p><h1>初始化媒体管理系统</h1><p>完成必要配置后即可进入新的管理控制台。</p></div></header><div v-if="status?.errors?.length" class="setup-errors"><article v-for="item in status.errors" :key="item.key"><strong>{{ item.label }}</strong><span>{{ item.message }}</span></article></div><form class="bootstrap-form" @submit.prevent="save"><fieldset><legend>安全与目录</legend><label>管理 Token<input v-model="form.ADMIN_TOKEN" type="password" required /></label><label>下载根目录<input v-model="form.DOWNLOAD_ROOT" required /></label><label>抖音子目录<input v-model="form.DOUYIN_DOWNLOAD_SUBDIR" /></label><label>X 子目录<input v-model="form.X_DOWNLOAD_SUBDIR" /></label></fieldset><fieldset><legend><Database :size="17" /> PostgreSQL</legend><label>主机<input v-model="form.DB_HOST" required /></label><label>端口<input v-model="form.DB_PORT" /></label><label>用户<input v-model="form.DB_USER" required /></label><label>密码<input v-model="form.DB_PASSWORD" type="password" required /></label><label>数据库<input v-model="form.DB_NAME" required /></label></fieldset><fieldset><legend>Redis</legend><label>连接地址<input v-model="form.REDIS_URL" required /></label><label>密码<input v-model="form.REDIS_PASSWORD" type="password" /></label></fieldset><footer><span v-if="message"><CheckCircle2 :size="16" />{{ message }}</span><button type="button" class="btn ghost" @click="restart"><RefreshCw :size="16" />重启服务</button><button class="btn primary" :disabled="busy"><Save :size="16" />{{ busy ? '保存中…' : '保存配置' }}</button></footer></form></section></main></template>
