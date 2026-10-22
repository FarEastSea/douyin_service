<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import { UserRound } from "@lucide/vue";
import { getToken } from "../api";

const props = defineProps<{
  id: number;
  platform: string;
  url?: string | null;
}>();
const element = ref<HTMLElement | null>(null),
  visible = ref(false);
const source = ref(""),
  loaded = ref(false),
  failed = ref(false);
let controller: AbortController | undefined,
  objectUrl = "",
  sequence = 0;
let observer: IntersectionObserver | undefined,
  lastAttempt = 0;
function release() {
  controller?.abort();
  if (objectUrl) URL.revokeObjectURL(objectUrl);
  objectUrl = "";
}
async function load() {
  if (!visible.value) return;
  const current = ++sequence;
  release();
  source.value = "";
  loaded.value = false;
  failed.value = false;
  lastAttempt = Date.now();
  if (props.platform !== "douyin") {
    source.value = props.url || "";
    return;
  }
  controller = new AbortController();
  const token = getToken();
  try {
    // Image elements cannot attach the token; do not depend on a media cookie
    // being present or current after switching origins or updating credentials.
    const response = await fetch(`/api/authors/${props.id}/avatar`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      signal: controller.signal,
      credentials: "same-origin",
    });
    if (
      !response.ok ||
      !response.headers.get("content-type")?.startsWith("image/")
    )
      throw new Error("Avatar unavailable");
    const blob = await response.blob();
    if (current !== sequence) return;
    objectUrl = URL.createObjectURL(blob);
    source.value = objectUrl;
  } catch {
    if (current === sequence) failed.value = true;
  }
}
function retry() {
  if (!document.hidden && failed.value && Date.now() - lastAttempt > 30_000)
    void load();
}
watch([() => props.id, () => props.platform, () => props.url, visible], load);
onMounted(() => {
  if (typeof IntersectionObserver === "undefined") visible.value = true;
  else {
    observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          visible.value = true;
          observer?.disconnect();
        }
      },
      { rootMargin: "100px" },
    );
    if (element.value) observer.observe(element.value);
  }
  window.addEventListener("online", retry);
  document.addEventListener("visibilitychange", retry);
});
onBeforeUnmount(() => {
  ++sequence;
  release();
  observer?.disconnect();
  window.removeEventListener("online", retry);
  document.removeEventListener("visibilitychange", retry);
});
</script>
<template>
  <span
    ref="element"
    class="avatar author-avatar"
    aria-hidden="true"
    :data-loaded="loaded && !failed"
  >
    <UserRound v-if="!loaded || failed" :size="18" />
    <img
      v-if="source && !failed"
      :src="source"
      alt=""
      width="32"
      height="32"
      referrerpolicy="no-referrer"
      decoding="async"
      @load="loaded = true"
      @error="
        failed = true;
        loaded = false;
      "
    />
  </span>
</template>
