import { useState, useEffect, useMemo, useCallback, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { useStore } from '../store'

const TRANSFORMATIONS = [
  { id: 'remove_duplicates', label: 'Remove Duplicates', icon: '🗑️', desc: 'Drop identical rows',   color: '#ef4444' },
  { id: 'fill_missing',      label: 'Fill Missing',      icon: '🔧', desc: 'Median / mode fill',    color: '#f97316' },
  { id: 'standardize_text',  label: 'Standardize Text',  icon: '✏️', desc: 'Lowercase + trim',      color: '#8b5cf6' },
  { id: 'cap_outliers',      label: 'Cap Outliers',      icon: '📊', desc: 'Modified Z-Score',      color: '#eab308' },
  { id: 'remove_empty_rows', label: 'Remove Empty Rows', icon: '🧹', desc: 'Drop all-null rows',    color: '#06b6d4' },
  { id: 'fix_dates',         label: 'Fix Dates',         icon: '📅', desc: 'Standardize formats',   color: '#22c55e' },
  { id: 'ai_clean',          label: 'Clean with AI',     icon: '✨', desc: 'Smart AI-powered clean', color: '#a855f7' },
  { id: 'ai_custom',         label: 'AI Custom Rule',    icon: '🤖', desc: 'Natural language rule',  color: '#ec4899' },
]


const TRANSFORM_TO_CLEAN: Record<string, string> = {
  remove_duplicates: 'remove_duplicates',
  fill_missing:      'fill_missing',
  standardize_text:  'standardize_text',
  cap_outliers:      'handle_outliers',
  remove_empty_rows: 'remove_empty_rows',
  fix_dates:         'fix_dates',
  ai_clean:          'ai_clean',
  ai_custom:         'ai_clean',   // custom AI rules use the ai_clean backend path
}

const ISSUE_TO_CLEAN: Record<string, string> = {
  duplicates: 'remove_duplicates',
  empty_rows: 'remove_empty_rows',
  missing:    'fill_missing',
  outliers:   'handle_outliers',
  messy_text: 'standardize_text',
  fix_dates:  'fix_dates',
}



function OriginalDataPreview({ file }: { file: File | null }) {
  const [rows, setRows] = useState<any[]>([])
  const [cols, setCols] = useState<string[]>([])
  useEffect(() => {
    if (!file) return
    const reader = new FileReader()
    reader.onload = (e) => {
      const text = e.target?.result as string
      const lines = text.split('\n').filter(Boolean).slice(0, 51) // header + 50 rows
      if (lines.length === 0) return
      const headers = lines[0].split(',').map(h => h.trim().replace(/^"|"$/g, ''))
      setCols(headers)
      const parsed = lines.slice(1).map(line => {
        const vals = line.split(',')
        const row: any = {}
        headers.forEach((h, i) => { row[h] = vals[i]?.trim().replace(/^"|"$/g, '') || '' })
        return row
      })
      setRows(parsed)
    }
    reader.readAsText(file)
  }, [file])
  if (rows.length === 0) return <div className="flex items-center justify-center py-16"><p className="text-sm" style={{ color:'rgba(255,255,255,0.3)' }}>Loading...</p></div>
  return (
    <div className="overflow-auto flex-1" style={{ maxHeight: '60vh' }}>
      <table className="w-full text-xs" style={{ color:'rgba(255,255,255,0.65)', borderCollapse:'collapse' }}>
        <thead className="sticky top-0" style={{ background:'#0a0714', zIndex:10 }}>
          <tr>
            <th className="p-3 text-left font-semibold" style={{ borderBottom:'1px solid rgba(255,255,255,0.08)', color:'rgba(255,255,255,0.3)', width:'40px' }}>#</th>
            {cols.map(c => <th key={c} className="p-3 text-left font-semibold whitespace-nowrap" style={{ borderBottom:'1px solid rgba(255,255,255,0.08)', color:'rgba(255,255,255,0.8)' }}>{c}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} style={{ borderBottom:'1px solid rgba(255,255,255,0.03)' }} onMouseEnter={e=>(e.currentTarget.style.background='rgba(124,58,237,0.05)')} onMouseLeave={e=>(e.currentTarget.style.background='transparent')}>
              <td className="p-3 font-mono" style={{ color:'rgba(255,255,255,0.2)' }}>{i+1}</td>
              {cols.map(c => <td key={c} className="p-3 whitespace-nowrap">{row[c]===''||row[c]==null?<span style={{ color:'#f87171', fontStyle:'italic' }}>null</span>:String(row[c])}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function buildUnifiedSQL(tableName: string, transformIds: string[], detectedIssues: Record<string, boolean>): string {
  const activeSteps: string[] = []
  const seen = new Set<string>()
  transformIds.forEach(id => {
    const cleanKey = TRANSFORM_TO_CLEAN[id]
    if (cleanKey && !seen.has(cleanKey)) { seen.add(cleanKey); activeSteps.push(cleanKey) }
  })
  const issueMap: Record<string, string> = {
    duplicates: 'remove_duplicates', empty_rows: 'remove_empty_rows',
    missing: 'fill_missing', outliers: 'handle_outliers',
    messy_text: 'standardize_text', fix_dates: 'fix_dates',
  }
  Object.entries(detectedIssues).forEach(([issue, detected]) => {
    if (detected) { const key = issueMap[issue]; if (key && !seen.has(key)) { seen.add(key); activeSteps.push(key) } }
  })
  if (activeSteps.length === 0) return `-- No transformations selected\nSELECT * FROM ${tableName};`
  const stepSQL: Record<string, (prev: string) => string> = {
    remove_duplicates: (prev) => `  -- Step: Remove duplicate rows\n  SELECT DISTINCT *\n  FROM ${prev}`,
    remove_empty_rows: (prev) => `  -- Step: Remove rows where all columns are NULL\n  SELECT *\n  FROM ${prev}\n  WHERE NOT (/* col1 IS NULL AND col2 IS NULL AND ... */)`,
    standardize_text:  (prev) => `  -- Step: Standardize text columns (lowercase + trim)\n  SELECT\n    * EXCEPT (text_col),\n    LOWER(TRIM(text_col)) AS text_col  -- repeat per text column\n  FROM ${prev}`,
    handle_outliers:   (prev) => `  -- Step: Cap outliers using Modified Z-Score method\n  SELECT\n    * EXCEPT (numeric_col),\n    CASE\n      WHEN numeric_col > (median + 3.5 * mad / 0.6745) THEN (median + 3.5 * mad / 0.6745)\n      WHEN numeric_col < (median - 3.5 * mad / 0.6745) THEN (median - 3.5 * mad / 0.6745)\n      ELSE numeric_col\n    END AS numeric_col  -- repeat per numeric column\n  FROM ${prev}`,
    fill_missing:      (prev) => `  -- Step: Fill missing values (median for numeric, mode for text)\n  SELECT\n    * EXCEPT (numeric_col, text_col),\n    COALESCE(numeric_col, PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY numeric_col) OVER()) AS numeric_col,\n    COALESCE(text_col, MODE() WITHIN GROUP (ORDER BY text_col) OVER()) AS text_col\n  FROM ${prev}`,
    fix_dates:         (prev) => `  -- Step: Standardize date columns to ISO format\n  SELECT\n    * EXCEPT (date_col),\n    TRY_CAST(date_col AS DATE) AS date_col  -- repeat per date column\n  FROM ${prev}`,
  }
  const stepLabels: Record<string, string> = {
    remove_duplicates: 'Deduplicated', remove_empty_rows: 'NoEmptyRows',
    standardize_text: 'TextStandardized', handle_outliers: 'OutliersCapped',
    fill_missing: 'MissingFilled', fix_dates: 'DateFixed',
  }
  let sql = `-- ══════════════════════════════════════════════════════════\n-- Unified Data Cleaning Pipeline\n-- Source table: ${tableName}\n-- Generated by DataPipe\n-- Compatible with: PostgreSQL · BigQuery · Snowflake · DuckDB\n-- ══════════════════════════════════════════════════════════\n\nWITH\nSource AS (\n  SELECT * FROM ${tableName}\n)`
  let lastStep = 'Source'
  activeSteps.forEach((step) => {
    const stepName = stepLabels[step] || step
    const body = stepSQL[step]?.(lastStep) || `  SELECT * FROM ${lastStep}`
    sql += `,\n\n${stepName} AS (\n${body}\n)`
    lastStep = stepName
  })
  sql += `\n\n-- ── Final Result ────────────────────────────────────────\nSELECT * FROM ${lastStep};`
  return sql
}

const SEV_COLOR: Record<string, { bg: string; text: string; border: string; dot: string; label: string }> = {
  GREEN:  { bg: 'rgba(34,197,94,0.08)',  text: '#4ade80', border: 'rgba(34,197,94,0.25)',  dot: '#22c55e', label: 'All Clear'        },
  YELLOW: { bg: 'rgba(234,179,8,0.08)',  text: '#fbbf24', border: 'rgba(234,179,8,0.25)',  dot: '#eab308', label: 'Needs Review'     },
  RED:    { bg: 'rgba(239,68,68,0.08)',  text: '#f87171', border: 'rgba(239,68,68,0.25)',  dot: '#ef4444', label: 'Action Required'  },
}

const KIND_LABEL: Record<string, { icon: string; plain: string }> = {
  added:       { icon: '➕', plain: 'New column appeared in your data that wasn\'t there before' },
  removed:     { icon: '➖', plain: 'A column that was previously there is now missing' },
  type_change: { icon: '🔄', plain: 'A column\'s data type changed (e.g. text → number)' },
}

const SCHEDULE_PRESETS = [
  { label: 'Live (5s)',  value: 'live',   cron: '',              ms: 5_000   },
  { label: '5 minutes',  value: '5m',     cron: '*/5 * * * *',  ms: 300_000 },
  { label: '15 minutes', value: '15m',    cron: '*/15 * * * *', ms: 900_000 },
  { label: '30 minutes', value: '30m',    cron: '*/30 * * * *', ms: 1_800_000 },
  { label: '1 hour',     value: '1h',     cron: '0 * * * *',    ms: 3_600_000 },
  { label: '6 hours',    value: '6h',     cron: '0 */6 * * *',  ms: 21_600_000 },
  { label: '12 hours',   value: '12h',    cron: '0 */12 * * *', ms: 43_200_000 },
  { label: 'Daily',      value: '24h',    cron: '0 0 * * *',    ms: 86_400_000 },
  { label: 'Weekly',     value: '1w',     cron: '0 0 * * 0',    ms: 604_800_000 },
  { label: 'Custom…',    value: 'custom', cron: '',              ms: 0 },
]

const TIMEZONES = [
  'UTC',
  'Africa/Abidjan','Africa/Cairo','Africa/Johannesburg','Africa/Lagos','Africa/Nairobi',
  'America/Anchorage','America/Argentina/Buenos_Aires','America/Bogota','America/Chicago',
  'America/Denver','America/Los_Angeles','America/Mexico_City','America/New_York',
  'America/Phoenix','America/Sao_Paulo','America/Toronto','America/Vancouver',
  'Asia/Baghdad','Asia/Bangkok','Asia/Colombo','Asia/Dhaka','Asia/Dubai',
  'Asia/Hong_Kong','Asia/Jakarta','Asia/Karachi','Asia/Kolkata','Asia/Kuala_Lumpur',
  'Asia/Manila','Asia/Seoul','Asia/Shanghai','Asia/Singapore','Asia/Taipei',
  'Asia/Tehran','Asia/Tokyo','Asia/Yekaterinburg',
  'Atlantic/Reykjavik',
  'Australia/Adelaide','Australia/Brisbane','Australia/Melbourne','Australia/Perth','Australia/Sydney',
  'Europe/Amsterdam','Europe/Athens','Europe/Berlin','Europe/Brussels','Europe/Budapest',
  'Europe/Copenhagen','Europe/Dublin','Europe/Helsinki','Europe/Istanbul','Europe/Kiev',
  'Europe/Lisbon','Europe/London','Europe/Madrid','Europe/Moscow','Europe/Oslo',
  'Europe/Paris','Europe/Prague','Europe/Rome','Europe/Stockholm','Europe/Vienna','Europe/Warsaw','Europe/Zurich',
  'Indian/Maldives','Indian/Mauritius',
  'Pacific/Auckland','Pacific/Fiji','Pacific/Guam','Pacific/Honolulu','Pacific/Tahiti',
]

type PipelineLogEntry = {
  id: string
  pipelineId: string
  timestamp: string
  status: 'success' | 'running' | 'failed' | 'queued'
  duration: number
  rowsIn: number
  rowsOut: number
  steps: { name: string; status: 'success'|'failed'|'skipped'; duration: number; message?: string }[]
  triggeredBy: 'manual' | 'schedule' | 'api'
  schedule?: string
}

type SavedPipeline = {
  id: string
  name: string
  sourceId: string
  destinationId: string
  sourceName: string
  destinationName: string
  destinationMode: 'replace' | 'append'
  transformations: any[]
  createdAt: string
  schedule: string | null
  scheduleLabel: string | null
  scheduleMs: number | null
  scheduleTimezone: string | null
  scheduleStartDate: string | null
  lastRun: string | null
  runCount: number
  logs: PipelineLogEntry[]
  isActive: boolean
}

type ClientPreview = {
  columns: string[]
  rows: any[]
  totalRows: number
  rowsRemoved: number
  sourceRows: number
}

function parseCSV(text: string): { headers: string[]; rows: Record<string, string>[] } {
  const lines = text.split('\n').filter(l => l.trim() !== '')
  if (lines.length === 0) return { headers: [], rows: [] }
  const parseRow = (line: string): string[] => {
    const result: string[] = []
    let cur = ''
    let inQuote = false
    for (let i = 0; i < line.length; i++) {
      const ch = line[i]
      if (ch === '"') { inQuote = !inQuote }
      else if (ch === ',' && !inQuote) { result.push(cur.trim()); cur = '' }
      else { cur += ch }
    }
    result.push(cur.trim())
    return result
  }
  const headers = parseRow(lines[0]).map(h => h.replace(/^"|"$/g, ''))
  const rows = lines.slice(1).map(line => {
    const vals = parseRow(line)
    const row: Record<string, string> = {}
    headers.forEach((h, i) => { row[h] = (vals[i] ?? '').replace(/^"|"$/g, '') })
    return row
  })
  return { headers, rows }
}

function applyTransforms(
  headers: string[],
  rows: Record<string, string>[],
  transformIds: string[]
): Record<string, string>[] {
  let data = [...rows]
  for (const id of transformIds) {
    switch (id) {
      case 'remove_duplicates': {
        const seen = new Set<string>()
        data = data.filter(row => {
          const key = JSON.stringify(row)
          if (seen.has(key)) return false
          seen.add(key); return true
        })
        break
      }
      case 'remove_empty_rows': {
        data = data.filter(row => headers.some(h => row[h] !== '' && row[h] != null))
        break
      }
      case 'standardize_text': {
        data = data.map(row => {
          const newRow = { ...row }
          headers.forEach(h => {
            const val = newRow[h] ?? ''
            if (val !== '' && isNaN(Number(val))) newRow[h] = val.toLowerCase().trim()
          })
          return newRow
        })
        break
      }
      case 'fill_missing': {
        headers.forEach(h => {
          const nonEmpty = data.map(r => r[h]).filter(v => v !== '' && v != null)
          if (nonEmpty.length === 0) return
          const allNumeric = nonEmpty.every(v => !isNaN(Number(v)))
          if (allNumeric) {
            const sorted = [...nonEmpty].map(Number).sort((a, b) => a - b)
            const mid = Math.floor(sorted.length / 2)
            const median = sorted.length % 2 === 0 ? String((sorted[mid - 1] + sorted[mid]) / 2) : String(sorted[mid])
            data = data.map(row => ({ ...row, [h]: row[h] === '' || row[h] == null ? median : row[h] }))
          } else {
            const freq: Record<string, number> = {}
            nonEmpty.forEach(v => { freq[v] = (freq[v] ?? 0) + 1 })
            const mode = Object.entries(freq).sort((a, b) => b[1] - a[1])[0]?.[0] ?? ''
            data = data.map(row => ({ ...row, [h]: row[h] === '' || row[h] == null ? mode : row[h] }))
          }
        })
        break
      }
      case 'cap_outliers': {
        headers.forEach(h => {
          const vals = data.map(r => r[h]).filter(v => v !== '' && !isNaN(Number(v))).map(Number)
          if (vals.length < 5) return
          const sorted = [...vals].sort((a, b) => a - b)
          const mid = Math.floor(sorted.length / 2)
          const median = sorted.length % 2 === 0 ? (sorted[mid - 1] + sorted[mid]) / 2 : sorted[mid]
          const deviations = vals.map(v => Math.abs(v - median))
          const madSorted = [...deviations].sort((a, b) => a - b)
          const mad = madSorted.length % 2 === 0 ? (madSorted[mid - 1] + madSorted[mid]) / 2 : madSorted[mid]
          if (mad === 0) return
          const threshold = 3.5 * mad / 0.6745
          const lo = median - threshold
          const hi = median + threshold
          data = data.map(row => {
            const v = row[h]
            if (v === '' || isNaN(Number(v))) return row
            const n = Number(v)
            if (n < lo) return { ...row, [h]: String(Math.round(lo * 100) / 100) }
            if (n > hi) return { ...row, [h]: String(Math.round(hi * 100) / 100) }
            return row
          })
        })
        break
      }
      case 'fix_dates': {
        headers.forEach(h => {
          const sample = data.slice(0, 20).map(r => r[h]).filter(Boolean)
          const looksLikeDate = sample.some(v => { const d = new Date(v); return !isNaN(d.getTime()) && /[\/\-]|\w+ \d{4}/.test(v) })
          if (!looksLikeDate) return
          data = data.map(row => {
            const v = row[h]
            if (!v) return row
            const d = new Date(v)
            if (isNaN(d.getTime())) return row
            return { ...row, [h]: d.toISOString().split('T')[0] }
          })
        })
        break
      }
      case 'ai_clean': {
        // AI clean: apply all standard transforms
        const seen2 = new Set<string>()
        data = data.filter(row => {
          const key = JSON.stringify(row)
          if (seen2.has(key)) return false
          seen2.add(key); return true
        })
        data = data.filter(row => headers.some(h => row[h] !== '' && row[h] != null))
        data = data.map(row => {
          const newRow = { ...row }
          headers.forEach(h => {
            const val = newRow[h] ?? ''
            if (val !== '' && isNaN(Number(val))) newRow[h] = val.toLowerCase().trim()
          })
          return newRow
        })
        headers.forEach(h => {
          const nonEmpty = data.map(r => r[h]).filter(v => v !== '' && v != null)
          if (nonEmpty.length === 0) return
          const allNumeric = nonEmpty.every(v => !isNaN(Number(v)))
          if (allNumeric) {
            const sorted = [...nonEmpty].map(Number).sort((a, b) => a - b)
            const mid = Math.floor(sorted.length / 2)
            const median = sorted.length % 2 === 0 ? String((sorted[mid - 1] + sorted[mid]) / 2) : String(sorted[mid])
            data = data.map(row => ({ ...row, [h]: row[h] === '' || row[h] == null ? median : row[h] }))
          } else {
            const freq: Record<string, number> = {}
            nonEmpty.forEach(v => { freq[v] = (freq[v] ?? 0) + 1 })
            const mode = Object.entries(freq).sort((a, b) => b[1] - a[1])[0]?.[0] ?? ''
            data = data.map(row => ({ ...row, [h]: row[h] === '' || row[h] == null ? mode : row[h] }))
          }
        })
        break
      }
      default: {
        // ai_custom_* IDs — these are natural-language rules
        // We apply best-effort heuristics client-side for preview
        // The real application happens server-side via the ai_clean flag
        if (id.startsWith('ai_custom_')) {
          // For preview, apply dedup + fill as a safe approximation
          const seen3 = new Set<string>()
          data = data.filter(row => {
            const key = JSON.stringify(row)
            if (seen3.has(key)) return false
            seen3.add(key); return true
          })
        }
        break
      }
      }
  }
  return data
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { bg: string; text: string; dot: string }> = {
    success: { bg: 'rgba(34,197,94,0.12)',  text: '#4ade80', dot: '#22c55e' },
    running: { bg: 'rgba(59,130,246,0.12)', text: '#60a5fa', dot: '#3b82f6' },
    failed:  { bg: 'rgba(239,68,68,0.12)',  text: '#f87171', dot: '#ef4444' },
    queued:  { bg: 'rgba(234,179,8,0.12)',  text: '#fbbf24', dot: '#eab308' },
  }
  const s = map[status] || map.queued
  return (
    <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-bold"
      style={{ background: s.bg, color: s.text }}>
      <span className={`w-1.5 h-1.5 rounded-full ${status === 'running' ? 'animate-pulse' : ''}`} style={{ background: s.dot }} />
      {status.charAt(0).toUpperCase() + status.slice(1)}
    </span>
  )
}

function fmtDur(ms: number) {
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms/1000).toFixed(1)}s`
  return `${Math.floor(ms/60000)}m ${Math.floor((ms%60000)/1000)}s`
}

function fmtRelative(ts: string) {
  const diff = Date.now() - new Date(ts).getTime()
  if (diff < 60000) return 'just now'
  if (diff < 3600000) return `${Math.floor(diff/60000)}m ago`
  if (diff < 86400000) return `${Math.floor(diff/3600000)}h ago`
  return `${Math.floor(diff/86400000)}d ago`
}

// ── Destination Combobox ──────────────────────────────────────────────────────
function DestinationSelect({
  value,
  onChange,
  onClear,
  sources,
  savedPipelines,
}: {
  value: string
  onChange: (v: string) => void
  onClear: () => void
  sources: { name: string }[]
  savedPipelines: SavedPipeline[]
}) {
  const [open, setOpen]   = useState(false)
  const [query, setQuery] = useState(value)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => { setQuery(value) }, [value])

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const allSuggestions = [
    ...new Set([
      ...savedPipelines.map(p => p.destinationId),
      ...sources.map(s => s.name),
    ])
  ].filter(Boolean)

  // Show ALL suggestions when query is empty, filter otherwise
  const filtered = query === ''
    ? allSuggestions
    : allSuggestions.filter(s => s.toLowerCase().includes(query.toLowerCase()))

  const commit = (v: string) => {
    setQuery(v)
    onChange(v)
    setOpen(false)
  }

  const handleClear = () => {
    setQuery('')
    onChange('')
    onClear()
    setOpen(true) // open immediately so user sees all options
  }

  return (
    <div ref={ref} style={{ position: 'relative' }}>
      <div style={{ position: 'relative' }}>
        <input
          type="text"
          value={query}
          placeholder="e.g. customers_cleaned.csv"
          onChange={e => { setQuery(e.target.value); onChange(e.target.value); setOpen(true) }}
          onFocus={() => setOpen(true)}
          className="w-full text-sm text-white rounded-xl px-4 py-3 outline-none"
          style={{ background: 'rgba(52,211,153,0.08)', border: '1px solid rgba(52,211,153,0.3)', paddingRight: '64px' }}
        />
        {/* Clear button — wipes text and opens dropdown with all options */}
        {query && (
          <button
            type="button"
            onClick={handleClear}
            style={{
              position: 'absolute', right: '36px', top: '50%', transform: 'translateY(-50%)',
              color: 'rgba(255,255,255,0.4)', background: 'none', border: 'none',
              cursor: 'pointer', padding: '4px', lineHeight: 1,
            }}
            title="Clear and show all options"
          >
            <svg width="12" height="12" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
              <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        )}
        <button
          type="button"
          onClick={() => setOpen(v => !v)}
          style={{ position: 'absolute', right: '10px', top: '50%', transform: 'translateY(-50%)', color: 'rgba(255,255,255,0.4)', background: 'none', border: 'none', cursor: 'pointer', padding: '4px' }}
        >
          <svg width="12" height="12" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"
            style={{ transform: open ? 'rotate(180deg)' : 'rotate(0deg)', transition: 'transform .15s' }}>
            <polyline points="6 9 12 15 18 9"/>
          </svg>
        </button>
      </div>

      {open && (
        <div style={{
          position: 'absolute', top: 'calc(100% + 4px)', left: 0, right: 0, zIndex: 1000,
          background: '#0d0a1a', border: '1px solid rgba(52,211,153,0.3)',
          borderRadius: '12px', overflow: 'hidden',
          boxShadow: '0 8px 32px rgba(0,0,0,0.6)',
          maxHeight: '240px', overflowY: 'auto',
        }}>
          {/* "Use as typed" option when query doesn't match any suggestion */}
          {query && !allSuggestions.includes(query) && (
            <button
              type="button"
              onClick={() => commit(query)}
              className="w-full text-left px-4 py-2.5 text-sm"
              style={{ color: '#34d399', background: 'transparent', border: 'none', cursor: 'pointer', borderBottom: '1px solid rgba(255,255,255,0.06)', display: 'block' }}
              onMouseEnter={e => (e.currentTarget.style.background = 'rgba(52,211,153,0.08)')}
              onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
            >
              ＋ Use "{query}"
            </button>
          )}
          {filtered.length === 0 && !query && (
            <div style={{ padding: '12px 16px', fontSize: '12px', color: 'rgba(255,255,255,0.3)' }}>
              No previous outputs yet. Type a name above.
            </div>
          )}
          {filtered.map(s => (
            <button
              key={s}
              type="button"
              onClick={() => commit(s)}
              className="w-full text-left px-4 py-2.5 text-sm"
              style={{ color: 'rgba(255,255,255,0.8)', background: s === value ? 'rgba(52,211,153,0.1)' : 'transparent', border: 'none', cursor: 'pointer', display: 'block', fontFamily: 'monospace', fontSize: '12px' }}
              onMouseEnter={e => (e.currentTarget.style.background = 'rgba(52,211,153,0.08)')}
              onMouseLeave={e => (e.currentTarget.style.background = s === value ? 'rgba(52,211,153,0.1)' : 'transparent')}
            >
              {s}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

// ── Mini Calendar ─────────────────────────────────────────────────────────────
function MiniCalendar({ value, onChange }: { value: string; onChange: (d: string) => void }) {
  const today = new Date(); today.setHours(0,0,0,0)
  const initDate = value ? new Date(value + 'T00:00:00') : today
  const [viewYear,  setViewYear]  = useState(initDate.getFullYear())
  const [viewMonth, setViewMonth] = useState(initDate.getMonth())
  const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
  const DAYS   = ['Su','Mo','Tu','We','Th','Fr','Sa']
  const firstDay    = new Date(viewYear, viewMonth, 1).getDay()
  const daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate()
  const cells: (number|null)[] = [...Array(firstDay).fill(null), ...Array.from({ length: daysInMonth }, (_, i) => i + 1)]
  while (cells.length % 7 !== 0) cells.push(null)
  const prevMonth = () => { if (viewMonth === 0) { setViewMonth(11); setViewYear(y => y-1) } else setViewMonth(m => m-1) }
  const nextMonth = () => { if (viewMonth === 11) { setViewMonth(0); setViewYear(y => y+1) } else setViewMonth(m => m+1) }
  const selectDay = (day: number) => {
    const d = new Date(viewYear, viewMonth, day)
    if (d < today) return
    const iso = `${viewYear}-${String(viewMonth+1).padStart(2,'0')}-${String(day).padStart(2,'0')}`
    onChange(iso === value ? '' : iso)
  }
  const sp = value ? value.split('-').map(Number) : []
  const isSelected = (d: number) => sp.length === 3 && sp[0]===viewYear && sp[1]===viewMonth+1 && sp[2]===d
  const isPast     = (d: number) => new Date(viewYear, viewMonth, d) < today
  const isToday2   = (d: number) => new Date(viewYear, viewMonth, d).getTime() === today.getTime()
  return (
    <div className="rounded-xl overflow-hidden" style={{ background: 'rgba(0,0,0,0.35)', border: '1px solid rgba(124,58,237,0.25)' }}>
      <div className="flex items-center justify-between px-3 py-2" style={{ borderBottom: '1px solid rgba(255,255,255,0.07)' }}>
        <button onClick={prevMonth} className="w-6 h-6 rounded-md flex items-center justify-center text-xs" style={{ color:'rgba(255,255,255,0.5)', background:'rgba(255,255,255,0.05)' }}>‹</button>
        <span className="text-xs font-bold text-white">{MONTHS[viewMonth]} {viewYear}</span>
        <button onClick={nextMonth} className="w-6 h-6 rounded-md flex items-center justify-center text-xs" style={{ color:'rgba(255,255,255,0.5)', background:'rgba(255,255,255,0.05)' }}>›</button>
      </div>
      <div className="grid grid-cols-7 px-2 pt-2 pb-1">
        {DAYS.map(d => <div key={d} className="text-center" style={{ fontSize:'9px', color:'rgba(255,255,255,0.25)', fontWeight:600 }}>{d}</div>)}
      </div>
      <div className="grid grid-cols-7 gap-0 px-2 pb-2">
        {cells.map((day, i) => {
          if (!day) return <div key={i} />
          const past=isPast(day); const sel=isSelected(day); const tod=isToday2(day)
          return (
            <button key={i} onClick={() => selectDay(day)} disabled={past}
              className="w-full aspect-square rounded-lg flex items-center justify-center transition-all"
              style={{ fontSize:'11px', fontWeight:sel?700:400, color:past?'rgba(255,255,255,0.15)':sel?'white':tod?'#a78bfa':'rgba(255,255,255,0.65)', background:sel?'#7c3aed':tod?'rgba(167,139,250,0.12)':'transparent', cursor:past?'not-allowed':'pointer' }}>
              {day}
            </button>
          )
        })}
      </div>
    </div>
  )
}

// ── Daily At Time Picker ──────────────────────────────────────────────────────
function DailyAtTimePicker({ value, onChange }: { value: string; onChange: (t: string) => void }) {
  const QUICK_TIMES = [
    { label:'6 AM', val:'06:00' }, { label:'7 AM', val:'07:00' }, { label:'8 AM', val:'08:00' },
    { label:'9 AM', val:'09:00' }, { label:'10 AM', val:'10:00' }, { label:'12 PM', val:'12:00' },
    { label:'2 PM', val:'14:00' }, { label:'5 PM', val:'17:00' }, { label:'8 PM', val:'20:00' },
    { label:'11 PM', val:'23:00' }, { label:'12 AM', val:'00:00' }, { label:'3 AM', val:'03:00' },
  ]
  const fmt12 = (v: string) => {
    if (!v) return ''
    const [h, m] = v.split(':').map(Number)
    return `${h%12||12}:${String(m).padStart(2,'0')} ${h>=12?'PM':'AM'}`
  }
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-4 gap-1.5">
        {QUICK_TIMES.map(t => (
          <button key={t.val} onClick={() => onChange(t.val)}
            className="py-1.5 rounded-lg text-xs font-semibold transition-all"
            style={{ background:value===t.val?'rgba(124,58,237,0.35)':'rgba(255,255,255,0.04)', border:`1px solid ${value===t.val?'rgba(124,58,237,0.6)':'rgba(255,255,255,0.08)'}`, color:value===t.val?'#c4b5fd':'rgba(255,255,255,0.55)' }}>
            {t.label}
          </button>
        ))}
      </div>
      <div className="flex items-center gap-3 px-3 py-2.5 rounded-xl" style={{ background:'rgba(124,58,237,0.06)', border:'1px solid rgba(124,58,237,0.2)' }}>
        <svg width="13" height="13" fill="none" stroke="#a78bfa" strokeWidth="2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
        <span className="text-xs" style={{ color:'rgba(255,255,255,0.4)' }}>Or enter exact time:</span>
        <input type="time" value={value} onChange={e => onChange(e.target.value)}
          className="flex-1 text-sm text-white rounded-lg px-3 py-1.5 outline-none"
          style={{ background:'rgba(255,255,255,0.06)', border:'1px solid rgba(124,58,237,0.3)', colorScheme:'dark' }} />
        {value && <span className="text-xs font-bold" style={{ color:'#a78bfa', minWidth:'60px' }}>{fmt12(value)}</span>}
      </div>
    </div>
  )
}

// ── Schedule Modal ────────────────────────────────────────────────────────────
function ScheduleModal({ pipeline, onClose, onSave }: {
  pipeline: SavedPipeline
  onClose: () => void
  onSave: (schedMs: number|null, schedLabel: string|null, schedCron: string|null, timezone: string, startDate: string|null) => void
}) {
  const [scheduleValue, setScheduleValue] = useState<string>(() => {
    if (!pipeline.scheduleMs) return 'none'
    const preset = SCHEDULE_PRESETS.find(p => p.ms === pipeline.scheduleMs)
    return preset?.value || 'custom'
  })
  const [customCron,   setCustomCron]   = useState('')
  const [customMs,     setCustomMs]     = useState(60000)
  const [timezone,     setTimezone]     = useState(pipeline.scheduleTimezone || 'UTC')
  const [scheduleTime, setScheduleTime] = useState('00:00')
  const [dailyAtTime,  setDailyAtTime]  = useState(pipeline.schedule ? (() => {
    const m = pipeline.schedule.match(/^(\d+) (\d+) \* \* \*$/)
    if (m) return `${String(Number(m[2])).padStart(2,'0')}:${String(Number(m[1])).padStart(2,'0')}`
    return '09:00'
  })() : '09:00')
  const [startDate,    setStartDate]    = useState<string>(pipeline.scheduleStartDate || '')
  const [showCalendar, setShowCalendar] = useState(false)
  const [scheduleTab,  setScheduleTab]  = useState<'frequency'|'daily-at'>('frequency')

  const isAbsolute   = ['24h','1w'].includes(scheduleValue)
  const isCustom     = scheduleValue === 'custom'
  const showTimezone = isAbsolute || isCustom || scheduleTab === 'daily-at'

  const fmt12 = (v: string) => { if (!v) return ''; const [h,m]=v.split(':').map(Number); return `${h%12||12}:${String(m).padStart(2,'0')} ${h>=12?'PM':'AM'}` }
  const fmtDate = (iso: string) => { if (!iso) return 'No date set'; const [y,mo,d]=iso.split('-'); const months=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']; return `${months[Number(mo)-1]} ${Number(d)}, ${y}` }

  const handleSave = () => {
    const finalStartDate = startDate || null
    if (scheduleTab === 'daily-at') {
      if (!dailyAtTime) { onSave(null, null, null, timezone, finalStartDate); return }
      const [h, m] = dailyAtTime.split(':').map(Number)
      onSave(86_400_000, `Daily at ${fmt12(dailyAtTime)}`, `${m} ${h} * * *`, timezone, finalStartDate)
      return
    }
    if (scheduleValue === 'none') { onSave(null, null, null, timezone, finalStartDate); return }
    const preset = SCHEDULE_PRESETS.find(p => p.value === scheduleValue)
    let finalCron = preset?.cron || null
    let finalLabel = preset?.label || null
    if (isAbsolute) {
      const [h, m] = scheduleTime.split(':')
      const hour = parseInt(h||'0'); const min = parseInt(m||'0')
      if (scheduleValue === '24h') { finalCron = `${min} ${hour} * * *`; finalLabel = `Daily at ${scheduleTime}` }
      else { finalCron = `${min} ${hour} * * 0`; finalLabel = `Weekly at ${scheduleTime}` }
    } else if (isCustom) {
      finalCron  = customCron || '* * * * *'
      finalLabel = `Custom (${fmtDur(customMs)})`
    }
    onSave(preset?.ms || customMs, finalLabel, finalCron, showTimezone ? timezone : 'UTC', finalStartDate)
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background:'rgba(0,0,0,0.85)' }} onClick={onClose}>
      <div className="rounded-2xl w-full max-w-lg max-h-[92vh] overflow-auto" style={{ background:'#0d0a1a', border:'1px solid rgba(255,255,255,0.12)' }} onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between p-5" style={{ borderBottom:'1px solid rgba(255,255,255,0.08)' }}>
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg flex items-center justify-center" style={{ background:'rgba(124,58,237,0.2)', border:'1px solid rgba(124,58,237,0.3)' }}>
              <svg width="14" height="14" fill="none" stroke="#a78bfa" strokeWidth="2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
            </div>
            <div><h3 className="font-bold text-white text-sm">Schedule Pipeline</h3><p className="text-xs" style={{ color:'rgba(255,255,255,0.4)' }}>{pipeline.name}</p></div>
          </div>
          <button onClick={onClose} style={{ color:'rgba(255,255,255,0.35)', fontSize:'18px' }}>✕</button>
        </div>
        <div className="flex gap-1 p-4 pb-0">
          {([{id:'frequency',label:'⏱ Frequency'},{id:'daily-at',label:'🕘 Run at time'}] as const).map(tab => (
            <button key={tab.id} onClick={() => setScheduleTab(tab.id)} className="flex-1 py-2 rounded-xl text-xs font-bold transition-all"
              style={{ background:scheduleTab===tab.id?'rgba(124,58,237,0.25)':'rgba(255,255,255,0.04)', border:`1px solid ${scheduleTab===tab.id?'rgba(124,58,237,0.5)':'rgba(255,255,255,0.08)'}`, color:scheduleTab===tab.id?'#c4b5fd':'rgba(255,255,255,0.45)' }}>
              {tab.label}
            </button>
          ))}
        </div>
        <div className="p-5 space-y-5">
          {scheduleTab === 'daily-at' && (
            <div className="space-y-4">
              <div className="px-4 py-3 rounded-xl" style={{ background:'rgba(124,58,237,0.08)', border:'1px solid rgba(124,58,237,0.2)' }}>
                <p className="text-xs font-bold text-white mb-0.5">Run every day at a fixed time</p>
                <p className="text-xs" style={{ color:'rgba(255,255,255,0.4)' }}>Pick a time — the pipeline runs automatically at that hour every day.</p>
              </div>
              <div><label className="text-xs font-bold uppercase tracking-widest block mb-3" style={{ color:'rgba(255,255,255,0.4)' }}>Pick a time</label><DailyAtTimePicker value={dailyAtTime} onChange={setDailyAtTime} /></div>
              {dailyAtTime && (
                <div className="rounded-xl px-4 py-3" style={{ background:'rgba(34,197,94,0.08)', border:'1px solid rgba(34,197,94,0.2)' }}>
                  <p className="text-xs font-bold" style={{ color:'#4ade80' }}>✓ Will run every day at {fmt12(dailyAtTime)}{timezone !== 'UTC' && ` (${timezone})`}{startDate && ` · starting ${fmtDate(startDate)}`}</p>
                </div>
              )}
            </div>
          )}
          {scheduleTab === 'frequency' && (
            <div>
              <label className="text-xs font-bold uppercase tracking-widest block mb-3" style={{ color:'rgba(255,255,255,0.4)' }}>Run Frequency</label>
              <div onClick={() => setScheduleValue('none')} className="flex items-center gap-3 p-3 rounded-xl cursor-pointer mb-2 transition-all"
                style={{ background:scheduleValue==='none'?'rgba(255,255,255,0.08)':'rgba(255,255,255,0.03)', border:`1px solid ${scheduleValue==='none'?'rgba(255,255,255,0.2)':'rgba(255,255,255,0.07)'}` }}>
                <div className="w-4 h-4 rounded-full flex items-center justify-center flex-shrink-0" style={{ border:`2px solid ${scheduleValue==='none'?'#a78bfa':'rgba(255,255,255,0.3)'}` }}>
                  {scheduleValue==='none' && <div className="w-2 h-2 rounded-full" style={{ background:'#a78bfa' }} />}
                </div>
                <div><p className="text-sm font-semibold text-white">Manual only</p><p className="text-xs" style={{ color:'rgba(255,255,255,0.35)' }}>Trigger runs on demand via "Run Now"</p></div>
              </div>
              <div className="grid grid-cols-4 gap-2">
                {SCHEDULE_PRESETS.map(preset => (
                  <div key={preset.value} onClick={() => setScheduleValue(preset.value)} className="p-2.5 rounded-xl cursor-pointer transition-all text-center"
                    style={{ background:scheduleValue===preset.value?'rgba(124,58,237,0.2)':'rgba(255,255,255,0.03)', border:`1px solid ${scheduleValue===preset.value?'rgba(124,58,237,0.5)':'rgba(255,255,255,0.07)'}` }}>
                    <p className="text-xs font-bold" style={{ color:scheduleValue===preset.value?'#a78bfa':'rgba(255,255,255,0.7)' }}>{preset.label}</p>
                    {preset.cron && !['24h','1w'].includes(preset.value) && <p className="text-xs mt-0.5 font-mono" style={{ color:'rgba(255,255,255,0.25)', fontSize:'9px' }}>{preset.cron}</p>}
                  </div>
                ))}
              </div>
              {isCustom && (
                <div className="mt-3 grid grid-cols-2 gap-3 p-3 rounded-xl" style={{ background:'rgba(124,58,237,0.06)', border:'1px solid rgba(124,58,237,0.2)' }}>
                  <div>
                    <label className="text-xs block mb-1" style={{ color:'rgba(255,255,255,0.4)' }}>Interval (ms)</label>
                    <input type="number" min={60000} value={customMs} onChange={e => setCustomMs(Number(e.target.value))} className="w-full text-sm text-white rounded-lg px-3 py-2 outline-none" style={{ background:'rgba(255,255,255,0.05)', border:'1px solid rgba(124,58,237,0.3)' }} />
                    <p className="text-xs mt-1" style={{ color:'rgba(255,255,255,0.3)' }}>= {fmtDur(customMs)}</p>
                  </div>
                  <div>
                    <label className="text-xs block mb-1" style={{ color:'rgba(255,255,255,0.4)' }}>Cron expression</label>
                    <input type="text" value={customCron} onChange={e => setCustomCron(e.target.value)} placeholder="*/5 * * * *" className="w-full text-sm text-white rounded-lg px-3 py-2 outline-none" style={{ background:'rgba(255,255,255,0.05)', border:'1px solid rgba(124,58,237,0.3)' }} />
                  </div>
                </div>
              )}
            </div>
          )}
          {showTimezone && (
            <div className="grid grid-cols-2 gap-4">
              {scheduleTab==='frequency' && isAbsolute && (
                <div>
                  <label className="text-xs font-bold uppercase tracking-widest block mb-2" style={{ color:'rgba(255,255,255,0.4)' }}>⏰ Time of Day</label>
                  <input type="time" value={scheduleTime} onChange={e => setScheduleTime(e.target.value)} className="w-full text-sm text-white rounded-xl px-4 py-3 outline-none" style={{ background:'rgba(255,255,255,0.05)', border:'1px solid rgba(124,58,237,0.3)', colorScheme:'dark' }} />
                </div>
              )}
              <div className={scheduleTab==='frequency' && isAbsolute ? '' : 'col-span-2'}>
                <label className="text-xs font-bold uppercase tracking-widest block mb-2" style={{ color:'rgba(255,255,255,0.4)' }}>🌍 Timezone</label>
                <select value={timezone} onChange={e => setTimezone(e.target.value)} className="w-full text-sm text-white rounded-xl px-4 py-3 outline-none" style={{ background:'rgba(255,255,255,0.05)', border:'1px solid rgba(124,58,237,0.3)' }}>
                  {TIMEZONES.map(tz => <option key={tz} value={tz} style={{ background:'#0d0a1a' }}>{tz}</option>)}
                </select>
              </div>
            </div>
          )}
          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="text-xs font-bold uppercase tracking-widest" style={{ color:'rgba(255,255,255,0.4)' }}>📅 Start Date</label>
              <div className="flex items-center gap-2">
                {startDate && <button onClick={() => setStartDate('')} className="text-xs px-2 py-0.5 rounded-md" style={{ color:'#f87171', background:'rgba(239,68,68,0.1)', border:'1px solid rgba(239,68,68,0.2)' }}>Clear</button>}
                <button onClick={() => setShowCalendar(v => !v)} className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-xl font-semibold transition-all"
                  style={{ background:showCalendar?'rgba(124,58,237,0.25)':'rgba(255,255,255,0.05)', border:`1px solid ${showCalendar?'rgba(124,58,237,0.5)':'rgba(255,255,255,0.1)'}`, color:showCalendar?'#c4b5fd':'rgba(255,255,255,0.55)' }}>
                  <svg width="11" height="11" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
                  {startDate ? fmtDate(startDate) : 'Pick start date'}
                </button>
              </div>
            </div>
            <p className="text-xs mb-3" style={{ color:'rgba(255,255,255,0.3)' }}>Optional — pipeline won't run before this date.</p>
            {showCalendar && <MiniCalendar value={startDate} onChange={d => { setStartDate(d); if (d) setShowCalendar(false) }} />}
            {startDate && !showCalendar && (
              <div className="flex items-center gap-2 px-3 py-2.5 rounded-xl" style={{ background:'rgba(124,58,237,0.08)', border:'1px solid rgba(124,58,237,0.2)' }}>
                <p className="text-xs" style={{ color:'#c4b5fd' }}>Starts on <strong>{fmtDate(startDate)}</strong></p>
              </div>
            )}
          </div>
          {scheduleTab==='frequency' && scheduleValue==='live' && (
  <div className="rounded-xl px-4 py-3" style={{ background:'rgba(239,68,68,0.08)', border:'1px solid rgba(239,68,68,0.2)' }}>
    <p className="text-xs font-bold" style={{ color:'#f87171' }}>⚡ Live mode polls every 5 seconds. Use only for actively updating sources — it makes a backend call every 5s.</p>
  </div>
)}
          {scheduleTab==='frequency' && scheduleValue!=='none' && scheduleValue!=='custom' && (
            <div className="rounded-xl px-4 py-3" style={{ background:'rgba(124,58,237,0.08)', border:'1px solid rgba(124,58,237,0.2)' }}>
              <p className="text-xs" style={{ color:'#c4b5fd' }}>⚡ Auto-runs every <strong>{SCHEDULE_PRESETS.find(p => p.value===scheduleValue)?.label}</strong>{isAbsolute && <> at <strong>{scheduleTime}</strong></>}{showTimezone && timezone!=='UTC' && <> in <strong>{timezone}</strong></>}{startDate && <> · starting <strong>{fmtDate(startDate)}</strong></>}</p>
            </div>
          )}
        </div>
        <div className="px-5 pb-5 flex gap-3">
          <button onClick={onClose} className="flex-1 py-3 rounded-xl font-semibold text-sm" style={{ background:'rgba(255,255,255,0.04)', border:'1px solid rgba(255,255,255,0.08)', color:'rgba(255,255,255,0.5)' }}>Cancel</button>
          <button onClick={handleSave} className="flex-1 py-3 rounded-xl font-bold text-white text-sm" style={{ background:'linear-gradient(135deg, #7c3aed, #2563eb)' }}>
            {scheduleValue==='none' && scheduleTab==='frequency' ? 'Remove Schedule' : 'Save Schedule'}
          </button>
        </div>
      </div>
    </div>
  )
}


// ── Google Sheets Modal ───────────────────────────────────────────────────────
function GSheetsModal({ onClose, onConnect }: {
  onClose: () => void
  onConnect: (email: string, sheetName: string, sheetId: string) => void
}) {
  const [step, setStep] = useState<'auth' | 'picker'>('auth')
  const [sheetSearch, setSheetSearch] = useState('')

  useEffect(() => {
    const init = async () => {
      try {
        // Use /check which also silently auto-refreshes expired tokens
        const status = await fetch('${import.meta.env.VITE_API_URL}/api/auth/google/check', { credentials: 'include' }).then(r => r.json())
        if (!status.authenticated) return

        setAuthEmail(status.email)

        // Show cached data instantly — no spinner
        const cachedData = await fetch('${import.meta.env.VITE_API_URL}/api/auth/google/sheets/cached', { credentials: 'include' }).then(r => r.json())
        if (cachedData.sheets?.length > 0) {
          setRealSheets(cachedData.sheets)
          setStep('picker')
        }

        // Refresh list silently in background
        fetch('${import.meta.env.VITE_API_URL}/api/auth/google/sheets', { credentials: 'include' })
          .then(r => r.json())
          .then(fresh => {
            if (fresh.sheets?.length > 0) {
              setRealSheets(fresh.sheets)
              setStep('picker')
            }
          })
          .catch(() => {})
      } catch {
        // Backend not running or no internet — fail silently
      }
    }
    init()
  }, [])

  const [loading, setLoading] = useState(false)
  const [selectedSheet, setSelectedSheet] = useState('')
  const [realSheets, setRealSheets] = useState<{ id: string; name: string }[]>([])
  const [authEmail, setAuthEmail] = useState('you@example.com')

  const MOCK_SHEETS = [
    'Sales Dashboard Q2 2026',
    'Customer CRM Master',
    'MyGov Analytics Tracker',
    'Marketing Spend Tracker',
    'HR Headcount Planning',
  ]
  const allSheets = realSheets.length > 0 ? realSheets.map(s => s.name) : MOCK_SHEETS
  const filteredSheets = sheetSearch.trim()
    ? allSheets.filter(name => name.toLowerCase().includes(sheetSearch.toLowerCase()))
    : allSheets
  // const MOCK_EMAIL = 'you@example.com'

const handleAuth = async () => {
  setLoading(true)
  try {
    const res = await fetch('${import.meta.env.VITE_API_URL}/api/auth/google/url', { credentials: 'include' })
    const urlData = await res.json()
    const url = urlData.url
    const newSessionId = urlData.session_id || ''
    if (!url) { setLoading(false); return }
    if (newSessionId) localStorage.setItem('datapro_session', newSessionId)
    const storedSession = newSessionId || localStorage.getItem('datapro_session') || ''
const popup = window.open(url, 'google-auth', 'width=500,height=600')
    let done = false

    const handleMsg = async (event: MessageEvent) => {
      if (event.data?.type !== 'GOOGLE_AUTH_SUCCESS') return
      if (done) return
      done = true
      clearInterval(poll)
      window.removeEventListener('message', handleMsg)
      const sid = event.data?.sessionId || storedSession
      if (sid) localStorage.setItem('datapro_session', sid)
      const sheetsRes = await fetch('${import.meta.env.VITE_API_URL}/api/auth/google/sheets', {
        credentials: 'include',
        headers: { 'X-Session-ID': sid }
      })
      const sheetsData = await sheetsRes.json()
      setRealSheets(sheetsData.sheets ?? [])
      setAuthEmail(sheetsData.email || '')
      setLoading(false)
      setStep('picker')
    }
    window.addEventListener('message', handleMsg)

    const poll = setInterval(async () => {
      if (done) return
      try {
        const statusRes = await fetch('${import.meta.env.VITE_API_URL}/api/auth/google/check', {
          credentials: 'include',
          headers: storedSession ? { 'X-Session-ID': storedSession } : {}
        })
        const status = await statusRes.json()
        if (status.authenticated && !done) {
          done = true
          clearInterval(poll)
          window.removeEventListener('message', handleMsg)
          try { popup?.close() } catch {}
          setAuthEmail(status.email)
          const sheetsRes = await fetch('${import.meta.env.VITE_API_URL}/api/auth/google/sheets', {
            credentials: 'include',
            headers: storedSession ? { 'X-Session-ID': storedSession } : {}
          })
          const sheetsData = await sheetsRes.json()
          setRealSheets(sheetsData.sheets ?? [])
          setLoading(false)
          setStep('picker')
        }
      } catch { }
    }, 2000)

    // Auto-cancel after 3 minutes if nothing happens
    setTimeout(() => {
      if (!done) {
        done = true
        clearInterval(poll)
        window.removeEventListener('message', handleMsg)
        setLoading(false)
      }
    }, 180000)
  } catch { setLoading(false) }
}

  const handlePick = () => {
    if (!selectedSheet) return
    const sheet = realSheets.find(s => s.name === selectedSheet)
    onConnect(authEmail, selectedSheet, sheet?.id ?? '')
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background: 'rgba(0,0,0,0.85)' }} onClick={onClose}>
      <div className="rounded-2xl w-full max-w-md" style={{ background: '#0d0a1a', border: '1px solid rgba(255,255,255,0.12)', maxHeight: '90vh', overflowY: 'auto' }} onClick={e => e.stopPropagation()}>
        {/* Header */}
        <div className="flex items-center justify-between p-5" style={{ borderBottom: '1px solid rgba(255,255,255,0.08)' }}>
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl flex items-center justify-center" style={{ background: 'rgba(52,211,153,0.12)', border: '1px solid rgba(52,211,153,0.3)' }}>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
                <rect x="4" y="2" width="16" height="20" rx="2" fill="#34A853" fillOpacity="0.2" stroke="#34A853" strokeWidth="1.5"/>
                <line x1="8" y1="9" x2="16" y2="9" stroke="#34A853" strokeWidth="1.5"/>
                <line x1="8" y1="13" x2="16" y2="13" stroke="#34A853" strokeWidth="1.5"/>
                <line x1="8" y1="17" x2="13" y2="17" stroke="#34A853" strokeWidth="1.5"/>
                <line x1="12" y1="2" x2="12" y2="9" stroke="#34A853" strokeWidth="1.5"/>
              </svg>
            </div>
            <div>
              <h3 className="font-bold text-white text-sm">Connect Google Sheets</h3>
              <p className="text-xs" style={{ color: 'rgba(255,255,255,0.4)' }}>Output cleaned data directly to a sheet</p>
            </div>
          </div>
          <button onClick={onClose} style={{ color: 'rgba(255,255,255,0.35)', fontSize: '18px' }}>✕</button>
        </div>

        <div className="p-5 space-y-4" style={{ overflowY: 'auto' }}>
          {step === 'auth' && (
            <>
              <div className="rounded-xl p-4" style={{ background: 'rgba(52,211,153,0.06)', border: '1px solid rgba(52,211,153,0.2)' }}>
                <p className="text-xs font-bold text-white mb-1.5">🔒 Secure OAuth via Google</p>
                <p className="text-xs leading-relaxed" style={{ color: 'rgba(255,255,255,0.5)' }}>
                  We use Google's standard sign-in screen. If you're already logged into Google in your browser, your accounts will be pre-populated for one-click selection — no URL copy-pasting needed.
                </p>
              </div>
              <button
                onClick={handleAuth}
                disabled={loading}
                className="w-full flex items-center justify-center gap-3 py-3 rounded-xl font-semibold text-sm transition-all"
                style={{
                  background: loading ? 'rgba(255,255,255,0.06)' : 'white',
                  color: loading ? 'rgba(255,255,255,0.4)' : '#1f1f1f',
                  border: 'none',
                  cursor: loading ? 'wait' : 'pointer',
                }}
              >
                {loading ? (
                  <>
                    <svg className="animate-spin" width="16" height="16" fill="none" stroke="#7c3aed" strokeWidth="2.5" viewBox="0 0 24 24">
                      <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/>
                    </svg>
                    <span style={{ color: 'rgba(255,255,255,0.5)' }}>Connecting to Google…</span>
                  </>
                ) : (
                  <>
                    <svg width="18" height="18" viewBox="0 0 24 24">
                      <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4"/>
                      <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/>
                      <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l3.66-2.84z" fill="#FBBC05"/>
                      <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/>
                    </svg>
                    Sign in with Google
                  </>
                )}
              </button>
              <p className="text-center text-xs" style={{ color: 'rgba(255,255,255,0.25)' }}>
                Tip: if the picker doesn't open, check that your browser isn't blocking Google's iframe (ad-blockers or privacy extensions can interfere).
              </p>
            </>
          )}

          {step === 'picker' && (
            <>
              <div className="flex items-center gap-2 px-3 py-2 rounded-xl" style={{ background: 'rgba(52,211,153,0.08)', border: '1px solid rgba(52,211,153,0.2)' }}>
                <span className="text-xs" style={{ color: '#34d399' }}>✓ Signed in as</span>
                <span className="text-xs font-bold text-white">{authEmail}</span>
              </div>

              {/* Search bar */}
              <div style={{ position: 'relative' }}>
                <svg width="13" height="13" fill="none" stroke="rgba(255,255,255,0.35)" strokeWidth="2"
                  viewBox="0 0 24 24"
                  style={{ position: 'absolute', left: '12px', top: '50%', transform: 'translateY(-50%)', pointerEvents: 'none' }}>
                  <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
                </svg>
                <input
                  type="text"
                  value={sheetSearch}
                  onChange={e => setSheetSearch(e.target.value)}
                  placeholder="Search spreadsheets by name…"
                  className="w-full text-sm text-white rounded-xl outline-none"
                  style={{
                    background: 'rgba(255,255,255,0.05)',
                    border: '1px solid rgba(52,211,153,0.25)',
                    padding: '10px 36px 10px 36px',
                  }}
                />
                {sheetSearch && (
                  <button
                    onClick={() => setSheetSearch('')}
                    style={{
                      position: 'absolute', right: '10px', top: '50%', transform: 'translateY(-50%)',
                      background: 'none', border: 'none', color: 'rgba(255,255,255,0.4)',
                      cursor: 'pointer', padding: '4px',
                    }}>
                    <svg width="11" height="11" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
                      <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
                    </svg>
                  </button>
                )}
              </div>

              <div>
                <p className="text-xs font-bold uppercase tracking-widest mb-3" style={{ color: 'rgba(255,255,255,0.4)' }}>
                  {sheetSearch
                    ? `${filteredSheets.length} result${filteredSheets.length !== 1 ? 's' : ''} for "${sheetSearch}"`
                    : 'Your Spreadsheets'}
                </p>
                <div className="space-y-2 max-h-48 overflow-auto">
                  {filteredSheets.length === 0 ? (
                    <div style={{ padding: '20px', textAlign: 'center', color: 'rgba(255,255,255,0.3)', fontSize: '13px' }}>
                      No spreadsheets match "{sheetSearch}"
                    </div>
                  ) : (
                    filteredSheets.map(sheet => {
  const isUnsupported = sheet.toLowerCase().endsWith('.xlsx') || sheet.toLowerCase().endsWith('.csv')
  return (
  <div
    key={sheet}
    onClick={() => { if (!isUnsupported) setSelectedSheet(sheet) }}
    className="flex items-center gap-3 px-4 py-3 rounded-xl transition-all"
    style={{
      background: selectedSheet === sheet ? 'rgba(52,211,153,0.1)' : 'rgba(255,255,255,0.03)',
      border: `1px solid ${selectedSheet === sheet ? 'rgba(52,211,153,0.35)' : 'rgba(255,255,255,0.07)'}`,
      cursor: isUnsupported ? 'not-allowed' : 'pointer',
      opacity: isUnsupported ? 0.5 : 1,
    }}
  >

                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
                          <rect x="3" y="2" width="18" height="20" rx="2" fill="#34A853" fillOpacity="0.2" stroke="#34A853" strokeWidth="1.5"/>
                          <line x1="7" y1="9" x2="17" y2="9" stroke="#34A853" strokeWidth="1.2"/>
                          <line x1="7" y1="13" x2="17" y2="13" stroke="#34A853" strokeWidth="1.2"/>
                          <line x1="7" y1="17" x2="13" y2="17" stroke="#34A853" strokeWidth="1.2"/>
                        </svg>
                        <span className="text-sm text-white flex-1">{sheet}</span>
                        {isUnsupported && (
          <span className="text-xs px-2 py-0.5 rounded-full flex-shrink-0"
            style={{ background: 'rgba(239,68,68,0.1)', color: '#f87171', border: '1px solid rgba(239,68,68,0.2)' }}>
            Not a Sheet — upload as .xlsx to Drive then convert
          </span>
        )}
        {selectedSheet === sheet && !isUnsupported && (
          <span style={{ color: '#34d399' }}>✓</span>
        )}
      </div>
  )}))}
                </div>
              </div>

              <button
                onClick={handlePick}
                disabled={!selectedSheet}
                className="w-full py-3 rounded-xl font-bold text-white text-sm disabled:opacity-40"
                style={{ background: 'linear-gradient(135deg, #34A853, #0d9488)' }}
              >
                {selectedSheet
                  ? `Use "${selectedSheet.length > 30 ? selectedSheet.slice(0, 30) + '…' : selectedSheet}"`
                  : 'Select a spreadsheet above'}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

function FullscreenTable({
  title, subtitle, columns, rows, onClose, accentColor = '#a78bfa',
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
    <div className="fixed inset-0 z-[60] flex flex-col" style={{ background: '#07050f' }}>
      <div className="flex items-center justify-between px-6 py-4 flex-shrink-0"
        style={{ borderBottom: '1px solid rgba(255,255,255,0.08)', background: 'rgba(255,255,255,0.02)' }}>
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg flex items-center justify-center"
            style={{ background: `${accentColor}20`, border: `1px solid ${accentColor}40` }}>
            <svg width="14" height="14" fill="none" stroke={accentColor} strokeWidth="2" viewBox="0 0 24 24">
              <rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/>
            </svg>
          </div>
          <div>
            <h2 className="font-bold text-white text-sm">{title}</h2>
            {subtitle && <p className="text-xs mt-0.5" style={{ color: 'rgba(255,255,255,0.4)' }}>{subtitle}</p>}
          </div>
          <span className="text-xs px-2.5 py-0.5 rounded-full font-medium ml-2"
            style={{ background: `${accentColor}15`, color: accentColor }}>
            {rows.length.toLocaleString()} rows · {columns.length} columns
          </span>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs" style={{ color: 'rgba(255,255,255,0.3)' }}>Press ESC to close</span>
          <button onClick={onClose}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold ml-2"
            style={{ background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.1)', color: 'rgba(255,255,255,0.6)' }}>
            Exit Fullscreen
          </button>
        </div>
      </div>
      <div className="flex-1 overflow-auto">
        <table className="w-full text-xs" style={{ color: 'rgba(255,255,255,0.65)', borderCollapse: 'collapse' }}>
          <thead className="sticky top-0" style={{ background: '#0a0714', zIndex: 10 }}>
            <tr>
              <th className="p-3 text-left font-semibold"
                style={{ borderBottom: '1px solid rgba(255,255,255,0.08)', color: 'rgba(255,255,255,0.3)', width: '52px' }}>#</th>
              {columns.map(col => (
                <th key={col} className="p-3 text-left font-semibold whitespace-nowrap"
                  style={{ borderBottom: '1px solid rgba(255,255,255,0.08)', color: 'rgba(255,255,255,0.8)' }}>{col}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i} style={{ borderBottom: '1px solid rgba(255,255,255,0.03)' }}
                onMouseEnter={e => (e.currentTarget.style.background = `${accentColor}08`)}
                onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}>
                <td className="p-3 font-mono" style={{ color: 'rgba(255,255,255,0.2)' }}>{i + 1}</td>
                {columns.map(col => (
                  <td key={col} className="p-3 whitespace-nowrap">
                    {row[col] === '' || row[col] === null || row[col] === undefined
                      ? <span style={{ color: '#f87171', fontStyle: 'italic' }}>null</span>
                      : typeof row[col] === 'number' ? row[col].toLocaleString() : String(row[col])}
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

// ── AI Custom Rule Modal ──────────────────────────────────────────────────────
function AICustomRuleModal({
  activeSourceName,
  colInfo,
  sampleData,
  onConfirm,
  onClose,
}: {
  activeSourceName: string
  colInfo: any[]
  sampleData: any[]
  onConfirm: (label: string, instruction: string) => void
  onClose: () => void
}) {
  const [messages, setMessages] = useState<{ role: 'user' | 'assistant'; content: string }[]>([
    {
      role: 'assistant',
      content: `Hi! Describe a cleaning operation for **${activeSourceName}** in plain English.\n\nExamples:\n- "Remove rows where age > 100"\n- "Fill missing salary with median salary per department"\n- "Flag rows where revenue is negative"\n- "Deduplicate on email column only"\n\nOnce you describe it, I'll confirm and add it as a pipeline step.`,
    },
  ])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [pendingInstruction, setPendingInstruction] = useState<string | null>(null)
  const [pendingLabel, setPendingLabel] = useState<string>('')
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const schemaContext = colInfo.slice(0, 15).map((c: any) =>
    `${c.column} (${c.dtype}, ${c.null} nulls, sample: "${c.sample}")`
  ).join('\n')

  const sampleContext = sampleData.slice(0, 2).map((row: any, i: number) =>
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
      const systemPrompt = `You are a data cleaning assistant inside a pipeline builder tool.
Dataset: ${activeSourceName}
Schema:\n${schemaContext}
Sample:\n${sampleContext}

The user wants to add a data cleaning RULE as a pipeline step.
Your job:
1. Understand their instruction
2. Reply with: a SHORT label (max 5 words, for the pipeline card) and a brief confirmation of what the rule will do.
3. Format your reply EXACTLY like this (no extra text):
LABEL: <short label here>
CONFIRM: <one sentence describing what will happen>

If the instruction is unclear, ask ONE clarifying question instead, without the LABEL/CONFIRM format.`

      const response = await fetch('https://api.anthropic.com/v1/messages', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model: 'claude-sonnet-4-20250514',
          max_tokens: 300,
          system: systemPrompt,
          messages: newMessages.map(m => ({ role: m.role, content: m.content })),
        }),
      })
      const data = await response.json()
      const text = data.content?.[0]?.text || ''
      const labelMatch = text.match(/LABEL:\s*(.+)/i)
      const confirmMatch = text.match(/CONFIRM:\s*(.+)/i)
      if (labelMatch && confirmMatch) {
        const label = labelMatch[1].trim()
        const confirm = confirmMatch[1].trim()
        setPendingInstruction(trimmed)
        setPendingLabel(label)
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: `✅ **${label}**\n\n${confirm}\n\nClick **"Add to Pipeline"** to add this as a step, or describe a different rule.`,
        }])
      } else {
        setMessages(prev => [...prev, { role: 'assistant', content: text }])
      }
    } catch {
      setMessages(prev => [...prev, { role: 'assistant', content: '⚠️ Could not reach AI. Check your connection.' }])
    } finally {
      setLoading(false)
    }
  }

  const renderMessage = (content: string) => {
    const parts = content.split(/(\*\*.*?\*\*)/g)
    return parts.map((p, i) =>
      p.startsWith('**') && p.endsWith('**')
        ? <strong key={i} style={{ color: 'white' }}>{p.slice(2, -2)}</strong>
        : <span key={i}>{p}</span>
    )
  }

  const QUICK = [
    'Remove rows where any numeric column is negative',
    'Fill missing text columns with "Unknown"',
    'Remove rows where email column has no @ symbol',
    'Cap all numeric columns at 99th percentile',
  ]

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: 'rgba(0,0,0,0.85)' }} onClick={onClose}>
      <div className="rounded-2xl w-full max-w-xl flex flex-col"
        style={{ background: '#0d0a1a', border: '1px solid rgba(236,72,153,0.3)', maxHeight: '85vh' }}
        onClick={e => e.stopPropagation()}>
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 flex-shrink-0"
          style={{ borderBottom: '1px solid rgba(255,255,255,0.08)' }}>
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg flex items-center justify-center"
              style={{ background: 'rgba(236,72,153,0.2)', border: '1px solid rgba(236,72,153,0.35)' }}>
              🤖
            </div>
            <div>
              <h3 className="font-bold text-white text-sm">Add AI Custom Rule</h3>
              <p className="text-xs" style={{ color: 'rgba(255,255,255,0.4)' }}>
                Describe a rule → it becomes a pipeline step
              </p>
            </div>
          </div>
          <button onClick={onClose} style={{ color: 'rgba(255,255,255,0.35)', fontSize: '18px' }}>✕</button>
        </div>

        {/* Quick buttons */}
        <div className="px-5 pt-4 flex flex-wrap gap-2 flex-shrink-0">
          {QUICK.map(q => (
            <button key={q} onClick={() => setInput(q)}
              className="text-xs px-3 py-1.5 rounded-full font-medium"
              style={{ background: 'rgba(236,72,153,0.1)', border: '1px solid rgba(236,72,153,0.25)', color: '#f472b6' }}>
              {q}
            </button>
          ))}
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-auto p-5 space-y-3" style={{ minHeight: 0 }}>
          {messages.map((msg, idx) => (
            <div key={idx} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
              {msg.role === 'assistant' && (
                <div className="w-6 h-6 rounded-md flex items-center justify-center flex-shrink-0 mr-2 mt-1 text-xs"
                  style={{ background: 'rgba(236,72,153,0.2)' }}>🤖</div>
              )}
              <div className="max-w-[85%] rounded-xl px-4 py-2.5 text-sm leading-relaxed"
                style={{
                  background: msg.role === 'user' ? 'rgba(236,72,153,0.2)' : 'rgba(255,255,255,0.04)',
                  border: msg.role === 'user' ? '1px solid rgba(236,72,153,0.35)' : '1px solid rgba(255,255,255,0.07)',
                  color: 'rgba(255,255,255,0.8)',
                }}>
                {renderMessage(msg.content)}
              </div>
            </div>
          ))}
          {loading && (
            <div className="flex justify-start">
              <div className="w-6 h-6 rounded-md flex items-center justify-center flex-shrink-0 mr-2 mt-1 text-xs"
                style={{ background: 'rgba(236,72,153,0.2)' }}>🤖</div>
              <div className="px-4 py-2.5 rounded-xl flex items-center gap-2"
                style={{ background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.07)' }}>
                {[0,1,2].map(i => (
                  <div key={i} className="w-1.5 h-1.5 rounded-full animate-bounce"
                    style={{ background: '#f472b6', animationDelay: `${i * 0.15}s` }} />
                ))}
              </div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        {/* Add to Pipeline button — only shown when AI confirmed a rule */}
        {pendingInstruction && (
          <div className="px-5 pb-3 flex-shrink-0">
            <button
              onClick={() => onConfirm(pendingLabel, pendingInstruction)}
              className="w-full py-2.5 rounded-xl font-bold text-white text-sm"
              style={{ background: 'linear-gradient(135deg, #ec4899, #8b5cf6)' }}>
              🤖 Add "{pendingLabel}" to Pipeline
            </button>
          </div>
        )}

        {/* Input */}
        <div className="px-5 pb-5 flex gap-2 flex-shrink-0"
          style={{ borderTop: '1px solid rgba(255,255,255,0.07)', paddingTop: '12px' }}>
          <input
            type="text"
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && !e.shiftKey && sendMessage()}
            placeholder='e.g. "remove rows where age > 100"'
            className="flex-1 text-sm text-white rounded-xl px-4 py-2.5 outline-none"
            style={{ background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(236,72,153,0.3)' }}
            disabled={loading}
          />
          <button onClick={sendMessage} disabled={loading || !input.trim()}
            className="px-4 py-2.5 rounded-xl font-bold text-white text-sm disabled:opacity-40"
            style={{ background: 'rgba(236,72,153,0.3)', border: '1px solid rgba(236,72,153,0.4)', flexShrink: 0 }}>
            Send
          </button>
        </div>
      </div>
    </div>
  )
}
// ─────────────────────────────────────────────────────────────────────────────
export default function Flow() {
  const navigate = useNavigate()
  const { sources, transformations, analysis, activeFile, addTransformation, removeTransformation, addAuditLog, setAnalysisTab, setPendingCleanOptions, switchActiveSource, setActiveDestinationName, setPipelineOutputFile, setLastPipelineRunAt, gsheetsDestination, setGsheetsDestination } = useStore()
  const [fullscreenTable, setFullscreenTable] = useState<{ title: string; subtitle?: string; columns: string[]; rows: any[]; accentColor?: string } | null>(null)
  const [savedPipelines, setSavedPipelines] = useState<SavedPipeline[]>(() => {
    try { return JSON.parse(localStorage.getItem('datapipe_pipelines') || '[]') } catch { return [] }
  })
  const persistPipelines = useCallback((pipes: SavedPipeline[]) => {
    setSavedPipelines(pipes); localStorage.setItem('datapipe_pipelines', JSON.stringify(pipes))
  }, [])

  const [selectedSourceId,  setSelectedSourceId]  = useState<string>(sources[0]?.name || '')
  const [destinationName,   setDestinationName]   = useState<string>('')
  const [destinationEdited, setDestinationEdited] = useState(false)
  // NEW: write mode and Google Sheets state
  const [destinationMode, setDestinationMode] = useState<'replace' | 'append'>('replace')
  const _setDestinationMode = setDestinationMode
  const [showGSheetsModal,  setShowGSheetsModal]  = useState(false)
  const [showGSheetsPicker, setShowGSheetsPicker] = useState(false)
  const [gsheetsPickerFor,  setGsheetsPickerFor]  = useState<'source'|'dest'>('dest')
  const [showAICustomModal, setShowAICustomModal] = useState(false)

  const [mysqlDestination, setMysqlDestination] = useState<any | null>(() => {
    try { return JSON.parse(localStorage.getItem('datapro_mysql_dest') || 'null') } catch { return null }
  })
  const setMysqlDest = (val: any | null) => {
    if (val) localStorage.setItem('datapro_mysql_dest', JSON.stringify(val))
    else localStorage.removeItem('datapro_mysql_dest')
    setMysqlDestination(val)
  }

  const [pgDestination, setPgDestination] = useState<any | null>(() => {
    try { return JSON.parse(localStorage.getItem('datapro_postgres_dest') || 'null') } catch { return null }
  })
  const setPgDest = (val: any | null) => {
    if (val) localStorage.setItem('datapro_postgres_dest', JSON.stringify(val))
    else localStorage.removeItem('datapro_postgres_dest')
    setPgDestination(val)
  }

  const [bqDestination, setBqDestination] = useState<any | null>(() => {
    try { return JSON.parse(localStorage.getItem('datapro_bigquery_dest') || 'null') } catch { return null }
  })
  const setBqDest = (val: any | null) => {
    if (val) localStorage.setItem('datapro_bigquery_dest', JSON.stringify(val))
    else localStorage.removeItem('datapro_bigquery_dest')
    setBqDestination(val)
  }

  const [awsDestinationState, setAwsDestinationState] = useState<any | null>(() => {
    try { return JSON.parse(localStorage.getItem('datapro_aws_dest') || 'null') } catch { return null }
  })
  const setAwsDest = (val: any | null) => {
    if (val) localStorage.setItem('datapro_aws_dest', JSON.stringify(val))
    else localStorage.removeItem('datapro_aws_dest')
    setAwsDestinationState(val)
  }

  const [mongoDestination, setMongoDestination] = useState<any | null>(() => {
    try { return JSON.parse(localStorage.getItem('datapro_mongo_dest') || 'null') } catch { return null } 
  })
  const setMongoDest = (val: any | null) => {
    if (val) localStorage.setItem('datapro_mongo_dest', JSON.stringify(val))
    else localStorage.removeItem('datapro_mongo_dest')
    setMongoDestination(val)
  }

  const [databricksDestination, setDatabricksDestination] = useState<any | null>(() => {
    try { return JSON.parse(localStorage.getItem('datapro_databricks_dest') || 'null') } catch { return null }
  })
  const setDatabricksDest = (val: any | null) => {
    if (val) localStorage.setItem('datapro_databricks_dest', JSON.stringify(val))
    else localStorage.removeItem('datapro_databricks_dest')
    setDatabricksDestination(val)
  }

  const [restDestination, setRestDestination] = useState<any | null>(() => {
    try { return JSON.parse(localStorage.getItem('datapro_rest_dest') || 'null') } catch { return null }
  })
  const setRestDest = (val: any | null) => {
    if (val) localStorage.setItem('datapro_rest_dest', JSON.stringify(val))
    else localStorage.removeItem('datapro_rest_dest')
    setRestDestination(val)
  }

  const gsheetsConnected = gsheetsDestination
  const setGsheetsConnected = (val: { email: string; sheetName: string; sheetId: string } | null) => {
    setGsheetsDestination(val)
    if (val?.sheetName) {
      setDestinationName(val.sheetName)
      setDestinationEdited(true)
    } else if (!val) {
      // on disconnect, reset to auto-name
      setDestinationEdited(false)
    }
  }
  const [showOriginalModal, setShowOriginalModal] = useState(false)  // ✅ correct placement

  const activeSource     = sources.find(s => s.name === selectedSourceId) || sources[0]
  const tableName        = activeSource?.name.replace('.csv','').replace(/[^a-zA-Z0-9_]/g,'_') || 'your_table'
  const effectiveDestination = gsheetsConnected?.sheetName || 
    (mysqlDestination ? `${mysqlDestination.database}.${mysqlDestination.table || mysqlDestination.tables?.[0] || '?'}_cleaned@${mysqlDestination.host}` : null) ||
    (pgDestination ? `${pgDestination.database}.${pgDestination.table || pgDestination.tables?.[0] || '?'}_cleaned@${pgDestination.host}` : null) ||
    (bqDestination ? `${bqDestination.project}.${bqDestination.dataset}.${bqDestination.table || bqDestination.tables?.[0] || '?'}` : null) ||
    (mongoDestination ? `${mongoDestination.database}.${mongoDestination.collection || mongoDestination.collections?.[0] || '?'}@mongo` : null) ||
    (awsDestinationState ? (awsDestinationState.service === 's3' ? `s3://${awsDestinationState.bucket}/${awsDestinationState.table || awsDestinationState.tables?.[0] || '?'}` : awsDestinationState.service === 'dynamodb' ? `${awsDestinationState.table || awsDestinationState.tables?.[0] || '?'}@dynamodb.${awsDestinationState.region}` : `${awsDestinationState.database}.${awsDestinationState.table || awsDestinationState.tables?.[0] || '?'}@${awsDestinationState.host}`) : null) ||
    (databricksDestination ? `${databricksDestination.catalog || 'hive_metastore'}.${databricksDestination.schema || databricksDestination.database || 'default'}.${databricksDestination.table || databricksDestination.tables?.[0] || '?'}@databricks` : null) ||
    (restDestination ? `rest:${(restDestination.base_url || '').replace(/https?:\/\//, '').replace(/\/$/, '')}${restDestination.endpoint || restDestination.endpoints?.[0] || '/'}` : null) ||
    destinationName
  const existingPipeline = savedPipelines.find(p => p.sourceId === selectedSourceId && p.destinationId === effectiveDestination)

  const [showSaveModal,           setShowSaveModal]           = useState(false)
  const [pipelineName,            setPipelineName]            = useState('')
  const [showScheduleModal, setShowScheduleModal] = useState<SavedPipeline|undefined>(undefined)
  const [showTransformPicker,     setShowTransformPicker]     = useState(false)
  const [showSQLDrawer,           setShowSQLDrawer]           = useState(false)
  const [showLogsPanel,           setShowLogsPanel]           = useState(false)
  const [showAlertModal,          setShowAlertModal]          = useState(false)
  const [showDriftPanel,          setShowDriftPanel]          = useState(false)
  const [showRulesPanel,          setShowRulesPanel]          = useState(false)
  const [showApprovalModal,       setShowApprovalModal]       = useState(false)
  const [showHowItWorks,          setShowHowItWorks]          = useState(false)
  const [selectedPipelineForLogs, setSelectedPipelineForLogs] = useState<SavedPipeline|undefined>(undefined)
  const [expandedLogId,           setExpandedLogId]           = useState<string|null>(null)
  const [alertType,               setAlertType]               = useState('missing')
  const [preview, setPreview] = useState<ClientPreview | null>(null)
  const [previewLoading,          setPreviewLoading]          = useState(false)
  const [copiedSQL,               setCopiedSQL]               = useState(false)
  const [pipelineRun,             setPipelineRun]             = useState<any>(null)
  const [baseline,                setBaseline]                = useState<any>(null)
  const [learnedRules,            setLearnedRules]            = useState<any[]>([])
  const [selectedIssueIds,        setSelectedIssueIds]        = useState<Set<string>>(new Set())
  const [approvingFixes,          setApprovingFixes]          = useState(false)
  const [driftLoading,            setDriftLoading]            = useState(false)
  const [scheduledTimers,         setScheduledTimers]         = useState<Record<string, ReturnType<typeof setInterval>>>({})
  const [lastRunResult, setLastRunResult] = useState<{ pipelineName: string; rowsIn: number; rowsOut: number; rowsAffected: number; mode: string; destinationType?: string } | null>(null)
const [postRunPreview, setPostRunPreview] = useState<{ columns: string[]; rows: any[] } | null>(null)
  // Save modal schedule state
  const [saveScheduleValue, setSaveScheduleValue] = useState('none')
  const [saveCustomCron,    setSaveCustomCron]    = useState('')
  const [saveCustomMs,      setSaveCustomMs]      = useState(60000)
  const [saveTimezone,      setSaveTimezone]      = useState('UTC')
  const [saveScheduleTime,  setSaveScheduleTime]  = useState('00:00')
  const [saveScheduleTab,   setSaveScheduleTab]   = useState<'frequency'|'daily-at'>('frequency')
  const [saveDailyAtTime,   setSaveDailyAtTime]   = useState('09:00')
  const [saveShowCalendar,  setSaveShowCalendar]  = useState(false)
const [saveStartDate,     setSaveStartDate]     = useState('')
const [activeSourceIsSheet, setActiveSourceIsSheet] = useState(false)
const [_gsheetsSource, _setGsheetsSource] = useState<{ email: string; sheetName: string } | null>(null)



  const unifiedSQL = useMemo(() =>
    buildUnifiedSQL(tableName, transformations.map(t => t.id), analysis?.issues || {}),
    [transformations, analysis, tableName]
  )

  useEffect(() => { if (!activeSource) return; loadPipelineData() }, [activeSource?.name])
  // AFTER:
useEffect(() => {
  if (transformations.length === 0) { setPreview(null); return }
  if (!activeFile) return
  fetchPreview()
  // eslint-disable-next-line react-hooks/exhaustive-deps
}, [transformations, activeFile])


  useEffect(() => {
  // Only auto-set destination when source changes AND user hasn't manually set it
  if (selectedSourceId && !destinationEdited) {
    // Leave destination empty — user fills it via the dropdown
  }
}, [selectedSourceId])

  useEffect(() => {
  // Only resume schedules that were already active — never auto-run on fresh upload
  savedPipelines.forEach(p => {
    if (p.isActive && p.scheduleMs && p.scheduleMs > 0 && p.lastRun && !scheduledTimers[p.id]) {
      startSchedule(p)
    }
  })
  return () => { Object.values(scheduledTimers).forEach(clearInterval) }
}, [])

  const startSchedule = (pipeline: SavedPipeline) => {
    if (!pipeline.scheduleMs || pipeline.scheduleMs <= 0) return
    const timer = setInterval(() => { runPipeline(pipeline, 'schedule') }, pipeline.scheduleMs)
    setScheduledTimers(prev => ({ ...prev, [pipeline.id]: timer }))
  }

  const stopSchedule = (pipelineId: string) => {
    const timer = scheduledTimers[pipelineId]
    if (timer) { clearInterval(timer); setScheduledTimers(prev => { const n={...prev}; delete n[pipelineId]; return n }) }
  }

  const runPipeline = useCallback(async (pipeline: SavedPipeline, triggeredBy: 'manual'|'schedule'|'api' = 'manual') => {
    const log: PipelineLogEntry = {
  id: crypto.randomUUID(),
  pipelineId: pipeline.id,
  timestamp: new Date().toISOString(),
  status: 'running',
  duration: 0,
  rowsIn: 0,
  rowsOut: 0,
  steps: pipeline.transformations.map(t => ({
    name: t.label,
    status: 'success' as const,
    duration: 0,
  })),
  triggeredBy,
  schedule: pipeline.scheduleLabel || undefined
}

    // ── Find the source file ─────────────────────────────────────────────
    const sourceObj = sources.find(s => s.name === pipeline.sourceId)
    let fileToUse = sourceObj?.file || (sourceObj?.type !== ('sheets' as string) ? activeFile : null) || null 

    // ── If source is MySQL, fetch table as CSV first ──────────────────────
    if (sourceObj?.type === 'mysql') {
      try {
        const savedMySQL = JSON.parse(localStorage.getItem('datapro_mysql_connections') || '[]')
        // sourceObj.name format: "database.table@host"
        // Format: "database.table@host" or "database.table1.table2@host"
const atIdx = (sourceObj.name || '').lastIndexOf('@')
const srcHost = atIdx >= 0 ? sourceObj.name.slice(atIdx + 1) : ''
const beforeAt = atIdx >= 0 ? sourceObj.name.slice(0, atIdx) : sourceObj.name || ''
const dotIdx = beforeAt.indexOf('.')
const srcDatabase = dotIdx >= 0 ? beforeAt.slice(0, dotIdx) : beforeAt
const srcTable = dotIdx >= 0 ? beforeAt.slice(dotIdx + 1) : ''
console.log('[Pipeline] Parsed MySQL source — database:', srcDatabase, 'table:', srcTable, 'host:', srcHost)
        const mysqlConn = savedMySQL.find((c: any) =>
  c.host === srcHost && c.database === srcDatabase
)
if (!mysqlConn) {
  console.error('[Pipeline] No matching MySQL connection found for', srcHost, srcDatabase)
  console.error('[Pipeline] Available connections:', savedMySQL.map((c: any) => `${c.database}@${c.host}`))
}
if (mysqlConn) {
  const tableToFetch = srcTable || mysqlConn.table || mysqlConn.tables?.[0] || ''
  console.log('[Pipeline] Fetching MySQL table:', tableToFetch, 'from', srcDatabase, '@', srcHost)
  const mysqlRes = await fetch('${import.meta.env.VITE_API_URL}/api/mysql/fetch-table', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      host: mysqlConn.host,
      port: mysqlConn.port,
      user: mysqlConn.user,
      password: mysqlConn.password,
      database: mysqlConn.database,
      ssl: mysqlConn.ssl,
      table: tableToFetch,
    }),
  })
          const mysqlData = await mysqlRes.json()
          if (mysqlData.download_url) {
            const csvRes = await fetch(`${import.meta.env.VITE_API_URL}${mysqlData.download_url}`)
            const blob = await csvRes.blob()
            fileToUse = new File([blob], `${sourceObj.name}.csv`, { type: 'text/csv' })
            console.log('[Pipeline] Fetched MySQL table as CSV:', mysqlData.rows, 'rows')
          }
        }
      } catch (e) {
  console.error('[Pipeline] MySQL fetch failed:', e)
  const realLog: PipelineLogEntry = {
    ...log,
    status: 'failed',
    duration: Date.now() - new Date(log.timestamp).getTime(),
    steps: pipeline.transformations.map(t => ({
      name: t.label,
      status: 'failed' as const,
      duration: 0,
      message: `MySQL fetch failed: ${String(e)}`,
    })),
  }
  const updated = savedPipelines.map(p =>
    p.id === pipeline.id
      ? { ...p, lastRun: realLog.timestamp, runCount: p.runCount + 1, logs: [realLog, ...p.logs].slice(0, 50) }
      : p
  )
  persistPipelines(updated)
  return realLog
}
    } // ← closes: if (sourceObj?.type === 'mysql')

    // ── If source is PostgreSQL, fetch table as CSV first ────────────────
    if (sourceObj?.type === 'postgres') {
      try {
        const savedPG = JSON.parse(localStorage.getItem('datapro_postgres_connections') || '[]')
        const atIdx = (sourceObj.name || '').lastIndexOf('@')
        const srcHost = atIdx >= 0 ? sourceObj.name.slice(atIdx + 1) : ''
        const beforeAt = atIdx >= 0 ? sourceObj.name.slice(0, atIdx) : sourceObj.name || ''
        const dotIdx = beforeAt.indexOf('.')
        const srcDatabase = dotIdx >= 0 ? beforeAt.slice(0, dotIdx) : beforeAt
        const srcTable = dotIdx >= 0 ? beforeAt.slice(dotIdx + 1) : ''
        console.log('[Pipeline] Parsed PostgreSQL source — database:', srcDatabase, 'table:', srcTable, 'host:', srcHost)
        const pgConn = savedPG.find((c: any) =>
          c.host === srcHost && c.database === srcDatabase
        )
        if (!pgConn) {
          console.error('[Pipeline] No matching PostgreSQL connection found for', srcHost, srcDatabase)
        }
        if (pgConn) {
          const tableToFetch = srcTable || pgConn.table || pgConn.tables?.[0] || ''
          console.log('[Pipeline] Fetching PostgreSQL table:', tableToFetch, 'from', srcDatabase, '@', srcHost)
          const pgRes = await fetch('${import.meta.env.VITE_API_URL}/api/postgres/fetch-table', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              host: pgConn.host,
              port: pgConn.port,
              user: pgConn.user,
              password: pgConn.password,
              database: pgConn.database,
              ssl: pgConn.ssl,
              table: tableToFetch,
            }),
          })
          const pgData = await pgRes.json()
          if (pgData.download_url) {
            const csvRes = await fetch(`${import.meta.env.VITE_API_URL}${pgData.download_url}`)
            const blob = await csvRes.blob()
            fileToUse = new File([blob], `${sourceObj.name}.csv`, { type: 'text/csv' })
            console.log('[Pipeline] Fetched PostgreSQL table as CSV:', pgData.rows, 'rows')
          } else {
            console.error('[Pipeline] PostgreSQL fetch failed:', pgData.error)
          }
        }
      } catch (e) {
        console.error('[Pipeline] PostgreSQL fetch failed:', e)
        const realLog: PipelineLogEntry = {
          ...log,
          status: 'failed',
          duration: Date.now() - new Date(log.timestamp).getTime(),
          steps: pipeline.transformations.map(t => ({
            name: t.label,
            status: 'failed' as const,
            duration: 0,
            message: `PostgreSQL fetch failed: ${String(e)}`,
          })),
        }
        const updated = savedPipelines.map(p =>
          p.id === pipeline.id
            ? { ...p, lastRun: realLog.timestamp, runCount: p.runCount + 1, logs: [realLog, ...p.logs].slice(0, 50) }
            : p
        )
        persistPipelines(updated)
        return realLog
      }
    }

    // ── If source is BigQuery, fetch table as CSV first ──────────────────
    if (sourceObj?.type === 'bigquery') {
      try {
        const savedBQ = JSON.parse(localStorage.getItem('datapro_bigquery_connections') || '[]')
        const parts = (sourceObj.name || '').split('.')
        const srcProject = parts[0] || ''
        const srcDataset = parts[1] || ''
        const srcTable   = parts[2] || ''
        const bqConn = savedBQ.find((c: any) => c.project === srcProject && c.dataset === srcDataset)
        if (bqConn) {
          const tableToFetch = srcTable || bqConn.table || bqConn.tables?.[0] || ''
          const bqRes = await fetch('${import.meta.env.VITE_API_URL}/api/bigquery/fetch-table', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              key_json: bqConn.key_json,
              project:  bqConn.project,
              dataset:  bqConn.dataset,
              table:    tableToFetch,
            }),
          })
          const bqData = await bqRes.json()
          if (bqData.download_url) {
            const csvRes = await fetch(`${import.meta.env.VITE_API_URL}${bqData.download_url}`)
            const blob = await csvRes.blob()
            fileToUse = new File([blob], `${sourceObj.name}.csv`, { type: 'text/csv' })
            console.log('[Pipeline] Fetched BigQuery table as CSV:', bqData.rows, 'rows')
          }
        }
      } catch (e) {
        console.error('[Pipeline] BigQuery fetch failed:', e)
      }
    }

    // ── If source is MongoDB, fetch collection as CSV first ──────────────
    if (sourceObj?.type === 'mongodb') {
      try {
        const savedMongo = JSON.parse(localStorage.getItem('datapro_mongo_connections') || '[]')
        const atIdx = (sourceObj.name || '').lastIndexOf('@')
        const beforeAt = atIdx >= 0 ? sourceObj.name.slice(0, atIdx) : sourceObj.name || ''
        const dotIdx = beforeAt.indexOf('.')
        const srcDatabase = dotIdx >= 0 ? beforeAt.slice(0, dotIdx) : beforeAt
        const srcCollection = dotIdx >= 0 ? beforeAt.slice(dotIdx + 1) : ''
        const mongoConn = savedMongo.find((c: any) => c.database === srcDatabase)
        if (mongoConn) {
          const collToFetch = srcCollection || mongoConn.collection || mongoConn.collections?.[0] || ''
          console.log('[Pipeline] Fetching MongoDB collection:', collToFetch, 'from', srcDatabase)
          const mongoRes = await fetch('${import.meta.env.VITE_API_URL}/api/mongo/fetch-collection', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              uri: mongoConn.uri,
              database: mongoConn.database,
              collection: collToFetch,
            }),
          })
          const mongoData = await mongoRes.json()
          if (mongoData.download_url) {
            const csvRes = await fetch(`${import.meta.env.VITE_API_URL}${mongoData.download_url}`)
            const blob = await csvRes.blob()
            fileToUse = new File([blob], `${sourceObj.name}.csv`, { type: 'text/csv' })
            console.log('[Pipeline] Fetched MongoDB collection as CSV:', mongoData.rows, 'rows')
          } else {
            console.error('[Pipeline] MongoDB fetch failed:', mongoData.error)
          }
        }
      } catch (e) {
        console.error('[Pipeline] MongoDB fetch failed:', e)
        const realLog: PipelineLogEntry = {
          ...log,
          status: 'failed',
          duration: Date.now() - new Date(log.timestamp).getTime(),
          steps: pipeline.transformations.map(t => ({
            name: t.label,
            status: 'failed' as const,
            duration: 0,
            message: `MongoDB fetch failed: ${String(e)}`,
          })),
        }
        const updated = savedPipelines.map(p =>
          p.id === pipeline.id
            ? { ...p, lastRun: realLog.timestamp, runCount: p.runCount + 1, logs: [realLog, ...p.logs].slice(0, 50) }
            : p
        )
        persistPipelines(updated)
        return realLog
      }
    }

    // ── If source is a Google Sheet, fetch it as CSV first ───────────────
    if (sourceObj?.type === 'sheets') {
      try {
        const sheetRes = await fetch('${import.meta.env.VITE_API_URL}/api/sheets/to-csv', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            sheetId:   sourceObj.sheetId || '',
            sheetName: sourceObj.name,
            tab:       sourceObj.tab || 'Sheet1',
          }),
        })
        const sheetData = await sheetRes.json()
        if (sheetData.download_url) {
          const csvRes = await fetch(`${import.meta.env.VITE_API_URL}${sheetData.download_url}`)
          const blob = await csvRes.blob()
          fileToUse = new File([blob], `${sourceObj.name}.csv`, { type: 'text/csv' })
          console.log('[Pipeline] Fetched Google Sheet as CSV:', sheetData.rows, 'rows')
        } else {
          console.error('[Pipeline] Could not fetch sheet as CSV:', sheetData.error)
        }
      } catch (e) {
        console.error('[Pipeline] Sheet fetch failed:', e)
      }
    }

        // ── If source is GCS, fetch file as CSV first ────────────────────────
    if (sourceObj && (sourceObj.type as string) === 'gcs') {
      try {
        const savedGCSMeta = JSON.parse(localStorage.getItem('datapro_gcs_connections') || '[]')
        const srcNameGCS = sourceObj.name || ''
        const withoutProto = srcNameGCS.replace('gcs://', '')
        const slashIdx = withoutProto.indexOf('/')
        const bucket = slashIdx >= 0 ? withoutProto.slice(0, slashIdx) : withoutProto
        const file = slashIdx >= 0 ? withoutProto.slice(slashIdx + 1) : ''
        const gcsConn = savedGCSMeta.find((c: any) => c.bucket === bucket)
        if (gcsConn) {
          const fetchRes = await fetch('${import.meta.env.VITE_API_URL}/api/gcs/fetch-file', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key_json: gcsConn.keyJson, project: gcsConn.project, bucket, file }),
          })
          const fetchData = await fetchRes.json()
          if (fetchData.download_url) {
            const csvRes = await fetch(`${import.meta.env.VITE_API_URL}${fetchData.download_url}`)
            const blob = await csvRes.blob()
            fileToUse = new File([blob], `${srcNameGCS}.csv`, { type: 'text/csv' })
            console.log('[Pipeline] Fetched GCS file as CSV')
          } else {
            console.error('[Pipeline] GCS fetch failed:', fetchData.error)
          }
        }
      } catch (e) {
        console.error('[Pipeline] GCS fetch failed:', e)
      }
    }

    // ── If source is Cloud SQL, fetch table as CSV first ─────────────────
    if (sourceObj && (sourceObj.type as string) === 'cloudsql') {
      try {
        const savedCSQLMeta = JSON.parse(localStorage.getItem('datapro_cloudsql_connections') || '[]')
        const srcNameCSQL = sourceObj.name || ''
        const atIdx = srcNameCSQL.lastIndexOf('@')
        const beforeAt = atIdx >= 0 ? srcNameCSQL.slice(0, atIdx) : srcNameCSQL
        const dotIdx = beforeAt.indexOf('.')
        const database = dotIdx >= 0 ? beforeAt.slice(0, dotIdx) : beforeAt
        const table = dotIdx >= 0 ? beforeAt.slice(dotIdx + 1) : ''
        const projectPart = atIdx >= 0 ? srcNameCSQL.slice(atIdx + 1).replace('cloudsql.', '') : ''
        const csqlConn = savedCSQLMeta.find((c: any) => c.database === database && c.project === projectPart)
        if (csqlConn) {
          const fetchRes = await fetch('${import.meta.env.VITE_API_URL}/api/cloudsql/fetch-table', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ...csqlConn, table }),
          })
          const fetchData = await fetchRes.json()
          if (fetchData.download_url) {
            const csvRes = await fetch(`${import.meta.env.VITE_API_URL}${fetchData.download_url}`)
            const blob = await csvRes.blob()
            fileToUse = new File([blob], `${srcNameCSQL}.csv`, { type: 'text/csv' })
            console.log('[Pipeline] Fetched Cloud SQL table as CSV:', fetchData.rows, 'rows')
          } else {
            console.error('[Pipeline] Cloud SQL fetch failed:', fetchData.error)
          }
        }
      } catch (e) {
        console.error('[Pipeline] Cloud SQL fetch failed:', e)
      }
    }

        // ── If source is Databricks, fetch table as CSV first ────────────────
    if (sourceObj && (sourceObj.type as string) === 'databricks') {
      try {
        const savedDB = JSON.parse(localStorage.getItem('datapro_databricks_connections') || '[]')
        const srcNameDB = sourceObj.name || ''
        // name format: "catalog.schema.table@host"
        const atIdx = srcNameDB.lastIndexOf('@')
        const srcHost = atIdx >= 0 ? srcNameDB.slice(atIdx + 1) : ''
        const beforeAt = atIdx >= 0 ? srcNameDB.slice(0, atIdx) : srcNameDB
        const parts = beforeAt.split('.')
        const catalog = parts.length >= 3 ? parts[0] : 'hive_metastore'
        const schema  = parts.length >= 3 ? parts[1] : (parts[0] || 'default')
        const table   = parts.length >= 3 ? parts[2] : (parts[1] || '')
        const dbConn = savedDB.find((c: any) => c.host === srcHost)
        if (dbConn) {
          const fetchRes = await fetch('${import.meta.env.VITE_API_URL}/api/databricks/fetch-table', {
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
            const csvRes = await fetch(`${import.meta.env.VITE_API_URL}${fetchData.download_url}`)
            const blob = await csvRes.blob()
            fileToUse = new File([blob], `${srcNameDB}.csv`, { type: 'text/csv' })
            console.log('[Pipeline] Fetched Databricks table as CSV:', fetchData.rows, 'rows')
          } else {
            console.error('[Pipeline] Databricks fetch failed:', fetchData.error)
          }
        }
      } catch (e) {
        console.error('[Pipeline] Databricks fetch failed:', e)
      }
    }

    // ── If source is REST API, fetch endpoint as CSV first ───────────────
    if (sourceObj && (sourceObj.type as string) === 'rest') {
      try {
        const savedRestMeta = JSON.parse(localStorage.getItem('datapro_rest_connections') || '[]')
        const srcNameRest = sourceObj.name || ''
        const withoutProto = srcNameRest.replace('rest:', '')
        const conn = savedRestMeta.find((c: any) => {
          const connHost = (c.base_url || '').replace(/https?:\/\//, '').replace(/\/$/, '')
          return withoutProto.startsWith(connHost)
        })
        if (conn) {
          const connHost = (conn.base_url || '').replace(/https?:\/\//, '').replace(/\/$/, '')
          const endpoint = withoutProto.slice(connHost.length) || '/'
          const fetchRes = await fetch('${import.meta.env.VITE_API_URL}/api/rest/fetch-endpoint', {
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
            const csvRes = await fetch(`${import.meta.env.VITE_API_URL}${fetchData.download_url}`)
            const blob = await csvRes.blob()
            fileToUse = new File([blob], `${srcNameRest}.csv`, { type: 'text/csv' })
            console.log('[Pipeline] Fetched REST API endpoint as CSV:', fetchData.rows, 'rows')
          } else {
            console.error('[Pipeline] REST API fetch failed:', fetchData.error)
          }
        }
      } catch (e) {
        console.error('[Pipeline] REST API fetch failed:', e)
      }
    }

    // ── If source is GA4, fetch report as CSV first ──────────────────────
    if (sourceObj && (sourceObj.type as string) === 'ga4') {
      try {
        const savedGA4Meta = JSON.parse(localStorage.getItem('datapro_ga4_connections') || '[]')
        const srcNameGA4 = sourceObj.name || ''
        const withoutProto = srcNameGA4.replace('ga4:', '')
        const slashIdx = withoutProto.indexOf('/')
        const propertyId = slashIdx >= 0 ? withoutProto.slice(0, slashIdx) : withoutProto
        const reportName = slashIdx >= 0 ? withoutProto.slice(slashIdx + 1) : ''
        const ga4Conn = savedGA4Meta.find((c: any) => c.propertyId === propertyId)
        if (ga4Conn) {
          const fetchRes = await fetch('${import.meta.env.VITE_API_URL}/api/ga4/fetch-report', {
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
            const csvRes = await fetch(`${import.meta.env.VITE_API_URL}${fetchData.download_url}`)
            const blob = await csvRes.blob()
            fileToUse = new File([blob], `${srcNameGA4}.csv`, { type: 'text/csv' })
            console.log('[Pipeline] Fetched GA4 report as CSV:', fetchData.rows, 'rows')
          } else {
            console.error('[Pipeline] GA4 fetch failed:', fetchData.error)
          }
        } else {
          console.error('[Pipeline] No GA4 connection found for property:', propertyId)
        }
      } catch (e) {
        console.error('[Pipeline] GA4 fetch failed:', e)
      }
    }

        let data: any = null
if (!fileToUse) {
  console.error('[Pipeline] No file available to process — MySQL fetch may have failed or returned empty')
  const failLog: PipelineLogEntry = {
    ...log,
    status: 'failed',
    duration: Date.now() - new Date(log.timestamp).getTime(),
    steps: pipeline.transformations.map(t => ({
      name: t.label,
      status: 'failed' as const,
      duration: 0,
      message: 'Could not fetch source data — check MySQL connection and table name',
    })),
  }
  const updated = savedPipelines.map(p =>
    p.id === pipeline.id
      ? { ...p, lastRun: failLog.timestamp, runCount: p.runCount + 1, logs: [failLog, ...p.logs].slice(0, 50) }
      : p
  )
  persistPipelines(updated)
  return failLog
}
if (fileToUse) {
  try {
    const formData = new FormData()
    formData.append('file', fileToUse)
    pipeline.transformations.forEach(t => {
      const key = TRANSFORM_TO_CLEAN[t.id]
      if (key) formData.append(key, 'true')
    })
    if (pipeline.transformations.find(t => t.id === 'ai_clean')) {
      formData.append('ai_clean', 'true')
    }
    const writeModeVal = pipeline.destinationMode || 'replace'
    formData.append('write_mode', writeModeVal)

// If appending, send the existing destination file to backend for merging
if (writeModeVal === 'append') {
  // Priority: 1) a loaded source that matches dest name, 2) any stored pipeline output
  const destSource = sources.find(s => s.name === pipeline.destinationId)
  const storedOutput = useStore.getState().pipelineOutputFile
  // Also check if destination name matches any OTHER loaded source (second dataset)
  const destAsSource = sources.find(s => 
    s.name === pipeline.destinationName || 
    s.name === pipeline.destinationId
  )
  const destFile =
    destAsSource?.file ||
    destSource?.file ||
    storedOutput ||   // use ANY stored output as append target
    null
  if (destFile) {
    formData.append('destination_file', destFile)
    console.log('[Pipeline] Appending onto:', destFile.name, destFile.size, 'bytes')
  } else {
    console.warn('[Pipeline] Append mode but no destination file found — running as replace')
  }
}

        const res = await fetch('/api/clean-data', { method: 'POST', body: formData })
        data = await res.json()

        // ── Fetch the cleaned file and store it ──────────────────────────
        if (data.download_url) {
          const fileRes = await fetch(`${import.meta.env.VITE_API_URL}${data.download_url}`)
          const blob = await fileRes.blob()
          const outputFile = new File([blob], pipeline.destinationName, { type: 'text/csv' })

          // Atomic store update: set the output file AND switch source in one go
// so Analysis.tsx's useEffect sees pipelineOutputFile already set
useStore.setState({
  pipelineOutputFile: outputFile,
  activeFile: outputFile,
  activeSourceName: pipeline.destinationName,
  activeDestinationName: pipeline.destinationName,
  analysis: null,
  analysisTab: 'preview',
  lastPipelineRunAt: new Date().toISOString(),
})

// ── Build post-run preview from cleaned CSV (works for ALL source types) ──
try {
  const csvText = await outputFile.text()
  const { headers, rows } = parseCSV(csvText)
  setPostRunPreview({
    columns: headers,
    rows: rows.slice(0, 100),
  })
} catch (e) {
  console.error('Post-run preview failed:', e)
}
        }
      } catch (e) {
        console.error('Pipeline backend call failed:', e)
      }
    }
    const activeGsheets = useStore.getState().gsheetsDestination
const activeMysqlDest = (() => { try { const v = localStorage.getItem('datapro_mysql_dest'); return v ? JSON.parse(v) : null } catch { return null } })()
const activeDatabricksDest = (() => { try { const v = localStorage.getItem('datapro_databricks_dest'); return v ? JSON.parse(v) : null } catch { return null } })()
const isGsheets = pipeline.destinationId?.toLowerCase().includes('sheet') || !!activeGsheets
const activeMongoDestConn = (() => { try { const v = localStorage.getItem('datapro_mongo_dest'); return v ? JSON.parse(v) : null } catch { return null } })()
const activePgDest = (() => { try { const v = localStorage.getItem('datapro_postgres_dest'); return v ? JSON.parse(v) : null } catch { return null } })()
const activeBqDest = (() => { try { const v = localStorage.getItem('datapro_bigquery_dest'); return v ? JSON.parse(v) : null } catch { return null } })()
const activeAwsDest = (() => { try { const v = localStorage.getItem('datapro_aws_dest'); return v ? JSON.parse(v) : null } catch { return null } })()
const actualRowsIn = data?.original_shape?.[0] ?? 0
const actualRowsOut = data?.cleaned_shape?.[0] ?? 0
if (actualRowsIn > 0 || data?.download_url) {
  setLastRunResult({
    pipelineName: pipeline.name,
    rowsIn: actualRowsIn,
    rowsOut: actualRowsOut,
    rowsAffected: actualRowsIn - actualRowsOut,
    mode: pipeline.destinationMode,
    destinationType: isGsheets ? 'sheets' : activeMysqlDest ? 'mysql' : activePgDest ? 'postgres' : activeBqDest ? 'bigquery' : activeMongoDestConn ? 'mongodb' : activeAwsDest ? 'aws' : activeDatabricksDest ? 'databricks' : 'csv',
  })
}

// ── Push to Google Sheets if connected ───────────────────────────────────────
    if (activeGsheets && data?.download_url) {
      try {
        const sheetRes = await fetch(`${import.meta.env.VITE_API_URL}${data.download_url}`)
        if (!sheetRes.ok) throw new Error('Could not download cleaned CSV')
        const csvText = await sheetRes.text()

        if (!csvText || csvText.trim().length === 0) {
          console.error('[GSheets] Empty CSV — skipping sheet update')
        } else {
          const storedSession = localStorage.getItem('datapro_session') || ''
          const updateRes = await fetch('${import.meta.env.VITE_API_URL}/api/update-gsheet', {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              ...(storedSession ? { 'X-Session-ID': storedSession } : {})
            },
            body: JSON.stringify({
              sheetId:   activeGsheets.sheetId || '',
              sheetName: activeGsheets.sheetName,
              email:     activeGsheets.email,
              csvData:   csvText,
              sessionId: storedSession,
            }),
          })
          const updateData = await updateRes.json()
          if (updateData.success) {
            console.log(`✅ Google Sheet "${activeGsheets.sheetName}" updated: ${updateData.rows_written} rows`)
            // Show a brief toast-style alert so user knows it worked
            alert(`✅ Google Sheet "${activeGsheets.sheetName}" updated with ${updateData.rows_written} rows!`)
          } else if (updateData.skipped) {
            console.warn('[GSheets] Rate limited:', updateData.reason)
          } else {
            console.error('[GSheets] Update failed:', updateData.error)
            alert(`⚠️ Sheet update failed: ${updateData.error}`)
          }
        }
      } catch (e) {
        console.error('Google Sheets update failed:', e)
        alert(`⚠️ Could not update Google Sheet. Check console for details.`)
      }
    }
    // ── Write back to MySQL if destination is MySQL ──────────────────────────────
    const mysqlDestConn = (() => {
      try {
        const destVal = localStorage.getItem('datapro_mysql_dest')
        return destVal ? JSON.parse(destVal) : null
      } catch { return null }
    })()

if (mysqlDestConn && data?.download_url) {
  try {
    const csvRes2 = await fetch(`${import.meta.env.VITE_API_URL}${data.download_url}`)
    if (csvRes2.ok) {
      const csvText2 = await csvRes2.text()
      // Use the source table name for destination (append _cleaned suffix server-side)
      // Extract dest table from pipeline destinationId: "database.table_cleaned@host"
const destTable = mysqlDestConn.table || mysqlDestConn.tables?.[0] || ''
console.log('[Pipeline] Writing to MySQL table (overwrite in-place):', destTable)
      const writeRes = await fetch('${import.meta.env.VITE_API_URL}/api/mysql/write-table', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          host: mysqlDestConn.host,
          port: mysqlDestConn.port,
          user: mysqlDestConn.user,
          password: mysqlDestConn.password,
          database: mysqlDestConn.database,
          table: destTable,
          ssl: mysqlDestConn.ssl,
          csvData: csvText2,
          mode: pipeline.destinationMode,
        }),
      })
          const writeData = await writeRes.json()
if (writeData.success) {
  console.log(`✅ MySQL table "${writeData.table}" updated: ${writeData.rows_written} rows`)
  // setLastRunResult already handles the success toast — no alert needed
} else {
            console.error('[MySQL write-back] failed:', writeData.error)
            alert(`⚠️ MySQL write failed: ${writeData.error}`)
          }
        }
      } catch (e) {
        console.error('MySQL write-back failed:', e)
      }
    }

    // ── Write back to PostgreSQL if destination is PostgreSQL ─────────────
    const pgDestConn = (() => {
      try {
        const destVal = localStorage.getItem('datapro_postgres_dest')
        return destVal ? JSON.parse(destVal) : null
      } catch { return null }
    })()

    if (pgDestConn && data?.download_url) {
      try {
        const csvRes3 = await fetch(`${import.meta.env.VITE_API_URL}${data.download_url}`)
        if (csvRes3.ok) {
          const csvText3 = await csvRes3.text()
          const destTable3 = pgDestConn.table || pgDestConn.tables?.[0] || ''
          console.log('[Pipeline] Writing to PostgreSQL table:', destTable3)
          const writeRes3 = await fetch('${import.meta.env.VITE_API_URL}/api/postgres/write-table', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              host: pgDestConn.host,
              port: pgDestConn.port,
              user: pgDestConn.user,
              password: pgDestConn.password,
              database: pgDestConn.database,
              table: destTable3,
              ssl: pgDestConn.ssl,
              csvData: csvText3,
              mode: pipeline.destinationMode,
            }),
          })
          const writeData3 = await writeRes3.json()
          if (writeData3.success) {
            console.log(`✅ PostgreSQL table "${writeData3.table}" updated: ${writeData3.rows_written} rows`)
          } else {
            console.error('[PostgreSQL write-back] failed:', writeData3.error)
            alert(`⚠️ PostgreSQL write failed: ${writeData3.error}`)
          }
        }
      } catch (e) {
        console.error('PostgreSQL write-back failed:', e)
      }
    }

    // ── Write back to BigQuery if destination is BigQuery ─────────────────
    const bqDestConn = (() => {
      try {
        const destVal = localStorage.getItem('datapro_bigquery_dest')
        return destVal ? JSON.parse(destVal) : null
      } catch { return null }
    })()

    if (bqDestConn && data?.download_url) {
      try {
        const csvRes4 = await fetch(`${import.meta.env.VITE_API_URL}${data.download_url}`)
        if (csvRes4.ok) {
          const csvText4 = await csvRes4.text()
          const destTable4 = bqDestConn.table || bqDestConn.tables?.[0] || ''
          console.log('[Pipeline] Writing to BigQuery table:', destTable4)
          const writeRes4 = await fetch('${import.meta.env.VITE_API_URL}/api/bigquery/write-table', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              key_json: bqDestConn.key_json,
              project:  bqDestConn.project,
              dataset:  bqDestConn.dataset,
              table:    destTable4,
              csvData:  csvText4,
              mode:     pipeline.destinationMode,
            }),
          })
          const writeData4 = await writeRes4.json()
          if (writeData4.success) {
            console.log(`✅ BigQuery table "${writeData4.table}" updated: ${writeData4.rows_written} rows`)
          } else {
            console.error('[BigQuery write-back] failed:', writeData4.error)
            alert(`⚠️ BigQuery write failed: ${writeData4.error}`)
          }
        }
      } catch (e) {
        console.error('BigQuery write-back failed:', e)
      }
    }
    // ── Write back to AWS if destination is AWS ──────────────────────────
    const awsDestConn = (() => {
      try {
        const v = localStorage.getItem('datapro_aws_dest')
        return v ? JSON.parse(v) : null
      } catch { return null }
    })()

    if (awsDestConn && data?.download_url) {
      try {
        const csvRes6 = await fetch(`${import.meta.env.VITE_API_URL}${data.download_url}`)
        if (csvRes6.ok) {
          const csvText6 = await csvRes6.text()
          const destItem6 = awsDestConn.table || awsDestConn.tables?.[0] || ''
          console.log('[Pipeline] Writing to AWS', awsDestConn.service, ':', destItem6)
          const writeRes6 = await fetch('${import.meta.env.VITE_API_URL}/api/aws/write-table', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              ...awsDestConn,
              service: awsDestConn.service,
              table: destItem6,
              csvData: csvText6,
              mode: pipeline.destinationMode,
            }),
          })
          const writeData6 = await writeRes6.json()
          if (writeData6.success) {
            console.log(`✅ AWS ${awsDestConn.service} write: ${destItem6} — ${writeData6.rows_written} rows`)
          } else {
            console.error('[AWS write-back] failed:', writeData6.error)
            alert(`⚠️ AWS write failed: ${writeData6.error}`)
          }
        }
      } catch (e) {
        console.error('AWS write-back failed:', e)
      }
    }
    // ── Write back to MongoDB if destination is MongoDB ──────────────────
    const mongoDestConn = (() => {
      try {
        const destVal = localStorage.getItem('datapro_mongo_dest')
        return destVal ? JSON.parse(destVal) : null
      } catch { return null }
    })()

    if (mongoDestConn && data?.download_url) {
      try {
        const csvRes5 = await fetch(`${import.meta.env.VITE_API_URL}${data.download_url}`)
        if (csvRes5.ok) {
          const csvText5 = await csvRes5.text()
          const destColl5 = mongoDestConn.collection || mongoDestConn.collections?.[0] || ''
          console.log('[Pipeline] Writing to MongoDB collection:', destColl5)
          const writeRes5 = await fetch('${import.meta.env.VITE_API_URL}/api/mongo/write-collection', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              uri: mongoDestConn.uri,
              database: mongoDestConn.database,
              collection: destColl5,
              csvData: csvText5,
              mode: pipeline.destinationMode,
            }),
          })
          const writeData5 = await writeRes5.json()
          if (writeData5.success) {
            console.log(`✅ MongoDB collection "${writeData5.collection}" updated: ${writeData5.rows_written} docs`)
          } else {
            console.error('[MongoDB write-back] failed:', writeData5.error)
            alert(`⚠️ MongoDB write failed: ${writeData5.error}`)
          }
        }
      } catch (e) {
        console.error('MongoDB write-back failed:', e)
      }
    }

    // ── Write back to Databricks if destination is Databricks ────────────
    const databricksDestConn = (() => {
      try {
        const v = localStorage.getItem('datapro_databricks_dest')
        return v ? JSON.parse(v) : null
      } catch { return null }
    })()

    if (databricksDestConn && data?.download_url) {
      try {
        const csvResDB = await fetch(`${import.meta.env.VITE_API_URL}${data.download_url}`)
        if (csvResDB.ok) {
          const csvTextDB = await csvResDB.text()
          const destTableDB = databricksDestConn.table || databricksDestConn.tables?.[0] || ''
          const destCatalogDB = databricksDestConn.catalog || 'hive_metastore'
          const destSchemaDB = databricksDestConn.schema || databricksDestConn.database || 'default'
          console.log('[Pipeline] Writing to Databricks table:', destCatalogDB, destSchemaDB, destTableDB)
          const writeResDB = await fetch('${import.meta.env.VITE_API_URL}/api/databricks/write-table', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              host: databricksDestConn.host,
              token: databricksDestConn.token,
              http_path: databricksDestConn.http_path,
              catalog: destCatalogDB,
              schema: destSchemaDB,
              table: destTableDB,
              csvData: csvTextDB,
              mode: pipeline.destinationMode,
            }),
          })
          const writeDataDB = await writeResDB.json()
          if (writeDataDB.success) {
            console.log(`✅ Databricks table "${destCatalogDB}.${destSchemaDB}.${destTableDB}" updated: ${writeDataDB.rows_written} rows`)
          } else {
            console.error('[Databricks write-back] failed:', writeDataDB.error)
            alert(`⚠️ Databricks write failed: ${writeDataDB.error}`)
          }
        }
      } catch (e) {
        console.error('Databricks write-back failed:', e)
      }
    }

    // ── Write back to REST API if destination is REST API ────────────────
    const restDestConn = (() => {
      try {
        const v = localStorage.getItem('datapro_rest_dest')
        return v ? JSON.parse(v) : null
      } catch { return null }
    })()

    if (restDestConn && data?.download_url) {
      try {
        const csvResRest = await fetch(`${import.meta.env.VITE_API_URL}${data.download_url}`)
        if (csvResRest.ok) {
          const csvTextRest = await csvResRest.text()
          const destEndpointRest = restDestConn.endpoint || restDestConn.endpoints?.[0] || '/'
          console.log('[Pipeline] Writing to REST API endpoint:', destEndpointRest)
          const writeResRest = await fetch('${import.meta.env.VITE_API_URL}/api/rest/write-endpoint', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              base_url: restDestConn.base_url,
              endpoint: destEndpointRest,
              headers: restDestConn.headers || {},
              method: restDestConn.write_method || 'POST',
              verify_ssl: restDestConn.verify_ssl,
              send_as: restDestConn.send_as || 'json',
              csvData: csvTextRest,
              mode: pipeline.destinationMode,
            }),
          })
          const writeDataRest = await writeResRest.json()
          if (writeDataRest.success) {
            console.log(`✅ REST API write-back: ${destEndpointRest} — ${writeDataRest.rows_written} rows`)
          } else {
            console.error('[REST API write-back] failed:', writeDataRest.error)
            alert(`⚠️ REST API write failed: ${writeDataRest.error}`)
          }
        }
      } catch (e) {
        console.error('REST API write-back failed:', e)
      }
    }// ── Update log in state ──────────────────────────────────────────────
const realRowsIn = data?.original_shape?.[0] ?? 0
const realRowsOut = data?.cleaned_shape?.[0] ?? 0
const realLog: PipelineLogEntry = {
  ...log,
  status: data?.download_url ? 'success' : 'failed',
  duration: Date.now() - new Date(log.timestamp).getTime(),
  rowsIn: realRowsIn,
  rowsOut: realRowsOut,
  steps: pipeline.transformations.map(t => ({
    name: t.label,
    status: (data?.download_url ? 'success' : 'failed') as 'success' | 'failed' | 'skipped',
    duration: Math.floor((Date.now() - new Date(log.timestamp).getTime()) / pipeline.transformations.length),
    message: !data?.download_url ? 'Transform failed — check backend logs' : undefined,
  })),
}
    const updated = savedPipelines.map(p =>
      p.id === pipeline.id
        ? { ...p, lastRun: realLog.timestamp, runCount: p.runCount + 1, logs: [realLog, ...p.logs].slice(0, 50) }
        : p
    )
    persistPipelines(updated)
    addAuditLog({
      id: crypto.randomUUID(), time: new Date().toLocaleString(),
      action: 'pipeline_run', entity_type: 'pipeline', entity_id: pipeline.id,
      details: `Pipeline "${pipeline.name}" ran · ${log.status} · ${log.rowsIn}→${log.rowsOut} rows · triggered by ${triggeredBy}`,
      user: 'local_user'
    })

    // ── Navigate to Analysis to show destination dataset ─────────────────
    // Scroll to saved pipelines section instead of navigating away
    setTimeout(() => {
      document.getElementById('saved-pipelines-section')?.scrollIntoView({ behavior: 'smooth' })
    }, 300)

    return log
  }, [savedPipelines, persistPipelines, addAuditLog, activeFile, sources,
      setPipelineOutputFile, setLastPipelineRunAt, setActiveDestinationName,
      switchActiveSource, navigate])

  const loadPipelineData = async () => {
    setDriftLoading(true)
    try {
      const [runRes, baselineRes, rulesRes] = await Promise.all([
        fetch(`/api/pipeline/${tableName}/run`),
        fetch(`/api/pipeline/${tableName}/baseline`),
        fetch(`/api/pipeline/${tableName}/rules`),
      ])
      setPipelineRun(await runRes.json()); setBaseline(await baselineRes.json()); setLearnedRules(await rulesRes.json())
    } catch {} finally { setDriftLoading(false) }
  }

  const approveFixes = async () => {
    if (selectedIssueIds.size === 0) return; setApprovingFixes(true)
    try {
      await fetch(`/api/pipeline/${tableName}/approve`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ approved_issue_ids:[...selectedIssueIds] }) })
      setShowApprovalModal(false); setSelectedIssueIds(new Set()); await loadPipelineData()
      addAuditLog({ id:crypto.randomUUID(), time:new Date().toLocaleString(), action:'approve_fixes', entity_type:'pipeline', entity_id:tableName, details:`Approved ${selectedIssueIds.size} fix(es)`, user:'local_user' })
    } catch {} finally { setApprovingFixes(false) }
  }

  const rollbackRule = async (ruleId: string) => {
    try { await fetch(`/api/pipeline/${tableName}/rules/${ruleId}`, { method:'DELETE' }); setLearnedRules(prev => prev.filter(r => r.id !== ruleId)) } catch {}
  }

const fetchPreview = useCallback(async () => {
  if (transformations.length === 0) { setPreview(null); return }
  if (!activeFile) return
  setPreviewLoading(true)
  try {
    const text = await activeFile.text()
    const { headers, rows } = parseCSV(text)
    const transformed = applyTransforms(headers, rows, transformations.map(t => t.id))
    setPreview({
      columns: headers,
      rows: transformed.slice(0, 100),
      totalRows: transformed.length,
      rowsRemoved: rows.length - transformed.length,
      sourceRows: rows.length,
    })
  } catch (e) {
    console.error('Preview failed:', e)
    setPreview(null)
  } finally {
    setPreviewLoading(false)
  }
}, [activeFile, transformations])

  const addT = (t: typeof TRANSFORMATIONS[0]) => {
    if (transformations.find(x => x.id === t.id)) return
    addTransformation({ ...t, type:t.id, applied:false })
    addAuditLog({ id:crypto.randomUUID(), time:new Date().toLocaleString(), action:'add_transformation', entity_type:'transformation', entity_id:t.id, details:`Added: ${t.label}`, user:'local_user' })
    const allTransforms = [...transformations, t]
    const cleanOpts: Record<string,boolean> = { remove_duplicates:false, remove_empty_rows:false, standardize_text:false, fix_dates:false, handle_outliers:false, fill_missing:false, ai_clean:false }
allTransforms.forEach(tr => { const key=TRANSFORM_TO_CLEAN[tr.id]; if (key) cleanOpts[key]=true })
    if (analysis?.issues) { Object.entries(analysis.issues).forEach(([issue,detected]) => { if (detected) { const key=ISSUE_TO_CLEAN[issue]; if (key) cleanOpts[key]=true } }) }
    setPendingCleanOptions(cleanOpts); setAnalysisTab('clean'); setShowTransformPicker(false)
  }

  const removeT = (id: string) => {
    removeTransformation(id)
    addAuditLog({ id:crypto.randomUUID(), time:new Date().toLocaleString(), action:'remove_transformation', entity_type:'transformation', entity_id:id, details:`Removed transformation: ${id}`, user:'local_user' })
  }

  const fmt12Save = (v: string) => { if (!v) return ''; const [h,m]=v.split(':').map(Number); return `${h%12||12}:${String(m).padStart(2,'0')} ${h>=12?'PM':'AM'}` }
  const fmtDateSave = (iso: string) => { if (!iso) return ''; const [y,mo,d]=iso.split('-'); const months=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']; return `${months[Number(mo)-1]} ${Number(d)}, ${y}` }

  const isSaveAbsolute   = ['24h','1w'].includes(saveScheduleValue)
  const isSaveCustom     = saveScheduleValue === 'custom'
  const showSaveTimezone = isSaveAbsolute || isSaveCustom || saveScheduleTab === 'daily-at'

  const handleSavePipeline = () => {
    if (!pipelineName.trim() || transformations.length === 0 || !effectiveDestination.trim()) return

    let schedMs: number | null = null
    let schedCron: string | null = null
    let schedLabel: string | null = null
    const finalTz        = showSaveTimezone ? saveTimezone : null
    const finalStartDate = saveStartDate || null

    if (saveScheduleTab === 'daily-at' && saveDailyAtTime) {
      const [h, m] = saveDailyAtTime.split(':').map(Number)
      schedMs    = 86_400_000
      schedCron  = `${m} ${h} * * *`
      schedLabel = `Daily at ${fmt12Save(saveDailyAtTime)}`
    } else if (saveScheduleValue !== 'none') {
      const preset = SCHEDULE_PRESETS.find(p => p.value === saveScheduleValue)
      schedMs    = saveScheduleValue === 'live' ? 5_000 : saveScheduleValue === 'custom' ? saveCustomMs : (preset?.ms || 0)
      schedCron  = saveScheduleValue === 'custom' ? saveCustomCron : (preset?.cron || null)
      schedLabel = saveScheduleValue === 'custom' ? `Custom (${fmtDur(saveCustomMs)})` : (preset?.label || null)
      if (isSaveAbsolute) {
        const [h, m] = saveScheduleTime.split(':')
        const hour = parseInt(h || '0')
        const min  = parseInt(m || '0')
        if (saveScheduleValue === '24h') { schedCron = `${min} ${hour} * * *`; schedLabel = `Daily at ${saveScheduleTime}` }
        else                             { schedCron = `${min} ${hour} * * 0`; schedLabel = `Weekly at ${saveScheduleTime}` }
      }
    }

    const existing = savedPipelines.find(p => p.sourceId === selectedSourceId && p.destinationId === effectiveDestination)

    const pipeline: SavedPipeline = {
      id:               existing?.id || crypto.randomUUID(),
      name:             pipelineName,
      sourceId:         selectedSourceId,
      destinationId:    effectiveDestination,
      sourceName:       activeSource?.name || selectedSourceId,
      destinationName:  effectiveDestination,
      transformations:  [...transformations],
      createdAt:        existing?.createdAt || new Date().toLocaleString(),
      schedule:         schedCron,
      scheduleLabel:    schedLabel,
      scheduleMs:       schedMs && schedMs > 0 ? schedMs : null,
      scheduleTimezone: finalTz,
      scheduleStartDate: finalStartDate,
      lastRun:          existing?.lastRun || null,
      runCount:         existing?.runCount || 0,
      logs:             existing?.logs || [],
      isActive:         !!(schedMs && schedMs > 0),
      destinationMode: destinationMode, 
    }

    if (existing) stopSchedule(existing.id)
    const newPipelines = existing
      ? savedPipelines.map(p => p.id === existing.id ? pipeline : p)
      : [...savedPipelines, pipeline]
    persistPipelines(newPipelines)
    if (schedMs && schedMs > 0) startSchedule(pipeline)

    addAuditLog({ id:crypto.randomUUID(), time:new Date().toLocaleString(), action:'save_pipeline', entity_type:'pipeline', entity_id:pipeline.id, details:`Saved "${pipelineName}" · ${selectedSourceId} → ${effectiveDestination} · ${transformations.length} transforms${schedLabel ? ` · ${schedLabel}` : ''}${finalStartDate ? ` · starts ${finalStartDate}` : ''}`, user:'local_user' })
    setShowSaveModal(false)
setPipelineName('')
setSaveScheduleValue('none')
setSaveStartDate('')
setSaveScheduleTab('frequency')
// Scroll to saved pipelines to show proof it was saved
setTimeout(() => {
  document.getElementById('saved-pipelines-section')?.scrollIntoView({ behavior: 'smooth' })
}, 300)
  }

  // FIX: was `scheduleLabel` (undefined), now correctly `schedLabel` (the local param)
  const handleScheduleUpdate = (pipeline: SavedPipeline, schedMs: number|null, schedLabel: string|null, schedCron: string|null, timezone: string, startDate: string|null) => {
    stopSchedule(pipeline.id)
    const updated: SavedPipeline = { ...pipeline, schedule: schedCron, scheduleLabel: schedLabel, scheduleMs: schedMs, scheduleTimezone: timezone, scheduleStartDate: startDate, isActive: !!schedMs && schedMs > 0 }
    persistPipelines(savedPipelines.map(p => p.id === pipeline.id ? updated : p))
    if (schedMs && schedMs > 0) startSchedule(updated)
    addAuditLog({ id:crypto.randomUUID(), time:new Date().toLocaleString(), action:'update_schedule', entity_type:'pipeline', entity_id:pipeline.id, details:schedLabel ? `Schedule set to "${schedLabel}" (${timezone})${startDate ? ` starting ${startDate}` : ''} for "${pipeline.name}"` : `Schedule removed for "${pipeline.name}"`, user:'local_user' })
    setShowScheduleModal(undefined)
  }

  const copySQL = () => { navigator.clipboard.writeText(unifiedSQL); setCopiedSQL(true); setTimeout(() => setCopiedSQL(false), 2000) }

  const runStatus    = pipelineRun?.status || 'GREEN'
  const driftScore   = pipelineRun?.driftScore || 0
  const runIssues: any[] = pipelineRun?.issues || []
  const statusColors = SEV_COLOR[runStatus] || SEV_COLOR.GREEN
  const isFirstRun   = !baseline || baseline.runCount === 0
  const destination  = effectiveDestination || (activeSource ? `${activeSource.name.replace('.csv','')}_cleaned.csv` : null)
  const getLatestLog = (p: SavedPipeline|undefined) => (!p || p.logs.length===0) ? null : p.logs[0]

  if (sources.length === 0) {
    return (
      <div className="p-8 max-w-5xl mx-auto">
        <div className="text-center py-24 rounded-2xl" style={{ background:'rgba(255,255,255,0.02)', border:'1px dashed rgba(255,255,255,0.08)' }}>
          <div className="text-5xl mb-4">🔗</div>
          <p className="text-white font-bold text-lg mb-2">No data source connected</p>
          <p className="text-sm mb-6" style={{ color:'rgba(255,255,255,0.35)' }}>Add a source first to build your pipeline</p>
          <button onClick={() => navigate('/sources')} className="px-6 py-3 rounded-xl text-sm font-bold text-white" style={{ background:'linear-gradient(135deg, #7c3aed, #2563eb)' }}>Go to Data Sources →</button>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen" style={{ background:'#07050f' }}>
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
      {/* TOP BAR */}
      <div className="flex items-center justify-between px-8 py-4 flex-wrap gap-3" style={{ borderBottom:'1px solid rgba(255,255,255,0.06)', background:'rgba(255,255,255,0.01)' }}>
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-xl flex items-center justify-center" style={{ background:'linear-gradient(135deg, #7c3aed, #2563eb)' }}>
            <svg width="16" height="16" fill="none" stroke="white" strokeWidth="2" viewBox="0 0 24 24"><circle cx="5" cy="12" r="2"/><circle cx="19" cy="5" r="2"/><circle cx="19" cy="19" r="2"/><path d="M7 12h4l4-5"/><path d="M11 12h4l2 5"/></svg>
          </div>
          <div>
            <h1 className="text-lg font-bold text-white">Pipeline Builder</h1>
            <p className="text-xs" style={{ color:'rgba(255,255,255,0.35)' }}>
              {savedPipelines.length} saved pipeline{savedPipelines.length!==1?'s':''}
              {savedPipelines.filter(p => p.isActive).length > 0 && <span className="ml-2" style={{ color:'#4ade80' }}>· {savedPipelines.filter(p => p.isActive).length} scheduled active</span>}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <button onClick={() => setShowDriftPanel(true)} className="flex items-center gap-2 px-3 py-2 rounded-xl text-xs font-semibold" style={{ background:statusColors.bg, border:`1px solid ${statusColors.border}`, color:statusColors.text }}>
            <span className="w-2 h-2 rounded-full animate-pulse" style={{ background:statusColors.dot }} />Schema: {statusColors.label}{driftScore>0&&<span className="opacity-70">· {driftScore}% drift</span>}
          </button>
          {savedPipelines.length > 0 && (
            <button onClick={() => { setSelectedPipelineForLogs(savedPipelines[0]); setShowLogsPanel(true) }} className="flex items-center gap-2 px-3 py-2 rounded-xl text-xs font-semibold" style={{ background:'rgba(99,102,241,0.1)', border:'1px solid rgba(99,102,241,0.25)', color:'#818cf8' }}>
              <svg width="12" height="12" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>Pipeline Runs
            </button>
          )}
          {learnedRules.length > 0 && <button onClick={() => setShowRulesPanel(true)} className="flex items-center gap-2 px-3 py-2 rounded-xl text-xs font-semibold" style={{ background:'rgba(124,58,237,0.12)', border:'1px solid rgba(124,58,237,0.25)', color:'#a78bfa' }}>🧠 {learnedRules.length} rules</button>}
          {runIssues.length > 0 && runStatus!=='GREEN' && <button onClick={() => setShowApprovalModal(true)} className="flex items-center gap-2 px-3 py-2 rounded-xl text-xs font-semibold" style={{ background:'rgba(239,68,68,0.12)', border:'1px solid rgba(239,68,68,0.25)', color:'#f87171' }}>⚠️ {runIssues.length} changes — Review</button>}
          {transformations.length > 0 && (
            <button onClick={() => setShowSQLDrawer(true)} className="flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-semibold" style={{ background:'rgba(99,102,241,0.12)', border:'1px solid rgba(99,102,241,0.25)', color:'#818cf8' }}>
              <svg width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M4 6h16M4 10h16M4 14h8M4 18h8"/></svg>SQL
            </button>
          )}
          <button onClick={() => setShowAlertModal(true)} className="flex items-center gap-2 px-3 py-2 rounded-xl text-sm font-semibold" style={{ background:'rgba(239,68,68,0.08)', border:'1px solid rgba(239,68,68,0.2)', color:'#f87171' }}>
            <svg width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9M13.73 21a2 2 0 0 1-3.46 0"/></svg>Alert
          </button>
          <button onClick={() => setShowTransformPicker(true)} className="flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-semibold text-white" style={{ background:'linear-gradient(135deg, #7c3aed, #2563eb)', boxShadow:'0 0 20px rgba(124,58,237,0.3)' }}>
            <span className="text-base">+</span> Add Transformation
          </button>
        </div>
      </div>

      <div className="p-8 space-y-6 max-w-7xl mx-auto">

        {/* SOURCE → DESTINATION */}
        <div className="rounded-2xl p-6 space-y-5" style={{ background:'rgba(255,255,255,0.02)', border:'1px solid rgba(255,255,255,0.07)' }}>
          <p className="text-xs font-bold tracking-widest uppercase" style={{ color:'rgba(255,255,255,0.3)' }}>Configure Pipeline Pair</p>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">

            {/* SOURCE — native <select> */}
            <div>
              <label className="text-xs font-semibold block mb-2" style={{ color:'rgba(255,255,255,0.5)' }}>📥 Source Dataset</label>
              <div className="flex flex-wrap gap-1.5 mb-2">
{(['CSV', 'Google Sheets', 'MySQL', 'PostgreSQL', 'BigQuery', 'MongoDB', 'AWS', 'GCS', 'Cloud SQL', 'Databricks', 'REST API', 'GA4'] as const).map(type => {
  const activeSourceIsGSheets = activeSource?.type === 'sheets'
  const activeSourceIsMySQL   = activeSource?.type === 'mysql'
  const activeSourceIsPG      = activeSource?.type === 'postgres'
  const activeSourceIsBQ      = activeSource?.type === 'bigquery'
  const activeSourceIsMongo   = activeSource?.type === 'mongodb'
  const activeSourceIsAWS     = activeSource?.type === 'aws'
  const activeSourceIsGCS     = (activeSource?.type as string) === 'gcs'
  const activeSourceIsCloudSQL = (activeSource?.type as string) === 'cloudsql'
  const activeSourceIsDatabricks = (activeSource?.type as string) === 'databricks'
  const isSourceActive =
    type === 'Google Sheets' ? (activeSourceIsSheet || activeSourceIsGSheets) :
    type === 'CSV'            ? (!activeSourceIsSheet && !activeSourceIsGSheets && !activeSourceIsMySQL && !activeSourceIsPG && !activeSourceIsBQ && !activeSourceIsMongo && !activeSourceIsAWS && !activeSourceIsGCS && !activeSourceIsCloudSQL && !activeSourceIsDatabricks) :
    type === 'MySQL'          ? activeSourceIsMySQL :
    type === 'PostgreSQL'     ? activeSourceIsPG :
    type === 'BigQuery'       ? activeSourceIsBQ :
    type === 'MongoDB'        ? activeSourceIsMongo :
    type === 'AWS'            ? activeSourceIsAWS :
    type === 'GCS'            ? activeSourceIsGCS :
    type === 'Cloud SQL'      ? activeSourceIsCloudSQL :
    type === 'Databricks'     ? activeSourceIsDatabricks :
    type === 'REST API'       ? (activeSource?.type as string) === 'rest' :
    type === 'GA4'            ? (activeSource?.type as string) === 'ga4' : false
  return (
    <button key={type}
      onClick={() => {
        if (type === 'Google Sheets' && !activeSourceIsSheet && !activeSourceIsGSheets) { setGsheetsPickerFor('source'); setShowGSheetsPicker(true) }
        if (type === 'CSV') { setActiveSourceIsSheet(false); if (sources.find(s => s.type === 'csv')) setSelectedSourceId(sources.find(s => s.type === 'csv')!.name) }
        if (type === 'AWS') {
          const awsSrc = sources.find(s => s.type === 'aws')
          if (awsSrc) { setSelectedSourceId(awsSrc.name); setActiveSourceIsSheet(false) }
          else alert('No AWS sources connected. Add one in Data Sources first.')
        }
        if (type === 'Databricks') {
          const dbSrc = sources.find(s => (s.type as string) === 'databricks')
          if (dbSrc) { setSelectedSourceId(dbSrc.name); setActiveSourceIsSheet(false) }
          else alert('No Databricks sources connected. Add one in Data Sources first.')
        }
        if (type === 'REST API') {
          const restSrc = sources.find(s => (s.type as string) === 'rest')
          if (restSrc) { setSelectedSourceId(restSrc.name); setActiveSourceIsSheet(false) }
          else alert('No REST API sources connected. Add one in Data Sources first.')
        }
        if (type === 'GA4') {
          const ga4Src = sources.find(s => (s.type as string) === 'ga4')
          if (ga4Src) { setSelectedSourceId(ga4Src.name); setActiveSourceIsSheet(false) }
          else alert('No GA4 sources connected. Add one in Data Sources first.')
        }
        if (type === 'PostgreSQL') {
          const pgSrc = sources.find(s => s.type === 'postgres')
          if (pgSrc) { setSelectedSourceId(pgSrc.name); setActiveSourceIsSheet(false) }
          else alert('No PostgreSQL sources connected. Add one in Data Sources first.')
        }
        if (type === 'BigQuery') {
          const bqSrc = sources.find(s => s.type === 'bigquery')
          if (bqSrc) { setSelectedSourceId(bqSrc.name); setActiveSourceIsSheet(false) }
          else alert('No BigQuery sources connected. Add one in Data Sources first.')
        }
        if (type === 'MongoDB') {
          const mongoSrc = sources.find(s => s.type === 'mongodb')
          if (mongoSrc) { setSelectedSourceId(mongoSrc.name); setActiveSourceIsSheet(false) }
          else alert('No MongoDB sources connected. Add one in Data Sources first.')
        }
        if (type === 'GCS') {
          const gcsSrc = sources.find(s => (s.type as string) === 'gcs')
          if (gcsSrc) { setSelectedSourceId(gcsSrc.name); setActiveSourceIsSheet(false) }
          else alert('No Cloud Storage sources connected. Add one in Data Sources first.')
        }
        if (type === 'Cloud SQL') {
          const csqlSrc = sources.find(s => (s.type as string) === 'cloudsql')
          if (csqlSrc) { setSelectedSourceId(csqlSrc.name); setActiveSourceIsSheet(false) }
          else alert('No Cloud SQL sources connected. Add one in Data Sources first.')
        }
      }}
      className="py-1.5 px-2 rounded-lg text-xs font-semibold transition-all"
      style={{
        background: isSourceActive ? (type === 'MySQL' ? 'rgba(234,179,8,0.2)' : type === 'PostgreSQL' ? 'rgba(99,102,241,0.2)' : type === 'MongoDB' ? 'rgba(34,197,94,0.2)' : type === 'AWS' ? 'rgba(255,153,0,0.2)' : type === 'GCS' ? 'rgba(66,133,244,0.2)' : type === 'Cloud SQL' ? 'rgba(52,168,83,0.2)' : type === 'Databricks' ? 'rgba(255,99,0,0.2)' : type === 'REST API' ? 'rgba(20,184,166,0.2)' : type === 'GA4' ? 'rgba(251,146,60,0.2)' : 'rgba(124,58,237,0.2)') : 'rgba(255,255,255,0.03)',
        border: `1px solid ${isSourceActive ? (type === 'MySQL' ? 'rgba(234,179,8,0.5)' : type === 'PostgreSQL' ? 'rgba(99,102,241,0.5)' : type === 'MongoDB' ? 'rgba(34,197,94,0.5)' : type === 'AWS' ? 'rgba(255,153,0,0.5)' : type === 'GCS' ? 'rgba(66,133,244,0.5)' : type === 'Cloud SQL' ? 'rgba(52,168,83,0.5)' : type === 'Databricks' ? 'rgba(255,99,0,0.5)' : type === 'REST API' ? 'rgba(20,184,166,0.5)' : type === 'GA4' ? 'rgba(251,146,60,0.5)' : 'rgba(124,58,237,0.5)') : 'rgba(255,255,255,0.06)'}`,
        color: isSourceActive ? (type === 'MySQL' ? '#fbbf24' : type === 'PostgreSQL' ? '#818cf8' : type === 'MongoDB' ? '#4ade80' : type === 'AWS' ? '#FF9900' : type === 'GCS' ? '#4285F4' : type === 'Cloud SQL' ? '#34A853' : type === 'Databricks' ? '#FF6300' : type === 'REST API' ? '#14b8a6' : type === 'GA4' ? '#fb923c' : '#c4b5fd') : 'rgba(255,255,255,0.55)',
        cursor: 'pointer',
      }}>
      {type === 'MySQL' ? (
        <span className="flex items-center justify-center gap-1">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
            <ellipse cx="12" cy="5" rx="9" ry="3" fill="#f59e0b" fillOpacity="0.7"/>
            <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" stroke="#f59e0b" strokeWidth="1.5" fill="none"/>
            <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" stroke="#f59e0b" strokeWidth="1.5" fill="none"/>
          </svg>
          MySQL
        </span>
      ) : type === 'PostgreSQL' ? (
        <span className="flex items-center justify-center gap-1">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
            <ellipse cx="12" cy="5" rx="9" ry="3" fill="#6366f1" fillOpacity="0.7"/>
            <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" stroke="#6366f1" strokeWidth="1.5" fill="none"/>
            <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" stroke="#6366f1" strokeWidth="1.5" fill="none"/>
          </svg>
          PostgreSQL
        </span>
      ) : type === 'BigQuery' ? (
        <span className="flex items-center justify-center gap-1">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
            <path d="M12 2L22 19H2L12 2Z" fill="#ea580c" fillOpacity="0.7" stroke="#ea580c" strokeWidth="1.5" strokeLinejoin="round"/>
          </svg>
          BigQuery
        </span>
      ) : type === 'MongoDB' ? (
        <span className="flex items-center justify-center gap-1">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
            <ellipse cx="12" cy="5" rx="9" ry="3" fill="#22c55e" fillOpacity="0.7"/>
            <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" stroke="#22c55e" strokeWidth="1.5" fill="none"/>
            <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" stroke="#22c55e" strokeWidth="1.5" fill="none"/>
          </svg>
          MongoDB
        </span>
      ) : type === 'AWS' ? (
        <span className="flex items-center justify-center gap-1">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
            <path d="M6.5 20C4.01 20 2 17.99 2 15.5C2 13.01 4.01 11 6.5 11C6.5 8.24 8.74 6 11.5 6C13.85 6 15.82 7.57 16.4 9.74C16.6 9.72 16.8 9.7 17 9.7C19.21 9.7 21 11.49 21 13.7C21 15.91 19.21 17.7 17 17.7H16.5" stroke="#FF9900" strokeWidth="1.5" strokeLinecap="round"/>
          </svg>
          AWS
        </span>
      ) : type === 'GCS' ? (
        <span className="flex items-center justify-center gap-1">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
            <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" stroke="#4285F4" strokeWidth="1.5" strokeLinejoin="round"/>
          </svg>
          Cloud Storage
        </span>
      ) : type === 'Cloud SQL' ? (
        <span className="flex items-center justify-center gap-1">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
            <ellipse cx="12" cy="5" rx="9" ry="3" fill="#34A853" fillOpacity="0.7"/>
            <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" stroke="#34A853" strokeWidth="1.5" fill="none"/>
            <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" stroke="#34A853" strokeWidth="1.5" fill="none"/>
          </svg>
          Cloud SQL
        </span>
) : type === 'Databricks' ? (
          <span className="flex items-center justify-center gap-1">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
              <path d="M12 2L2 8l10 6 10-6-10-6zM2 16l10 6 10-6M2 12l10 6 10-6" stroke="#FF6300" strokeWidth="1.5" strokeLinejoin="round"/>
            </svg>
            Databricks
          </span>
      ) : type === 'REST API' ? (
          <span className="flex items-center justify-center gap-1">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
              <circle cx="12" cy="12" r="9" stroke="#14b8a6" strokeWidth="1.5"/>
              <path d="M2 12h20M12 3a13 13 0 0 1 3 9 13 13 0 0 1-3 9 13 13 0 0 1-3-9 13 13 0 0 1 3-9z" stroke="#14b8a6" strokeWidth="1.5" fill="none"/>
            </svg>
            REST API
          </span>
      ) : type === 'GA4' ? (
          <span className="flex items-center justify-center gap-1">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
              <path d="M3 3v18h18" stroke="#fb923c" strokeWidth="1.5" strokeLinecap="round"/>
              <path d="M7 16l4-4 4 4 4-8" stroke="#fb923c" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
            GA4
          </span>
      ) : type}
    </button>
  )
})}
</div>
              <div style={{ position:'relative' }}>
                <select
                  value={selectedSourceId}
                  onChange={e => { setSelectedSourceId(e.target.value); setDestinationEdited(false) }}
                  className="w-full text-sm text-white rounded-xl px-4 py-3 outline-none appearance-none"
                  style={{ background:'rgba(124,58,237,0.1)', border:'1px solid rgba(124,58,237,0.3)', paddingRight:'36px', cursor:'pointer' }}
                >
                  {sources.map(s => (
                    <option key={s.name} value={s.name} style={{ background:'#0d0a1a' }} title={s.name}>
                      {s.type === 'mysql'
                        ? `🗄️ ${s.name.split('.')[0]}.${s.name.split('.')[1]?.split('@')[0] || '?'} @ ${s.name.split('@')[1] || ''}`
                        : s.name}
                    </option>
                  ))}
                </select>
                <div style={{ position:'absolute', right:'12px', top:'50%', transform:'translateY(-50%)', pointerEvents:'none' }}>
                  <svg width="12" height="12" fill="none" stroke="rgba(167,139,250,0.7)" strokeWidth="2" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
                </div>
              </div>
              <p className="text-xs mt-1.5 break-all" style={{ color:'rgba(255,255,255,0.25)' }}>
                {activeSource ? `${activeSource.type?.toUpperCase()} · ${activeSource.name} · ${activeSource.addedAt}` : 'Select a loaded source file'}
              </p>
            </div>

            {/* DESTINATION — combobox with clear button + GSheets + write mode */}
            <div>
              <label className="text-xs font-semibold block mb-2" style={{ color:'rgba(255,255,255,0.5)' }}>📤 Destination / Output Name</label>
              <div className="flex flex-wrap gap-1.5 mb-2">
  {(['CSV', 'Google Sheets', 'MySQL', 'PostgreSQL', 'BigQuery', 'MongoDB', 'AWS', 'GCS', 'Cloud SQL', 'Databricks', 'REST API', 'GA4'] as const).map(type => {
    const isActive =
      type === 'Google Sheets' ? !!gsheetsConnected :
      type === 'AWS' ? !!awsDestinationState :
      type === 'CSV' ? (!gsheetsConnected && !mysqlDestination && !pgDestination && !bqDestination && !mongoDestination && !awsDestinationState && !databricksDestination && !restDestination) :
      type === 'MySQL' ? !!mysqlDestination :
      type === 'PostgreSQL' ? !!pgDestination :
      type === 'BigQuery' ? !!bqDestination :
      type === 'MongoDB' ? !!mongoDestination :
      type === 'Databricks' ? !!databricksDestination :
      type === 'REST API' ? !!restDestination :
      type === 'GA4' ? false : false
    return (
      <button key={type}
        onClick={() => {
          if (type === 'Google Sheets' && !gsheetsConnected) { setGsheetsPickerFor('dest'); setShowGSheetsPicker(true) }
          if (type === 'CSV') { setGsheetsConnected(null); setMysqlDest(null); setMongoDest(null) }
          if (type === 'MongoDB' as any && !mongoDestination) {
            const savedMongo = JSON.parse(localStorage.getItem('datapro_mongo_connections') || '[]')
            if (savedMongo.length > 0) {
              setMongoDest(savedMongo[0])
              localStorage.setItem('datapro_mongo_dest', JSON.stringify(savedMongo[0]))
            } else alert('No MongoDB connections saved. Add one in Data Sources first.')
          }
          if (type === 'MongoDB' as any && mongoDestination) {
            setMongoDest(null)
            localStorage.removeItem('datapro_mongo_dest')
          }
          if (type === 'AWS') {
            const savedAWS = JSON.parse(localStorage.getItem('datapro_aws_connections') || '[]')
            if (awsDestinationState) {
              setAwsDest(null)
            } else if (savedAWS.length > 0) {
              setAwsDest(savedAWS[0])
            } else {
              alert('No AWS connections saved. Add one in Data Sources first.')
            }
          }
          if (type === 'GCS') {
            const savedGCS = JSON.parse(localStorage.getItem('datapro_gcs_connections') || '[]')
            if (savedGCS.length > 0) {
              setDestinationName(`gcs://${savedGCS[0].bucket}/${savedGCS[0].files?.[0] || savedGCS[0].file || ''}`)
              setDestinationEdited(true)
            } else {
              alert('No Cloud Storage connections saved. Add one in Data Sources first.')
            }
          }
          if (type === 'Cloud SQL') {
            const savedCSQLConns = JSON.parse(localStorage.getItem('datapro_cloudsql_connections') || '[]')
            if (savedCSQLConns.length > 0) {
              const c = savedCSQLConns[0]
              setDestinationName(`${c.database}.${c.table || c.tables?.[0] || '?'}@cloudsql.${c.project}`)
              setDestinationEdited(true)
            } else {
              alert('No Cloud SQL connections saved. Add one in Data Sources first.')
            }
          }
          if (type === 'Databricks' && !databricksDestination) {
            const savedDB = JSON.parse(localStorage.getItem('datapro_databricks_connections') || '[]')
            if (savedDB.length > 0) {
              setDatabricksDest(savedDB[0])
            } else {
              alert('No Databricks connections saved. Add one in Data Sources first.')
            }
          }
          if (type === 'Databricks' && databricksDestination) {
            setDatabricksDest(null)
          }

          if (type === 'REST API' && !restDestination) {
            const savedRestConns = JSON.parse(localStorage.getItem('datapro_rest_connections') || '[]')
            if (savedRestConns.length > 0) {
              setRestDest({ ...savedRestConns[0], endpoint: savedRestConns[0].endpoints?.[0] || '/' })
            } else {
              alert('No REST API connections saved. Add one in Data Sources first.')
            }
          }
          if (type === 'REST API' && restDestination) {
            setRestDest(null)
          }
          if (type === 'GA4') {
            alert('GA4 is a source-only connector. Use CSV destination to export cleaned GA4 data.')
          }

          if (type === 'MySQL' && !mysqlDestination) {
            const saved = JSON.parse(localStorage.getItem('datapro_mysql_connections') || '[]')
            if (saved.length > 0) setMysqlDest(saved[0])
            else alert('No MySQL connections saved. Add one in Data Sources first.')
          }
          if (type === 'MySQL' && mysqlDestination) setMysqlDest(null)
          if (type === 'PostgreSQL' as any && !pgDestination) {
            const savedPG = JSON.parse(localStorage.getItem('datapro_postgres_connections') || '[]')
            if (savedPG.length > 0) {
              setPgDest(savedPG[0])
              localStorage.setItem('datapro_postgres_dest', JSON.stringify(savedPG[0]))
            } else alert('No PostgreSQL connections saved. Add one in Data Sources first.')
          }
          if (type === 'PostgreSQL' as any && pgDestination) {
            setPgDest(null)
            localStorage.removeItem('datapro_postgres_dest')
          }
          if (type === 'BigQuery' as any && !bqDestination) {
            const savedBQ = JSON.parse(localStorage.getItem('datapro_bigquery_connections') || '[]')
            if (savedBQ.length > 0) {
              setBqDest(savedBQ[0])
              localStorage.setItem('datapro_bigquery_dest', JSON.stringify(savedBQ[0]))
            } else alert('No BigQuery connections saved. Add one in Data Sources first.')
          }
          if (type === 'BigQuery' as any && bqDestination) {
            setBqDest(null)
            localStorage.removeItem('datapro_bigquery_dest')
          }
        }}
        className="flex-1 py-1.5 rounded-lg text-xs font-semibold transition-all"
        style={{
          background: isActive ? (type === 'Google Sheets' ? 'rgba(52,211,153,0.15)' : type === 'MySQL' ? 'rgba(234,179,8,0.15)' : type === 'PostgreSQL' ? 'rgba(99,102,241,0.15)' : type === 'BigQuery' ? 'rgba(234,88,12,0.15)' : type === 'MongoDB' ? 'rgba(34,197,94,0.15)' : type === 'GCS' ? 'rgba(66,133,244,0.15)' : type === 'Cloud SQL' ? 'rgba(52,168,83,0.15)' : type === 'Databricks' ? 'rgba(255,99,0,0.15)' : type === 'REST API' ? 'rgba(20,184,166,0.15)' : type === 'GA4' ? 'rgba(251,146,60,0.15)' : 'rgba(124,58,237,0.2)') : 'rgba(255,255,255,0.04)',
          border: `1px solid ${isActive ? (type === 'Google Sheets' ? 'rgba(52,211,153,0.4)' : type === 'MySQL' ? 'rgba(234,179,8,0.4)' : type === 'PostgreSQL' ? 'rgba(99,102,241,0.4)' : type === 'BigQuery' ? 'rgba(234,88,12,0.4)' : type === 'MongoDB' ? 'rgba(34,197,94,0.4)' : type === 'GCS' ? 'rgba(66,133,244,0.4)' : type === 'Cloud SQL' ? 'rgba(52,168,83,0.4)' : type === 'Databricks' ? 'rgba(255,99,0,0.4)' : type === 'REST API' ? 'rgba(20,184,166,0.4)' : type === 'GA4' ? 'rgba(251,146,60,0.4)' : 'rgba(124,58,237,0.5)') : 'rgba(255,255,255,0.08)'}`,
          color: isActive ? (type === 'Google Sheets' ? '#34d399' : type === 'MySQL' ? '#fbbf24' : type === 'PostgreSQL' ? '#818cf8' : type === 'BigQuery' ? '#fb923c' : type === 'MongoDB' ? '#4ade80' : type === 'GCS' ? '#4285F4' : type === 'Cloud SQL' ? '#34A853' : type === 'Databricks' ? '#FF6300' : type === 'REST API' ? '#14b8a6' : type === 'GA4' ? '#fb923c' : '#c4b5fd') : 'rgba(255,255,255,0.45)',
          cursor: 'pointer',
        }}>
        {type === 'MySQL' ? (
          <span className="flex items-center justify-center gap-1">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
              <ellipse cx="12" cy="5" rx="9" ry="3" fill="#f59e0b" fillOpacity="0.7"/>
              <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" stroke="#f59e0b" strokeWidth="1.5" fill="none"/>
              <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" stroke="#f59e0b" strokeWidth="1.5" fill="none"/>
            </svg>
            MySQL
          </span>
        ) : type === 'PostgreSQL' ? (
          <span className="flex items-center justify-center gap-1">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
              <ellipse cx="12" cy="5" rx="9" ry="3" fill="#6366f1" fillOpacity="0.7"/>
              <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" stroke="#6366f1" strokeWidth="1.5" fill="none"/>
              <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" stroke="#6366f1" strokeWidth="1.5" fill="none"/>
            </svg>
            PostgreSQL
          </span>
        ) : type === 'BigQuery' ? (
          <span className="flex items-center justify-center gap-1">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
              <path d="M12 2L22 19H2L12 2Z" fill="#ea580c" fillOpacity="0.7" stroke="#ea580c" strokeWidth="1.5" strokeLinejoin="round"/>
            </svg>
            BigQuery
          </span>
        ) : type === 'MongoDB' ? (
          <span className="flex items-center justify-center gap-1">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
              <ellipse cx="12" cy="5" rx="9" ry="3" fill="#22c55e" fillOpacity="0.7"/>
              <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" stroke="#22c55e" strokeWidth="1.5" fill="none"/>
              <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" stroke="#22c55e" strokeWidth="1.5" fill="none"/>
            </svg>
            MongoDB
          </span>
        ) : type === 'GCS' ? (
          <span className="flex items-center justify-center gap-1">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
              <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" stroke="#4285F4" strokeWidth="1.5" strokeLinejoin="round"/>
            </svg>
            Cloud Storage
          </span>
        ) : type === 'Cloud SQL' ? (
          <span className="flex items-center justify-center gap-1">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
              <ellipse cx="12" cy="5" rx="9" ry="3" fill="#34A853" fillOpacity="0.7"/>
              <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" stroke="#34A853" strokeWidth="1.5" fill="none"/>
              <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" stroke="#34A853" strokeWidth="1.5" fill="none"/>
            </svg>
            Cloud SQL
          </span>
        ) : type === 'Databricks' ? (
          <span className="flex items-center justify-center gap-1">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
              <path d="M12 2L2 8l10 6 10-6-10-6zM2 16l10 6 10-6M2 12l10 6 10-6" stroke="#FF6300" strokeWidth="1.5" strokeLinejoin="round"/>
            </svg>
            Databricks
          </span>
        ) : type === 'REST API' ? (
          <span className="flex items-center justify-center gap-1">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
              <circle cx="12" cy="12" r="9" stroke="#14b8a6" strokeWidth="1.5"/>
              <path d="M2 12h20M12 3a13 13 0 0 1 3 9 13 13 0 0 1-3 9 13 13 0 0 1-3-9 13 13 0 0 1 3-9z" stroke="#14b8a6" strokeWidth="1.5" fill="none"/>
            </svg>
            REST API
          </span>
        ) : type === 'GA4' ? (
          <span className="flex items-center justify-center gap-1">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
              <path d="M3 3v18h18" stroke="#fb923c" strokeWidth="1.5" strokeLinecap="round"/>
              <path d="M7 16l4-4 4 4 4-8" stroke="#fb923c" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
            GA4
          </span>
        ) : type}
      </button>
    )
  })}
</div>
{/* BigQuery destination connected pill + table selector */}
{bqDestination && (() => {
  const savedBQConns = JSON.parse(localStorage.getItem('datapro_bigquery_connections') || '[]')
  const allBqTables: string[] = bqDestination.tables?.length
    ? bqDestination.tables
    : [bqDestination.table].filter(Boolean)
  return (
    <div className="mb-2 space-y-2">
      <div className="flex items-center justify-between px-3 py-2 rounded-xl" style={{ background:'rgba(234,88,12,0.08)', border:'1px solid rgba(234,88,12,0.25)' }}>
        <div className="flex items-center gap-2 min-w-0 flex-1">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none">
            <path d="M12 2L22 19H2L12 2Z" fill="#ea580c" fillOpacity="0.7" stroke="#ea580c" strokeWidth="1.5" strokeLinejoin="round"/>
          </svg>
          <span className="text-xs font-bold text-white break-all">
            🔷 {bqDestination.project}.{bqDestination.dataset}.{bqDestination.table || bqDestination.tables?.[0] || '?'}
          </span>
        </div>
        <button onClick={() => { setBqDest(null); localStorage.removeItem('datapro_bigquery_dest') }} className="text-xs px-2 py-0.5 rounded-md flex-shrink-0 ml-2" style={{ color:'#f87171', background:'rgba(239,68,68,0.1)', border:'1px solid rgba(239,68,68,0.2)' }}>Disconnect</button>
      </div>
      {savedBQConns.length > 1 && (
        <div style={{ position: 'relative' }}>
          <select
            value={`${bqDestination.project}@${bqDestination.dataset}`}
            onChange={e => {
              const chosen = savedBQConns.find((c: any) => `${c.project}@${c.dataset}` === e.target.value)
              if (chosen) setBqDest(chosen)
            }}
            className="w-full text-xs text-white rounded-xl px-3 py-2 outline-none appearance-none"
            style={{ background:'rgba(234,88,12,0.08)', border:'1px solid rgba(234,88,12,0.3)', paddingRight:'28px', cursor:'pointer' }}
          >
            {savedBQConns.map((c: any) => (
              <option key={c.id} value={`${c.project}@${c.dataset}`} style={{ background:'#0d0a1a' }}>
                {c.project}.{c.dataset}
              </option>
            ))}
          </select>
          <div style={{ position:'absolute', right:'8px', top:'50%', transform:'translateY(-50%)', pointerEvents:'none' }}>
            <svg width="10" height="10" fill="none" stroke="rgba(234,88,12,0.7)" strokeWidth="2" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
          </div>
        </div>
      )}
      {allBqTables.length > 0 && (
        <div style={{ position: 'relative' }}>
          <select
            value={bqDestination.table || allBqTables[0]}
            onChange={e => setBqDest({ ...bqDestination, table: e.target.value })}
            className="w-full text-xs text-white rounded-xl px-3 py-2 outline-none appearance-none"
            style={{ background:'rgba(234,88,12,0.06)', border:'1px solid rgba(234,88,12,0.2)', paddingRight:'28px', cursor:'pointer' }}
          >
            {allBqTables.map((tbl: string) => (
              <option key={tbl} value={tbl} style={{ background:'#0d0a1a' }}>{tbl}</option>
            ))}
          </select>
          <div style={{ position:'absolute', right:'8px', top:'50%', transform:'translateY(-50%)', pointerEvents:'none' }}>
            <svg width="10" height="10" fill="none" stroke="rgba(234,88,12,0.7)" strokeWidth="2" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
          </div>
        </div>
      )}
    </div>
  )
})()}
{/* MySQL destination connected pill + table selector */}
              {mysqlDestination && (() => {
                const savedConns = JSON.parse(localStorage.getItem('datapro_mysql_connections') || '[]')
                const allMysqlTables: string[] = mysqlDestination.tables?.length
                  ? mysqlDestination.tables
                  : [mysqlDestination.table].filter(Boolean)
                return (
                  <div className="mb-2 space-y-2">
                    <div className="flex items-center justify-between px-3 py-2 rounded-xl" style={{ background:'rgba(234,179,8,0.08)', border:'1px solid rgba(234,179,8,0.25)' }}>
                      <div className="flex items-center gap-2 min-w-0 flex-1">
                        <svg width="13" height="13" fill="none" stroke="#fbbf24" strokeWidth="2" viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
                        <span className="text-xs font-bold text-white break-all">
  🗄️ {mysqlDestination.database}.{mysqlDestination.table || mysqlDestination.tables?.[0] || '?'} @ {mysqlDestination.host}
</span>
                      </div>
                      <button onClick={() => setMysqlDest(null)} className="text-xs px-2 py-0.5 rounded-md flex-shrink-0 ml-2" style={{ color:'#f87171', background:'rgba(239,68,68,0.1)', border:'1px solid rgba(239,68,68,0.2)' }}>Disconnect</button>
                    </div>
                    {/* Connection selector (if multiple saved connections) */}
                    {savedConns.length > 1 && (
                      <div style={{ position: 'relative' }}>
                        <select
                          value={`${mysqlDestination.database}@${mysqlDestination.host}`}
                          onChange={e => {
                            const chosen = savedConns.find((c: any) => `${c.database}@${c.host}` === e.target.value)
                            if (chosen) setMysqlDest(chosen)
                          }}
                          className="w-full text-xs text-white rounded-xl px-3 py-2 outline-none appearance-none"
                          style={{ background:'rgba(234,179,8,0.08)', border:'1px solid rgba(234,179,8,0.3)', paddingRight:'28px', cursor:'pointer' }}
                        >
                          {savedConns.map((c: any) => (
                            <option key={c.id} value={`${c.database}@${c.host}`} style={{ background:'#0d0a1a' }}>
                              {c.database}@{c.host}
                            </option>
                          ))}
                        </select>
                        <div style={{ position:'absolute', right:'8px', top:'50%', transform:'translateY(-50%)', pointerEvents:'none' }}>
                          <svg width="10" height="10" fill="none" stroke="rgba(251,191,36,0.7)" strokeWidth="2" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
                        </div>
                      </div>
                    )}
                    {/* Table selector */}
                    {allMysqlTables.length > 0 && (
                      <div style={{ position: 'relative' }}>
                        <select
                          value={mysqlDestination.table || allMysqlTables[0]}
                          onChange={e => setMysqlDest({ ...mysqlDestination, table: e.target.value })}
                          className="w-full text-xs text-white rounded-xl px-3 py-2 outline-none appearance-none"
                          style={{ background:'rgba(234,179,8,0.06)', border:'1px solid rgba(234,179,8,0.2)', paddingRight:'28px', cursor:'pointer' }}
                        >
                          {allMysqlTables.map((tbl: string) => (
  <option key={tbl} value={tbl} style={{ background:'#0d0a1a' }}>
    {tbl} 
  </option>
))}
                        </select>
                        <div style={{ position:'absolute', right:'8px', top:'50%', transform:'translateY(-50%)', pointerEvents:'none' }}>
                          <svg width="10" height="10" fill="none" stroke="rgba(251,191,36,0.7)" strokeWidth="2" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
                        </div>
                      </div>
                    )}
                  </div>
                )
              })()}

              {/* REST API destination connected pill */}
              {restDestination && (() => {
                const savedRestConns2 = JSON.parse(localStorage.getItem('datapro_rest_connections') || '[]')
                const allRestEndpoints: string[] = restDestination.endpoints?.length
                  ? restDestination.endpoints
                  : [restDestination.endpoint].filter(Boolean)
                return (
                  <div className="mb-2 space-y-2">
                    <div className="flex items-center justify-between px-3 py-2 rounded-xl"
                      style={{ background:'rgba(20,184,166,0.08)', border:'1px solid rgba(20,184,166,0.25)' }}>
                      <div className="flex items-center gap-2 min-w-0 flex-1">
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none">
                          <circle cx="12" cy="12" r="10" stroke="#14b8a6" strokeWidth="1.5"/>
                          <path d="M2 12h20M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" stroke="#14b8a6" strokeWidth="1.5" fill="none"/>
                        </svg>
                        <span className="text-xs font-bold text-white break-all">
                          🌐 {(restDestination.base_url || '').replace(/https?:\/\//, '')}{restDestination.endpoint || restDestination.endpoints?.[0] || '/'}
                        </span>
                      </div>
                      <button onClick={() => setRestDest(null)}
                        className="text-xs px-2 py-0.5 rounded-md flex-shrink-0 ml-2"
                        style={{ color:'#f87171', background:'rgba(239,68,68,0.1)', border:'1px solid rgba(239,68,68,0.2)' }}>
                        Disconnect
                      </button>
                    </div>
                    {allRestEndpoints.length > 1 && (
                      <div style={{ position: 'relative' }}>
                        <select
                          value={restDestination.endpoint || allRestEndpoints[0]}
                          onChange={e => setRestDest({ ...restDestination, endpoint: e.target.value })}
                          className="w-full text-xs text-white rounded-xl px-3 py-2 outline-none appearance-none"
                          style={{ background:'rgba(20,184,166,0.06)', border:'1px solid rgba(20,184,166,0.2)', paddingRight:'28px', cursor:'pointer' }}>
                          {allRestEndpoints.map((ep: string) => (
                            <option key={ep} value={ep} style={{ background:'#0d0a1a' }}>{ep}</option>
                          ))}
                        </select>
                        <div style={{ position:'absolute', right:'8px', top:'50%', transform:'translateY(-50%)', pointerEvents:'none' }}>
                          <svg width="10" height="10" fill="none" stroke="rgba(20,184,166,0.7)" strokeWidth="2" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
                        </div>
                      </div>
                    )}
                    {savedRestConns2.length > 1 && (
                      <div style={{ position: 'relative' }}>
                        <select
                          value={restDestination.base_url}
                          onChange={e => {
                            const chosen = savedRestConns2.find((c: any) => c.base_url === e.target.value)
                            if (chosen) setRestDest({ ...chosen, endpoint: chosen.endpoints?.[0] || '/' })
                          }}
                          className="w-full text-xs text-white rounded-xl px-3 py-2 outline-none appearance-none"
                          style={{ background:'rgba(20,184,166,0.08)', border:'1px solid rgba(20,184,166,0.3)', paddingRight:'28px', cursor:'pointer' }}>
                          {savedRestConns2.map((c: any) => (
                            <option key={c.id} value={c.base_url} style={{ background:'#0d0a1a' }}>
                              {(c.base_url || '').replace(/https?:\/\//, '')}
                            </option>
                          ))}
                        </select>
                        <div style={{ position:'absolute', right:'8px', top:'50%', transform:'translateY(-50%)', pointerEvents:'none' }}>
                          <svg width="10" height="10" fill="none" stroke="rgba(20,184,166,0.7)" strokeWidth="2" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
                        </div>
                      </div>
                    )}
                  </div>
                )
              })()}

{/* PostgreSQL destination connected pill */}
              {pgDestination && (() => {
                const savedPGConns = JSON.parse(localStorage.getItem('datapro_postgres_connections') || '[]')
                const allPgTables: string[] = pgDestination.tables?.length
                  ? pgDestination.tables
                  : [pgDestination.table].filter(Boolean)
                return (
                  <div className="mb-2 space-y-2">
                    <div className="flex items-center justify-between px-3 py-2 rounded-xl" style={{ background:'rgba(99,102,241,0.08)', border:'1px solid rgba(99,102,241,0.25)' }}>
                      <div className="flex items-center gap-2 min-w-0 flex-1">
                        <svg width="13" height="13" fill="none" stroke="#818cf8" strokeWidth="2" viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
                        <span className="text-xs font-bold text-white break-all">
                          🐘 {pgDestination.database}.{pgDestination.table || pgDestination.tables?.[0] || '?'} @ {pgDestination.host}
                        </span>
                      </div>
                      <button onClick={() => { setPgDest(null) }} className="text-xs px-2 py-0.5 rounded-md flex-shrink-0 ml-2" style={{ color:'#f87171', background:'rgba(239,68,68,0.1)', border:'1px solid rgba(239,68,68,0.2)' }}>Disconnect</button>
                    </div>
                    {savedPGConns.length > 1 && (
                      <div style={{ position: 'relative' }}>
                        <select
                          value={`${pgDestination.database}@${pgDestination.host}`}
                          onChange={e => {
                            const chosen = savedPGConns.find((c: any) => `${c.database}@${c.host}` === e.target.value)
                            if (chosen) setPgDest(chosen)
                          }}
                          className="w-full text-xs text-white rounded-xl px-3 py-2 outline-none appearance-none"
                          style={{ background:'rgba(99,102,241,0.08)', border:'1px solid rgba(99,102,241,0.3)', paddingRight:'28px', cursor:'pointer' }}
                        >
                          {savedPGConns.map((c: any) => (
                            <option key={c.id} value={`${c.database}@${c.host}`} style={{ background:'#0d0a1a' }}>
                              {c.database}@{c.host}
                            </option>
                          ))}
                        </select>
                        <div style={{ position:'absolute', right:'8px', top:'50%', transform:'translateY(-50%)', pointerEvents:'none' }}>
                          <svg width="10" height="10" fill="none" stroke="rgba(129,140,248,0.7)" strokeWidth="2" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
                        </div>
                      </div>
                    )}
                    {allPgTables.length > 0 && (
                      <div style={{ position: 'relative' }}>
                        <select
                          value={pgDestination.table || allPgTables[0]}
                          onChange={e => setPgDest({ ...pgDestination, table: e.target.value })}
                          className="w-full text-xs text-white rounded-xl px-3 py-2 outline-none appearance-none"
                          style={{ background:'rgba(99,102,241,0.06)', border:'1px solid rgba(99,102,241,0.2)', paddingRight:'28px', cursor:'pointer' }}
                        >
                          {allPgTables.map((tbl: string) => (
                            <option key={tbl} value={tbl} style={{ background:'#0d0a1a' }}>{tbl}</option>
                          ))}
                        </select>
                        <div style={{ position:'absolute', right:'8px', top:'50%', transform:'translateY(-50%)', pointerEvents:'none' }}>
                          <svg width="10" height="10" fill="none" stroke="rgba(129,140,248,0.7)" strokeWidth="2" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
                        </div>
                      </div>
                    )}
                  </div>
                )
              })()}

              {/* MongoDB destination connected pill + collection selector */}
              {mongoDestination && (() => {
                const savedMongoConns2 = JSON.parse(localStorage.getItem('datapro_mongo_connections') || '[]')
                const allMongoColls: string[] = mongoDestination.collections?.length
                  ? mongoDestination.collections
                  : [mongoDestination.collection].filter(Boolean)
                return (
                  <div className="mb-2 space-y-2">
                    <div className="flex items-center justify-between px-3 py-2 rounded-xl" style={{ background:'rgba(34,197,94,0.08)', border:'1px solid rgba(34,197,94,0.25)' }}>
                      <div className="flex items-center gap-2 min-w-0 flex-1">
                        <svg width="13" height="13" fill="none" stroke="#4ade80" strokeWidth="2" viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
                        <span className="text-xs font-bold text-white break-all">
                          🍃 {mongoDestination.database}.{mongoDestination.collection || mongoDestination.collections?.[0] || '?'}
                        </span>
                      </div>
                      <button onClick={() => { setMongoDest(null); localStorage.removeItem('datapro_mongo_dest') }} className="text-xs px-2 py-0.5 rounded-md flex-shrink-0 ml-2" style={{ color:'#f87171', background:'rgba(239,68,68,0.1)', border:'1px solid rgba(239,68,68,0.2)' }}>Disconnect</button>
                    </div>
                    {savedMongoConns2.length > 1 && (
                      <div style={{ position: 'relative' }}>
                        <select
                          value={mongoDestination.database}
                          onChange={e => {
                            const chosen = savedMongoConns2.find((c: any) => c.database === e.target.value)
                            if (chosen) setMongoDest(chosen)
                          }}
                          className="w-full text-xs text-white rounded-xl px-3 py-2 outline-none appearance-none"
                          style={{ background:'rgba(34,197,94,0.08)', border:'1px solid rgba(34,197,94,0.3)', paddingRight:'28px', cursor:'pointer' }}
                        >
                          {savedMongoConns2.map((c: any) => (
                            <option key={c.id} value={c.database} style={{ background:'#0d0a1a' }}>{c.database}</option>
                          ))}
                        </select>
                        <div style={{ position:'absolute', right:'8px', top:'50%', transform:'translateY(-50%)', pointerEvents:'none' }}>
                          <svg width="10" height="10" fill="none" stroke="rgba(34,197,94,0.7)" strokeWidth="2" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
                        </div>
                      </div>
                    )}
                    {allMongoColls.length > 0 && (
                      <div style={{ position: 'relative' }}>
                        <select
                          value={mongoDestination.collection || allMongoColls[0]}
                          onChange={e => setMongoDest({ ...mongoDestination, collection: e.target.value })}
                          className="w-full text-xs text-white rounded-xl px-3 py-2 outline-none appearance-none"
                          style={{ background:'rgba(34,197,94,0.06)', border:'1px solid rgba(34,197,94,0.2)', paddingRight:'28px', cursor:'pointer' }}
                        >
                          {allMongoColls.map((coll: string) => (
                            <option key={coll} value={coll} style={{ background:'#0d0a1a' }}>{coll}</option>
                          ))}
                        </select>
                        <div style={{ position:'absolute', right:'8px', top:'50%', transform:'translateY(-50%)', pointerEvents:'none' }}>
                          <svg width="10" height="10" fill="none" stroke="rgba(34,197,94,0.7)" strokeWidth="2" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
                        </div>
                      </div>
                    )}
                  </div>
                )
              })()}

{/* AWS destination connected pill */}
              {awsDestinationState && (
                <div className="mb-2 space-y-2">
                  <div className="flex items-center justify-between px-3 py-2 rounded-xl" style={{ background:'rgba(255,153,0,0.08)', border:'1px solid rgba(255,153,0,0.25)' }}>
                    <div className="flex items-center gap-2 min-w-0 flex-1">
                      <svg width="13" height="13" viewBox="0 0 24 24" fill="none">
                        <path d="M6.5 20C4.01 20 2 17.99 2 15.5C2 13.01 4.01 11 6.5 11C6.5 8.24 8.74 6 11.5 6C13.85 6 15.82 7.57 16.4 9.74C16.6 9.72 16.8 9.7 17 9.7C19.21 9.7 21 11.49 21 13.7C21 15.91 19.21 17.7 17 17.7H16.5" stroke="#FF9900" strokeWidth="1.5" strokeLinecap="round"/>
                      </svg>
                      <span className="text-xs font-bold text-white break-all">
                        ☁️ {awsDestinationState.service?.toUpperCase()} · {awsDestinationState.service === 's3' ? awsDestinationState.bucket : awsDestinationState.database || awsDestinationState.tableName} · {awsDestinationState.region}
                      </span>
                    </div>
                    <button onClick={() => setAwsDest(null)} className="text-xs px-2 py-0.5 rounded-md flex-shrink-0 ml-2" style={{ color:'#f87171', background:'rgba(239,68,68,0.1)', border:'1px solid rgba(239,68,68,0.2)' }}>Disconnect</button>
                  </div>
                </div>
              )}

{/* Databricks destination connected pill + table selector */}
              {databricksDestination && (() => {
                const savedDBConns = JSON.parse(localStorage.getItem('datapro_databricks_connections') || '[]')
                const allDBTables: string[] = databricksDestination.tables?.length
                  ? databricksDestination.tables
                  : [databricksDestination.table].filter(Boolean)
                return (
                  <div className="mb-2 space-y-2">
                    <div className="flex items-center justify-between px-3 py-2 rounded-xl" style={{ background:'rgba(255,99,0,0.08)', border:'1px solid rgba(255,99,0,0.25)' }}>
                      <div className="flex items-center gap-2 min-w-0 flex-1">
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none">
                          <path d="M12 2L2 8l10 6 10-6-10-6zM2 16l10 6 10-6M2 12l10 6 10-6" stroke="#FF6300" strokeWidth="1.5" strokeLinejoin="round"/>
                        </svg>
                        <span className="text-xs font-bold text-white break-all">
                          ⚡ {databricksDestination.catalog || 'hive_metastore'}.{databricksDestination.schema || databricksDestination.database || 'default'}.{databricksDestination.table || databricksDestination.tables?.[0] || '?'}
                        </span>
                      </div>
                      <button onClick={() => setDatabricksDest(null)} className="text-xs px-2 py-0.5 rounded-md flex-shrink-0 ml-2" style={{ color:'#f87171', background:'rgba(239,68,68,0.1)', border:'1px solid rgba(239,68,68,0.2)' }}>Disconnect</button>
                    </div>
                    {savedDBConns.length > 1 && (
                      <div style={{ position: 'relative' }}>
                        <select
                          value={`${databricksDestination.host}`}
                          onChange={e => {
                            const chosen = savedDBConns.find((c: any) => c.host === e.target.value)
                            if (chosen) setDatabricksDest(chosen)
                          }}
                          className="w-full text-xs text-white rounded-xl px-3 py-2 outline-none appearance-none"
                          style={{ background:'rgba(255,99,0,0.08)', border:'1px solid rgba(255,99,0,0.3)', paddingRight:'28px', cursor:'pointer' }}
                        >
                          {savedDBConns.map((c: any) => (
                            <option key={c.id} value={c.host} style={{ background:'#0d0a1a' }}>
                              {c.host}
                            </option>
                          ))}
                        </select>
                        <div style={{ position:'absolute', right:'8px', top:'50%', transform:'translateY(-50%)', pointerEvents:'none' }}>
                          <svg width="10" height="10" fill="none" stroke="rgba(255,99,0,0.7)" strokeWidth="2" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
                        </div>
                      </div>
                    )}
                    {allDBTables.length > 0 && (
                      <div style={{ position: 'relative' }}>
                        <select
                          value={databricksDestination.table || allDBTables[0]}
                          onChange={e => setDatabricksDest({ ...databricksDestination, table: e.target.value })}
                          className="w-full text-xs text-white rounded-xl px-3 py-2 outline-none appearance-none"
                          style={{ background:'rgba(255,99,0,0.06)', border:'1px solid rgba(255,99,0,0.2)', paddingRight:'28px', cursor:'pointer' }}
                        >
                          {allDBTables.map((tbl: string) => (
                            <option key={tbl} value={tbl} style={{ background:'#0d0a1a' }}>{tbl}</option>
                          ))}
                        </select>
                        <div style={{ position:'absolute', right:'8px', top:'50%', transform:'translateY(-50%)', pointerEvents:'none' }}>
                          <svg width="10" height="10" fill="none" stroke="rgba(255,99,0,0.7)" strokeWidth="2" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
                        </div>
                      </div>
                    )}
                  </div>
                )
              })()}

{/* Write mode toggle — always visible for all destination types */}
              <div className="flex gap-2 mt-2">
                {(['replace', 'append'] as const).map(mode => (
                  <button
                    key={mode}
                    onClick={() => _setDestinationMode(mode)}
                    className="flex-1 py-1.5 rounded-lg text-xs font-semibold transition-all"
                    style={{
                      background: destinationMode === mode ? 'rgba(124,58,237,0.25)' : 'rgba(255,255,255,0.04)',
                      border: `1px solid ${destinationMode === mode ? 'rgba(124,58,237,0.5)' : 'rgba(255,255,255,0.08)'}`,
                      color: destinationMode === mode ? '#c4b5fd' : 'rgba(255,255,255,0.45)',
                      cursor: 'pointer',
                    }}>
                    {mode === 'replace' ? '♻️ Replace existing' : '➕ Append to existing'}
                  </button>
                ))}
              </div>

              {/* Google Sheets connected pill OR connect button */}
              {gsheetsConnected ? (
                <div className="flex items-center justify-between mb-2 px-3 py-2 rounded-xl" style={{ background:'rgba(52,211,153,0.08)', border:'1px solid rgba(52,211,153,0.25)' }}>
                  <div className="flex items-center gap-2">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none">
                      <rect x="3" y="2" width="18" height="20" rx="2" fill="#34A853" fillOpacity="0.3" stroke="#34A853" strokeWidth="1.5"/>
                    </svg>
                    <span className="text-xs font-bold text-white truncate" style={{ maxWidth: '140px' }}>{gsheetsConnected.sheetName}</span>
                    <span className="text-xs" style={{ color:'rgba(255,255,255,0.4)' }}>· {gsheetsConnected.email}</span>
                  </div>
                  <button
                    onClick={() => { setGsheetsConnected(null); localStorage.removeItem('datapro_gsheets_dest') }}
                    className="text-xs px-2 py-0.5 rounded-md flex-shrink-0"
                    style={{ color:'#f87171', background:'rgba(239,68,68,0.1)', border:'1px solid rgba(239,68,68,0.2)' }}
                  >
                    Disconnect
                  </button>
                </div>
                ) : null}
              {/* Combobox with ✕ clear button — only show for CSV destination */}
              {!gsheetsConnected && !mysqlDestination && !pgDestination && (
                <DestinationSelect
                  value={destinationName}
                  onChange={v => { setDestinationName(v); setDestinationEdited(true) }}
                  onClear={() => setDestinationEdited(true)}
                  sources={sources}
                  savedPipelines={savedPipelines}
                />
              )}

              {/* Auto-overwrite note */}
              <p className="text-xs mt-2" style={{ color:'rgba(255,255,255,0.25)' }}>
                {destinationMode === 'replace'
                  ? '♻️ Each run overwrites the destination with the latest cleaned data'
                  : '➕ Each run appends new cleaned rows to the existing destination'}
              </p>
            </div>
          </div>

          {existingPipeline && (
            <div className="flex items-center justify-between px-4 py-3 rounded-xl" style={{ background:'rgba(124,58,237,0.08)', border:'1px solid rgba(124,58,237,0.2)' }}>
              <div className="flex items-center gap-3">
                <div className="w-2 h-2 rounded-full animate-pulse" style={{ background:'#a78bfa' }} />
                <div>
                  <p className="text-sm font-bold text-white">{existingPipeline.name}</p>
                  <p className="text-xs" style={{ color:'rgba(255,255,255,0.4)' }}>
                    {existingPipeline.runCount} runs · last: {existingPipeline.lastRun ? fmtRelative(existingPipeline.lastRun) : 'never'}
                    {existingPipeline.scheduleLabel && (
                      <span style={{ color:'#a78bfa' }}>
                        {' '}· ⏱ {existingPipeline.scheduleLabel}
                        {existingPipeline.scheduleTimezone && ` (${existingPipeline.scheduleTimezone})`}
                        {existingPipeline.scheduleStartDate && ` · from ${existingPipeline.scheduleStartDate}`}
                      </span>
                    )}
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-2">
                <button onClick={() => { setSelectedPipelineForLogs(existingPipeline); setShowLogsPanel(true) }} className="text-xs px-3 py-1.5 rounded-lg font-semibold" style={{ background:'rgba(99,102,241,0.12)', border:'1px solid rgba(99,102,241,0.25)', color:'#818cf8' }}>View Runs</button>
                <button onClick={() => setShowScheduleModal(existingPipeline)} className="text-xs px-3 py-1.5 rounded-lg font-semibold" style={{ background:'rgba(124,58,237,0.12)', border:'1px solid rgba(124,58,237,0.25)', color:'#a78bfa' }}>⏱ Schedule</button>
                <button onClick={() => runPipeline(existingPipeline, 'manual')} className="text-xs px-3 py-1.5 rounded-lg font-semibold text-white" style={{ background:'rgba(124,58,237,0.2)', border:'1px solid rgba(124,58,237,0.3)' }}>▶ Run Now</button>
              </div>
            </div>
          )}
        </div>

        {/* PIPELINE FLOW DIAGRAM */}
        <div className="rounded-2xl p-6 overflow-x-auto" style={{ background:'rgba(255,255,255,0.02)', border:'1px solid rgba(255,255,255,0.06)' }}>
          <p className="text-xs font-semibold mb-5 tracking-widest uppercase" style={{ color:'rgba(255,255,255,0.25)' }}>Pipeline Flow</p>
          <div className="flex items-center gap-3 min-w-max">
            <div className="rounded-2xl p-4 flex-shrink-0" style={{ background:'rgba(124,58,237,0.08)', border:'1px solid rgba(124,58,237,0.25)', minWidth:'200px' }}>
              <div className="flex items-center gap-2 mb-3">
                <div className="w-6 h-6 rounded-lg flex items-center justify-center" style={{ background:'rgba(124,58,237,0.3)' }}>
                  <svg width="11" height="11" fill="none" stroke="#a78bfa" strokeWidth="2" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                </div>
                <span className="text-xs font-bold tracking-widest uppercase" style={{ color:'#a78bfa' }}>Source</span>
              </div>
              <p className="font-bold text-white text-sm break-all">{activeSource?.name || '—'}</p>
              <p className="text-xs mt-1" style={{ color:'rgba(255,255,255,0.35)' }}>{activeSource?.type?.toUpperCase()} · {activeSource?.addedAt}</p>
              <div className="flex items-center gap-2 mt-3 flex-wrap">
  <span className="text-xs px-2 py-0.5 rounded-full font-medium" style={{ background:'rgba(34,197,94,0.12)', color:'#4ade80' }}>● Connected</span>
  <button onClick={() => navigate('/analysis')} className="text-xs font-semibold" style={{ color:'#a78bfa' }}>Analyze →</button>
  <button onClick={() => setShowOriginalModal(true)} className="text-xs font-semibold" style={{ color:'#34d399' }}>View Original</button>
</div>
            </div>

            {transformations.length === 0 ? (
              <>
                <div className="flex items-center gap-1 flex-shrink-0"><div className="h-px w-8" style={{ background:'rgba(255,255,255,0.1)' }} /><div className="w-2 h-2 rotate-45 border-t border-r" style={{ borderColor:'rgba(255,255,255,0.15)' }} /></div>
                <div className="rounded-2xl p-4 flex-shrink-0 cursor-pointer transition-all" onClick={() => setShowTransformPicker(true)}
                  style={{ background:'rgba(255,255,255,0.02)', border:'1px dashed rgba(255,255,255,0.1)', minWidth:'180px' }}
                  onMouseEnter={e => (e.currentTarget.style.borderColor='rgba(124,58,237,0.4)')}
                  onMouseLeave={e => (e.currentTarget.style.borderColor='rgba(255,255,255,0.1)')}>
                  <p className="text-xs font-bold tracking-widest uppercase mb-2" style={{ color:'rgba(255,255,255,0.2)' }}>Transform</p>
                  <p className="text-xs mb-3" style={{ color:'rgba(255,255,255,0.2)' }}>No transforms added</p>
                  <button className="text-xs font-semibold px-3 py-1.5 rounded-lg" style={{ background:'rgba(124,58,237,0.2)', color:'#a78bfa' }}>+ Add</button>
                </div>
              </>
            ) : (
              transformations.map((t, idx) => {
                const meta=TRANSFORMATIONS.find(x => x.id===t.id); const color=meta?.color||'#7c3aed'
                return (
                  <div key={t.id} className="flex items-center gap-3 flex-shrink-0">
                    <div className="flex items-center gap-1"><div className="h-px w-8" style={{ background:`${color}40` }} /><div className="w-2 h-2 rotate-45 border-t border-r" style={{ borderColor:`${color}60` }} /></div>
                    <div className="rounded-2xl p-4 flex-shrink-0 relative group" style={{ background:`${color}10`, border:`1px solid ${color}30`, minWidth:'170px' }}>
                      <button onClick={() => removeT(t.id)} className="absolute top-2 right-2 w-5 h-5 rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-all" style={{ background:'rgba(239,68,68,0.2)', color:'#f87171', fontSize:'9px', border:'1px solid rgba(239,68,68,0.3)' }}>✕</button>
                      <div className="flex items-center gap-2 mb-2"><span className="text-base">{t.icon}</span><span className="text-xs font-bold tracking-widest uppercase" style={{ color }}>Step {idx+1}</span></div>
                      <p className="font-bold text-white text-sm">{t.label}</p>
                      <p className="text-xs mt-0.5" style={{ color:'rgba(255,255,255,0.35)' }}>{t.desc}</p>
                    </div>
                  </div>
                )
              })
            )}

            {/* Inline add-transform button */}
            <div className="flex items-center gap-2 flex-shrink-0">
              <div className="flex items-center gap-1"><div className="h-px w-6" style={{ background:'rgba(124,58,237,0.25)' }} /></div>
              <div
                onClick={() => setShowTransformPicker(true)}
                className="rounded-xl flex items-center justify-center cursor-pointer transition-all"
                style={{ width:'40px', height:'40px', background:'rgba(124,58,237,0.08)', border:'2px dashed rgba(124,58,237,0.3)', flexShrink:0 }}
                onMouseEnter={e => (e.currentTarget.style.background='rgba(124,58,237,0.2)')}
                onMouseLeave={e => (e.currentTarget.style.background='rgba(124,58,237,0.08)')}
                title="Add transformation"
              >
                <svg width="16" height="16" fill="none" stroke="#a78bfa" strokeWidth="2.5" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>
              </div>
              <div className="flex items-center gap-1"><div className="h-px w-6" style={{ background:'rgba(52,211,153,0.25)' }} /><div className="w-2 h-2 rotate-45 border-t border-r" style={{ borderColor:'rgba(52,211,153,0.4)' }} /></div>
            </div>

            <div className="rounded-2xl p-4 flex-shrink-0" style={{ background:'rgba(52,211,153,0.06)', border:'1px solid rgba(52,211,153,0.2)', minWidth:'220px' }}>
              <div className="flex items-center gap-2 mb-3">
                <div className="w-6 h-6 rounded-lg flex items-center justify-center" style={{ background:'rgba(52,211,153,0.2)' }}>
                  <svg width="11" height="11" fill="none" stroke="#34d399" strokeWidth="2" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                </div>
                <span className="text-xs font-bold tracking-widest uppercase" style={{ color:'#34d399' }}>Destination</span>
              </div>
              <p className="font-bold text-white text-sm break-all">{destination || '—'}</p>
              <p className="text-xs mt-1" style={{ color:'rgba(255,255,255,0.35)' }}>
                {gsheetsConnected ? '📊 Google Sheets' : 'Cleaned output'} · {destinationMode === 'replace' ? '♻️ Replace' : '➕ Append'}
              </p>
              {existingPipeline && (() => {
                const latestLog = getLatestLog(existingPipeline)
                return latestLog ? (
                  <div className="mt-3 space-y-2">
                    <div className="flex items-center gap-2"><StatusBadge status={latestLog.status} /><span className="text-xs" style={{ color:'rgba(255,255,255,0.35)' }}>{fmtRelative(latestLog.timestamp)}</span></div>
                    {latestLog.status==='success' && <p className="text-xs" style={{ color:'rgba(255,255,255,0.4)' }}>{latestLog.rowsIn.toLocaleString()} → {latestLog.rowsOut.toLocaleString()} rows · {fmtDur(latestLog.duration)}</p>}
                    {latestLog.status==='failed'  && <p className="text-xs" style={{ color:'#f87171' }}>{latestLog.steps.find(s => s.status==='failed')?.message||'Run failed'}</p>}
                    <button onClick={() => { setSelectedPipelineForLogs(existingPipeline); setShowLogsPanel(true) }}
                      className="w-full flex items-center justify-center gap-1.5 py-1.5 rounded-lg text-xs font-semibold transition-all"
                      style={{ background:latestLog.status==='failed'?'rgba(239,68,68,0.12)':'rgba(99,102,241,0.12)', border:`1px solid ${latestLog.status==='failed'?'rgba(239,68,68,0.25)':'rgba(99,102,241,0.25)'}`, color:latestLog.status==='failed'?'#f87171':'#818cf8' }}>
                      <svg width="10" height="10" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>View Run Logs
                    </button>
                  </div>
                ) : <span className="inline-block mt-3 text-xs px-2 py-0.5 rounded-full font-medium" style={{ background:'rgba(52,211,153,0.1)', color:'#34d399' }}>Pipeline active · no runs yet</span>
              })()}
              {/* {!existingPipeline && <span className="inline-block mt-3 text-xs px-2 py-0.5 rounded-full font-medium" style={{ background:'rgba(52,211,153,0.1)', color:'#34d399' }}>Ready to save</span>} */}
              {lastRunResult && !gsheetsConnected && (
                <button
                  onClick={async () => {
  const outputFile = useStore.getState().pipelineOutputFile
  const fileToDownload = outputFile?.name === destinationName ? outputFile : null
  if (fileToDownload) {
    const url = URL.createObjectURL(fileToDownload)
    const a = document.createElement('a')
    a.href = url
    a.download = destinationName || 'cleaned_output.csv'
    a.click()
    URL.revokeObjectURL(url)
    return
  }
  if (!activeFile) return
  const formData = new FormData()
  formData.append('file', activeFile)
  transformations.forEach(t => { const k = TRANSFORM_TO_CLEAN[t.id]; if (k) formData.append(k, 'true') })
  formData.append('write_mode', destinationMode)
  try {
    const res = await fetch('/api/clean-data', { method: 'POST', body: formData })
    const data = await res.json()
    if (data.download_url) {
      const fileRes = await fetch(`${import.meta.env.VITE_API_URL}${data.download_url}`)
      const blob = await fileRes.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = destinationName || 'cleaned_output.csv'
      a.click()
      URL.revokeObjectURL(url)
    }
  } catch (e) { console.error('Download failed', e) }
}}
                  className="w-full flex items-center justify-center gap-2 mt-3 py-2 rounded-xl text-xs font-bold text-white"
                  style={{ background:'linear-gradient(135deg, #7c3aed, #2563eb)' }}
                >
                  <svg width="12" height="12" fill="none" stroke="white" strokeWidth="2" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                  Download CSV
                </button>
              )}
              
            </div>
          </div>

          {transformations.length > 0 && (
            <div className="mt-6 pt-6 border-t flex items-center justify-between gap-4 flex-wrap" style={{ borderColor:'rgba(255,255,255,0.07)' }}>
              <div>
                <p className="text-sm font-bold text-white">{existingPipeline ? `Update pipeline: ${existingPipeline.name}` : 'Save this pipeline'}</p>
                <p className="text-xs mt-0.5" style={{ color:'rgba(255,255,255,0.35)' }}>Saved pipelines are unique to this source ↔ destination pair · You'll be redirected to Analysis after saving</p>
              </div>
              <div className="flex items-center gap-3">
                <button onClick={() => setShowSaveModal(true)} disabled={!effectiveDestination.trim()}
                  className="flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-bold text-white disabled:opacity-40"
                  style={{ background:'linear-gradient(135deg, #7c3aed, #2563eb)', boxShadow:'0 4px 20px rgba(124,58,237,0.35)' }}>
                  <svg width="14" height="14" fill="none" stroke="white" strokeWidth="2" viewBox="0 0 24 24"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
                  {existingPipeline ? 'Update Pipeline' : 'Save Pipeline'}
                </button>
                <button onClick={() => { transformations.forEach(t => removeT(t.id)) }} className="px-4 py-2.5 rounded-xl text-sm font-semibold" style={{ background:'rgba(239,68,68,0.08)', border:'1px solid rgba(239,68,68,0.2)', color:'#f87171' }}>Clear All</button>
              </div>
            </div>
          )}
        </div>

        {/* SAVED PIPELINES LIST */}
        {savedPipelines.length > 0 && (
          <div id="saved-pipelines-section" className="rounded-2xl overflow-hidden" style={{ background:'rgba(255,255,255,0.02)', border:'1px solid rgba(255,255,255,0.06)' }}>
            <div className="px-6 py-4 flex items-center justify-between" style={{ borderBottom:'1px solid rgba(255,255,255,0.06)', background:'rgba(255,255,255,0.01)' }}>
              <p className="text-sm font-bold text-white">Saved Pipelines</p>
              <span className="text-xs px-2.5 py-1 rounded-full" style={{ background:'rgba(255,255,255,0.06)', color:'rgba(255,255,255,0.4)' }}>{savedPipelines.length} pipeline{savedPipelines.length!==1?'s':''}</span>
            </div>
            <div className="divide-y" style={{ borderColor:'rgba(255,255,255,0.05)' }}>
              {savedPipelines.map(pipeline => {
                const successLogs=pipeline.logs.filter(l => l.status==='success').length
                const totalLogs=pipeline.logs.length
                const successRate=totalLogs>0?Math.round(successLogs/totalLogs*100):100
                const isRunningNow=pipeline.isActive&&!!scheduledTimers[pipeline.id]
                const latestLog=getLatestLog(pipeline)
                return (
                  <div key={pipeline.id} className="px-6 py-4 hover:bg-white/[0.01] transition-colors">
                    <div className="flex items-start justify-between gap-4">
                      <div className="flex items-start gap-4 flex-1 min-w-0">
                        <div className="mt-1 flex-shrink-0"><div className={`w-3 h-3 rounded-full ${isRunningNow?'animate-pulse':''}`} style={{ background:isRunningNow?'#22c55e':pipeline.lastRun?'#a78bfa':'rgba(255,255,255,0.2)' }} /></div>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2 flex-wrap mb-1">
                            <p className="font-bold text-white text-sm">{pipeline.name}</p>
                            {isRunningNow && <span className="text-xs px-2 py-0.5 rounded-full font-bold" style={{ background:'rgba(34,197,94,0.12)', color:'#4ade80' }}>⏱ {pipeline.scheduleLabel}{pipeline.scheduleTimezone&&pipeline.scheduleTimezone!=='UTC'&&<span style={{ opacity:0.7 }}> · {pipeline.scheduleTimezone}</span>}</span>}
                            {!isRunningNow&&pipeline.scheduleLabel&&<span className="text-xs px-2 py-0.5 rounded-full" style={{ background:'rgba(234,179,8,0.1)', color:'#fbbf24' }}>⏸ {pipeline.scheduleLabel}{pipeline.scheduleTimezone&&pipeline.scheduleTimezone!=='UTC'&&<span style={{ opacity:0.7 }}> · {pipeline.scheduleTimezone}</span>}</span>}
                            {pipeline.scheduleStartDate&&<span className="text-xs px-2 py-0.5 rounded-full" style={{ background:'rgba(99,102,241,0.1)', color:'#818cf8' }}>📅 from {pipeline.scheduleStartDate}</span>}
                            {latestLog&&<StatusBadge status={latestLog.status} />}
                          </div>
                          <p className="text-xs mb-2" style={{ color:'rgba(255,255,255,0.4)' }}><span style={{ color:'#a78bfa' }}>{pipeline.sourceName}</span><span style={{ color:'rgba(255,255,255,0.25)' }}> → </span><span style={{ color:'#34d399' }}>{pipeline.destinationName}</span></p>
                          <div className="flex items-center gap-4 flex-wrap">
                            <div className="flex gap-1.5 flex-wrap items-center">
  {pipeline.transformations.map(t => (
    <span key={t.id} className="text-xs px-2 py-0.5 rounded-md font-medium flex items-center gap-1" style={{ background:'rgba(255,255,255,0.05)', color:'rgba(255,255,255,0.5)' }}>
      {t.icon} {t.label}
    </span>
  ))}
  {latestLog && latestLog.status === 'success' && (
    <span className="text-xs px-2 py-0.5 rounded-md" style={{ background:'rgba(34,197,94,0.1)', color:'#4ade80' }}>
      {latestLog.rowsIn.toLocaleString()} → {latestLog.rowsOut.toLocaleString()} rows · {fmtDur(latestLog.duration)}
    </span>
  )}
  {latestLog && latestLog.status === 'failed' && (
    <span className="text-xs px-2 py-0.5 rounded-md" style={{ background:'rgba(239,68,68,0.1)', color:'#f87171' }}>
      ✕ {latestLog.steps.find(s => s.status==='failed')?.message || 'Failed'}
    </span>
  )}
</div>
                            <div className="flex items-center gap-3 text-xs" style={{ color:'rgba(255,255,255,0.3)' }}>
                              <span>{pipeline.runCount} runs</span>
                              {totalLogs>0&&<span style={{ color:successRate>80?'#4ade80':'#f87171' }}>{successRate}% success</span>}
                              {pipeline.lastRun&&<span>last: {fmtRelative(pipeline.lastRun)}</span>}
                            </div>
                          </div>
                        </div>
                      </div>
                      <div className="flex items-center gap-2 flex-shrink-0 flex-wrap justify-end">
                        {isRunningNow?(
                          <button onClick={() => { stopSchedule(pipeline.id); persistPipelines(savedPipelines.map(p => p.id===pipeline.id?{...p,isActive:false}:p)) }} className="text-xs px-3 py-1.5 rounded-lg font-semibold" style={{ background:'rgba(234,179,8,0.1)', border:'1px solid rgba(234,179,8,0.25)', color:'#fbbf24' }}>⏸ Pause</button>
                        ):pipeline.scheduleMs?(
                          <button onClick={() => { startSchedule(pipeline); persistPipelines(savedPipelines.map(p => p.id===pipeline.id?{...p,isActive:true}:p)) }} className="text-xs px-3 py-1.5 rounded-lg font-semibold" style={{ background:'rgba(34,197,94,0.08)', border:'1px solid rgba(34,197,94,0.2)', color:'#4ade80' }}>▶ Resume</button>
                        ):null}
                        <button onClick={() => setShowScheduleModal(pipeline)} className="text-xs px-3 py-1.5 rounded-lg font-semibold" style={{ background:'rgba(124,58,237,0.1)', border:'1px solid rgba(124,58,237,0.2)', color:'#a78bfa' }}>⏱ Schedule</button>
                        <button onClick={() => { setSelectedPipelineForLogs(pipeline); setShowLogsPanel(true) }} className="text-xs px-3 py-1.5 rounded-lg font-semibold"
                          style={{ background:latestLog?.status==='failed'?'rgba(239,68,68,0.12)':'rgba(99,102,241,0.1)', border:`1px solid ${latestLog?.status==='failed'?'rgba(239,68,68,0.25)':'rgba(99,102,241,0.2)'}`, color:latestLog?.status==='failed'?'#f87171':'#818cf8' }}>
                          {latestLog?.status==='failed'?'⚠ View Error':'Runs & Logs'}
                        </button>
                        <button onClick={() => runPipeline(pipeline,'manual')} className="text-xs px-3 py-1.5 rounded-lg font-semibold text-white" style={{ background:'rgba(124,58,237,0.15)', border:'1px solid rgba(124,58,237,0.3)' }}>▶ Run Now</button>
                        <button onClick={() => {
                          if (window.confirm(`Permanently delete pipeline "${pipeline.name}"? This cannot be undone.`)) {
                            stopSchedule(pipeline.id)
                            persistPipelines(savedPipelines.filter(p => p.id!==pipeline.id))
                            addAuditLog({ id:crypto.randomUUID(), time:new Date().toLocaleString(), action:'delete_pipeline', entity_type:'pipeline', entity_id:pipeline.id, details:`Deleted pipeline "${pipeline.name}"`, user:'local_user' })
                          }
                        }} className="text-xs px-2 py-1.5 rounded-lg" style={{ background:'rgba(239,68,68,0.08)', border:'1px solid rgba(239,68,68,0.15)', color:'#f87171' }} title="Permanently delete pipeline">🗑</button>
                      </div>
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        )}
      {/* POST-RUN PREVIEW */}
      {postRunPreview && postRunPreview.rows.length > 0 && (
        <div className="rounded-2xl overflow-hidden" style={{ background:'rgba(255,255,255,0.02)', border:'1px solid rgba(52,211,153,0.2)' }}>
          <div className="flex items-center justify-between px-6 py-4" style={{ borderBottom:'1px solid rgba(255,255,255,0.06)' }}>
            <div className="flex items-center gap-3">
              <div className="w-2 h-2 rounded-full animate-pulse" style={{ background:'#34d399' }} />
              <p className="font-semibold text-white text-sm">Post-Transformation Preview</p>
              <span className="text-xs px-2.5 py-0.5 rounded-full" style={{ background:'rgba(52,211,153,0.1)', color:'#34d399' }}>
                Top 100 rows · destination
              </span>
              <span className="text-xs" style={{ color:'rgba(255,255,255,0.3)' }}>
                {postRunPreview?.columns.length} columns
              </span>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => setFullscreenTable({ title: 'Post-Transformation Preview', subtitle: 'Top 100 rows from destination after pipeline run', columns: postRunPreview?.columns ?? [], rows: postRunPreview?.rows ?? [], accentColor: '#34d399' })}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold"
                style={{ background:'rgba(52,211,153,0.1)', border:'1px solid rgba(52,211,153,0.25)', color:'#34d399' }}
              >
                <svg width="11" height="11" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/>
                </svg>
                Fullscreen
              </button>
              <button onClick={() => setPostRunPreview(null)} style={{ color:'rgba(255,255,255,0.3)', fontSize:'16px', background:'none', border:'none', cursor:'pointer' }}>✕</button>
            </div>
          </div>
          <div className="overflow-auto" style={{ maxHeight:'320px' }}>
            <table className="w-full text-xs" style={{ color:'rgba(255,255,255,0.65)', borderCollapse:'collapse' }}>
              <thead className="sticky top-0" style={{ background:'#0a0714', zIndex:10 }}>
                <tr>
                  <th className="p-3 text-left font-semibold" style={{ borderBottom:'1px solid rgba(255,255,255,0.08)', color:'rgba(255,255,255,0.3)', width:'40px' }}>#</th>
                  {postRunPreview?.columns.map(col => (
                    <th key={col} className="p-3 text-left font-semibold whitespace-nowrap" style={{ borderBottom:'1px solid rgba(255,255,255,0.08)', color:'rgba(255,255,255,0.8)' }}>{col}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {(postRunPreview?.rows ?? []).map((row, i) => (
                  <tr key={i} style={{ borderBottom:'1px solid rgba(255,255,255,0.03)' }}
                    onMouseEnter={e => (e.currentTarget.style.background = 'rgba(52,211,153,0.04)')}
                    onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}>
                    <td className="p-3 font-mono" style={{ color:'rgba(255,255,255,0.2)' }}>{i + 1}</td>
                    {postRunPreview?.columns.map(col => (
                      <td key={col} className="p-3 whitespace-nowrap">
                        {row[col] === '' || row[col] === null || row[col] === undefined
                          ? <span style={{ color:'#f87171', fontStyle:'italic' }}>null</span>
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

      {lastRunResult && (
        <div className="rounded-2xl px-6 py-4 flex items-center justify-between gap-4 flex-wrap"
          style={{ background:'rgba(192, 194, 193, 0.08)', border:'1px solid rgba(34,197,94,0.25)' }}>
          <div className="flex items-center gap-3">
            <span className="text-xl">✅</span>
            <div>
              <p className="font-bold text-white text-sm">"{lastRunResult.pipelineName}" ran successfully</p>
              <p className="text-xs mt-0.5" style={{ color:'rgba(255,255,255,0.5)' }}>
                {lastRunResult.rowsIn.toLocaleString()} rows in → {lastRunResult.rowsOut.toLocaleString()} rows out
                {lastRunResult.rowsAffected > 0 && <span style={{ color:'#f87171' }}> · −{lastRunResult.rowsAffected.toLocaleString()} removed</span>}
                {' · '}{lastRunResult.mode === 'append' ? '➕ Appended' : '♻️ Replaced'}
              </p>
            </div>
          </div>
          <button onClick={() => setLastRunResult(null)} style={{ color:'rgba(255,255,255,0.3)', fontSize:'18px', background:'none', border:'none', cursor:'pointer' }}>✕</button>
        </div>
      )}
          {/* STATS */}
        {(transformations.length > 0 || analysis) && (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {[
              { label: 'Source Rows',      value: lastRunResult ? lastRunResult.rowsIn.toLocaleString()  : (preview?.sourceRows?.toLocaleString() ?? '—'), color: '#a78bfa' },
{ label: 'Transforms',       value: transformations.length, color: '#60a5fa' },
{ label: destinationMode === 'append' ? 'Total After Append' : 'Rows After Clean',
  value: lastRunResult ? lastRunResult.rowsOut.toLocaleString() : (preview?.totalRows?.toLocaleString() ?? '—'), color: '#34d399' },
{ label: 'Rows Removed',
  value: lastRunResult ? lastRunResult.rowsAffected.toLocaleString() : (preview?.rowsRemoved?.toLocaleString() ?? '—'),
  color: (lastRunResult?.rowsAffected ?? preview?.rowsRemoved ?? 0) > 0 ? '#f87171' : 'rgba(255,255,255,0.3)' },

            ].map(s => (
              <div key={s.label} className="rounded-2xl px-5 py-4" style={{ background:'rgba(255,255,255,0.02)', border:'1px solid rgba(255,255,255,0.06)' }}>
                <p className="text-xs mb-1" style={{ color:'rgba(255,255,255,0.35)' }}>{s.label}</p>
                <p className="text-2xl font-bold" style={{ color:s.color }}>{s.value}</p>
              </div>
            ))}
          </div>
        )}

        {/* SCHEMA MONITOR */}
        {!driftLoading && pipelineRun && (
          <div className="rounded-2xl overflow-hidden" style={{ border:`1px solid ${statusColors.border}` }}>
            <div className="px-6 py-4" style={{ background:statusColors.bg, borderBottom:'1px solid rgba(255,255,255,0.06)' }}>
              <div className="flex items-start justify-between gap-4">
                <div className="flex items-start gap-3">
                  <span className="w-3 h-3 rounded-full block mt-1 animate-pulse" style={{ background:statusColors.dot }} />
                  <div>
                    <div className="flex items-center gap-2">
                      <p className="font-bold text-white">Schema Change Detector</p>
                      <span className="text-xs px-2 py-0.5 rounded-full font-bold" style={{ background:statusColors.bg, color:statusColors.text, border:`1px solid ${statusColors.border}` }}>{statusColors.label}</span>
                      {driftScore>0&&<span className="text-xs px-2 py-0.5 rounded-full" style={{ background:'rgba(255,255,255,0.08)', color:'rgba(255,255,255,0.5)' }}>{driftScore}% drift</span>}
                    </div>
                    <p className="text-xs mt-1" style={{ color:'rgba(255,255,255,0.45)' }}>
                      {isFirstRun?'Watching your data structure. Will alert you if anything changes.':runStatus==='GREEN'?`Matches baseline from ${baseline?.updatedAt}.`:`Changed since ${baseline?.updatedAt}. Review before running.`}
                    </p>
                  </div>
                </div>
                <div className="flex gap-2">
                  <button onClick={() => setShowHowItWorks(v => !v)} className="text-xs px-3 py-1.5 rounded-lg font-semibold" style={{ background:'rgba(255,255,255,0.06)', color:'rgba(255,255,255,0.45)', border:'1px solid rgba(255,255,255,0.1)' }}>{showHowItWorks?'Hide':'? How it works'}</button>
                  <button onClick={loadPipelineData} className="text-xs px-3 py-1.5 rounded-lg" style={{ background:'rgba(255,255,255,0.06)', color:'rgba(255,255,255,0.5)', border:'1px solid rgba(255,255,255,0.1)' }}>↻</button>
                </div>
              </div>
            </div>
            <div style={{ background:'rgba(255,255,255,0.01)' }}>
              {isFirstRun?(
                <div className="px-6 py-6"><p className="font-semibold text-white mb-1">No baseline yet — first upload</p><p className="text-xs" style={{ color:'rgba(255,255,255,0.4)' }}>Save a pipeline to set the baseline.</p></div>
              ):runIssues.length===0?(
                <div className="px-6 py-5 flex items-center gap-4">
                  <div className="w-10 h-10 rounded-xl flex items-center justify-center flex-shrink-0" style={{ background:'rgba(34,197,94,0.1)', border:'1px solid rgba(34,197,94,0.2)' }}>✅</div>
                  <div><p className="font-semibold text-white">Structure unchanged from baseline</p><p className="text-xs mt-0.5" style={{ color:'rgba(255,255,255,0.4)' }}>Safe to run. {baseline?.runCount>0&&`${baseline.runCount} successful run${baseline.runCount!==1?'s':''}.`}</p></div>
                </div>
              ):(
                <div className="p-5 space-y-3">
                  <div className="rounded-xl px-4 py-3" style={{ background:'rgba(255,255,255,0.03)', border:'1px solid rgba(255,255,255,0.08)' }}>
                    <p className="text-sm font-semibold text-white">{runIssues.length} change{runIssues.length!==1?'s':''} detected</p>
                    <p className="text-xs mt-0.5" style={{ color:'rgba(255,255,255,0.4)' }}>{runStatus==='RED'?'Breaking — pipeline paused until reviewed.':'Minor — review recommended.'}</p>
                  </div>
                  <button onClick={() => setShowApprovalModal(true)} className="w-full py-3 rounded-xl text-sm font-bold text-white" style={{ background:'linear-gradient(135deg, #7c3aed, #2563eb)' }}>Review All Changes →</button>
                </div>
              )}
            </div>
          </div>
        )}
        {/* DOWNLOAD CSV */}
        {(lastRunResult || (preview && preview.rows.length > 0)) && !gsheetsConnected && (
          <div className="flex items-center justify-between px-5 py-4 rounded-2xl"
            style={{ background: 'rgba(124,58,237,0.08)', border: '1px solid rgba(124,58,237,0.25)' }}>
            <div>
              <p className="text-sm font-bold text-white">Output ready</p>
              <p className="text-xs mt-0.5" style={{ color: 'rgba(255,255,255,0.4)' }}>
                {lastRunResult
                  ? `${lastRunResult.rowsOut.toLocaleString()} rows · ${destinationName}`
                  : `${preview?.totalRows?.toLocaleString()} rows · ${destinationName}`}
              </p>
            </div>
            <button
              onClick={async () => {
  const outputFile = useStore.getState().pipelineOutputFile
  if (outputFile) {
    const url = URL.createObjectURL(outputFile)
    const a = document.createElement('a')
    a.href = url
    a.download = outputFile.name || destinationName || 'cleaned_output.csv'
    a.click()
    URL.revokeObjectURL(url)
    return
  }
  if (!activeFile) return
  const formData = new FormData()
  formData.append('file', activeFile)
  transformations.forEach(t => { const k = TRANSFORM_TO_CLEAN[t.id]; if (k) formData.append(k, 'true') })
  try {
    const res = await fetch('/api/clean-data', { method: 'POST', body: formData })
    const data = await res.json()
    if (data.download_url) {
      const fileRes = await fetch(`${import.meta.env.VITE_API_URL}${data.download_url}`)
      const blob = await fileRes.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = destinationName || 'cleaned_output.csv'
      a.click()
      URL.revokeObjectURL(url)
    }
  } catch (e) { console.error('Download failed', e) }
}}
              className="flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-bold text-white"
              style={{ background: 'linear-gradient(135deg, #7c3aed, #2563eb)', boxShadow: '0 4px 20px rgba(124,58,237,0.3)', flexShrink: 0 }}
            >
              <svg width="13" height="13" fill="none" stroke="white" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                <polyline points="7 10 12 15 17 10"/>
                <line x1="12" y1="15" x2="12" y2="3"/>
              </svg>
              Download Cleaned CSV
            </button>
          </div>
        )}
        {/* DATA PREVIEW */}
        {transformations.length > 0 && (
          <div className="rounded-2xl overflow-hidden" style={{ background:'rgba(255,255,255,0.02)', border:'1px solid rgba(255,255,255,0.06)' }}>
            <div className="flex items-center justify-between px-6 py-4" style={{ borderBottom:'1px solid rgba(255,255,255,0.06)' }}>
              <div className="flex items-center gap-3">
                <div className="w-2 h-2 rounded-full" style={{ background:'#34d399' }} />
                <p className="font-semibold text-white text-sm">Post-Transform Preview</p>
                <span className="text-xs px-2.5 py-0.5 rounded-full" style={{ background:'rgba(255,255,255,0.06)', color:'rgba(255,255,255,0.4)' }}>First 100 rows shown</span>
              </div>
              {preview && (
  <div className="flex items-center gap-4 text-xs" style={{ color:'rgba(255,255,255,0.35)' }}>
    <span>
      Showing first <strong>{Math.min(100, preview.rows.length)}</strong> rows of{' '}
      <strong>
        {lastRunResult
          ? lastRunResult.rowsOut.toLocaleString()
          : preview.totalRows?.toLocaleString()}
      </strong>{' '}
      {destinationMode === 'append' ? 'total (after append)' : 'total'}
    </span>
    {preview.rowsRemoved > 0 && <span style={{ color:'#f87171' }}>−{preview.rowsRemoved.toLocaleString()} rows removed</span>}
  </div>
)}
            {preview && preview.rows.length > 0 && (
              <button
                onClick={() => setFullscreenTable({ title: 'Post-Transform Preview', subtitle: `${(lastRunResult ? lastRunResult.rowsOut : preview?.totalRows)?.toLocaleString()} rows · ${destinationName}`, columns: preview?.columns ?? [], rows: preview?.rows ?? [], accentColor: '#34d399' })}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold"
                style={{ background:'rgba(52,211,153,0.1)', border:'1px solid rgba(52,211,153,0.25)', color:'#34d399' }}
              >
                <svg width="11" height="11" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/>
                </svg>
                Fullscreen
              </button>
            )}
            </div>
            {previewLoading?(
              <div className="flex items-center justify-center py-16"><svg className="animate-spin mr-3" width="18" height="18" fill="none" stroke="#7c3aed" strokeWidth="2.5" viewBox="0 0 24 24"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg><span className="text-sm" style={{ color:'rgba(255,255,255,0.4)' }}>Applying transforms...</span></div>
            ):preview && preview.rows.length > 0?(
              <div className="overflow-auto" style={{ maxHeight:'400px' }}>
                <table className="w-full text-xs" style={{ color:'rgba(255,255,255,0.65)', borderCollapse:'collapse' }}>
  <thead className="sticky top-0" style={{ background:'#0a0714', zIndex:10 }}>
    <tr>
      <th className="p-3 text-left font-semibold" style={{ borderBottom:'1px solid rgba(255,255,255,0.08)', color:'rgba(255,255,255,0.3)', width:'40px' }}>#</th>
      {(preview?.columns ?? []).map((col: string) => (<th key={col} className="p-3 text-left font-semibold whitespace-nowrap" style={{ borderBottom:'1px solid rgba(255,255,255,0.08)', color:'rgba(255,255,255,0.7)' }}>{col}</th>))}
    </tr>
  </thead>
  <tbody>
    {(preview?.rows ?? []).map((row: any, i: number) => (
  <tr key={i} style={{ borderBottom:'1px solid rgba(255,255,255,0.03)' }} onMouseEnter={e => (e.currentTarget.style.background='rgba(124,58,237,0.05)')} onMouseLeave={e => (e.currentTarget.style.background='transparent')}>
    <td className="p-3 font-mono" style={{ color:'rgba(255,255,255,0.2)' }}>
      {i + 1}
    </td>
                        {(preview?.columns ?? []).map((col: string) => (<td key={col} className="p-3 whitespace-nowrap">{row[col]===''||row[col]===null||row[col]===undefined?<span style={{ color:'#f87171', fontStyle:'italic' }}>null</span>:typeof row[col]==='number'?row[col].toLocaleString():String(row[col])}</td>))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ):(
              <div className="flex items-center justify-center py-16"><p className="text-sm" style={{ color:'rgba(255,255,255,0.3)' }}>{activeFile?'Preview appears after transforms are applied':'Upload a data source to see preview'}</p></div>
            )}
          </div>
        )}

        {transformations.length===0&&savedPipelines.length===0&&(
          <div className="rounded-2xl py-20 text-center" style={{ background:'rgba(255,255,255,0.01)', border:'1px dashed rgba(255,255,255,0.06)' }}>
            <div className="w-16 h-16 rounded-2xl mx-auto mb-5 flex items-center justify-center" style={{ background:'rgba(124,58,237,0.1)', border:'1px solid rgba(124,58,237,0.2)' }}>
              <svg width="24" height="24" fill="none" stroke="#a78bfa" strokeWidth="1.5" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>
            </div>
            <p className="font-bold text-white text-lg mb-2">No transformations yet</p>
            <p className="text-sm mb-6" style={{ color:'rgba(255,255,255,0.3)' }}>Select a source, set a destination, add transforms, then save your pipeline</p>
            <button onClick={() => setShowTransformPicker(true)} className="px-6 py-3 rounded-xl font-semibold text-white text-sm" style={{ background:'linear-gradient(135deg, #7c3aed, #2563eb)' }}>+ Add First Transformation</button>
          </div>
        )}
      </div>

      {/* SAVE PIPELINE MODAL */}
      {showSaveModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background:'rgba(0,0,0,0.8)' }} onClick={() => setShowSaveModal(false)}>
          <div className="rounded-2xl w-full max-w-lg max-h-[92vh] overflow-auto" style={{ background:'#0d0a1a', border:'1px solid rgba(255,255,255,0.1)' }} onClick={e => e.stopPropagation()}>
            <div className="p-6 border-b" style={{ borderColor:'rgba(255,255,255,0.08)' }}>
              <h3 className="font-bold text-white text-base mb-1">Save Pipeline</h3>
              <p className="text-xs" style={{ color:'rgba(255,255,255,0.4)' }}><span style={{ color:'#a78bfa' }}>{selectedSourceId}</span>{' → '}<span style={{ color:'#34d399' }}>{destinationName}</span></p>
            </div>
            <div className="p-6 space-y-5">
              <div>
                <label className="text-xs font-semibold block mb-2" style={{ color:'rgba(255,255,255,0.5)' }}>Pipeline Name *</label>
                <input type="text" value={pipelineName} onChange={e => setPipelineName(e.target.value)} placeholder="e.g. Daily Customer Cleanse" autoFocus className="w-full text-sm text-white rounded-xl px-4 py-3 outline-none" style={{ background:'rgba(255,255,255,0.05)', border:'1px solid rgba(124,58,237,0.3)' }} />
              </div>
              <div className="rounded-xl p-3" style={{ background:'rgba(255,255,255,0.03)', border:'1px solid rgba(255,255,255,0.07)' }}>
                <p className="text-xs font-semibold mb-2" style={{ color:'rgba(255,255,255,0.4)' }}>{transformations.length} transforms</p>
                <div className="flex flex-wrap gap-1.5">{transformations.map(t => (<span key={t.id} className="text-xs px-2 py-1 rounded-lg flex items-center gap-1" style={{ background:'rgba(255,255,255,0.06)', color:'rgba(255,255,255,0.6)' }}>{t.icon} {t.label}</span>))}</div>
              </div>
              <div>
                <label className="text-xs font-bold uppercase tracking-widest block mb-3" style={{ color:'rgba(255,255,255,0.4)' }}>⏱ Execution Schedule</label>
                <div className="flex gap-1 mb-4">
                  {([{id:'frequency',label:'⏱ Frequency'},{id:'daily-at',label:'🕘 Run at time'}] as const).map(tab => (
                    <button key={tab.id} onClick={() => setSaveScheduleTab(tab.id)} className="flex-1 py-2 rounded-xl text-xs font-bold transition-all"
                      style={{ background:saveScheduleTab===tab.id?'rgba(124,58,237,0.25)':'rgba(255,255,255,0.04)', border:`1px solid ${saveScheduleTab===tab.id?'rgba(124,58,237,0.5)':'rgba(255,255,255,0.08)'}`, color:saveScheduleTab===tab.id?'#c4b5fd':'rgba(255,255,255,0.45)' }}>
                      {tab.label}
                    </button>
                  ))}
                </div>
                {saveScheduleTab==='daily-at'&&(
                  <div className="space-y-3">
                    <div className="px-4 py-3 rounded-xl" style={{ background:'rgba(124,58,237,0.08)', border:'1px solid rgba(124,58,237,0.2)' }}><p className="text-xs" style={{ color:'#c4b5fd' }}>Pipeline runs every day at the chosen time and outputs the full cleaned file.</p></div>
                    <DailyAtTimePicker value={saveDailyAtTime} onChange={setSaveDailyAtTime} />
                    {saveDailyAtTime&&(<div className="rounded-xl px-4 py-3" style={{ background:'rgba(34,197,94,0.08)', border:'1px solid rgba(34,197,94,0.2)' }}><p className="text-xs font-bold" style={{ color:'#4ade80' }}>✓ Will run every day at {fmt12Save(saveDailyAtTime)}</p></div>)}
                  </div>
                )}
                {saveScheduleTab==='frequency'&&(
                  <div className="space-y-3">
                    <div onClick={() => setSaveScheduleValue('none')} className="flex items-center gap-3 p-3 rounded-xl cursor-pointer mb-2 transition-all"
                      style={{ background:saveScheduleValue==='none'?'rgba(255,255,255,0.07)':'rgba(255,255,255,0.02)', border:`1px solid ${saveScheduleValue==='none'?'rgba(255,255,255,0.18)':'rgba(255,255,255,0.06)'}` }}>
                      <div className="w-4 h-4 rounded-full flex items-center justify-center flex-shrink-0" style={{ border:`2px solid ${saveScheduleValue==='none'?'#a78bfa':'rgba(255,255,255,0.3)'}` }}>
                        {saveScheduleValue==='none'&&<div className="w-2 h-2 rounded-full" style={{ background:'#a78bfa' }} />}
                      </div>
                      <div><p className="text-sm font-semibold text-white">Manual only</p><p className="text-xs" style={{ color:'rgba(255,255,255,0.35)' }}>Run on demand via Run Now</p></div>
                    </div>
                    <div className="grid grid-cols-4 gap-2">
                      {SCHEDULE_PRESETS.map(preset => (
                        <div key={preset.value} onClick={() => setSaveScheduleValue(preset.value)} className="p-2.5 rounded-xl cursor-pointer transition-all text-center"
                          style={{ background:saveScheduleValue===preset.value?'rgba(124,58,237,0.2)':'rgba(255,255,255,0.03)', border:`1px solid ${saveScheduleValue===preset.value?'rgba(124,58,237,0.5)':'rgba(255,255,255,0.07)'}` }}>
                          <p className="text-xs font-bold" style={{ color:saveScheduleValue===preset.value?'#a78bfa':'rgba(255,255,255,0.7)' }}>{preset.label}</p>
                          {preset.cron&&!['24h','1w'].includes(preset.value)&&<p className="font-mono mt-0.5" style={{ color:'rgba(255,255,255,0.25)', fontSize:'9px' }}>{preset.cron}</p>}
                        </div>
                      ))}
                    </div>
                    {isSaveCustom&&(
                      <div className="mt-3 grid grid-cols-2 gap-3 p-3 rounded-xl" style={{ background:'rgba(124,58,237,0.06)', border:'1px solid rgba(124,58,237,0.2)' }}>
                        <div>
                          <label className="text-xs block mb-1" style={{ color:'rgba(255,255,255,0.4)' }}>Interval (ms)</label>
                          <input type="number" min={60000} value={saveCustomMs} onChange={e => setSaveCustomMs(Number(e.target.value))} className="w-full text-sm text-white rounded-lg px-3 py-2 outline-none" style={{ background:'rgba(255,255,255,0.05)', border:'1px solid rgba(124,58,237,0.3)' }} />
                          <p className="text-xs mt-1" style={{ color:'rgba(255,255,255,0.3)' }}>= {fmtDur(saveCustomMs)}</p>
                        </div>
                        <div>
                          <label className="text-xs block mb-1" style={{ color:'rgba(255,255,255,0.4)' }}>Cron expression</label>
                          <input type="text" value={saveCustomCron} onChange={e => setSaveCustomCron(e.target.value)} placeholder="*/5 * * * *" className="w-full text-sm text-white rounded-lg px-3 py-2 outline-none" style={{ background:'rgba(255,255,255,0.05)', border:'1px solid rgba(124,58,237,0.3)' }} />
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
              {showSaveTimezone&&(
                <div className="grid grid-cols-2 gap-4">
                  {saveScheduleTab==='frequency'&&isSaveAbsolute&&(<div><label className="text-xs font-bold uppercase tracking-widest block mb-2" style={{ color:'rgba(255,255,255,0.4)' }}>⏰ Time of Day</label><input type="time" value={saveScheduleTime} onChange={e => setSaveScheduleTime(e.target.value)} className="w-full text-sm text-white rounded-xl px-4 py-3 outline-none" style={{ background:'rgba(255,255,255,0.05)', border:'1px solid rgba(124,58,237,0.3)', colorScheme:'dark' }} /></div>)}
                  <div className={saveScheduleTab==='frequency'&&isSaveAbsolute?'':'col-span-2'}>
                    <label className="text-xs font-bold uppercase tracking-widest block mb-2" style={{ color:'rgba(255,255,255,0.4)' }}>🌍 Timezone</label>
                    <select value={saveTimezone} onChange={e => setSaveTimezone(e.target.value)} className="w-full text-sm text-white rounded-xl px-4 py-3 outline-none" style={{ background:'rgba(255,255,255,0.05)', border:'1px solid rgba(124,58,237,0.3)' }}>
                      {TIMEZONES.map(tz => <option key={tz} value={tz} style={{ background:'#0d0a1a' }}>{tz}</option>)}
                    </select>
                  </div>
                </div>
              )}
              <div>
                <div className="flex items-center justify-between mb-2">
                  <label className="text-xs font-bold uppercase tracking-widest" style={{ color:'rgba(255,255,255,0.4)' }}>📅 Start Date</label>
                  <div className="flex items-center gap-2">
                    {saveStartDate&&<button onClick={() => setSaveStartDate('')} className="text-xs px-2 py-0.5 rounded-md" style={{ color:'#f87171', background:'rgba(239,68,68,0.1)', border:'1px solid rgba(239,68,68,0.2)' }}>Clear</button>}
                    <button onClick={() => setSaveShowCalendar(v => !v)} className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-xl font-semibold transition-all"
                      style={{ background:saveShowCalendar?'rgba(124,58,237,0.25)':'rgba(255,255,255,0.05)', border:`1px solid ${saveShowCalendar?'rgba(124,58,237,0.5)':'rgba(255,255,255,0.1)'}`, color:saveShowCalendar?'#c4b5fd':'rgba(255,255,255,0.55)' }}>
                      <svg width="11" height="11" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
                      {saveStartDate ? fmtDateSave(saveStartDate) : 'Pick start date'}
                    </button>
                  </div>
                </div>
                <p className="text-xs mb-3" style={{ color:'rgba(255,255,255,0.3)' }}>Optional — pipeline won't run before this date. Leave empty to start immediately.</p>
                {saveShowCalendar&&<MiniCalendar value={saveStartDate} onChange={d => { setSaveStartDate(d); if (d) setSaveShowCalendar(false) }} />}
                {saveStartDate&&!saveShowCalendar&&(<div className="flex items-center gap-2 px-3 py-2.5 rounded-xl" style={{ background:'rgba(124,58,237,0.08)', border:'1px solid rgba(124,58,237,0.2)' }}><p className="text-xs" style={{ color:'#c4b5fd' }}>Starts on <strong>{fmtDateSave(saveStartDate)}</strong></p></div>)}
              </div>
              {saveScheduleTab==='frequency'&&saveScheduleValue!=='none'&&saveScheduleValue!=='custom'&&(
                <div className="rounded-xl px-4 py-2.5" style={{ background:'rgba(124,58,237,0.08)', border:'1px solid rgba(124,58,237,0.2)' }}>
                  <p className="text-xs" style={{ color:'#c4b5fd' }}>⚡ Auto-runs every <strong>{SCHEDULE_PRESETS.find(p => p.value===saveScheduleValue)?.label}</strong>{isSaveAbsolute&&<> at <strong>{saveScheduleTime}</strong></>}{showSaveTimezone&&saveTimezone!=='UTC'&&<> in <strong>{saveTimezone}</strong></>}{saveStartDate&&<> · starting <strong>{fmtDateSave(saveStartDate)}</strong></>}</p>
                </div>
              )}
            </div>
            <div className="px-6 pb-6 flex gap-3">
              <button onClick={() => setShowSaveModal(false)} className="flex-1 py-3 rounded-xl font-semibold" style={{ background:'rgba(255,255,255,0.04)', border:'1px solid rgba(255,255,255,0.08)', color:'rgba(255,255,255,0.5)' }}>Cancel</button>
              <button onClick={handleSavePipeline} disabled={!pipelineName.trim()} className="flex-1 py-3 rounded-xl font-bold text-white disabled:opacity-40" style={{ background:'linear-gradient(135deg, #7c3aed, #2563eb)' }}>Save Pipeline</button>
            </div>
          </div>
        </div>
      )}

      {/* Schedule Modal */}
      {showScheduleModal != null && (
        <ScheduleModal
          pipeline={showScheduleModal}
          onClose={() => setShowScheduleModal(undefined)}
          onSave={(schedMs, schedLabel, schedCron, timezone, startDate) =>
            handleScheduleUpdate(showScheduleModal, schedMs, schedLabel, schedCron, timezone, startDate)
          }
        />
      )}

      {/* LOGS PANEL */}
      {showLogsPanel && selectedPipelineForLogs != null && (
        <div className="fixed inset-0 z-50 flex justify-end" onClick={() => setShowLogsPanel(false)}>
          <div className="absolute inset-0" style={{ background:'rgba(0,0,0,0.7)' }} />
          <div className="relative flex flex-col shadow-2xl" style={{ width:'680px', height:'100%', background:'#07050f', borderLeft:'1px solid rgba(255,255,255,0.08)' }} onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between px-6 py-5" style={{ borderBottom:'1px solid rgba(255,255,255,0.07)', background:'rgba(255,255,255,0.01)' }}>
              <div>
                <div className="flex items-center gap-3 mb-1">
                  <div className="w-8 h-8 rounded-lg flex items-center justify-center" style={{ background:'rgba(99,102,241,0.2)', border:'1px solid rgba(99,102,241,0.3)' }}><svg width="14" height="14" fill="none" stroke="#818cf8" strokeWidth="2" viewBox="0 0 24 24"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg></div>
                  <h2 className="font-bold text-white">Pipeline Runs</h2>
                </div>
                <p className="text-xs" style={{ color:'rgba(255,255,255,0.35)' }}><span style={{ color:'#a78bfa' }}>{selectedPipelineForLogs.sourceName}</span><span style={{ color:'rgba(255,255,255,0.2)' }}> → </span><span style={{ color:'#34d399' }}>{selectedPipelineForLogs.destinationName}</span><span className="ml-3">{selectedPipelineForLogs.logs.length} runs logged</span></p>
              </div>
              <div className="flex items-center gap-2">
                <select value={selectedPipelineForLogs.id} onChange={e => { const p=savedPipelines.find(x => x.id===e.target.value); if (p) setSelectedPipelineForLogs(p) }} className="text-xs text-white rounded-lg px-3 py-2 outline-none" style={{ background:'rgba(255,255,255,0.06)', border:'1px solid rgba(255,255,255,0.1)' }}>
                  {savedPipelines.map(p => <option key={p.id} value={p.id} style={{ background:'#0d0a1a' }}>{p.name}</option>)}
                </select>
                <button onClick={() => runPipeline(selectedPipelineForLogs,'manual')} className="text-xs px-3 py-2 rounded-lg font-bold text-white" style={{ background:'rgba(124,58,237,0.2)', border:'1px solid rgba(124,58,237,0.35)' }}>▶ Run Now</button>
                <button onClick={() => setShowLogsPanel(false)} style={{ color:'rgba(255,255,255,0.35)', fontSize:'20px' }}>✕</button>
              </div>
            </div>
            <div className="grid grid-cols-4 divide-x border-white/5" style={{ borderBottom:'1px solid rgba(255,255,255,0.07)' }}>
              {[
                { label:'Total Runs',   value:selectedPipelineForLogs.runCount },
                { label:'Success Rate', value:selectedPipelineForLogs.logs.length>0?`${Math.round(selectedPipelineForLogs.logs.filter(l => l.status==='success').length/selectedPipelineForLogs.logs.length*100)}%`:'—', color:'#4ade80' },
                { label:'Avg Duration', value:selectedPipelineForLogs.logs.length>0?fmtDur(selectedPipelineForLogs.logs.reduce((a,l)=>a+l.duration,0)/selectedPipelineForLogs.logs.length):'—' },
                { label:'Schedule',     value:selectedPipelineForLogs.scheduleLabel||'Manual', color:selectedPipelineForLogs.scheduleLabel?'#a78bfa':'rgba(255,255,255,0.4)' },
              ].map(s => (<div key={s.label} className="px-4 py-3 text-center border-white/5"><p className="text-xs mb-0.5" style={{ color:'rgba(255,255,255,0.35)' }}>{s.label}</p><p className="text-lg font-bold" style={{ color:s.color||'white' }}>{s.value}</p></div>))}
            </div>
            {(() => { const latestLog=getLatestLog(selectedPipelineForLogs); if (!latestLog||latestLog.status==='success') return null; return (
              <div className="px-6 py-3 flex items-center gap-3" style={{ background:'rgba(239,68,68,0.08)', borderBottom:'1px solid rgba(239,68,68,0.15)' }}>
                <span className="text-sm">⚠️</span>
                <div className="flex-1"><p className="text-xs font-bold" style={{ color:'#f87171' }}>Last run failed — {fmtRelative(latestLog.timestamp)}</p><p className="text-xs" style={{ color:'rgba(255,255,255,0.45)' }}>{latestLog.steps.find(s => s.status==='failed')?.message||'Unknown error'}{' · '}<button onClick={() => setExpandedLogId(latestLog.id)} className="underline" style={{ color:'#f87171' }}>See details</button></p></div>
                <button onClick={() => navigate('/audit-logs')} className="text-xs px-3 py-1.5 rounded-lg font-semibold flex-shrink-0" style={{ background:'rgba(239,68,68,0.15)', border:'1px solid rgba(239,68,68,0.3)', color:'#f87171' }}>View in Audit Log →</button>
              </div>
            )})()}
            <div className="flex-1 overflow-auto p-6">
              {selectedPipelineForLogs.logs.length===0?(
                <div className="flex flex-col items-center justify-center h-full text-center py-20">
                  <div className="w-16 h-16 rounded-2xl mb-5 flex items-center justify-center" style={{ background:'rgba(255,255,255,0.03)', border:'1px solid rgba(255,255,255,0.08)' }}><svg width="24" height="24" fill="none" stroke="rgba(255,255,255,0.2)" strokeWidth="1.5" viewBox="0 0 24 24"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg></div>
                  <p className="text-white font-semibold mb-2">No runs yet</p>
                  <p className="text-xs mb-5" style={{ color:'rgba(255,255,255,0.3)' }}>Click "Run Now" to execute this pipeline manually</p>
                  <button onClick={() => runPipeline(selectedPipelineForLogs,'manual')} className="px-5 py-2.5 rounded-xl text-sm font-bold text-white" style={{ background:'linear-gradient(135deg, #7c3aed, #2563eb)' }}>▶ Run Pipeline Now</button>
                </div>
              ):(
                <div className="relative">
                  <div className="absolute left-4 top-0 bottom-0 w-px" style={{ background:'rgba(255,255,255,0.07)' }} />
                  <div className="space-y-3 pl-12">
                    {selectedPipelineForLogs.logs.map(log => {
                      const isExpanded=expandedLogId===log.id; const dotColor=log.status==='success'?'#22c55e':log.status==='running'?'#3b82f6':'#ef4444'
                      return (
                        <div key={log.id} className="relative">
                          <div className="absolute -left-12 top-4 flex flex-col items-center"><div className="w-3 h-3 rounded-full border-2" style={{ background:dotColor, borderColor:'#07050f', zIndex:1 }} /></div>
                          <div className="rounded-xl overflow-hidden cursor-pointer transition-all" style={{ background:isExpanded?'rgba(255,255,255,0.04)':'rgba(255,255,255,0.02)', border:`1px solid ${isExpanded?'rgba(255,255,255,0.12)':'rgba(255,255,255,0.06)'}` }} onClick={() => setExpandedLogId(isExpanded?null:log.id)}>
                            <div className="flex items-center justify-between px-4 py-3 gap-4">
                              <div className="flex items-center gap-3 min-w-0">
                                <StatusBadge status={log.status} />
                                <div className="min-w-0">
                                  <p className="text-xs font-bold text-white">{new Date(log.timestamp).toLocaleString()}</p>
                                  <p className="text-xs" style={{ color:'rgba(255,255,255,0.35)' }}>{fmtRelative(log.timestamp)} · {log.triggeredBy==='schedule'?`⏱ ${log.schedule}`:log.triggeredBy==='manual'?'🖱 Manual':'🔌 API'}</p>
                                </div>
                              </div>
                              <div className="flex items-center gap-3 flex-shrink-0">
                                <div className="text-xs text-right" style={{ color:'rgba(255,255,255,0.45)' }}><p>{log.rowsIn.toLocaleString()} → {log.rowsOut.toLocaleString()} rows</p><p style={{ color:'rgba(255,255,255,0.3)' }}>{fmtDur(log.duration)}</p></div>
                                {log.status==='failed'&&(<button onClick={e => { e.stopPropagation(); navigate('/audit-logs') }} className="text-xs px-2.5 py-1.5 rounded-lg font-semibold flex-shrink-0" style={{ background:'rgba(239,68,68,0.12)', border:'1px solid rgba(239,68,68,0.25)', color:'#f87171' }}>Audit Log →</button>)}
                                <span style={{ color:'rgba(255,255,255,0.25)', fontSize:'10px' }}>{isExpanded?'▲':'▼'}</span>
                              </div>
                            </div>
                            {isExpanded&&(
                              <div className="px-4 pb-4 space-y-2 border-t" style={{ borderColor:'rgba(255,255,255,0.07)' }}>
                                <p className="text-xs font-semibold mt-3 mb-2 uppercase tracking-widest" style={{ color:'rgba(255,255,255,0.3)' }}>Step-by-step</p>
                                <div className="flex h-2 rounded-full overflow-hidden gap-0.5 mb-3">{log.steps.map((step,si) => (<div key={si} className="flex-1 rounded-full" style={{ background:step.status==='success'?'#22c55e':step.status==='failed'?'#ef4444':'rgba(255,255,255,0.1)' }} />))}</div>
                                {log.steps.map((step,si) => (
                                  <div key={si} className="flex items-center justify-between py-2 px-3 rounded-lg" style={{ background:'rgba(255,255,255,0.03)', border:'1px solid rgba(255,255,255,0.05)' }}>
                                    <div className="flex items-center gap-2"><span className="text-xs">{step.status==='success'?'✅':step.status==='failed'?'❌':'⏭'}</span><span className="text-xs font-medium text-white">{step.name}</span>{step.message&&<span className="text-xs" style={{ color:'#f87171' }}>{step.message}</span>}</div>
                                    <span className="text-xs font-mono" style={{ color:'rgba(255,255,255,0.3)' }}>{fmtDur(step.duration)}</span>
                                  </div>
                                ))}
                                <div className="flex items-center justify-between pt-2 border-t" style={{ borderColor:'rgba(255,255,255,0.06)' }}>
                                  <div className="text-xs"><p style={{ color:'rgba(255,255,255,0.4)' }}><span className="text-white font-medium">{log.rowsIn.toLocaleString()}</span> in → <span className="text-white font-medium">{log.rowsOut.toLocaleString()}</span> out <span style={{ color:'#f87171' }}>(−{(log.rowsIn-log.rowsOut).toLocaleString()})</span></p><p style={{ color:'rgba(255,255,255,0.3)' }}>Total: {fmtDur(log.duration)}</p></div>
                                  <div className="flex items-center gap-2">
                                    {log.status==='failed'&&(<button onClick={e => { e.stopPropagation(); navigate('/audit-logs') }} className="text-xs px-3 py-1.5 rounded-lg font-semibold" style={{ background:'rgba(239,68,68,0.12)', border:'1px solid rgba(239,68,68,0.25)', color:'#f87171' }}>View in Audit Log →</button>)}
                                    <button onClick={e => { e.stopPropagation(); runPipeline(selectedPipelineForLogs,'manual') }} className="text-xs px-3 py-1.5 rounded-lg font-semibold text-white" style={{ background:'rgba(124,58,237,0.2)', border:'1px solid rgba(124,58,237,0.3)' }}>↻ Re-run</button>
                                  </div>
                                </div>
                              </div>
                            )}
                          </div>
                        </div>
                      )
                    })}
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* SQL DRAWER */}
      {showSQLDrawer&&(
        <div className="fixed inset-0 z-50 flex justify-end" onClick={() => setShowSQLDrawer(false)}>
          <div className="absolute inset-0" style={{ background:'rgba(0,0,0,0.6)' }} />
          <div className="relative flex flex-col shadow-2xl" style={{ width:'600px', height:'100%', background:'#0a0714', borderLeft:'1px solid rgba(255,255,255,0.08)' }} onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between p-5" style={{ borderBottom:'1px solid rgba(255,255,255,0.06)' }}>
              <div><h2 className="font-bold text-white text-sm">Unified SQL Pipeline</h2><p className="text-xs mt-0.5" style={{ color:'rgba(255,255,255,0.35)' }}>Table: <span style={{ color:'#818cf8' }}>{tableName}</span></p></div>
              <div className="flex items-center gap-2">
                <button onClick={copySQL} className="flex items-center gap-1.5 px-4 py-2 rounded-lg text-xs font-semibold" style={{ background:copiedSQL?'rgba(34,197,94,0.15)':'rgba(99,102,241,0.15)', border:copiedSQL?'1px solid rgba(34,197,94,0.3)':'1px solid rgba(99,102,241,0.3)', color:copiedSQL?'#4ade80':'#818cf8' }}>{copiedSQL?'✓ Copied!':'Copy SQL'}</button>
                <button onClick={() => setShowSQLDrawer(false)} style={{ color:'rgba(255,255,255,0.35)', fontSize:'18px' }}>✕</button>
              </div>
            </div>
            <div className="flex-1 overflow-auto p-5"><pre className="p-5 text-xs overflow-auto leading-relaxed rounded-2xl" style={{ background:'rgba(0,0,0,0.5)', color:'#a5f3fc', fontFamily:"'JetBrains Mono','Fira Code','Courier New',monospace", whiteSpace:'pre-wrap', border:'1px solid rgba(99,102,241,0.2)' }}>{unifiedSQL}</pre></div>
          </div>
        </div>
      )}

      {/* DRIFT PANEL */}
      {showDriftPanel&&(
        <div className="fixed inset-0 z-50 flex justify-end" onClick={() => setShowDriftPanel(false)}>
          <div className="absolute inset-0" style={{ background:'rgba(0,0,0,0.6)' }} />
          <div className="relative flex flex-col shadow-2xl" style={{ width:'480px', height:'100%', background:'#0a0714', borderLeft:'1px solid rgba(255,255,255,0.08)' }} onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between p-5" style={{ borderBottom:'1px solid rgba(255,255,255,0.06)' }}>
              <div><h2 className="font-bold text-white">Schema Change Report</h2><p className="text-xs mt-0.5" style={{ color:'rgba(255,255,255,0.35)' }}>{activeSource?.name}</p></div>
              <button onClick={() => setShowDriftPanel(false)} style={{ color:'rgba(255,255,255,0.35)', fontSize:'18px' }}>✕</button>
            </div>
            <div className="flex-1 overflow-auto p-5 space-y-4">
              <div className="rounded-2xl p-4" style={{ background:statusColors.bg, border:`1px solid ${statusColors.border}` }}>
                <div className="flex items-center gap-3 mb-2"><span className="w-3 h-3 rounded-full" style={{ background:statusColors.dot }} /><p className="font-bold text-white">{statusColors.label}</p></div>
                <p className="text-xs" style={{ color:'rgba(255,255,255,0.5)' }}>{runStatus==='GREEN'?'No changes. Pipeline can run automatically.':runStatus==='YELLOW'?'Minor changes. Worth reviewing.':'Breaking changes. Pipeline paused until reviewed.'}</p>
              </div>
              {runIssues.length===0?(
                <div className="text-center py-10"><p className="text-4xl mb-3">✅</p><p className="text-sm font-semibold text-white">No changes detected</p></div>
              ):(
                <>
                  {runIssues.map((issue: any) => {
                    const sev=SEV_COLOR[issue.severity]||SEV_COLOR.YELLOW; const kind=KIND_LABEL[issue.kind]||{icon:'❓',plain:issue.description}
                    return (<div key={issue.id} className="rounded-xl p-4" style={{ background:sev.bg, border:`1px solid ${sev.border}` }}><div className="flex items-center gap-2 mb-2"><span>{kind.icon}</span><p className="text-sm font-bold text-white">{issue.column}</p><span className="ml-auto text-xs px-2 py-0.5 rounded-full" style={{ background:'rgba(0,0,0,0.2)', color:sev.text }}>{issue.severity==='RED'?'Breaking':'Minor'}</span></div><p className="text-xs mb-1" style={{ color:'rgba(255,255,255,0.6)' }}>{kind.plain}</p><p className="text-xs" style={{ color:'rgba(255,255,255,0.4)' }}>{issue.suggestion}</p></div>)
                  })}
                  <button onClick={() => { setShowDriftPanel(false); setShowApprovalModal(true) }} className="w-full py-3 rounded-xl font-bold text-white text-sm" style={{ background:'linear-gradient(135deg, #7c3aed, #2563eb)' }}>Review & Approve Fixes →</button>
                </>
              )}
            </div>
          </div>
        </div>
      )}

      {/* RULES PANEL */}
      {showRulesPanel&&(
        <div className="fixed inset-0 z-50 flex justify-end" onClick={() => setShowRulesPanel(false)}>
          <div className="absolute inset-0" style={{ background:'rgba(0,0,0,0.6)' }} />
          <div className="relative flex flex-col shadow-2xl" style={{ width:'480px', height:'100%', background:'#0a0714', borderLeft:'1px solid rgba(255,255,255,0.08)' }} onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between p-5" style={{ borderBottom:'1px solid rgba(255,255,255,0.06)' }}>
              <div><h2 className="font-bold text-white">🧠 Learned Rules</h2><p className="text-xs mt-1" style={{ color:'rgba(255,255,255,0.4)' }}>Auto-applied on future uploads</p></div>
              <button onClick={() => setShowRulesPanel(false)} style={{ color:'rgba(255,255,255,0.35)', fontSize:'18px' }}>✕</button>
            </div>
            <div className="flex-1 overflow-auto p-5 space-y-3">
              {learnedRules.length===0?(
                <div className="text-center py-12"><p className="text-4xl mb-3">🧠</p><p className="text-sm font-semibold text-white mb-2">Nothing learned yet</p><p className="text-xs" style={{ color:'rgba(255,255,255,0.35)' }}>Approve schema fixes to save them as rules.</p></div>
              ):learnedRules.map((rule: any) => (
                <div key={rule.id} className="rounded-xl p-4" style={{ background:'rgba(124,58,237,0.06)', border:'1px solid rgba(124,58,237,0.18)' }}>
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex-1"><p className="text-sm font-semibold text-white mb-0.5">{rule.pattern}</p><p className="text-xs mb-2" style={{ color:'rgba(255,255,255,0.5)' }}>Fix: {rule.fix}</p><div className="flex items-center gap-3"><span className="text-xs px-2 py-0.5 rounded-full" style={{ background:'rgba(124,58,237,0.15)', color:'#a78bfa' }}>{rule.confidence}% confidence</span><span className="text-xs" style={{ color:'rgba(255,255,255,0.3)' }}>Used {rule.usedCount}×</span></div></div>
                    <button onClick={() => rollbackRule(rule.id)} className="text-xs px-2.5 py-1.5 rounded-lg font-semibold flex-shrink-0" style={{ background:'rgba(239,68,68,0.1)', color:'#f87171', border:'1px solid rgba(239,68,68,0.2)' }}>Undo</button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* APPROVAL MODAL */}
      {showApprovalModal&&(
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background:'rgba(0,0,0,0.8)' }} onClick={() => setShowApprovalModal(false)}>
          <div className="rounded-2xl w-full max-w-xl max-h-[85vh] overflow-auto" style={{ background:'#0d0a1a', border:'1px solid rgba(255,255,255,0.1)' }} onClick={e => e.stopPropagation()}>
            <div className="p-5" style={{ borderBottom:'1px solid rgba(255,255,255,0.08)' }}><h3 className="font-bold text-white mb-1">Review Data Changes</h3><p className="text-xs" style={{ color:'rgba(255,255,255,0.45)' }}>Tick the changes you accept — the system will remember and auto-handle them next time.</p></div>
            <div className="p-5 space-y-3">
              {runIssues.map((issue: any) => {
                const sev=SEV_COLOR[issue.severity]||SEV_COLOR.YELLOW; const kind=KIND_LABEL[issue.kind]||{icon:'❓',plain:issue.description}; const isSelected=selectedIssueIds.has(issue.id)
                return (
                  <div key={issue.id} onClick={() => { const n=new Set(selectedIssueIds); isSelected?n.delete(issue.id):n.add(issue.id); setSelectedIssueIds(n) }} className="rounded-xl p-4 cursor-pointer transition-all" style={{ background:isSelected?'rgba(124,58,237,0.12)':sev.bg, border:isSelected?'1px solid rgba(124,58,237,0.4)':`1px solid ${sev.border}` }}>
                    <div className="flex items-start gap-3">
                      <div className="w-5 h-5 rounded-md flex items-center justify-center flex-shrink-0 mt-0.5" style={{ background:isSelected?'#7c3aed':'transparent', border:`2px solid ${isSelected?'#7c3aed':'rgba(255,255,255,0.2)'}` }}>{isSelected&&<span className="text-white font-black" style={{ fontSize:'10px' }}>✓</span>}</div>
                      <div><p className="text-sm font-bold text-white mb-1">{kind.icon} {issue.column}</p><p className="text-xs" style={{ color:'rgba(255,255,255,0.6)' }}>{kind.plain}</p><p className="text-xs mt-1" style={{ color:'rgba(255,255,255,0.4)' }}>If approved: {issue.suggestion}</p></div>
                    </div>
                  </div>
                )
              })}
            </div>
            <div className="px-5 pb-5 flex gap-3">
              <button onClick={() => setShowApprovalModal(false)} className="flex-1 py-3 rounded-xl font-semibold" style={{ background:'rgba(255,255,255,0.04)', border:'1px solid rgba(255,255,255,0.08)', color:'rgba(255,255,255,0.5)' }}>Cancel</button>
              <button onClick={approveFixes} disabled={selectedIssueIds.size===0||approvingFixes} className="flex-1 py-3 rounded-xl font-bold text-white disabled:opacity-40" style={{ background:'linear-gradient(135deg, #7c3aed, #2563eb)' }}>{approvingFixes?'Saving...':`Approve ${selectedIssueIds.size>0?selectedIssueIds.size+' change'+(selectedIssueIds.size!==1?'s':''):'Selected'}`}</button>
            </div>
          </div>
        </div>
      )}

      {/* TRANSFORM PICKER */}
      {showTransformPicker&&(
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background:'rgba(0,0,0,0.75)' }} onClick={() => setShowTransformPicker(false)}>
          <div className="rounded-2xl p-6 max-w-lg w-full" style={{ background:'#0d0a1a', border:'1px solid rgba(255,255,255,0.1)' }} onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-2"><h3 className="font-bold text-white">Add Transformation</h3><button onClick={() => setShowTransformPicker(false)} style={{ color:'rgba(255,255,255,0.4)' }}>✕</button></div>
            <p className="text-xs mb-5" style={{ color:'rgba(255,255,255,0.35)' }}>Select a transform. You'll be sent to Analysis with it pre-ticked after saving the pipeline.</p>
            <div className="grid grid-cols-2 gap-3">
              {TRANSFORMATIONS.map(t => {
                const added=!!transformations.find(x => x.id===t.id)
                const isAICustom = t.id === 'ai_custom'
                const isAITransform = t.id === 'ai_clean' || t.id === 'ai_custom'
                const plan = localStorage.getItem('datapro_plan') || 'none'
                const isAILocked = isAITransform && (plan === 'none' || plan === 'free' || plan === 'solo')
                return (
                  <div key={t.id} onClick={() => {
                    if (added) return
                    if (isAILocked) { alert('AI transformations require a Starter or Pro plan. Go to /pricing to upgrade.'); return }
                    if (isAICustom) { setShowTransformPicker(false); setShowAICustomModal(true) }
                    else addT(t)
                  }} className="p-4 rounded-xl transition-all"
                    style={{ background:added?'rgba(34,197,94,0.06)':isAILocked?'rgba(255,255,255,0.02)':'rgba(255,255,255,0.03)', border:added?'1px solid rgba(34,197,94,0.2)':isAILocked?'1px solid rgba(255,255,255,0.06)':`1px solid ${t.color}20`, cursor:added?'default':isAILocked?'not-allowed':'pointer', opacity:added?0.6:isAILocked?0.5:1 }}
                    onMouseEnter={e => { if (!added && !isAILocked) e.currentTarget.style.background=`${t.color}10` }}
                    onMouseLeave={e => { if (!added && !isAILocked) e.currentTarget.style.background='rgba(255,255,255,0.03)' }}>
                    <div className="flex items-center gap-2 mb-1.5">
                      <span className="text-lg">{t.icon}</span>
                      <span className="font-semibold text-white text-sm">{t.label}</span>
                      {added && <span className="ml-auto text-xs font-bold" style={{ color:'#22c55e' }}>✓</span>}
                      {isAILocked && !added && <span className="ml-auto text-xs font-bold px-2 py-0.5 rounded-full" style={{ background:'rgba(234,179,8,0.12)', color:'#fbbf24', border:'1px solid rgba(234,179,8,0.2)' }}>🔒 Paid</span>}
                    </div>
                    <p className="text-xs" style={{ color:'rgba(255,255,255,0.35)' }}>{isAILocked ? 'Requires Starter or Pro plan' : t.desc}</p>
                  </div>
                )
              })}
            </div>
          </div>
        </div>
      )}

      {/* ALERT MODAL */}
      {showAlertModal&&(
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background:'rgba(0,0,0,0.75)' }} onClick={() => setShowAlertModal(false)}>
          <div className="rounded-2xl p-6 max-w-md w-full" style={{ background:'#0d0a1a', border:'1px solid rgba(255,255,255,0.1)' }} onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-5"><h3 className="font-bold text-white">Set Pipeline Alert</h3><button onClick={() => setShowAlertModal(false)} style={{ color:'rgba(255,255,255,0.4)' }}>✕</button></div>
            <div className="space-y-2 mb-5">
              {[{id:'missing',label:'Missing values exceed threshold'},{id:'schema',label:'Schema changes detected'},{id:'failure',label:'Flow failure or empty data'},{id:'staleness',label:'Data not updated on time'}].map(a => (
                <div key={a.id} onClick={() => setAlertType(a.id)} className="flex items-center gap-3 p-3 rounded-xl cursor-pointer" style={{ background:alertType===a.id?'rgba(124,58,237,0.15)':'rgba(255,255,255,0.04)', border:`1px solid rgba(124,58,237,${alertType===a.id?'0.35':'0.1'})` }}>
                  <div className="w-4 h-4 rounded-full flex items-center justify-center" style={{ border:`2px solid ${alertType===a.id?'#7c3aed':'rgba(255,255,255,0.3)'}` }}>{alertType===a.id&&<div className="w-2 h-2 rounded-full" style={{ background:'#7c3aed' }} />}</div>
                  <p className="text-sm text-white">{a.label}</p>
                </div>
              ))}
            </div>
            <button onClick={() => { addAuditLog({ id:crypto.randomUUID(), time:new Date().toLocaleString(), action:'set_alert', entity_type:'pipeline_alert', entity_id:alertType, details:`Alert: ${alertType}`, user:'local_user' }); setShowAlertModal(false) }} className="w-full py-3 rounded-xl font-bold text-white text-sm" style={{ background:'linear-gradient(135deg, #7c3aed, #2563eb)' }}>Save Alert</button>
          </div>
        </div>
      )}

      {/* GOOGLE SHEETS MODAL */}
      {showGSheetsModal && (
        <GSheetsModal
          onClose={() => setShowGSheetsModal(false)}
          onConnect={(email, sheetName, sheetId) => {
            const conn = { email, sheetName, sheetId }
            setGsheetsConnected(conn)
            setShowGSheetsModal(false)
            addAuditLog({
              id: crypto.randomUUID(), time: new Date().toLocaleString(),
              action: 'connect_dest_gsheets', entity_type: 'destination', entity_id: sheetName,
              details: `Google Sheets destination connected: "${sheetName}" (${email})`, user: 'local_user',
            })
          }}
        />
      )}

      {/* SHEETS PICKER FROM CONNECTED SOURCES — no re-auth needed */}
      {showGSheetsPicker && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background: 'rgba(0,0,0,0.85)' }} onClick={() => setShowGSheetsPicker(false)}>
          <div className="rounded-2xl w-full max-w-md overflow-hidden" style={{ background: '#0d0a1a', border: '1px solid rgba(52,211,153,0.3)' }} onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between px-5 py-4" style={{ borderBottom: '1px solid rgba(255,255,255,0.08)' }}>
              <div>
                <h3 className="font-bold text-white text-sm">Select Google Sheet</h3>
                <p className="text-xs mt-0.5" style={{ color: 'rgba(255,255,255,0.4)' }}>
                  {gsheetsPickerFor === 'source' ? 'Pick a source sheet from your connected sources' : 'Pick a destination sheet from your connected sources'}
                </p>
              </div>
              <button onClick={() => setShowGSheetsPicker(false)} style={{ color: 'rgba(255,255,255,0.35)', fontSize: '18px' }}>✕</button>
            </div>
            <div className="p-5 space-y-2">
              {sources.filter(s => s.type === 'sheets').length === 0 ? (
                <div className="text-center py-10">
                  <p className="text-3xl mb-3">📊</p>
                  <p className="text-sm font-semibold text-white mb-2">No Google Sheets connected yet</p>
                  <p className="text-xs mb-4" style={{ color: 'rgba(255,255,255,0.4)' }}>Go to Data Sources and connect a Google Sheet first.</p>
                  <button onClick={() => { setShowGSheetsPicker(false); window.location.href = '/sources' }}
                    className="px-5 py-2.5 rounded-xl text-sm font-bold text-white"
                    style={{ background: 'linear-gradient(135deg, #34A853, #0d9488)' }}>
                    Go to Data Sources →
                  </button>
                </div>
              ) : (
                <>
                  {sources.filter(s => s.type === 'sheets').map(s => (
                    <div key={s.id}
                      onClick={() => {
                        if (gsheetsPickerFor === 'dest') {
                          setGsheetsConnected({ email: '', sheetName: s.name, sheetId: s.sheetId || '' })
                          addAuditLog({ id: crypto.randomUUID(), time: new Date().toLocaleString(), action: 'connect_dest_gsheets', entity_type: 'destination', entity_id: s.name, details: `Google Sheets destination: "${s.name}"`, user: 'local_user' })
                        } else {
                          setSelectedSourceId(s.name)
                          setActiveSourceIsSheet(true)
                          switchActiveSource(s.file || null, s.name)
                        }
                        setShowGSheetsPicker(false)
                      }}
                      className="flex items-center gap-3 px-4 py-3 rounded-xl cursor-pointer transition-all"
                      style={{ background: 'rgba(52,211,153,0.06)', border: '1px solid rgba(52,211,153,0.2)' }}
                      onMouseEnter={e => (e.currentTarget.style.background = 'rgba(52,211,153,0.14)')}
                      onMouseLeave={e => (e.currentTarget.style.background = 'rgba(52,211,153,0.06)')}>
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
                        <rect x="3" y="2" width="18" height="20" rx="2" fill="#34A853" fillOpacity="0.2" stroke="#34A853" strokeWidth="1.5"/>
                        <line x1="7" y1="9" x2="17" y2="9" stroke="#34A853" strokeWidth="1.2"/>
                        <line x1="7" y1="13" x2="17" y2="13" stroke="#34A853" strokeWidth="1.2"/>
                        <line x1="7" y1="17" x2="13" y2="17" stroke="#34A853" strokeWidth="1.2"/>
                      </svg>
                      <div className="flex-1 min-w-0">
                        <p className="text-sm font-semibold text-white truncate">{s.name}</p>
                        <p className="text-xs" style={{ color: 'rgba(255,255,255,0.4)' }}>Connected · {s.addedAt}</p>
                      </div>
                      <span style={{ color: '#34d399', fontSize: '18px' }}>→</span>
                    </div>
                  ))}
                </>
              )}
            </div>
          </div>
        </div>
      )}
    {/* VIEW ORIGINAL MODAL */}
{showOriginalModal && activeSource && (
  <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background:'rgba(0,0,0,0.85)' }} onClick={() => setShowOriginalModal(false)}>
    <div className="rounded-2xl w-full max-w-5xl max-h-[85vh] flex flex-col" style={{ background:'#0d0a1a', border:'1px solid rgba(255,255,255,0.1)' }} onClick={e => e.stopPropagation()}>
      <div className="flex items-center justify-between px-6 py-4 flex-shrink-0" style={{ borderBottom:'1px solid rgba(255,255,255,0.08)' }}>
        <div>
          <h3 className="font-bold text-white">Original Source Data</h3>
          <p className="text-xs mt-0.5" style={{ color:'rgba(255,255,255,0.4)' }}>{activeSource.name} · showing first 50 rows (download capped at 50k)</p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => {
              if (!activeFile) return
              // Read file and cap at 50k rows
              const reader = new FileReader()
              reader.onload = (e) => {
                const text = e.target?.result as string
                const lines = text.split('\n')
                const capped = lines.slice(0, 50001).join('\n') // header + 50k rows
                const blob = new Blob([capped], { type: 'text/csv' })
                const url = URL.createObjectURL(blob)
                const a = document.createElement('a')
                a.href = url
                a.download = activeSource.name.replace('.csv', '_original_50k.csv')
                a.click()
                URL.revokeObjectURL(url)
              }
              reader.readAsText(activeFile)
            }}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold"
            style={{ background:'rgba(124,58,237,0.15)', border:'1px solid rgba(124,58,237,0.3)', color:'#a78bfa' }}
          >
            ⬇ Download Original (max 50k rows)
          </button>
          <button onClick={() => setShowOriginalModal(false)} style={{ color:'rgba(255,255,255,0.35)', fontSize:'20px' }}>✕</button>
        </div>
      </div>
      <OriginalDataPreview file={activeFile} />
    </div>
  </div>
)}

{/* showSourceGSheetsModal replaced by showGSheetsPicker above */}

{/* AI CUSTOM RULE MODAL */}
      {showAICustomModal && (
        <AICustomRuleModal
          activeSourceName={activeSource?.name || 'dataset'}
          colInfo={analysis?.col_info || []}
          sampleData={analysis?.sample_data || []}
          onClose={() => setShowAICustomModal(false)}
          onConfirm={(label, instruction) => {
            // Add as a custom transformation visible in the pipeline
            const customTransform = {
              id: `ai_custom_${Date.now()}`,
              label: label,
              icon: '🤖',
              desc: instruction.length > 60 ? instruction.slice(0, 57) + '...' : instruction,
              color: '#ec4899',
              type: 'ai_custom',
              applied: false,
            }
            addTransformation(customTransform)
            addAuditLog({
              id: crypto.randomUUID(), time: new Date().toLocaleString(),
              action: 'add_transformation', entity_type: 'transformation',
              entity_id: customTransform.id,
              details: `Added AI Custom Rule: "${label}" — ${instruction}`,
              user: 'local_user',
            })
            setShowAICustomModal(false)
          }}
        />
      )}
    </div>
  )
}