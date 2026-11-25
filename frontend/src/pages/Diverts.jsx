import React, { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function Diverts() {
  const [batches, setBatches] = useState([])
  const [scans, setScans] = useState([])
  const [scanId, setScanId] = useState('')
  const [readjudicateDest, setReadjudicateDest] = useState({})
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const [replay, setReplay] = useState(null)

  const load = async (preferredScanId = '') => {
    const [bs, ss] = await Promise.all([api('/回流批次'), api('/扫描')])
    setBatches(bs)
    setScans(ss)
    const wanted = preferredScanId || scanId
    if (wanted && ss.some((x) => String(x.id) === String(wanted) && (x.status === '已路由' || x.status === '已入袋'))) {
      setScanId(String(wanted))
    } else {
      const eligible = ss.find((x) => x.status === '已路由' || x.status === '已入袋')
      setScanId(eligible ? String(eligible.id) : '')
    }
  }
  useEffect(() => { load().catch((e) => setErr(e.message)) }, [])

  const create = async () => {
    setErr(''); setMsg('')
    try {
      const result = await api('/错分检出', {
        method: 'POST',
        body: JSON.stringify({ 扫描编号: Number(scanId) }),
      })
      setMsg(`已错分检出并创建/挂载回流批次 ${result.回执?.回流批次号 || result.批次?.code}；原口门在口=${result.回执?.原口门在口}`)
      await load()
    } catch (e) {
      setErr(e.message)
      await load().catch(() => {})
    }
  }

  const readjudicate = async (id) => {
    setErr(''); setMsg('')
    try {
      const dest = (readjudicateDest[id] || '').trim()
      await api(`/扫描/${id}/再裁决`, {
        method: 'POST',
        body: JSON.stringify(dest ? { 目的地码: dest, dest_code: dest } : {}),
      })
      setMsg(`扫描 ${id} 已再裁决回路由态`)
      await load()
    } catch (e) {
      setErr(e.message)
    }
  }

  const showReplay = async (id) => {
    setErr('')
    try {
      const data = await api(`/错分检出/${id}/重放`)
      setReplay(data)
    } catch (e) {
      setErr(e.message)
    }
  }

  const eligible = scans.filter((x) => x.status === '已路由' || x.status === '已入袋')
  const wrongScans = scans.filter((x) => x.status === '错分回流')

  return (
    <div className={surface_panel ? "panel" : "panel"}>
      <h2>错分回流 / 错分检出</h2>
      <p className="muted">口径：检出即扣减原口门在口；活动袋关联同事务作废，原袋与批次留痕，并开放再裁决。</p>
      {msg && <div className="msg" role="status">{msg}</div>}
      {err && <div className="err" role="alert">{err}</div>}
      <div className="row">
        <label>已路由/已入袋扫描
          <select
            data-testid="divert-scan-select"
            value={scanId}
            onChange={(e) => setScanId(e.target.value)}
          >
            <option value="">选择</option>
            {eligible.map((s) => (
              <option key={s.id} value={s.id}>{s.id} · {s.barcode} · {s.status}</option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="danger"
          data-testid="divert-detect-button"
          data-action="detect"
          disabled={!scanId}
          onClick={create}
        >
          错分检出（建回流批次）
        </button>
        <button type="button" className="secondary" onClick={load}>刷新回流页</button>
      </div>

      <h3>待再裁决错分件</h3>
      <table data-testid="divert-open-list">
        <thead>
          <tr><th>扫描</th><th>条码</th><th>原口门</th><th>原袋关联</th><th>回流批次</th><th>再裁决入口</th><th>重放</th></tr>
        </thead>
        <tbody>
          {wrongScans.map((s) => (
            <tr key={s.id} className="row-divert">
              <td>{s.id}</td>
              <td>{s.barcode}</td>
              <td>{s.原口门码 || s.口门码 || s.original_chute_id || '-'}</td>
              <td>{s.original_bag_id ? `原袋 ${s.原袋码 || s.original_bag_id}（已作废留痕）` : '无袋'}</td>
              <td>{s.回流批次号 || s.divert_batch_id}</td>
              <td>
                <span className="inline-action">
                  <input
                    aria-label={`回流再裁决目的地-${s.id}`}
                    data-testid={`divert-readjudicate-dest-${s.id}`}
                    value={readjudicateDest[s.id] || ''}
                    onChange={(e) => setReadjudicateDest((prev) => ({ ...prev, [s.id]: e.target.value }))}
                    placeholder="目的地码，可空"
                  />
                  <button
                    type="button"
                    data-testid={`divert-readjudicate-${s.id}`}
                    data-action="readjudicate"
                    data-scan-id={s.id}
                    onClick={() => readjudicate(s.id)}
                  >
                    再裁决
                  </button>
                </span>
              </td>
              <td>
                <button
                  type="button"
                  data-testid={`divert-replay-${s.id}`}
                  className="secondary"
                  onClick={() => showReplay(s.id)}
                >
                  重放结局
                </button>
              </td>
            </tr>
          ))}
          {wrongScans.length === 0 && <tr><td colSpan="7" className="muted">暂无错分桶待裁决件</td></tr>}
        </tbody>
      </table>

      <h3>回流批次</h3>
      <table data-testid="batch-list">
        <thead>
          <tr><th>编号</th><th>批次号</th><th>扫描</th><th>条码</th><th>扫描状态</th><th>原口门</th><th>原袋</th><th>批次状态</th><th>时间</th></tr>
        </thead>
        <tbody>
          {batches.map((b) => (
            <tr key={b.id}>
              <td>{b.id}</td>
              <td data-testid="batch-code">{b.回流批次号 || b.code}</td>
              <td>{b.扫描编号 || '-'}</td>
              <td>{b.条码 || '-'}</td>
              <td>{b.扫描状态 || '-'}</td>
              <td>{b.原口门码 || b.原口门编号 || '-'}</td>
              <td>{b.原袋编号 ? `${b.原袋码 || ''} #${b.原袋编号}` : '-'}</td>
              <td>{b.status}</td>
              <td>{b.created_at}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {replay && (
        <div className="replay-box" data-testid="replay-box">
          <h3>审计重放 · 扫描 {replay.扫描编号}</h3>
          <p>重放一致：<strong>{replay.重放一致 ? '是' : '否'}</strong>；事件 {replay.事件?.length || 0} 条；占用净额 {replay.占用事件净额}</p>
          <pre>{JSON.stringify(replay.一致性, null, 2)}</pre>
        </div>
      )}
    </div>
  )
}
