<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import { ArrowRight, Search } from "@lucide/vue";
import { useRouter } from "vue-router";
import { contextActions, modalDepth, type WorkspaceAction } from "../workspace";
import {
  focusFirst,
  restoreFocus,
  trapFocus,
  captureFocusOrigin,
} from "../focus";
const emit = defineEmits<{ close: [] }>(),
  router = useRouter();
const query = ref(""),
  active = ref(0),
  panel = ref<HTMLElement | null>(null),
  input = ref<HTMLInputElement | null>(null);
let origin: HTMLElement | null = null;
const pages = [
  ["工作台", "/dashboard"],
  ["下载任务", "/operations/tasks"],
  ["抖音作者", "/authors/douyin"],
  ["X 用户", "/authors/x"],
  ["自动化运行", "/automation"],
  ["系统状态", "/operations/maintenance/services"],
  ["设置", "/settings/application"],
];
const actions = computed<WorkspaceAction[]>(() =>
  [
    {
      id: "new-download",
      label: "新建下载",
      run: () => {
        window.dispatchEvent(new CustomEvent("app:new-download"));
      },
    },
    ...contextActions.value,
    ...pages.map(([label, path]) => ({
      id: path,
      label: `前往${label}`,
      run: async () => {
        await router.push(path);
      },
    })),
  ].filter(
    (item) =>
      !query.value ||
      item.label.toLowerCase().includes(query.value.toLowerCase()),
  ),
);
async function choose(item?: WorkspaceAction) {
  if (!item || item.disabled) return;
  emit("close");
  await nextTick();
  await item.run();
}
function keydown(event: KeyboardEvent) {
  if (event.key === "Escape") {
    event.preventDefault();
    emit("close");
  } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    active.value =
      (active.value +
        (event.key === "ArrowDown" ? 1 : -1) +
        actions.value.length) %
      Math.max(1, actions.value.length);
  } else if (event.key === "Enter") {
    event.preventDefault();
    void choose(actions.value[active.value]);
  } else trapFocus(event, panel.value);
}
onMounted(() => {
  origin = captureFocusOrigin();
  modalDepth.value++;
  document.addEventListener("keydown", keydown);
  void nextTick(() => focusFirst(panel.value, input.value));
});
onBeforeUnmount(() => {
  modalDepth.value--;
  document.removeEventListener("keydown", keydown);
  void nextTick(() => restoreFocus(origin));
});
</script>
<template>
  <Teleport to="body"
    ><div class="command-backdrop" @click.self="emit('close')">
      <section
        ref="panel"
        class="command-palette"
        role="dialog"
        aria-modal="true"
        aria-label="命令面板"
      >
        <label class="command-search"
          ><Search :size="18" /><input
            ref="input"
            v-model="query"
            aria-label="搜索命令和页面"
            placeholder="搜索命令或前往页面…"
            @input="active = 0"
          /><kbd>Esc</kbd></label
        >
        <div class="command-results">
          <button
            v-for="(item, index) in actions"
            :key="item.id"
            :class="{ active: active === index, danger: item.danger }"
            :disabled="item.disabled"
            @pointermove="active = index"
            @click="choose(item)"
          >
            <span>{{ item.label }}</span
            ><ArrowRight :size="14" />
          </button>
          <p v-if="!actions.length" class="command-empty">
            没有匹配的命令。任务与作者可在各自页面中搜索。
          </p>
        </div>
        <footer>↑ ↓ 选择 · Enter 执行<span>命令与页面</span></footer>
      </section>
    </div></Teleport
  >
</template>
