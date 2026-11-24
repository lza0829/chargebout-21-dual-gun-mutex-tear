import { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function Guns() {
  const [list, setList] = useState([])
  const load = () => api.枪线().then(setList)
  useEffect(() => {
    load()
  }, [])
  return (
    <div className={surface_panel ? "page" : "page"}>
      <h2>枪线</h2>
      <p className="lead">各桩枪口有效态由持有会话推导；同桩互斥时另一枪显示为占用并注明占因。</p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>桩位</th>
              <th>枪名</th>
              <th>状态</th>
              <th>占因 / 持有会话</th>
            </tr>
          </thead>
          <tbody>
            {list.map((g) => (
              <tr key={g.id}>
                <td>{g.pile_name}</td>
                <td>{g.name}</td>
                <td>
                  <span className={`tag ${g.status}`}>{g.status}</span>
                </td>
                <td>{g.会话号 ? `${g.会话号} · ${g.占因}` : g.占因 || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <button type="button" onClick={load} style={{ marginTop: '0.75rem' }}>
          刷新
        </button>
      </div>
    </div>
  )
}
