import React, { useEffect, useState } from 'react'
import { api } from '../api'

function ScanRow({ row, busy, onAction }) {
  const [dest, setDest] = useState(row.dest_code || '')
  const wrong = row.status === '错分回流'
  const detectable = row.status === '已路由' || row.status === '已入袋'
  const fateOpen = row.可再裁决 !== false
  const fateOk = row.同命运 === true || row.same_fate === true
  const gapCodes = row.gap_codes || row.缺口码列表 || []
  return (
    <tr data-testid={`scan-row-${row.id}`} className={wrong ? 'row-divert' : ''}>
      <td>{row.id}</td>
      <td>{row.barcode}</td>
      <td>{row.dest_code}</td>
      <td>
        <span
          className={`status-pill ${wrong ? 'status-divert lit' : `status-${row.status}`}`}
          data-testid="scan-status"
        >
          {row.status}
        </span>
      </td>
      <td>{row.口门码 || row.chute_id || '-'}</td>
      <td>
        {row.bag_id ? (row.当前袋码 || row.bag_id) : row.original_bag_id ? `原袋 ${row.原袋码 || row.original_bag_id}（已作废）` : '-'}
      </td>
      <td>{row.回流批次号 || row.divert_batch_id || '-'}</td>
      <td className="actions-cell">
        {row.status === '待路由' && (
          <button
            type="button"
            data-testid={`route-${row.id}`}
            data-action="route"
            data-scan-id={row.id}
            disabled={busy}
            onClick={() => onAction('route', row.id)}
          >
            路由
          </button>
        )}
        {detectable && (
          <button
            type="button"
            className="danger"
            data-testid={`detect-${row.id}`}
            data-action="detect"
            data-scan-id={row.id}
            disabled={busy}
            onClick={() => onAction('detect', row.id)}
          >
            错分检出
          </button>
        )}
        {wrong && (
          <span className="inline-action">
            {!fateOk && (
              <span className="err" data-testid={`scan-fate-gap-${row.id}`} title={(row.缺口文案 || gapCodes.join('、') || '回流关联命运不一致')}>
                关联缺口{gapCodes.length ? `：${gapCodes.join('、')}` : ''}（入口冻结）
              </span>
            )}
            <input
              aria-label={`再裁决目的地-${row.id}`}
              data-testid={`readjudicate-input-${row.id}`}
              data-scan-id={row.id}
              value={dest}
              onChange={(e) => setDest(e.target.value)}
              placeholder="目的地码"
            />
            <button
              type="button"
              data-testid={`readjudicate-${row.id}`}
              data-action="readjudicate"
              data-scan-id={row.id}
              data-open={fateOpen ? 'true' : 'false'}
              disabled={busy || !fateOpen}
              title={fateOpen ? '' : '回流关联账未清，再裁决入口已冻结；请先回滚半截回流或核销关联缺口'}
              onClick={() => onAction('readjudicate', row.id, { 目的地码: dest || undefined })}
            >
              再裁决（路由）
            </button>
            {!fateOk && (
              <button
                type="button"
                className="danger"
                data-testid={`scan-rollback-${row.id}`}
                data-action="rollback-half"
                disabled={busy}
                onClick={() => onAction('rollback', row.id)}
              >
                半截回滚
              </button>
            )}
          </span>
        )}
      </td>
    </tr>
  )
}

const surface_panel = true
export default function Scans() {
  const [rows, setRows] = useState([])
  const [barcode, setBarcode] = useState('')
  const [dest, setDest] = useState('110')
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')
  const [busyId, setBusyId] = useState(null)
  const load = () => api('/扫描').then(setRows).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [])

  const create = async () => {
    setErr(''); setMsg('')
    try {
      const scan = await api('/扫描', { method: 'POST', body: JSON.stringify({ 条码: barcode, 目的地码: dest }) })
      setMsg(`过机装载成功：${scan.barcode} #${scan.id}`)
      setBarcode('')
      load()
    } catch (e) { setErr(e.message) }
  }

  const action = async (kind, id, body) => {
    setErr(''); setMsg(''); setBusyId(id)
    try {
      if (kind === 'route') {
        await api(`/扫描/${id}/路由`, { method: 'POST', body: JSON.stringify({}) })
        setMsg('路由成功，口门在口已同刷')
      } else if (kind === 'detect') {
        const result = await api('/错分检出', { method: 'POST', body: JSON.stringify({ 扫描编号: id }) })
        const r = result.回执 || {}
        setMsg(`错分检出成功：${r.回流批次号 || ''}；原口门在口=${r.原口门在口 ?? ''}；${r.原袋编号 ? '原袋关联已作废留痕' : '无原袋'}；总览错分桶=${result.总览?.错分桶数量 ?? ''}`)
      } else if (kind === 'rollback') {
        await api(`/回流关联/${id}/回滚`, { method: 'POST', body: JSON.stringify({}) })
        setMsg('半截回流已整笔回滚：扫描/批次/关联/口门恢复同命运')
      } else {
        await api(`/扫描/${id}/再裁决`, { method: 'POST', body: JSON.stringify(body || {}) })
        setMsg('再裁决成功：已回到新口门，可继续入袋封袋')
      }
      await load()
    } catch (e) {
      setErr(e.message)
      await load().catch(() => {})
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className={surface_panel ? "panel" : "panel"}>
      <h2>过机扫描 / 扫描册</h2>
      <div className="row">
        <label>条码<input value={barcode} onChange={(e) => setBarcode(e.target.value)} /></label>
        <label>目的地码<input value={dest} onChange={(e) => setDest(e.target.value)} /></label>
        <button type="button" onClick={create}>装载扫描</button>
        <button type="button" className="secondary" onClick={load}>刷新扫描册</button>
      </div>
      {msg && <div className="msg" role="status">{msg}</div>}
      {err && <div className="err" role="alert">{err}</div>}
      <div className="table-scroll">
        <table data-testid="scan-book">
          <thead>
            <tr>
              <th>编号</th><th>条码</th><th>目的地</th><th>扫描状态</th><th>口门</th>
              <th>活动袋/原袋关联</th><th>回流批次</th><th>操作 / 再裁决入口</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => <ScanRow key={r.id} row={r} busy={busyId === r.id} onAction={action} />)}
          </tbody>
        </table>
      </div>
    </div>
  )
}
