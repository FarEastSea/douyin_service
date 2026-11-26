import { watch } from "vue";
import { useAppStore } from "../stores/app";

export function usePagePolling(intervalMs: number, refresh: () => void) {
  const store = useAppStore();
  let next = Date.now() + intervalMs;
  watch(
    () => store.pollTick,
    (now) => {
      if (!store.authenticated || (now < next && now !== store.resumedAt)) return;
      next = now + intervalMs;
      refresh();
    },
  );
}
