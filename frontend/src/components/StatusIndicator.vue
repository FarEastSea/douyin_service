<script setup lang="ts">
import { Circle, CircleCheck, CircleAlert, TriangleAlert, Activity } from "@lucide/vue";
import { computed } from "vue";
import { statusPresentation } from "../workspace";

const icons: Record<string, typeof Circle> = {
  neutral: Circle,
  success: CircleCheck,
  danger: CircleAlert,
  warning: TriangleAlert,
  info: Activity,
};
const props = defineProps<{ status?: string; label?: string }>();
const state = computed(() => statusPresentation(props.status));
</script>
<template>
  <span
    class="status-indicator"
    :data-tone="state.tone"
  >
    <component
      :is="icons[state.tone] || Circle"
      :size="12"
      aria-hidden="true"
    />
    <span>{{ label || state.label }}</span>
  </span>
</template>
