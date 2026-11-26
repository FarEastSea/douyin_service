<script setup lang="ts">
defineProps<{ title: string }>();
</script>
<template>
  <section class="dashboard-card">
    <div class="section-heading">
      <div>
        <h2>
          {{ title }}
          <slot name="count" />
        </h2>
        <p
          v-if="$slots.description"
          class="muted"
        >
          <slot name="description" />
        </p>
      </div>
      <slot name="actions" />
    </div>
    <slot />
  </section>
</template>
