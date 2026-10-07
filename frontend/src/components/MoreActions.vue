<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, useId } from "vue";
import { Ellipsis, X } from "@lucide/vue";
import {
  focusFirst,
  restoreFocus,
  rememberMenuOrigin,
  releaseMenuOrigin,
} from "../focus";
withDefaults(defineProps<{ label?: string }>(), { label: "更多操作" });
const open = ref(false),
  trigger = ref<HTMLElement | null>(null),
  panel = ref<HTMLElement | null>(null),
  id = useId();
const position = ref({ top: "0px", left: "0px" });
function close() {
  releaseMenuOrigin(trigger.value);
  const previousPanel = panel.value;
  open.value = false;
  document.removeEventListener("keydown", keydown, true);
  void nextTick(() => {
    if (
      document.activeElement === document.body ||
      previousPanel?.contains(document.activeElement)
    )
      restoreFocus(trigger.value);
  });
}
function keydown(event: KeyboardEvent) {
  if (event.key === "Escape") {
    event.preventDefault();
    event.stopImmediatePropagation();
    close();
  } else if (event.key === "Tab") close();
  else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
    event.preventDefault();
    const items = [
      ...(panel.value?.querySelectorAll<HTMLElement>(
        "button:not(:disabled),a",
      ) || []),
    ];
    const current = items.indexOf(document.activeElement as HTMLElement);
    const index =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? items.length - 1
          : (current + (event.key === "ArrowDown" ? 1 : -1) + items.length) %
            items.length;
    items[index]?.focus();
  }
}
async function show() {
  const rect = trigger.value?.getBoundingClientRect();
  if (!rect) return;
  position.value = {
    top:
      Math.min(rect.bottom + 6, Math.max(12, window.innerHeight - 360)) + "px",
    left:
      Math.max(12, Math.min(rect.right - 240, window.innerWidth - 252)) + "px",
  };
  rememberMenuOrigin(trigger.value);
  open.value = true;
  document.addEventListener("keydown", keydown, true);
  await nextTick();
  panel.value
    ?.querySelectorAll("button,a")
    .forEach((item) => item.setAttribute("role", "menuitem"));
  focusFirst(panel.value);
}
function choose(event: MouseEvent) {
  if ((event.target as HTMLElement).closest("button:not(:disabled),a")) {
    rememberMenuOrigin(trigger.value);
    close();
  }
}
onBeforeUnmount(() => document.removeEventListener("keydown", keydown, true));
</script>
<template>
  <button
    ref="trigger"
    class="icon-btn"
    type="button"
    :aria-label="label + '，更多操作'"
    :title="label"
    aria-haspopup="menu"
    :aria-expanded="open"
    :aria-controls="id"
    @click="show"
  >
    <Ellipsis :size="18" />
  </button>
  <Teleport to="body"
    ><div v-if="open" class="action-popover-backdrop" @click.self="close">
      <section
        :id="id"
        ref="panel"
        class="action-popover"
        :style="position"
        role="menu"
        :aria-label="label + '，更多操作'"
        tabindex="-1"
      >
        <header>
          <strong>{{ label }}</strong>
        </header>
        <div @click="choose"><slot /></div>
      </section></div
  ></Teleport>
</template>
