<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { AlertCircle, CheckCircle2, ChevronDown, FileCheck, Info, LoaderCircle, Upload } from "@lucide/vue";
import { api, jsonBody } from "../api";
import { dateTime } from "../workspace";
import StatusIndicator from "./StatusIndicator.vue";
const props = defineProps<{ cookie: string; saving?: boolean }>();
const emit = defineEmits<{ 'update:cookie': [value: string]; saved: [] }>();
const status = ref<any>(), error = ref(""), loadError = ref(""), notice = ref(""), proxy = ref(""), busy = ref(false);
const expanded = ref(true), filename = ref(""), fileInput = ref<HTMLInputElement>();
let timer: number | undefined, disposed = false, initialized = false;
const testing = computed(() => ['pending', 'downloading'].includes(status.value?.test_task?.status));
const feedback = computed(() => {
  const task = status.value?.test_task;
  if (error.value) return { tone: 'error', text: error.value };
  if (!props.cookie.trim() && task?.status === 'failed') return { tone: 'error', text: task.error_message || '会话检测未完成，请查看检测任务。', code: task.error_code };
  if (notice.value) return { tone: 'info', text: notice.value };
  if (testing.value) return { tone: 'info', text: '会话检测已排队或正在执行，结果会自动更新。' };
  if (status.value?.status === 'ready') return { tone: 'success', text: '会话检测通过，可以开启采集。' };
  return null;
});
function message(e: any) {
  return e.status === 525 ? '管理服务连接失败（HTTP 525），请稍后刷新；此错误无法判断 Instagram 会话是否有效。' : e.message;
}
async function load() {
  try {
    const result: any = await api('/platform-downloads/instagram/session');
    if (disposed) return;
    status.value = result; loadError.value = '';
    if (!initialized) { expanded.value = !result.configured || !!props.cookie; initialized = true; }
    if (!['pending','downloading'].includes(result.test_task?.status)) notice.value = '';
  } catch (e: any) { if (!disposed) loadError.value = message(e); }
}
async function act(path: string, body?: object) {
  busy.value = true; error.value = ''; notice.value = '';
  try {
    await api('/platform-downloads/instagram/' + path, { method: 'POST', ...(body ? jsonBody(body) : {}) });
    if (path === 'config/cookie') { emit('update:cookie', ''); emit('saved'); expanded.value = false; filename.value = ''; }
    if (path === 'connection') proxy.value = '';
    notice.value = '已保存，检测任务已提交；完成后会在此显示结果。'; await load();
  } catch (e: any) { error.value = message(e); }
  finally { busy.value = false; }
}
async function importFile(event: Event) {
  const input = event.target as HTMLInputElement, file = input.files?.[0];
  if (!file) return;
  error.value = ''; notice.value = '';
  if (file.size > 131072) { error.value = 'Cookie 文件不能超过 128 KB'; input.value = ''; return; }
  try { const text = await file.text(); emit('update:cookie', text); filename.value = file.name; expanded.value = true; notice.value = `已读取 ${file.name}，请点击“保存并检测会话”。`; }
  catch { error.value = '无法读取文件，请重新选择。'; }
  input.value = '';
}
function pasted(event: Event) { filename.value = ''; notice.value = ''; error.value = ''; emit('update:cookie', (event.target as HTMLTextAreaElement).value); }
watch(() => props.cookie, (value, old) => { if (value) expanded.value = true; else if (old) { filename.value = ''; void load(); } });
onMounted(() => { void load(); timer = window.setInterval(() => { if (!document.hidden) void load(); }, 10000); });
onBeforeUnmount(() => { disposed = true; window.clearInterval(timer); });
const labels: Record<string, string> = { ready: '检测通过', unchecked: '尚未通过检测', auth_required: '需更新会话', checkpoint: '需浏览器验证' };
</script>
<template>
  <section class="setting-section instagram-session">
    <header><div><h2>登录会话</h2><p>在正常浏览器登录 Instagram，粘贴 Cookie 或导入导出文件，然后保存并检测。</p></div><StatusIndicator :status="status?.status === 'ready' ? 'success' : 'warning'" :label="testing ? '检测中' : labels[status?.status] || '读取状态中'" /></header>
    <div v-if="loadError" class="load-error-banner" role="alert"><AlertCircle :size="18" /><span>{{ loadError }}</span><button class="text-button" @click="load">重新读取状态</button></div>
    <div v-if="feedback" class="instagram-feedback" :data-tone="feedback.tone" :role="feedback.tone === 'error' ? 'alert' : 'status'" aria-live="polite">
      <AlertCircle v-if="feedback.tone === 'error'" :size="20" /><CheckCircle2 v-else-if="feedback.tone === 'success'" :size="20" /><LoaderCircle v-else-if="testing" :size="20" /><Info v-else :size="20" />
      <div><strong>{{ feedback.text }}</strong><small v-if="feedback.code">错误代码：{{ feedback.code }}</small></div>
      <RouterLink v-if="status?.test_task" class="text-button" :to="{path:'/operations/tasks',query:{task_key:`instagram:${status.test_task.id}`}}">查看检测任务</RouterLink>
    </div>
    <div class="instagram-cookie-toolbar"><div><strong>Cookie</strong><small>{{ status?.configured ? '已加密保存，内容不会回显' : '尚未保存登录会话' }}</small></div><button class="btn ghost" type="button" :aria-expanded="expanded" aria-controls="instagram-cookie-editor" @click="expanded = !expanded"><ChevronDown :size="16" />{{ expanded ? '收起' : '更新 Cookie' }}</button></div>
    <div v-show="expanded" id="instagram-cookie-editor" class="instagram-cookie-editor">
      <div class="instagram-file-row"><input ref="fileInput" class="visually-hidden" type="file" accept=".txt,.json" aria-label="导入 Instagram Cookie 文件" @change="importFile" /><button class="btn ghost" type="button" :disabled="busy || saving" @click="fileInput?.click()"><Upload :size="16" />导入 Cookie 文件</button><span v-if="filename" class="instagram-file-name"><FileCheck :size="16" />已读取：{{ filename }}</span><span v-else class="field-help">支持 Netscape、浏览器 JSON 和 Cookie 文本</span></div>
      <label for="instagram-cookie-text">粘贴或检查待保存的 Cookie</label><textarea id="instagram-cookie-text" :value="cookie" rows="5" autocomplete="off" spellcheck="false" placeholder="粘贴 Cookie；文件导入的内容也会显示在这里" @input="pasted" />
      <div class="detail-links"><button class="btn primary" :disabled="busy || saving || !cookie.trim()" @click="act('config/cookie', {cookie})">{{ busy ? '提交中…' : '保存并检测会话' }}</button><small class="field-help">仅在本页编辑，不保存到浏览器存储。</small></div>
    </div>
    <div class="instagram-session-actions"><button class="btn ghost" :disabled="busy || saving || !status?.configured || testing || !!cookie.trim()" @click="act('session/test')">重新检测已保存会话</button><small class="field-help" v-if="cookie.trim()">有待保存的 Cookie，请先保存并检测。</small><small v-else-if="status?.validated_at" class="field-help">最近通过：{{ dateTime(status.validated_at + 'Z') }}</small></div>
    <details class="instagram-connection"><summary>网络出口与请求用量</summary><dl v-if="status" class="fact-list"><div><dt>固定出口</dt><dd>{{ status.proxy_configured ? '已配置代理' : '服务器直连' }}</dd></div><div><dt>接口请求</dt><dd>最近一小时 {{ status.hourly_used }} 次 · 最近24小时 {{ status.daily_used }} 次</dd></div><div v-if="status.cooldown_until"><dt>冷却至</dt><dd>{{ dateTime(status.cooldown_until + 'Z') }}</dd></div></dl><p class="field-help">尽量保持浏览器登录与采集出口一致。代理地址加密保存，不回显。</p><label class="setting-row"><span class="setting-copy"><strong>固定 HTTP/HTTPS 代理</strong><small>留空保持现有代理；切换直连请使用下方按钮。</small></span><span class="setting-control"><input v-model="proxy" type="password" autocomplete="new-password" placeholder="http://代理地址:端口" /></span></label><div class="detail-links"><button class="btn ghost" :disabled="busy || testing || !proxy.trim()" @click="act('connection', {proxy:proxy.trim()})">保存代理并检测</button><button class="btn ghost" :disabled="busy || testing || !status?.proxy_configured" @click="act('connection', {proxy:''})">切换直连并检测</button></div></details>
    <p class="field-help">检测证明会话可用；Stories、Highlights 和私密内容仍以当前账号实际访问权限为准。遇到验证请先在正常浏览器处理。</p>
  </section>
</template>
