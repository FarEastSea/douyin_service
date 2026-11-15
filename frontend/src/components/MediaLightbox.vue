<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from "vue";
import {
  ChevronLeft,
  ChevronRight,
  Download,
  Pause,
  Play,
  X,
  ZoomIn,
  ZoomOut,
} from "@lucide/vue";
import {
  focusFirst,
  restoreFocus,
  trapFocus,
  captureFocusOrigin,
} from "../focus";
import { modalDepth } from "../workspace";
import type { MediaItem } from "../types";

const props = defineProps<{
  open: boolean;
  items: MediaItem[];
  start?: number;
}>();
const emit = defineEmits<{ close: [] }>();

const MIN_SCALE = 1;
const MAX_SCALE = 5;
const SLIDE_SECONDS = 4;
const SWIPE_DISTANCE = 60;
const SPEEDS = [1, 1.25, 1.5, 2, 0.5];

const index = ref(0);
const failed = ref(false);
const dialogElement = ref<HTMLElement | null>(null);
const videoElement = ref<HTMLVideoElement | null>(null);
const scale = ref(1);
const offset = ref({ x: 0, y: 0 });
const speed = ref(1);
const slideshow = ref(false);
let returnFocus: HTMLElement | null = null;
let slideTimer: number | undefined;
let gesture: { id: number; x: number; y: number; originX: number; originY: number; pan: boolean } | null = null;

const current = computed(() => props.items[index.value]);
const isImage = computed(() => current.value?.type === "image" && !failed.value);
const zoomed = computed(() => scale.value > 1.01);
const imageStyle = computed(() => ({
  transform: `translate(${offset.value.x}px, ${offset.value.y}px) scale(${scale.value})`,
  cursor: zoomed.value ? "grab" : "default",
}));

function resetView() {
  scale.value = 1;
  offset.value = { x: 0, y: 0 };
}
function move(delta: number) {
  if (!props.items.length) return;
  index.value = (index.value + delta + props.items.length) % props.items.length;
}
function jump(target: number) {
  if (props.items.length) index.value = Math.min(Math.max(target, 0), props.items.length - 1);
}
function zoom(factor: number) {
  if (!isImage.value) return;
  const next = Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale.value * factor));
  scale.value = next;
  if (next <= 1.01) resetView();
}
function toggleZoom() {
  if (!isImage.value) return;
  if (zoomed.value) resetView();
  else scale.value = 2.5;
}
function wheel(event: WheelEvent) {
  if (!isImage.value) return;
  event.preventDefault();
  zoom(event.deltaY < 0 ? 1.2 : 1 / 1.2);
}
function pointerDown(event: PointerEvent) {
  // 视频自带的进度条需要横向拖动，不在视频上识别滑动手势。
  if (!isImage.value || (event.target as HTMLElement).closest("button, a")) return;
  if (event.pointerType === "mouse" && event.button !== 0) return;
  gesture = {
    id: event.pointerId, x: event.clientX, y: event.clientY,
    originX: offset.value.x, originY: offset.value.y, pan: zoomed.value,
  };
}
function pointerMove(event: PointerEvent) {
  if (!gesture || gesture.id !== event.pointerId) return;
  // 鼠标在舞台之外松开时收不到 pointerup；按键已松开就结束这次拖动，避免图片继续跟随光标。
  if (event.pointerType === "mouse" && event.buttons === 0) {
    gesture = null;
    return;
  }
  if (!gesture.pan) return;
  offset.value = {
    x: gesture.originX + event.clientX - gesture.x,
    y: gesture.originY + event.clientY - gesture.y,
  };
}
function pointerUp(event: PointerEvent) {
  if (!gesture || gesture.id !== event.pointerId) return;
  const dx = event.clientX - gesture.x, dy = event.clientY - gesture.y;
  const swipe = !gesture.pan && event.pointerType !== "mouse";
  gesture = null;
  if (swipe && props.items.length > 1 && Math.abs(dx) > SWIPE_DISTANCE && Math.abs(dx) > Math.abs(dy) * 1.5)
    move(dx < 0 ? 1 : -1);
}
function cycleSpeed() {
  speed.value = SPEEDS[(SPEEDS.indexOf(speed.value) + 1) % SPEEDS.length]!;
}
function applySpeed() {
  if (videoElement.value) videoElement.value.playbackRate = speed.value;
}
function clearSlideTimer() {
  window.clearTimeout(slideTimer);
  slideTimer = undefined;
}
function scheduleSlide() {
  clearSlideTimer();
  // 图片定时切换；视频播放完再切换（见 videoEnded），加载失败的项目直接跳过。
  if (!slideshow.value || !props.open || props.items.length < 2) return;
  if (current.value?.type === "video" && !failed.value) return;
  slideTimer = window.setTimeout(() => move(1), (failed.value ? 1 : SLIDE_SECONDS) * 1000);
}
// 浏览器可能拒绝带声音的自动播放；静音后再试一次，仍被拒绝则按图片的节奏定时切换，避免连续播放停在这一项。
async function autoplay() {
  const video = videoElement.value;
  if (!video) return;
  try {
    await video.play();
  } catch {
    video.muted = true;
    try {
      await video.play();
    } catch {
      if (slideshow.value && videoElement.value === video) {
        clearSlideTimer();
        slideTimer = window.setTimeout(() => move(1), SLIDE_SECONDS * 1000);
      }
    }
  }
}
function toggleSlideshow() {
  slideshow.value = !slideshow.value;
  if (slideshow.value && current.value?.type === "video") void autoplay();
}
function videoEnded() {
  if (slideshow.value && props.items.length > 1) move(1);
}
function videoReady() {
  applySpeed();
  if (slideshow.value) void autoplay();
}
function preloadNeighbours() {
  for (const delta of [1, -1]) {
    const item = props.items[(index.value + delta + props.items.length) % props.items.length];
    if (item?.type === "image" && item !== current.value) new Image().src = item.url;
  }
}
function close() {
  emit("close");
}
function keydown(event: KeyboardEvent) {
  if (!props.open) return;
  if (event.key === "Escape") {
    if (zoomed.value) resetView();
    else close();
  } else if (event.key === "ArrowLeft") move(-1);
  else if (event.key === "ArrowRight") move(1);
  else if (event.key === "Home") jump(0);
  else if (event.key === "End") jump(props.items.length - 1);
  else if (event.key === "+" || event.key === "=") zoom(1.25);
  else if (event.key === "-") zoom(0.8);
  else if (event.key === "0") resetView();
  trapFocus(event, dialogElement.value);
}
watch(index, () => {
  failed.value = false;
  resetView();
  if (props.open && props.items.length > 1) preloadNeighbours();
  scheduleSlide();
});
watch([slideshow, failed], scheduleSlide);
watch(speed, applySpeed);
watch(
  () => props.open,
  async (open) => {
    modalDepth.value += open ? 1 : -1;
    if (open) {
      returnFocus = captureFocusOrigin();
      index.value = Math.min(
        Math.max(props.start || 0, 0),
        Math.max(0, props.items.length - 1),
      );
      failed.value = false;
      slideshow.value = false;
      resetView();
      if (props.items.length > 1) preloadNeighbours();
      await nextTick();
      focusFirst(
        dialogElement.value,
        dialogElement.value?.querySelector<HTMLElement>(".lightbox-close"),
      );
    } else {
      clearSlideTimer();
      slideshow.value = false;
      gesture = null;
      const target = returnFocus;
      returnFocus = null;
      await nextTick();
      restoreFocus(target);
    }
  },
);
onMounted(() => window.addEventListener("keydown", keydown));
onBeforeUnmount(() => {
  window.removeEventListener("keydown", keydown);
  clearSlideTimer();
  if (props.open) modalDepth.value--;
});
</script>

<template>
  <Teleport to="body">
    <Transition name="fade">
      <div
        v-if="open"
        ref="dialogElement"
        class="lightbox"
        role="dialog"
        aria-modal="true"
        aria-label="媒体预览"
        tabindex="-1"
        @click.self="close"
      >
        <header class="lightbox-bar">
          <div>
            <strong>{{ current?.title || "媒体预览" }}</strong
            ><span v-if="items.length > 1"
              >{{ index + 1 }} / {{ items.length }}</span
            >
          </div>
          <div class="lightbox-tools">
            <template v-if="isImage">
              <button class="icon-btn" title="缩小 (-)" aria-label="缩小" :disabled="!zoomed" @click="zoom(0.8)"><ZoomOut :size="18" /></button>
              <button class="lightbox-chip" :title="zoomed ? '恢复原始大小 (0)' : '放大 (+)'" :aria-label="`当前缩放 ${Math.round(scale * 100)}%，点击${zoomed ? '恢复' : '放大'}`" @click="toggleZoom">{{ Math.round(scale * 100) }}%</button>
              <button class="icon-btn" title="放大 (+)" aria-label="放大" :disabled="scale >= MAX_SCALE" @click="zoom(1.25)"><ZoomIn :size="18" /></button>
            </template>
            <button v-else-if="current?.type === 'video' && !failed" class="lightbox-chip" title="切换播放速度" :aria-label="`播放速度 ${speed} 倍，点击切换`" @click="cycleSpeed">{{ speed }}×</button>
            <button
              v-if="items.length > 1"
              class="icon-btn"
              :class="{ active: slideshow }"
              :title="slideshow ? '停止连续播放' : '连续播放'"
              :aria-label="slideshow ? '停止连续播放' : '连续播放'"
              :aria-pressed="slideshow"
              @click="toggleSlideshow"
            >
              <Pause v-if="slideshow" :size="18" /><Play v-else :size="18" />
            </button>
            <a
              v-if="current"
              class="icon-btn"
              :href="current.url"
              download
              title="下载"
              aria-label="下载当前媒体"
              ><Download :size="18"
            /></a>
            <button
              class="icon-btn lightbox-close"
              title="关闭"
              aria-label="关闭媒体预览"
              @click="close"
            >
              <X :size="20" />
            </button>
          </div>
        </header>
        <main
          class="lightbox-stage"
          :class="{ zoomed }"
          @wheel="wheel"
          @pointerdown="pointerDown"
          @pointermove="pointerMove"
          @pointerup="pointerUp"
          @pointercancel="gesture = null"
        >
          <div v-if="failed || !current" class="media-fallback">
            媒体加载失败，请关闭后重试
          </div>
          <video
            v-else-if="current.type === 'video'"
            ref="videoElement"
            :key="current.url"
            controls
            playsinline
            preload="metadata"
            :src="current.url"
            @loadedmetadata="videoReady"
            @ended="videoEnded"
            @error="failed = true"
          />
          <img
            v-else
            :key="current.url"
            :src="current.url"
            :alt="current.title || '图片预览'"
            :style="imageStyle"
            draggable="false"
            @dblclick="toggleZoom"
            @error="failed = true"
          />
          <button
            v-if="items.length > 1"
            class="lightbox-nav prev"
            aria-label="上一项"
            @click="move(-1)"
          >
            <ChevronLeft />
          </button>
          <button
            v-if="items.length > 1"
            class="lightbox-nav next"
            aria-label="下一项"
            @click="move(1)"
          >
            <ChevronRight />
          </button>
        </main>
      </div>
    </Transition>
  </Teleport>
</template>
