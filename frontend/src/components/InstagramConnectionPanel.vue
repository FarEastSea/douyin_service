<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { api, jsonBody } from "../api";
import { dateTime } from "../workspace";
import StatusIndicator from "./StatusIndicator.vue";
const emit = defineEmits<{ cookie: [value: string] }>();
const status = ref<any>(), error = ref(""), notice = ref(""), proxy = ref(""), busy = ref(false);
let timer: number | undefined;
let disposed = false;
async function load() {
  try { const result = await api("/platform-downloads/instagram/session"); if (!disposed) status.value = result; }
  catch (e: any) { if (!disposed) error.value = e.message; }
}
async function act(path: string, body?: object) {
  busy.value = true; error.value = ""; notice.value = "";
  try {
    await api("/platform-downloads/instagram/" + path, { method: "POST", ...(body ? jsonBody(body) : {}) });
    proxy.value = ""; notice.value = "检测任务已提交，完成后会自动更新状态。"; await load();
  } catch (e: any) { error.value = e.message; }
  finally { busy.value = false; }
}
async function importFile(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  if (file.size > 131072) { error.value = "Cookie 文件不能超过128KB"; input.value = ""; return; }
  try { emit("cookie", await file.text()); notice.value = "文件已读入下方 Cookie 输入框，请保存当前页。"; }
  catch { error.value = "无法读取文件，请重新选择。"; }
  input.value = "";
}
onMounted(() => { void load(); timer = window.setInterval(() => { if (!document.hidden) void load(); }, 10000); });
onBeforeUnmount(() => { disposed = true; window.clearInterval(timer); });
const labels: Record<string, string> = { ready: "会话检测通过", unchecked: "等待检测", auth_required: "需要更新会话", checkpoint: "需要浏览器验证" };
</script>
<template>
  <section class="setting-section">
    <header><div><h2>会话与固定出口</h2><p>在正常浏览器登录 Instagram，导入该站的 Cookie 文本或导出文件。尽量保持登录与采集出口一致。</p></div></header>
    <div v-if="error" class="load-error-banner" role="alert">{{ error }}</div>
    <p v-if="notice" role="status">{{ notice }}</p>
    <dl v-if="status" class="fact-list">
      <div><dt>会话状态</dt><dd><StatusIndicator :status="status.status === 'ready' ? 'success' : 'warning'" :label="labels[status.status] || status.status" /></dd></div>
      <div><dt>固定代理</dt><dd>{{ status.proxy_configured ? '已加密配置' : '使用服务器固定直连出口' }}</dd></div>
      <div><dt>接口请求</dt><dd>最近一小时 {{ status.hourly_used }} 次 · 最近24小时 {{ status.daily_used }} 次</dd></div>
      <div v-if="status.cooldown_until"><dt>冷却至</dt><dd>{{ dateTime(status.cooldown_until + 'Z') }}</dd></div>
      <div v-if="status.last_error"><dt>下一步</dt><dd>{{ status.last_error }}</dd></div>
      <div v-if="status.test_task"><dt>最近检测任务</dt><dd><RouterLink :to="{path:'/operations/tasks',query:{task_key:`instagram:${status.test_task.id}`}}">查看任务 · {{ status.test_task.status }}</RouterLink></dd></div>
    </dl>
    <div class="setting-list">
      <label class="setting-row"><span class="setting-copy"><strong>导入 Cookie 文件</strong><small>支持 Netscape、浏览器 JSON 导出；仅在本页读取，不保存到浏览器存储。</small></span><span class="setting-control"><input type="file" accept=".txt,.json" @change="importFile" /></span></label>
      <label class="setting-row"><span class="setting-copy"><strong>固定 HTTP/HTTPS 代理</strong><small>留空不会改变代理。保存为空可通过下方“使用直连”操作完成；地址不会回显。</small></span><span class="setting-control"><input v-model="proxy" type="password" autocomplete="new-password" placeholder="http://代理地址:端口" /></span></label>
    </div>
    <div class="detail-links">
      <button class="btn ghost" :disabled="busy || !proxy.trim()" @click="act('connection', {proxy:proxy.trim()})">保存固定代理并检测</button>
      <button class="btn ghost" :disabled="busy || !status?.proxy_configured" @click="act('connection', {proxy:''})">使用直连并检测</button>
      <button class="btn ghost" :disabled="busy || !status?.configured" @click="act('session/test')">重新检测会话</button>
    </div>
    <p class="field-help">检测通过只证明当前会话可用。Stories、Highlights 及私密内容以实际账号可访问范围为准；遇到验证请在正常浏览器处理后重新导入。</p>
  </section>
</template>
