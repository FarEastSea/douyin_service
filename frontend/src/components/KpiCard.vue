<script setup lang="ts">
import type { Component } from "vue";
import { RouterLink, type RouteLocationRaw } from "vue-router";
defineProps<{
  to?: RouteLocationRaw;
  icon: Component;
  value: string | number;
  tone?: "info" | "danger";
  progress?: number | null;
}>();
</script>
<template>
  <component
    :is="to ? RouterLink : 'div'"
    v-bind="to ? { to } : {}"
    class="kpi-card"
    :data-tone="tone"
  >
    <span class="kpi-label">
      <component
        :is="icon"
        :size="15"
        aria-hidden="true"
      />
      <slot name="label" />
    </span>
    <strong>{{ value }}</strong>
    <small><slot /></small>
    <span
      v-if="progress != null"
      class="kpi-meter"
      :class="{ danger: tone === 'danger' }"
      :style="{ '--progress': Math.max(0, Math.min(100, progress)) + '%' }"
      aria-hidden="true"
    />
  </component>
</template>
