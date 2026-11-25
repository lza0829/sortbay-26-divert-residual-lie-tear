import React, { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function Bags() {
  const [bags, setBags] = useState([])
  const [chutes, setChutes] = useState([])
  const [scans, setScans] = useState([])
  const [chuteId, setChuteId] = useState('')
  const [bagId, setBagId] = useState('')
  const [scanId, setScanId] = useState('')
  const [manualScanId, setManualScanId] = useState('')
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const load = async () => {
    setBags(await api('/封袋'))
    const cs = await api('/口门'); setChutes(cs)
    if (!chuteId && cs[0]) setChuteId(String(cs[0].id))
    const ss = await api('/扫描'); setScans(ss)
  }
  useEffect(() => { load().catch((e) => setErr(e.message)) }, [])
  const openBag = async () => {
    try {
      const b = await api('/封袋/开袋', { method: 'POST', body: JSON.stringify({ 口门编号: Number(chuteId) }) })
      setMsg(`已开袋 ${b.code}`); setBagId(String(b.id)); load()
    } catch (e) { setErr(e.message) }
  }
  const add = async () => {
    const target = manualScanId || scanId
    if (!bagId || !target) {
      setErr('请填写袋编号并选择或输入扫描编号')
      return
    }
    try {
      const scan = await api(`/封袋/${bagId}/入袋`, { method: 'POST', body: JSON.stringify({ 扫描编号: Number(target) }) })
      setMsg(`入袋成功：扫描 ${scan.id} / ${scan.status}`)
      setManualScanId('')
      load()
    } catch (e) {
      setErr(e.message)
      load().catch(() => {})
    }
  }
  const seal = async () => {
    try {
      const b = await api(`/封袋/${bagId}/封口`, { method: 'POST' })
      setMsg(`封袋成功：${b.code}`); load()
    } catch (e) { setErr(e.message) }
  }
  const routedScans = scans.filter((x) => x.status === '已路由')
  return (
    <div className={surface_panel ? "panel" : "panel"}>
      <h2>封袋 / 原袋入口</h2>
      {msg && <div className="msg" role="status">{msg}</div>}
      {err && <div className="err" role="alert">{err}</div>}
      <div className="row">
        <label>口门
          <select value={chuteId} onChange={(e) => setChuteId(e.target.value)}>
            {chutes.map((c) => <option key={c.id} value={c.id}>{c.code}</option>)}
          </select>
        </label>
        <button type="button" data-testid="bag-open-button" data-action="open-bag" onClick={openBag}>开袋</button>
      </div>
      <div className="row">
        <label>袋编号<input data-testid="bag-id-input" value={bagId} onChange={(e) => setBagId(e.target.value)} /></label>
        <label>已路由扫描
          <select value={scanId} onChange={(e) => setScanId(e.target.value)}>
            <option value="">选择</option>
            {routedScans.map((s) => <option key={s.id} value={s.id}>{s.id} · {s.barcode}</option>)}
          </select>
        </label>
        <label>或扫描编号<input data-testid="bag-manual-scan-id" value={manualScanId} onChange={(e) => setManualScanId(e.target.value)} placeholder="含被拦截的已检出件" /></label>
        <button type="button" data-testid="bag-add-button" onClick={add}>入袋</button>
        <button type="button" className="secondary" data-testid="bag-seal-button" data-action="seal-bag" onClick={seal}>封口</button>
      </div>
      <p className="muted">已错分检出件不会出现在“已路由扫描”中；直接输入其编号由后端钉死：未再裁决前不得由原袋入口收成已入袋。</p>
      <table data-testid="bag-table">
        <thead><tr><th>编号</th><th>袋码</th><th>口门</th><th>状态</th><th>件数</th><th>错分检出作废关联</th></tr></thead>
        <tbody>
          {bags.map((b) => (
            <tr key={b.id}>
              <td>{b.id}</td><td>{b.code}</td><td>{b.口门码}</td><td>{b.status}</td><td>{b.件数}</td>
              <td className={b.错分检出作废关联数 ? 'warn-text' : ''}>{b.错分检出作废关联数 || 0}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
