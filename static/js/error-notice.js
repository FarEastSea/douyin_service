(function () {
    'use strict';

    const originalFetch = window.fetch.bind(window);
    let lastFingerprint = '';
    let lastShownAt = 0;

    const statusMessages = {
        400: ['请求参数不正确', '请核对输入内容和必填项后重试。'],
        401: ['身份验证失败', '请更新账号凭据或 Cookie 后重试。'],
        403: ['当前操作没有权限', '请检查账号权限、Cookie 状态或文件访问权限。'],
        404: ['请求的资源不存在', '请刷新页面确认数据是否仍然存在。'],
        409: ['数据状态存在冲突', '请刷新页面获取最新状态，确认后再重试。'],
        422: ['请求参数校验失败', '请根据参数详情修正输入内容后重试。'],
        429: ['请求过于频繁', '请降低操作频率，等待一段时间后重试。'],
        500: ['服务器处理请求时发生异常', '请复制错误编号，并在服务端错误日志中检索。'],
        502: ['上游服务响应异常', '请检查网络、反向代理和上游平台状态，稍后重试。'],
        503: ['服务暂时不可用', '请检查数据库、Redis 和后台任务进程是否正常。'],
        504: ['上游服务响应超时', '请检查网络和上游服务状态，稍后重试。']
    };

    function plainText(value) {
        return String(value || '')
            .replace(/<script[\s\S]*?<\/script>/gi, ' ')
            .replace(/<style[\s\S]*?<\/style>/gi, ' ')
            .replace(/<[^>]+>/g, ' ')
            .replace(/\s+/g, ' ')
            .trim();
    }

    function normalizeMessage(value, status) {
        const text = plainText(value);
        const translations = {
            'Internal Server Error': '服务器处理请求时发生异常',
            'Bad Gateway': '上游服务响应异常',
            'Service Unavailable': '服务暂时不可用',
            'Gateway Timeout': '上游服务响应超时',
            'Not Found': '请求的资源不存在'
        };
        return translations[text] || text || (statusMessages[status] || ['请求处理失败'])[0];
    }

    async function parseResponse(response) {
        const text = await response.clone().text();
        if (!text) return {};
        try {
            return JSON.parse(text);
        } catch (_) {
            return { message: text };
        }
    }

    function endpointOf(input) {
        const raw = typeof input === 'string' ? input : (input && input.url) || '';
        try {
            const url = new URL(raw, window.location.origin);
            return url.pathname;
        } catch (_) {
            return String(raw).split('?')[0];
        }
    }

    function formatDetails(details) {
        if (!Array.isArray(details) || !details.length) return '';
        return details.map(item => {
            const field = item && item.field ? `参数 ${item.field}` : '参数';
            const message = item && item.message ? item.message : '输入内容不符合要求';
            return `${field}：${message}`;
        }).join('；');
    }

    function ensurePanel() {
        let panel = document.getElementById('globalErrorNotice');
        if (panel) return panel;
        panel = document.createElement('section');
        panel.id = 'globalErrorNotice';
        panel.className = 'global-error-notice';
        panel.setAttribute('role', 'alert');
        panel.setAttribute('aria-live', 'assertive');
        document.body.appendChild(panel);
        return panel;
    }

    function diagnosticText(info) {
        return [
            `错误说明：${info.message}`,
            `处理建议：${info.suggestion}`,
            `错误编号：${info.errorId || '无（可能未到达应用服务）'}`,
            `状态码：${info.status || '网络错误'}`,
            `错误类型：${info.errorType || '未知'}`,
            `请求位置：${info.endpoint || '未知'}`,
            `发生时间：${info.time}`,
            info.details ? `参数详情：${info.details}` : ''
        ].filter(Boolean).join('\n');
    }

    function showErrorNotice(info) {
        const fingerprint = `${info.errorId}|${info.status}|${info.endpoint}|${info.message}`;
        const now = Date.now();
        if (fingerprint === lastFingerprint && now - lastShownAt < 30000) return;
        lastFingerprint = fingerprint;
        lastShownAt = now;

        const panel = ensurePanel();
        panel.innerHTML = '';

        const heading = document.createElement('div');
        heading.className = 'global-error-heading';
        const title = document.createElement('strong');
        title.textContent = '操作未完成';
        const close = document.createElement('button');
        close.type = 'button';
        close.className = 'global-error-close';
        close.textContent = '关闭';
        close.addEventListener('click', () => panel.classList.remove('show'));
        heading.append(title, close);

        const message = document.createElement('div');
        message.className = 'global-error-message';
        message.textContent = info.message;

        const suggestion = document.createElement('div');
        suggestion.className = 'global-error-suggestion';
        suggestion.textContent = `建议：${info.suggestion}`;

        const meta = document.createElement('div');
        meta.className = 'global-error-meta';
        meta.textContent = [
            info.errorId ? `错误编号 ${info.errorId}` : '',
            info.status ? `状态码 ${info.status}` : '网络连接失败',
            info.endpoint ? `位置 ${info.endpoint}` : '',
            info.errorType ? `类型 ${info.errorType}` : ''
        ].filter(Boolean).join(' · ');

        const actions = document.createElement('div');
        actions.className = 'global-error-actions';
        const copy = document.createElement('button');
        copy.type = 'button';
        copy.textContent = '复制排障信息';
        copy.addEventListener('click', async () => {
            try {
                await navigator.clipboard.writeText(diagnosticText(info));
                copy.textContent = '已复制';
                setTimeout(() => { copy.textContent = '复制排障信息'; }, 1500);
            } catch (_) {
                copy.textContent = '复制失败，请手动记录错误编号';
            }
        });
        actions.appendChild(copy);

        if (info.details) {
            const details = document.createElement('div');
            details.className = 'global-error-details';
            details.textContent = `参数详情：${info.details}`;
            panel.append(heading, message, suggestion, details, meta, actions);
        } else {
            panel.append(heading, message, suggestion, meta, actions);
        }
        panel.classList.add('show');
    }

    window.fetch = async function monitoredFetch(input, init) {
        const endpoint = endpointOf(input);
        try {
            const response = await originalFetch(input, init);
            if (!response.ok && endpoint.startsWith('/api/')) {
                const payload = await parseResponse(response);
                const defaults = statusMessages[response.status] || ['请求处理失败', '请稍后重试；若问题持续，请记录排障信息。'];
                showErrorNotice({
                    message: normalizeMessage(payload.message || payload.detail, response.status) || defaults[0],
                    suggestion: plainText(payload.suggestion) || defaults[1],
                    errorId: plainText(payload.error_id || response.headers.get('X-Error-ID')),
                    errorType: plainText(payload.error_type || payload.code),
                    status: response.status,
                    endpoint,
                    details: formatDetails(payload.details),
                    time: new Date().toLocaleString('zh-CN', { hour12: false })
                });
            }
            return response;
        } catch (error) {
            if (endpoint.startsWith('/api/')) {
                showErrorNotice({
                    message: '无法连接到应用服务',
                    suggestion: '请确认 Web 服务正在运行，并检查网络、反向代理和浏览器控制台。',
                    errorId: '',
                    errorType: error && error.name ? error.name : '网络错误',
                    status: '',
                    endpoint,
                    details: '',
                    time: new Date().toLocaleString('zh-CN', { hour12: false })
                });
            }
            throw error;
        }
    };
})();
