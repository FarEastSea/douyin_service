<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { X } from "@lucide/vue";
import {
  focusFirst,
  restoreFocus,
  trapFocus,
  captureFocusOrigin,
} from "../focus";
import { modalDepth, inspectorDepth } from "../workspace";
const props = withDefaults(
  defineProps<{
    open: boolean;
    title: string;
    subtitle?: string;
    modal?: boolean;
  }>(),
  { modal: false },
);
const emit = defineEmits<{ close: [] }>();
const panel = ref<HTMLElement | null>(null),
  wide = ref(false);
let query: MediaQueryList | null = null,
  origin: HTMLElement | null = null,
  counted = false;
function update() {
  wide.value = Boolean(query?.matches) && !props.modal;
}
function countModal() {
  const next = props.open && !wide.value;
  if (next !== counted) {
    modalDepth.value += next ? 1 : -1;
    if (!props.modal) inspectorDepth.value += next ? 1 : -1;
    counted = next;
  }
}
function keydown(event: KeyboardEvent) {
  if (!props.open || modalDepth.value > (counted ? 1 : 0)) return;
  if (event.key === "Escape") {
    event.preventDefault();
    emit("close");
  } else if (!wide.value) trapFocus(event, panel.value);
}
watch([() => props.open, wide], async ([open], previous) => {
  countModal();
  if (open && !previous?.[0]) {
    origin = captureFocusOrigin();
    await nextTick();
    focusFirst(panel.value);
  } else if (open && previous?.[1] !== wide.value) {
    await nextTick();
    focusFirst(panel.value);
  } else if (!open && previous?.[0]) {
    await nextTick();
    restoreFocus(origin);
    origin = null;
  }
});
onMounted(() => {
  query = matchMedia("(min-width: 1440px)");
  update();
  query.addEventListener("change", update);
  countModal();
  document.addEventListener("keydown", keydown);
  if (props.open) {
    origin = captureFocusOrigin();
    void nextTick(() => focusFirst(panel.value));
  }
});
onBeforeUnmount(() => {
  query?.removeEventListener("change", update);
  document.removeEventListener("keydown", keydown);
  if (counted) {
    modalDepth.value--;
    if (!props.modal) inspectorDepth.value--;
  }
  restoreFocus(origin);
});
</script>
<template>
  <Teleport to="body" :disabled="wide"
    ><div v-if="open" class="inspector-host" :class="{ docked: wide }">
      <button
        v-if="!wide"
        class="inspector-backdrop"
        aria-label="关闭详情"
        @click="emit('close')"
      />
      <section
        ref="panel"
        class="inspector-panel"
        :role="wide ? 'complementary' : 'dialog'"
        :aria-modal="wide ? undefined : true"
        :aria-label="title"
        :inert="modalDepth > (counted ? 1 : 0)"
        tabindex="-1"
      >
        <header class="inspector-header">
          <div>
            <h2>{{ title }}</h2>
            <p v-if="subtitle">{{ subtitle }}</p>
          </div>
          <button class="icon-btn" aria-label="关闭详情" @click="emit('close')">
            <X :size="18" />
          </button>
        </header>
        <div class="inspector-body"><slot /></div>
        <footer v-if="$slots.footer" class="inspector-footer">
          <slot name="footer" />
        </footer>
      </section></div
  ></Teleport>
</template>
