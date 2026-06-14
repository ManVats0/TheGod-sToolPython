  import React, { useState, useCallback, useEffect, useMemo, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { useStore } from '../store'
import TimeSeriesChart from '../components/TimeSeriesChart'
import { API_BASE } from '../config'

  const IMPUTATION_METHODS = [
    { value:'none',               label:'None',            desc:'Do not impute.' },
    { value:'mean',               label:'Mean',            desc:'Column average. Best for normally distributed numeric data.' },
    { value:'median',             label:'Median',          desc:'Middle value. Best for skewed data.' },
    { value:'mode',               label:'Mode',            desc:'Most frequent. Best for categorical columns.' },
    { value:'ffill',              label:'Forward Fill',    desc:'Last known value forward. Best for time-series.' },
    { value:'bfill',              label:'Backward Fill',   desc:'Next known value backward. Best for time-series.' },
    { value:'knn',                label:'KNN',             desc:'K nearest neighbours. Best when columns correlate.' },
    { value:'constant_zero',      label:'Constant (0)',    desc:'Replace numeric nulls with 0.' },
    { value:'constant_unknown',   label:'Unknown',         desc:'Text → "unknown", numeric → -1.' },
    { value:'interpolate_linear', label:'Linear Interp',   desc:'Estimate from surrounding values.' },
    { value:'interpolate_time',   label:'Index Interp',    desc:'Estimate by row index.' },
  ]

  const CLEANING_OPTIONS = [
    { key:'remove_duplicates', label:'Remove Duplicates',  desc:'Delete identical rows',              issueKey:'duplicates' },
    { key:'remove_empty_rows', label:'Remove Empty Rows',  desc:'Delete completely blank lines',       issueKey:'empty_rows' },
    { key:'fill_missing',      label:'Fill Missing (NaN)', desc:'Median for numbers · mode for text', issueKey:'missing'    },
    { key:'handle_outliers',   label:'Cap Outliers',       desc:'Modified Z-Score method',             issueKey:'outliers'   },
    { key:'standardize_text',  label:'Standardize Text',   desc:'Lowercase + trim spaces',             issueKey:'messy_text' },
    { key:'fix_dates',         label:'Fix Dates',          desc:'Convert to standard date format',     issueKey:'fix_dates'  },
  ]

  const ISSUE_BADGES: Record<string, { bg: string; text: string }> = {
    'Missing value': { bg: 'rgba(249,115,22,0.15)', text: '#fb923c' },
    'Duplicate row': { bg: 'rgba(239,68,68,0.15)',  text: '#f87171' },
    'Outlier':       { bg: 'rgba(234,179,8,0.15)',  text: '#fbbf24' },
  }

  const sparkleCSS = `
  @keyframes sparkle-pulse {
    0%, 100% { box-shadow: 0 0 20px rgba(124,58,237,0.6), 0 0 40px rgba(37,99,235,0.3), 0 0 0 0 rgba(124,58,237,0.4); }
    50% { box-shadow: 0 0 30px rgba(124,58,237,0.9), 0 0 60px rgba(37,99,235,0.5), 0 0 0 8px rgba(124,58,237,0); }
  }
  @keyframes sparkle-shine {
    0% { background-position: -200% center; }
    100% { background-position: 200% center; }
  }
  .sparkle-btn {
    animation: sparkle-pulse 1.8s ease-in-out infinite, sparkle-shine 2.5s linear infinite;
    background: linear-gradient(135deg, #7c3aed, #2563eb, #7c3aed) !important;
    background-size: 200% auto !important;
  }
  `

  function SaveToSourceButton({ result, activeSourceName, sources, API_BASE }: {
    result: any
    activeSourceName: string
    sources: any[]
    API_BASE: string
  }) {
    const [saving, setSaving] = React.useState(false)
    const [saveMsg, setSaveMsg] = React.useState('')

    const sourceObj = sources.find((s: any) => s.name === activeSourceName)
    const srcType = sourceObj?.type ? String(sourceObj.type) : ''

    // Only show for non-CSV, non-catalog, writable sources
    const labelMap: Record<string, string> = {
      sheets:   'Save to Google Sheet',
      mysql:    'Save to MySQL',
      postgres: 'Save to PostgreSQL',
      bigquery: 'Save to BigQuery',
      mongodb:  'Save to MongoDB',
      aws:      'Save to AWS',
      cloudsql: 'Save to Cloud SQL',
    }

    if (!srcType || !labelMap[srcType]) return null

    const label = labelMap[srcType]

    const handleSave = async () => {
      const confirmed = window.confirm(
        `⚠️ This will replace your existing data in the original source:\n\n"${activeSourceName}"\n\nwith the cleaned version. This cannot be undone.\n\nAre you sure?`
      )
      if (!confirmed) return

      setSaving(true)
      setSaveMsg('')

      try {
        // Download the cleaned CSV blob from backend
        const csvRes = await fetch(`${API_BASE}${result.download_url}`)
        if (!csvRes.ok) { setSaveMsg('❌ Could not download cleaned file.'); setSaving(false); return }
        const blob = await csvRes.blob()
        const formData = new FormData()
        formData.append('file', new File([blob], 'cleaned.csv', { type: 'text/csv' }))
        formData.append('source_type', srcType)
        formData.append('source_name', activeSourceName)

        if (srcType === 'sheets') {
          const storedSession = localStorage.getItem('datapro_session') || ''
          if (storedSession) formData.append('session_id', storedSession)
          if (sourceObj?.sheetId) formData.append('sheet_id', sourceObj.sheetId)
          if (sourceObj?.tab) formData.append('tab', sourceObj.tab)
        }

        else if (srcType === 'mysql') {
          const savedMySQL = JSON.parse(localStorage.getItem('datapro_mysql_connections') || '[]')
          const atIdx = activeSourceName.lastIndexOf('@')
          const host = atIdx >= 0 ? activeSourceName.slice(atIdx + 1) : ''
          const beforeAt = atIdx >= 0 ? activeSourceName.slice(0, atIdx) : activeSourceName
          const dotIdx = beforeAt.indexOf('.')
          const database = dotIdx >= 0 ? beforeAt.slice(0, dotIdx) : beforeAt
          const table = dotIdx >= 0 ? beforeAt.slice(dotIdx + 1) : ''
          const conn = savedMySQL.find((c: any) =>
            c.database === database &&
            (c.host === host || (c.host === 'localhost' && host === '127.0.0.1') || (c.host === '127.0.0.1' && host === 'localhost'))
          )
          if (!conn) { setSaveMsg('❌ MySQL connection not found. Please reconnect in Sources.'); setSaving(false); return }
          formData.append('mysql_host', conn.host)
          formData.append('mysql_port', conn.port)
          formData.append('mysql_user', conn.user)
          formData.append('mysql_password', conn.password)
          formData.append('mysql_database', conn.database)
          formData.append('mysql_ssl', String(conn.ssl))
          formData.append('mysql_table', table)
        }

        else if (srcType === 'postgres') {
          const savedPG = JSON.parse(localStorage.getItem('datapro_postgres_connections') || '[]')
          const atIdx = activeSourceName.lastIndexOf('@')
          const host = atIdx >= 0 ? activeSourceName.slice(atIdx + 1) : ''
          const beforeAt = atIdx >= 0 ? activeSourceName.slice(0, atIdx) : activeSourceName
          const dotIdx = beforeAt.indexOf('.')
          const database = dotIdx >= 0 ? beforeAt.slice(0, dotIdx) : beforeAt
          const table = dotIdx >= 0 ? beforeAt.slice(dotIdx + 1) : ''
          const conn = savedPG.find((c: any) =>
            c.database === database &&
            (c.host === host || (c.host === 'localhost' && host === '127.0.0.1') || (c.host === '127.0.0.1' && host === 'localhost'))
          )
          if (!conn) { setSaveMsg('❌ PostgreSQL connection not found. Please reconnect in Sources.'); setSaving(false); return }
          formData.append('mysql_host', conn.host)
          formData.append('mysql_port', conn.port)
          formData.append('mysql_user', conn.user)
          formData.append('mysql_password', conn.password)
          formData.append('mysql_database', conn.database)
          formData.append('mysql_ssl', String(conn.ssl))
          formData.append('mysql_table', table)
        }

        else if (srcType === 'bigquery') {
          const parts = activeSourceName.split('.')
          const project = parts[0] || ''
          const dataset = parts[1] || ''
          const table = parts[2] || ''
          const savedBQ = JSON.parse(localStorage.getItem('datapro_bigquery_connections') || '[]')
          const conn = savedBQ.find((c: any) => c.project === project && c.dataset === dataset)
          if (!conn) { setSaveMsg('❌ BigQuery connection not found. Please reconnect in Sources.'); setSaving(false); return }
          formData.append('bq_key_json', conn.key_json)
          formData.append('bq_project', project)
          formData.append('bq_dataset', dataset)
          formData.append('bq_table', table)
        }

        else if (srcType === 'mongodb') {
          const atIdx = activeSourceName.lastIndexOf('@')
          const beforeAt = atIdx >= 0 ? activeSourceName.slice(0, atIdx) : activeSourceName
          const dotIdx = beforeAt.indexOf('.')
          const database = dotIdx >= 0 ? beforeAt.slice(0, dotIdx) : beforeAt
          const collection = dotIdx >= 0 ? beforeAt.slice(dotIdx + 1) : ''
          const savedMongo = JSON.parse(localStorage.getItem('datapro_mongo_connections') || '[]')
          const conn = savedMongo.find((c: any) => c.database === database)
          if (!conn) { setSaveMsg('❌ MongoDB connection not found. Please reconnect in Sources.'); setSaving(false); return }
          formData.append('mongo_uri', conn.uri)
          formData.append('mongo_database', conn.database)
          formData.append('mongo_collection', collection)
        }

        else if (srcType === 'aws') {
          const savedAWS = JSON.parse(localStorage.getItem('datapro_aws_connections') || '[]')
          const conn = savedAWS.find((c: any) => {
            if (c.service === 's3') return activeSourceName.startsWith(`s3://${c.bucket}`)
            if (c.service === 'dynamodb') return activeSourceName.includes(`@dynamodb.${c.region}`)
            const atIdx = activeSourceName.lastIndexOf('@')
            const host = atIdx >= 0 ? activeSourceName.slice(atIdx + 1) : ''
            const beforeAt = atIdx >= 0 ? activeSourceName.slice(0, atIdx) : activeSourceName
            const dotIdx = beforeAt.indexOf('.')
            const database = dotIdx >= 0 ? beforeAt.slice(0, dotIdx) : beforeAt
            return c.host === host && c.database === database
          })
          if (!conn) { setSaveMsg('❌ AWS connection not found. Please reconnect in Sources.'); setSaving(false); return }
          formData.append('aws_service', conn.service)
          formData.append('aws_region', conn.region)
          formData.append('aws_access_key_id', conn.accessKeyId)
          formData.append('aws_secret_access_key', conn.secretAccessKey)
          formData.append('aws_bucket', conn.bucket || '')
          formData.append('aws_host', conn.host || '')
          formData.append('aws_port', conn.port || '')
          formData.append('aws_user', conn.user || '')
          formData.append('aws_password', conn.password || '')
          formData.append('aws_database', conn.database || '')
          const atIdx = activeSourceName.lastIndexOf('@')
          const beforeAt = atIdx >= 0 ? activeSourceName.slice(0, atIdx) : activeSourceName
          const dotIdx = beforeAt.indexOf('.')
          const awsTable = conn.service === 's3'
            ? activeSourceName.replace(`s3://${conn.bucket}/`, '')
            : dotIdx >= 0 ? beforeAt.slice(dotIdx + 1) : beforeAt
          formData.append('aws_table', awsTable)
        }

        else if (srcType === 'cloudsql') {
          const savedCSQL = JSON.parse(localStorage.getItem('datapro_cloudsql_connections') || '[]')
          const atIdx = activeSourceName.lastIndexOf('@')
          const beforeAt = atIdx >= 0 ? activeSourceName.slice(0, atIdx) : activeSourceName
          const dotIdx = beforeAt.indexOf('.')
          const database = dotIdx >= 0 ? beforeAt.slice(0, dotIdx) : beforeAt
          const table = dotIdx >= 0 ? beforeAt.slice(dotIdx + 1) : ''
          const projectPart = atIdx >= 0 ? activeSourceName.slice(atIdx + 1).replace('cloudsql.', '') : ''
          const conn = savedCSQL.find((c: any) => c.database === database && c.project === projectPart)
          if (!conn) { setSaveMsg('❌ Cloud SQL connection not found. Please reconnect in Sources.'); setSaving(false); return }
          formData.append('mysql_host', conn.instanceConnectionName)
          formData.append('mysql_port', conn.port)
          formData.append('mysql_user', conn.user)
          formData.append('mysql_password', conn.password)
          formData.append('mysql_database', conn.database)
          formData.append('mysql_ssl', 'true')
          formData.append('mysql_table', table)
          formData.append('db_type', conn.dbType || 'postgres')
        }

        const res = await fetch(`${API_BASE}/api/write-to-source`, { method: 'POST', body: formData })
        const data = await res.json()
        if (data.success) {
          setSaveMsg(`✅ Saved successfully! ${data.rows_written ? `${data.rows_written} rows written.` : ''}`)
        } else {
          setSaveMsg(`❌ Failed: ${data.error || 'Unknown error'}`)
        }
      } catch {
        setSaveMsg('❌ Could not reach backend.')
      } finally {
        setSaving(false)
      }
    }

    return (
      <div className="flex flex-col items-center gap-2">
        <button
          onClick={handleSave}
          disabled={saving}
          className="py-3 px-8 rounded-xl font-bold text-white disabled:opacity-50"
          style={{ background: 'linear-gradient(135deg, #0ea5e9, #0369a1)' }}
        >
          {saving ? '⏳ Saving...' : label}
        </button>
        {saveMsg && (
          <p className="text-xs font-semibold text-center" style={{ color: saveMsg.startsWith('✅') ? '#4ade80' : '#f87171' }}>
            {saveMsg}
          </p>
        )}
      </div>
    )
  }

  function Pill({ children, color }: { children: React.ReactNode; color: string }) {
    return (
      <span className="px-2.5 py-0.5 rounded-full text-xs font-semibold"
        style={{ background: `${color}20`, color }}>
        {children}
      </span>
    )
  }

  function StatCard({ label, value, color }: { label: string; value: string | number; color?: string }) {
    return (
      <div className="rounded-2xl p-5 text-center"
        style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.07)' }}>
        <p className="text-xs mb-2" style={{ color: 'rgba(255,255,255,0.4)' }}>{label}</p>
        <p className="text-2xl font-bold" style={{ color: color || 'white' }}>{value}</p>
      </div>
    )
  }

  // ── Fullscreen Table Modal ────────────────────────────────────────────────────
  function FullscreenTable({
    title,
    subtitle,
    columns,
    rows,
    onClose,
    accentColor = '#a78bfa',
  }: {
    title: string
    subtitle?: string
    columns: string[]
    rows: any[]
    onClose: () => void
    accentColor?: string
  }) {
    useEffect(() => {
      const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
      document.addEventListener('keydown', handler)
      return () => document.removeEventListener('keydown', handler)
    }, [onClose])

    return (
      <div
        className="fixed inset-0 z-[60] flex flex-col"
        style={{ background: '#07050f' }}
      >
        {/* Header */}
        <div
          className="flex items-center justify-between px-6 py-4 flex-shrink-0"
          style={{ borderBottom: '1px solid rgba(255,255,255,0.08)', background: 'rgba(255,255,255,0.02)' }}
        >
          <div className="flex items-center gap-3">
            <div
              className="w-8 h-8 rounded-lg flex items-center justify-center"
              style={{ background: `${accentColor}20`, border: `1px solid ${accentColor}40` }}
            >
              <svg width="14" height="14" fill="none" stroke={accentColor} strokeWidth="2" viewBox="0 0 24 24">
                <rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/>
              </svg>
            </div>
            <div>
              <h2 className="font-bold text-white text-sm">{title}</h2>
              {subtitle && <p className="text-xs mt-0.5" style={{ color: 'rgba(255,255,255,0.4)' }}>{subtitle}</p>}
            </div>
            <span
              className="text-xs px-2.5 py-0.5 rounded-full font-medium ml-2"
              style={{ background: `${accentColor}15`, color: accentColor }}
            >
              {rows.length.toLocaleString()} rows · {columns.length} columns
            </span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs" style={{ color: 'rgba(255,255,255,0.3)' }}>Press ESC to close</span>
            <button
              onClick={onClose}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold ml-2"
              style={{ background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.1)', color: 'rgba(255,255,255,0.6)' }}
            >
              <svg width="12" height="12" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M8 3v3a2 2 0 0 1-2 2H3m18 0h-3a2 2 0 0 1-2-2V3m0 18v-3a2 2 0 0 1 2-2h3M3 16h3a2 2 0 0 1 2 2v3"/>
              </svg>
              Exit Fullscreen
            </button>
          </div>
        </div>

        {/* Table */}
        <div className="flex-1 overflow-auto">
          <table className="w-full text-xs" style={{ color: 'rgba(255,255,255,0.65)', borderCollapse: 'collapse' }}>
            <thead className="sticky top-0" style={{ background: '#0a0714', zIndex: 10 }}>
              <tr>
                <th
                  className="p-3 text-left font-semibold"
                  style={{ borderBottom: '1px solid rgba(255,255,255,0.08)', color: 'rgba(255,255,255,0.3)', width: '52px' }}
                >#</th>
                {columns.map((col) => (
                  <th
                    key={col}
                    className="p-3 text-left font-semibold whitespace-nowrap"
                    style={{ borderBottom: '1px solid rgba(255,255,255,0.08)', color: 'rgba(255,255,255,0.8)' }}
                  >{col}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr
                  key={i}
                  style={{ borderBottom: '1px solid rgba(255,255,255,0.03)' }}
                  onMouseEnter={e => (e.currentTarget.style.background = `${accentColor}08`)}
                  onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
                >
                  <td className="p-3 font-mono" style={{ color: 'rgba(255,255,255,0.2)' }}>{i + 1}</td>
                  {columns.map((col) => (
                    <td key={col} className="p-3 whitespace-nowrap">
                      {row[col] === '' || row[col] === null || row[col] === undefined
                        ? <span style={{ color: '#f87171', fontStyle: 'italic' }}>null</span>
                        : typeof row[col] === 'number'
                          ? row[col].toLocaleString()
                          : String(row[col])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    )
  }

  // ── Edit Flow Source Modal ────────────────────────────────────────────────────
  function EditSourceModal({
    sources,
    activeSourceName,
    activeDestinationName,
    onConfirm,
    onClose,
  }: {
    sources: any[]
    activeSourceName: string
    activeDestinationName: string
    onConfirm: (sourceName: string, destinationName: string) => void
    onClose: () => void
  }) {
    const [selectedSource, setSelectedSource] = useState(activeSourceName)
    const [selectedDest, setSelectedDest]     = useState(activeDestinationName)
    const [expandedSource, setExpandedSource] = useState(false)
    const [expandedDest, setExpandedDest]     = useState(false)

    useEffect(() => {
      if (selectedSource && !activeDestinationName) {
        setSelectedDest(selectedSource.replace('.csv', '') + '_cleaned.csv')
      }
    }, [selectedSource])

    const csvSources = sources.filter(s => {
      const typeStr = (s && s.type) ? String(s.type) : '';
      return typeStr === 'csv' || typeStr === 'catalog' || typeStr === 'sheets' || typeStr === 'ga4' || typeStr === 'databricks' || typeStr === 'rest';
    })

    return (
      <div
        className="fixed inset-0 z-50 flex items-center justify-end"
        style={{ background: 'rgba(0,0,0,0.75)' }}
        onClick={onClose}
      >
        <div
          className="relative flex flex-col h-full shadow-2xl"
          style={{ width: '480px', background: '#0d0a1a', borderLeft: '1px solid rgba(255,255,255,0.1)' }}
          onClick={e => e.stopPropagation()}
        >
          <div
            className="flex items-center justify-between px-6 py-5"
            style={{ borderBottom: '1px solid rgba(255,255,255,0.08)', background: 'rgba(255,255,255,0.01)' }}
          >
            <div>
              <h2 className="font-bold text-white text-base">Edit Flow Source</h2>
              <p className="text-xs mt-0.5" style={{ color: 'rgba(255,255,255,0.4)' }}>
                Switch source dataset and output destination
              </p>
            </div>
            <button onClick={onClose} style={{ color: 'rgba(255,255,255,0.4)', fontSize: '20px', lineHeight: 1 }}>✕</button>
          </div>

          <div className="flex-1 overflow-auto p-6 space-y-6">
            <div>
              <label className="text-xs font-semibold block mb-2" style={{ color: 'rgba(255,255,255,0.5)' }}>
                Source Connection
              </label>
              <div
                className="relative w-full rounded-xl px-4 py-3 flex items-center justify-between cursor-pointer"
                style={{ background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(124,58,237,0.3)' }}
                onClick={() => { setExpandedSource(v => !v); setExpandedDest(false) }}
              >
                <span className="text-sm text-white font-medium">{selectedSource || 'Select source…'}</span>
                <div className="flex items-center gap-2">
                  <button
                    onClick={e => { e.stopPropagation(); setSelectedSource(activeSourceName) }}
                    className="w-7 h-7 rounded-lg flex items-center justify-center"
                    style={{ background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.1)' }}
                    title="Reset to current"
                  >
                    <svg width="12" height="12" fill="none" stroke="rgba(255,255,255,0.5)" strokeWidth="2" viewBox="0 0 24 24">
                      <polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
                    </svg>
                  </button>
                  <svg
                    width="14" height="14" fill="none" stroke="rgba(255,255,255,0.4)" strokeWidth="2" viewBox="0 0 24 24"
                    style={{ transform: expandedSource ? 'rotate(180deg)' : 'none', transition: 'transform 0.2s' }}
                  >
                    <polyline points="6 9 12 15 18 9"/>
                  </svg>
                </div>
              </div>

              {expandedSource && (
                <div
                  className="mt-1 rounded-xl overflow-hidden"
                  style={{ background: '#110d20', border: '1px solid rgba(124,58,237,0.3)', boxShadow: '0 8px 32px rgba(0,0,0,0.5)' }}
                >
                  {csvSources.length === 0 ? (
                    <div className="px-4 py-3 text-xs" style={{ color: 'rgba(255,255,255,0.35)' }}>No sources available</div>
                  ) : csvSources.map(s => (
                    <div
                      key={s.id}
                      onClick={() => { setSelectedSource(s.name); setExpandedSource(false) }}
                      className="flex items-center gap-3 px-4 py-3 cursor-pointer"
                      style={{
                        background: selectedSource === s.name ? 'rgba(124,58,237,0.15)' : 'transparent',
                        borderBottom: '1px solid rgba(255,255,255,0.05)',
                      }}
                      onMouseEnter={e => { if (selectedSource !== s.name) e.currentTarget.style.background = 'rgba(255,255,255,0.04)' }}
                      onMouseLeave={e => { if (selectedSource !== s.name) e.currentTarget.style.background = 'transparent' }}
                    >
                      <span className="text-base">
                        {s.type === 'catalog' ? '🗂️' : s.type === 'sheets' ? '📊' : s.type === 'ga4' ? '📈' : s.type === 'databricks' ? '🧱' : s.type === 'rest' ? '🌐' : '📄'}
                      </span>
                      <div className="flex-1 min-w-0">
                        <p className="text-sm font-medium text-white truncate">{s.name}</p>
                        <p className="text-xs" style={{ color: 'rgba(255,255,255,0.35)' }}>{s.type.toUpperCase()} · Added {s.addedAt}</p>
                      </div>
                      {selectedSource === s.name && (
                        <svg width="14" height="14" fill="none" stroke="#a78bfa" strokeWidth="2.5" viewBox="0 0 24 24">
                          <polyline points="20 6 9 17 4 12"/>
                        </svg>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div>
              <label className="text-xs font-semibold block mb-2" style={{ color: 'rgba(255,255,255,0.5)' }}>
                📤 Destination / Output Name
              </label>
              <div
                className="relative w-full rounded-xl px-4 py-3 flex items-center justify-between cursor-pointer"
                style={{ background: 'rgba(52,211,153,0.08)', border: '1px solid rgba(52,211,153,0.25)' }}
                onClick={() => { setExpandedDest(v => !v); setExpandedSource(false) }}
              >
                <span className="text-sm text-white font-medium">{selectedDest || 'Select destination…'}</span>
                <svg
                  width="14" height="14" fill="none" stroke="rgba(255,255,255,0.4)" strokeWidth="2" viewBox="0 0 24 24"
                  style={{ transform: expandedDest ? 'rotate(180deg)' : 'none', transition: 'transform 0.2s' }}
                >
                  <polyline points="6 9 12 15 18 9"/>
                </svg>
              </div>

              {expandedDest && (
                <div
                  className="mt-1 rounded-xl overflow-hidden"
                  style={{ background: '#110d20', border: '1px solid rgba(52,211,153,0.3)', boxShadow: '0 8px 32px rgba(0,0,0,0.5)' }}
                >
                  {!csvSources.some(s => s.name === selectedDest) && selectedDest && (
                    <div
                      onClick={() => setExpandedDest(false)}
                      className="flex items-center gap-3 px-4 py-3 cursor-pointer"
                      style={{ background: 'rgba(52,211,153,0.15)', borderBottom: '1px solid rgba(255,255,255,0.05)' }}
                    >
                      <span className="text-base">✨</span>
                      <div className="flex-1 min-w-0">
                        <p className="text-sm font-medium text-white truncate">{selectedDest}</p>
                        <p className="text-xs" style={{ color: 'rgba(255,255,255,0.35)' }}>Generated Output</p>
                      </div>
                      <svg width="14" height="14" fill="none" stroke="#34d399" strokeWidth="2.5" viewBox="0 0 24 24">
                        <polyline points="20 6 9 17 4 12"/>
                      </svg>
                    </div>
                  )}
                  {csvSources.map(s => (
                    <div
                      key={s.id}
                      onClick={() => { setSelectedDest(s.name); setExpandedDest(false) }}
                      className="flex items-center gap-3 px-4 py-3 cursor-pointer"
                      style={{
                        background: selectedDest === s.name ? 'rgba(52,211,153,0.15)' : 'transparent',
                        borderBottom: '1px solid rgba(255,255,255,0.05)',
                      }}
                      onMouseEnter={e => { if (selectedDest !== s.name) e.currentTarget.style.background = 'rgba(255,255,255,0.04)' }}
                      onMouseLeave={e => { if (selectedDest !== s.name) e.currentTarget.style.background = 'transparent' }}
                    >
                      <span className="text-base">{s.type === 'catalog' ? '🗂️' : '📄'}</span>
                      <div className="flex-1 min-w-0">
                        <p className="text-sm font-medium text-white truncate">{s.name}</p>
                        <p className="text-xs" style={{ color: 'rgba(255,255,255,0.35)' }}>{s.type.toUpperCase()}</p>
                      </div>
                      {selectedDest === s.name && (
                        <svg width="14" height="14" fill="none" stroke="#34d399" strokeWidth="2.5" viewBox="0 0 24 24">
                          <polyline points="20 6 9 17 4 12"/>
                        </svg>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>

            {selectedSource && (
              <div className="rounded-xl p-4" style={{ background: 'rgba(124,58,237,0.08)', border: '1px solid rgba(124,58,237,0.2)' }}>
                <div className="flex items-center gap-2 mb-2">
                  <div className="w-2 h-2 rounded-full animate-pulse" style={{ background: '#a78bfa' }} />
                  <p className="text-xs font-semibold text-white">Active Source</p>
                </div>
                <p className="text-sm font-bold text-white">{selectedSource}</p>
                {selectedDest && (
                  <p className="text-xs mt-1" style={{ color: 'rgba(255,255,255,0.4)' }}>
                    → Output: <span style={{ color: '#34d399' }}>{selectedDest}</span>
                  </p>
                )}
                {selectedSource !== activeSourceName && (
                  <p
                    className="text-xs mt-2 px-2 py-1 rounded-lg inline-block"
                    style={{ background: 'rgba(234,179,8,0.1)', color: '#fbbf24', border: '1px solid rgba(234,179,8,0.2)' }}
                  >
                    ⚡ Will re-run analysis with new source
                  </p>
                )}
              </div>
            )}
          </div>

          <div
            className="px-6 py-5 flex gap-3"
            style={{ borderTop: '1px solid rgba(255,255,255,0.08)', background: 'rgba(255,255,255,0.01)' }}
          >
            <button
              onClick={onClose}
              className="flex-1 py-2.5 rounded-xl font-semibold text-sm"
              style={{ background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.08)', color: 'rgba(255,255,255,0.5)' }}
            >
              CANCEL
            </button>
            <button
              onClick={() => onConfirm(selectedSource, selectedDest)}
              disabled={!selectedSource}
              className="flex-1 py-2.5 rounded-xl font-bold text-sm text-white disabled:opacity-40"
              style={{ background: 'linear-gradient(135deg, #7c3aed, #2563eb)' }}
            >
              OK
            </button>
          </div>
        </div>
      </div>
    )
  }

  // ── AI Chat Clean Component ───────────────────────────────────────────────────
  function AIChatClean({
    colInfo,
    sampleData,
    // _activeFile,
    activeSourceName,
    setCustomCleanOps,
    setOptions,
  }: {
    colInfo: any[]
    sampleData: any[]
    activeFile: File | null
    activeSourceName: string
    setCustomCleanOps: React.Dispatch<React.SetStateAction<{key: string, label: string, instruction: string}[]>>
    setOptions: React.Dispatch<React.SetStateAction<Record<string, boolean>>>
  }) {
    void colInfo
    const [messages, setMessages] = useState<{ role: 'user' | 'assistant'; content: string }[]>([
      {
        role: 'assistant',
        content: `Hi! I'm your AI data cleaning assistant. Tell me what you'd like to do with **${activeSourceName}** and I'll generate Python or SQL code to clean your data.\n\nTry commands like:\n- "remove all rows where age is over 100"\n- "fill missing salary with the median salary per department"\n- "flag rows where revenue is negative"\n- "remove duplicates based on email column"`,
      },
    ])
    const [input, setInput] = useState('')
    const [loading, setLoading] = useState(false)
    const [copiedIdx, setCopiedIdx] = useState<number | null>(null)
    const bottomRef = useRef<HTMLDivElement>(null)

    useEffect(() => {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    }, [messages])

    const sampleContext = sampleData.slice(0, 3).map((row, i) =>
      `Row ${i + 1}: ${JSON.stringify(row)}`
    ).join('\n')

    const sendMessage = async () => {
      const trimmed = input.trim()
      if (!trimmed || loading) return

      const userMsg = { role: 'user' as const, content: trimmed }
      const newMessages = [...messages, userMsg]
      setMessages(newMessages)
      setInput('')
      setLoading(true)

      try {
        // Check plan and AI usage gate
        const token = localStorage.getItem('datapro_token')
        if (!token) {
          setMessages(prev => [...prev, { role: 'assistant', content: '⚠️ You are not logged in. Please log in and subscribe to use AI features.' }])
          setLoading(false)
          return
        }
        const gateRes = await fetch(`${API_BASE}/api/ai-usage`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }
        })
        const gateData = await gateRes.json()
        if (!gateRes.ok) {
          const isNoPlan = gateData.error === 'no_plan'
          const isLimit = gateData.error === 'limit_reached'
          setMessages(prev => [...prev, {
            role: 'assistant',
            content: isNoPlan
              ? `🔒 **AI access requires a paid plan.**\n\nYou are on the free tier. [Upgrade your plan](/pricing) to use AI-powered cleaning.\n\nClick **Upgrade** in the sidebar or go to /pricing.`
              : isLimit
              ? `⚡ **Daily AI limit reached (50/day).**\n\nYou've used all 50 AI queries for today on the Starter plan. Upgrade to Pro for unlimited AI access.\n\n[View plans →](/pricing)`
              : `⚠️ ${gateData.message || 'AI unavailable'}`
          }])
          setLoading(false)
          return
        }

const response = await fetch(`${API_BASE}/api/ai-clean`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
  body: JSON.stringify({
    instructions: trimmed,
    csv_preview: sampleContext,
  }),
})
const aiData = await response.json()
if (aiData.success) {
  const opKey = `ai_custom_${Date.now()}`
  const newOp = {
    key: opKey,
    label: trimmed,
    instruction: trimmed,
  }
  setCustomCleanOps(prev => [...prev, newOp])
  setOptions(prev => ({ ...prev, [opKey]: true }))
}
const assistantText = aiData.success
  ? `${aiData.explanation}\n\n✅ Added as a new cleaning step below — click **Run Cleaning** to apply!`
  : '⚠️ ' + (aiData.error || 'AI unavailable')
        setMessages(prev => [...prev, { role: 'assistant', content: assistantText }])
      } catch (err) {
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: '⚠️ Could not reach the AI. Make sure you are connected and the API is available.',
        }])
      } finally {
        setLoading(false)
      }
    }

    const copyCode = (text: string, idx: number) => {
      navigator.clipboard.writeText(text)
      setCopiedIdx(idx)
      setTimeout(() => setCopiedIdx(null), 2000)
    }

    // Simple markdown-ish renderer for code blocks
    const renderMessage = (content: string, msgIdx: number) => {
      const parts = content.split(/(```[\s\S]*?```)/g)
      return parts.map((part, i) => {
        if (part.startsWith('```')) {
          const lines = part.slice(3, -3).split('\n')
          const lang = lines[0].trim()
          const code = lines.slice(1).join('\n')
          return (
            <div key={i} className="relative my-3 rounded-xl overflow-hidden"
              style={{ background: 'rgba(0,0,0,0.5)', border: '1px solid rgba(255,255,255,0.08)' }}>
              <div className="flex items-center justify-between px-4 py-2"
                style={{ background: 'rgba(255,255,255,0.04)', borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
                <span className="text-xs font-bold" style={{ color: '#a78bfa' }}>{lang || 'code'}</span>
                <button
                  onClick={() => copyCode(code, msgIdx * 100 + i)}
                  className="text-xs px-2 py-0.5 rounded-md font-semibold transition-all"
                  style={{
                    background: copiedIdx === msgIdx * 100 + i ? 'rgba(34,197,94,0.2)' : 'rgba(255,255,255,0.08)',
                    color: copiedIdx === msgIdx * 100 + i ? '#4ade80' : 'rgba(255,255,255,0.5)',
                    border: '1px solid rgba(255,255,255,0.1)',
                  }}>
                  {copiedIdx === msgIdx * 100 + i ? '✓ Copied' : 'Copy'}
                </button>
              </div>
              <pre className="p-4 text-xs overflow-auto leading-relaxed"
                style={{ color: '#a5f3fc', fontFamily: "'JetBrains Mono','Fira Code',monospace", margin: 0 }}>
                {code}
              </pre>
            </div>
          )
        }
        // Render plain text with basic bold (**text**) support
        return (
          <p key={i} className="text-sm leading-relaxed whitespace-pre-wrap"
            style={{ color: 'rgba(255,255,255,0.75)' }}
            dangerouslySetInnerHTML={{
              __html: part
                .replace(/\*\*(.*?)\*\*/g, '<strong style="color:white">$1</strong>')
                .replace(/`(.*?)`/g, '<code style="background:rgba(255,255,255,0.08);padding:1px 5px;border-radius:4px;font-size:11px;color:#c4b5fd">$1</code>'),
            }}
          />
        )
      })
    }

    const QUICK_COMMANDS = [
      'Remove duplicate rows',
      'Fill missing values with column median',
      'Flag outliers in numeric columns',
      'Remove rows with more than 50% missing data',
    ]

    return (
      <div className="flex flex-col" style={{ height: '600px' }}>
        {/* Info banner */}
        <div className="flex items-start gap-3 mb-4 px-4 py-3 rounded-xl"
          style={{ background: 'rgba(124,58,237,0.08)', border: '1px solid rgba(124,58,237,0.2)' }}>
          <span style={{ fontSize: '16px' }}>✨</span>
          <div>
            <p className="text-xs font-bold text-white mb-0.5">AI-Powered Natural Language Cleaning</p>
            <p className="text-xs" style={{ color: 'rgba(255,255,255,0.45)' }}>
              Describe what you want in plain English. The AI understands your dataset schema and generates ready-to-run Python & SQL code.
            </p>
          </div>
        </div>

        {/* Quick command pills */}
        <div className="flex flex-wrap gap-2 mb-4">
          {QUICK_COMMANDS.map(cmd => (
            <button key={cmd} onClick={() => setInput(cmd)}
              className="text-xs px-3 py-1.5 rounded-full font-medium transition-all"
              style={{ background: 'rgba(124,58,237,0.1)', border: '1px solid rgba(124,58,237,0.25)', color: '#a78bfa' }}
              onMouseEnter={e => (e.currentTarget.style.background = 'rgba(124,58,237,0.2)')}
              onMouseLeave={e => (e.currentTarget.style.background = 'rgba(124,58,237,0.1)')}>
              {cmd}
            </button>
          ))}
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-auto space-y-4 pr-1" style={{ minHeight: 0 }}>
          {messages.map((msg, idx) => (
            <div key={idx} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
              {msg.role === 'assistant' && (
                <div className="w-7 h-7 rounded-lg flex items-center justify-center flex-shrink-0 mr-3 mt-1"
                  style={{ background: 'linear-gradient(135deg, #7c3aed, #2563eb)', fontSize: '12px' }}>
                  ✨
                </div>
              )}
              <div className="max-w-[85%] rounded-2xl px-4 py-3"
                style={{
                  background: msg.role === 'user'
                    ? 'linear-gradient(135deg, rgba(124,58,237,0.3), rgba(37,99,235,0.3))'
                    : 'rgba(255,255,255,0.04)',
                  border: msg.role === 'user'
                    ? '1px solid rgba(124,58,237,0.4)'
                    : '1px solid rgba(255,255,255,0.08)',
                }}>
                {msg.role === 'user'
                  ? <p className="text-sm text-white">{msg.content}</p>
                  : renderMessage(msg.content, idx)}
              </div>
            </div>
          ))}
          {loading && (
            <div className="flex justify-start">
              <div className="w-7 h-7 rounded-lg flex items-center justify-center flex-shrink-0 mr-3 mt-1"
                style={{ background: 'linear-gradient(135deg, #7c3aed, #2563eb)', fontSize: '12px' }}>✨</div>
              <div className="px-4 py-3 rounded-2xl" style={{ background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.08)' }}>
                <div className="flex items-center gap-2">
                  <div className="flex gap-1">
                    {[0,1,2].map(i => (
                      <div key={i} className="w-1.5 h-1.5 rounded-full animate-bounce"
                        style={{ background: '#a78bfa', animationDelay: `${i * 0.15}s` }} />
                    ))}
                  </div>
                  <span className="text-xs" style={{ color: 'rgba(255,255,255,0.4)' }}>Analyzing your data...</span>
                </div>
              </div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        {/* Input */}
        <div className="mt-4 flex gap-2">
          <input
            type="text"
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && !e.shiftKey && sendMessage()}
            placeholder='e.g. "remove rows where age > 100" or "fill missing salary with median"'
            className="flex-1 text-sm text-white rounded-xl px-4 py-3 outline-none"
            style={{ background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(124,58,237,0.3)' }}
            disabled={loading}
          />
          <button
            onClick={sendMessage}
            disabled={loading || !input.trim()}
            className="px-5 py-3 rounded-xl font-bold text-white text-sm disabled:opacity-40 flex items-center gap-2"
            style={{ background: 'linear-gradient(135deg, #7c3aed, #2563eb)', flexShrink: 0 }}>
            <svg width="14" height="14" fill="none" stroke="white" strokeWidth="2.5" viewBox="0 0 24 24">
              <line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>
            </svg>
            Send
          </button>
        </div>
      </div>
    )
  }

  export default function Analysis() {
    const navigate = useNavigate()
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    const {
      activeFile, activeSourceName, analysis, setAnalysis,
      analysisTab, setAnalysisTab,
      alertConditions, addNotifications, addAuditLog,
      pendingCleanOptions, setPendingCleanOptions, notifyEmail,
      sources, switchActiveSource,
      activeDestinationName, setActiveDestinationName,
  pipelineOutputFile,
  } = useStore()

    const [analyzing, setAnalyzing]         = useState(false)
    const [cleaning, setCleaning]           = useState(false)
    const [result, setResult]               = useState<any>(null)
    const [filterIssue, setFilterIssue]     = useState('All')
    const [filterCol, setFilterCol]         = useState('All')
    const [showOriginal, setShowOriginal]   = useState(false)
    const [showSQL, setShowSQL]             = useState(false)
    const [showEditSource, setShowEditSource] = useState(false)
    const [imputationMethod, setImputationMethod] = useState('none')
    const [options, setOptions] = useState<Record<string, boolean>>({
  remove_duplicates: false, remove_empty_rows: false, standardize_text: false,
  fix_dates: false, handle_outliers: false, fill_missing: false
})
const [customCleanOps, setCustomCleanOps] = useState<{key: string, label: string, instruction: string}[]>([])

    // Fullscreen state
    const [fullscreenTable, setFullscreenTable] = useState<{
      title: string
      subtitle?: string
      columns: string[]
      rows: any[]
      accentColor?: string
    } | null>(null)

    // Track which source the current analysis belongs to
    const analysisSourceRef = useRef<string>('')
    // Track if a re-run was triggered by a source switch (avoid double-running)
    // const switchTriggeredRef = useRef(false)

    const liveSQLPreview = useMemo(() => {
      const tableName = activeSourceName?.replace('.csv', '').replace(/[^a-zA-Z0-9_]/g, '_') || 'raw_dataset'
      if (!Object.values(options).some(Boolean) && imputationMethod === 'none') {
        return `SELECT * FROM ${tableName};`
      }
      let sql = `WITH Step0_Base AS (\n  SELECT * FROM ${tableName}\n)`
      let lastStep = 'Step0_Base'
      let step = 1

      if (options.remove_duplicates) {
        const n = `Step${step++}_Deduplicated`
        sql += `,\n\n${n} AS (\n  SELECT DISTINCT *\n  FROM ${lastStep}\n)`
        lastStep = n
      }
      if (options.remove_empty_rows) {
        const n = `Step${step++}_NoEmptyRows`
        sql += `,\n\n${n} AS (\n  SELECT *\n  FROM ${lastStep}\n  WHERE NOT (/* all cols IS NULL */)\n)`
        lastStep = n
      }
      if (options.standardize_text) {
        const n = `Step${step++}_Standardized`
        sql += `,\n\n${n} AS (\n  SELECT\n    * EXCEPT (text_col),\n    LOWER(TRIM(text_col)) AS text_col\n  FROM ${lastStep}\n)`
        lastStep = n
      }
      if (options.handle_outliers) {
        const n = `Step${step++}_OutliersCapped`
        sql += `,\n\n${n} AS (\n  SELECT\n    * EXCEPT (numeric_col),\n    CASE\n      WHEN numeric_col > upper_bound THEN upper_bound\n      WHEN numeric_col < lower_bound THEN lower_bound\n      ELSE numeric_col\n    END AS numeric_col\n  FROM ${lastStep}\n)`
        lastStep = n
      }
      if (options.fill_missing || imputationMethod !== 'none') {
        const method = imputationMethod !== 'none' ? imputationMethod : 'median/mode'
        const n = `Step${step++}_Imputed`
        sql += `,\n\n${n} AS (\n  SELECT\n    * EXCEPT (col),\n    COALESCE(col, /* ${method} of col */) AS col\n  FROM ${lastStep}\n)`
        lastStep = n
      }
      if (options.fix_dates) {
        const n = `Step${step++}_FixedDates`
        sql += `,\n\n${n} AS (\n  SELECT\n    * EXCEPT (date_col),\n    TRY_CAST(date_col AS DATE) AS date_col\n  FROM ${lastStep}\n)`
        lastStep = n
      }
      sql += `\n\nSELECT * FROM ${lastStep};`
      return sql
    }, [activeSourceName, options, imputationMethod])

    useEffect(() => {
      if (!document.getElementById('sparkle-css')) {
        const style = document.createElement('style')
        style.id = 'sparkle-css'
        style.textContent = sparkleCSS
        document.head.appendChild(style)
      }
      if (!document.getElementById('razorpay-checkout-js')) {
        const s = document.createElement('script')
        s.id = 'razorpay-checkout-js'
        s.src = 'https://checkout.razorpay.com/v1/checkout.js'
        document.head.appendChild(s)
      }
    }, [])

    // ── Re-run analysis when source changes OR analysis is cleared ──────────
  useEffect(() => {
      const sourceObj = sources.find(s => s.name === activeSourceName)
      const hasSource = pipelineOutputFile || activeFile || sourceObj?.file || activeSourceName
      if (!hasSource) return
      if (analyzing) return

      // First load only — no analysis yet and ref is empty
      if (analysis === null && analysisSourceRef.current === '') {
        runAnalysis()
        return
      }

      // Source explicitly switched — ref will differ from current name
      if (analysisSourceRef.current !== '' && analysisSourceRef.current !== activeSourceName) {
        runAnalysis()
      }
    }, [activeSourceName, pipelineOutputFile])
    // this effect fires and re-runs automatically.

    // ── Scan ALL connected sources for notifications (throttled to 1/hour) ──
    const allSourcesScanStartedRef = useRef(false)
    useEffect(() => {
      if (allSourcesScanStartedRef.current) return
      if (sources.length === 0) return
      const lastScan = useStore.getState().lastSourcesScanAt || 0
      const ONE_HOUR = 60 * 60 * 1000
      if (Date.now() - lastScan < ONE_HOUR) return
      allSourcesScanStartedRef.current = true
      useStore.getState().setLastSourcesScanAt(Date.now())
      scanAllSourcesForNotifications()
    }, [sources.length])

    const fetchSourceAsCSVFile = useCallback(async (src: any): Promise<File | null> => {
      try {
        if (src.type === 'csv' || src.type === 'sheets') {
          return src.file || null
        }
        if (src.type === 'rest') {
          const savedRestMeta = JSON.parse(localStorage.getItem('datapro_rest_connections') || '[]')
          const withoutProto = src.name.replace('rest:', '')
          const conn = savedRestMeta.find((c: any) => {
            const connHost = (c.base_url || '').replace(/https?:\/\//, '').replace(/\/$/, '')
            return withoutProto.startsWith(connHost)
          })
          if (!conn) return null
          const connHost = (conn.base_url || '').replace(/https?:\/\//, '').replace(/\/$/, '')
          const endpoint = withoutProto.slice(connHost.length) || '/'
          const fetchRes = await fetch(`${API_BASE}/api/rest/fetch-endpoint`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              base_url: conn.base_url, endpoint, headers: conn.headers || {},
              verify_ssl: conn.verify_ssl, data_path: conn.data_path || '',
              method: conn.method || 'GET', timeout: conn.timeout || 30,
            }),
          })
          const fetchData = await fetchRes.json()
          if (!fetchData.download_url) return null
          const csvRes = await fetch(`${API_BASE}${fetchData.download_url}`)
          const blob = await csvRes.blob()
          return new File([blob], `${src.name}.csv`, { type: 'text/csv' })
        }
        if (src.type === 'aws') {
          const savedAWS = JSON.parse(localStorage.getItem('datapro_aws_connections') || '[]')
          const srcName = src.name
          const awsConn = savedAWS.find((c: any) => {
            if (c.service === 's3') return srcName.startsWith(`s3://${c.bucket}`)
            if (c.service === 'dynamodb') return srcName.includes(`@dynamodb.${c.region}`)
            if (c.service === 'cost-explorer') return srcName.startsWith('cost-explorer:') && srcName.includes(`@${c.region}`)
            const atIdx = srcName.lastIndexOf('@')
            const host = atIdx >= 0 ? srcName.slice(atIdx + 1) : ''
            const beforeAt = atIdx >= 0 ? srcName.slice(0, atIdx) : srcName
            const dotIdx = beforeAt.indexOf('.')
            const database = dotIdx >= 0 ? beforeAt.slice(0, dotIdx) : beforeAt
            return c.host === host && c.database === database
          })
          if (!awsConn) return null
          let awsTable = ''
          if (awsConn.service === 's3') awsTable = srcName.replace(`s3://${awsConn.bucket}/`, '')
          else if (awsConn.service === 'dynamodb') awsTable = srcName.split('@')[0]
          else if (awsConn.service === 'cost-explorer') awsTable = srcName.replace('cost-explorer:', '').split('@')[0]
          else {
            const atIdx = srcName.lastIndexOf('@')
            const beforeAt = atIdx >= 0 ? srcName.slice(0, atIdx) : srcName
            const dotIdx = beforeAt.indexOf('.')
            awsTable = dotIdx >= 0 ? beforeAt.slice(dotIdx + 1) : beforeAt
          }
          const fetchRes = await fetch(`${API_BASE}/api/aws/fetch-table`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ...awsConn, service: awsConn.service, table: awsTable }),
          })
          const fetchData = await fetchRes.json()
          if (!fetchData.download_url) return null
          const csvRes = await fetch(`${API_BASE}${fetchData.download_url}`)
          const blob = await csvRes.blob()
          return new File([blob], `${src.name}.csv`, { type: 'text/csv' })
        }
        if ((src.type as string) === 'databricks') {
          const savedDB = JSON.parse(localStorage.getItem('datapro_databricks_connections') || '[]')
          const atIdx = src.name.lastIndexOf('@')
          const srcHost = atIdx >= 0 ? src.name.slice(atIdx + 1) : ''
          const beforeAt = atIdx >= 0 ? src.name.slice(0, atIdx) : src.name
          const parts = beforeAt.split('.')
          const catalog = parts.length >= 3 ? parts[0] : 'hive_metastore'
          const schema  = parts.length >= 3 ? parts[1] : (parts[0] || 'default')
          const table   = parts.length >= 3 ? parts[2] : (parts[1] || '')
          const dbConn = savedDB.find((c: any) => c.host === srcHost)
          if (!dbConn) return null
          const fetchRes = await fetch(`${API_BASE}/api/databricks/fetch-table`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ host: dbConn.host, token: dbConn.token, http_path: dbConn.http_path, catalog, schema, table }),
          })
          const fetchData = await fetchRes.json()
          if (!fetchData.download_url) return null
          const csvRes = await fetch(`${API_BASE}${fetchData.download_url}`)
          const blob = await csvRes.blob()
          return new File([blob], `${src.name}.csv`, { type: 'text/csv' })
        }
        if ((src.type as string) === 'ga4') {
          const savedGA4Meta = JSON.parse(localStorage.getItem('datapro_ga4_connections') || '[]')
          const withoutProto = src.name.replace('ga4:', '')
          const slashIdx = withoutProto.indexOf('/')
          const propertyId = slashIdx >= 0 ? withoutProto.slice(0, slashIdx) : withoutProto
          const reportName = slashIdx >= 0 ? withoutProto.slice(slashIdx + 1) : ''
          const ga4Conn = savedGA4Meta.find((c: any) => c.propertyId === propertyId)
          if (!ga4Conn) return null
          const fetchRes = await fetch(`${API_BASE}/api/ga4/fetch-report`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              key_json: ga4Conn.keyJson, property_id: ga4Conn.propertyId,
              metrics: ga4Conn.metrics || 'sessions,activeUsers,newUsers,screenPageViews',
              dimensions: ga4Conn.dimensions || 'date,sessionDefaultChannelGroup',
              start_date: ga4Conn.startDate || '30daysAgo', end_date: ga4Conn.endDate || 'today',
              report_name: reportName,
            }),
          })
          const fetchData = await fetchRes.json()
          if (!fetchData.download_url) return null
          const csvRes = await fetch(`${API_BASE}${fetchData.download_url}`)
          const blob = await csvRes.blob()
          return new File([blob], `${src.name}.csv`, { type: 'text/csv' })
        }
        if ((src.type as string) === 'gcs') {
          const savedGCSMeta = JSON.parse(localStorage.getItem('datapro_gcs_connections') || '[]')
          const withoutProto = src.name.replace('gcs://', '')
          const slashIdx = withoutProto.indexOf('/')
          const bucket = slashIdx >= 0 ? withoutProto.slice(0, slashIdx) : withoutProto
          const file = slashIdx >= 0 ? withoutProto.slice(slashIdx + 1) : ''
          const conn = savedGCSMeta.find((c: any) => String(c.bucket) === bucket)
          if (!conn) return null
          const fetchRes = await fetch(`${API_BASE}/api/gcs/fetch-file`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key_json: conn.keyJson, project: conn.project, bucket, file }),
          })
          const fetchData = await fetchRes.json()
          if (!fetchData.download_url) return null
          const csvRes = await fetch(`${API_BASE}${fetchData.download_url}`)
          const blob = await csvRes.blob()
          return new File([blob], `${src.name}.csv`, { type: 'text/csv' })
        }
        if ((src.type as string) === 'cloudsql') {
          const savedCSQLMeta = JSON.parse(localStorage.getItem('datapro_cloudsql_connections') || '[]')
          const atIdx = src.name.lastIndexOf('@')
          const beforeAt = atIdx >= 0 ? src.name.slice(0, atIdx) : src.name
          const dotIdx = beforeAt.indexOf('.')
          const database = dotIdx >= 0 ? beforeAt.slice(0, dotIdx) : beforeAt
          const table = dotIdx >= 0 ? beforeAt.slice(dotIdx + 1) : ''
          const projectPart = atIdx >= 0 ? src.name.slice(atIdx + 1).replace('cloudsql.', '') : ''
          const conn = savedCSQLMeta.find((c: any) => c.database === database && c.project === projectPart)
          if (!conn) return null
          const endpoint = conn.dbType === 'mysql' ? '/api/mysql/fetch-table' : '/api/postgres/fetch-table'
          const fetchRes = await fetch(`${API_BASE}${endpoint}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              host: conn.instanceConnectionName, port: conn.port, user: conn.user,
              password: conn.password, database: conn.database, ssl: true, table,
            }),
          })
          const fetchData = await fetchRes.json()
          if (!fetchData.download_url) return null
          const csvRes = await fetch(`${API_BASE}${fetchData.download_url}`)
          const blob = await csvRes.blob()
          return new File([blob], `${src.name}.csv`, { type: 'text/csv' })
        }
        return null
      } catch {
        return null
      }
    }, [])

    const scanAllSourcesForNotifications = useCallback(async () => {
      const savedEmail = localStorage.getItem('datapro_alert_email') || notifyEmail

      for (const src of sources) {
        try {
          const formData = new FormData()
          let usesDirectSourceType = false

          if (src.type === 'csv' || src.type === 'sheets') {
            if (!src.file) continue
            formData.append('file', src.file)
          } else if (src.type === 'mysql') {
            const savedMySQL = JSON.parse(localStorage.getItem('datapro_mysql_connections') || '[]')
            const atIndex = src.name.lastIndexOf('@')
            const host = atIndex !== -1 ? src.name.slice(atIndex + 1) : ''
            const beforeAt = atIndex !== -1 ? src.name.slice(0, atIndex) : src.name
            const dotIndex = beforeAt.indexOf('.')
            const database = dotIndex !== -1 ? beforeAt.slice(0, dotIndex) : beforeAt
            const table = dotIndex !== -1 ? beforeAt.slice(dotIndex + 1) : ''
            const conn = savedMySQL.find((c: any) =>
              c.database === database && (c.host === host || (c.host === 'localhost' && host === '127.0.0.1') || (c.host === '127.0.0.1' && host === 'localhost'))
            )
            if (!conn) continue
            usesDirectSourceType = true
            formData.append('source_type', 'mysql')
            formData.append('mysql_host', conn.host)
            formData.append('mysql_port', conn.port)
            formData.append('mysql_user', conn.user)
            formData.append('mysql_password', conn.password)
            formData.append('mysql_database', conn.database)
            formData.append('mysql_ssl', String(conn.ssl))
            formData.append('mysql_table', table)
          } else if (src.type === 'postgres') {
            const savedPG = JSON.parse(localStorage.getItem('datapro_postgres_connections') || '[]')
            const atIndex = src.name.lastIndexOf('@')
            const host = atIndex !== -1 ? src.name.slice(atIndex + 1) : ''
            const beforeAt = atIndex !== -1 ? src.name.slice(0, atIndex) : src.name
            const dotIndex = beforeAt.indexOf('.')
            const database = dotIndex !== -1 ? beforeAt.slice(0, dotIndex) : beforeAt
            const table = dotIndex !== -1 ? beforeAt.slice(dotIndex + 1) : ''
            const conn = savedPG.find((c: any) =>
              c.database === database && (c.host === host || (c.host === 'localhost' && host === '127.0.0.1') || (c.host === '127.0.0.1' && host === 'localhost'))
            )
            if (!conn) continue
            usesDirectSourceType = true
            formData.append('source_type', 'postgres')
            formData.append('mysql_host', conn.host)
            formData.append('mysql_port', conn.port)
            formData.append('mysql_user', conn.user)
            formData.append('mysql_password', conn.password)
            formData.append('mysql_database', conn.database)
            formData.append('mysql_ssl', String(conn.ssl))
            formData.append('mysql_table', table)
          } else if (src.type === 'mongodb') {
            const savedMongo = JSON.parse(localStorage.getItem('datapro_mongo_connections') || '[]')
            const atIdx = src.name.lastIndexOf('@')
            const beforeAt = atIdx >= 0 ? src.name.slice(0, atIdx) : src.name
            const dotIdx = beforeAt.indexOf('.')
            const database = dotIdx >= 0 ? beforeAt.slice(0, dotIdx) : beforeAt
            const collection = dotIdx >= 0 ? beforeAt.slice(dotIdx + 1) : ''
            const conn = savedMongo.find((c: any) => c.database === database)
            if (!conn) continue
            usesDirectSourceType = true
            formData.append('source_type', 'mongodb')
            formData.append('mysql_host', conn.uri)
            formData.append('mysql_user', conn.database)
            formData.append('mysql_database', conn.database)
            formData.append('mysql_table', collection || conn.collection || conn.collections?.[0] || '')
          } else if (src.type === 'bigquery') {
            const parts = src.name.split('.')
            const project = parts[0] || ''
            const dataset = parts[1] || ''
            const table = parts[2] || ''
            const savedBQ = JSON.parse(localStorage.getItem('datapro_bigquery_connections') || '[]')
            const conn = savedBQ.find((c: any) => c.project === project && c.dataset === dataset)
            if (!conn) continue
            usesDirectSourceType = true
            formData.append('source_type', 'bigquery')
            formData.append('bq_key_json', conn.key_json)
            formData.append('bq_project', project)
            formData.append('bq_dataset', dataset)
            formData.append('bq_table', table)
          } else {
            // AWS, GCS, Cloud SQL, Databricks, REST, GA4 — fetch as CSV then send as file
            const csvFile = await fetchSourceAsCSVFile(src)
            if (!csvFile) continue
            formData.append('file', csvFile)
          }

          if (!usesDirectSourceType && !formData.has('file') && !formData.has('source_type')) continue

          formData.append('notify_email', savedEmail || '')
          formData.append('alert_staleness', String(alertConditions.staleness))
          formData.append('alert_schema', String(alertConditions.schema))
          formData.append('alert_anomalies', String(alertConditions.anomalies))
          formData.append('alert_flow', String(alertConditions.flow))

          const res = await fetch(`${API_BASE}/api/analyze`, { method: 'POST', body: formData })
          const data = await res.json()
          if (data.notifications?.length) {
            addNotifications(data.notifications.map((n: any) => ({ ...n, sourceName: src.name })))
          }
        } catch {
          // Skip this source silently — don't block others
        }
      }
    }, [sources, notifyEmail, alertConditions, addNotifications, fetchSourceAsCSVFile])

    // ── Scan ALL connected sources for notifications (runs once on mount) ───
    const allSourcesScannedRef = useRef(false)
    useEffect(() => {
      if (allSourcesScannedRef.current) return
      if (sources.length === 0) return
      allSourcesScannedRef.current = true
      scanAllSourcesForNotificationsOnMount()
    }, [sources.length])

    const scanAllSourcesForNotificationsOnMount = useCallback(async () => {
      for (const src of sources) {
        try {
          const formData = new FormData()

          if (src.type === 'csv' || src.type === 'sheets') {
            if (!src.file) continue
            formData.append('file', src.file)
          } else if (src.type === 'mysql') {
            const savedMySQL = JSON.parse(localStorage.getItem('datapro_mysql_connections') || '[]')
            const atIndex = src.name.lastIndexOf('@')
            const host = atIndex !== -1 ? src.name.slice(atIndex + 1) : ''
            const beforeAt = atIndex !== -1 ? src.name.slice(0, atIndex) : src.name
            const dotIndex = beforeAt.indexOf('.')
            const database = dotIndex !== -1 ? beforeAt.slice(0, dotIndex) : beforeAt
            const table = dotIndex !== -1 ? beforeAt.slice(dotIndex + 1) : ''
            const conn = savedMySQL.find((c: any) =>
              c.database === database && (c.host === host || (c.host === 'localhost' && host === '127.0.0.1') || (c.host === '127.0.0.1' && host === 'localhost'))
            )
            if (!conn) continue
            formData.append('source_type', 'mysql')
            formData.append('mysql_host', conn.host)
            formData.append('mysql_port', conn.port)
            formData.append('mysql_user', conn.user)
            formData.append('mysql_password', conn.password)
            formData.append('mysql_database', conn.database)
            formData.append('mysql_ssl', String(conn.ssl))
            formData.append('mysql_table', table)
          } else if (src.type === 'postgres') {
            const savedPG = JSON.parse(localStorage.getItem('datapro_postgres_connections') || '[]')
            const atIndex = src.name.lastIndexOf('@')
            const host = atIndex !== -1 ? src.name.slice(atIndex + 1) : ''
            const beforeAt = atIndex !== -1 ? src.name.slice(0, atIndex) : src.name
            const dotIndex = beforeAt.indexOf('.')
            const database = dotIndex !== -1 ? beforeAt.slice(0, dotIndex) : beforeAt
            const table = dotIndex !== -1 ? beforeAt.slice(dotIndex + 1) : ''
            const conn = savedPG.find((c: any) =>
              c.database === database && (c.host === host || (c.host === 'localhost' && host === '127.0.0.1') || (c.host === '127.0.0.1' && host === 'localhost'))
            )
            if (!conn) continue
            formData.append('source_type', 'postgres')
            formData.append('mysql_host', conn.host)
            formData.append('mysql_port', conn.port)
            formData.append('mysql_user', conn.user)
            formData.append('mysql_password', conn.password)
            formData.append('mysql_database', conn.database)
            formData.append('mysql_ssl', String(conn.ssl))
            formData.append('mysql_table', table)
          } else if (src.type === 'mongodb') {
            const savedMongo = JSON.parse(localStorage.getItem('datapro_mongo_connections') || '[]')
            const atIdx = src.name.lastIndexOf('@')
            const beforeAt = atIdx >= 0 ? src.name.slice(0, atIdx) : src.name
            const dotIdx = beforeAt.indexOf('.')
            const database = dotIdx >= 0 ? beforeAt.slice(0, dotIdx) : beforeAt
            const collection = dotIdx >= 0 ? beforeAt.slice(dotIdx + 1) : ''
            const conn = savedMongo.find((c: any) => c.database === database)
            if (!conn) continue
            formData.append('source_type', 'mongodb')
            formData.append('mysql_host', conn.uri)
            formData.append('mysql_user', conn.database)
            formData.append('mysql_database', conn.database)
            formData.append('mysql_table', collection || conn.collection || conn.collections?.[0] || '')
          } else if (src.type === 'bigquery') {
            const parts = src.name.split('.')
            const project = parts[0] || ''
            const dataset = parts[1] || ''
            const table = parts[2] || ''
            const savedBQ = JSON.parse(localStorage.getItem('datapro_bigquery_connections') || '[]')
            const conn = savedBQ.find((c: any) => c.project === project && c.dataset === dataset)
            if (!conn) continue
            formData.append('source_type', 'bigquery')
            formData.append('bq_key_json', conn.key_json)
            formData.append('bq_project', project)
            formData.append('bq_dataset', dataset)
            formData.append('bq_table', table)
          } else {
            // Skip source types we can't silently re-fetch (rest, aws, ga4, databricks, gcs, cloudsql)
            continue
          }

          const savedEmail = localStorage.getItem('datapro_alert_email') || notifyEmail
          formData.append('notify_email', savedEmail || '')
          formData.append('alert_staleness', String(alertConditions.staleness))
          formData.append('alert_schema', String(alertConditions.schema))
          formData.append('alert_anomalies', String(alertConditions.anomalies))
          formData.append('alert_flow', String(alertConditions.flow))

          const res = await fetch(`${API_BASE}/api/analyze`, { method: 'POST', body: formData })
          const data = await res.json()
          if (data.notifications?.length) {
            addNotifications(data.notifications.map((n: any) => ({ ...n, sourceName: src.name })))
          }
        } catch {
          // Skip silently — don't block other sources from being scanned
        }
      }
    }, [sources, notifyEmail, alertConditions, addNotifications])

    useEffect(() => {
      const issueToKey: Record<string, string> = {
        duplicates: 'remove_duplicates', empty_rows: 'remove_empty_rows',
        missing: 'fill_missing', outliers: 'handle_outliers',
        messy_text: 'standardize_text', fix_dates: 'fix_dates'
      }
      if (pendingCleanOptions) {
        const merged = { ...pendingCleanOptions }
        if (analysis?.issues) {
          Object.entries(analysis.issues).forEach(([issue, detected]) => {
            if (detected) { const key = issueToKey[issue]; if (key) merged[key] = true }
          })
        }
        setOptions(merged)
        setPendingCleanOptions(null)
        setTimeout(() => {
          document.getElementById('clean-section')?.scrollIntoView({ behavior: 'smooth' })
        }, 300)
      } else if (analysis) {
        const newOpts = { ...options }
        ;(analysis.top4_issues || []).forEach((issue: string) => {
          const key = issueToKey[issue]
          if (key) newOpts[key] = true
        })
        setOptions(newOpts)
      }
    }, [pendingCleanOptions, analysis])

    const runAnalysis = useCallback(async () => {
      void activeFile // suppress ts6133 — used below and in AIChatClean
      const sourceObj2 = sources.find(s => s.name === activeSourceName)
      // For MySQL sources, never use a stale file — always query live
      const fileToUse = (sourceObj2?.type === 'mysql' || sourceObj2?.type === 'postgres')
        ? null
        : (pipelineOutputFile || activeFile || sourceObj2?.file || null)
      if (!fileToUse && !activeSourceName) return

      setAnalyzing(true)
      const formData = new FormData()
      if (fileToUse) {
        formData.append('file', fileToUse)
      } else if (sourceObj2?.type === 'mysql') {
        const savedMySQL = JSON.parse(localStorage.getItem('datapro_mysql_connections') || '[]')
        const atIndex = activeSourceName.lastIndexOf('@')
        const host = atIndex !== -1 ? activeSourceName.slice(atIndex + 1) : ''
        const beforeAt = atIndex !== -1 ? activeSourceName.slice(0, atIndex) : activeSourceName
        const dotIndex = beforeAt.indexOf('.')
        const database = dotIndex !== -1 ? beforeAt.slice(0, dotIndex) : beforeAt
        const table = dotIndex !== -1 ? beforeAt.slice(dotIndex + 1) : ''
        const mysqlConn = savedMySQL.find((c: any) =>
          c.database === database && (
            c.host === host ||
            (c.host === 'localhost' && host === '127.0.0.1') ||
            (c.host === '127.0.0.1' && host === 'localhost')
          )
        )
        console.log('MySQL debug:', { activeSourceName, database, table, host, savedMySQL, mysqlConn })
        if (!mysqlConn) {
          console.error('❌ No MySQL connection found for', { database, host })
          alert(`MySQL connection not found for ${database}@${host}. Please reconnect in Sources.`)
          return
        }
        if (mysqlConn) {
          formData.append('source_type', 'mysql')
          formData.append('mysql_host', mysqlConn.host)
          formData.append('mysql_port', mysqlConn.port)
          formData.append('mysql_user', mysqlConn.user)
          formData.append('mysql_password', mysqlConn.password)
          formData.append('mysql_database', mysqlConn.database)
          formData.append('mysql_ssl', String(mysqlConn.ssl))
          formData.append('mysql_table', table)
        }
      } else if (sourceObj2?.type === 'postgres') {
        const savedPG = JSON.parse(localStorage.getItem('datapro_postgres_connections') || '[]')
        const atIndex = activeSourceName.lastIndexOf('@')
        const host = atIndex !== -1 ? activeSourceName.slice(atIndex + 1) : ''
        const beforeAt = atIndex !== -1 ? activeSourceName.slice(0, atIndex) : activeSourceName
        const dotIndex = beforeAt.indexOf('.')
        const database = dotIndex !== -1 ? beforeAt.slice(0, dotIndex) : beforeAt
        const table = dotIndex !== -1 ? beforeAt.slice(dotIndex + 1) : ''
        const pgConn = savedPG.find((c: any) =>
          c.database === database && (
            c.host === host ||
            (c.host === 'localhost' && host === '127.0.0.1') ||
            (c.host === '127.0.0.1' && host === 'localhost')
          )
        )
        console.log('PostgreSQL debug:', { activeSourceName, database, table, host, savedPG, pgConn })
        if (!pgConn) {
          console.error('❌ No PostgreSQL connection found for', { database, host })
          alert(`PostgreSQL connection not found for ${database}@${host}. Please reconnect in Sources.`)
          return
        }
        if (pgConn) {
          formData.append('source_type', 'postgres')
          formData.append('mysql_host', pgConn.host)
          formData.append('mysql_port', pgConn.port)
          formData.append('mysql_user', pgConn.user)
          formData.append('mysql_password', pgConn.password)
          formData.append('mysql_database', pgConn.database)
          formData.append('mysql_ssl', String(pgConn.ssl))
          formData.append('mysql_table', table)
        }
      } else if (sourceObj2?.type === 'mongodb') {
        const savedMongo = JSON.parse(localStorage.getItem('datapro_mongo_connections') || '[]')
        const atIdx = activeSourceName.lastIndexOf('@')
        const beforeAt = atIdx >= 0 ? activeSourceName.slice(0, atIdx) : activeSourceName
        const dotIdx = beforeAt.indexOf('.')
        const database = dotIdx >= 0 ? beforeAt.slice(0, dotIdx) : beforeAt
        const collection = dotIdx >= 0 ? beforeAt.slice(dotIdx + 1) : ''
        const mongoConn = savedMongo.find((c: any) => c.database === database)
        console.log('MongoDB debug:', { activeSourceName, database, collection, savedMongo, mongoConn })
        if (!mongoConn) {
          console.error('❌ No MongoDB connection found for', { database })
          alert(`MongoDB connection not found for ${database}. Please reconnect in Sources.`)
          return
        }
        formData.append('source_type', 'mongodb')
        formData.append('mysql_host', mongoConn.uri)
        formData.append('mysql_user', mongoConn.database)
        formData.append('mysql_database', mongoConn.database)
        formData.append('mysql_table', collection || mongoConn.collection || mongoConn.collections?.[0] || '')
      } 
      
      else if (sourceObj2?.type === 'bigquery') {
        // name format: "project.dataset.table"
        const parts = activeSourceName.split('.')
        const project = parts[0] || ''
        const dataset = parts[1] || ''
        const table = parts[2] || ''
        const savedBQ = JSON.parse(localStorage.getItem('datapro_bigquery_connections') || '[]')
        const bqConn = savedBQ.find((c: any) => c.project === project && c.dataset === dataset)
        console.log('BigQuery debug:', { activeSourceName, project, dataset, table, bqConn })
        if (!bqConn) {
          console.error('❌ No BigQuery connection found for', { project, dataset })
          alert(`BigQuery connection not found for ${project}.${dataset}. Please reconnect in Sources.`)
          return
        }
        formData.append('source_type', 'bigquery')
        formData.append('bq_key_json', bqConn.key_json)
        formData.append('bq_project', project)
        formData.append('bq_dataset', dataset)
        formData.append('bq_table', table)
        } else if ((sourceObj2?.type as string) === 'rest') {
        const savedRestMeta = JSON.parse(localStorage.getItem('datapro_rest_connections') || '[]')
        // name format: "rest:hostname/endpoint"
        const srcNameRest = activeSourceName
        const withoutProto = srcNameRest.replace('rest:', '')
        const conn = savedRestMeta.find((c: any) => {
          const connHost = (c.base_url || '').replace(/https?:\/\//, '').replace(/\/$/, '')
          return withoutProto.startsWith(connHost)
        })
        if (!conn) {
          alert(`REST API connection not found for ${activeSourceName}. Please reconnect in Sources.`)
          setAnalyzing(false)
          return
        }
        // Extract the endpoint from the source name
        const connHost = (conn.base_url || '').replace(/https?:\/\//, '').replace(/\/$/, '')
        const endpoint = withoutProto.slice(connHost.length) || '/'
        try {
          const fetchRes = await fetch(`${API_BASE}/api/rest/fetch-endpoint`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              base_url: conn.base_url,
              endpoint,
              headers: conn.headers || {},
              verify_ssl: conn.verify_ssl,
              data_path: conn.data_path || '',
              method: conn.method || 'GET',
              timeout: conn.timeout || 30,
            }),
          })
          const fetchData = await fetchRes.json()
          if (fetchData.download_url) {
            const csvRes = await fetch(`${API_BASE}${fetchData.download_url}`)
            const blob = await csvRes.blob()
            const realFile = new File([blob], `${activeSourceName}.csv`, { type: 'text/csv' })
            formData.append('file', realFile)
          } else {
            alert(`REST API fetch failed: ${fetchData.error}`)
            setAnalyzing(false)
            return
          }
        } catch (e) {
          alert(`REST API fetch error: ${e}`)
          setAnalyzing(false)
          return
        }
      } else if (sourceObj2?.type === 'aws') {
        const savedAWS = JSON.parse(localStorage.getItem('datapro_aws_connections') || '[]')
        // Parse source name to identify the connection
        const srcName = activeSourceName
        const awsConn = savedAWS.find((c: any) => {
          if (c.service === 's3') return srcName.startsWith(`s3://${c.bucket}`)
          if (c.service === 'dynamodb') return srcName.includes(`@dynamodb.${c.region}`)
          const atIdx = srcName.lastIndexOf('@')
          const host = atIdx >= 0 ? srcName.slice(atIdx + 1) : ''
          const beforeAt = atIdx >= 0 ? srcName.slice(0, atIdx) : srcName
          const dotIdx = beforeAt.indexOf('.')
          const database = dotIdx >= 0 ? beforeAt.slice(0, dotIdx) : beforeAt
          return c.host === host && c.database === database
        })
        if (!awsConn) {
          alert(`AWS connection not found for ${activeSourceName}. Please reconnect in Sources.`)
          setAnalyzing(false)
          return
        }
        // Extract table/key from source name
        let awsTable = ''
        if (awsConn.service === 's3') {
          awsTable = srcName.replace(`s3://${awsConn.bucket}/`, '')
        } else if (awsConn.service === 'dynamodb') {
          awsTable = srcName.split('@')[0]
        } else {
          const atIdx = srcName.lastIndexOf('@')
          const beforeAt = atIdx >= 0 ? srcName.slice(0, atIdx) : srcName
          const dotIdx = beforeAt.indexOf('.')
          awsTable = dotIdx >= 0 ? beforeAt.slice(dotIdx + 1) : beforeAt
        }
        // Fetch the table as CSV first
        try {
          const fetchRes = await fetch(`${API_BASE}/api/aws/fetch-table`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ...awsConn, service: awsConn.service, table: awsTable }),
          })
          const fetchData = await fetchRes.json()
          if (fetchData.download_url) {
            const csvRes = await fetch(`${API_BASE}${fetchData.download_url}`)
            const blob = await csvRes.blob()
            const realFile = new File([blob], `${activeSourceName}.csv`, { type: 'text/csv' })
            formData.append('file', realFile)
          } else {
            alert(`AWS fetch failed: ${fetchData.error}`)
            setAnalyzing(false)
            return
          }
        } catch (e) {
          alert(`AWS fetch error: ${e}`)
setAnalyzing(false)
        return
        }
      } else if ((sourceObj2?.type as string) === 'databricks') {
        try {
          const savedDB = JSON.parse(localStorage.getItem('datapro_databricks_connections') || '[]')
          const atIdx = activeSourceName.lastIndexOf('@')
          const srcHost = atIdx >= 0 ? activeSourceName.slice(atIdx + 1) : ''
          const beforeAt = atIdx >= 0 ? activeSourceName.slice(0, atIdx) : activeSourceName
          const parts = beforeAt.split('.')
          const catalog = parts.length >= 3 ? parts[0] : 'hive_metastore'
          const schema  = parts.length >= 3 ? parts[1] : (parts[0] || 'default')
          const table   = parts.length >= 3 ? parts[2] : (parts[1] || '')
          const dbConn = savedDB.find((c: any) => c.host === srcHost)
          if (!dbConn) {
            alert(`Databricks connection not found for ${srcHost}. Please reconnect in Sources.`)
            setAnalyzing(false)
            return
          }
          const fetchRes = await fetch(`${API_BASE}/api/databricks/fetch-table`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              host: dbConn.host,
              token: dbConn.token,
              http_path: dbConn.http_path,
              catalog,
              schema,
              table,
            }),
          })
          const fetchData = await fetchRes.json()
          if (fetchData.download_url) {
            const csvRes = await fetch(`${API_BASE}${fetchData.download_url}`)
            const blob = await csvRes.blob()
            const realFile = new File([blob], `${activeSourceName}.csv`, { type: 'text/csv' })
            formData.append('file', realFile)
          } else {
            alert(`Databricks fetch failed: ${fetchData.error}`)
            setAnalyzing(false)
            return
          }
        } catch (e) {
          alert(`Databricks fetch error: ${e}`)
          setAnalyzing(false)
          return
        }
      } else if ((sourceObj2?.type as string) === 'ga4') {
        try {
          const savedGA4Meta = JSON.parse(localStorage.getItem('datapro_ga4_connections') || '[]')
          // name format: "ga4:propertyId/reportName"
          const withoutProto = activeSourceName.replace('ga4:', '')
          const slashIdx = withoutProto.indexOf('/')
          const propertyId = slashIdx >= 0 ? withoutProto.slice(0, slashIdx) : withoutProto
          const reportName = slashIdx >= 0 ? withoutProto.slice(slashIdx + 1) : ''
          const ga4Conn = savedGA4Meta.find((c: any) => c.propertyId === propertyId)
          if (!ga4Conn) {
            alert(`GA4 connection not found for property ${propertyId}. Please reconnect in Sources.`)
            setAnalyzing(false)
            return
          }
          const fetchRes = await fetch(`${API_BASE}/api/ga4/fetch-report`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              key_json: ga4Conn.keyJson,
              property_id: ga4Conn.propertyId,
              metrics: ga4Conn.metrics || 'sessions,activeUsers,newUsers,screenPageViews',
              dimensions: ga4Conn.dimensions || 'date,sessionDefaultChannelGroup',
              start_date: ga4Conn.startDate || '30daysAgo',
              end_date: ga4Conn.endDate || 'today',
              report_name: reportName,
            }),
          })
          const fetchData = await fetchRes.json()
          if (fetchData.download_url) {
            const csvRes = await fetch(`${API_BASE}${fetchData.download_url}`)
            const blob = await csvRes.blob()
            const realFile = new File([blob], `${activeSourceName}.csv`, { type: 'text/csv' })
            formData.append('file', realFile)
          } else {
            alert(`GA4 fetch failed: ${fetchData.error}`)
            setAnalyzing(false)
            return
          }
        } catch (e) {
          alert(`GA4 fetch error: ${e}`)
          setAnalyzing(false)
          return
        }
      } else if (sourceObj2?.type === 'sheets') {
        if (!sourceObj2?.file) {
          alert('Google Sheet file not found. Please re-import the sheet from Data Sources.')
          setAnalyzing(false)
          return
        }
        formData.append('file', sourceObj2.file)
      } else {
        formData.append('source_name', activeSourceName)
        formData.append('source_type', 'sheets')
      }
      formData.append('notify_email', notifyEmail)
      formData.append('alert_staleness', String(alertConditions.staleness))
      formData.append('alert_schema', String(alertConditions.schema))
      formData.append('alert_anomalies', String(alertConditions.anomalies))
      formData.append('alert_flow', String(alertConditions.flow))
      try {
        const res = await fetch(`${API_BASE}/api/analyze`, { method: 'POST', body: formData })
        const data = await res.json()
        setAnalysis(data)
        analysisSourceRef.current = activeSourceName
        setAnalysisTab('preview')
        setFilterIssue('All')
        setFilterCol('All')
        if (data.notifications?.length) {
          addNotifications(data.notifications.map((n: any) => ({ ...n, sourceName: activeSourceName })))
        }
        addAuditLog({
          id: crypto.randomUUID(), time: new Date().toLocaleString(),
          action: 'analyze', entity_type: 'csv_file', entity_id: crypto.randomUUID(),
          details: `Analyzed: ${activeFile?.name ?? activeSourceName} · ${data.dataset_info?.rows?.toLocaleString()} rows · Quality: ${data.quality_score}%`,
          user: 'local_user'
        })
      } catch {
        alert('Analysis failed! Make sure backend is running.')
      } finally {
        setAnalyzing(false)
      }
    }, [activeFile, activeSourceName, pipelineOutputFile, notifyEmail, alertConditions])

    const runClean = useCallback(async () => {
      // For DB/cloud sources, activeFile is null but we have a fetched preview
      // from analysis.sample_data — fetch the original file via the analyze endpoint's download link
      let fileToClean: File | null = activeFile

      if (!fileToClean && analysis?.original_file) {
        try {
          const origRes = await fetch(`${API_BASE}${analysis.original_file}`)
          const blob = await origRes.blob()
          fileToClean = new File([blob], `${activeSourceName || 'source'}.csv`, { type: 'text/csv' })
        } catch {
          alert('Could not load source data for cleaning. Try re-analyzing first.')
          return
        }
      }

      if (!fileToClean) {
        alert('No data available to clean. Try re-analyzing first.')
        return
      }

      setCleaning(true)

      const activeCustomOps = customCleanOps.filter(op => options[op.key])

      try {
        let lastResult: any = null

        // Run standard cleaning first if any standard options selected
        const standardOptions = Object.fromEntries(
          Object.entries(options).filter(([k]) => !k.startsWith('ai_custom_'))
        )
        const hasStandardOps = Object.values(standardOptions).some(Boolean)

        if (hasStandardOps) {
          const formData = new FormData()
          formData.append('file', fileToClean)
          Object.entries(standardOptions).forEach(([k, v]) => formData.append(k, String(v)))
          formData.append('imputation_method', imputationMethod)
          const res = await fetch(`${API_BASE}/api/clean-data`, { method: 'POST', body: formData })
          lastResult = await res.json()
          // Download the cleaned file to pass to next step
          if (lastResult.download_url) {
            const csvRes = await fetch(`${API_BASE}${lastResult.download_url}`)
            const blob = await csvRes.blob()
            fileToClean = new File([blob], 'intermediate.csv', { type: 'text/csv' })
          }
        }

        // Run each custom AI op sequentially
        for (const op of activeCustomOps) {
          const formData = new FormData()
          formData.append('file', fileToClean)
          formData.append('instruction', op.instruction)
          const res = await fetch(`${API_BASE}/api/ai-custom-clean`, { method: 'POST', body: formData })
          const aiResult = await res.json()
          if (aiResult.success && aiResult.download_url) {
            lastResult = {
              ...(lastResult || {}),
              ...aiResult,
              original_shape: lastResult?.original_shape || aiResult.original_shape || [0, 0],
              cleaned_shape: aiResult.cleaned_shape || lastResult?.cleaned_shape || [0, 0],
              options_used: {
                ...(lastResult?.options_used || {}),
                [op.label]: true,
              },
            }
            const csvRes = await fetch(`${API_BASE}${lastResult.download_url}`)
            const blob = await csvRes.blob()
            fileToClean = new File([blob], 'intermediate.csv', { type: 'text/csv' })
          }
        }

        if (!lastResult) {
          alert('No cleaning operation produced a result.')
          setCleaning(false)
          return
        }

        const safeResult = {
          preview_data: [],
          preview_columns: [],
          options_used: {},
          code_used: '',
          ...lastResult,
          original_shape: lastResult.original_shape ?? [0, 0],
          cleaned_shape: lastResult.cleaned_shape ?? [0, 0],
          download_url: lastResult.download_url ?? '',
        }

        setResult(safeResult)
        setAnalysisTab('success' as any)
        addAuditLog({
          id: crypto.randomUUID(), time: new Date().toLocaleString(),
          action: 'clean', entity_type: 'csv_file', entity_id: crypto.randomUUID(),
          details: `Cleaned: ${fileToClean.name} · ${safeResult.original_shape[0]} → ${safeResult.cleaned_shape[0]} rows`,
          user: 'local_user'
        })
      } catch {
        alert('Cleaning failed!')
      } finally {
        setCleaning(false)
      }
    }, [activeFile, analysis, activeSourceName, options, imputationMethod, customCleanOps])

    const handleEditSourceConfirm = useCallback((sourceName: string, destinationName: string) => {
      setShowEditSource(false)
      setActiveDestinationName(destinationName)

      if (sourceName === activeSourceName) return

      const sourceObj = sources.find(s => s.name === sourceName)
      if (!sourceObj) return

      if (sourceObj.type === 'mysql') {
        const parts = sourceObj.name.split('.')
        const tableAndHost = parts[1] || ''
        const table = tableAndHost.split('@')[0] || ''
        const database = parts[0] || ''
        const host = tableAndHost.split('@')[1] || ''
        const mysqlMeta = JSON.parse(localStorage.getItem('datapro_mysql_connections') || '[]')
        const conn = mysqlMeta.find((c: any) => String(c.host) === host && String(c.database) === database)
        if (conn) {
          localStorage.setItem('datapro_active_mysql_source', JSON.stringify({ ...conn, table }))
        }
      } else if (sourceObj.type === 'postgres') {
        const parts = sourceObj.name.split('.')
        const tableAndHost = parts[1] || ''
        const table = tableAndHost.split('@')[0] || ''
        const database = parts[0] || ''
        const host = tableAndHost.split('@')[1] || ''
        const pgMeta = JSON.parse(localStorage.getItem('datapro_postgres_connections') || '[]')
        const conn = pgMeta.find((c: any) => String(c.host) === host && String(c.database) === database)
        if (conn) {
          localStorage.setItem('datapro_active_postgres_source', JSON.stringify({ ...conn, table }))
        }
      } else if (sourceObj.type === 'bigquery') {
        const parts = sourceObj.name.split('.')
        const project = parts[0] || ''
        const dataset = parts[1] || ''
        const table = parts[2] || ''
        const bqMeta = JSON.parse(localStorage.getItem('datapro_bigquery_connections') || '[]')
        const conn = bqMeta.find((c: any) => String(c.project) === project && String(c.dataset) === dataset)
        if (conn) {
          localStorage.setItem('datapro_active_bigquery_source', JSON.stringify({ ...conn, table }))
        }
      } else if (sourceObj.type === 'mongodb') {
        const atIdx = sourceObj.name.lastIndexOf('@')
        const beforeAt = atIdx >= 0 ? sourceObj.name.slice(0, atIdx) : sourceObj.name
        const dotIdx = beforeAt.indexOf('.')
        const database = dotIdx >= 0 ? beforeAt.slice(0, dotIdx) : beforeAt
        const collection = dotIdx >= 0 ? beforeAt.slice(dotIdx + 1) : ''
        const mongoMeta = JSON.parse(localStorage.getItem('datapro_mongo_connections') || '[]')
        const conn = mongoMeta.find((c: any) => String(c.database) === database)
        if (conn) {
          localStorage.setItem('datapro_active_mongo_source', JSON.stringify({ ...conn, collection }))
        }
      } else if (sourceObj.type === 'aws') {
        const awsMeta = JSON.parse(localStorage.getItem('datapro_aws_connections') || '[]')
        const conn = awsMeta.find((c: any) => {
          if (c.service === 's3') return sourceObj.name.startsWith(`s3://${c.bucket}`)
          if (c.service === 'dynamodb') return sourceObj.name.includes(`@dynamodb.${c.region}`)
          const atIdx = sourceObj.name.lastIndexOf('@')
          const host = atIdx >= 0 ? sourceObj.name.slice(atIdx + 1) : ''
          const beforeAt = atIdx >= 0 ? sourceObj.name.slice(0, atIdx) : sourceObj.name
          const dotIdx = beforeAt.indexOf('.')
          const database = dotIdx >= 0 ? beforeAt.slice(0, dotIdx) : beforeAt
          return String(c.host) === host && String(c.database) === database
        })
        if (conn) {
          localStorage.setItem('datapro_active_aws_source', JSON.stringify({ ...conn }))
        }
      } else if ((sourceObj.type as string) === 'gcs') {
        const savedGCSMeta = JSON.parse(localStorage.getItem('datapro_gcs_connections') || '[]')
        const withoutProto = sourceObj.name.replace('gcs://', '')
        const slashIdx = withoutProto.indexOf('/')
        const bucket = slashIdx >= 0 ? withoutProto.slice(0, slashIdx) : withoutProto
        const file = slashIdx >= 0 ? withoutProto.slice(slashIdx + 1) : ''
        const conn = savedGCSMeta.find((c: any) => String(c.bucket) === bucket)
        if (conn) {
          localStorage.setItem('datapro_active_gcs_source', JSON.stringify({ ...conn, file }))
        }
      } else if ((sourceObj.type as string) === 'cloudsql') {
        const savedCSQLMeta = JSON.parse(localStorage.getItem('datapro_cloudsql_connections') || '[]')
        const atIdx = sourceObj.name.lastIndexOf('@')
        const beforeAt = atIdx >= 0 ? sourceObj.name.slice(0, atIdx) : sourceObj.name
        const dotIdx = beforeAt.indexOf('.')
        const database = dotIdx >= 0 ? beforeAt.slice(0, dotIdx) : beforeAt
        const table = dotIdx >= 0 ? beforeAt.slice(dotIdx + 1) : ''
        const projectPart = atIdx >= 0 ? sourceObj.name.slice(atIdx + 1).replace('cloudsql.', '') : ''
        const conn = savedCSQLMeta.find((c: any) => String(c.database) === database && String(c.project) === projectPart)
        if (conn) {
          localStorage.setItem('datapro_active_cloudsql_source', JSON.stringify({ ...conn, table }))
        }
      } else if ((sourceObj.type as string) === 'databricks') {
        const databricksMeta = JSON.parse(localStorage.getItem('datapro_databricks_connections') || '[]')
        const atIdx = sourceObj.name.lastIndexOf('@')
        const beforeAt = atIdx >= 0 ? sourceObj.name.slice(0, atIdx) : sourceObj.name
        const parts = beforeAt.split('.')
        const catalog = parts[0] || ''
        const schema = parts[1] || ''
        const table = parts[2] || ''
        const host = atIdx >= 0 ? sourceObj.name.slice(atIdx + 1) : ''
        const conn = databricksMeta.find((c: any) => String(c.host) === host && String(c.catalog) === catalog && String(c.schema) === schema)
        if (conn) {
          localStorage.setItem('datapro_active_databricks_source', JSON.stringify({ ...conn, table }))
        }
      } else if ((sourceObj.type as string) === 'rest') {
        const savedRestMeta = JSON.parse(localStorage.getItem('datapro_rest_connections') || '[]')
        const withoutProto = sourceObj.name.replace('rest:', '')
        const conn = savedRestMeta.find((c: any) => {
          const connHost = (c.base_url || '').replace(/https?:\/\//, '')
          return withoutProto.startsWith(connHost)
        })
        if (conn) {
          localStorage.setItem('datapro_active_rest_source', JSON.stringify({ ...conn }))
        }
      } else if ((sourceObj.type as string) === 'ga4') {
        const savedGA4Meta = JSON.parse(localStorage.getItem('datapro_ga4_connections') || '[]')
        const withoutProto = sourceObj.name.replace('ga4:', '')
        const slashIdx = withoutProto.indexOf('/')
        const propertyId = slashIdx >= 0 ? withoutProto.slice(0, slashIdx) : withoutProto
        const conn = savedGA4Meta.find((c: any) => c.propertyId === propertyId)
        if (conn) {
          localStorage.setItem('datapro_active_ga4_source', JSON.stringify({ ...conn }))
        }
      }

      // switchActiveSource sets analysis → null, which triggers the useEffect above
      switchActiveSource(sourceObj.file ?? null, sourceObj.name)
      setResult(null)
    }, [activeSourceName, sources, switchActiveSource, setActiveDestinationName])

    // ── Helpers for fullscreen ───────────────────────────────────────────────
    const openFullscreen = useCallback((
      title: string,
      columns: string[],
      rows: any[],
      subtitle?: string,
      accentColor?: string
    ) => {
      setFullscreenTable({ title, subtitle, columns, rows, accentColor })
    }, [])

    const anyCleanSelected = Object.values(options).some(Boolean) || imputationMethod !== 'none'

  // Google Analytics Frontend Tracking Initialization
  useEffect(() => {
    const anyWindow = window as any;
    if (!anyWindow.dataLayer) {
      const scriptId = "google-analytics-script";
      if (!document.getElementById(scriptId)) {
        const gaScript = document.createElement("script");
        gaScript.id = scriptId;
        gaScript.async = true;
        gaScript.src = "https://www.googletagmanager.com/gtag/js?id=G-TRACKING_ID";
        document.head.appendChild(gaScript);

        const configScript = document.createElement("script");
        configScript.textContent = `
          window.dataLayer = window.dataLayer || [];
          function gtag(){dataLayer.push(arguments);}
          gtag('js', new Date());
          gtag('config', 'G-TRACKING_ID', { page_path: window.location.pathname });
        `;
        document.head.appendChild(configScript);
      }
    } else if (typeof anyWindow.gtag === 'function') {
      anyWindow.gtag('config', 'G-TRACKING_ID', { page_path: window.location.pathname });
    }
  }, []);

    const FloatingCleanBtn = () => (
      <div className="fixed bottom-6 right-6 z-40">
        <button
          onClick={() => {
            if (anyCleanSelected) runClean()
            else document.getElementById('clean-section')?.scrollIntoView({ behavior: 'smooth' })
          }}
          disabled={cleaning}
          className={`flex items-center gap-2 px-5 py-3 rounded-2xl font-bold text-white text-sm shadow-2xl transition-all disabled:opacity-50 ${anyCleanSelected ? 'sparkle-btn' : ''}`}
          style={{
            background: 'linear-gradient(135deg, #7c3aed, #2563eb)',
            boxShadow: anyCleanSelected ? '0 8px 32px rgba(124,58,237,0.5)' : '0 4px 16px rgba(0,0,0,0.4)',
          }}
        >
          {cleaning ? (
            <>
              <svg className="animate-spin" width="15" height="15" fill="none" stroke="white" strokeWidth="2.5" viewBox="0 0 24 24">
                <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/>
              </svg>
              Cleaning...
            </>
          ) : anyCleanSelected ? (
            <>
              <svg width="15" height="15" fill="none" stroke="white" strokeWidth="2.5" viewBox="0 0 24 24">
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
              </svg>
              Run Cleaning
              <span className="text-xs font-normal opacity-75 ml-1">
                ({Object.values(options).filter(Boolean).length} selected)
              </span>
            </>
          ) : (
            <>
              <svg width="15" height="15" fill="none" stroke="white" strokeWidth="2.5" viewBox="0 0 24 24">
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
              </svg>
              Go to Clean
            </>
          )}
        </button>
      </div>
    )

    if (!activeFile && !activeSourceName) {
      return (
        <div className="p-8 max-w-5xl mx-auto">
          <div
            className="text-center py-24 rounded-2xl"
            style={{ background: 'rgba(255,255,255,0.02)', border: '1px dashed rgba(255,255,255,0.08)' }}
          >
            <div className="text-5xl mb-4">📊</div>
            <p className="text-white font-bold text-lg mb-2">No file selected</p>
            <p className="text-sm mb-6" style={{ color: 'rgba(255,255,255,0.35)' }}>Go to Data Sources and upload a CSV to begin</p>
            <button
              onClick={() => navigate('/sources')}
              className="px-6 py-3 rounded-xl text-sm font-bold text-white"
              style={{ background: 'linear-gradient(135deg, #7c3aed, #2563eb)' }}
            >
              Go to Data Sources →
            </button>
          </div>
        </div>
      )
    }

    if (analyzing) {
      return (
        <div className="p-8 max-w-5xl mx-auto flex items-center justify-center" style={{ minHeight: '60vh' }}>
          <div className="text-center">
            <div
              className="w-16 h-16 rounded-2xl mx-auto mb-6 flex items-center justify-center animate-spin"
              style={{ background: 'linear-gradient(135deg, #7c3aed, #2563eb)' }}
            >
              <svg width="24" height="24" fill="none" stroke="white" strokeWidth="2.5" viewBox="0 0 24 24">
                <path d="M3 3v18h18"/><path d="M7 16l4-4 4 4 4-8"/>
              </svg>
            </div>
            <p className="text-white font-bold text-xl mb-2">Analyzing your data...</p>
            <p className="text-sm" style={{ color: 'rgba(255,255,255,0.35)' }}>
              {activeSourceName && <span>Source: <span style={{ color: '#a78bfa' }}>{activeSourceName}</span></span>}
            </p>
            <p className="text-sm mt-1" style={{ color: 'rgba(255,255,255,0.35)' }}>Detecting issues, outliers, and patterns</p>
          </div>
        </div>
      )
    }

    if (analysisTab === 'success' && result && result.original_shape && result.cleaned_shape) {
      const previewData = result.preview_data || []
      const previewCols = result.preview_columns || (previewData[0] ? Object.keys(previewData[0]) : [])
      return (
        <div className="p-8 max-w-6xl mx-auto space-y-6">
          {/* Fullscreen overlay */}
          {fullscreenTable && (
            <FullscreenTable
              title={fullscreenTable.title}
              subtitle={fullscreenTable.subtitle}
              columns={fullscreenTable.columns}
              rows={fullscreenTable.rows}
              accentColor={fullscreenTable.accentColor}
              onClose={() => setFullscreenTable(null)}
            />
          )}

          <div
            className="rounded-3xl p-10 text-center"
            style={{ background: 'rgba(34,197,94,0.05)', border: '2px solid rgba(34,197,94,0.2)' }}
          >
            <div className="text-5xl mb-4">✅</div>
            <h2 className="text-3xl font-bold text-white mb-2">Perfectly Cleaned!</h2>
            <p className="text-sm mb-6" style={{ color: 'rgba(255,255,255,0.4)' }}>Original file preserved. Nothing was overwritten.</p>
            <div className="grid grid-cols-3 gap-4 max-w-xs mx-auto mb-6">
              <StatCard label="Original" value={result.original_shape[0].toLocaleString()} />
              <StatCard label="Cleaned" value={result.cleaned_shape[0].toLocaleString()} color="#22c55e" />
              <StatCard label="Columns" value={result.cleaned_shape[1]} />
            </div>
            <div
              className="mb-6 p-4 rounded-2xl text-left"
              style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.07)' }}
            >
              <p className="text-xs font-semibold mb-3" style={{ color: 'rgba(255,255,255,0.4)' }}>Operations applied</p>
              <div className="flex flex-wrap gap-2">
                {Object.entries(result.options_used).filter(([, v]) => v).map(([k, v]) => (
                  <span
                    key={k}
                    className="px-3 py-1 rounded-full text-xs font-medium"
                    style={{ background: 'rgba(34,197,94,0.1)', color: '#4ade80' }}
                  >
                    {k === 'imputation_method' ? `Imputation: ${v}` : k.replace(/_/g, ' ')}
                  </span>
                ))}
              </div>
            </div>
<div className="flex gap-3 justify-center flex-wrap">
  <a
                href={`${API_BASE}${result.download_url}`}
                className="py-3 px-8 rounded-xl font-bold text-white text-center"
                style={{ background: 'linear-gradient(135deg, #16a34a, #15803d)' }}
              >
                Download Cleaned CSV
              </a>
              <a
                href={`${API_BASE}${analysis?.original_file}`}
                className="py-3 px-8 rounded-xl font-semibold text-center"
                style={{ background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.08)', color: 'rgba(255,255,255,0.7)' }}
              >
                Download Original
              </a>
              <SaveToSourceButton
                result={result}
                activeSourceName={activeSourceName}
                sources={sources}
                API_BASE={API_BASE}
              />
              <button
                onClick={() => setAnalysisTab('preview')}
                className="py-3 px-6 rounded-xl font-semibold"
                style={{ background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.08)', color: 'rgba(255,255,255,0.5)' }}
              >
                Back to Analysis
              </button>
            </div>
          </div>

          {previewData.length > 0 && (
            <div
              className="rounded-3xl overflow-hidden"
              style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(52,211,153,0.2)' }}
            >
              <div
                className="flex items-center justify-between px-6 py-4"
                style={{ borderBottom: '1px solid rgba(255,255,255,0.07)', background: 'rgba(52,211,153,0.04)' }}
              >
                <div className="flex items-center gap-3">
                  <div className="w-2 h-2 rounded-full" style={{ background: '#34d399' }} />
                  <p className="font-semibold text-white text-sm">Cleaned Data Preview</p>
                  <span
                    className="text-xs px-2.5 py-0.5 rounded-full font-medium"
                    style={{ background: 'rgba(52,211,153,0.12)', color: '#34d399' }}
                  >
                    First 500 rows
                  </span>
                </div>
                <div className="flex items-center gap-3">
                  <span className="text-xs" style={{ color: 'rgba(255,255,255,0.35)' }}>
                    {previewData.length.toLocaleString()} of {result.cleaned_shape[0].toLocaleString()} rows shown
                  </span>
                  <button
                    onClick={() => openFullscreen('Cleaned Data Preview', previewCols, previewData, `${previewData.length} of ${result.cleaned_shape[0].toLocaleString()} rows`, '#34d399')}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold"
                    style={{ background: 'rgba(52,211,153,0.1)', border: '1px solid rgba(52,211,153,0.25)', color: '#34d399' }}
                  >
                    <svg width="11" height="11" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                      <path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/>
                    </svg>
                    Fullscreen
                  </button>
                </div>
              </div>
              <div className="overflow-auto" style={{ maxHeight: '500px' }}>
                <table className="w-full text-xs" style={{ color: 'rgba(255,255,255,0.65)', borderCollapse: 'collapse' }}>
                  <thead className="sticky top-0" style={{ background: '#0a0714', zIndex: 10 }}>
                    <tr>
                      <th
                        className="p-3 text-left font-semibold whitespace-nowrap"
                        style={{ borderBottom: '1px solid rgba(255,255,255,0.08)', color: 'rgba(255,255,255,0.3)', width: '48px' }}
                      >#</th>
                      {previewCols.map((col: string) => (
                        <th
                          key={col}
                          className="p-3 text-left font-semibold whitespace-nowrap"
                          style={{ borderBottom: '1px solid rgba(255,255,255,0.08)', color: 'rgba(255,255,255,0.8)' }}
                        >{col}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {previewData.map((row: any, i: number) => (
                      <tr
                        key={i}
                        style={{ borderBottom: '1px solid rgba(255,255,255,0.03)' }}
                        onMouseEnter={e => (e.currentTarget.style.background = 'rgba(52,211,153,0.04)')}
                        onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
                      >
                        <td className="p-3 font-mono" style={{ color: 'rgba(255,255,255,0.2)' }}>{i + 1}</td>
                        {previewCols.map((col: string) => (
                          <td key={col} className="p-3 whitespace-nowrap">
                            {row[col] === '' || row[col] === null || row[col] === undefined
                              ? <span style={{ color: '#f87171', fontStyle: 'italic' }}>null</span>
                              : typeof row[col] === 'number'
                                ? row[col].toLocaleString()
                                : String(row[col])}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )
    }

    if (!analysis) return null

    const info = analysis.dataset_info || {}
    const sample = analysis.sample_data || []
    const firstRow = sample[0] || {}
    const colInfo = analysis.col_info || []
    const describe = analysis.describe || {}
    const describeStats = ['count','mean','std','min','25%','50%','75%','max']
    const describeCols = Object.keys(describe)
    const problems = analysis.problems || []
    const counts = analysis.problem_counts || {}
    const totalProblems = analysis.total_problems || problems.length
    const ts = analysis.time_series || {}
    const issueTypes = ['All', ...new Set(problems.map((p: any) => p.issue))]
    const problemCols = ['All', ...new Set(problems.map((p: any) => p.column))]
    const filteredProblems = problems.filter((p: any) =>
      (filterIssue === 'All' || p.issue === filterIssue) &&
      (filterCol === 'All' || p.column === filterCol)
    )
    const selectedMethod = IMPUTATION_METHODS.find(m => m.value === imputationMethod)

    const tabs = [
      { id: 'preview',  label: 'Preview' },
      { id: 'info',     label: 'Columns' },
      { id: 'describe', label: 'Statistics' },
      { id: 'problems', label: 'Problems', badge: totalProblems },
      ...(ts.detected && ts.chart_data ? [{ id: 'timeseries', label: 'Time Series' }] : []),
      { id: 'aichat',   label: '✨ AI Clean' },
    ]

    // Build row/col arrays for fullscreen from current tab
const getPreviewFullscreenData = () => {
      const cols = Object.keys(firstRow)
      return { columns: cols, rows: sample }
    }

    return (
    <div className="p-8 max-w-7xl mx-auto space-y-6 pb-24">

      {/* Fullscreen overlay */}
        {fullscreenTable && (
          <FullscreenTable
            title={fullscreenTable.title}
            subtitle={fullscreenTable.subtitle}
            columns={fullscreenTable.columns}
            rows={fullscreenTable.rows}
            accentColor={fullscreenTable.accentColor}
            onClose={() => setFullscreenTable(null)}
          />
        )}

        <FloatingCleanBtn />

        {showEditSource && (
          <EditSourceModal
            sources={sources}
            activeSourceName={activeSourceName}
            activeDestinationName={activeDestinationName}
            onConfirm={handleEditSourceConfirm}
            onClose={() => setShowEditSource(false)}
          />
        )}

        {/* SQL Modal */}
        {showSQL && (
          <div
            className="fixed inset-0 z-50 flex items-center justify-center p-4"
            style={{ background: 'rgba(0,0,0,0.8)' }}
            onClick={() => setShowSQL(false)}
          >
            <div
              className="rounded-2xl p-6 max-w-4xl w-full max-h-[85vh] flex flex-col"
              style={{ background: '#0d0a1a', border: '1px solid rgba(255,255,255,0.1)' }}
              onClick={e => e.stopPropagation()}
            >
              <div className="flex items-center justify-between mb-4">
                <div>
                  <h2 className="font-bold text-white flex items-center gap-2">
                    <svg width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4" />
                    </svg>
                    Live SQL Translation
                  </h2>
                  <p className="text-xs mt-1" style={{ color: 'rgba(255,255,255,0.4)' }}>Portable CTE chain — runnable in PostgreSQL, BigQuery, Snowflake, and more.</p>
                </div>
                <div className="flex items-center gap-3">
                  <button
                    onClick={() => navigator.clipboard.writeText(liveSQLPreview)}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold"
                    style={{ background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.1)', color: 'rgba(255,255,255,0.6)' }}
                  >
                    Copy SQL
                  </button>
                  <button onClick={() => setShowSQL(false)} style={{ color: 'rgba(255,255,255,0.4)', fontSize: '20px' }}>✕</button>
                </div>
              </div>
              <pre
                className="text-xs overflow-auto p-5 rounded-xl font-mono leading-relaxed flex-1"
                style={{ color: '#a5f3fc', background: 'rgba(0,0,0,0.4)', border: '1px solid rgba(255,255,255,0.05)' }}
              >
                {liveSQLPreview}
              </pre>
            </div>
          </div>
        )}

        {showOriginal && analysis.original_preview && (
          <div
            className="fixed inset-0 z-50 flex items-center justify-center p-4"
            style={{ background: 'rgba(0,0,0,0.8)' }}
            onClick={() => setShowOriginal(false)}
          >
            <div
              className="rounded-2xl p-6 max-w-6xl w-full max-h-[85vh] overflow-auto"
              style={{ background: '#0d0a1a', border: '1px solid rgba(255,255,255,0.1)' }}
              onClick={e => e.stopPropagation()}
            >
              <div className="flex items-center justify-between mb-4">
                <div>
                  <h2 className="font-bold text-white">Original Data — first 50 rows</h2>
                  <p className="text-xs mt-0.5" style={{ color: 'rgba(255,255,255,0.4)' }}>Untouched source.</p>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => {
                      const cols = Object.keys(analysis.original_preview[0] || {})
                      openFullscreen('Original Data', cols, analysis.original_preview, 'First 50 rows · untouched source', '#a78bfa')
                      setShowOriginal(false)
                    }}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold"
                    style={{ background: 'rgba(124,58,237,0.1)', border: '1px solid rgba(124,58,237,0.25)', color: '#a78bfa' }}
                  >
                    <svg width="11" height="11" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                      <path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/>
                    </svg>
                    Fullscreen
                  </button>
                  <button onClick={() => setShowOriginal(false)} style={{ color: 'rgba(255,255,255,0.4)', fontSize: '20px' }}>✕</button>
                </div>
              </div>
              <div className="overflow-auto">
                <table className="w-full text-xs" style={{ color: 'rgba(255,255,255,0.7)' }}>
                  <thead className="sticky top-0" style={{ background: '#0d0a1a' }}>
                    <tr>
                      <th
                        className="p-2 text-left font-semibold"
                        style={{ color: 'rgba(255,255,255,0.3)', borderBottom: '1px solid rgba(255,255,255,0.08)' }}
                      >#</th>
                      {Object.keys(analysis.original_preview[0] || {}).map(k => (
                        <th
                          key={k}
                          className="p-2 text-left font-semibold text-white whitespace-nowrap"
                          style={{ borderBottom: '1px solid rgba(255,255,255,0.08)' }}
                        >{k}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {analysis.original_preview.map((row: any, i: number) => (
                      <tr
                        key={i}
                        style={{
                          background: i % 2 === 0 ? 'rgba(255,255,255,0.02)' : 'transparent',
                          borderBottom: '1px solid rgba(255,255,255,0.04)'
                        }}
                      >
                        <td className="p-2" style={{ color: 'rgba(255,255,255,0.2)' }}>{i + 1}</td>
                        {Object.values(row).map((val: any, j) => (
                          <td key={j} className="p-2 whitespace-nowrap">
                            {val === '' ? <span style={{ color: '#f87171', fontStyle: 'italic' }}>null</span> : String(val)}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}

        {/* ── Page header ─────────────────────────────────────────────────────── */}
        <div className="flex items-start justify-between flex-wrap gap-4">
          <div className="flex items-center gap-3">
            <div
              className="w-8 h-8 rounded-lg flex items-center justify-center"
              style={{ background: 'rgba(124,58,237,0.2)', border: '1px solid rgba(124,58,237,0.3)' }}
            >
              <svg width="14" height="14" fill="none" stroke="#a78bfa" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M3 3v18h18"/><path d="M7 16l4-4 4 4 4-8"/>
              </svg>
            </div>
            <div>
              <h1 className="text-2xl font-bold text-white">Analysis</h1>
              <div className="flex items-center gap-2 mt-0.5 flex-wrap">
                <button
                  onClick={() => setShowEditSource(true)}
                  className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-medium transition-all group"
                  style={{ background: 'rgba(124,58,237,0.1)', border: '1px solid rgba(124,58,237,0.25)', color: '#a78bfa' }}
                >
                  <span>📥 {activeSourceName}</span>
                  <svg
                    width="10" height="10" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24"
                    className="opacity-60 group-hover:opacity-100"
                  >
                    <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
                    <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
                  </svg>
                </button>
                {activeDestinationName && (
                  <>
                    <span style={{ color: 'rgba(255,255,255,0.2)' }}>→</span>
                    <span
                      className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-medium"
                      style={{ background: 'rgba(52,211,153,0.08)', border: '1px solid rgba(52,211,153,0.2)', color: '#34d399' }}
                    >
                      📤 {activeDestinationName}
                    </span>
                  </>
                )}
                {analysis.primary_key && (
                  <span className="ml-1" style={{ color: '#60a5fa', fontSize: '12px' }}>· PK: {analysis.primary_key}</span>
                )}
                {ts.detected && (
                  <span style={{ color: '#22d3ee', fontSize: '12px' }}>· {ts.date_col} ({ts.frequency})</span>
                )}
              </div>
            </div>
          </div>
          <div className="flex gap-2 flex-wrap">
            <button
              onClick={() => setShowEditSource(true)}
              className="flex items-center gap-1.5 px-4 py-2 rounded-xl text-sm font-medium transition-all"
              style={{ background: 'rgba(124,58,237,0.1)', border: '1px solid rgba(124,58,237,0.25)', color: '#a78bfa' }}
            >
              <svg width="12" height="12" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
                <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
              </svg>
              Edit Flow Source
            </button>
            <button
              onClick={() => setShowOriginal(true)}
              className="px-4 py-2 rounded-xl text-sm font-medium transition-all"
              style={{ background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.08)', color: 'rgba(255,255,255,0.7)' }}
            >
              View Original
            </button>
            <a
              href={`${API_BASE}${analysis.original_file}`}
              className="px-4 py-2 rounded-xl text-sm font-medium transition-all"
              style={{ background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.08)', color: 'rgba(255,255,255,0.7)' }}
            >
              Download Original
            </a>
            <button
              onClick={runAnalysis}
              className="px-4 py-2 rounded-xl text-sm font-semibold text-white transition-all"
              style={{ background: 'rgba(124,58,237,0.25)', border: '1px solid rgba(124,58,237,0.35)' }}
            >
              Re-analyze
            </button>
          </div>
        </div>

        {/* Insight banner */}
        <div
          className="rounded-2xl px-6 py-4 flex items-start gap-3"
          style={{ background: 'rgba(59,130,246,0.07)', border: '1px solid rgba(59,130,246,0.15)' }}
        >
          <span style={{ color: '#60a5fa', fontSize: '16px', marginTop: '1px' }}>💡</span>
          <p className="text-sm" style={{ color: '#bfdbfe' }}>{analysis.insight}</p>
        </div>

        {/* Stat cards */}
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <StatCard label="Total Rows" value={info.rows?.toLocaleString() || 0} />
          <StatCard label="Columns" value={info.columns || 0} />
          <StatCard label="Memory" value={`${(info.memory_mb || 0).toFixed(1)} MB`} />
          <StatCard label="Problems Found" value={totalProblems.toLocaleString()} color={totalProblems > 0 ? '#f87171' : '#4ade80'} />
        </div>

        {/* Problem pills */}
        {(counts.missing > 0 || counts.duplicates > 0 || counts.outliers > 0) && (
          <div className="flex flex-wrap gap-3">
            {counts.missing > 0 && (
              <div className="rounded-2xl px-5 py-3" style={{ background: 'rgba(249,115,22,0.08)', border: '1px solid rgba(249,115,22,0.2)' }}>
                <p className="font-bold text-lg" style={{ color: '#fb923c' }}>{counts.missing.toLocaleString()}</p>
                <p className="text-xs" style={{ color: 'rgba(249,115,22,0.6)' }}>Missing values</p>
              </div>
            )}
            {counts.duplicates > 0 && (
              <div className="rounded-2xl px-5 py-3" style={{ background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.2)' }}>
                <p className="font-bold text-lg" style={{ color: '#f87171' }}>{counts.duplicates.toLocaleString()}</p>
                <p className="text-xs" style={{ color: 'rgba(239,68,68,0.6)' }}>Duplicate rows</p>
              </div>
            )}
            {counts.outliers > 0 && (
              <div className="rounded-2xl px-5 py-3" style={{ background: 'rgba(234,179,8,0.08)', border: '1px solid rgba(234,179,8,0.2)' }}>
                <p className="font-bold text-lg" style={{ color: '#fbbf24' }}>{counts.outliers.toLocaleString()}</p>
                <p className="text-xs" style={{ color: 'rgba(234,179,8,0.6)' }}>Outlier values</p>
              </div>
            )}
          </div>
        )}

        {/* Quality + missing bars */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div
            className="rounded-3xl p-8 text-center"
            style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.07)' }}
          >
            <div
              className="w-28 h-28 mx-auto mb-4 rounded-3xl flex items-center justify-center"
              style={{
                background: analysis.quality_score > 80
                  ? 'linear-gradient(135deg,#059669,#0d9488)'
                  : 'linear-gradient(135deg,#dc2626,#ea580c)'
              }}
            >
              <span className="text-3xl font-black text-white">{analysis.quality_score}%</span>
            </div>
            <h3 className="font-bold text-white text-lg mb-1">Dataset Quality</h3>
            <p className="text-sm" style={{ color: 'rgba(255,255,255,0.4)' }}>
              {analysis.quality_score > 80 ? 'Excellent!' : 'Needs cleaning'}
            </p>
          </div>
          <div
            className="lg:col-span-2 rounded-3xl p-6"
            style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.07)' }}
          >
            <h3 className="font-semibold text-white mb-4">Missing Values by Column</h3>
            {analysis.top_missing?.length > 0 ? (
              <div className="space-y-3">
                {analysis.top_missing.map(({ col, count }: any) => (
                  <div key={col}>
                    <div className="flex justify-between text-sm mb-1.5">
                      <span className="font-medium text-white">{col}</span>
                      <span className="font-semibold" style={{ color: '#fb923c' }}>
                        {count.toLocaleString()} ({Math.round(count / info.rows * 100)}%)
                      </span>
                    </div>
                    <div className="h-1.5 rounded-full overflow-hidden" style={{ background: 'rgba(255,255,255,0.08)' }}>
                      <div
                        className="h-full rounded-full"
                        style={{ width: `${Math.min(count / info.rows * 100, 100)}%`, background: '#f97316' }}
                      />
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="font-semibold text-center mt-6" style={{ color: '#4ade80' }}>No missing values!</p>
            )}
          </div>
        </div>

        {/* Main tabs */}
        <div
          className="rounded-3xl overflow-hidden"
          style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.07)' }}
        >
          <div
            className="flex overflow-x-auto p-2 gap-1"
            style={{ borderBottom: '1px solid rgba(255,255,255,0.07)', background: 'rgba(255,255,255,0.02)' }}
          >
            {tabs.map(t => (
              <button
                key={t.id}
                onClick={() => setAnalysisTab(t.id)}
                className="px-4 py-2 rounded-xl text-sm font-medium flex items-center gap-1.5 whitespace-nowrap transition-all"
                style={{
                  background: analysisTab === t.id ? 'rgba(124,58,237,0.2)' : 'transparent',
                  color: analysisTab === t.id ? '#a78bfa' : 'rgba(255,255,255,0.4)',
                  border: analysisTab === t.id ? '1px solid rgba(124,58,237,0.3)' : '1px solid transparent'
                }}
              >
                {t.label}
                {(t as any).badge > 0 && (
                  <span
                    className="px-1.5 py-0.5 rounded-full text-xs font-bold"
                    style={{ background: '#ef4444', color: 'white', fontSize: '10px' }}
                  >
                    {(t as any).badge.toLocaleString()}
                  </span>
                )}
              </button>
            ))}
          </div>

          <div className="p-6">

            {/* ── Preview tab ── */}
            {analysisTab === 'preview' && (
              <div>
                <div className="flex items-center justify-between mb-3">
                  <p className="text-xs font-semibold" style={{ color: 'rgba(255,255,255,0.4)' }}>
                    Showing {sample.length} sample rows
                  </p>
                  <button
                    onClick={() => {
                      const { columns, rows } = getPreviewFullscreenData()
                      openFullscreen('Data Preview', columns, rows, `${sample.length} sample rows · ${activeSourceName}`, '#a78bfa')
                    }}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold"
                    style={{ background: 'rgba(124,58,237,0.1)', border: '1px solid rgba(124,58,237,0.25)', color: '#a78bfa' }}
                  >
                    <svg width="11" height="11" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                      <path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/>
                    </svg>
                    Fullscreen
                  </button>
                </div>
                <div className="overflow-auto max-h-96">
                  <table className="w-full text-xs" style={{ color: 'rgba(255,255,255,0.65)' }}>
                    <thead className="sticky top-0" style={{ background: 'rgba(7,5,15,0.95)' }}>
                      <tr>{Object.keys(firstRow).map(k => (
                        <th
                          key={k}
                          className="p-2.5 text-left font-semibold whitespace-nowrap"
                          style={{ borderBottom: '1px solid rgba(255,255,255,0.08)', color: 'rgba(255,255,255,0.8)' }}
                        >{k}</th>
                      ))}</tr>
                    </thead>
                    <tbody>
                      {sample.map((row: any, i: number) => (
                        <tr
                          key={i}
                          style={{ borderBottom: '1px solid rgba(255,255,255,0.04)' }}
                          onMouseEnter={e => (e.currentTarget.style.background = 'rgba(124,58,237,0.05)')}
                          onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
                        >
                          {Object.values(row).map((val: any, j) => (
                            <td key={j} className="p-2.5 whitespace-nowrap">
                              {val === '' ? <span style={{ color: '#f87171', fontStyle: 'italic' }}>null</span>
                                : typeof val === 'number' ? val.toLocaleString() : String(val)}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {/* ── Columns tab ── */}
            {analysisTab === 'info' && (
              <div>
                <div className="flex items-center justify-between mb-3">
                  <p className="text-xs font-semibold" style={{ color: 'rgba(255,255,255,0.4)' }}>
                    {colInfo.length} columns
                  </p>
                  <button
                    onClick={() => {
                      const colHeaders = ['Column','Type','Non-Null','Missing','Unique','Sample']
                      const colRows = colInfo.map((col: any) => ({
                        Column: col.column + (col.column === analysis.primary_key ? ' (PK)' : '') + (col.column === ts.date_col ? ' (Date)' : ''),
                        Type: col.dtype,
                        'Non-Null': col.non_null,
                        Missing: col.null,
                        Unique: col.unique,
                        Sample: col.sample,
                      }))
                      openFullscreen('Column Info', colHeaders, colRows, `${colInfo.length} columns · ${activeSourceName}`, '#6366f1')
                    }}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold"
                    style={{ background: 'rgba(99,102,241,0.1)', border: '1px solid rgba(99,102,241,0.25)', color: '#818cf8' }}
                  >
                    <svg width="11" height="11" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                      <path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/>
                    </svg>
                    Fullscreen
                  </button>
                </div>
                <div className="overflow-auto max-h-96">
                  <table className="w-full text-sm" style={{ color: 'rgba(255,255,255,0.65)' }}>
                    <thead className="sticky top-0" style={{ background: 'rgba(7,5,15,0.95)' }}>
                      <tr>{['Column','Type','Non-Null','Missing','Unique','Sample'].map(h => (
                        <th
                          key={h}
                          className="p-3 text-left font-semibold whitespace-nowrap"
                          style={{ borderBottom: '1px solid rgba(255,255,255,0.08)', color: 'rgba(255,255,255,0.8)' }}
                        >{h}</th>
                      ))}</tr>
                    </thead>
                    <tbody>
                      {colInfo.map((col: any, i: number) => (
                        <tr
                          key={i}
                          style={{ borderBottom: '1px solid rgba(255,255,255,0.04)' }}
                          onMouseEnter={e => (e.currentTarget.style.background = 'rgba(124,58,237,0.05)')}
                          onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
                        >
                          <td className="p-3 font-medium text-white whitespace-nowrap">
                            {col.column}
                            {col.column === analysis.primary_key && <Pill color="#3b82f6">PK</Pill>}
                            {col.column === ts.date_col && <span className="ml-1"><Pill color="#06b6d4">Date</Pill></span>}
                          </td>
                          <td className="p-3"><Pill color="#6366f1">{col.dtype}</Pill></td>
                          <td className="p-3" style={{ color: '#4ade80' }}>{col.non_null.toLocaleString()}</td>
                          <td className="p-3">
                            {col.null > 0
                              ? <span className="font-semibold" style={{ color: '#fb923c' }}>{col.null.toLocaleString()}</span>
                              : <span style={{ color: '#4ade80' }}>0</span>}
                          </td>
                          <td className="p-3" style={{ color: 'rgba(255,255,255,0.4)' }}>{col.unique.toLocaleString()}</td>
                          <td className="p-3 whitespace-nowrap" style={{ color: 'rgba(255,255,255,0.4)' }}>{col.sample}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {/* ── Statistics tab ── */}
            {analysisTab === 'describe' && (
              <div>
                <div className="flex items-center justify-between mb-3">
                  <p className="text-xs font-semibold" style={{ color: 'rgba(255,255,255,0.4)' }}>
                    Descriptive statistics for numeric columns
                  </p>
                  <button
                    onClick={() => {
                      const statRows = describeStats.map(stat => {
                        const row: any = { Stat: stat }
                        describeCols.forEach(col => {
                          row[col] = describe[col][stat] !== undefined && describe[col][stat] !== ''
                            ? (typeof describe[col][stat] === 'number'
                              ? describe[col][stat].toLocaleString(undefined, { maximumFractionDigits: 2 })
                              : String(describe[col][stat]))
                            : '—'
                        })
                        return row
                      })
                      openFullscreen('Statistics', ['Stat', ...describeCols], statRows, `${describeCols.length} numeric columns`, '#8b5cf6')
                    }}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold"
                    style={{ background: 'rgba(139,92,246,0.1)', border: '1px solid rgba(139,92,246,0.25)', color: '#a78bfa' }}
                  >
                    <svg width="11" height="11" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                      <path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/>
                    </svg>
                    Fullscreen
                  </button>
                </div>
                <div className="overflow-auto max-h-96">
                  <table className="w-full text-sm" style={{ color: 'rgba(255,255,255,0.65)' }}>
                    <thead className="sticky top-0" style={{ background: 'rgba(7,5,15,0.95)' }}>
                      <tr>
                        <th
                          className="p-3 text-left font-semibold"
                          style={{ borderBottom: '1px solid rgba(255,255,255,0.08)', color: 'white' }}
                        >Stat</th>
                        {describeCols.map(col => (
                          <th
                            key={col}
                            className="p-3 text-left font-semibold whitespace-nowrap"
                            style={{ borderBottom: '1px solid rgba(255,255,255,0.08)', color: 'white' }}
                          >{col}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {describeStats.map(stat => (
                        <tr
                          key={stat}
                          style={{ borderBottom: '1px solid rgba(255,255,255,0.04)' }}
                          onMouseEnter={e => (e.currentTarget.style.background = 'rgba(124,58,237,0.05)')}
                          onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
                        >
                          <td className="p-3 font-bold whitespace-nowrap" style={{ color: '#a78bfa' }}>{stat}</td>
                          {describeCols.map(col => (
                            <td key={col} className="p-3 whitespace-nowrap">
                              {describe[col][stat] !== undefined && describe[col][stat] !== ''
                                ? (typeof describe[col][stat] === 'number'
                                  ? describe[col][stat].toLocaleString(undefined, { maximumFractionDigits: 2 })
                                  : String(describe[col][stat]))
                                : <span style={{ color: 'rgba(255,255,255,0.15)' }}>—</span>}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {/* ── Problems tab ── */}
            {analysisTab === 'problems' && (
              <div>
                {problems.length === 0 ? (
                  <div className="text-center py-12">
                    <div className="text-4xl mb-3">✨</div>
                    <p className="font-bold text-white mb-1">No problems found!</p>
                    <p className="text-sm" style={{ color: 'rgba(255,255,255,0.35)' }}>Your data is clean.</p>
                  </div>
                ) : (
                  <div>
                    {totalProblems > problems.length && (
                      <div
                        className="rounded-xl px-4 py-2.5 mb-4 text-sm"
                        style={{ background: 'rgba(234,179,8,0.08)', border: '1px solid rgba(234,179,8,0.2)', color: '#fbbf24' }}
                      >
                        Showing {problems.length.toLocaleString()} of {totalProblems.toLocaleString()} total problems
                      </div>
                    )}
                    <div className="flex flex-wrap gap-3 mb-4">
                      <div>
                        <label className="text-xs block mb-1" style={{ color: 'rgba(255,255,255,0.4)' }}>Issue Type</label>
                        <select
                          value={filterIssue}
                          onChange={e => setFilterIssue(e.target.value)}
                          className="text-sm text-white rounded-xl px-3 py-2 outline-none"
                          style={{ background: 'rgba(255,255,255,0.08)', border: '1px solid rgba(255,255,255,0.12)' }}
                        >
                          {issueTypes.map(t => <option key={t} value={t} style={{ background: '#0d0a1a' }}>{t}</option>)}
                        </select>
                      </div>
                      <div>
                        <label className="text-xs block mb-1" style={{ color: 'rgba(255,255,255,0.4)' }}>Column</label>
                        <select
                          value={filterCol}
                          onChange={e => setFilterCol(e.target.value)}
                          className="text-sm text-white rounded-xl px-3 py-2 outline-none"
                          style={{ background: 'rgba(255,255,255,0.08)', border: '1px solid rgba(255,255,255,0.12)' }}
                        >
                          {problemCols.map(c => <option key={c} value={c} style={{ background: '#0d0a1a' }}>{c}</option>)}
                        </select>
                      </div>
                      <div className="flex items-end pb-2">
                        <span className="text-sm" style={{ color: 'rgba(255,255,255,0.3)' }}>{filteredProblems.length.toLocaleString()} shown</span>
                      </div>
                      <div className="flex items-end pb-2 ml-auto">
                        <button
                          onClick={() => openFullscreen(
                            'Problems',
                            ['Row #', analysis.primary_key ? `${analysis.primary_key} (PK)` : 'Identifier', 'Column', 'Issue', 'Value'],
                            filteredProblems.map((p: any) => ({
                              'Row #': p.row_number,
                              [analysis.primary_key ? `${analysis.primary_key} (PK)` : 'Identifier']: p.primary_key,
                              'Column': p.column,
                              'Issue': p.issue,
                              'Value': p.value || '—',
                            })),
                            `${filteredProblems.length} problems`,
                            '#f87171'
                          )}
                          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold"
                          style={{ background: 'rgba(239,68,68,0.1)', border: '1px solid rgba(239,68,68,0.25)', color: '#f87171' }}
                        >
                          <svg width="11" height="11" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                            <path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/>
                          </svg>
                          Fullscreen
                        </button>
                      </div>
                    </div>
                    <div className="flex flex-wrap gap-2 mb-4">
                      {issueTypes.filter(t => t !== 'All').map(t => {
                        const count = problems.filter((p: any) => p.issue === t).length
                        const badge = ISSUE_BADGES[t] || { bg: 'rgba(255,255,255,0.1)', text: 'rgba(255,255,255,0.5)' }
                        return (
                          <span
                            key={t}
                            onClick={() => setFilterIssue(filterIssue === t ? 'All' : t)}
                            className="px-3 py-1 rounded-full text-xs font-semibold cursor-pointer"
                            style={{
                              background: badge.bg,
                              color: badge.text,
                              outline: filterIssue === t ? `2px solid ${badge.text}40` : 'none'
                            }}
                          >
                            {t}: {count.toLocaleString()}
                          </span>
                        )
                      })}
                    </div>
                    <div className="overflow-auto max-h-72">
                      <table className="w-full text-sm" style={{ color: 'rgba(255,255,255,0.65)' }}>
                        <thead className="sticky top-0" style={{ background: 'rgba(7,5,15,0.95)' }}>
                          <tr>
                            {['Row #', analysis.primary_key ? `${analysis.primary_key} (PK)` : 'Identifier', 'Column', 'Issue', 'Value'].map(h => (
                              <th
                                key={h}
                                className="p-3 text-left font-semibold whitespace-nowrap"
                                style={{ borderBottom: '1px solid rgba(255,255,255,0.08)', color: 'white' }}
                              >{h}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {filteredProblems.map((p: any, i: number) => {
                            const b = ISSUE_BADGES[p.issue] || { bg: 'rgba(255,255,255,0.1)', text: 'rgba(255,255,255,0.5)' }
                            return (
                              <tr
                                key={i}
                                style={{ borderBottom: '1px solid rgba(255,255,255,0.04)' }}
                                onMouseEnter={e => (e.currentTarget.style.background = 'rgba(124,58,237,0.05)')}
                                onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
                              >
                                <td className="p-3 font-mono" style={{ color: 'rgba(255,255,255,0.3)' }}>{p.row_number}</td>
                                <td className="p-3 font-mono" style={{ color: '#60a5fa' }}>{p.primary_key}</td>
                                <td className="p-3 font-medium text-white">{p.column}</td>
                                <td className="p-3">
                                  <span
                                    className="px-2 py-0.5 rounded-full text-xs font-semibold"
                                    style={{ background: b.bg, color: b.text }}
                                  >{p.issue}</span>
                                </td>
                                <td className="p-3 font-mono" style={{ color: 'rgba(255,255,255,0.3)' }}>{p.value || '—'}</td>
                              </tr>
                            )
                          })}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* ── AI Clean tab ── */}
            {analysisTab === 'aichat' && (
              <AIChatClean
                colInfo={colInfo}
                sampleData={sample}
                activeFile={activeFile}
                activeSourceName={activeSourceName}
                setCustomCleanOps={setCustomCleanOps}
                setOptions={setOptions}
              />
            )}

            {/* ── Time series tab ── */}
            {analysisTab === 'timeseries' && ts.chart_data && (
              <div>
                <div className="flex items-center gap-3 mb-5">
                  <div>
                    <p className="font-bold text-white">{ts.chart_data.value_col} over time</p>
                    <p className="text-xs mt-0.5" style={{ color: 'rgba(255,255,255,0.4)' }}>
                      Column: <span style={{ color: '#22d3ee' }}>{ts.date_col}</span>
                      &nbsp;·&nbsp; Frequency: <span style={{ color: '#22d3ee' }}>{ts.frequency}</span>
                      &nbsp;·&nbsp; {ts.chart_data.dates.length} data points
                    </p>
                  </div>
                </div>
                <TimeSeriesChart chartData={ts.chart_data} />
              </div>
            )}
          </div>
        </div>

        {/* ── CLEAN SECTION ─────────────────────────────────────────────────── */}
        <div
          id="clean-section"
          className="rounded-3xl overflow-hidden"
          style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(124,58,237,0.2)' }}
        >
          <div
            className="flex items-center justify-between px-6 py-4"
            style={{ borderBottom: '1px solid rgba(255,255,255,0.07)', background: 'rgba(124,58,237,0.05)' }}
          >
            <div className="flex items-center gap-3">
              <div
                className="w-7 h-7 rounded-lg flex items-center justify-center"
                style={{ background: 'rgba(124,58,237,0.25)', border: '1px solid rgba(124,58,237,0.35)' }}
              >
                <svg width="13" height="13" fill="none" stroke="#a78bfa" strokeWidth="2.5" viewBox="0 0 24 24">
                  <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
                </svg>
              </div>
              <div>
                <h2 className="font-bold text-white text-sm">Clean Data</h2>
                <p className="text-xs" style={{ color: 'rgba(255,255,255,0.35)' }}>Issues detected in your data are ticked automatically.</p>
              </div>
            </div>
            {anyCleanSelected && (
              <span
                className="px-2.5 py-1 rounded-full text-xs font-semibold"
                style={{ background: 'rgba(124,58,237,0.2)', color: '#a78bfa', border: '1px solid rgba(124,58,237,0.3)' }}
              >
                {Object.values(options).filter(Boolean).length} selected
              </span>
            )}
          </div>

          <div className="p-6 space-y-6">
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {[...CLEANING_OPTIONS, ...customCleanOps.map(op => ({
                key: op.key,
                label: `✨ ${op.label}`,
                desc: 'AI-generated cleaning step',
                issueKey: ''
              }))].map(({ key, label, desc, issueKey }) => {
                const detected = analysis.issues?.[issueKey]
                const checked = options[key]
                return (
                  <div
                    key={key}
                    onClick={() => setOptions(p => ({ ...p, [key]: !p[key] }))}
                    className="p-4 rounded-2xl cursor-pointer transition-all"
                    style={{
                      background: checked ? 'rgba(124,58,237,0.1)' : 'rgba(255,255,255,0.03)',
                      border: detected
                        ? '1px solid rgba(52,211,153,0.35)'
                        : `1px solid rgba(124,58,237,${checked ? '0.3' : '0.1'})`
                    }}
                  >
                    <div className="flex items-center gap-3">
                      <div
                        className="w-5 h-5 rounded-md flex items-center justify-center flex-shrink-0"
                        style={{
                          background: checked ? '#7c3aed' : 'transparent',
                          border: `2px solid ${checked ? '#7c3aed' : 'rgba(255,255,255,0.25)'}`
                        }}
                      >
                        {checked && <span className="text-white font-black" style={{ fontSize: '10px' }}>✓</span>}
                      </div>
                      <div>
                        <div className="font-semibold text-white text-sm">
                          {label}
                          {detected && (
                            <span
                              className="ml-2 px-2 py-0.5 rounded-full text-xs"
                              style={{ background: 'rgba(52,211,153,0.12)', color: '#34d399' }}
                            >Detected</span>
                          )}
                        </div>
                        <div className="text-xs mt-0.5" style={{ color: 'rgba(255,255,255,0.35)' }}>{desc}</div>
                      </div>
                    </div>
                  </div>
                )
              })}
            </div>

            {/* Advanced Imputation */}
            <div
              className="rounded-2xl p-5"
              style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.07)' }}
            >
              <div className="flex items-center justify-between mb-1">
                <p className="font-bold text-white text-sm">
                  Advanced Imputation
                  {analysis.issues?.missing && (
                    <span
                      className="ml-2 px-2 py-0.5 rounded-full text-xs"
                      style={{ background: 'rgba(52,211,153,0.12)', color: '#34d399' }}
                    >Missing detected</span>
                  )}
                </p>
                <span
                  className="text-xs px-2.5 py-1 rounded-full font-medium"
                  style={{
                    background: imputationMethod === 'none' ? 'rgba(255,255,255,0.08)' : 'rgba(124,58,237,0.2)',
                    color: imputationMethod === 'none' ? 'rgba(255,255,255,0.35)' : '#a78bfa'
                  }}
                >
                  {imputationMethod === 'none' ? 'Off' : 'Active'}
                </span>
              </div>
              <p className="text-xs mb-4" style={{ color: 'rgba(255,255,255,0.3)' }}>
                Statistical fill technique. Use instead of "Fill Missing" above, not both.
              </p>
              <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-2">
                {IMPUTATION_METHODS.map(({ value, label, desc }) => (
                  <div
                    key={value}
                    onClick={() => setImputationMethod(value)}
                    className="p-3 rounded-xl cursor-pointer transition-all relative group"
                    style={{
                      background: imputationMethod === value ? 'rgba(124,58,237,0.15)' : 'rgba(255,255,255,0.03)',
                      border: `1px solid rgba(124,58,237,${imputationMethod === value ? '0.4' : '0.1'})`,
                      color: imputationMethod === value ? 'white' : 'rgba(255,255,255,0.45)'
                    }}
                  >
                    <p className="font-semibold text-sm">{label}</p>
                    <div
                      className="absolute bottom-full left-0 mb-2 w-52 rounded-xl p-3 text-xs hidden group-hover:block z-10 pointer-events-none"
                      style={{ background: '#0d0a1a', border: '1px solid rgba(255,255,255,0.1)', color: 'rgba(255,255,255,0.7)' }}
                    >
                      {desc}
                    </div>
                  </div>
                ))}
              </div>
              {imputationMethod !== 'none' && (
                <div
                  className="mt-3 px-4 py-2 rounded-xl"
                  style={{ background: 'rgba(124,58,237,0.08)', border: '1px solid rgba(124,58,237,0.15)' }}
                >
                  <p className="text-xs" style={{ color: '#c4b5fd' }}>
                    <span className="font-bold">Selected:</span> {selectedMethod?.label} — {selectedMethod?.desc}
                  </p>
                </div>
              )}
            </div>

            {/* Buttons */}
            <div className="flex gap-3">
              <button
                onClick={() => setShowSQL(true)}
                className="px-5 py-3.5 rounded-xl font-semibold transition-all flex items-center gap-2 border"
                style={{ background: 'rgba(59,130,246,0.1)', borderColor: 'rgba(59,130,246,0.2)', color: '#60a5fa' }}
              >
                <svg width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4" />
                </svg>
                View SQL
              </button>
              <button
                onClick={() => {
                  setOptions({
                    remove_duplicates: false, remove_empty_rows: false, standardize_text: false,
                    fix_dates: false, handle_outliers: false, fill_missing: false
                  })
                  setImputationMethod('none')
                }}
                className="px-6 py-3.5 rounded-xl font-semibold transition-all"
                style={{ background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.08)', color: 'rgba(255,255,255,0.6)' }}
              >
                Reset
              </button>
              <button
                onClick={runClean}
                disabled={cleaning || !anyCleanSelected}
                className={`flex-1 py-3.5 rounded-xl font-bold text-white transition-all disabled:opacity-40 ${anyCleanSelected && !cleaning ? 'sparkle-btn' : ''}`}
                style={{ background: 'linear-gradient(135deg, #7c3aed, #2563eb)' }}
              >
                {cleaning ? (
                  <span className="flex items-center justify-center gap-2">
                    <svg className="animate-spin" width="16" height="16" fill="none" stroke="white" strokeWidth="2.5" viewBox="0 0 24 24">
                      <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/>
                    </svg>
                    Cleaning...
                  </span>
                ) : 'Run Cleaning'}
              </button>
            </div>
          </div>
        </div>
      </div>
    )
  }
