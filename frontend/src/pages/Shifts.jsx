import React, { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function Shifts() {
  const [rows, setRows] = useState([])
  const [rec, setRec] = useState(null)
  const [err, setErr] = useState('')
  const load = async () => {
    setRows(await api('/班次'))
    setRec(await api('/班次/对账'))
  }
  useEffect(() => { load().catch((e) => setErr(e.message)) }, [])
  const seal = async (id) => {
    try { await api(`/班次/${id}/封班`, { method: 'POST' }); load() }
    catch (e) { setErr(e.message) }
  }
  return (
    <div className={surface_panel ? "panel" : "panel"}>
      <h2>班次对账</h2>
      {err && <div className="err">{err}</div>}
      {rec && (
        <div className="grid" data-testid="shift-reconcile">
          <div className="stat"><div className="n">{rec.扫描总数}</div><div className="l">扫描总数</div></div>
          <div className="stat"><div className="n">{rec.已路由及以上}</div><div className="l">已路由及以上</div></div>
          <div className="stat"><div className="n">{rec.已封袋数}</div><div className="l">已封袋数</div></div>
          <div className="stat"><div className="n">{rec.未清件数}</div><div className="l">未清件数</div></div>
          <div className={`stat ${rec.对账一致 ? 'ok-stat' : 'bad-stat'}`}><div className="n">{rec.错分桶数 ?? 0}</div><div className="l">错分桶 / 对账{rec.对账一致 ? '一致' : '有差异'}</div></div>
          <div className="stat"><div className="n">{rec.错分检出事件数 ?? 0}</div><div className="l">错分检出事件</div></div>
          <div className="stat"><div className="n">{rec.再裁决事件数 ?? 0}</div><div className="l">再裁决事件</div></div>
        </div>
      )}
      {rec && rec.差异?.length > 0 && (
        <div className="err" data-testid="shift-reconcile-diff">
          {rec.差异.join('；')}
        </div>
      )}
      <table data-testid="shift-table">
        <thead><tr><th>编号</th><th>营业日</th><th>班次</th><th>封班</th><th>操作</th></tr></thead>
        <tbody>
          {rows.map((s) => (
            <tr key={s.id}>
              <td>{s.id}</td><td>{s.business_day}</td><td>{s.name}</td><td>{s.sealed ? '已封班' : '未封班'}</td>
              <td>{!s.sealed && (
                <button
                  type="button"
                  data-testid={`shift-seal-${s.id}`}
                  data-action="seal-shift"
                  data-shift-id={s.id}
                  onClick={() => seal(s.id)}
                >
                  封班
                </button>
              )}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
