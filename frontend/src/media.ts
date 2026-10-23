import type { MediaItem } from './types'

export function isVideoMedia(type?: string | null) {
  return type === 'video' || type === 'live' || type === 'live_photo'
}

export function mediaPreviewType(type?: string | null): MediaItem['type'] {
  return isVideoMedia(type) ? 'video' : 'image'
}

export function openMedia(items: MediaItem[], start = 0) {
  window.dispatchEvent(new CustomEvent('app:preview', { detail: { items, start } }))
}
