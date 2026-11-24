import { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function Sessions() {
  const [list, setList] = useState([])
  const [detail, setDetail] = useState(null)
  const load = () => api.会话().then(setList)
  useEffect(() => {
    load()
  }, [])
  return (
    <div className={surface_panel ? "page" : "page"}>
      <h2>会话册</h2>
      <p className="lead">
        最近会话列表；持有中（充电中/结算中/已超时）的会话会占住整桩，桩位与双枪条按同一口径推导。
      </p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>会话号</th>
              <th>站点桩位</th>
              <th>枪线</th>
              <th>状态</th>
              <th>桩/枪有效态</th>
              <th>电量</th>
              <th>金额</th>
              <th>营业日</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.map((s) => (
              <tr key={s.id}>
                <td>{s.slip_no}</td>
                <td>
                  {s.site_name} · {s.pile_name}
                </td>
                <td>{s.gun_name}</td>
                <td>
                  <span className={`tag ${s.status}`}>{s.status}</span>
                  {s.持有中 && <span className="hint"> 持有中</span>}
                </td>
                <td>
                  {s.桩位有效态} / {s.枪线有效态}
                </td>
                <td>{s.kwh}</td>
                <td>{s.amount}</td>
                <td>{s.business_day}</td>
                <td>
                  <button type="button" onClick={() => api.会话详情(s.id).then(setDetail)}>
                    详情
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <button type="button" onClick={load} style={{ marginTop: '0.75rem' }}>
          刷新
        </button>
      </div>
      {detail && (
        <div className="panel">
          <h3>
            {detail.slip_no} · <span className={`tag ${detail.status}`}>{detail.status}</span>
          </h3>
          <p>
            锁定电价 {detail.price_locked} · 电量 {detail.kwh} · 金额 {detail.amount}
          </p>
          <p className="hint">
            桩位有效态 {detail.桩位有效态} · 枪线有效态 {detail.枪线有效态} · 口径 {detail.并充口径}
          </p>
          <h3>脉冲</h3>
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>电量增量</th>
                <th>功率</th>
              </tr>
            </thead>
            <tbody>
              {(detail.脉冲 || []).map((p) => (
                <tr key={p.id}>
                  <td>{p.created_at}</td>
                  <td>{p.delta_kwh}</td>
                  <td>{p.power_kw}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>事件</h3>
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>动作</th>
                <th>备注</th>
              </tr>
            </thead>
            <tbody>
              {(detail.事件 || []).map((e) => (
                <tr key={e.id}>
                  <td>{e.created_at}</td>
                  <td>{e.action}</td>
                  <td>{e.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
