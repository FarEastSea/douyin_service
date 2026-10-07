<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from "vue";
import { confirmation, modalDepth, settleConfirmation } from "../workspace";
import {
  focusFirst,
  restoreFocus,
  trapFocus,
  captureFocusOrigin,
} from "../focus";
const dialog = ref<HTMLElement | null>(null);
let origin: HTMLElement | null = null,
  counted = false;
function keydown(event: KeyboardEvent) {
  if (!confirmation.value) return;
  if (event.key === "Escape") {
    event.preventDefault();
    settleConfirmation(false);
  } else trapFocus(event, dialog.value);
}
watch(confirmation, async (value) => {
  if (value) {
    origin = captureFocusOrigin();
    modalDepth.value++;
    counted = true;
    document.addEventListener("keydown", keydown);
    await nextTick();
    focusFirst(dialog.value);
  } else {
    if (counted) modalDepth.value--;
    counted = false;
    document.removeEventListener("keydown", keydown);
    await nextTick();
    restoreFocus(origin);
  }
});
onBeforeUnmount(() => {
  document.removeEventListener("keydown", keydown);
  if (counted) modalDepth.value--;
  settleConfirmation(false);
});
</script>
<template>
  <Teleport to="body"
    ><div
      v-if="confirmation"
      class="dialog-backdrop"
      @click.self="settleConfirmation(false)"
    >
      <section
        ref="dialog"
        class="confirm-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="confirm-title"
        aria-describedby="confirm-message"
        tabindex="-1"
      >
        <h2 id="confirm-title">{{ confirmation.title }}</h2>
        <p id="confirm-message">{{ confirmation.message }}</p>
        <footer>
          <button class="btn ghost" @click="settleConfirmation(false)">
            取消</button
          ><button
            class="btn"
            :class="confirmation.danger ? 'danger' : 'primary'"
            @click="settleConfirmation(true)"
          >
            {{ confirmation.confirmLabel }}
          </button>
        </footer>
      </section>
    </div></Teleport
  >
</template>
