        const API_BASE = '/api';

        // 平台 / 子标签状态
        let currentPlatform = 'douyin';
        let currentSubTab = { douyin: 'tasks', x: 'tasks' };
        let loadedPanels = {};
        let searchTimeout = null;

        // 抖音状态
        let currentStatusFilter = '';
        let currentTaskAuthorFilter = null;
        let currentTaskAuthorName = '';
        let tasksPage = 1, tasksPageSize = 10, tasksTotalPages = 1, tasksTotal = 0;
        let authorsPage = 1, authorsPageSize = 10, authorsTotalPages = 1, authorsTotal = 0;
        let authorsSubscribeFilter = '', authorsStatusFilter = 'all';
        let pollTimer = null, pollInterval = 5000;
        let runtimeConfig = null;
        let bootstrapMode = false;
        let currentMediaPreviewType = null;
        let currentPreviewImages = [];
        let currentPreviewImageIndex = 0;
        let currentAuthorPreview = null;
        let selectedWorkIds = new Set();
        const customSelectRegistry = new Map();

        // X 状态
        let currentXStatusFilter = '';
        let xTasksPage = 1, xTasksPageSize = 10, xTasksTotalPages = 1, xTasksTotal = 0;
        let xAuthorsPage = 1, xAuthorsPageSize = 10, xAuthorsTotalPages = 1, xAuthorsTotal = 0;

        // 动态调整轮询频率
        function adjustPollInterval(downloadingCount) {
            const newInterval = downloadingCount > 0 ? 3000 : 10000; // 有下载任务3秒，无下载任务10秒
            if (newInterval !== pollInterval) {
                pollInterval = newInterval;
                clearInterval(pollTimer);
                pollTimer = setInterval(refreshTasks, pollInterval);
            }
        }

        function normalizeApiErrorMessage(message, fallback = '请求失败') {
            const rawMessage = typeof message === 'string' ? message.trim() : '';
            if (!rawMessage) {
                return fallback;
            }

            const plainText = rawMessage
                .replace(/<script[\s\S]*?<\/script>/gi, ' ')
                .replace(/<style[\s\S]*?<\/style>/gi, ' ')
                .replace(/<[^>]+>/g, ' ')
                .replace(/&nbsp;/gi, ' ')
                .replace(/&lt;/gi, '<')
                .replace(/&gt;/gi, '>')
                .replace(/&amp;/gi, '&')
                .replace(/\s+/g, ' ')
                .trim();

            if (/502\s+Bad Gateway/i.test(plainText) || /Bad Gateway/i.test(plainText)) {
                return '服务网关返回 502，后端可能正在重启或请求抖音超时，请稍后重试';
            }

            return plainText || fallback;
        }

        let toastTimer = null;

        // 显示提示
        function showToast(message, type = 'success') {
            const toast = document.getElementById('toast');
            const text = typeof message === 'string' ? message : JSON.stringify(message);
            const displayText = type === 'error'
                ? normalizeApiErrorMessage(text)
                : text;
            toast.textContent = displayText;
            toast.className = `toast ${type} show`;
            if (toastTimer) {
                clearTimeout(toastTimer);
            }
            toastTimer = setTimeout(() => {
                toast.classList.remove('show');
                toastTimer = null;
            }, type === 'error' ? 12000 : 3000);
        }

        // 格式化文件大小
        function formatSize(bytes) {
            if (bytes < 1024) return bytes + ' B';
            if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
            if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' MB';
            return (bytes / 1024 / 1024 / 1024).toFixed(2) + ' GB';
        }

        function updateTaskAuthorFilterBanner() {
            const banner = document.getElementById('taskAuthorFilterBanner');
            const text = document.getElementById('taskAuthorFilterText');
            if (!banner || !text) return;

            if (!currentTaskAuthorFilter) {
                banner.classList.remove('active');
                text.innerHTML = '';
                return;
            }

            const safeAuthorName = escapeHtml(currentTaskAuthorName || `作者 ${currentTaskAuthorFilter}`);
            text.innerHTML = `当前仅显示 <strong>${safeAuthorName}</strong> 的下载任务`;
            banner.classList.add('active');
        }

        function syncBodyScrollLock() {
            document.body.style.overflow = document.querySelector('.preview-modal.show') ? 'hidden' : '';
        }

        // 统一请求封装，避免缓存导致数据过期
        function wrapApiResponse(response) {
            const safeJson = async () => {
                const text = await response.clone().text();
                if (!text) {
                    return null;
                }

                try {
                    return JSON.parse(text);
                } catch {
                    return {
                        detail: text,
                        message: text,
                        raw_text: text
                    };
                }
            };

            Object.defineProperty(response, 'json', {
                value: safeJson,
                configurable: true,
                writable: true
            });

            return response;
        }

        async function apiFetch(url, options = {}) {
            const headers = {
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache',
                ...(options.headers || {})
            };
            const response = await fetch(url, {
                cache: 'no-store',
                ...options,
                headers
            });

            return wrapApiResponse(response);
        }

        async function readApiResponseBody(response) {
            return response.json();
        }

        function getApiMessage(payload, fallback = '请求失败') {
            let message = fallback;
            if (payload && typeof payload === 'object') {
                message = payload.detail || payload.message || fallback;
            } else if (typeof payload === 'string' && payload.trim()) {
                message = payload.trim();
            }
            return normalizeApiErrorMessage(message, fallback);
        }

        async function apiRequest(url, options = {}, fallback = '请求失败') {
            const response = await apiFetch(url, options);
            const payload = await readApiResponseBody(response);
            if (!response.ok) {
                throw new Error(getApiMessage(payload, fallback));
            }
            return payload;
        }

        const AUTHOR_ACCOUNT_STATUS_MARKER_PREFIX = '__ACCOUNT_STATUS__|';

        function parseAuthorStatusMarker(lastError) {
            if (typeof lastError !== 'string' || !lastError.startsWith(AUTHOR_ACCOUNT_STATUS_MARKER_PREFIX)) {
                return {
                    code: '',
                    label: '',
                    detail: '',
                    errorMessage: typeof lastError === 'string' ? lastError : ''
                };
            }

            const parts = lastError.split('|', 4);
            const code = ['deleted', 'banned', 'restricted', 'unavailable'].includes(parts[1]) ? parts[1] : 'unavailable';
            return {
                code,
                label: parts[2] || '状态异常',
                detail: parts[3] || '',
                errorMessage: ''
            };
        }

        function getAuthorProfileUrl(author) {
            const shareUrl = typeof author?.share_url === 'string' ? author.share_url.trim() : '';
            if (shareUrl && /^https:\/\/www\.douyin\.com\/user\//.test(shareUrl)) {
                return shareUrl;
            }

            const secUid = typeof author?.sec_uid === 'string' ? author.sec_uid.trim() : '';
            if (!secUid) {
                return '';
            }

            let normalizedSecUid = secUid;
            try {
                normalizedSecUid = decodeURIComponent(secUid);
            } catch (_) {
                normalizedSecUid = secUid;
            }

            return `https://www.douyin.com/user/${encodeURIComponent(normalizedSecUid)}`;
        }

        function getStatusTag(status, retryCount) {
            const statusMap = {
                'downloading': ['下载中', 'status-downloading'],
                'completed': ['已完成', 'status-completed'],
                'paused': ['已暂停', 'status-paused'],
                'failed': ['失败', 'status-failed'],
                'pending': ['等待中', 'status-pending'],
                'cancelled': ['已取消', 'status-failed'],
                'partial': ['部分完成', 'status-paused'],
                'not_started': ['未开始', 'status-pending']
            };
            const [text, cls] = statusMap[status] || ['未知', 'status-pending'];
            let html = `<span class="status-tag ${cls}">${text}</span>`;
            if (retryCount > 0) {
                const badgeCls = retryCount >= 5 ? 'danger' : retryCount >= 3 ? 'warn' : 'normal';
                html += `<span class="retry-badge ${badgeCls}" title="已重试 ${retryCount} 次">×${retryCount}</span>`;
            }
            return html;
        }

        // 获取系统状态
        async function fetchStatus() {
            try {
                const res = await apiFetch(`${API_BASE}/status`);
                const data = await res.json();
                if (!res.ok) {
                    throw new Error(getApiMessage(data, '获取系统状态失败'));
                }

                document.getElementById('statAuthors').textContent = data.total_authors;
                document.getElementById('statSubscribed').textContent = data.subscribed_authors;
                document.getElementById('statPending').textContent = data.pending_tasks;
                document.getElementById('statDownloading').textContent = data.downloading_tasks;
                document.getElementById('statTotal').textContent = data.total_downloads;

                // 综合健康状态指示灯（结合 Redis + Worker + Beat）
                let healthLevel = 'green'; // 默认健康
                let healthMsg = '运行正常';
                const issues = [];

                if (!data.redis_connected) {
                    healthLevel = 'red';
                    issues.push('Redis 断开');
                }

                if (data.celery_workers <= 0) {
                    healthLevel = healthLevel === 'red' ? 'red' : 'yellow';
                    issues.push('Worker 离线');
                }

                // 检查 Beat 状态
                try {
                    const pRes = await apiFetch(`${API_BASE}/process/status`);
                    const pData = await pRes.json();
                    if (!pRes.ok) {
                        throw new Error(getApiMessage(pData, '获取进程状态失败'));
                    }

                    if (!pData.worker?.running) {
                        healthLevel = healthLevel === 'red' ? 'red' : 'yellow';
                        if (!issues.includes('Worker 离线')) issues.push('Worker 进程未运行');
                    }

                    if (!pData.beat?.running) {
                        if (healthLevel === 'green') healthLevel = 'yellow';
                        issues.push('Beat 未运行');
                    }
                } catch (_) {
                    if (healthLevel === 'green') healthLevel = 'yellow';
                    issues.push('进程状态未知');
                }

                const dot = document.getElementById('statusDot');
                const text = document.getElementById('statusText');

                if (healthLevel === 'red') {
                    dot.style.background = 'var(--error)';
                    healthMsg = issues.join(' · ');
                } else if (healthLevel === 'yellow') {
                    dot.style.background = 'var(--warning)';
                    healthMsg = issues.join(' · ');
                } else {
                    dot.style.background = 'var(--success)';
                    healthMsg = '运行正常';
                }
                text.textContent = healthMsg;
            } catch (e) {
                document.getElementById('statusDot').style.background = 'var(--error)';
                document.getElementById('statusText').textContent = '连接失败';
            }
        }

        // 检查 Cookie 状态
        async function checkCookie() {
            try {
                const res = await apiFetch(`${API_BASE}/config/cookie`);
                const data = await res.json();
                if (!res.ok) {
                    throw new Error(getApiMessage(data, '检查 Cookie 失败'));
                }
                const status = document.getElementById('cookieStatus');
                if (data.success) {
                    status.textContent = '已配置';
                    status.className = 'status-tag status-completed';
                } else {
                    status.textContent = '未配置';
                    status.className = 'status-tag status-failed';
                }
            } catch (e) {
                console.error('检查 Cookie 失败', e);
            }
        }

        // 保存 Cookie
        async function saveCookie() {
            const cookie = document.getElementById('cookieInput').value.trim();
            if (!cookie) {
                showToast('请输入 Cookie', 'error');
                return;
            }

            try {
                const res = await apiFetch(`${API_BASE}/config/cookie`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ cookie })
                });
                const data = await res.json();
                if (data.success) {
                    showToast(data.message || 'Cookie 保存成功');
                    checkCookie();
                    document.getElementById('cookieInput').value = '';
                } else {
                    showToast('保存失败: ' + data.message, 'error');
                }
            } catch (e) {
                showToast('保存失败', 'error');
            }
        }

        function setInputValue(id, value) {
            const el = document.getElementById(id);
            if (el && value !== undefined && value !== null) el.value = value;
        }

        function renderRuntimeConfig(config) {
            if (!config) return;
            runtimeConfig = config;
            const autoCheck = document.getElementById('autoCheckEnabled');
            if (autoCheck) autoCheck.checked = !!config.auto_check_enabled;
            setInputValue('subscriptionCheckHours', Math.max(1, Math.round((config.subscription_check_interval || 21600) / 3600)));
            setInputValue('douyinRequestDelay', config.douyin_request_delay ?? 3);
            setInputValue('authorCheckDelay', config.author_check_delay ?? 30);
            setInputValue('downloadTimeout', config.download_timeout ?? 30);
            setInputValue('downloadRetryCount', config.download_retry_count ?? 3);
            setInputValue('downloadRetryDelay', config.download_retry_delay ?? 5);
            setInputValue('stuckTaskTimeout', config.stuck_task_timeout ?? 600);
        }

        async function loadRuntimeConfig() {
            try {
                const res = await apiFetch(`${API_BASE}/config/runtime`);
                const data = await res.json();
                if (!res.ok) {
                    throw new Error(getApiMessage(data, '加载运行配置失败'));
                }
                if (data.config) {
                    renderRuntimeConfig(data.config);
                }
            } catch (e) {
                console.error('加载运行配置失败', e);
            }
        }

        function getRuntimeFormValues() {
            const hours = parseFloat(document.getElementById('subscriptionCheckHours')?.value || '6');
            const requestDelay = parseFloat(document.getElementById('douyinRequestDelay')?.value || '3');
            const authorDelay = parseFloat(document.getElementById('authorCheckDelay')?.value || '30');
            const timeout = parseInt(document.getElementById('downloadTimeout')?.value || '30');
            const retryCount = parseInt(document.getElementById('downloadRetryCount')?.value || '3');
            const retryDelay = parseInt(document.getElementById('downloadRetryDelay')?.value || '5');
            const stuckTimeout = parseInt(document.getElementById('stuckTaskTimeout')?.value || '600');

            const values = [hours, requestDelay, authorDelay, timeout, retryCount, retryDelay, stuckTimeout];
            if (values.some(v => Number.isNaN(v))) {
                throw new Error('请填写有效的数字配置');
            }

            return {
                auto_check_enabled: !!document.getElementById('autoCheckEnabled')?.checked,
                subscription_check_interval: Math.round(hours * 3600),
                douyin_request_delay: requestDelay,
                author_check_delay: authorDelay,
                download_timeout: timeout,
                download_retry_count: retryCount,
                download_retry_delay: retryDelay,
                stuck_task_timeout: stuckTimeout
            };
        }

        async function saveRuntimeConfig() {
            let payload;
            try {
                payload = getRuntimeFormValues();
            } catch (e) {
                showToast(e.message, 'error');
                return;
            }

            try {
                const res = await apiFetch(`${API_BASE}/config/runtime`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const data = await res.json();
                if (res.ok && data.success) {
                    renderRuntimeConfig(data.data?.config);
                    showToast(data.message || '运行配置已保存');
                } else {
                    showToast(data.detail || data.message || '保存失败', 'error');
                }
            } catch (e) {
                showToast('保存运行配置失败', 'error');
            }
        }

        const COMPLETE_CONFIG_DOMAINS = [
            { id: 'app', title: '应用与存储', description: '服务名称、调试模式与媒体保存位置', keys: ['APP_NAME', 'DEBUG', 'DOWNLOAD_DIR', 'X_DOWNLOAD_DIR'] },
            { id: 'auth', title: '平台认证', description: '抖音和 X/Twitter 的登录凭据与下载引擎', keys: ['DOUYIN_COOKIE', 'X_COOKIE', 'X_COOKIE_FILE', 'X_DOWNLOAD_ENGINE'] },
            { id: 'download', title: '下载执行', description: '并发、分块、超时和失败重试策略', keys: ['MAX_CONCURRENT_DOWNLOADS', 'DOWNLOAD_CHUNK_SIZE', 'DOWNLOAD_TIMEOUT', 'DOWNLOAD_RETRY_COUNT', 'DOWNLOAD_RETRY_DELAY', 'STUCK_TASK_TIMEOUT'] },
            { id: 'subscription', title: '订阅与风控', description: '自动检查频率和平台请求节流', keys: ['AUTO_CHECK_ENABLED', 'DEFAULT_CHECK_INTERVAL', 'MIN_CHECK_INTERVAL', 'REQUEST_DELAY', 'AUTHOR_CHECK_DELAY'] },
            { id: 'database', title: '数据库', description: 'PostgreSQL 或 MySQL 的生产连接信息', keys: ['DB_TYPE', 'DB_HOST', 'DB_PORT', 'DB_USER', 'DB_PASSWORD', 'DB_NAME'] },
            { id: 'queue', title: 'Redis 与任务队列', description: '缓存、消息队列和任务结果存储', keys: ['REDIS_URL', 'REDIS_PASSWORD', 'CELERY_BROKER_URL', 'CELERY_RESULT_BACKEND'] },
            { id: 'x-task', title: 'X 任务留存', description: 'X 下载任务日志与状态的保留策略', keys: ['X_TASK_LOG_MAX_LINES', 'X_TASK_LOG_TTL_SECONDS', 'X_TASK_STATE_TTL_SECONDS'] }
        ];
        const COMPLETE_CONFIG_BOOLEAN_KEYS = new Set(['DEBUG', 'AUTO_CHECK_ENABLED']);
        const COMPLETE_CONFIG_NUMBER_KEYS = new Set([
            'DB_PORT', 'MAX_CONCURRENT_DOWNLOADS', 'DOWNLOAD_CHUNK_SIZE', 'DOWNLOAD_TIMEOUT',
            'DOWNLOAD_RETRY_COUNT', 'DOWNLOAD_RETRY_DELAY', 'DEFAULT_CHECK_INTERVAL',
            'MIN_CHECK_INTERVAL', 'REQUEST_DELAY', 'AUTHOR_CHECK_DELAY', 'STUCK_TASK_TIMEOUT',
            'X_TASK_LOG_MAX_LINES', 'X_TASK_LOG_TTL_SECONDS', 'X_TASK_STATE_TTL_SECONDS'
        ]);
        let completeConfigLoaded = false;
        let completeConfigRuntimeKeys = new Set();
        let activeCompleteConfigDomain = COMPLETE_CONFIG_DOMAINS[0].id;

        function switchCompleteConfigDomain(domainId) {
            if (!COMPLETE_CONFIG_DOMAINS.some(domain => domain.id === domainId)) return;
            activeCompleteConfigDomain = domainId;

            document.querySelectorAll('#completeConfigNav [data-config-domain]').forEach(button => {
                const active = button.dataset.configDomain === domainId;
                button.classList.toggle('active', active);
                button.setAttribute('aria-selected', String(active));
                button.tabIndex = active ? 0 : -1;
            });
            document.querySelectorAll('#completeConfigGrid .config-domain').forEach(section => {
                const active = section.dataset.configDomain === domainId;
                section.classList.toggle('active', active);
                section.hidden = !active;
            });
            _recalcSettingsHeight();
        }

        function completeConfigInput(field, value) {
            const key = escapeHtml(field.key);
            const label = `${escapeHtml(field.label)}${field.required ? ' <span class="required-mark">*</span>' : ''}`;
            const hint = field.help ? `<span class="config-field-help">${escapeHtml(field.help)}</span>` : '';
            let control;
            if (COMPLETE_CONFIG_BOOLEAN_KEYS.has(field.key)) {
                const checked = String(value).toLowerCase() === 'true' ? ' checked' : '';
                control = `<label class="config-switch"><input type="checkbox" data-config-key="${key}"${checked}><span>${checked ? '已启用' : '已关闭'}</span></label>`;
            } else if (field.key === 'DB_TYPE') {
                control = `<select data-config-key="${key}">
                    <option value="postgresql"${value === 'postgresql' ? ' selected' : ''}>PostgreSQL</option>
                    <option value="mysql"${value === 'mysql' ? ' selected' : ''}>MySQL / MariaDB</option>
                </select>`;
            } else {
                const isCookie = field.key === 'DOUYIN_COOKIE' || field.key === 'X_COOKIE';
                const type = field.secret ? 'password' : COMPLETE_CONFIG_NUMBER_KEYS.has(field.key) ? 'number' : 'text';
                const step = ['REQUEST_DELAY', 'AUTHOR_CHECK_DELAY'].includes(field.key) ? 'any' : '1';
                const attrs = `${field.required ? ' required' : ''}${type === 'number' ? ` step="${step}" min="0"` : ''}`;
                control = isCookie
                    ? `<textarea data-config-key="${key}"${attrs} rows="3" autocomplete="off">${escapeHtml(String(value ?? ''))}</textarea>`
                    : `<input type="${type}" data-config-key="${key}" value="${escapeHtml(String(value ?? ''))}"${attrs} autocomplete="${field.secret ? 'new-password' : 'off'}">`;
            }
            const mode = completeConfigRuntimeKeys.has(field.key) || ['DOUYIN_COOKIE', 'X_COOKIE'].includes(field.key)
                ? '<span class="config-apply-badge immediate">立即生效</span>'
                : '<span class="config-apply-badge restart">重启生效</span>';
            return `<div class="config-field"><label>${label}${mode}</label>${control}${hint}<code>${key}</code></div>`;
        }

        function renderCompleteConfig(data) {
            const fields = data.fields || [];
            const values = data.values || {};
            const fieldMap = new Map(fields.map(field => [field.key, field]));
            completeConfigRuntimeKeys = new Set(data.runtime_keys || []);
            const nav = document.getElementById('completeConfigNav');
            const grid = document.getElementById('completeConfigGrid');
            if (!nav || !grid) return;

            nav.setAttribute('role', 'tablist');
            nav.innerHTML = COMPLETE_CONFIG_DOMAINS.map(domain =>
                `<button class="secondary" type="button" role="tab" data-config-domain="${domain.id}" aria-controls="config-domain-${domain.id}">${domain.title}</button>`
            ).join('');

            grid.innerHTML = COMPLETE_CONFIG_DOMAINS.map(domain => {
                const domainFields = domain.keys.map(key => fieldMap.get(key)).filter(Boolean);
                return `<section class="settings-section config-domain" id="config-domain-${domain.id}" role="tabpanel" data-config-domain="${domain.id}">
                    <div class="config-domain-heading"><div><h4>${domain.title}</h4><p>${domain.description}</p></div><span>${domainFields.length} 项</span></div>
                    <div class="settings-form-grid">${domainFields.map(field => completeConfigInput(field, values[field.key]?.value ?? field.default)).join('')}</div>
                </section>`;
            }).join('');

            nav.querySelectorAll('[data-config-domain]').forEach(button => {
                button.addEventListener('click', () => switchCompleteConfigDomain(button.dataset.configDomain));
            });
            switchCompleteConfigDomain(activeCompleteConfigDomain);

            grid.querySelectorAll('.config-switch input').forEach(input => {
                input.addEventListener('change', () => {
                    const text = input.parentElement.querySelector('span');
                    if (text) text.textContent = input.checked ? '已启用' : '已关闭';
                });
            });
            const status = document.getElementById('completeConfigStatus');
            if (status) {
                status.textContent = `${fields.length} 项配置`;
                status.className = 'status-tag status-completed';
            }
            completeConfigLoaded = true;
            _recalcSettingsHeight();
        }

        async function loadCompleteConfig() {
            const status = document.getElementById('completeConfigStatus');
            if (status) {
                status.textContent = '读取中';
                status.className = 'status-tag status-pending';
            }
            try {
                const data = await apiRequest(`${API_BASE}/config/all`, {}, '读取完整配置失败');
                renderCompleteConfig(data);
            } catch (e) {
                if (status) {
                    status.textContent = '读取失败';
                    status.className = 'status-tag status-failed';
                }
                const grid = document.getElementById('completeConfigGrid');
                if (grid) grid.innerHTML = `<div class="empty-state"><p>${escapeHtml(e.message || '读取完整配置失败')}</p></div>`;
            }
        }

        async function saveCompleteConfig() {
            const controls = [...document.querySelectorAll('#completeConfigGrid [data-config-key]')];
            if (!controls.length) {
                showToast('配置尚未加载', 'error');
                return;
            }
            const invalid = controls.find(control => !control.checkValidity());
            if (invalid) {
                const domainId = invalid.closest('.config-domain')?.dataset.configDomain;
                if (domainId) switchCompleteConfigDomain(domainId);
                invalid.reportValidity();
                return;
            }
            const values = {};
            controls.forEach(control => {
                values[control.dataset.configKey] = control.type === 'checkbox' ? control.checked : control.value;
            });
            try {
                const data = await apiRequest(`${API_BASE}/config/all`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ values })
                }, '保存完整配置失败');
                showToast(data.message || '配置已保存');
                await loadCompleteConfig();
                loadRuntimeConfig();
                loadDbConfig();
                checkCookie();
                checkXCookie();
            } catch (e) {
                showToast(e.message || '保存完整配置失败', 'error');
            }
        }
        function refreshTasks() {
            fetchTasks();
            fetchStatus();
        }

        // 获取任务列表
        async function fetchTasks() {
            try {
                let url = `${API_BASE}/tasks/?page=${tasksPage}&page_size=${tasksPageSize}`;
                if (currentStatusFilter) {
                    url += `&status=${currentStatusFilter}`;
                }
                if (currentTaskAuthorFilter) {
                    url += `&author_id=${encodeURIComponent(currentTaskAuthorFilter)}`;
                }
                const res = await apiFetch(url);
                const data = await res.json();
                if (!res.ok) {
                    throw new Error(getApiMessage(data, '获取任务失败'));
                }

                // 处理分页响应
                tasksTotal = data.total;
                tasksTotalPages = data.pages;

                // 统计下载中任务数量并调整轮询频率
                const downloadingCount = data.items.filter(t => t.status === 'downloading').length;
                adjustPollInterval(downloadingCount);
                updateTaskAuthorFilterBanner();

                renderTasks(data.items);
                renderTasksPagination();
            } catch (e) {
                console.error('获取任务失败', e);
                showToast(e.message || '获取任务失败', 'error');
            }
        }

        // 渲染任务列表
        function renderTasks(tasks) {
            const container = document.getElementById('taskList');
            hideErrorTooltip();

            if (!tasks.length) {
                container.innerHTML = `
                    <div class="empty-state">
                        <div class="empty-state-icon">📭</div>
                        <p>暂无下载任务</p>
                    </div>
                `;
                return;
            }

            // 收集当前页所有失败任务的错误信息用于"复制全部失败原因"
            const failedErrors = tasks.filter(t => t.error_message && ['failed','cancelled'].includes(t.status));
            let copyBtnsHtml = '';
            if (failedErrors.length > 1) {
                copyBtnsHtml = `<div style="text-align:right;margin-bottom:8px;display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap;">
                    <button class="copy-btn" onclick="copyAllFailedErrors()" title="复制当前页所有失败原因">📋 复制本页失败原因 (${failedErrors.length})</button>
                    <button class="copy-btn" onclick="copyAllFailedErrorsFromServer()" title="从服务器获取并复制所有失败任务的错误信息">📋 复制所有失败原因</button>
                </div>`;
            } else if (failedErrors.length === 1) {
                copyBtnsHtml = `<div style="text-align:right;margin-bottom:8px;">
                    <button class="copy-btn" onclick="copyAllFailedErrorsFromServer()" title="从服务器获取并复制所有失败任务的错误信息">📋 复制所有失败原因</button>
                </div>`;
            }
            window._currentPageFailedErrors = failedErrors.map(t => `[任务${t.id}] ${t.file_name || '未知'}: ${t.error_message}`);
            window._currentTaskMap = Object.fromEntries(tasks.map(t => [t.id, t]));

            container.innerHTML = copyBtnsHtml + tasks.map(task => {
                const hasError = task.error_message && ['failed', 'cancelled'].includes(task.status);
                const errorAttr = hasError ? ` data-error="${task.error_message.replace(/"/g, '&quot;')}"` : '';
                const authorTag = task.author_nickname ? `<span style="color:var(--text-secondary);font-size:11px;margin-left:6px;">👤 ${escapeHtml(task.author_nickname)}</span>` : '';
                const fileName = escapeHtml(task.file_name || task.work_title || '未知文件');
                const canPreview = Boolean(task.preview_url) && ['video', 'image'].includes(task.preview_media_type || '');
                const previewAction = canPreview ? ` onclick="openTaskPreview(${task.id})" title="预览媒体"` : '';
                const previewButton = canPreview
                    ? `<button class="secondary preview-btn" onclick="openTaskPreview(${task.id})">预览</button>`
                    : '';
                const thumbnailHtml = task.preview_media_type === 'image' && task.preview_url
                    ? `<img src="${escapeHtml(task.preview_url)}" alt="${fileName}" loading="lazy" referrerpolicy="no-referrer" onerror="this.replaceWith(document.createTextNode('🖼️'))">`
                    : (task.preview_media_type === 'video' ? '🎬' : '🖼️');
                const tooltipHtml = hasError ? `
                    <div class="error-tooltip">
                        <div class="error-tooltip-header">
                            <span>❌ 错误详情</span>
                            <button class="copy-btn" onclick="event.stopPropagation();copyErrorMsg(${task.id})" title="复制错误信息">📋 复制</button>
                        </div>
                        <div class="error-tooltip-body" id="errtip-${task.id}">${escapeHtml(task.error_message)}</div>
                    </div>` : '';
                const mobileErrorHtml = hasError ? `
                    <div class="error-detail-mobile" id="err-${task.id}">
                        <div class="error-detail-mobile-header">
                            <span>❌ 错误详情</span>
                            <button class="copy-btn" onclick="event.stopPropagation();copyErrorMsg(${task.id})" title="复制错误信息">📋 复制</button>
                        </div>
                        <div class="error-detail-mobile-body">${escapeHtml(task.error_message)}</div>
                    </div>` : '';
                const errorToggle = hasError ? `<button class="error-toggle-btn" onclick="event.stopPropagation();toggleErrorDetail(${task.id})" title="查看错误">详情</button>` : '';
                return `
                <div class="task-item"${errorAttr}>
                    ${tooltipHtml}
                    <div class="task-thumbnail ${canPreview ? 'previewable' : ''}"${previewAction}>${thumbnailHtml}</div>
                    <div class="task-info">
                        <div class="task-name">${fileName}${authorTag}</div>
                        <div class="progress-bar">
                            <div class="progress-fill" style="width: ${task.progress_percent}%"></div>
                        </div>
                        <div class="task-meta">
                            <span>${formatSize(task.downloaded_bytes)} / ${formatSize(task.total_bytes)}</span>
                            <span>${task.progress_percent.toFixed(1)}%</span>
                        </div>
                        ${mobileErrorHtml}
                    </div>
                    <div class="task-status-wrap" style="display:flex;align-items:center;flex-shrink:0;">
                        ${getStatusTag(task.status, task.retry_count || 0)}
                        ${errorToggle}
                    </div>
                    <div class="task-actions">
                        ${previewButton}
                        ${task.status === 'downloading' ? `
                            <button class="secondary" onclick="pauseTask(${task.id})">暂停</button>
                            <button class="secondary" onclick="forceRetryTask(${task.id})" title="任务卡住时使用">强制重试</button>
                        ` : ''}
                        ${task.status === 'paused' ? `<button onclick="resumeTask(${task.id})">恢复</button>` : ''}
                        ${['failed', 'cancelled'].includes(task.status) ? `
                            <button onclick="retryTask(${task.id})">重试</button>
                            <button class="secondary" onclick="refreshRetryTask(${task.id})" title="重新获取下载链接后重试">🔄 刷新重试</button>
                        ` : ''}
                        ${task.status === 'pending' ? `<button class="secondary" onclick="forceRetryTask(${task.id})" title="任务卡住时使用">强制重试</button>` : ''}
                        ${task.status !== 'completed' ? `<button class="secondary" onclick="deleteTask(${task.id})" title="删除此任务">删除</button>` : ''}
                    </div>
                </div>`;
            }).join('');

            // 为带错误的任务项绑定hover定位逻辑
            container.querySelectorAll('.task-item[data-error]').forEach(item => {
                item.addEventListener('mouseenter', function() {
                    showErrorTooltip(this);
                });
                item.addEventListener('mouseleave', function() {
                    scheduleErrorTooltipHide(this);
                });
            });
        }

        // 渲染任务分页控件
        function renderTasksPagination() {
            const container = document.getElementById('tasksPagination');
            if (!container) return;

            const pageButtons = [];
            const maxButtons = 5;
            let startPage = Math.max(1, tasksPage - Math.floor(maxButtons / 2));
            let endPage = Math.min(tasksTotalPages, startPage + maxButtons - 1);

            if (endPage - startPage < maxButtons - 1) {
                startPage = Math.max(1, endPage - maxButtons + 1);
            }

            for (let i = startPage; i <= endPage; i++) {
                pageButtons.push(`
                    <button class="pagination-btn ${i === tasksPage ? 'active' : ''}" 
                            onclick="goToTasksPage(${i})">${i}</button>
                `);
            }

            container.innerHTML = `
                <div class="pagination-info">
                    共 ${tasksTotal} 条，第 ${tasksPage}/${tasksTotalPages} 页
                </div>
                <div class="pagination-controls">
                    <select class="page-size-select" onchange="changeTasksPageSize(this.value)">
                        <option value="10" ${tasksPageSize === 10 ? 'selected' : ''}>10条/页</option>
                        <option value="20" ${tasksPageSize === 20 ? 'selected' : ''}>20条/页</option>
                        <option value="50" ${tasksPageSize === 50 ? 'selected' : ''}>50条/页</option>
                    </select>
                    <button class="pagination-btn" onclick="goToTasksPage(1)" ${tasksPage <= 1 ? 'disabled' : ''}>首页</button>
                    <button class="pagination-btn" onclick="goToTasksPage(${tasksPage - 1})" ${tasksPage <= 1 ? 'disabled' : ''}>上一页</button>
                    ${pageButtons.join('')}
                    <button class="pagination-btn" onclick="goToTasksPage(${tasksPage + 1})" ${tasksPage >= tasksTotalPages ? 'disabled' : ''}>下一页</button>
                    <button class="pagination-btn" onclick="goToTasksPage(${tasksTotalPages})" ${tasksPage >= tasksTotalPages ? 'disabled' : ''}>末页</button>
                </div>
            `;
        }

        // 任务分页导航
        function goToTasksPage(page) {
            if (page < 1 || page > tasksTotalPages) return;
            tasksPage = page;
            fetchTasks();
        }

        // 改变任务每页数量
        function changeTasksPageSize(size) {
            tasksPageSize = parseInt(size);
            tasksPage = 1;
            fetchTasks();
        }

        function openTaskPreview(taskId) {
            const task = window._currentTaskMap?.[taskId];
            if (!task) {
                showToast('未找到任务信息', 'error');
                return;
            }

            if (!task.preview_url || !task.preview_media_type) {
                showToast('当前任务暂无可用预览', 'error');
                return;
            }

            const metaParts = [];
            if (task.author_nickname) metaParts.push(`作者：${task.author_nickname}`);
            if (task.work_type === 'images') {
                const totalImages = Number(task.image_count || 0);
                metaParts.push(totalImages > 1 ? `图集任务 ${Number(task.file_index || 0) + 1}/${totalImages}` : '图片任务');
            } else if (task.work_type === 'video') {
                metaParts.push('视频任务');
            }

            openMediaPreview({
                type: task.preview_media_type,
                title: task.file_name || task.work_title || '媒体预览',
                meta: metaParts.join(' · '),
                url: task.preview_url,
                images: task.preview_media_type === 'image' ? [task.preview_url] : []
            });
        }

        function openVideoPreview(taskId) {
            openTaskPreview(taskId);
        }

        function openMediaPreview(config) {
            const modal = document.getElementById('mediaPreviewModal');
            const video = document.getElementById('mediaPreviewPlayer');
            const image = document.getElementById('imagePreviewPlayer');
            const title = document.getElementById('mediaPreviewTitle');
            const meta = document.getElementById('mediaPreviewMeta');
            const galleryControls = document.getElementById('previewGalleryControls');
            if (!modal || !video || !image || !title || !meta || !galleryControls) return;

            const previewType = config?.type === 'video' ? 'video' : 'image';
            const imageItems = Array.isArray(config?.images) ? config.images.filter(Boolean) : [];
            const primaryUrl = config?.url || imageItems[0];
            if (!primaryUrl) {
                showToast('暂无可用预览', 'error');
                return;
            }

            currentMediaPreviewType = previewType;
            currentPreviewImages = previewType === 'image'
                ? (imageItems.length ? imageItems : [primaryUrl])
                : [];
            currentPreviewImageIndex = Math.max(0, Math.min(config?.startIndex || 0, Math.max(currentPreviewImages.length - 1, 0)));

            title.textContent = config?.title || '媒体预览';
            meta.textContent = config?.meta || '';

            video.pause();
            video.onloadedmetadata = null;
            video.removeAttribute('src');
            video.load();
            image.onload = null;
            image.onerror = null;
            image.removeAttribute('src');

            if (previewType === 'video') {
                galleryControls.classList.remove('show');
                image.style.display = 'none';
                video.style.display = 'block';
                video.onloadedmetadata = () => fitMediaPreview();
                video.src = primaryUrl;
            } else {
                video.style.display = 'none';
                image.style.display = 'block';
                renderCurrentPreviewImage();
            }

            modal.classList.add('show');
            modal.setAttribute('aria-hidden', 'false');
            syncBodyScrollLock();
        }

        function renderCurrentPreviewImage() {
            const image = document.getElementById('imagePreviewPlayer');
            const galleryControls = document.getElementById('previewGalleryControls');
            const counter = document.getElementById('previewCounter');
            const prevBtn = document.getElementById('previewPrevBtn');
            const nextBtn = document.getElementById('previewNextBtn');
            if (!image || !galleryControls || !counter || !prevBtn || !nextBtn || !currentPreviewImages.length) {
                return;
            }

            const imageUrl = currentPreviewImages[currentPreviewImageIndex];
            const isGallery = currentPreviewImages.length > 1;
            galleryControls.classList.toggle('show', isGallery);
            counter.textContent = isGallery ? `${currentPreviewImageIndex + 1} / ${currentPreviewImages.length}` : '';
            prevBtn.disabled = currentPreviewImageIndex <= 0;
            nextBtn.disabled = currentPreviewImageIndex >= currentPreviewImages.length - 1;
            image.onload = () => fitMediaPreview();
            image.src = imageUrl;
        }

        function changePreviewImage(step) {
            if (currentMediaPreviewType !== 'image' || currentPreviewImages.length <= 1) {
                return;
            }

            currentPreviewImageIndex = Math.max(
                0,
                Math.min(currentPreviewImages.length - 1, currentPreviewImageIndex + step)
            );
            renderCurrentPreviewImage();
        }

        function fitMediaPreview() {
            const modal = document.getElementById('mediaPreviewModal');
            const dialog = modal?.querySelector('.preview-dialog');
            const video = document.getElementById('mediaPreviewPlayer');
            const image = document.getElementById('imagePreviewPlayer');
            const header = modal?.querySelector('.preview-header');
            if (!modal?.classList.contains('show') || !dialog) {
                return;
            }

            const mediaWidth = currentMediaPreviewType === 'video' ? video?.videoWidth : image?.naturalWidth;
            const mediaHeight = currentMediaPreviewType === 'video' ? video?.videoHeight : image?.naturalHeight;
            if (!mediaWidth || !mediaHeight) {
                return;
            }

            const ratio = mediaWidth / mediaHeight;
            const mobile = window.innerWidth <= 768;
            const modalPaddingX = mobile ? 20 : 48;
            const bodyPadding = mobile ? 24 : 36;
            const headerHeight = header?.offsetHeight || 72;
            const bottomReserve = mobile ? 120 : 48;
            const maxMediaWidth = Math.max(240, window.innerWidth - modalPaddingX - bodyPadding);
            const maxMediaHeight = Math.max(220, window.innerHeight - headerHeight - bodyPadding - bottomReserve);

            let targetWidth = Math.min(maxMediaWidth, maxMediaHeight * ratio);
            let targetHeight = targetWidth / ratio;
            if (targetHeight > maxMediaHeight) {
                targetHeight = maxMediaHeight;
                targetWidth = targetHeight * ratio;
            }

            dialog.classList.toggle('portrait', ratio < 1);
            dialog.style.setProperty('--preview-dialog-width', `${Math.ceil(targetWidth + bodyPadding)}px`);

            if (currentMediaPreviewType === 'video') {
                dialog.style.setProperty('--preview-video-width', `${Math.ceil(targetWidth)}px`);
                dialog.style.setProperty('--preview-video-height', `${Math.ceil(targetHeight)}px`);
            } else {
                dialog.style.setProperty('--preview-image-width', `${Math.ceil(targetWidth)}px`);
                dialog.style.setProperty('--preview-image-height', `${Math.ceil(targetHeight)}px`);
            }
        }

        function closeMediaPreview() {
            const modal = document.getElementById('mediaPreviewModal');
            const video = document.getElementById('mediaPreviewPlayer');
            const image = document.getElementById('imagePreviewPlayer');
            if (video) {
                video.pause();
                video.onloadedmetadata = null;
                video.removeAttribute('src');
                video.load();
                video.style.display = 'none';
            }
            if (image) {
                image.onload = null;
                image.onerror = null;
                image.removeAttribute('src');
                image.style.display = 'none';
            }
            currentMediaPreviewType = null;
            currentPreviewImages = [];
            currentPreviewImageIndex = 0;
            if (modal) {
                modal.classList.remove('show');
                modal.setAttribute('aria-hidden', 'true');
            }
            syncBodyScrollLock();
        }

        function closeVideoPreview() {
            closeMediaPreview();
        }

        function onPreviewBackdropClick(event) {
            if (event.target?.id === 'mediaPreviewModal') {
                closeMediaPreview();
            }
        }

        // 开始下载
        async function startDownload() {
            const url = document.getElementById('shareUrl').value.trim();
            if (!url) {
                showToast('请输入分享链接', 'error');
                return;
            }

            try {
                const res = await apiFetch(`${API_BASE}/tasks/download`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ share_url: url, download_all: true })
                });
                const data = await res.json();

                if (res.ok) {
                    const authorName = data.author_nickname || '作者';
                    if (data.url_type === 'work') {
                        showToast(`已创建单作品下载任务 (${data.created_tasks} 个文件)`, 'success');
                    } else if (data.author_already_exists) {
                        showToast(`作者 ${authorName} 已存在，正在下载新作品`, 'success');
                        if (typeof data.author_position === 'number') {
                            navigateToAuthorByPosition(data.author_id, data.author_position);
                        } else {
                            navigateToAuthor(data.author_id);
                        }
                    } else {
                        showToast(`已开始下载 ${authorName} 的作品`, 'success');
                    }
                    document.getElementById('shareUrl').value = '';
                    refreshTasks();
                    fetchAuthors();
                } else {
                    showToast(data.detail || '下载失败', 'error');
                }
            } catch (e) {
                showToast('请求失败', 'error');
            }
        }

        // 暂停任务
        async function pauseTask(taskId) {
            try {
                await apiFetch(`${API_BASE}/tasks/${taskId}/pause`, { method: 'POST' });
                showToast('任务已暂停');
                refreshTasks();
            } catch (e) {
                showToast('操作失败', 'error');
            }
        }

        // 恢复任务
        async function resumeTask(taskId) {
            try {
                await apiFetch(`${API_BASE}/tasks/${taskId}/resume`, { method: 'POST' });
                showToast('任务已恢复');
                refreshTasks();
            } catch (e) {
                showToast('操作失败', 'error');
            }
        }

        // 重试任务
        async function retryTask(taskId) {
            try {
                await apiFetch(`${API_BASE}/tasks/${taskId}/retry`, { method: 'POST' });
                showToast('任务已重新提交');
                refreshTasks();
            } catch (e) {
                showToast('操作失败', 'error');
            }
        }

        // 强制重试任务（用于卡住的任务）
        async function forceRetryTask(taskId) {
            if (!confirm('确定要强制重试此任务吗？这将重置任务状态和进度。')) {
                return;
            }
            try {
                const res = await apiFetch(`${API_BASE}/tasks/${taskId}/force-retry`, { method: 'POST' });
                const data = await res.json();
                if (res.ok) {
                    showToast('任务已强制重新提交');
                    refreshTasks();
                } else {
                    showToast(data.detail || '操作失败', 'error');
                }
            } catch (e) {
                showToast('操作失败', 'error');
            }
        }

        // 删除任务
        async function deleteTask(taskId) {
            if (!confirm('确定要删除此任务吗？')) {
                return;
            }
            try {
                const res = await apiFetch(`${API_BASE}/tasks/${taskId}`, { method: 'DELETE' });
                const data = await res.json();
                if (res.ok) {
                    showToast('任务已删除');
                    refreshTasks();
                } else {
                    showToast(data.detail || '删除失败', 'error');
                }
            } catch (e) {
                showToast('删除失败', 'error');
            }
        }

        // 重新分发所有待处理任务
        async function redispatchPending() {
            if (!confirm('确定要重新分发所有待处理任务吗？这将把等待中的任务重新发送给Worker执行。')) {
                return;
            }
            try {
                const res = await apiFetch(`${API_BASE}/tasks/redispatch-pending`, { method: 'POST' });
                const data = await res.json();
                if (res.ok) {
                    const count = data.data?.dispatched || 0;
                    if (count > 0) {
                        showToast(`已重新分发 ${count} 个待处理任务`);
                    } else {
                        showToast('没有待处理的任务需要分发');
                    }
                    refreshTasks();
                } else {
                    showToast(data.detail || '操作失败', 'error');
                }
            } catch (e) {
                showToast('操作失败', 'error');
            }
        }

        // 批量重试所有失败的任务
        async function retryAllFailed() {
            if (!confirm('确定要重试所有失败的任务吗？')) {
                return;
            }
            try {
                const res = await apiFetch(`${API_BASE}/tasks/retry-all-failed`, { method: 'POST' });
                const data = await res.json();
                if (res.ok) {
                    const count = data.data?.count || 0;
                    if (count > 0) {
                        showToast(`已重新提交 ${count} 个失败任务`);
                    } else {
                        showToast('没有失败的任务需要重试');
                    }
                    refreshTasks();
                } else {
                    showToast(data.detail || '操作失败', 'error');
                }
            } catch (e) {
                showToast('操作失败', 'error');
            }
        }

        // 获取作者列表
        async function fetchAuthors() {
            try {
                const params = new URLSearchParams({
                    page: authorsPage,
                    page_size: authorsPageSize,
                });
                if (authorsSubscribeFilter === 'true' || authorsSubscribeFilter === 'false') {
                    params.set('is_subscribed', authorsSubscribeFilter);
                }
                if (authorsStatusFilter && authorsStatusFilter !== 'all') {
                    params.set('account_status', authorsStatusFilter);
                }
                const data = await apiRequest(`${API_BASE}/authors/?${params.toString()}`, {}, '获取作者失败');

                // 处理分页响应
                authorsTotal = data.total;
                authorsTotalPages = data.pages;

                renderAuthors(data.items);
                renderAuthorsPagination();
            } catch (e) {
                console.error('获取作者失败', e);
                showToast(e.message || '获取作者失败', 'error');
            }
        }

        function closeFilterSelects(exceptEl = null) {
            document.querySelectorAll('.filter-select.open').forEach(el => {
                if (exceptEl && el === exceptEl) return;
                el.classList.remove('open');
                const trigger = el.querySelector('[data-filter-trigger]');
                if (trigger) trigger.setAttribute('aria-expanded', 'false');
            });
        }

        function triggerFilterSelectOnChange(root) {
            const handlerName = (root?.dataset?.filterOnchange || '').trim();
            if (!handlerName) return;
            const fn = window[handlerName];
            if (typeof fn === 'function') fn();
        }

        function setFilterSelectValue(inputId, value, emitChange = false) {
            const hidden = document.getElementById(inputId);
            if (!hidden) return;
            if (value !== undefined && value !== null) {
                hidden.value = String(value);
            }
            const state = customSelectRegistry.get(inputId);
            if (state?.syncFromInput) {
                state.syncFromInput();
                if (emitChange) triggerFilterSelectOnChange(state.root);
            }
        }

        function initFilterSelects() {
            document.querySelectorAll('[data-filter-select]').forEach(root => {
                const trigger = root.querySelector('[data-filter-trigger]');
                const labelEl = root.querySelector('[data-filter-label]');
                const menu = root.querySelector('[data-filter-menu]');
                const hidden = root.querySelector('input[type="hidden"]');
                const options = Array.from(root.querySelectorAll('[data-value]'));
                if (!trigger || !labelEl || !menu || !hidden || !options.length) return;

                const applyValue = (value) => {
                    hidden.value = value ?? '';
                    let matched = options.find(btn => (btn.dataset.value ?? '') === value);
                    if (!matched) matched = options[0];
                    options.forEach(btn => btn.classList.toggle('active', btn === matched));
                    labelEl.textContent = matched?.dataset.label || matched?.textContent?.trim() || '';
                };

                const syncFromInput = () => {
                    const fallback = options.find(btn => btn.classList.contains('active'))?.dataset.value
                        || options[0].dataset.value
                        || '';
                    const currentValue = hidden.value || fallback;
                    applyValue(currentValue);
                };

                if (hidden.id) {
                    customSelectRegistry.set(hidden.id, { root, syncFromInput });
                }

                if (root.dataset.bound === '1') {
                    syncFromInput();
                    return;
                }

                trigger.addEventListener('click', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    const shouldOpen = !root.classList.contains('open');
                    closeFilterSelects(shouldOpen ? root : null);
                    root.classList.toggle('open', shouldOpen);
                    trigger.setAttribute('aria-expanded', shouldOpen ? 'true' : 'false');
                });

                options.forEach(btn => {
                    btn.addEventListener('click', (e) => {
                        e.preventDefault();
                        const next = btn.dataset.value ?? '';
                        const changed = hidden.value !== next;
                        applyValue(next);
                        root.classList.remove('open');
                        trigger.setAttribute('aria-expanded', 'false');
                        if (changed) triggerFilterSelectOnChange(root);
                    });
                });

                root.dataset.bound = '1';
                syncFromInput();
            });
        }

        // 作者筛选条件变更：读取下拉框、重置到第一页并刷新
        function onAuthorFilterChange() {
            const subEl = document.getElementById('authorSubscribeFilter');
            const statusEl = document.getElementById('authorStatusFilter');
            authorsSubscribeFilter = subEl ? subEl.value : '';
            authorsStatusFilter = statusEl ? statusEl.value : 'all';
            authorsPage = 1;
            fetchAuthors();
        }

        // 渲染作者列表
        function renderAuthors(authors) {
            const container = document.getElementById('authorList');

            if (!authors.length) {
                container.innerHTML = `
                    <div class="empty-state">
                        <div class="empty-state-icon">👥</div>
                        <p>暂无作者</p>
                    </div>
                `;
                return;
            }

            window._currentAuthorMap = Object.fromEntries(authors.map(author => [author.id, author]));

            container.innerHTML = authors.map(author => {
                const authorStatus = parseAuthorStatusMarker(author.last_error);
                const errorHtml = authorStatus.errorMessage
                    ? `<div class="author-error" style="color:var(--error);font-size:11px;width:100%;padding:2px 0 0 52px;word-break:break-all;">⚠ ${escapeHtml(authorStatus.errorMessage)}</div>`
                    : '';
                const avatarHtml = author.avatar_url
                    ? `<img src="${escapeHtml(author.avatar_url)}" alt="${escapeHtml(author.nickname || '作者')}头像" loading="lazy" referrerpolicy="no-referrer" onerror="this.replaceWith(document.createTextNode('👤'))">`
                    : '👤';
                const profileUrl = getAuthorProfileUrl(author);
                const statusBadgeHtml = authorStatus.label
                    ? `<span class="author-status-badge author-status-${authorStatus.code}" title="${escapeHtml(authorStatus.detail || authorStatus.label)}">${escapeHtml(authorStatus.label)}</span>`
                    : '';
                const authorNameHtml = profileUrl
                    ? `<a class="author-link" href="${escapeHtml(profileUrl)}" target="_blank" rel="noopener noreferrer nofollow" title="打开作者主页">${escapeHtml(author.nickname || '未知作者')}</a>`
                    : escapeHtml(author.nickname || '未知作者');
                return `
                <div class="author-item" data-author-id="${author.id}" style="flex-wrap:wrap;">
                    <div class="author-avatar">${avatarHtml}</div>
                    <div class="author-name"><div class="author-name-row">${authorNameHtml}${statusBadgeHtml}</div></div>
                    <span style="color: var(--text-secondary); font-size: 12px;">
                        ${author.total_works || 0} 作品 / ${author.downloaded_works || 0} 已下载
                    </span>
                    <div class="author-actions">
                        <button class="subscribe-toggle ${author.is_subscribed ? 'subscribed' : 'secondary'}" 
                                onclick="toggleSubscribe(${author.id}, ${author.is_subscribed})">
                            ${author.is_subscribed ? '已订阅' : '订阅'}
                        </button>
                        <button class="secondary" onclick="openAuthorWorksPreview(${author.id})">作品管理</button>
                        <button class="secondary" onclick="filterTasksByAuthor(${author.id})">关联任务</button>
                        <button class="secondary sync-avatar-btn" onclick="syncAuthorAvatar(${author.id})">同步头像</button>
                        <button class="secondary dl-btn" onclick="downloadAuthor(${author.id})" style="padding: 6px 12px; font-size: 12px;">
                            下载
                        </button>
                        <button class="secondary author-delete-btn" onclick="deleteAuthor(${author.id})">删除</button>
                    </div>
                    ${errorHtml}
                </div>`;
            }).join('');
        }

        // 渲染作者分页控件
        function renderAuthorsPagination() {
            const container = document.getElementById('authorsPagination');
            if (!container) return;

            const pageButtons = [];
            const maxButtons = 5;
            let startPage = Math.max(1, authorsPage - Math.floor(maxButtons / 2));
            let endPage = Math.min(authorsTotalPages, startPage + maxButtons - 1);

            if (endPage - startPage < maxButtons - 1) {
                startPage = Math.max(1, endPage - maxButtons + 1);
            }

            for (let i = startPage; i <= endPage; i++) {
                pageButtons.push(`
                    <button class="pagination-btn ${i === authorsPage ? 'active' : ''}" 
                            onclick="goToAuthorsPage(${i})">${i}</button>
                `);
            }

            container.innerHTML = `
                <div class="pagination-info">
                    共 ${authorsTotal} 位作者，第 ${authorsPage}/${authorsTotalPages} 页
                </div>
                <div class="pagination-controls">
                    <select class="page-size-select" onchange="changeAuthorsPageSize(this.value)">
                        <option value="10" ${authorsPageSize === 10 ? 'selected' : ''}>10条/页</option>
                        <option value="20" ${authorsPageSize === 20 ? 'selected' : ''}>20条/页</option>
                        <option value="50" ${authorsPageSize === 50 ? 'selected' : ''}>50条/页</option>
                    </select>
                    <button class="pagination-btn" onclick="goToAuthorsPage(1)" ${authorsPage <= 1 ? 'disabled' : ''}>首页</button>
                    <button class="pagination-btn" onclick="goToAuthorsPage(${authorsPage - 1})" ${authorsPage <= 1 ? 'disabled' : ''}>上一页</button>
                    ${pageButtons.join('')}
                    <button class="pagination-btn" onclick="goToAuthorsPage(${authorsPage + 1})" ${authorsPage >= authorsTotalPages ? 'disabled' : ''}>下一页</button>
                    <button class="pagination-btn" onclick="goToAuthorsPage(${authorsTotalPages})" ${authorsPage >= authorsTotalPages ? 'disabled' : ''}>末页</button>
                </div>
            `;
        }

        // 作者分页导航
        function goToAuthorsPage(page) {
            if (page < 1 || page > authorsTotalPages) return;
            authorsPage = page;
            fetchAuthors();
        }

        // 改变作者每页数量
        function changeAuthorsPageSize(size) {
            authorsPageSize = parseInt(size);
            authorsPage = 1;
            fetchAuthors();
        }

        // 添加作者
        async function addAuthor() {
            const url = document.getElementById('authorUrl').value.trim();
            if (!url) {
                showToast('请输入分享链接', 'error');
                return;
            }

            try {
                const data = await apiRequest(`${API_BASE}/authors/`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ share_url: url })
                }, '添加作者失败');

                document.getElementById('authorUrl').value = '';
                if (data.already_exists) {
                    showToast(`作者 ${data.nickname || '未知作者'} 已存在，正在跳转...`);
                    if (typeof data.position === 'number') {
                        await navigateToAuthorByPosition(data.id, data.position);
                    } else {
                        await navigateToAuthor(data.id);
                    }
                } else {
                    showToast(`已添加作者 ${data.nickname || '未知作者'}`);
                    fetchAuthors();
                }
            } catch (e) {
                showToast(e.message || '请求失败', 'error');
            }
        }

        // 处理作者输入框输入事件（搜索或添加）
        function handleAuthorInput(value) {
            const searchResults = document.getElementById('authorSearchResults');
            if (!value.trim()) {
                searchResults.style.display = 'none';
                return;
            }

            // 如果是链接，不进行搜索
            if (value.includes('douyin.com') || value.includes('v.douyin')) {
                searchResults.style.display = 'none';
                return;
            }

            // 防抖搜索
            clearTimeout(searchTimeout);
            searchTimeout = setTimeout(() => searchAuthors(value), 300);
        }

        // 搜索作者
        async function searchAuthors(query) {
            const searchResults = document.getElementById('authorSearchResults');
            try {
                const data = await apiRequest(`${API_BASE}/authors/search?q=${encodeURIComponent(query)}`, {}, '搜索作者失败');

                if (data.items && data.items.length > 0) {
                    searchResults.innerHTML = data.items.map(author => {
                        const avatarHtml = author.avatar_url
                            ? `<img src="${escapeHtml(author.avatar_url)}" alt="" loading="lazy" referrerpolicy="no-referrer" style="width:100%;height:100%;object-fit:cover;border-radius:50%;" onerror="this.replaceWith(document.createTextNode('👤'))">`
                            : '👤';
                        return `
                        <div class="search-result-item" data-nickname="${escapeHtml(author.nickname || '未知作者')}" onclick="selectSearchResult(${author.id}, ${author.position}, this.dataset.nickname)">
                            <div class="result-avatar">${avatarHtml}</div>
                            <div class="result-name">${escapeHtml(author.nickname || '未知作者')}</div>
                        </div>`;
                    }).join('');
                    searchResults.style.display = 'block';
                } else {
                    searchResults.innerHTML = '<div class="search-result-item"><div class="result-name" style="color: var(--text-secondary);">未找到匹配的作者</div></div>';
                    searchResults.style.display = 'block';
                }
            } catch (e) {
                console.error('搜索作者失败', e);
                searchResults.style.display = 'none';
            }
        }

        // 选择搜索结果
        async function selectSearchResult(authorId, position, nickname) {
            document.getElementById('authorSearchResults').style.display = 'none';
            document.getElementById('authorUrl').value = '';
            showToast(`正在跳转到 ${nickname}...`);
            const numericPosition = Number(position);
            if (Number.isFinite(numericPosition)) {
                await navigateToAuthorByPosition(authorId, numericPosition);
            } else {
                await navigateToAuthor(authorId);
            }
        }

        // 处理作者输入框按钮点击
        function handleAuthorAction() {
            const value = document.getElementById('authorUrl').value.trim();
            if (!value) {
                showToast('请输入链接或搜索关键词', 'error');
                return;
            }

            // 判断是链接还是搜索关键词
            if (value.includes('douyin.com') || value.includes('v.douyin')) {
                addAuthor();
            } else {
                searchAuthors(value);
            }
        }

        // 导航到作者（根据ID）
        async function navigateToAuthor(authorId) {
            try {
                const authorDetail = await apiRequest(`${API_BASE}/authors/${authorId}`, {}, '获取作者信息失败');
                const data = await apiRequest(`${API_BASE}/authors/search?q=${encodeURIComponent(authorDetail.sec_uid || String(authorId))}`, {}, '获取作者位置失败');
                const matchedAuthor = data.items?.find(a => a.id === authorId);
                if (matchedAuthor && typeof matchedAuthor.position === 'number') {
                    await navigateToAuthorByPosition(authorId, matchedAuthor.position);
                } else {
                    switchSubTab('authors');
                    await fetchAuthorsAndHighlight(authorId);
                }
            } catch (e) {
                await fetchAuthorsAndHighlight(authorId);
            }
        }

        // 导航到作者（根据位置）
        async function navigateToAuthorByPosition(authorId, position) {
            switchSubTab('authors');

            // 计算页码
            const targetPage = Math.floor(position / authorsPageSize) + 1;
            authorsPage = targetPage;
            await fetchAuthorsAndHighlight(authorId);
        }

        // 获取作者并高亮
        async function fetchAuthorsAndHighlight(authorId) {
            await fetchAuthors();
            highlightAuthor(authorId);
        }

        // 高亮作者
        function highlightAuthor(authorId) {
            const authorList = document.getElementById('authorList');
            const items = authorList.querySelectorAll('.author-item');
            items.forEach(item => {
                item.classList.remove('highlight');
            });

            // 找到目标作者并高亮
            const targetItem = authorList.querySelector(`[data-author-id="${authorId}"]`);
            if (targetItem) {
                targetItem.classList.add('highlight');
                targetItem.scrollIntoView({ behavior: 'smooth', block: 'center' });
            }
        }

        // 切换平台
        function switchPlatform(platform) {
            currentPlatform = platform;
            // 桌面平台标签
            document.querySelectorAll('.platform-tab').forEach(t => t.classList.toggle('active', t.dataset.platform === platform));
            // 移动端底部标签
            document.querySelectorAll('.mobile-tab').forEach(t => t.classList.toggle('active', t.dataset.platform === platform));
            // 显示/隐藏平台容器
            document.querySelectorAll('.platform-container').forEach(c => {
                if (c.dataset.platform === platform) {
                    c.style.display = '';
                    c.classList.add('platform-active');
                } else {
                    c.style.display = 'none';
                    c.classList.remove('platform-active');
                }
            });

            // 恢复子标签（桌面侧边栏与移动端均生效）
            const sub = currentSubTab[platform] || 'tasks';
            renderSubTabs(sub);

            // 加载该平台所有数据
            loadAllPlatformData(platform);
        }

        // 切换子标签（仅移动端生效）
        function switchSubTab(sub) {
            currentSubTab[currentPlatform] = sub;
            renderSubTabs(sub);
            loadPlatformData(currentPlatform, sub);
        }

        function renderSubTabs(activeSub) {
            document.querySelectorAll('.sub-tab').forEach(t => t.classList.toggle('active', t.dataset.sub === activeSub));
            const container = document.querySelector(`.platform-container[data-platform="${currentPlatform}"]`);
            if (!container) return;
            // 所有视口都切换 panel-visible（桌面侧边栏 + 移动端）
            container.querySelectorAll('.card[data-sub]').forEach(card => {
                card.classList.toggle('panel-visible', card.dataset.sub === activeSub);
            });
        }

        // 加载某平台全部数据（桌面端切换平台时调用）
        function loadAllPlatformData(platform) {
            if (platform === 'douyin') {
                loadPlatformData(platform, 'tasks');
                loadPlatformData(platform, 'authors');
                loadPlatformData(platform, 'settings');
            } else {
                loadPlatformData(platform, 'tasks');
                loadPlatformData(platform, 'authors');
                loadPlatformData(platform, 'settings');
            }
        }

        function loadPlatformData(platform, sub) {
            const key = `${platform}_${sub}`;
            if (loadedPanels[key]) return;
            loadedPanels[key] = true;
            if (platform === 'douyin') {
                if (sub === 'tasks') fetchTasks();
                else if (sub === 'authors') fetchAuthors();
                else if (sub === 'settings') {
                    checkCookie();
                    loadRuntimeConfig();
                    loadProcessStatus();
                    loadDbConfig();
                }
            } else {
                if (sub === 'tasks') fetchXTasks();
                else if (sub === 'authors') fetchXAuthors();
                else if (sub === 'settings') checkXCookie();
            }
        }

        // 检测是否为移动端
        function isMobile() {
            return window.innerWidth <= 768;
        }

        // 点击页面其他地方关闭搜索结果
        document.addEventListener('click', (e) => {
            const searchResults = document.getElementById('authorSearchResults');
            const inputGroup = e.target.closest('.search-input-group');
            if (!inputGroup && searchResults) {
                searchResults.style.display = 'none';
            }
            if (!e.target.closest('.filter-select')) {
                closeFilterSelects();
            }
        });

        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                if (document.getElementById('mediaPreviewModal')?.classList.contains('show')) {
                    closeMediaPreview();
                } else if (document.getElementById('authorWorksModal')?.classList.contains('show')) {
                    closeAuthorWorksPreview();
                }
                closeFilterSelects();
            }
        });

        window.addEventListener('resize', () => {
            fitMediaPreview();
        });

        // 切换订阅
        async function toggleSubscribe(authorId, isSubscribed) {
            try {
                const action = isSubscribed ? 'unsubscribe' : 'subscribe';
                await apiRequest(`${API_BASE}/authors/${authorId}/${action}`, { method: 'POST' }, '更新订阅状态失败');
                showToast(isSubscribed ? '已取消订阅' : '已订阅');
                fetchAuthors();
                fetchStatus();
            } catch (e) {
                showToast(e.message || '操作失败', 'error');
            }
        }

        async function syncAuthorAvatar(authorId) {
            const btn = document.querySelector(`.author-item[data-author-id="${authorId}"] .sync-avatar-btn`);
            const oldText = btn?.textContent || '同步头像';
            if (btn) {
                btn.textContent = '同步中...';
                btn.disabled = true;
            }

            try {
                const data = await apiRequest(`${API_BASE}/authors/${authorId}/sync-avatar`, { method: 'POST' }, '同步作者状态失败');
                const statusInfo = parseAuthorStatusMarker(data.last_error);
                if (statusInfo.label) {
                    showToast(`已更新作者状态：${statusInfo.label}`);
                } else {
                    showToast(`已同步 ${data.nickname || '作者'} 头像`);
                }
                fetchAuthors();
            } catch (e) {
                showToast(e.message || '同步失败', 'error');
            } finally {
                if (btn) {
                    btn.textContent = oldText;
                    btn.disabled = false;
                }
            }
        }

        // 下载作者作品
        async function downloadAuthor(authorId) {
            const btn = document.querySelector(`.author-item[data-author-id="${authorId}"] .dl-btn`);
            if (btn) { btn.textContent = '提交中...'; btn.disabled = true; }
            try {
                const data = await apiRequest(`${API_BASE}/authors/${authorId}/download`, { method: 'POST' }, '提交下载任务失败');
                if (btn) { btn.textContent = '已提交 ✓'; setTimeout(() => { btn.textContent = '下载'; btn.disabled = false; }, 2000); }
                showToast(data.message || '下载任务已提交');
                refreshTasks();
                fetchAuthors();
                fetchStatus();
            } catch (e) {
                if (btn) { btn.textContent = '下载'; btn.disabled = false; }
                showToast(e.message || '操作失败', 'error');
            }
        }

        async function deleteAuthor(authorId) {
            const author = window._currentAuthorMap?.[authorId];
            const authorName = author?.nickname || `作者 ${authorId}`;
            const confirmed = confirm(
                `确定删除 ${authorName} 吗？\n` +
                '这会删除该作者、已下载的视频/图片、本地文件、相关任务记录和下载历史，恢复到添加该作者之前的状态。\n' +
                '此操作不会加入排除列表，后续仍可重新添加同一作者。'
            );
            if (!confirmed) {
                return;
            }

            const btn = document.querySelector(`.author-item[data-author-id="${authorId}"] .author-delete-btn`);
            const oldText = btn?.textContent || '删除';
            const previousPage = authorsPage;
            const nextTotal = Math.max(0, (authorsTotal || 0) - 1);
            const nextPages = Math.max(1, Math.ceil(nextTotal / authorsPageSize));
            if (authorsPage > nextPages) {
                authorsPage = nextPages;
            }

            if (btn) {
                btn.textContent = '删除中...';
                btn.disabled = true;
            }

            try {
                const data = await apiRequest(`${API_BASE}/authors/${authorId}`, { method: 'DELETE' }, '删除作者失败');
                if (currentAuthorPreview?.authorId === authorId) {
                    closeAuthorWorksPreview();
                }
                if (currentTaskAuthorFilter === authorId) {
                    currentTaskAuthorFilter = null;
                    currentTaskAuthorName = '';
                    tasksPage = 1;
                    updateTaskAuthorFilterBanner();
                }

                const refreshResults = await Promise.allSettled([fetchAuthors(), fetchTasks(), fetchStatus()]);
                refreshResults
                    .filter(result => result.status === 'rejected')
                    .forEach(result => console.warn('删除作者后刷新失败:', result.reason));
                showToast(data.message || `${authorName} 已删除`);
            } catch (e) {
                authorsPage = previousPage;
                showToast(e.message || '删除作者失败', 'error');
            } finally {
                if (btn && document.body.contains(btn)) {
                    btn.textContent = oldText;
                    btn.disabled = false;
                }
            }
        }

        async function fetchAllAuthorWorks(authorId) {
            const pageSize = 100;
            let page = 1;
            const allWorks = [];

            while (true) {
                const works = await apiRequest(`${API_BASE}/authors/${authorId}/works?page=${page}&page_size=${pageSize}`, {}, '获取作者作品失败');

                const pageItems = Array.isArray(works) ? works : [];
                allWorks.push(...pageItems);

                if (pageItems.length < pageSize) {
                    break;
                }
                page += 1;
            }

            return allWorks;
        }

        async function openAuthorWorksPreview(authorId) {
            const author = window._currentAuthorMap?.[authorId];
            const modal = document.getElementById('authorWorksModal');
            const title = document.getElementById('authorWorksTitle');
            const meta = document.getElementById('authorWorksMeta');
            const summary = document.getElementById('authorWorksSummary');
            const grid = document.getElementById('authorWorksGrid');
            const tasksBtn = document.getElementById('authorWorksTasksBtn');
            if (!modal || !title || !meta || !summary || !grid || !tasksBtn) {
                return;
            }

            currentAuthorPreview = {
                authorId,
                authorName: author?.nickname || `作者 ${authorId}`,
                works: []
            };
            selectedWorkIds = new Set();
            title.textContent = `${currentAuthorPreview.authorName} 的作品管理`;
            meta.textContent = '可预览、删除（含已下载文件）、重新下载与重试，支持多选';
            summary.textContent = '正在加载作品...';
            grid.innerHTML = `
                <div class="empty-state" style="grid-column:1 / -1;">
                    <div class="empty-state-icon">🪄</div>
                    <p>正在加载作者作品...</p>
                </div>
            `;
            tasksBtn.style.display = '';

            modal.classList.add('show');
            modal.setAttribute('aria-hidden', 'false');
            syncBodyScrollLock();

            try {
                const works = await fetchAllAuthorWorks(authorId);
                currentAuthorPreview.works = Array.isArray(works) ? works : [];
                window._currentAuthorWorksMap = Object.fromEntries(currentAuthorPreview.works.map(work => [work.id, work]));
                renderAuthorWorksPreview(currentAuthorPreview.works);
            } catch (e) {
                summary.textContent = e.message || '加载作者作品失败';
                grid.innerHTML = `
                    <div class="empty-state" style="grid-column:1 / -1;">
                        <div class="empty-state-icon">⚠️</div>
                        <p>${escapeHtml(e.message || '加载作者作品失败')}</p>
                    </div>
                `;
            }
        }

        function renderAuthorWorksPreview(works) {
            const summary = document.getElementById('authorWorksSummary');
            const grid = document.getElementById('authorWorksGrid');
            if (!summary || !grid) return;

            if (!works.length) {
                summary.textContent = '当前作者暂无作品记录';
                grid.innerHTML = `
                    <div class="empty-state" style="grid-column:1 / -1;">
                        <div class="empty-state-icon">📭</div>
                        <p>当前作者暂无作品记录</p>
                    </div>
                `;
                return;
            }

            const completedWorks = works.filter(work => work.download_status === 'completed').length;
            const previewableWorks = works.filter(work => Boolean(work.primary_preview_url)).length;
            summary.textContent = `共 ${works.length} 个作品，${completedWorks} 个已完成，${previewableWorks} 个可直接预览`;

            // 清理已不存在的选中项
            const validIds = new Set(works.map(w => w.id));
            selectedWorkIds = new Set([...selectedWorkIds].filter(id => validIds.has(id)));

            grid.innerHTML = works.map(work => {
                const safeTitle = escapeHtml(work.title || '未命名作品');
                const typeText = work.work_type === 'video' ? '视频作品' : `图集 ${work.image_count || 0} 张`;
                const canPreview = work.work_type === 'video'
                    ? Boolean(work.video_url)
                    : Array.isArray(work.image_urls) && work.image_urls.length > 0;
                const coverHtml = work.work_type === 'images' && work.primary_preview_url
                    ? `<img src="${escapeHtml(work.primary_preview_url)}" alt="${safeTitle}" loading="lazy" referrerpolicy="no-referrer" onerror="this.replaceWith(document.createTextNode('🖼️'))">`
                    : `<span>${work.work_type === 'video' ? '🎬' : '🖼️'}</span>`;
                const hasFailed = Array.isArray(work.files) && work.files.some(f => f.status === 'failed' || f.status === 'cancelled');
                const isChecked = selectedWorkIds.has(work.id);
                const canManageFiles = work.work_type === 'images' && Array.isArray(work.files) && work.files.length > 0;

                return `
                    <div class="author-work-card${isChecked ? ' selected' : ''}" data-work-id="${work.id}">
                        <label class="work-select">
                            <input type="checkbox" ${isChecked ? 'checked' : ''} onchange="toggleWorkSelect(${work.id}, this.checked)">
                        </label>
                        <div class="author-work-cover">${coverHtml}</div>
                        <div class="author-work-title">${safeTitle}</div>
                        <div class="author-work-meta">
                            <span>${typeText}</span>
                            <span>${work.completed_task_count || 0}/${work.total_task_count || 0}</span>
                        </div>
                        <div class="author-work-meta">
                            ${getStatusTag(work.download_status, 0)}
                        </div>
                        <div class="author-work-actions">
                            ${canPreview ? `<button class="secondary" onclick="previewAuthorWork(${work.id})">预览</button>` : ''}
                            <button class="secondary" onclick="redownloadWork(${work.id})">重新下载</button>
                            ${hasFailed ? `<button class="secondary" onclick="retryWorkFailed(${work.id})">重试失败</button>` : ''}
                            <button class="secondary" onclick="filterTasksByAuthor(${work.author_id})">关联任务</button>
                            ${canManageFiles ? `<button class="secondary" onclick="toggleWorkFiles(${work.id})">单文件管理</button>` : ''}
                            <button class="secondary work-delete-btn" onclick="deleteWork(${work.id})">删除</button>
                        </div>
                        ${canManageFiles ? `<div class="work-files-panel" id="workFiles-${work.id}" style="display:none;">${renderWorkFilesPanel(work)}</div>` : ''}
                    </div>
                `;
            }).join('');

            updateWorksSelectionUI();
        }

        function renderWorkFilesPanel(work) {
            const files = Array.isArray(work.files) ? work.files : [];
            if (!files.length) {
                return '<div class="work-files-empty">暂无文件任务记录</div>';
            }
            return files.map(file => {
                const idxLabel = `#${(file.file_index ?? 0) + 1}`;
                const statusTag = getStatusTag(file.status, 0);
                const previewBtn = file.local_available && file.preview_url
                    ? `<button class="secondary" onclick="previewWorkFile(${work.id}, ${file.file_index})">预览</button>`
                    : '';
                return `
                    <div class="work-file-row">
                        <span class="work-file-idx">${idxLabel}</span>
                        <span class="work-file-status">${statusTag}</span>
                        ${previewBtn}
                        <button class="secondary work-file-delete" onclick="deleteWorkFile(${work.id}, ${file.file_index})">删除</button>
                    </div>
                `;
            }).join('');
        }

        function previewAuthorWork(workId) {
            const work = window._currentAuthorWorksMap?.[workId];
            if (!work) {
                showToast('未找到作品信息', 'error');
                return;
            }

            if (work.work_type === 'video') {
                if (!work.video_url) {
                    showToast('当前作品暂无可用视频预览', 'error');
                    return;
                }
                openMediaPreview({
                    type: 'video',
                    title: work.title || '视频预览',
                    meta: `${currentAuthorPreview?.authorName || '作者'} · 视频作品`,
                    url: work.video_url
                });
                return;
            }

            if (!Array.isArray(work.image_urls) || work.image_urls.length === 0) {
                showToast('当前作品暂无可用图片预览', 'error');
                return;
            }

            openMediaPreview({
                type: 'image',
                title: work.title || '图片预览',
                meta: `${currentAuthorPreview?.authorName || '作者'} · 图集 ${work.image_urls.length} 张`,
                url: work.image_urls[0],
                images: work.image_urls
            });
        }

        function previewWorkFile(workId, fileIndex) {
            const work = window._currentAuthorWorksMap?.[workId];
            if (!work) return;
            const file = (work.files || []).find(f => (f.file_index ?? 0) === fileIndex);
            if (!file || !file.preview_url) {
                showToast('该文件暂无可用预览', 'error');
                return;
            }
            openMediaPreview({
                type: file.media_type === 'video' ? 'video' : 'image',
                title: work.title || (file.media_type === 'video' ? '实况图片预览' : '图片预览'),
                meta: `${currentAuthorPreview?.authorName || '作者'} · 第 ${fileIndex + 1} 张${file.media_type === 'video' ? '（实况）' : ''}`,
                url: file.preview_url,
                images: file.media_type === 'video' ? [] : [file.preview_url]
            });
        }

        function toggleWorkFiles(workId) {
            const panel = document.getElementById(`workFiles-${workId}`);
            if (!panel) return;
            panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
        }

        function toggleWorkSelect(workId, checked) {
            if (checked) {
                selectedWorkIds.add(workId);
            } else {
                selectedWorkIds.delete(workId);
            }
            const card = document.querySelector(`.author-work-card[data-work-id="${workId}"]`);
            if (card) card.classList.toggle('selected', checked);
            updateWorksSelectionUI();
        }

        function toggleSelectAllWorks(checked) {
            const works = currentAuthorPreview?.works || [];
            selectedWorkIds = checked ? new Set(works.map(w => w.id)) : new Set();
            document.querySelectorAll('#authorWorksGrid .work-select input[type="checkbox"]').forEach(cb => {
                cb.checked = checked;
            });
            document.querySelectorAll('#authorWorksGrid .author-work-card').forEach(card => {
                card.classList.toggle('selected', checked);
            });
            updateWorksSelectionUI();
        }

        function updateWorksSelectionUI() {
            const count = selectedWorkIds.size;
            const countEl = document.getElementById('worksSelectedCount');
            const batchBtn = document.getElementById('worksBatchDeleteBtn');
            const selectAll = document.getElementById('worksSelectAll');
            if (countEl) countEl.textContent = `已选 ${count}`;
            if (batchBtn) batchBtn.disabled = count === 0;
            if (selectAll) {
                const total = (currentAuthorPreview?.works || []).length;
                selectAll.checked = total > 0 && count === total;
                selectAll.indeterminate = count > 0 && count < total;
            }
        }

        async function reloadCurrentAuthorWorks() {
            if (!currentAuthorPreview?.authorId) return;
            try {
                const works = await fetchAllAuthorWorks(currentAuthorPreview.authorId);
                currentAuthorPreview.works = Array.isArray(works) ? works : [];
                window._currentAuthorWorksMap = Object.fromEntries(currentAuthorPreview.works.map(work => [work.id, work]));
                renderAuthorWorksPreview(currentAuthorPreview.works);
            } catch (e) {
                showToast(e.message || '刷新作品列表失败', 'error');
            }
        }

        async function deleteWork(workId) {
            const work = window._currentAuthorWorksMap?.[workId];
            const name = work?.title ? `《${work.title}》` : '该作品';
            if (!confirm(`确定删除${name}吗？\n将同时删除已下载的文件、任务与历史记录，且不可恢复。删除后订阅检查不会重新下载。`)) {
                return;
            }
            try {
                const data = await apiRequest(`${API_BASE}/works/${workId}`, { method: 'DELETE' }, '删除作品失败');
                showToast(data.message || '作品已删除');
                selectedWorkIds.delete(workId);
                await reloadCurrentAuthorWorks();
                fetchAuthors();
            } catch (e) {
                showToast(e.message || '删除作品失败', 'error');
            }
        }

        async function batchDeleteWorks() {
            const ids = [...selectedWorkIds];
            if (!ids.length) return;
            if (!confirm(`确定删除选中的 ${ids.length} 个作品吗？\n将同时删除已下载的文件、任务与历史记录，且不可恢复。删除后订阅检查不会重新下载。`)) {
                return;
            }
            try {
                const data = await apiRequest(`${API_BASE}/works/batch-delete`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ work_ids: ids })
                }, '批量删除失败');
                showToast(data.message || '已批量删除');
                selectedWorkIds = new Set();
                await reloadCurrentAuthorWorks();
                fetchAuthors();
            } catch (e) {
                showToast(e.message || '批量删除失败', 'error');
            }
        }

        async function deleteWorkFile(workId, fileIndex) {
            if (!confirm(`确定删除第 ${fileIndex + 1} 个文件吗？\n将删除该文件及其任务记录，且不可恢复。重新下载该作品时此文件不会再次下载。`)) {
                return;
            }
            try {
                const data = await apiRequest(`${API_BASE}/works/${workId}/files/${fileIndex}`, { method: 'DELETE' }, '删除文件失败');
                showToast(data.message || '文件已删除');
                await reloadCurrentAuthorWorks();
                const panel = document.getElementById(`workFiles-${workId}`);
                if (panel) panel.style.display = 'block';
            } catch (e) {
                showToast(e.message || '删除文件失败', 'error');
            }
        }

        async function redownloadWork(workId) {
            try {
                const data = await apiRequest(`${API_BASE}/works/${workId}/redownload`, { method: 'POST' }, '重新下载失败');
                showToast(data.message || '已提交重新下载');
                await reloadCurrentAuthorWorks();
            } catch (e) {
                showToast(e.message || '重新下载失败', 'error');
            }
        }

        async function retryWorkFailed(workId) {
            try {
                const data = await apiRequest(`${API_BASE}/works/${workId}/retry-failed`, { method: 'POST' }, '重试失败任务出错');
                showToast(data.message || '已重试失败任务');
                await reloadCurrentAuthorWorks();
            } catch (e) {
                showToast(e.message || '重试失败任务出错', 'error');
            }
        }

        function closeAuthorWorksPreview() {
            const modal = document.getElementById('authorWorksModal');
            if (modal) {
                modal.classList.remove('show');
                modal.setAttribute('aria-hidden', 'true');
            }
            currentAuthorPreview = null;
            selectedWorkIds = new Set();
            syncBodyScrollLock();
        }

        function onAuthorWorksBackdropClick(event) {
            if (event.target?.id === 'authorWorksModal') {
                closeAuthorWorksPreview();
            }
        }

        function filterTasksByAuthor(authorId) {
            const author = window._currentAuthorMap?.[authorId];
            currentTaskAuthorFilter = authorId;
            currentTaskAuthorName = author?.nickname || currentAuthorPreview?.authorName || `作者 ${authorId}`;
            tasksPage = 1;
            updateTaskAuthorFilterBanner();
            closeAuthorWorksPreview();
            switchSubTab('tasks');
            fetchTasks();
        }

        function viewCurrentAuthorTasks() {
            if (!currentAuthorPreview?.authorId) {
                return;
            }
            filterTasksByAuthor(currentAuthorPreview.authorId);
        }

        function clearTaskAuthorFilter() {
            currentTaskAuthorFilter = null;
            currentTaskAuthorName = '';
            tasksPage = 1;
            updateTaskAuthorFilterBanner();
            fetchTasks();
        }

        // 检查所有订阅
        async function checkAllSubscriptions() {
            try {
                const data = await apiRequest(`${API_BASE}/authors/check-all`, { method: 'POST' }, '启动订阅检查失败');
                showToast(data.message || '正在检查更新...');
            } catch (e) {
                showToast(e.message || '操作失败', 'error');
            }
        }

        // 抖音任务状态标签切换
        document.querySelectorAll('#dyTaskTabBar .tab').forEach(tab => {
            tab.addEventListener('click', () => {
                document.querySelectorAll('#dyTaskTabBar .tab').forEach(t => t.classList.remove('active'));
                tab.classList.add('active');
                currentStatusFilter = tab.dataset.status;
                tasksPage = 1;
                updateTaskHeaderButtons();
                fetchTasks();
            });
        });

        function updateTaskHeaderButtons() {
            const isFailed = currentStatusFilter === 'failed';
            const isDownloading = currentStatusFilter === 'downloading';
            const isPaused = currentStatusFilter === 'paused';
            const isPending = currentStatusFilter === 'pending';
            const isAll = !currentStatusFilter;

            const show = (id, visible) => {
                const el = document.getElementById(id);
                if (el) el.style.display = visible ? '' : 'none';
            };

            // 全部暂停: 全部/下载中/待处理 页显示
            show('btnPauseAll', isAll || isDownloading || isPending);
            // 分发待处理: 全部/待处理 页显示
            show('btnRedispatch', isAll || isPending);
            // 强制重试卡住: 全部/下载中 页显示
            show('btnForceRetryAll', isAll || isDownloading);
            // 批量重试: 全部/失败 页显示
            show('btnRetryAllFailed', isAll || isFailed);
            // 刷新链接重试: 仅失败页
            show('btnRefreshRetryAll', isFailed);
            // 全部删除: 仅失败页
            show('btnDeleteAllStatus', isFailed);
        }

        // X 任务标签切换
        document.querySelectorAll('#xTabBar .tab').forEach(tab => {
            tab.addEventListener('click', () => {
                document.querySelectorAll('#xTabBar .tab').forEach(t => t.classList.remove('active'));
                tab.classList.add('active');
                currentXStatusFilter = tab.dataset.xStatus;
                xTasksPage = 1;
                fetchXTasks();
            });
        });

        // ============ X/Twitter 下载功能 ============

        async function startXDownload() {
            const urlInput = document.getElementById('xProfileUrl');
            const url = urlInput.value.trim();
            if (!url) {
                showToast('请输入 X/Twitter 用户主页链接或用户名', 'error');
                return;
            }
            try {
                const res = await apiFetch(`${API_BASE}/x/download`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ profile_url: url })
                });
                const data = await res.json();
                if (res.ok) {
                    showToast(`${data.author_display_name || `@${data.username}`} 已进入 X 下载队列`);
                    urlInput.value = '';
                    fetchXTasks();
                    fetchXAuthors();
                } else {
                    showToast(data.detail || '创建任务失败', 'error');
                }
            } catch (e) {
                showToast('请求失败', 'error');
            }
        }

        async function fetchXTasks() {
            try {
                let url = `${API_BASE}/x/tasks?page=${xTasksPage}&page_size=${xTasksPageSize}`;
                if (currentXStatusFilter) url += `&status=${currentXStatusFilter}`;
                const res = await apiFetch(url);
                const data = await res.json();

                xTasksTotal = data.total;
                xTasksTotalPages = data.pages;

                const list = document.getElementById('xTaskList');
                if (!data.items || data.items.length === 0) {
                    list.innerHTML = '<div class="empty-state"><div class="empty-state-icon">🐦</div><p>暂无 X 下载任务</p></div>';
                    document.getElementById('xTasksPagination').innerHTML = '';
                    return;
                }

                list.innerHTML = data.items.map(t => {
                    const st = getXTaskStatusMeta(t);
                    const displayName = escapeHtml(t.author_display_name || `@${t.username}`);
                    const escapedUsername = escapeHtml(t.username);
                    const handleText = displayName === `@${escapedUsername}` ? displayName : `${displayName} · @${escapedUsername}`;
                    const totalMedia = Number(t.total_media_count || 0);
                    const downloadedMedia = Number(t.downloaded_media_count || 0);
                    const storedMedia = Number(t.file_count || 0);
                    const progressValue = t.status === 'completed'
                        ? 100
                        : Math.max(0, Math.min(100, Number(t.progress_percent || 0)));
                    const progressWidth = progressValue > 0
                        ? progressValue
                        : ((downloadedMedia || storedMedia || t.has_live_state) ? Math.min(28, Math.max(8, (downloadedMedia || storedMedia || 1) * 6)) : 0);
                    const progressSummary = totalMedia > 0
                        ? `${downloadedMedia}/${totalMedia} 媒体`
                        : (storedMedia > 0 ? `${storedMedia} 个媒体已落盘` : '等待引擎返回媒体信息');
                    const taskMetrics = [
                        `<span class="x-metric-pill">${escapeHtml((t.engine_name || 'gallery-dl').toUpperCase())}</span>`,
                        `<span class="x-metric-pill">${escapeHtml(progressSummary)}</span>`,
                        t.has_live_state ? '<span class="x-metric-pill is-live">实时状态</span>' : '<span class="x-metric-pill">归档状态</span>',
                        t.retry_count ? `<span class="x-metric-pill">重试 ${t.retry_count} 次</span>` : ''
                    ].filter(Boolean).join('');
                    const activityTime = formatDateTime(t.completed_at || t.last_heartbeat_at || t.started_at || t.created_at);
                    const errorInfo = t.error_message
                        ? `<div class="x-task-error">${escapeHtml(t.error_message)}</div>`
                        : '';

                    let actions = '';
                    if (t.status === 'downloading' || t.status === 'pending') {
                        actions += `<button class="secondary" onclick="cancelXTask(${t.id})">取消</button>`;
                    }
                    if (t.status === 'failed' || t.status === 'cancelled') {
                        actions += `<button class="secondary" onclick="retryXTask(${t.id})">重试</button>`;
                    }
                    actions += `<button class="secondary" onclick="deleteXTask(${t.id})">删除</button>`;
                    if (t.status !== 'pending' || t.last_log_line || t.output_log) {
                        actions += `<button class="secondary" onclick="toggleXLog(${t.id})">日志</button>`;
                    }

                    return `
                        <div class="task-item x-task-item" id="x-task-${t.id}">
                            <div class="task-thumbnail x-brand-icon">𝕏</div>
                            <div class="task-info">
                                <div class="x-task-topline">
                                    <div class="x-task-title">
                                        <div class="x-task-handle">${handleText}</div>
                                        <div class="x-task-phase"><span>阶段</span><strong>${escapeHtml(st.phaseLabel)}</strong></div>
                                    </div>
                                    <span class="status-tag ${st.cls}">${st.label}</span>
                                </div>
                                <div class="x-progress-rail"><div class="x-progress-fill" style="width:${progressWidth}%"></div></div>
                                <div class="x-task-metrics">${taskMetrics}</div>
                                <div class="x-task-meta">
                                    <span>${activityTime}</span>
                                    <span>${progressValue ? `${Math.round(progressValue)}%` : '等待进度'}</span>
                                </div>
                                ${errorInfo}
                                <div class="x-log-container" id="x-log-${t.id}"></div>
                            </div>
                            <div class="task-actions">
                                ${actions}
                            </div>
                        </div>`;
                }).join('');

                renderXPagination();
            } catch (e) {
                console.error('获取 X 任务失败:', e);
            }
        }

        function refreshXTasks() {
            fetchXTasks();
        }

        function renderXPagination() {
            const container = document.getElementById('xTasksPagination');
            if (xTasksTotalPages <= 1) {
                container.innerHTML = '';
                return;
            }
            container.innerHTML = `
                <div class="pagination-info">第 ${xTasksPage}/${xTasksTotalPages} 页，共 ${xTasksTotal} 条</div>
                <div class="pagination-controls">
                    <button class="secondary" onclick="xGoToPage(1)" ${xTasksPage <= 1 ? 'disabled' : ''} style="padding:6px 12px;font-size:12px;">首页</button>
                    <button class="secondary" onclick="xGoToPage(${xTasksPage - 1})" ${xTasksPage <= 1 ? 'disabled' : ''} style="padding:6px 12px;font-size:12px;">上一页</button>
                    <button class="secondary" onclick="xGoToPage(${xTasksPage + 1})" ${xTasksPage >= xTasksTotalPages ? 'disabled' : ''} style="padding:6px 12px;font-size:12px;">下一页</button>
                    <button class="secondary" onclick="xGoToPage(${xTasksTotalPages})" ${xTasksPage >= xTasksTotalPages ? 'disabled' : ''} style="padding:6px 12px;font-size:12px;">末页</button>
                </div>`;
        }

        function xGoToPage(page) {
            if (page < 1 || page > xTasksTotalPages) return;
            xTasksPage = page;
            fetchXTasks();
        }

        async function cancelXTask(taskId) {
            try {
                const res = await apiFetch(`${API_BASE}/x/tasks/${taskId}/cancel`, { method: 'POST' });
                if (res.ok) {
                    showToast('任务已取消');
                    fetchXTasks();
                } else {
                    const data = await res.json();
                    showToast(data.detail || '取消失败', 'error');
                }
            } catch (e) {
                showToast('操作失败', 'error');
            }
        }

        async function retryXTask(taskId) {
            try {
                const res = await apiFetch(`${API_BASE}/x/tasks/${taskId}/retry`, { method: 'POST' });
                if (res.ok) {
                    showToast('任务已重新提交');
                    fetchXTasks();
                } else {
                    const data = await res.json();
                    showToast(data.detail || '重试失败', 'error');
                }
            } catch (e) {
                showToast('操作失败', 'error');
            }
        }

        async function deleteXTask(taskId) {
            if (!confirm('确认删除此任务？')) return;
            try {
                const res = await apiFetch(`${API_BASE}/x/tasks/${taskId}`, { method: 'DELETE' });
                if (res.ok) {
                    showToast('任务已删除');
                    fetchXTasks();
                } else {
                    const data = await res.json();
                    showToast(data.detail || '删除失败', 'error');
                }
            } catch (e) {
                showToast('操作失败', 'error');
            }
        }

        async function toggleXLog(taskId) {
            const container = document.getElementById(`x-log-${taskId}`);
            if (!container) return;
            if (!container.classList.contains('show')) {
                container.classList.add('show');
                await fetchXLog(taskId);
            } else {
                container.classList.remove('show');
            }
        }

        async function fetchXLog(taskId) {
            try {
                const res = await apiFetch(`${API_BASE}/x/tasks/${taskId}/log?start=0`);
                const data = await res.json();
                const container = document.getElementById(`x-log-${taskId}`);
                if (container) {
                    container.textContent = data.lines && data.lines.length
                        ? data.lines.join('\n')
                        : '暂无任务日志';
                    container.scrollTop = container.scrollHeight;
                }
            } catch (e) {
                console.error('获取日志失败:', e);
            }
        }

        async function saveXCookie() {
            const input = document.getElementById('xCookieInput');
            const cookie = input.value.trim();
            if (!cookie) {
                showToast('请输入 Cookie 内容', 'error');
                return;
            }
            try {
                const res = await apiFetch(`${API_BASE}/x/config/cookie`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ cookie: cookie })
                });
                const data = await res.json();
                if (res.ok) {
                    showToast(data.message || 'X Cookie 已保存');
                    checkXCookie();
                    input.value = '';
                } else {
                    showToast(data.detail || '保存失败', 'error');
                }
            } catch (e) {
                showToast('保存失败', 'error');
            }
        }

        async function checkXCookie() {
            try {
                const res = await apiFetch(`${API_BASE}/x/config/cookie`);
                const data = await res.json();
                if (!res.ok) {
                    throw new Error(getApiMessage(data, '检查 X Cookie 失败'));
                }
                const status = document.getElementById('xCookieStatus');
                if (data.configured) {
                    status.textContent = '已配置';
                    status.className = 'status-tag status-completed';
                } else {
                    status.textContent = '未配置';
                    status.className = 'status-tag status-pending';
                }
            } catch (e) {
                console.error('检查 X Cookie 失败:', e);
            }
        }

        // 输入框回车触发
        document.getElementById('xProfileUrl')?.addEventListener('keypress', function(e) {
            if (e.key === 'Enter') startXDownload();
        });
        document.getElementById('xAuthorUrl')?.addEventListener('keypress', function(e) {
            if (e.key === 'Enter') addXAuthor();
        });
        document.getElementById('shareUrl')?.addEventListener('keypress', function(e) {
            if (e.key === 'Enter') startDownload();
        });

        // ============ X 用户管理 ============

        async function addXAuthor() {
            const input = document.getElementById('xAuthorUrl');
            const url = input.value.trim();
            if (!url) { showToast('请输入链接或用户名', 'error'); return; }
            try {
                const res = await apiFetch(`${API_BASE}/x/authors/`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ profile_url: url })
                });
                const data = await res.json();
                if (res.ok) {
                    showToast(`${data.display_name || `@${data.username}`} 已加入 X 用户列表`);
                    input.value = '';
                    fetchXAuthors();
                } else {
                    showToast(data.detail || '添加失败', 'error');
                }
            } catch (e) { showToast('请求失败', 'error'); }
        }

        async function fetchXAuthors() {
            try {
                const res = await apiFetch(`${API_BASE}/x/authors/?page=${xAuthorsPage}&page_size=${xAuthorsPageSize}`);
                const data = await res.json();
                xAuthorsTotal = data.total;
                xAuthorsTotalPages = data.pages;
                const list = document.getElementById('xAuthorList');
                if (!data.items || data.items.length === 0) {
                    list.innerHTML = '<div class="empty-state"><div class="empty-state-icon">👥</div><p>暂无 X 用户</p></div>';
                    document.getElementById('xAuthorsPagination').innerHTML = '';
                    return;
                }
                list.innerHTML = data.items.map(a => {
                    const status = getXAuthorStatusMeta(a);
                    const displayName = escapeHtml(a.display_name || `@${a.username}`);
                    const escapedUsername = escapeHtml(a.username);
                    const subtitle = displayName === `@${escapedUsername}` ? '' : `@${escapedUsername}`;
                    const metaItems = [
                        `<span class="x-author-meta-item">已下载 ${a.total_downloads || 0}</span>`,
                        `<span class="x-author-meta-item">检查间隔 ${escapeHtml(formatIntervalSeconds(a.check_interval))}</span>`,
                        `<span class="x-author-meta-item">${a.last_check_time ? `上次检查 ${escapeHtml(formatDateTime(a.last_check_time))}` : '尚未检查'}</span>`
                    ].join('');
                    const errorBlock = a.last_error ? `<div class="x-author-error">${escapeHtml(a.last_error)}</div>` : '';
                    return `
                    <div class="author-item x-author-card" data-author-id="${a.id}">
                        <div class="author-avatar x-brand-icon">𝕏</div>
                        <div class="x-author-body">
                            <div class="x-author-title-row">
                                <div class="author-name x-author-name">${displayName}</div>
                                <span class="x-author-status${status.cls}">${escapeHtml(status.label)}</span>
                            </div>
                            <div class="x-task-phase">${subtitle ? `<span>${subtitle}</span>` : '<span>已连接 X 用户</span>'}</div>
                            <div class="x-author-meta">${metaItems}</div>
                            ${errorBlock}
                            <div class="x-author-actions">
                                <button class="subscribe-toggle ${a.is_subscribed ? 'subscribed' : 'secondary'}" onclick="toggleXSubscribe(${a.id}, ${a.is_subscribed})">
                                    ${a.is_subscribed ? '已订阅' : '订阅'}
                                </button>
                                <button class="secondary" onclick="downloadXAuthor(${a.id})">下载</button>
                                <button class="secondary" onclick="deleteXAuthor(${a.id})">删除</button>
                            </div>
                        </div>
                    </div>`;
                }).join('');
                renderXAuthorsPagination();
            } catch (e) { console.error('获取 X 用户失败:', e); }
        }

        function renderXAuthorsPagination() {
            const container = document.getElementById('xAuthorsPagination');
            if (xAuthorsTotalPages <= 1) { container.innerHTML = ''; return; }
            container.innerHTML = `
                <div class="pagination-info">共 ${xAuthorsTotal} 位用户，第 ${xAuthorsPage}/${xAuthorsTotalPages} 页</div>
                <div class="pagination-controls">
                    <button class="pagination-btn" onclick="xAuthorsGoToPage(1)" ${xAuthorsPage <= 1 ? 'disabled' : ''}>首页</button>
                    <button class="pagination-btn" onclick="xAuthorsGoToPage(${xAuthorsPage - 1})" ${xAuthorsPage <= 1 ? 'disabled' : ''}>上一页</button>
                    <button class="pagination-btn" onclick="xAuthorsGoToPage(${xAuthorsPage + 1})" ${xAuthorsPage >= xAuthorsTotalPages ? 'disabled' : ''}>下一页</button>
                    <button class="pagination-btn" onclick="xAuthorsGoToPage(${xAuthorsTotalPages})" ${xAuthorsPage >= xAuthorsTotalPages ? 'disabled' : ''}>末页</button>
                </div>`;
        }

        function xAuthorsGoToPage(page) {
            if (page < 1 || page > xAuthorsTotalPages) return;
            xAuthorsPage = page;
            fetchXAuthors();
        }

        async function toggleXSubscribe(authorId, isSubscribed) {
            try {
                const action = isSubscribed ? 'unsubscribe' : 'subscribe';
                const res = await apiFetch(`${API_BASE}/x/authors/${authorId}/${action}`, { method: 'POST' });
                if (res.ok) {
                    const data = await res.json();
                    showToast(data.message || (isSubscribed ? '已取消订阅' : '已订阅'));
                } else {
                    const d = await res.json(); showToast(d.detail || '操作失败', 'error');
                }
                fetchXAuthors();
            } catch (e) { showToast('操作失败', 'error'); }
        }

        async function downloadXAuthor(authorId) {
            try {
                const res = await apiFetch(`${API_BASE}/x/authors/${authorId}/download`, { method: 'POST' });
                if (res.ok) {
                    const data = await res.json();
                    showToast(data.message || '下载任务已提交');
                    fetchXTasks();
                } else {
                    const d = await res.json(); showToast(d.detail || '操作失败', 'error');
                }
            } catch (e) { showToast('操作失败', 'error'); }
        }

        async function deleteXAuthor(authorId) {
            if (!confirm('确定删除此用户？')) return;
            try {
                const res = await apiFetch(`${API_BASE}/x/authors/${authorId}`, { method: 'DELETE' });
                if (res.ok) {
                    const data = await res.json();
                    showToast(data.message || '已删除');
                    fetchXAuthors();
                } else {
                    const d = await res.json(); showToast(d.detail || '删除失败', 'error');
                }
            } catch (e) { showToast('删除失败', 'error'); }
        }

        async function checkAllXSubscriptions() {
            try {
                const res = await apiFetch(`${API_BASE}/x/authors/check-all`, { method: 'POST' });
                const data = await res.json();
                showToast(data.message || '正在检查 X 订阅更新...');
            } catch (e) { showToast('操作失败', 'error'); }
        }

        // ============ 设置面板折叠 ============

        function toggleSettings(platform) {
            const bodyId = platform === 'douyin' ? 'dySettingsBody' : 'xSettingsBody';
            const toggleId = platform === 'douyin' ? 'dySettingsToggle' : 'xSettingsToggle';
            const body = document.getElementById(bodyId);
            const toggle = document.getElementById(toggleId);
            const isCollapsed = body.classList.contains('collapsed');
            if (isCollapsed) {
                body.classList.remove('collapsed');
                body.style.maxHeight = body.scrollHeight + 200 + 'px';
                toggle.classList.add('expanded');
                toggle.textContent = '▼ 折叠设置';
            } else {
                body.style.maxHeight = '0';
                body.classList.add('collapsed');
                toggle.classList.remove('expanded');
                toggle.textContent = '▶ 展开设置';
            }
            localStorage.setItem(`settings-${platform}-expanded`, isCollapsed ? '1' : '0');
        }

        function switchSettingsTab(platform, tab) {
            const prefix = platform === 'douyin' ? 'dy' : 'x';
            const tabsContainer = document.getElementById(`${prefix}SettingsSubTabs`);
            let activeTab = tab;
            if (tabsContainer) {
                if (!tabsContainer.querySelector(`[data-stab="${activeTab}"]`)) {
                    activeTab = tabsContainer.querySelector('.settings-sub-tab')?.dataset.stab || activeTab;
                }
                tabsContainer.querySelectorAll('.settings-sub-tab').forEach(t => {
                    t.classList.toggle('active', t.dataset.stab === activeTab);
                });
            }
            const bodyEl = document.getElementById(`${prefix}SettingsBody`);
            if (bodyEl) {
                bodyEl.querySelectorAll('.settings-tab-panel').forEach(p => {
                    p.style.display = p.dataset.stab === activeTab ? '' : 'none';
                });
                // 重算 maxHeight
                if (!bodyEl.classList.contains('collapsed')) {
                    bodyEl.style.maxHeight = bodyEl.scrollHeight + 200 + 'px';
                }
            }
            localStorage.setItem(`settings-${platform}-tab`, activeTab);
        }

        // ============ 版本检查与更新 ============

        let _aboutInfoLoaded = false;

        function openAboutTab() {
            switchSettingsTab('douyin', 'about');
            if (!_aboutInfoLoaded) {
                _aboutInfoLoaded = true;
                loadUpdateInfo();
            }
        }

        function _recalcSettingsHeight() {
            const bodyEl = document.getElementById('dySettingsBody');
            if (bodyEl && !bodyEl.classList.contains('collapsed')) {
                bodyEl.style.maxHeight = bodyEl.scrollHeight + 200 + 'px';
            }
        }

        function _formatCommit(c) {
            if (!c) return '—';
            let dateText = '';
            if (c.date) {
                const d = new Date(c.date);
                if (!isNaN(d.getTime())) dateText = d.toLocaleString('zh-CN');
            }
            const short = escapeHtml(c.short || '');
            const subject = escapeHtml(c.subject || '');
            return `<code>${short}</code> ${subject}${dateText ? ' · ' + escapeHtml(dateText) : ''}`;
        }

        function renderUpdateInfo(data) {
            const infoEl = document.getElementById('updateVersionInfo');
            if (!infoEl) return;
            const rows = [];
            rows.push(`<div class="update-row"><span class="update-label">当前分支</span><span>${escapeHtml(data.branch || '—')}</span></div>`);
            rows.push(`<div class="update-row"><span class="update-label">当前版本</span><span>${_formatCommit(data.current)}</span></div>`);
            if (data.remote) {
                rows.push(`<div class="update-row"><span class="update-label">远程最新</span><span>${_formatCommit(data.remote)}</span></div>`);
            }
            if (data.remote_url) {
                rows.push(`<div class="update-row"><span class="update-label">仓库地址</span><span style="word-break:break-all;">${escapeHtml(data.remote_url)}</span></div>`);
            }
            if (data.repo_dir) {
                rows.push(`<div class="update-row"><span class="update-label">项目目录</span><span style="word-break:break-all;">${escapeHtml(data.repo_dir)}</span></div>`);
            }
            let statusHtml;
            if (data.has_update) {
                statusHtml = `<span class="update-badge update-badge-new">落后 ${data.behind} 个提交，可更新</span>`;
            } else if (data.remote) {
                statusHtml = `<span class="update-badge update-badge-latest">已是最新版本</span>`;
            } else {
                statusHtml = `<span class="update-badge">${escapeHtml(data.message || '无法比较')}</span>`;
            }
            rows.push(`<div class="update-row"><span class="update-label">状态</span><span>${statusHtml}</span></div>`);
            if (data.has_local_changes) {
                rows.push(`<div class="update-row"><span class="update-label" style="color:var(--warning);">提示</span><span style="color:var(--warning);">检测到本地有未提交改动，更新（快进合并）可能失败</span></div>`);
            }
            infoEl.innerHTML = rows.join('');
        }

        function _toggleApplyBtn(show, data = null) {
            const applyBtn = document.getElementById('applyUpdateBtn');
            if (!applyBtn) return;
            applyBtn.style.display = show ? '' : 'none';
            const disabledReason = data && data.update_supported === false ? data.update_disabled_reason : '';
            applyBtn.disabled = !!disabledReason;
            applyBtn.title = disabledReason || '';
            if (disabledReason && show) {
                const resultEl = document.getElementById('updateResult');
                if (resultEl) {
                    resultEl.style.display = '';
                    resultEl.innerHTML = `<div style="color:var(--warning);"><strong>网页端更新已禁用</strong><br>${escapeHtml(disabledReason)}</div>`;
                }
            }
        }

        async function loadUpdateInfo() {
            const infoEl = document.getElementById('updateVersionInfo');
            try {
                const data = await apiRequest(`${API_BASE}/update/info`, {}, '读取版本失败');
                renderUpdateInfo(data);
                _toggleApplyBtn(!!data.has_update, data);
            } catch (e) {
                if (infoEl) infoEl.innerHTML = `<p style="color:var(--error);margin:0;">${escapeHtml(e.message || '读取版本失败')}</p>`;
                _toggleApplyBtn(false);
            } finally {
                _recalcSettingsHeight();
            }
        }

        async function checkForUpdate() {
            const btn = document.getElementById('checkUpdateBtn');
            const resultEl = document.getElementById('updateResult');
            const orig = btn ? btn.textContent : '';
            if (btn) { btn.disabled = true; btn.textContent = '检查中...'; }
            if (resultEl) resultEl.style.display = 'none';
            try {
                const data = await apiRequest(`${API_BASE}/update/check`, {}, '检查更新失败');
                renderUpdateInfo(data);
                _toggleApplyBtn(!!data.has_update, data);
                showToast(data.has_update ? `发现新版本：落后 ${data.behind} 个提交` : '已是最新版本');
            } catch (e) {
                showToast(e.message || '检查更新失败', 'error');
                const infoEl = document.getElementById('updateVersionInfo');
                if (infoEl) infoEl.innerHTML = `<p style="color:var(--error);margin:0;">${escapeHtml(e.message || '检查更新失败')}</p>`;
            } finally {
                if (btn) { btn.disabled = false; btn.textContent = orig; }
                _recalcSettingsHeight();
            }
        }

        async function applyUpdate() {
            if (!confirm('确定拉取远程仓库的最新代码并更新吗？\n更新会执行 git pull（--ff-only）并重启后台 Worker/Beat。')) return;
            const btn = document.getElementById('applyUpdateBtn');
            const resultEl = document.getElementById('updateResult');
            const orig = btn ? btn.textContent : '';
            if (btn) { btn.disabled = true; btn.textContent = '更新中...'; }
            try {
                const data = await apiRequest(`${API_BASE}/update/apply`, { method: 'POST' }, '更新失败');
                if (resultEl) {
                    resultEl.style.display = '';
                    const lines = [`<strong>${escapeHtml(data.message || '更新完成')}</strong>`];
                    if (data.updated && data.before && data.after) {
                        lines.push(`版本：<code>${escapeHtml(data.before.short || '')}</code> → <code>${escapeHtml(data.after.short || '')}</code>`);
                    }
                    if (Array.isArray(data.restart) && data.restart.length) {
                        lines.push(escapeHtml(data.restart.join('；')));
                    }
                    if (data.restart_note) lines.push(`<span style="color:var(--text-secondary);">${escapeHtml(data.restart_note)}</span>`);
                    resultEl.innerHTML = lines.map(l => `<div>${l}</div>`).join('');
                }
                showToast(data.updated ? '更新成功' : '已是最新版本');
                await loadUpdateInfo();
            } catch (e) {
                showToast(e.message || '更新失败', 'error');
                if (resultEl) {
                    resultEl.style.display = '';
                    resultEl.innerHTML = `<div style="color:var(--error);">${escapeHtml(e.message || '更新失败')}</div>`;
                }
            } finally {
                if (btn) { btn.disabled = false; btn.textContent = orig; }
                _recalcSettingsHeight();
            }
        }

        async function diagnoseUpdate() {
            const btn = document.getElementById('diagnoseUpdateBtn');
            const resultEl = document.getElementById('updateResult');
            const orig = btn ? btn.textContent : '';
            if (btn) { btn.disabled = true; btn.textContent = '诊断中...'; }
            try {
                const data = await apiRequest(`${API_BASE}/update/diagnose`, {}, '诊断失败');
                if (resultEl) {
                    resultEl.style.display = '';
                    resultEl.innerHTML = `<div><strong>更新诊断</strong></div><pre style="white-space:pre-wrap;word-break:break-all;margin:8px 0 0;font-size:12px;">${escapeHtml(JSON.stringify(data, null, 2))}</pre>`;
                }
            } catch (e) {
                showToast(e.message || '诊断失败', 'error');
                if (resultEl) { resultEl.style.display = ''; resultEl.innerHTML = `<div style="color:var(--error);">${escapeHtml(e.message || '诊断失败')}</div>`; }
            } finally {
                if (btn) { btn.disabled = false; btn.textContent = orig; }
                _recalcSettingsHeight();
            }
        }

        async function restartWebService() {
            if (!confirm('确定重启 Web 服务吗？\ngunicorn 将热重载工作进程以加载最新代码，期间可能短暂中断。')) return;
            const btn = document.getElementById('restartServiceBtn');
            const orig = btn ? btn.textContent : '';
            if (btn) { btn.disabled = true; btn.textContent = '重启中...'; }
            try {
                const data = await apiRequest(`${API_BASE}/service/restart`, { method: 'POST' }, '重启失败');
                if (data.success) {
                    showToast(data.message || '已触发热重载，页面即将刷新');
                    // 等待 gunicorn 完成 worker 替换后刷新页面
                    setTimeout(() => { location.reload(); }, 5000);
                } else {
                    showToast(data.message || '未能自动重启', 'error');
                }
            } catch (e) {
                showToast(e.message || '重启失败', 'error');
            } finally {
                if (btn) { btn.disabled = false; btn.textContent = orig; }
            }
        }

        function initSettingsCollapse() {
            ['douyin', 'x'].forEach(platform => {
                const expanded = localStorage.getItem(`settings-${platform}-expanded`) === '1';
                const bodyId = platform === 'douyin' ? 'dySettingsBody' : 'xSettingsBody';
                const toggleId = platform === 'douyin' ? 'dySettingsToggle' : 'xSettingsToggle';
                const body = document.getElementById(bodyId);
                const toggle = document.getElementById(toggleId);

                // 恢复设置子标签
                let savedTab = localStorage.getItem(`settings-${platform}-tab`) || (platform === 'douyin' ? 'all' : 'config');
                if (platform === 'douyin' && localStorage.getItem('settings-layout-version') !== '2') {
                    savedTab = 'all';
                    localStorage.setItem('settings-layout-version', '2');
                }
                if (platform === 'douyin' && savedTab === 'config') savedTab = 'all';
                switchSettingsTab(platform, savedTab);

                if (expanded && body && toggle) {
                    body.classList.remove('collapsed');
                    body.style.maxHeight = body.scrollHeight + 200 + 'px';
                    toggle.classList.add('expanded');
                    toggle.textContent = '▼ 折叠设置';
                }
            });
        }

        // ============ 工具函数 ============

        function escapeHtml(str) {
            return String(str ?? '')
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }

        function formatDateTime(value) {
            if (!value) return '时间未知';
            const date = new Date(value);
            if (Number.isNaN(date.getTime())) return '时间未知';
            return date.toLocaleString();
        }

        function formatIntervalSeconds(seconds) {
            const safeSeconds = Number(seconds || 0);
            if (!safeSeconds) return '未设置';
            if (safeSeconds % 3600 === 0) return `${safeSeconds / 3600} 小时`;
            if (safeSeconds >= 3600) return `${(safeSeconds / 3600).toFixed(1)} 小时`;
            if (safeSeconds >= 60) return `${Math.round(safeSeconds / 60)} 分钟`;
            return `${safeSeconds} 秒`;
        }

        function getXTaskStatusMeta(task) {
            const statusMap = {
                pending: { label: '等待中', cls: 'status-pending' },
                downloading: { label: '下载中', cls: 'status-downloading' },
                completed: { label: '已完成', cls: 'status-completed' },
                failed: { label: '失败', cls: 'status-failed' },
                cancelled: { label: '已取消', cls: 'status-failed' }
            };
            const phaseMap = {
                queued: '排队中',
                preparing: '准备中',
                running: '抓取中',
                finalizing: '整理结果',
                completed: '完成',
                failed: '失败',
                cancelled: '已取消'
            };
            const status = statusMap[task.status] || { label: task.status || '未知', cls: 'status-pending' };
            return {
                label: status.label,
                cls: status.cls,
                phaseLabel: phaseMap[task.phase] || task.phase || '等待中'
            };
        }

        function getXAuthorStatusMeta(author) {
            const code = author.account_status || 'active';
            const label = author.account_status_label || '正常';
            if (code === 'active') return { label, cls: '' };
            if (code === 'restricted') return { label, cls: ' is-risk' };
            return { label, cls: ' is-error' };
        }

        function toggleErrorDetail(taskId) {
            const el = document.getElementById(`err-${taskId}`);
            if (el) el.classList.toggle('show');
        }

        let activeErrorTooltipItem = null;
        let errorTooltipHideTimer = null;

        function clearErrorTooltipHideTimer() {
            if (errorTooltipHideTimer) {
                clearTimeout(errorTooltipHideTimer);
                errorTooltipHideTimer = null;
            }
        }

        function getErrorTooltipForItem(item) {
            const tooltip = item?._errorTooltip || item?.querySelector('.error-tooltip');
            if (tooltip && item) {
                item._errorTooltip = tooltip;
                tooltip.__ownerItem = item;
                if (!tooltip.__hoverBound) {
                    tooltip.addEventListener('mouseenter', clearErrorTooltipHideTimer);
                    tooltip.addEventListener('mouseleave', () => hideErrorTooltip(tooltip.__ownerItem));
                    tooltip.__hoverBound = true;
                }
            }
            return tooltip || null;
        }

        function positionErrorTooltip(item, tooltip) {
            const viewportPadding = 16;
            const offset = 12;
            tooltip.classList.remove('place-top', 'place-bottom');
            tooltip.style.left = `${viewportPadding}px`;
            tooltip.style.top = `${viewportPadding}px`;

            const anchor = item.querySelector('.error-toggle-btn') || item;
            const rect = anchor.getBoundingClientRect();
            const tooltipWidth = tooltip.offsetWidth || Math.min(520, window.innerWidth - viewportPadding * 2);
            const tooltipHeight = tooltip.offsetHeight || 0;
            const preferredLeft = anchor === item
                ? rect.left + Math.min(Math.max(rect.width * 0.12, 20), 72)
                : rect.left + rect.width / 2 - tooltipWidth / 2;
            const left = Math.min(
                Math.max(preferredLeft, viewportPadding),
                Math.max(viewportPadding, window.innerWidth - tooltipWidth - viewportPadding)
            );

            const canPlaceTop = rect.top >= tooltipHeight + offset + viewportPadding;
            const preferredTop = canPlaceTop
                ? rect.top - tooltipHeight - offset
                : rect.bottom + offset;
            const top = Math.min(
                Math.max(preferredTop, viewportPadding),
                Math.max(viewportPadding, window.innerHeight - tooltipHeight - viewportPadding)
            );

            tooltip.style.left = `${Math.round(left)}px`;
            tooltip.style.top = `${Math.round(top)}px`;
            tooltip.classList.add(canPlaceTop ? 'place-top' : 'place-bottom');
        }

        function showErrorTooltip(item) {
            if (window.matchMedia('(hover: none), (pointer: coarse)').matches) return;
            const tooltip = getErrorTooltipForItem(item);
            if (!tooltip) return;
            clearErrorTooltipHideTimer();
            if (activeErrorTooltipItem && activeErrorTooltipItem !== item) {
                hideErrorTooltip(activeErrorTooltipItem);
            }
            if (tooltip.parentElement !== document.body) {
                document.body.appendChild(tooltip);
            }
            activeErrorTooltipItem = item;
            tooltip.classList.add('show');
            positionErrorTooltip(item, tooltip);
        }

        function scheduleErrorTooltipHide(item = activeErrorTooltipItem) {
            clearErrorTooltipHideTimer();
            errorTooltipHideTimer = setTimeout(() => hideErrorTooltip(item), 120);
        }

        function hideErrorTooltip(item = activeErrorTooltipItem) {
            if (!item) return;
            clearErrorTooltipHideTimer();
            const tooltip = getErrorTooltipForItem(item);
            if (tooltip) {
                tooltip.classList.remove('show', 'place-top', 'place-bottom');
                tooltip.style.left = '';
                tooltip.style.top = '';
                if (item.isConnected && tooltip.parentElement === document.body) {
                    item.insertBefore(tooltip, item.firstChild);
                }
            }
            if (activeErrorTooltipItem === item) {
                activeErrorTooltipItem = null;
            }
        }

        function refreshErrorTooltipPosition() {
            if (!activeErrorTooltipItem) return;
            const tooltip = getErrorTooltipForItem(activeErrorTooltipItem);
            if (tooltip && tooltip.classList.contains('show')) {
                positionErrorTooltip(activeErrorTooltipItem, tooltip);
            }
        }

        if (!window.__errorTooltipViewportBound) {
            window.addEventListener('resize', refreshErrorTooltipPosition);
            window.addEventListener('scroll', () => hideErrorTooltip(), true);
            window.__errorTooltipViewportBound = true;
        }

        // ============ 主题管理 ============

        const THEME_KEY = 'theme-preference';
        const themeOrder = ['auto', 'light', 'dark'];
        const themeIcons = { auto: '🌗', light: '☀️', dark: '🌙' };

        function getSystemTheme() {
            return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
        }

        function applyTheme(pref) {
            const effective = pref === 'auto' ? getSystemTheme() : pref;
            document.documentElement.setAttribute('data-theme', effective);
            const btn = document.getElementById('themeToggle');
            if (btn) btn.textContent = themeIcons[pref] || '🌗';
        }

        function cycleTheme() {
            const current = localStorage.getItem(THEME_KEY) || 'auto';
            const idx = themeOrder.indexOf(current);
            const next = themeOrder[(idx + 1) % themeOrder.length];
            localStorage.setItem(THEME_KEY, next);
            applyTheme(next);
        }

        function initTheme() {
            const pref = localStorage.getItem(THEME_KEY) || 'auto';
            applyTheme(pref);
            window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
                const p = localStorage.getItem(THEME_KEY) || 'auto';
                if (p === 'auto') applyTheme('auto');
            });
        }

        // ============ 复制功能 ============

        function copyToClipboard(text, btn) {
            navigator.clipboard.writeText(text).then(() => {
                if (btn) {
                    const old = btn.textContent;
                    btn.textContent = '✅ 已复制';
                    btn.classList.add('copied');
                    setTimeout(() => { btn.textContent = old; btn.classList.remove('copied'); }, 1500);
                }
                showToast('已复制到剪贴板');
            }).catch(() => {
                // fallback
                const ta = document.createElement('textarea');
                ta.value = text;
                ta.style.cssText = 'position:fixed;left:-9999px;';
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
                showToast('已复制到剪贴板');
            });
        }

        function copyErrorMsg(taskId) {
            const el = document.getElementById(`errtip-${taskId}`) || document.querySelector(`#err-${taskId} .error-detail-mobile-body`);
            if (el) {
                const btn = event?.target?.closest('.copy-btn');
                copyToClipboard(el.textContent, btn);
            }
        }

        function copyAllFailedErrors() {
            if (window._currentPageFailedErrors?.length) {
                const btn = event?.target?.closest('.copy-btn');
                copyToClipboard(window._currentPageFailedErrors.join('\n\n'), btn);
            }
        }

        async function copyAllFailedErrorsFromServer() {
            const btn = event?.target?.closest('.copy-btn');
            try {
                if (btn) { btn.textContent = '⏳ 加载中...'; btn.disabled = true; }
                const res = await apiFetch(`${API_BASE}/tasks/all-failed-errors`);
                const data = await res.json();
                if (res.ok && data.data?.errors?.length) {
                    copyToClipboard(data.data.errors.join('\n\n'), btn);
                } else {
                    showToast('没有失败任务的错误记录');
                    if (btn) { btn.textContent = '📋 复制所有失败原因'; btn.disabled = false; }
                }
            } catch (e) {
                showToast('获取失败', 'error');
                if (btn) { btn.textContent = '📋 复制所有失败原因'; btn.disabled = false; }
            }
        }

        async function deleteAllByStatus() {
            const statusName = { failed: '失败', cancelled: '已取消', paused: '已暂停', completed: '已完成', pending: '待处理', downloading: '下载中' };
            const status = currentStatusFilter;
            if (!status) { showToast('请先切换到具体状态页面再进行全部删除', 'error'); return; }
            const name = statusName[status] || status;
            if (!confirm(`确定要删除所有【${name}】状态的任务吗？此操作不可恢复！`)) return;
            try {
                const res = await apiFetch(`${API_BASE}/tasks/batch-delete?status=${status}`, { method: 'POST' });
                const data = await res.json();
                if (res.ok) {
                    const count = data.data?.count || 0;
                    showToast(count > 0 ? `已删除 ${count} 个${name}任务` : `没有${name}任务`);
                    refreshTasks();
                } else {
                    const errMsg = typeof data.detail === 'string' ? data.detail : (data.message || '删除失败');
                    showToast(errMsg, 'error');
                }
            } catch (e) {
                showToast('删除失败', 'error');
            }
        }

        // ============ 全部暂停 ============

        async function pauseAllTasks() {
            if (!confirm('确定要暂停所有下载中和等待中的任务吗？')) return;
            try {
                const res = await apiFetch(`${API_BASE}/tasks/pause-all`, { method: 'POST' });
                const data = await res.json();
                if (res.ok) {
                    const count = data.data?.count || 0;
                    showToast(count > 0 ? `已暂停 ${count} 个任务` : '没有需要暂停的任务');
                    refreshTasks();
                } else {
                    showToast(data.detail || '操作失败', 'error');
                }
            } catch (e) {
                showToast('操作失败', 'error');
            }
        }

        // ============ 刷新链接重试 ============

        async function refreshRetryTask(taskId) {
            if (!confirm('确定要重新获取下载链接并重试此任务吗？')) return;
            try {
                const res = await apiFetch(`${API_BASE}/tasks/refresh-retry/${taskId}`, { method: 'POST' });
                const data = await res.json();
                if (res.ok) {
                    showToast(data.message || '已刷新链接并重新提交');
                    refreshTasks();
                } else {
                    showToast(data.detail || '操作失败', 'error');
                }
            } catch (e) {
                showToast('操作失败', 'error');
            }
        }

        async function refreshRetryAllFailed() {
            if (!confirm('确定要重新获取所有失败任务的下载链接并重试吗？这可能需要一些时间。')) return;
            try {
                const res = await apiFetch(`${API_BASE}/tasks/refresh-retry-all-failed`, { method: 'POST' });
                const data = await res.json();
                if (res.ok) {
                    showToast(data.message || '已刷新链接并重新提交');
                    refreshTasks();
                } else {
                    showToast(data.detail || '操作失败', 'error');
                }
            } catch (e) {
                showToast('操作失败', 'error');
            }
        }

        // ============ 批量强制重试卡住的下载任务 ============

        async function forceRetryAllDownloading() {
            if (!confirm('确定要强制重试所有卡住的下载中任务吗？这将重置它们的状态和进度。')) return;
            try {
                const res = await apiFetch(`${API_BASE}/tasks/force-retry-all-downloading`, { method: 'POST' });
                const data = await res.json();
                if (res.ok) {
                    const count = data.data?.count || 0;
                    if (count > 0) {
                        showToast(`已强制重新提交 ${count} 个下载中任务`);
                    } else {
                        showToast('没有卡住的下载中任务');
                    }
                    refreshTasks();
                } else {
                    showToast(data.detail || '操作失败', 'error');
                }
            } catch (e) {
                showToast('操作失败', 'error');
            }
        }

        // ============ 服务管理 ============

        async function loadProcessStatus() {
            try {
                const res = await apiFetch(`${API_BASE}/process/status`);
                const data = await res.json();
                if (!res.ok) {
                    throw new Error(getApiMessage(data, '获取进程状态失败'));
                }
                // Worker
                const wDot = document.getElementById('workerStatusDot');
                const wText = document.getElementById('workerStatusText');
                const wPid = document.getElementById('workerPidText');
                if (data.worker?.running) {
                    wDot.style.background = 'var(--success)';
                    wText.textContent = '运行中';
                    wPid.textContent = `PID: ${data.worker.pid}`;
                } else {
                    wDot.style.background = 'var(--error)';
                    wText.textContent = '已停止';
                    wPid.textContent = '';
                }
                // Beat
                const bDot = document.getElementById('beatStatusDot');
                const bText = document.getElementById('beatStatusText');
                const bPid = document.getElementById('beatPidText');
                if (data.beat?.running) {
                    bDot.style.background = 'var(--success)';
                    bText.textContent = '运行中';
                    bPid.textContent = `PID: ${data.beat.pid}`;
                } else {
                    bDot.style.background = 'var(--error)';
                    bText.textContent = '已停止';
                    bPid.textContent = '';
                }
                // Concurrency slider
                if (data.worker?.concurrency) {
                    document.getElementById('concurrencySlider').value = data.worker.concurrency;
                    document.getElementById('concurrencyValue').textContent = data.worker.concurrency;
                }
            } catch (e) {
                // ignore
            }
        }

        async function controlProcess(type, action) {
            const btn = document.getElementById(type === 'worker' ? (action === 'start' ? 'workerStartBtn' : 'workerStopBtn') : (action === 'start' ? 'beatStartBtn' : 'beatStopBtn'));
            const oldText = btn.textContent;
            btn.disabled = true;
            btn.textContent = '处理中...';
            try {
                const res = await apiFetch(`${API_BASE}/process/${type}/${action}`, { method: 'POST' });
                const data = await res.json();
                showToast(data.message || (data.success ? '操作成功' : '操作失败'), data.success ? 'success' : 'error');
                // 刷新状态
                setTimeout(loadProcessStatus, 1000);
                setTimeout(fetchStatus, 2000);
            } catch (e) {
                showToast('操作失败', 'error');
            } finally {
                btn.disabled = false;
                btn.textContent = oldText;
            }
        }

        async function saveConcurrency() {
            const val = parseInt(document.getElementById('concurrencySlider').value);
            if (isNaN(val) || val < 1 || val > 20) { showToast('请选择 1～20 之间的数值', 'error'); return; }
            if (!confirm(`确定将最大同时下载数设为 ${val} 吗？这会重启 Worker。`)) return;
            try {
                const res = await apiFetch(`${API_BASE}/process/concurrency`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ concurrency: val })
                });
                const data = await res.json();
                showToast(data.message || '已更新');
                setTimeout(loadProcessStatus, 2000);
                setTimeout(fetchStatus, 3000);
            } catch (e) {
                showToast('操作失败', 'error');
            }
        }

        // ============ 数据库配置 ============

        function onDbTypeChange() {
            const t = document.getElementById('dbType').value;
            if (t === 'postgresql') {
                document.getElementById('dbPort').placeholder = '5432';
                document.getElementById('dbUser').placeholder = 'postgres';
            } else if (t === 'mysql') {
                document.getElementById('dbPort').placeholder = '3306';
                document.getElementById('dbUser').placeholder = 'root';
            }
        }

        async function testDbConnection() {
            const cfg = getDbFormValues();
            if (!cfg.db_host || !cfg.db_name) { showToast('请填写主机和数据库名', 'error'); return; }
            try {
                const res = await apiFetch(`${API_BASE}/config/database/test`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(cfg)
                });
                const data = await res.json();
                if (res.ok && data.success) {
                    showToast('数据库连接成功');
                } else {
                    showToast(data.message || data.detail || '连接失败', 'error');
                }
            } catch (e) {
                showToast('测试请求失败', 'error');
            }
        }

        async function saveDbConfig() {
            const cfg = getDbFormValues();
            if (!cfg.db_host || !cfg.db_name) {
                showToast('请填写完整的数据库配置', 'error'); return;
            }
            if (!confirm('保存后需要重启服务才能生效，确定继续？')) return;
            try {
                const res = await apiFetch(`${API_BASE}/config/database`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(cfg)
                });
                const data = await res.json();
                if (res.ok && data.success) {
                    showToast('数据库配置已保存，请重启服务');
                } else {
                    showToast(data.message || data.detail || '保存失败', 'error');
                }
            } catch (e) {
                showToast('保存请求失败', 'error');
            }
        }

        function getDbFormValues() {
            return {
                db_type: document.getElementById('dbType').value,
                db_host: document.getElementById('dbHost').value.trim(),
                db_port: parseInt(document.getElementById('dbPort').value) || 0,
                db_user: document.getElementById('dbUser').value.trim(),
                db_password: document.getElementById('dbPassword').value,
                db_name: document.getElementById('dbName').value.trim()
            };
        }

        async function loadDbConfig() {
            try {
                const res = await apiFetch(`${API_BASE}/config/database`);
                const data = await res.json();
                if (!res.ok) {
                    throw new Error(getApiMessage(data, '加载数据库配置失败'));
                }
                if (data.db_type) {
                    setFilterSelectValue('dbType', data.db_type);
                    onDbTypeChange();
                    document.getElementById('dbHost').value = data.db_host || '';
                    document.getElementById('dbPort').value = data.db_port || '';
                    document.getElementById('dbUser').value = data.db_user || '';
                    document.getElementById('dbName').value = data.db_name || '';
                }
            } catch (e) {
                // 配置接口可能还不可用
            }
        }


        function renderBootstrapPanel(status) {
            bootstrapMode = !status.ready;
            let panel = document.getElementById('bootstrapPanel');
            if (!panel) {
                panel = document.createElement('div');
                panel.id = 'bootstrapPanel';
                panel.className = 'bootstrap-panel';
                const main = document.querySelector('.app-main');
                if (main) main.prepend(panel);
            }
            if (status.ready) { panel.style.display = 'none'; return; }

            const missing = (status.missing || [])
                .map(i => `<span class="status-tag status-failed">${escapeHtml(i.label || i.key)} (${escapeHtml(i.key)})</span>`)
                .join('');
            const errors = (status.errors || [])
                .map(i => `<div class="bootstrap-error-item"><strong>${escapeHtml(i.label || i.key)}</strong><span>${escapeHtml(i.message || '连接或配置检查失败')}</span></div>`)
                .join('');
            const fields = status.fields || [];
            const values = status.values || {};
            const groups = fields.reduce((acc, f) => { (acc[f.group] ||= []).push(f); return acc; }, {});
            const forms = Object.entries(groups).map(([group, items]) => `
                <div class="bootstrap-group"><h4>${escapeHtml(group)}</h4><div class="bootstrap-grid">
                    ${items.map(f => {
                        const current = values[f.key]?.value ?? f.default ?? '';
                        const type = f.secret ? 'password' : 'text';
                        return `<label class="bootstrap-field"><span>${escapeHtml(f.label)}${f.required ? ' *' : ''}</span><input data-env-key="${escapeHtml(f.key)}" type="${type}" value="${escapeHtml(current)}" placeholder="${escapeHtml(f.default || '')}" autocomplete="off"></label>`;
                    }).join('')}
                </div></div>`).join('');
            panel.innerHTML = `
                <div class="bootstrap-head"><div><h2>配置维护模式</h2><p>项目配置未通过检查，当前网页端已正常启动，但下载、订阅、任务管理等业务功能暂不运行。请在此页修改配置，保存后重启 Web 服务。</p></div><button class="secondary" onclick="restartWebService()">重启 Web 服务</button></div>
                <div class="bootstrap-missing"><strong>缺失配置：</strong>${missing || '<span class="status-tag status-completed">无</span>'}</div>
                ${errors ? `<div class="bootstrap-errors"><strong>异常情况：</strong>${errors}</div>` : ''}
                ${forms}
                <div class="settings-actions"><button onclick="saveBootstrapConfig()">保存配置</button><button class="secondary" onclick="loadBootstrapStatus()">重新检测</button></div>
                <p class="form-hint">密码类字段会脱敏显示；保留 ******** 保存时不会覆盖已有密钥。数据库和 Redis 这类基础配置修改后，需要重启 Web 服务才会让正式功能恢复。</p>`;
            panel.style.display = '';
            switchPlatform('douyin');
            switchSubTab('settings');
            switchSettingsTab('douyin', 'database');
            _recalcSettingsHeight();
        }

        async function loadBootstrapStatus() {
            const status = await apiRequest(`${API_BASE}/bootstrap/status`, {}, '读取初始化状态失败');
            renderBootstrapPanel(status);
            return status;
        }

        async function saveBootstrapConfig() {
            const values = {};
            document.querySelectorAll('#bootstrapPanel [data-env-key]').forEach(input => { values[input.dataset.envKey] = input.value; });
            try {
                const data = await apiRequest(`${API_BASE}/bootstrap/config`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ values }) }, '保存初始化配置失败');
                showToast(data.message || '配置已保存');
                renderBootstrapPanel(data);
            } catch (e) { showToast(e.message || '保存初始化配置失败', 'error'); }
        }

        // ============ 初始化 ============

        async function init() {
            initTheme();
            initFilterSelects();
            initSettingsCollapse();
            try {
                const bootstrap = await loadBootstrapStatus();
                if (!bootstrap.ready) {
                    document.getElementById('statusDot').style.background = 'var(--warning)';
                    document.getElementById('statusText').textContent = '等待初始化配置';
                    return;
                }
            } catch (e) { console.error(e); }
            fetchStatus();
            switchPlatform('douyin');
            loadRuntimeConfig();
            loadCompleteConfig();
            loadDbConfig();
            loadProcessStatus();
            pollTimer = setInterval(() => {
                if (currentPlatform === 'douyin') refreshTasks();
                else fetchXTasks();
            }, pollInterval);
        }

        // ============ 活动日志查看器 ============
        let logPollingTimer = null;

        function getLogSettings() {
            const enabled = localStorage.getItem('logEnabled') === 'true';
            let levels;
            try {
                levels = JSON.parse(localStorage.getItem('logLevels') || '["info","warning","error"]');
            } catch { levels = ["info", "warning", "error"]; }
            return { enabled, levels };
        }

        // ============ Celery 诊断模块 ============

        async function runCeleryTest() {
            const btn = document.getElementById('celeryTestBtn');
            const box = document.getElementById('diagResult');
            btn.disabled = true; btn.textContent = '⏳ 测试中(最多15秒)...';
            box.innerHTML = '<p style="color:var(--text-secondary);text-align:center;">正在提交测试任务并等待 Worker 执行...</p>';
            try {
                const res = await apiFetch(`${API_BASE}/celery-test`, { method: 'POST' });
                const data = await res.json();
                const color = data.success ? 'var(--success)' : 'var(--error)';
                box.innerHTML = `
                    <div style="font-size:16px;font-weight:600;color:${color};margin-bottom:12px;">${data.message}</div>
                    <pre style="font-size:12px;white-space:pre-wrap;word-break:break-all;color:var(--text-secondary);margin:0;">${JSON.stringify(data, null, 2)}</pre>
                `;
                if (data.success) fetchLogs();
            } catch (e) {
                box.innerHTML = `<div style="color:var(--error);">请求失败: ${e.message}</div>`;
            }
            btn.disabled = false; btn.textContent = '🧪 运行测试任务';
        }

        async function runCeleryDebug() {
            const btn = document.getElementById('celeryDebugBtn');
            const box = document.getElementById('diagResult');
            btn.disabled = true; btn.textContent = '⏳ 诊断中...';
            box.innerHTML = '<p style="color:var(--text-secondary);text-align:center;">正在收集 Celery 诊断信息...</p>';
            try {
                const res = await apiFetch(`${API_BASE}/celery-debug`);
                const data = await res.json();

                let html = '<div style="font-size:14px;font-weight:600;margin-bottom:12px;">🔍 诊断报告</div>';

                // 队列长度
                html += '<div style="margin-bottom:12px;"><b>Redis 队列积压:</b><br>';
                const ql = data.queue_lengths || {};
                for (const [q, len] of Object.entries(ql)) {
                    const warn = (q !== 'celery' && len > 0) ? ' ⚠️ 旧队列有积压!' : '';
                    const color = len > 0 ? (q === 'celery' ? 'var(--text-primary)' : 'var(--error)') : 'var(--text-secondary)';
                    html += `<span style="display:inline-block;margin-right:16px;color:${color};">${q}: <b>${len}</b>${warn}</span>`;
                }
                html += '</div>';

                // Worker 状态
                const ping = data.ping || {};
                const workers = Object.keys(ping);
                const workerColor = workers.length > 0 ? 'var(--success)' : 'var(--error)';
                html += `<div style="margin-bottom:12px;"><b>Worker:</b> <span style="color:${workerColor}">${workers.length > 0 ? workers.join(', ') : '❌ 无在线 Worker'}</span></div>`;

                // Worker 监听的队列
                const aq = data.active_queues || {};
                for (const [w, queues] of Object.entries(aq)) {
                    html += `<div style="margin-bottom:8px;"><b>${w} 监听队列:</b> ${queues.join(', ')}</div>`;
                }

                // 已注册任务
                const reg = data.registered || {};
                for (const [w, tasks] of Object.entries(reg)) {
                    html += `<div style="margin-bottom:8px;"><b>${w} 注册任务 (${tasks.length}):</b><br>`;
                    html += `<span style="font-size:11px;color:var(--text-secondary);">${tasks.join(', ')}</span></div>`;
                }

                // 活跃任务和预留任务
                const active = data.active_tasks || {};
                const reserved = data.reserved_tasks || {};
                for (const [w, tasks] of Object.entries(active)) {
                    html += `<div style="margin-bottom:8px;"><b>${w} 活跃任务:</b> ${tasks.length > 0 ? tasks.map(t => t.name).join(', ') : '无'}</div>`;
                }
                for (const [w, tasks] of Object.entries(reserved)) {
                    html += `<div style="margin-bottom:8px;"><b>${w} 预留任务:</b> ${tasks.length > 0 ? tasks.map(t => t.name).join(', ') : '无'}</div>`;
                }

                // Celery 队列 peek
                if (data.celery_queue_peek && typeof data.celery_queue_peek === 'object') {
                    html += `<div style="margin-bottom:8px;"><b>队列中第一个任务:</b> ${data.celery_queue_peek.headers_task} (id: ${data.celery_queue_peek.headers_id})</div>`;
                }

                // 配置
                html += '<div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border);">';
                html += `<b>配置:</b> <pre style="font-size:11px;white-space:pre-wrap;margin:4px 0 0 0;color:var(--text-secondary);">${JSON.stringify(data.config, null, 2)}</pre>`;
                html += '</div>';

                box.innerHTML = html;
            } catch (e) {
                box.innerHTML = `<div style="color:var(--error);">诊断请求失败: ${e.message}</div>`;
            }
            btn.disabled = false; btn.textContent = '🔍 深度诊断';
        }

        async function purgeOldQueues() {
            if (!confirm('确认清除 download/scheduler 旧队列中的积压任务？')) return;
            const box = document.getElementById('diagResult');
            try {
                const res = await apiFetch(`${API_BASE}/celery-purge-old`, { method: 'POST' });
                const data = await res.json();
                box.innerHTML = `<div style="color:var(--success);">✅ 旧队列已清除: download=${data.purged?.download || 0}, scheduler=${data.purged?.scheduler || 0}</div>`;
            } catch (e) {
                box.innerHTML = `<div style="color:var(--error);">清除失败: ${e.message}</div>`;
            }
        }

        async function viewWorkerLog() {
            const box = document.getElementById('diagResult');
            box.innerHTML = '<p style="color:var(--text-secondary);text-align:center;">正在读取 Worker 日志...</p>';
            try {
                const res = await apiFetch(`${API_BASE}/worker-log?lines=80`);
                const data = await res.json();
                if (data.lines && data.lines.length > 0) {
                    box.innerHTML = `<div style="margin-bottom:8px;"><b>Worker 日志</b> (最后 ${data.lines.length} 行，共 ${data.total_lines} 行)</div>
                        <pre style="font-size:11px;white-space:pre-wrap;word-break:break-all;color:var(--text-secondary);margin:0;max-height:400px;overflow-y:auto;">${data.lines.map(l => l.replace(/</g,'&lt;')).join('\n')}</pre>`;
                } else {
                    box.innerHTML = `<div style="color:var(--text-secondary);">${data.message || '日志为空'}</div>`;
                }
            } catch (e) {
                box.innerHTML = `<div style="color:var(--error);">读取日志失败: ${e.message}</div>`;
            }
        }

        function initLogViewer() {
            const s = getLogSettings();
            const toggle = document.getElementById('logEnabled');
            if (toggle) toggle.checked = s.enabled;
            document.querySelectorAll('.log-filter-bar input[data-log-level]').forEach(cb => {
                cb.checked = s.levels.includes(cb.dataset.logLevel);
            });
            if (s.enabled) startLogPolling();
        }

        function toggleLogViewer() {
            const enabled = document.getElementById('logEnabled').checked;
            localStorage.setItem('logEnabled', enabled);
            if (enabled) { fetchLogs(); startLogPolling(); }
            else stopLogPolling();
        }

        function onLogFilterChange() {
            const levels = [];
            document.querySelectorAll('.log-filter-bar input[data-log-level]').forEach(cb => {
                if (cb.checked) levels.push(cb.dataset.logLevel);
            });
            localStorage.setItem('logLevels', JSON.stringify(levels));
            renderLogList();
        }

        let _rawLogs = [];

        async function fetchLogs() {
            try {
                const res = await apiFetch(`${API_BASE}/logs?count=200`);
                if (!res.ok) { console.error('获取日志失败:', res.status); return; }
                const data = await res.json();
                _rawLogs = data.logs || [];
                renderLogList();
            } catch (e) {
                console.error('获取日志失败:', e);
            }
        }

        function renderLogList() {
            const container = document.getElementById('logList');
            if (!container) return;
            const s = getLogSettings();
            const filtered = _rawLogs.filter(l => s.levels.includes(l.level));
            if (!filtered.length) {
                container.innerHTML = '<div style="color:var(--text-secondary);text-align:center;padding:24px;">暂无日志</div>';
                return;
            }
            container.innerHTML = filtered.map(l => {
                const d = new Date(l.ts * 1000);
                const time = d.toLocaleString('zh-CN', { hour12: false });
                const detail = l.detail ? `<span class="log-detail">${escapeHtml(l.detail)}</span>` : '';
                return `<div class="log-entry level-${l.level}"><span class="log-time">${time}</span><span class="log-source">[${l.source}]</span>${escapeHtml(l.msg)}${detail}</div>`;
            }).join('');
        }

        async function clearLogs() {
            try {
                const res = await apiFetch(`${API_BASE}/logs`, { method: 'DELETE' });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                _rawLogs = [];
                renderLogList();
                showToast('日志已清空');
            } catch (e) {
                showToast('清空失败', 'error');
            }
        }

        function copyLogs() {
            const container = document.getElementById('logList');
            if (!container || !_rawLogs.length) {
                showToast('没有日志可以复制', 'error');
                return;
            }
            const s = getLogSettings();
            const filtered = _rawLogs.filter(l => s.levels.includes(l.level));
            const text = filtered.map(l => {
                const d = new Date(l.ts * 1000);
                const time = d.toLocaleString('zh-CN', { hour12: false });
                const detail = l.detail ? ` | ${l.detail}` : '';
                return `[${time}] [${l.level.toUpperCase()}] [${l.source}] ${l.msg}${detail}`;
            }).join('\n');
            copyToClipboard(text);
        }

        function startLogPolling() {
            stopLogPolling();
            logPollingTimer = setInterval(fetchLogs, 5000);
        }

        function stopLogPolling() {
            if (logPollingTimer) { clearInterval(logPollingTimer); logPollingTimer = null; }
        }

        init().then(() => {
            if (!bootstrapMode) initLogViewer();
        });
