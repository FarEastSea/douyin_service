let menuOrigin: HTMLElement | null = null;
export function rememberMenuOrigin(target: HTMLElement | null) {
  menuOrigin = target;
}
export function releaseMenuOrigin(target: HTMLElement | null) {
  window.setTimeout(() => {
    if (menuOrigin === target) menuOrigin = null;
  }, 0);
}
export function captureFocusOrigin() {
  const target = menuOrigin;
  menuOrigin = null;
  return target?.isConnected
    ? target
    : document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
}

const focusableSelector = [
  "a[href]",
  "summary",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "video[controls]",
  "audio[controls]",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

export function focusableElements(container: HTMLElement | null) {
  if (!container) return [];
  return [...container.querySelectorAll<HTMLElement>(focusableSelector)].filter(
    (element) =>
      !element.closest("[inert]") && element.getClientRects().length > 0,
  );
}

export function focusFirst(
  container: HTMLElement | null,
  preferred?: HTMLElement | null,
) {
  const target =
    preferred && container?.contains(preferred)
      ? preferred
      : focusableElements(container)[0] || container;
  target?.focus();
}

export function trapFocus(event: KeyboardEvent, container: HTMLElement | null) {
  if (event.key !== "Tab" || !container) return;
  const focusable = focusableElements(container);
  const first = focusable[0],
    last = focusable.at(-1);
  if (!first || !last) {
    event.preventDefault();
    container.focus();
    return;
  }
  if (!container.contains(document.activeElement)) {
    event.preventDefault();
    first.focus();
  } else if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

export function restoreFocus(target: HTMLElement | null | undefined) {
  if (target?.isConnected && !target.closest("[inert]")) target.focus();
  else {
    const fallback = document.getElementById("main-content");
    if (fallback && !fallback.closest("[inert]")) fallback.focus();
  }
}
