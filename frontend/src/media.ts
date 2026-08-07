import type { MediaItem } from './types'

export function openMedia(items: MediaItem[], start = 0) {
  window.dispatchEvent(new CustomEvent('app:preview', { detail: { items, start } }))
}
