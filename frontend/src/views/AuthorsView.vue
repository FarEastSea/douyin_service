<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Plus, Search, UserRound } from "@lucide/vue";
import { api, jsonBody } from "../api";
import { useAppStore } from "../stores/app";
import { confirmAction, dateTime, useContextActions } from "../workspace";
import PageHeader from "../components/PageHeader.vue";
import StatusIndicator from "../components/StatusIndicator.vue";
import StateView from "../components/StateView.vue";
import InspectorPanel from "../components/InspectorPanel.vue";
import MoreActions from "../components/MoreActions.vue";
import FilterToolbar from "../components/FilterToolbar.vue";
import Pager from "../components/Pager.vue";
import IssueDetail from "../components/IssueDetail.vue";
import type { Author, XAuthor, PageData } from "../types";
type AuthorRecord = Partial<Author & XAuthor> & {
  id: number;
  is_subscribed: boolean;
};
const props = defineProps<{ platform: string }>(),
  route = useRoute(),
  router = useRouter(),
  store = useAppStore();
const isX = computed(() => props.platform === "x"),
  prefix = computed(() => (isX.value ? "/x/authors" : "/authors"));
const authors = ref<AuthorRecord[]>([]),
  page = ref(Number(route.query.page) || 1),
  pages = ref(1),
  total = ref(0),
  loading = ref(false),
  loaded = ref(false),
  error = ref(""),
  updatedAt = ref("");
const search = ref(String(route.query.q || "")),
  subscribed = ref(String(route.query.subscribed || "")),
  account = ref(String(route.query.account || "all"));
const addOpen = ref(false),
  input = ref(""),
  adding = ref(false),
  selected = computed(
    () => Number(route.query.author || route.query.focus) || 0,
  );
const detail = ref<AuthorRecord>(),
  detailBusy = ref(false),
  detailError = ref(""),
  tab = ref("overview"),
  history = ref<any[]>([]),
  automation = ref<any>(),
  resourceError = ref(""),
  resourceBusy = ref(false),
  interval = ref(0),
  busyIds = ref<number[]>([]);
let sequence = 0,
  detailSequence = 0,
  resourceSequence = 0,
  timer: number | undefined;
function name(item: AuthorRecord) {
  return isX.value
    ? item.display_name || "@" + item.username
    : item.nickname || "作者 " + item.id;
}
function syncQuery() {
  void router.replace({
    query: {
      ...route.query,
      page: page.value > 1 ? String(page.value) : undefined,
      q: search.value || undefined,
      subscribed: subscribed.value || undefined,
      account: account.value !== "all" ? account.value : undefined,
    },
  });
}
async function load() {
  const current = ++sequence;
  loading.value = true;
  const params = new URLSearchParams({
    page: String(page.value),
    page_size: "20",
  });
  if (search.value.trim()) params.set("q", search.value.trim());
  if (subscribed.value) params.set("is_subscribed", subscribed.value);
  if (!isX.value) params.set("account_status", account.value);
  try {
    const data = await api<PageData<AuthorRecord>>(
      `${prefix.value}/?${params}`,
    );
    if (current !== sequence) return;
    authors.value = data.items;
    pages.value = data.pages;
    total.value = data.total;
    loaded.value = true;
    error.value = "";
    updatedAt.value = new Date().toISOString();
    if (page.value > Math.max(1, data.pages)) {
      page.value = Math.max(1, data.pages);
      syncQuery();
      void load();
    }
  } catch (e: any) {
    if (current === sequence) error.value = e.message || "无法读取作者";
  } finally {
    if (current === sequence) loading.value = false;
  }
}
function filters() {
  page.value = 1;
  syncQuery();
  void load();
}
function queueSearch() {
  clearTimeout(timer);
  timer = window.setTimeout(filters, 350);
}
function changePage(value: number) {
  page.value = value;
  syncQuery();
  void load();
}
function openAuthor(item: AuthorRecord) {
  void router.replace({
    query: {
      ...route.query,
      author: String(item.id),
      focus: undefined,
      position: undefined,
    },
  });
}
function closeAuthor() {
  void router.replace({
    query: {
      ...route.query,
      author: undefined,
      focus: undefined,
      position: undefined,
    },
  });
}
async function loadDetail() {
  const current = ++detailSequence;
  ++resourceSequence;
  detail.value = undefined;
  detailError.value = "";
  tab.value = "overview";
  automation.value = undefined;
  history.value = [];
  if (!selected.value) return;
  detailBusy.value = true;
  try {
    const data = await api<AuthorRecord>(`${prefix.value}/${selected.value}`);
    if (current === detailSequence) {
      detail.value = data;
      interval.value = data.check_interval || 0;
      if (!isX.value) void loadResource();
    }
  } catch (e: any) {
    if (current === detailSequence) detailError.value = e.message;
  } finally {
    if (current === detailSequence) detailBusy.value = false;
  }
}
async function loadResource() {
  const current = ++resourceSequence;
  resourceBusy.value = true;
  resourceError.value = "";
  try {
    if (!isX.value && tab.value === "overview") {
      const data = await api(`${prefix.value}/${selected.value}/auto-update`);
      if (current === resourceSequence) automation.value = data;
    } else if (!isX.value && tab.value === "history") {
      const data = await api<any>(
        `${prefix.value}/${selected.value}/profile-history`,
      );
      if (current === resourceSequence) history.value = data.items || [];
    }
  } catch (e: any) {
    if (current === resourceSequence) resourceError.value = e.message;
  } finally {
    if (current === resourceSequence) resourceBusy.value = false;
  }
}
async function action(item: AuthorRecord, verb: string, method = "POST") {
  if (busyIds.value.includes(item.id)) return;
  if (
    !isX.value &&
    store.risk.active &&
    ["download", "check", "reconcile", "sync-avatar"].includes(verb)
  )
    return store.notify("抖音请求正在冷却", "info");
  const requestedPlatform = props.platform;
  const path = verb
    ? `${prefix.value}/${item.id}/${verb}`
    : `${prefix.value}/${item.id}`;
  busyIds.value.push(item.id);
  try {
    const result = await api<any>(path, { method });
    store.notify(result.message || "操作完成");
    if (requestedPlatform === props.platform) {
      await load();
      if (selected.value === item.id) await loadDetail();
    }
  } catch (e: any) {
    store.notify(e.message || "操作失败", "error");
  } finally {
    busyIds.value = busyIds.value.filter((id) => id !== item.id);
  }
}
async function remove(item: AuthorRecord) {
  const message = isX.value
    ? `删除 ${name(item)} 的订阅、用户和关联数据库记录？已下载的磁盘文件保留。`
    : `删除作者“${name(item)}”、其作品、任务和已下载文件？文件删除不可恢复。`;
  if (!(await confirmAction(message, "删除作者", "删除作者"))) return;
  await action(item, "", "DELETE");
  if (!authors.value.some((author) => author.id === item.id) && !error.value)
    closeAuthor();
}
async function add() {
  if (!input.value.trim() || adding.value) return;
  const target = props.platform;
  adding.value = true;
  try {
    const data = await api<any>(prefix.value + "/", {
      method: "POST",
      ...jsonBody(
        isX.value
          ? {
              profile_url: input.value.trim(),
              is_subscribed: false,
              check_interval: 3600,
            }
          : {
              share_url: input.value.trim(),
              is_subscribed: false,
              check_interval: 21600,
            },
      ),
    });
    if (target !== props.platform) return;
    addOpen.value = false;
    input.value = "";
    store.notify(data.already_exists ? "作者已存在，已打开详情" : "作者已添加");
    await load();
    if (data.id) openAuthor(data);
    await store.refreshStatus();
  } catch (e: any) {
    store.notify(e.message, "error");
  } finally {
    adding.value = false;
  }
}
async function checkAll() {
  if (!isX.value && store.risk.active) return;
  try {
    const data = await api<any>(prefix.value + "/check-all", {
      method: "POST",
    });
    store.notify(data.message);
  } catch (e: any) {
    store.notify(e.message, "error");
  }
}
async function saveInterval() {
  if (!detail.value || !Number.isFinite(interval.value) || interval.value <= 0)
    return;
  try {
    await api(`${prefix.value}/${detail.value.id}`, {
      method: "PUT",
      ...jsonBody({ check_interval: interval.value }),
    });
    store.notify("检查间隔已保存");
    await loadDetail();
  } catch (e: any) {
    store.notify(e.message, "error");
  }
}
useContextActions(() => [
  ...(detail.value
    ? [
        {
          id: "download-current-author",
          label: "下载当前作者内容",
          run: () => action(detail.value!, "download"),
          disabled:
            busyIds.value.includes(detail.value.id) ||
            (!isX.value && store.risk.active),
        },
        {
          id: "toggle-current-author",
          label: detail.value.is_subscribed
            ? "取消订阅当前作者"
            : "订阅当前作者",
          run: () =>
            action(
              detail.value!,
              detail.value!.is_subscribed ? "unsubscribe" : "subscribe",
            ),
        },
      ]
    : []),
  {
    id: "add-author",
    label: "添加" + (isX.value ? " X 用户" : "抖音作者"),
    run: () => {
      addOpen.value = true;
    },
  },
  {
    id: "author-check",
    label: "检查当前平台订阅",
    run: checkAll,
    disabled: !isX.value && store.risk.active,
  },
]);
watch(selected, loadDetail);
watch(tab, loadResource);
watch(
  () => props.platform,
  () => {
    sequence++;
    detailSequence++;
    authors.value = [];
    loaded.value = false;
    page.value = Number(route.query.page) || 1;
    search.value = String(route.query.q || "");
    subscribed.value = String(route.query.subscribed || "");
    account.value = "all";
    addOpen.value = false;
    void load();
    void loadDetail();
  },
);
watch(
  () => route.query,
  () => {
    const nextPage = Number(route.query.page) || 1,
      nextSearch = String(route.query.q || ""),
      nextSubscribed = String(route.query.subscribed || ""),
      nextAccount = String(route.query.account || "all");
    if (
      nextPage === page.value &&
      nextSearch === search.value &&
      nextSubscribed === subscribed.value &&
      nextAccount === account.value
    )
      return;
    page.value = nextPage;
    search.value = nextSearch;
    subscribed.value = nextSubscribed;
    account.value = nextAccount;
    void load();
  },
);
onMounted(() => {
  void load();
  void loadDetail();
});
onBeforeUnmount(() => {
  sequence++;
  detailSequence++;
  resourceSequence++;
  clearTimeout(timer);
});
</script>
<template>
  <section class="workspace-page authors-page">
    <PageHeader
      title="作者"
      description="管理订阅、最近检查与已保存的内容。"
      :busy="loading"
      :updated-at="updatedAt"
      refreshable
      @refresh="load"
      ><MoreActions label="作者操作"
        ><button :disabled="!isX && store.risk.active" @click="checkAll">
          检查当前平台订阅</button
        ><RouterLink to="/automation">查看自动化运行</RouterLink></MoreActions
      ><button
        class="btn primary"
        :disabled="!isX && store.risk.active"
        @click="addOpen = true"
      >
        <Plus :size="15" />添加作者
      </button></PageHeader
    >
    <nav class="view-tabs" aria-label="作者平台">
      <RouterLink to="/authors/douyin">抖音</RouterLink
      ><RouterLink to="/authors/x">X / Twitter</RouterLink>
    </nav>
    <div v-if="error && authors.length" class="load-error-banner" role="alert">
      <IssueDetail :message="error" impact="显示上次读取的数据。" /><button
        class="text-button"
        @click="load"
      >
        重试
      </button>
    </div>
    <FilterToolbar
      :active="
        [
          subscribed && (subscribed === 'true' ? '已订阅' : '未订阅'),
          account !== 'all' && '账号状态',
        ]
          .filter(Boolean)
          .join(' · ')
      "
      @clear="
        subscribed = '';
        account = 'all';
        filters();
      "
      ><template #search
        ><label class="search"
          ><Search :size="16" /><input
            v-model="search"
            aria-label="搜索作者"
            :placeholder="isX ? '搜索用户名或显示名称' : '搜索作者名称或 ID'"
            @input="queueSearch" /></label></template
      ><select v-model="subscribed" aria-label="订阅状态" @change="filters">
        <option value="">全部订阅</option>
        <option value="true">已订阅</option>
        <option value="false">未订阅</option></select
      ><select
        v-if="!isX"
        v-model="account"
        aria-label="账号状态"
        @change="filters"
      >
        <option value="all">全部账号</option>
        <option value="normal">正常</option>
        <option value="abnormal">异常</option>
        <option value="banned">封禁 / 禁言</option>
        <option value="deleted">已销号</option>
        <option value="restricted">不可访问</option>
      </select></FilterToolbar
    ><StateView
      v-if="!authors.length"
      :loading="loading"
      :error="error"
      :title="
        search || subscribed || account !== 'all'
          ? '没有匹配的作者'
          : '还没有添加作者'
      "
      description="添加作者，订阅后即可自动检查更新。"
      @retry="load"
    />
    <div v-else class="table-shell">
      <table class="data-table author-table">
        <thead>
          <tr>
            <th>作者</th>
            <th>{{ isX ? "媒体文件" : "作品" }}</th>
            <th>{{ isX ? "账号状态" : "自动更新" }}</th>
            <th>订阅</th>
            <th class="actions-col">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="item in authors"
            :key="item.id"
            :class="{ selected: selected === item.id }"
          >
            <td>
              <div class="author-cell">
                <span class="avatar"
                  ><img
                    v-if="item.avatar_url"
                    :src="
                      isX
                        ? item.avatar_url
                        : '/api/authors/' + item.id + '/avatar'
                    "
                    alt=""
                    loading="lazy" /><UserRound v-else :size="18"
                /></span>
                <div>
                  <button class="record-link" @click="openAuthor(item)">
                    {{ name(item) }}</button
                  ><span>{{
                    isX
                      ? "@" + item.username
                      : "最近检查 " +
                        dateTime(
                          item.last_auto_update_at || item.last_check_time,
                        )
                  }}</span>
                </div>
              </div>
            </td>
            <td data-label="内容">
              <strong>{{
                isX ? (item.total_downloads ?? "—") : (item.total_works ?? "—")
              }}</strong
              ><span v-if="!isX"
                >{{ item.downloaded_works ?? "—" }} 已下载</span
              >
            </td>
            <td data-label="状态">
              <span v-if="isX" :class="{ danger: !!item.last_error }">{{
                item.account_status_label || "状态未确认"
              }}</span
              ><StatusIndicator
                v-else
                :status="
                  item.auto_update_status ||
                  (item.is_subscribed ? 'waiting' : 'unsubscribed')
                "
              /><button
                v-if="item.last_error"
                class="error-excerpt"
                :title="item.last_error"
                @click="openAuthor(item)"
              >
                最近检查异常
              </button>
            </td>
            <td data-label="订阅">
              <button
                class="switch"
                :class="{ on: item.is_subscribed }"
                role="switch"
                :aria-checked="item.is_subscribed"
                :aria-label="
                  (item.is_subscribed ? '取消订阅 ' : '订阅 ') + name(item)
                "
                :disabled="busyIds.includes(item.id)"
                @click="
                  action(item, item.is_subscribed ? 'unsubscribe' : 'subscribe')
                "
              >
                <i />
              </button>
            </td>
            <td class="actions-col">
              <MoreActions :label="name(item)"
                ><button @click="openAuthor(item)">查看作者详情</button
                ><RouterLink
                  v-if="!isX"
                  :to="`/authors/douyin/${item.id}/works`"
                  >浏览作品</RouterLink
                ><button
                  :disabled="
                    busyIds.includes(item.id) || (!isX && store.risk.active)
                  "
                  @click="action(item, 'download')"
                >
                  下载作者内容</button
                ><button
                  class="danger"
                  :disabled="busyIds.includes(item.id)"
                  @click="remove(item)"
                >
                  删除作者
                </button></MoreActions
              >
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <Pager
      v-if="loaded"
      :page="page"
      :pages="pages"
      :total="total"
      @change="changePage"
    />
    <InspectorPanel
      :open="!!selected"
      :title="detail ? name(detail) : '作者详情'"
      :subtitle="isX ? 'X / Twitter' : '抖音作者'"
      @close="closeAuthor"
      ><StateView
        v-if="!detail"
        :loading="detailBusy"
        :error="detailError"
        @retry="loadDetail"
      /><template v-else
        ><nav class="view-tabs" aria-label="作者详情">
          <button
            :class="{ active: tab === 'overview' }"
            @click="tab = 'overview'"
          >
            概览</button
          ><button
            v-if="!isX"
            :class="{ active: tab === 'history' }"
            @click="tab = 'history'"
          >
            资料历史</button
          ><button
            :class="{ active: tab === 'configuration' }"
            @click="tab = 'configuration'"
          >
            订阅配置
          </button>
        </nav>
        <template v-if="tab === 'overview'"
          ><dl class="detail-fields">
            <div>
              <dt>订阅</dt>
              <dd>{{ detail.is_subscribed ? "已订阅" : "未订阅" }}</dd>
            </div>
            <div>
              <dt>最近检查</dt>
              <dd>
                {{
                  dateTime(
                    automation?.last_auto_update_at || detail.last_check_time,
                  )
                }}
              </dd>
            </div>
            <div v-if="!isX">
              <dt>有效间隔</dt>
              <dd>
                {{
                  automation?.check_interval_seconds != null
                    ? Math.round(automation.check_interval_seconds / 60) +
                      " 分钟"
                    : "未确认"
                }}
              </dd>
            </div>
            <div v-if="!isX">
              <dt>预计下次检查</dt>
              <dd>{{ dateTime(automation?.expected_next_auto_update_at) }}</dd>
            </div>
          </dl>
          <IssueDetail
            v-if="detail.last_error"
            :message="detail.last_error"
          /><a
            v-if="detail.share_url || detail.profile_url"
            class="text-button"
            :href="detail.share_url || detail.profile_url"
            target="_blank"
            rel="noopener noreferrer"
            >打开主页</a
          ><RouterLink
            v-if="!isX"
            class="btn"
            :to="`/authors/douyin/${detail.id}/works`"
            >浏览全部作品</RouterLink
          >
          <div v-if="automation?.history?.length" class="result-list">
            <h3>最近检查轨迹</h3>
            <article v-for="item in automation.history" :key="item.report_id">
              <div>
                <RouterLink :to="'/automation?run=' + item.report_id">{{
                  dateTime(item.started_at)
                }}</RouterLink
                ><StatusIndicator :status="item.status" />
              </div>
              <p>{{ item.message }}</p>
            </article>
          </div></template
        ><template v-else-if="tab === 'history'"
          ><StateView
            v-if="resourceBusy || resourceError || !history.length"
            :loading="resourceBusy"
            :error="resourceError"
            title="暂无资料变化"
            @retry="loadResource"
          />
          <div class="result-list">
            <article v-for="item in history" :key="item.id">
              <time>{{ dateTime(item.observed_at) }}</time
              ><strong>{{
                item.field_name === "nickname"
                  ? "名称"
                  : item.field_name === "avatar_url"
                    ? "头像"
                    : item.field_name
              }}</strong>
              <p>{{ item.old_value || "空" }} → {{ item.new_value || "空" }}</p>
            </article>
          </div></template
        ><template v-else
          ><label v-if="!isX" class="stacked-field"
            >检查间隔（秒）<input
              v-model.number="interval"
              type="number"
              min="1"
          /></label>
          <p v-if="isX" class="inline-note">
            已配置检查间隔：{{ detail.check_interval ?? "未确认" }}
            秒。订阅检查沿用当前平台调度规则。
          </p>
          <p v-else class="inline-note">
            实际调度还会遵守全局最短间隔与平台请求节奏。
          </p>
          <button
            v-if="!isX"
            class="btn primary"
            :disabled="busyIds.includes(detail.id)"
            @click="saveInterval"
          >
            保存间隔
          </button>
          <div class="danger-zone">
            <h3>删除作者</h3>
            <p>
              {{
                isX ? "删除用户记录及订阅。" : "同时删除作品、下载任务和文件。"
              }}
            </p>
            <button class="btn danger" @click="remove(detail)">删除作者</button>
          </div></template
        ><IssueDetail
          v-if="resourceError && tab === 'overview'"
          :message="resourceError" /></template
      ><template #footer
        ><template v-if="detail"
          ><button
            class="btn"
            :disabled="
              busyIds.includes(detail.id) || (!isX && store.risk.active)
            "
            @click="action(detail, 'download')"
          >
            下载内容</button
          ><MoreActions label="更多作者操作"
            ><button
              v-if="!isX"
              :disabled="store.risk.active"
              @click="action(detail, 'check')"
            >
              检查新作品</button
            ><button
              v-if="!isX"
              :disabled="store.risk.active"
              @click="action(detail, 'sync-avatar')"
            >
              同步资料与头像</button
            ><button
              v-if="!isX"
              :disabled="store.risk.active"
              @click="action(detail, 'reconcile')"
            >
              作者全量对账</button
            ><button
              @click="
                action(
                  detail,
                  detail.is_subscribed ? 'unsubscribe' : 'subscribe',
                )
              "
            >
              {{ detail.is_subscribed ? "取消订阅" : "订阅作者" }}
            </button></MoreActions
          ></template
        ></template
      ></InspectorPanel
    >
    <InspectorPanel
      :open="addOpen"
      title="添加作者"
      :subtitle="isX ? 'X 主页链接或 @用户名' : '抖音作者主页或分享链接'"
      modal
      @close="addOpen = false"
      ><form class="stacked-form" @submit.prevent="add">
        <label class="stacked-field"
          >{{ isX ? "用户主页或用户名" : "作者主页链接"
          }}<input
            v-model="input"
            required
            :placeholder="isX ? '@username' : '粘贴作者分享链接'"
        /></label>
        <p class="inline-note">添加后默认未订阅。可在作者列表开启自动更新。</p>
        <button
          class="btn primary"
          :disabled="adding || (!isX && store.risk.active)"
        >
          {{ adding ? "添加中…" : "添加作者" }}
        </button>
      </form></InspectorPanel
    >
  </section>
</template>
