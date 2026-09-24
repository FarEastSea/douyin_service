export type SettingsMode = 'settings' | 'maintenance'

export interface SettingsNavItem {
  id: string
  label: string
}

export interface SettingsNavGroup {
  id: string
  label: string
  items: SettingsNavItem[]
}

export interface SettingsFieldSection {
  title: string
  description: string
  envKeys?: string[]
  runtimeKeys?: string[]
}

export interface SettingsPageDefinition {
  id: string
  title: string
  description: string
  fieldSections?: SettingsFieldSection[]
}

export const settingsPages: Record<string, SettingsPageDefinition> = {
  application: {
    id: 'application', title: '应用与安全', description: '管理服务名称、调试开关和管理端访问边界。',
    fieldSections: [
      { title: '应用身份', description: '用于识别当前服务实例，不影响已有媒体数据。', envKeys: ['APP_NAME', 'DEBUG'] },
      { title: '访问安全', description: '管理 Token 和跨域来源属于敏感边界，修改后立即使用新值。', envKeys: ['ADMIN_TOKEN', 'CORS_ALLOWED_ORIGINS'] },
    ],
  },
  storage: {
    id: 'storage', title: '下载目录', description: '统一管理下载根目录和各平台的相对子目录。',
    fieldSections: [
      { title: '根目录', description: '所有平台媒体都位于同一个根目录下。', envKeys: ['DOWNLOAD_ROOT'] },
      { title: '平台子目录', description: '仅填写相对目录；保存时系统会同步迁移数据库中的已有路径。', envKeys: ['DOUYIN_DOWNLOAD_SUBDIR', 'X_DOWNLOAD_SUBDIR', 'TIKTOK_DOWNLOAD_SUBDIR', 'WEIBO_DOWNLOAD_SUBDIR', 'BILIBILI_DOWNLOAD_SUBDIR', 'XHS_DOWNLOAD_SUBDIR'] },
    ],
  },
  connections: {
    id: 'connections', title: '数据库与 Redis', description: '配置持久化数据库、缓存和消息连接。',
    fieldSections: [
      { title: '数据库', description: '当前生产环境使用 PostgreSQL；密码始终保持不回显。', envKeys: ['DB_TYPE', 'DB_HOST', 'DB_PORT', 'DB_USER', 'DB_PASSWORD', 'DB_NAME'] },
      { title: 'Redis', description: '用于任务状态、缓存、限流和配置同步。', envKeys: ['REDIS_URL', 'REDIS_PASSWORD'] },
    ],
  },
  jobs: {
    id: 'jobs', title: '后台任务', description: '配置 Celery 使用的消息队列与结果存储。',
    fieldSections: [
      { title: '任务基础设施', description: '修改连接后，相关进程可能需要由部署系统重新启动。', envKeys: ['CELERY_BROKER_URL', 'CELERY_RESULT_BACKEND'] },
    ],
  },
  downloads: {
    id: 'downloads', title: '下载执行', description: '控制下载并发、超时、重试和卡住任务判定。',
    fieldSections: [
      { title: '并发与传输', description: '限制同时下载数量和每次写入的数据块大小。', envKeys: ['MAX_CONCURRENT_DOWNLOADS', 'DOWNLOAD_CHUNK_SIZE'] },
      { title: '失败恢复', description: '运行期参数在下一次任务调用时生效。', runtimeKeys: ['download_timeout', 'download_retry_count', 'download_retry_delay', 'stuck_task_timeout'] },
    ],
  },
  subscriptions: {
    id: 'subscriptions', title: '订阅检查', description: '控制自动检查周期、有界增量扫描和定期全量对账。',
    fieldSections: [
      { title: '调度周期', description: '启用自动检查，并限制允许配置的最短检查周期。', envKeys: ['MIN_CHECK_INTERVAL'], runtimeKeys: ['auto_check_enabled', 'subscription_check_interval'] },
      { title: '增量扫描', description: '在安全回看窗口内识别已知作品，减少无意义的历史翻页。', runtimeKeys: ['subscription_known_streak', 'subscription_max_pages', 'subscription_safe_lookback_pages', 'subscription_full_reconcile_interval'] },
    ],
  },
  risk: {
    id: 'risk', title: '抖音请求与风控', description: '控制抖音接口节奏、作者间隔和风控后的恢复策略。',
    fieldSections: [
      { title: '请求节奏', description: '所有 Worker 共享请求节奏，避免多个任务瞬间集中访问。', runtimeKeys: ['douyin_request_delay', 'author_check_delay'] },
      { title: '风控恢复', description: '进入冷却后暂停接口请求，满足条件后再恢复。', runtimeKeys: ['douyin_risk_cooldown_seconds', 'douyin_risk_auto_retry'] },
    ],
  },
  notifications: {
    id: 'notifications', title: '通知中心', description: '选择需要通知的事件，并配置 Webhook、Bark、邮件和 Gotify。',
    fieldSections: [
      { title: '通知规则', description: '统一开关、事件范围和相同通知的去重时间。', envKeys: ['NOTIFY_ENABLED', 'NOTIFY_ON_NEW_WORKS', 'NOTIFY_ON_DOWNLOAD_FAILURE', 'NOTIFY_ON_RISK', 'NOTIFY_ON_SUBSCRIPTION_FAILURE', 'NOTIFY_DEDUPE_SECONDS'] },
      { title: 'Webhook', description: '向自定义服务发送带可选 HMAC 签名的事件。', envKeys: ['WEBHOOK_ENABLED', 'WEBHOOK_URL', 'WEBHOOK_SECRET'] },
      { title: 'Bark', description: '向 Bark 设备推送通知。', envKeys: ['BARK_ENABLED', 'BARK_SERVER_URL', 'BARK_DEVICE_KEY'] },
      { title: '邮件', description: '通过 SMTP 向一个或多个收件地址发送通知。', envKeys: ['EMAIL_ENABLED', 'SMTP_HOST', 'SMTP_PORT', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'SMTP_FROM', 'SMTP_TO', 'SMTP_SECURITY'] },
      { title: 'Gotify', description: '向自托管 Gotify 服务推送通知。', envKeys: ['GOTIFY_ENABLED', 'GOTIFY_SERVER_URL', 'GOTIFY_TOKEN'] },
    ],
  },
  'account-douyin': { id: 'account-douyin', title: '抖音账号', description: '管理 Cookie、浏览器身份、User-Agent 和代理绑定。' },
  'account-x': {
    id: 'account-x', title: 'X 账号', description: '管理 gallery-dl 引擎、Cookie 和任务日志保留。',
    fieldSections: [{ title: '下载选项', description: 'Cookie 内容在下方凭据区域单独保存。', envKeys: ['X_DOWNLOAD_ENGINE', 'X_COOKIE_FILE', 'X_TASK_LOG_MAX_LINES', 'X_TASK_LOG_TTL_SECONDS', 'X_TASK_STATE_TTL_SECONDS'] }],
  },
  'account-tiktok': {
    id: 'account-tiktok', title: 'TikTok 账号', description: '管理 TikTok 主页与单条作品下载凭据。',
    fieldSections: [{ title: '下载选项', description: '网页加密 Cookie 优先于可选的服务器 Cookie 文件。', envKeys: ['TIKTOK_DOWNLOAD_ENGINE', 'TIKTOK_COOKIE_FILE'] }],
  },
  'account-weibo': {
    id: 'account-weibo', title: '微博账号', description: '管理微博主页与单条动态下载凭据。',
    fieldSections: [{ title: '下载选项', description: '网页加密 Cookie 优先于可选的服务器 Cookie 文件。', envKeys: ['WEIBO_DOWNLOAD_ENGINE', 'WEIBO_COOKIE_FILE'] }],
  },
  'account-bilibili': {
    id: 'account-bilibili', title: 'B站账号', description: '管理 B站用户主页与单条视频下载凭据。',
    fieldSections: [{ title: '下载选项', description: '网页加密 Cookie 优先于可选的服务器 Cookie 文件。', envKeys: ['BILIBILI_DOWNLOAD_ENGINE', 'BILIBILI_COOKIE_FILE'] }],
  },
  'account-xhs': {
    id: 'account-xhs', title: '小红书账号', description: '仅用于单条图文、视频和实况笔记，不开放作者主页批量采集。',
    fieldSections: [{ title: '下载选项', description: 'Cookie 用于需要登录或更高质量的单条笔记。', envKeys: ['XHS_DOWNLOAD_ENGINE', 'XHS_COOKIE_FILE'] }],
  },
  archive: { id: 'archive', title: '归档与导出', description: '定义新任务使用的目录、文件名、作品范围和元数据规则。' },
  other: { id: 'other', title: '其他配置', description: '后端新增但尚未归入固定分类的配置。' },
}

export const maintenancePages: Record<string, SettingsPageDefinition> = {
  services: { id: 'services', title: '进程与依赖', description: '检查 Web 依赖、下载 Worker 和定时调度器。' },
  platforms: { id: 'platforms', title: '平台就绪', description: '核对平台引擎、Cookie、FFmpeg、目录和真实任务证据。' },
  storage: { id: 'storage', title: '存储巡检与维护', description: '扫描数据库与媒体文件，并采用可恢复方式处理问题。' },
  logs: { id: 'logs', title: '活动日志', description: '查看可操作事件、失败证据和关联请求。' },
  update: { id: 'update', title: '更新与系统信息', description: '检查版本、复制诊断并查看系统入口。' },
}

export const settingsNavigation: SettingsNavGroup[] = [
  { id: 'infrastructure', label: '基础设施', items: [
    { id: 'application', label: '应用与安全' }, { id: 'storage', label: '下载目录' },
    { id: 'connections', label: '数据库与 Redis' }, { id: 'jobs', label: '后台任务' },
  ] },
  { id: 'automation', label: '下载与自动化', items: [
    { id: 'downloads', label: '下载执行' }, { id: 'subscriptions', label: '订阅检查' },
    { id: 'risk', label: '抖音请求与风控' }, { id: 'notifications', label: '通知中心' },
  ] },
  { id: 'accounts', label: '平台账号', items: [
    { id: 'account-douyin', label: '抖音' }, { id: 'account-x', label: 'X' },
    { id: 'account-tiktok', label: 'TikTok' }, { id: 'account-weibo', label: '微博' },
    { id: 'account-bilibili', label: 'B站' }, { id: 'account-xhs', label: '小红书' },
  ] },
  { id: 'archive', label: '归档与导出', items: [{ id: 'archive', label: '归档与导出' }] },
]

export const maintenanceNavigation: SettingsNavGroup[] = [
  { id: 'service', label: '服务状态', items: [
    { id: 'services', label: '进程与依赖' }, { id: 'platforms', label: '平台就绪' },
  ] },
  { id: 'storage', label: '存储', items: [{ id: 'storage', label: '存储巡检与维护' }] },
  { id: 'diagnostics', label: '诊断', items: [
    { id: 'logs', label: '活动日志' }, { id: 'update', label: '更新与系统信息' },
  ] },
]

export const platformCredentials = [
  { id: 'tiktok', pageId: 'account-tiktok', name: 'TikTok', cookieKey: 'TIKTOK_COOKIE' },
  { id: 'weibo', pageId: 'account-weibo', name: '微博', cookieKey: 'WEIBO_COOKIE' },
  { id: 'bilibili', pageId: 'account-bilibili', name: 'B站', cookieKey: 'BILIBILI_COOKIE' },
  { id: 'xhs', pageId: 'account-xhs', name: '小红书', cookieKey: 'XHS_COOKIE' },
] as const

export const knownCredentialKeys = new Set([
  'DOUYIN_COOKIE', 'X_COOKIE', ...platformCredentials.map(item => item.cookieKey),
])

export const assignedEnvKeys = new Set([
  ...Object.values(settingsPages).flatMap(page => page.fieldSections?.flatMap(section => section.envKeys || []) || []),
  ...knownCredentialKeys,
])

export const settingsSectionIds = new Set(Object.keys(settingsPages))
export const maintenanceSectionIds = new Set(Object.keys(maintenancePages))

export function sectionPath(mode: SettingsMode, section: string) {
  return mode === 'maintenance' ? `/operations/maintenance/${section}` : `/settings/${section}`
}
