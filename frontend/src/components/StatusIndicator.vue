<script setup lang="ts">
import { computed } from "vue";
import { statusPresentation } from "../workspace";
const props = defineProps<{ status?: string; label?: string }>();
const state = computed(() => statusPresentation(props.status));
</script>
<template>
  <span class="status-indicator" :data-tone="state.tone"
    ><i aria-hidden="true" /><span>{{ label || state.label }}</span></span
  >
</template>
