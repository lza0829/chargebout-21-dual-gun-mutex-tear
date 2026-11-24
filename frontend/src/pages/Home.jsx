import { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function Home() {
  const [o, setO] = useState(null)
  const [err, setErr] = useState('')
  const load = () =>
    api
      .总览()
      .then(setO)
      .catch((e) => setErr(e.message))
  useEffect(() => {
    load()
  }, [])
  if (!o) return <div className={surface_panel ? "page" : "page"}><p className="msg">{err || '加载中'}</p></div>
  const pileItems = [
    ['桩位空闲', o.桩位空闲],
    ['桩位占用', o.桩位占用],
    ['桩位故障', o.桩位故障],
    ['桩位维护', o.桩位维护],
  ]
  const sessItems = [
    ['充电中', o.充电中],
    ['结算中', o.结算中],
    ['已超时', o.已超时],
    ['已关账', o.已关账],
    ['已中断', o.已中断],
  ]
  const holdItems = [
    ['持有会话数', o.持有会话数],
    ['持有桩位数', o.持有桩位数],
    ['在充电量', o.在充电量],
    ['本日关账金额', o.本日关账金额],
  ]
  const group = (title, items) => (
    <>
      <h3>{title}</h3>
      <div className="grid">
        {items.map(([k, v]) => (
          <div className="stat" key={k}>
            <div className="k">{k}</div>
            <div className="v">{v}</div>
          </div>
        ))}
      </div>
    </>
  )
  return (
    <div className="page">
      <h2>总览</h2>
      <p className="lead">
        营业日 {o.营业日} · 充日{o.充日已封印 ? '已封印' : '未封印'} · 口径：<b>{o.并充口径}</b>（同桩上限 {o.同桩并充上限} 笔）
      </p>
      {group('桩位', pileItems)}
      {group('会话', sessItems)}
      {group('持有与电量', holdItems)}
      <p className="hint">
        桩位占用按「持有会话」推导，两张表口径一致：持有会话数 = 充电中 + 结算中 + 已超时；持有桩位数 ≤ 桩位数。
      </p>
      <button type="button" onClick={load}>刷新</button>
      <p className="msg">{err}</p>
    </div>
  )
}
