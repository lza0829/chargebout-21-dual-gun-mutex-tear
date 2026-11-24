function qs(params) {
  const search = new URLSearchParams()
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') search.set(k, String(v))
  })
  const s = search.toString()
  return s ? `?${s}` : ''
}

async function req(path, opts = {}) {
  const r = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  const text = await r.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = { detail: text }
  }
  if (!r.ok) {
    const detail = data?.detail
    const msg =
      (typeof detail === 'object' && detail && (detail.拒因 || detail.消息)) ||
      detail ||
      data?.消息 ||
      text ||
      '请求失败'
    const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
    if (typeof detail === 'object' && detail) err.payload = detail
    throw err
  }
  return data
}

export const api = {
  健康: () => req('/健康'),
  总览: () => req('/总览'),
  站点: () => req('/站点'),
  桩位: (站点编号) => req(`/桩位${qs({ 站点编号 })}`),
  枪线: (桩位编号) => req(`/枪线${qs({ 桩位编号 })}`),
  费率: () => req('/费率'),
  会话: (状态) => req(`/会话${qs({ 状态 })}`),
  会话详情: (id) => req(`/会话/${id}`),
  预检: (桩位编号, 枪线编号) => req(`/开会话/预检${qs({ 桩位编号, 枪线编号 })}`),
  开会话: (body) => req('/开会话', { method: 'POST', body: JSON.stringify(body) }),
  开充审计: (桩位编号) => req(`/开充审计${qs({ 桩位编号 })}`),
  脉冲: (id, body) => req(`/会话/${id}/脉冲`, { method: 'POST', body: JSON.stringify(body) }),
  结算: (id) => req(`/会话/${id}/结算`, { method: 'POST', body: '{}' }),
  超时: (id) => req(`/会话/${id}/超时`, { method: 'POST', body: '{}' }),
  中断: (id, 原因 = '急停') =>
    req(`/会话/${id}/中断`, { method: 'POST', body: JSON.stringify({ 原因 }) }),
  充日: () => req('/充日'),
  封印: (备注 = '') => req('/充日/封印', { method: 'POST', body: JSON.stringify({ 备注 }) }),
  解封: () => req('/充日/解封', { method: 'POST', body: '{}' }),
  设置: () => req('/设置'),
  写设置: (body) => req('/设置', { method: 'POST', body: JSON.stringify(body) }),
  改桩态: (id, 状态) =>
    req(`/桩位/${id}/状态`, { method: 'POST', body: JSON.stringify({ 状态 }) }),
}
