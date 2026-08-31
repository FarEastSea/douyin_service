export interface PageData<T> { items: T[]; total: number; page: number; page_size: number; pages: number }

export interface PlatformCapabilities {
  tasks: boolean; authors: boolean; works: boolean; subscriptions: boolean;
  subscription_reports: boolean; settings: boolean; profile_download: boolean; work_download: boolean;
}
export interface MediaPlatform {
  id: string; name: string; short_name: string; route_prefix: string; icon_text: string;
  domains: string[]; capabilities: PlatformCapabilities;
}

export interface Task {
  id: number; file_name?: string; status: string; total_bytes: number; downloaded_bytes: number;
  download_speed: number; progress_percent: number; error_message?: string; retry_count: number;
  author_id?: number; author_nickname?: string; work_title?: string; work_type?: string;
  preview_media_type?: 'image' | 'video'; preview_url?: string; local_preview_available: boolean;
  created_at: string; completed_at?: string; error_code?: string; error_category?: string;
  error_action?: string; retry_after?: number;
}

export interface Author {
  id: number; sec_uid: string; nickname?: string; avatar_url?: string; share_url?: string;
  is_subscribed: boolean; total_works: number; downloaded_works: number; last_error?: string;
  auto_update_status?: string; auto_update_message?: string; created_at: string;
}

export interface WorkFile { task_id: number; file_index: number; status: string; file_name?: string; preview_url?: string; media_type: string; local_available: boolean }
export interface Work {
  id: number; aweme_id: string; author_id: number; title?: string; work_type: string; image_count: number;
  is_downloaded: boolean; discovered_at: string; published_at?: string; video_url?: string;
  cover_url?: string; duration_ms?: number; width?: number; height?: number;
  music_title?: string; music_author?: string; music_url?: string; hashtags: string[];
  metadata_schema_version: number; raw_data_version: number; metadata_refreshed_at?: string;
  digg_count?: number; comment_count?: number; collect_count?: number; share_count?: number; play_count?: number;
  image_urls: string[]; primary_preview_url?: string; download_status: string;
  completed_task_count: number; total_task_count: number; files: WorkFile[];
}

export interface XTask {
  id: number; username: string; status: string; phase?: string; file_count: number;
  progress_percent: number; error_message?: string; last_log_line?: string; preview_count: number;
  created_at: string;
}
export interface XAuthor { id: number; username: string; display_name?: string; avatar_url?: string; is_subscribed: boolean; total_downloads: number; account_status_label?: string; last_error?: string }
export interface MediaItem { url: string; type: 'image' | 'video'; title?: string }
