<script setup lang="ts">
import { nextTick, ref, watch } from 'vue';
import PageHeader from "../components/PageHeader.vue";
import SettingsSections from "../components/SettingsSections.vue";
import MaintenanceSections from "../components/MaintenanceSections.vue";
import IssueDetail from "../components/IssueDetail.vue";
import StateView from "../components/StateView.vue";
import { useSettingsController } from "../composables/useSettingsController";
import type { SettingsMode } from "../settings-registry";
const props = withDefaults(
  defineProps<{ mode?: SettingsMode; section?: string }>(),
  { mode: "settings", section: "application" },
);
const controller = useSettingsController(props);
const {
  activePage,
  hasActiveChanges,
  activeDirtyCount,
  discardActiveChanges,
  refreshActivePage,
  saveCurrentPage,
  pageLoading,
  saving,
  saveError,
  configurationFeedback,
  queueChange,
  cancelQueueChange,
  fieldErrors,
} = controller;
const errorSummary = ref<HTMLElement | null>(null);
watch(() => Object.keys(fieldErrors.value).length, async (value, previous) => {
  if (value > previous) { await nextTick(); errorSummary.value?.focus(); }
});
function focusField(key: string) {
  const field = document.getElementById('setting-'+key);
  field?.focus({preventScroll:true});
  field?.scrollIntoView({block:'center'});
}
</script>
<template>
  <section class="workspace-page config-workspace">
    <PageHeader
      :title="activePage.title"
      :description="activePage.description"
      :busy="pageLoading || saving"
      refreshable
      @refresh="refreshActivePage"
      ><span v-if="hasActiveChanges" class="dirty-indicator"
        >{{ activeDirtyCount }} 项未保存</span
      ><button
        v-if="hasActiveChanges"
        class="btn"
        :disabled="saving"
        @click="discardActiveChanges"
      >
        放弃修改</button
      ><button
        v-if="mode === 'settings'"
        class="btn primary"
        :disabled="saving || pageLoading || !hasActiveChanges"
        @click="saveCurrentPage()"
      >
        {{ saving ? "保存中…" : "保存修改" }}
      </button></PageHeader
    >
    <div v-if="saveError" class="config-alert" role="alert">
      <IssueDetail :message="saveError" />
    </div>
    <div v-if="Object.keys(fieldErrors).length" ref="errorSummary" class="field-error-summary" tabindex="-1" role="alert" aria-label="请检查以下配置字段"><strong>请检查以下字段</strong><button v-for="(message,key) in fieldErrors" :key="key" class="text-button" @click="focusField(String(key))">{{message}}</button></div>
    <p
      v-if="configurationFeedback && mode === 'settings'"
      class="maintenance-note"
      role="status"
    >
      {{ configurationFeedback }}
    </p>
    <div
      v-if="queueChange.pending || queueChange.state === 'failed'"
      class="config-alert"
      role="status"
    >
      <span>{{ queueChange.message }}</span
      ><button
        v-if="queueChange.pending"
        class="btn compact"
        @click="cancelQueueChange"
      >
        撤回待生效变更
      </button>
    </div>
    <StateView v-if="pageLoading" loading /><SettingsSections
      v-else-if="mode === 'settings'"
      :controller="controller"
    /><MaintenanceSections v-else :controller="controller" />
  </section>
</template>
