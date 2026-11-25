import React, { useEffect, useState } from 'react'
import { api } from '../api'

export default function Audit() {
  const [rows, setRows] = useState([])
  const [scanId, setScanId] = useState('')
  const [replay, setReplay] = useState(null)
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')

  const load = () => api('/审计?limit=300').then(setRows).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [])

  const replayScan = async (id) => {
    setErr(''); setMsg('')
    const target = id || Number(scanId)
    if (!target) {
      setErr('请输入扫描编号')
      return
    }
    try {
      const data = await api(`/审计/重放?扫描编号=${encodeURIComponent(target)}`)
      setReplay(data)
      setMsg(`扫描 ${target} 重放${data.重放一致 ? '一致' : '不一致'}`)
    } catch (e) {
      setErr(e.message)
    }
  }

  return (
    <div className="panel">
      <h2>审计与结局重放</h2>
      {msg && <div className="msg" role="status">{msg}</div>}
      {err && <div className="err" role="alert">{err}</div>}
      <div className="row">
        <label>扫描编号<input data-testid="audit-scan-id-input" value={scanId} onChange={(e) => setScanId(e.target.value)} placeholder="例如 1" /></label>
        <button type="button" data-testid="audit-replay-button" onClick={() => replayScan()}>审计重放</button>
        <button type="button" className="secondary" onClick={load}>刷新审计</button>
      </div>
      <table data-testid="audit-list">
        <thead><tr><th>ID</th><th>时间</th><th>类型</th><th>关联</th><th>动作</th><th>明细</th><th>重放</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td>{r.id}</td>
              <td>{r.created_at}</td>
              <td>{r.kind}</td>
              <td>{r.ref_id ?? '-'}</td>
              <td>{r.action}</td>
              <td className="note-cell">{r.note}</td>
              <td>{r.kind === '错分检出' && r.ref_id ? (
                <button type="button" className="secondary" onClick={() => replayScan(r.ref_id)}>重放结局</button>
              ) : '-'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {replay && (
        <div className="replay-box" data-testid="audit-replay-detail">
          <h3>重放结果 · 扫描 {replay.扫描编号}</h3>
          <p>重放一致：<strong>{replay.重放一致 ? '是' : '否'}</strong></p>
          <pre>{JSON.stringify({ 一致性: replay.一致性, 最新检出结局: replay.最新检出结局, 事件: replay.事件?.map((e) => ({ id: e.id, type: e.event_type, payload: e.payload })) }, null, 2)}</pre>
        </div>
      )}
    </div>
  )
}
