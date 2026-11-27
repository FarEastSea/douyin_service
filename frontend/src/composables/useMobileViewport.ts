import { onMounted, onBeforeUnmount } from "vue";

// Chromium can resize the layout viewport itself. On visual-viewport-only
// browsers, expose only the part actually obscured by the on-screen keyboard.
export function useMobileViewport() {
  const viewport = window.visualViewport;
  let frame = 0;
  let top = 0;
  let bottom = 0;
  function measure() {
    frame = 0;
    const mobile =
      viewport && viewport.scale === 1 && window.matchMedia("(max-width: 899px)").matches;
    const nextTop = mobile ? Math.max(0, Math.round(viewport.offsetTop)) : 0;
    const nextBottom = mobile
      ? Math.max(0, Math.round(window.innerHeight - viewport.height - viewport.offsetTop))
      : 0;
    const style = document.documentElement.style;
    if (nextTop !== top) style.setProperty("--viewport-top", nextTop + "px");
    if (nextBottom !== bottom) style.setProperty("--viewport-bottom", nextBottom + "px");
    top = nextTop;
    bottom = nextBottom;
  }
  function schedule() {
    if (!frame) frame = requestAnimationFrame(measure);
  }
  onMounted(() => {
    measure();
    viewport?.addEventListener("resize", schedule);
    viewport?.addEventListener("scroll", schedule);
    window.addEventListener("resize", schedule);
  });
  onBeforeUnmount(() => {
    cancelAnimationFrame(frame);
    viewport?.removeEventListener("resize", schedule);
    viewport?.removeEventListener("scroll", schedule);
    window.removeEventListener("resize", schedule);
    document.documentElement.style.removeProperty("--viewport-top");
    document.documentElement.style.removeProperty("--viewport-bottom");
  });
}
