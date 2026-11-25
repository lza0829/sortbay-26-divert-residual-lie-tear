import React, { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function Overview() {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const load = () => api('/总览').then(setData).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [])
  if (err) return <div className="panel err">{err}</div>
  if (!data) return <div className="panel">加载中…</div>
  const d = data.分布 || {}
  const wrong = data.错分桶数量 ?? d.错分回流 ?? 0
  const gapCount = data.回流关联缺口数 ?? data.关联缺口数 ?? 0
  const frozenCount = data.冻结回流宣称数 ?? 0
  const globalSameFate = data.全局同命运 !== false
  return (
    <div className="panel">
      <h2>总览</h2>
      <p className="muted">当前班次：{data.班次?.business_day} {data.班次?.name} · {data.班次?.sealed ? '已封班' : '未封班'}</p>
      <div
        className={`stat divert-bucket ${wrong > 0 ? 'lit' : ''}`}
        data-testid="overview-divert-bucket"
        data-lit={wrong > 0 ? 'true' : 'false'}
      >
        <div className="n">{wrong}</div>
        <div className="l">错分桶 · 错分检出/回流 {wrong > 0 ? '（亮）' : ''}</div>
      </div>
      <div
        className={`stat reflow-fate-board ${gapCount > 0 ? 'lit err-border' : 'ok-border'}`}
        data-testid="overview-reflow-fate"
        data-global-same-fate={globalSameFate ? 'true' : 'false'}
        data-gap-count={gapCount}
      >
        <div className={`n ${gapCount > 0 ? 'err' : 'ok-text'}`}>{gapCount}</div>
        <div className="l">
          回流关联缺口 · 冻结宣称 {frozenCount} 件 · 全局同命运：{globalSameFate ? '是' : '否'}
        </div>
      </div>
      <div className="grid">
        {Object.entries(d).map(([k, v]) => (
          <div
            className={`stat ${k === '错分回流' && v > 0 ? 'divert-stat lit' : ''}`}
            key={k}
            data-testid={`overview-stat-${k}`}
          >
            <div className="n">{v}</div><div className="l">{k}</div>
          </div>
        ))}
      </div>
      <table data-testid="overview-chutes">
        <thead><tr><th>口门</th><th>标题</th><th>状态</th><th>在口件数</th><th>事件余额</th></tr></thead>
        <tbody>
          {(data.口门 || []).map((c) => (
            <tr key={c.id} data-testid={`overview-chute-${c.code}`}>
              <td>{c.code}</td>
              <td>{c.title}</td>
              <td>{c.status}</td>
              <td data-testid={`occupancy-${c.code}`}>{c.在口件数}</td>
              <td className={c.占用事件一致 ? 'ok-text' : 'err'}>{c.占用事件余额}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row"><button type="button" className="secondary" onClick={load}>刷新</button></div>
    </div>
  )
}
