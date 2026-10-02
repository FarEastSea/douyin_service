// Presentation only: recovery recommendations come from structured server codes.
export function issuePresentation(message = '', code = '') {
  const known: Record<string, [string, string]> = {
    cookie_invalid: ['账号登录状态需要检查', '前往对应平台账号页核对凭据。'],
    browser_identity_missing: ['浏览器请求上下文不完整', '核对账号页中的浏览器身份与 User-Agent。'],
    rate_limited: ['请求受到限流', '等待保护性冷却结束后再检查。'],
    network_error: ['网络请求未完成', '检查连接后重试，已有结果会保留。'],
    signature_rejected: ['采集请求签名被拒绝', '查看请求诊断，核对服务端签名实现。'],
  }
  const technical = /Traceback|(?:\w+Error|Exception):|SQL:|\[SQL|INSERT INTO|SELECT .* FROM|psycopg|<html|<!DOCTYPE|\n.{80}/i.test(message)
  const entry = known[code]
  return {
    title: entry?.[0] || (technical ? '操作未完成，诊断中包含技术错误' : message.trim().split('\n')[0]?.slice(0, 110) || '操作未完成'),
    nextAction: entry?.[1] || '展开诊断核对原因；需要协助时复制完整信息。',
    details: message,
  }
}

export function reportSummary(report: { status?: string; checked_authors?: number; remaining_authors?: number; new_works?: number; summary?: string }) {
  if (['failed', 'interrupted'].includes(report.status || '')) return '本轮检查未完成，已保留检查结果与诊断。'
  return `本轮已检查 ${report.checked_authors ?? '—'} 位 · 新作品 ${report.new_works ?? '—'} 个 · 待续检 ${report.remaining_authors ?? '—'} 位`
}
