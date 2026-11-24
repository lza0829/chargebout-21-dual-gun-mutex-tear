import { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function Piles() {
  const [list, setList] = useState([])
  const [msg, setMsg] = useState('')
  const load = () => api.桩位().then(setList)
  useEffect(() => {
    load()
  }, [])
  const setStatus = async (id, 状态) => {
    try {
      await api.改桩态(id, 状态)
      setMsg(`桩位 ${id} → ${状态}`)
      load()
    } catch (e) {
      setMsg(e.message)
    }
  }
  return (
    <div className={surface_panel ? "page" : "page"}>
      <h2>桩位</h2>
      <p className="lead">
        占用态由「持有会话」推导（同桩互斥单充）：桩位有充电中/结算中/已超时任一未结会话即为占用，双枪条同步显示每把枪的占因。
      </p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>站点</th>
              <th>桩名</th>
              <th>额定功率</th>
              <th>费率</th>
              <th>状态</th>
              <th>双枪条</th>
              <th>在充会话</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.map((p) => (
              <tr key={p.id}>
                <td>{p.site_name}</td>
                <td>{p.name}</td>
                <td>{p.rated_kw}</td>
                <td>{p.rate_name}</td>
                <td>
                  <span className={`tag ${p.status}`}>{p.status}</span>
                  {p.存储状态 !== p.status && <div className="hint">存储态 {p.存储状态}</div>}
                </td>
                <td>
                  <div className="gunbar compact">
                    {(p.枪线 || []).map((g) => (
                      <span key={g.id} className={`gunchip ${g.状态}`} title={g.占因 || '可开充'}>
                        {g.name} · {g.状态}
                        {g.会话号 ? ` · ${g.会话号}` : ''}
                      </span>
                    ))}
                  </div>
                </td>
                <td>{p.在充会话 ? `${p.在充会话.会话号}（${p.在充会话.状态}）` : '—'}</td>
                <td className="row" style={{ border: 0 }}>
                  {['空闲', '维护', '故障'].map((s) => (
                    <button
                      key={s}
                      type="button"
                      onClick={() => setStatus(p.id, s)}
                      disabled={!!p.在充会话}
                    >
                      {s}
                    </button>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="msg">{msg}</p>
      </div>
    </div>
  )
}
