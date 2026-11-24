import { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function OpenSession() {
  const [piles, setPiles] = useState([])
  const [pileId, setPileId] = useState('')
  const [gunId, setGunId] = useState('')
  const [pre, setPre] = useState(null)
  const [msg, setMsg] = useState('')
  const [ok, setOk] = useState(false)
  const [receipt, setReceipt] = useState(null)
  const [audit, setAudit] = useState([])

  const loadPiles = () =>
    api.桩位().then((list) => {
      setPiles(list)
      setPileId((cur) => cur || (list[0] ? String(list[0].id) : ''))
    })

  const loadPrecheck = () => {
    if (!pileId) return
    api
      .预检(Number(pileId), gunId ? Number(gunId) : undefined)
      .then(setPre)
      .catch((e) => setMsg(e.message))
  }

  const loadAudit = () =>
    api
      .开充审计(pileId ? Number(pileId) : undefined)
      .then(setAudit)
      .catch(() => {})

  useEffect(() => {
    loadPiles()
  }, [])
  useEffect(() => {
    setGunId('')
  }, [pileId])
  useEffect(() => {
    loadPrecheck()
  }, [pileId, gunId])
  useEffect(() => {
    loadAudit()
  }, [pileId])

  const submit = async () => {
    setMsg('')
    setOk(false)
    setReceipt(null)
    try {
      const body = { 桩位编号: Number(pileId) }
      if (gunId) body.枪线编号 = Number(gunId)
      const r = await api.开会话(body)
      setMsg(
        `已开 ${r.会话号} · 桩位${r.桩位有效态} · 枪线${r.枪线名称}/${r.枪线有效态} · 锁定电价 ${r.锁定电价}`,
      )
      setOk(true)
      setReceipt(r)
    } catch (e) {
      setMsg(e.message)
      if (e.payload?.审计编号) setReceipt(null)
    }
    await Promise.all([loadPiles(), loadPrecheck(), loadAudit()])
  }

  const current = piles.find((p) => String(p.id) === pileId)
  const gunBars = current?.枪线 || []

  return (
    <div className={surface_panel ? "page" : "page"}>
      <h2>开会话</h2>
      <p className="lead">
        口径：<b>同桩互斥单充</b>——同一桩位任意时刻至多一笔充电中/未结会话；另一枪点开充将整笔拒绝，且不改动已有会话。
      </p>
      <div className="panel">
        <div className="row">
          <label>桩位</label>
          <select value={pileId} onChange={(e) => setPileId(e.target.value)}>
            {piles.map((p) => (
              <option key={p.id} value={p.id}>
                {p.site_name} · {p.name} · {p.status}
                {p.在充会话 ? ` · 在充 ${p.在充会话.会话号}` : ''}
              </option>
            ))}
          </select>
          <label>枪线</label>
          <select value={gunId} onChange={(e) => setGunId(e.target.value)}>
            <option value="">自动选可用（{pre?.选枪谓词 || '编号升序首个可用'}）</option>
            {gunBars.map((g) => (
              <option key={g.id} value={g.id} disabled={g.状态 !== '可用'}>
                {g.name} · {g.状态}
                {g.占因 ? `（${g.占因}）` : ''}
              </option>
            ))}
          </select>
          <button type="button" className="primary" onClick={submit} disabled={!pileId}>
            发起开充
          </button>
        </div>

        {current && (
          <div className="gunbar">
            {gunBars.map((g) => (
              <div key={g.id} className={`guncard ${g.状态}`}>
                <div className="k">{g.name}</div>
                <div className="v">
                  <span className={`tag ${g.状态}`}>{g.状态}</span>
                </div>
                <div className="hint">{g.占因 || '可开充'}</div>
                {g.会话号 && <div className="hint">持有 {g.会话号}（{g.会话状态}）</div>}
              </div>
            ))}
            <div className={`guncard ${pre?.可开充 ? '可用' : '占用'}`}>
              <div className="k">本桩并充闸门</div>
              <div className="v">{pre?.可开充 ? '可开充' : '拒绝'}</div>
              <div className="hint">{pre?.拒因 || '无持有会话'}</div>
            </div>
          </div>
        )}

        <p className={`msg ${ok ? 'ok' : ''}`}>{msg}</p>
      </div>

      {receipt && receipt.成功 && (
        <div className="panel">
          <h3>开充回执</h3>
          <table>
            <tbody>
              <tr>
                <th>会话号</th>
                <td>{receipt.会话号}</td>
                <th>会话编号</th>
                <td>{receipt.会话编号}</td>
              </tr>
              <tr>
                <th>桩位有效态</th>
                <td>{receipt.桩位有效态}</td>
                <th>枪线有效态</th>
                <td>
                  {receipt.枪线名称} · {receipt.枪线有效态}
                </td>
              </tr>
              <tr>
                <th>锁定电价</th>
                <td>{receipt.锁定电价}</td>
                <th>在充电量</th>
                <td>{receipt.总览?.在充电量}</td>
              </tr>
              <tr>
                <th>总览充电中</th>
                <td>{receipt.总览?.充电中}</td>
                <th>持有桩位数</th>
                <td>{receipt.总览?.持有桩位数}</td>
              </tr>
            </tbody>
          </table>
          <h3>同桩双枪条</h3>
          <div className="gunbar">
            {(receipt.双枪条 || []).map((g) => (
              <div key={g.id} className={`guncard ${g.状态}`}>
                <div className="k">{g.name}</div>
                <div className="v">
                  <span className={`tag ${g.状态}`}>{g.状态}</span>
                </div>
                <div className="hint">{g.占因 || g.会话号 || '空闲'}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="panel">
        <h3>开充审计流水 {pileId ? `· 桩位 ${current?.name || pileId}` : '· 最近 200 条'}</h3>
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>桩位</th>
              <th>枪线</th>
              <th>结果</th>
              <th>拒因 / 会话</th>
              <th>规则</th>
            </tr>
          </thead>
          <tbody>
            {audit.map((a) => (
              <tr key={a.id}>
                <td>{a.created_at}</td>
                <td>{a.pile_id ?? '—'}</td>
                <td>{a.gun_id ?? '自动'}</td>
                <td>
                  <span className={`tag ${a.result === '开充成功' ? '充电中' : '故障'}`}>{a.result}</span>
                </td>
                <td>{a.reason || a.slip_no || '—'}</td>
                <td>{a.rule}</td>
              </tr>
            ))}
            {audit.length === 0 && (
              <tr>
                <td colSpan={6} className="hint">
                  暂无开充流水
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
