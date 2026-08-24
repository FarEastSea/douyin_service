const REPORT_STATUS: Record<string, string> = {
  running: '执行中',
  completed: '已完成',
  partial_rate_limited: '请求受限，等待续检',
  partial_timeout: '本轮超时，等待续检',
  partial_authentication: 'Cookie 身份信息缺失',
  failed: '异常终止',
  interrupted: '任务中断',
  skipped: '已跳过',
}

export function reportStatusLabel(status?: string | null) {
  return REPORT_STATUS[String(status || '')] || '未知状态'
}

export function riskTypeLabel(code?: string | null, serverLabel?: string | null) {
  if (serverLabel) return serverLabel
  return ({
    browser_identity_missing: '浏览器身份信息缺失',
    argus_blocked: '抖音安全校验拦截',
    rate_limited: '抖音请求频率受限',
  } as Record<string, string>)[String(code || '')] || '抖音请求保护'
}

export function riskReasonLabel(code?: string | null, reason?: string | null, serverLabel?: string | null) {
  if (serverLabel) return serverLabel
  const text = String(reason || '')
  if (code === 'browser_identity_missing' || /uifid not found/i.test(text)) {
    return '请求缺少或未识别 UIFID 浏览器身份标识，抖音安全校验拒绝了本次请求。'
  }
  if (code === 'argus_blocked' || /argussecurityplugin/i.test(text)) return '抖音安全校验拒绝了本次请求。'
  if (code === 'rate_limited') return '抖音判定请求过于频繁，系统已暂停新的业务请求。'
  return text || '抖音上游安全校验拒绝了本次请求。'
}
