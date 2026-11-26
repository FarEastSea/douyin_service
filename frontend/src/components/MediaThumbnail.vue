<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { Image, Play } from "@lucide/vue";
import { isVideoMedia } from "../media";
const props = defineProps<{
  src?: string | null;
  type?: string;
  alt?: string;
  natural?: boolean;
  priority?: boolean;
  width?: number | null;
  height?: number | null;
}>();
const failed = ref(false);
const source = computed(() =>
  props.src?.startsWith("/operations/") ? `/api${props.src}` : props.src || "",
);
watch(source, () => {
  failed.value = false;
});
</script>

<template>
  <span
    class="media-thumbnail"
    :class="{ 'thumbnail-missing': !source || failed, 'natural-thumbnail': natural }"
  >
    <img
      :width="width || 160"
      :height="height || 160"
      v-if="natural && source && !failed"
      class="media-ambient"
      :src="source"
      alt=""
      aria-hidden="true"
      loading="lazy"
      decoding="async"
      referrerpolicy="no-referrer"
    />
    <img
      :width="width || 160"
      :height="height || 160"
      v-if="source && !failed"
      :src="source"
      :alt="alt || ''"
      :loading="priority ? 'eager' : 'lazy'"
      :fetchpriority="priority ? 'high' : 'auto'"
      decoding="async"
      referrerpolicy="no-referrer"
      @error="failed = true"
    />
    <span
      v-else
      class="thumbnail-fallback"
    >
      <Play
        v-if="isVideoMedia(type)"
        :size="22"
        aria-hidden="true"
      />
      <Image
        v-else
        :size="22"
        aria-hidden="true"
      />
      <span class="sr-only">暂无缩略图</span>
    </span>
    <span
      v-if="source && !failed && isVideoMedia(type)"
      class="thumbnail-play"
      aria-hidden="true"
    >
      <Play
        :size="12"
        fill="currentColor"
      />
    </span>
  </span>
</template>
