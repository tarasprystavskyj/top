import { useEffect, useMemo, useState } from 'react';
import { apiFetch } from '../utils/api';
import {
  avgPriceDiffColorRule,
  computePollingIntervalMs,
  computeSharedDomain,
  preparePnlSeries,
  seriesToSvgPath,
} from '../utils/backtestValidation';
import {
  buildRefreshSessionPlan,
  buildLiveSummaryModel,
  formatLiveTableCell,
  getLiveChartVisibility,
  inferLiveTableColumns,
  normalizeLiveChartPayload,
  normalizeLiveTableRows,
  shouldShowNoLiveSessions,
  selectLiveSessionState,
  applyLiveAutoRefreshToggle,
} from '../utils/liveSessionUi';

type FileEntry = { name: string; path: string; size: number; modified_at: string; symbol_guess?: string | null };
type Point = { ts: string; value: number };
type LiveSessionStatus = 'running' | 'stopped' | 'error' | 'unknown';
type LiveSessionEntry = {
  name: string;
  path: string;
  exchange?: string | null;
  timeframe?: string | null;
  status?: LiveSessionStatus;
  updated_at?: string | null;
};
type LiveSessionInspect = {
  path: string;
  name?: string;
  exchange?: string | null;
  timeframe?: string | null;
  status?: string | null;
  started_at?: string | null;
  updated_at?: string | null;
  open_legs?: number;
  filled_orders?: number;
  last_debug_event?: { level?: string; event_type?: string; ts?: string } | null;
  last_equity_ts?: string | null;
};
type LiveChartPayload = { live?: Point[]; backtest?: Point[]; distance?: Point[] };
type LiveTableKind = 'open_positions' | 'orders' | 'debug_events' | 'stdio';
type TableStateMap = Record<LiveTableKind, any[]>;
type TableLoadingMap = Record<LiveTableKind, boolean>;
type TableErrorMap = Record<LiveTableKind, string | null>;

function LineChart({ title, series }: { title: string; series: { name: string; color: string; data: Point[] }[] }) {
  const width = 920;
  const height = 320;
  const padding = 46;
  const domain = computeSharedDomain(series);
  if (!domain.pointCount) return <div style={{ border: '1px solid #d1d5db', borderRadius: 8, padding: 12 }}>{title}: No data</div>;
  const { xMin, xMax, yMin, yMax } = domain;
  const hasTimeX = xMax > 10_000_000_000;

  const fmtTime = (ms: number) => {
    if (!Number.isFinite(ms)) return '';
    return new Date(ms).toISOString().slice(5, 16).replace('T', ' ');
  };
  const yTickVals = [yMin, (yMin + yMax) / 2, yMax];
  const xTickVals = [xMin, (xMin + xMax) / 2, xMax];

  return (
    <div style={{ border: '1px solid #d1d5db', borderRadius: 8, padding: 12, background: '#fff' }}>
      <h4 style={{ marginTop: 0 }}>{title}</h4>
      <svg viewBox={`0 0 ${width} ${height}`} style={{ width: '100%', height: 320 }}>
        <rect x={0} y={0} width={width} height={height} fill="#fff" />
        <line x1={padding} y1={height - padding} x2={width - padding} y2={height - padding} stroke="#cbd5e1" />
        <line x1={padding} y1={padding} x2={padding} y2={height - padding} stroke="#cbd5e1" />
        {yTickVals.map((v, i) => {
          const y = height - padding - ((v - yMin) / (Math.max(1e-9, yMax - yMin))) * (height - padding * 2);
          return (
            <g key={`y-${i}`}>
              <line x1={padding} y1={y} x2={width - padding} y2={y} stroke="#e2e8f0" strokeDasharray="4 4" />
              <text x={padding - 8} y={y + 4} textAnchor="end" fontSize="11" fill="#475569">
                {v.toFixed(2)}
              </text>
            </g>
          );
        })}
        {xTickVals.map((v, i) => {
          const x = padding + ((v - xMin) / (Math.max(1e-9, xMax - xMin))) * (width - padding * 2);
          return (
            <g key={`x-${i}`}>
              <line x1={x} y1={padding} x2={x} y2={height - padding} stroke="#e2e8f0" strokeDasharray="4 4" />
              <text x={x} y={height - padding + 16} textAnchor="middle" fontSize="11" fill="#475569">
                {hasTimeX ? fmtTime(v) : v.toFixed(0)}
              </text>
            </g>
          );
        })}
        {series.map(s => {
          const pts = s.data.map((d, i) => {
            const tsMs = Date.parse(String(d.ts || ''));
            return { x: Number.isFinite(tsMs) ? tsMs : i, y: Number(d.value) || 0, ts: d.ts };
          });
          const path = seriesToSvgPath(pts, width, height, padding, domain);
          return <path key={s.name} d={path} stroke={s.color} strokeWidth={2} fill="none" />;
        })}
        <text x={padding} y={padding - 10} fontSize="11" fill="#334155">Y: value</text>
        <text x={width - padding} y={height - 8} textAnchor="end" fontSize="11" fill="#334155">X: {hasTimeX ? 'timestamp' : 'index'}</text>
      </svg>
      <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
        {series.map(s => (
          <span key={s.name} style={{ color: s.color, fontWeight: 600 }}>{s.name}</span>
        ))}
      </div>
    </div>
  );
}

const LIVE_TABLE_DEFS: { kind: LiveTableKind; title: string; emptyLabel: string }[] = [
  { kind: 'open_positions', title: 'Latest open positions', emptyLabel: 'No open positions' },
  { kind: 'orders', title: 'Latest orders', emptyLabel: 'No orders' },
  { kind: 'debug_events', title: 'Latest debug events', emptyLabel: 'No debug events' },
  { kind: 'stdio', title: 'Latest stdio log lines', emptyLabel: 'No stdio lines' },
];

const STATUS_COLOR_MAP: Record<string, string> = {
  running: '#16a34a',
  stopped: '#64748b',
  error: '#dc2626',
  unknown: '#94a3b8',
};

const DEBUG_LEVEL_COLOR_MAP: Record<string, string> = {
  error: '#dc2626',
  warn: '#d97706',
  warning: '#d97706',
  info: '#0284c7',
  debug: '#7c3aed',
};

function fmtTs(ts?: string | null) {
  if (!ts) return '—';
  const dt = new Date(ts);
  if (!Number.isFinite(dt.getTime())) return String(ts);
  return dt.toISOString().replace('T', ' ').slice(0, 19);
}

function normalizeStatus(value: any): LiveSessionStatus {
  const s = String(value || '').toLowerCase();
  if (s === 'running' || s === 'stopped' || s === 'error') return s;
  return 'unknown';
}

function normalizeLiveSessionEntry(raw: any): LiveSessionEntry {
  return {
    name: String(raw?.name || raw?.path || 'unknown_session'),
    path: String(raw?.path || ''),
    exchange: raw?.exchange != null ? String(raw.exchange) : null,
    timeframe: raw?.timeframe != null ? String(raw.timeframe) : null,
    status: normalizeStatus(raw?.status),
    updated_at: raw?.updated_at != null ? String(raw.updated_at) : null,
  };
}

function normalizeLiveSessionInspect(raw: any): LiveSessionInspect {
  const debug = raw?.last_debug_event && typeof raw.last_debug_event === 'object' ? raw.last_debug_event : null;
  return {
    path: String(raw?.path || ''),
    name: raw?.name != null ? String(raw.name) : undefined,
    exchange: raw?.exchange != null ? String(raw.exchange) : null,
    timeframe: raw?.timeframe != null ? String(raw.timeframe) : null,
    status: normalizeStatus(raw?.status),
    started_at: raw?.started_at != null ? String(raw.started_at) : null,
    updated_at: raw?.updated_at != null ? String(raw.updated_at) : null,
    open_legs: Number.isFinite(Number(raw?.open_legs)) ? Number(raw.open_legs) : 0,
    filled_orders: Number.isFinite(Number(raw?.filled_orders)) ? Number(raw.filled_orders) : 0,
    last_debug_event: debug
      ? {
          level: debug.level != null ? String(debug.level) : undefined,
          event_type: debug.event_type != null ? String(debug.event_type) : undefined,
          ts: debug.ts != null ? String(debug.ts) : undefined,
        }
      : null,
    last_equity_ts: raw?.last_equity_ts != null ? String(raw.last_equity_ts) : null,
  };
}

function normalizeLiveSessionStatus(raw: any): LiveSessionInspect {
  return normalizeLiveSessionInspect(raw || {});
}

function emptyLiveTables(): TableStateMap {
  return { open_positions: [], orders: [], debug_events: [], stdio: [] };
}

function emptyLiveTableLoading(): TableLoadingMap {
  return { open_positions: false, orders: false, debug_events: false, stdio: false };
}

function emptyLiveTableErrors(): TableErrorMap {
  return { open_positions: null, orders: null, debug_events: null, stdio: null };
}

export default function BacktestLiveValidationPage() {
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [selectedPath, setSelectedPath] = useState('');
  const [inspect, setInspect] = useState<any>(null);
  const [runData, setRunData] = useState<any>(null);
  const [runId, setRunId] = useState<string>('');
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [debugMode, setDebugMode] = useState(true);
  const [debugOpen, setDebugOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastPayload, setLastPayload] = useState<any>(null);
  const [lastRefreshTs, setLastRefreshTs] = useState<string>('');
  const [normalizePnl, setNormalizePnl] = useState(false);

  const [liveSessions, setLiveSessions] = useState<LiveSessionEntry[]>([]);
  const [selectedLivePath, setSelectedLivePath] = useState('');
  const [liveInspect, setLiveInspect] = useState<LiveSessionInspect | null>(null);
  const [liveStatus, setLiveStatus] = useState<LiveSessionInspect | null>(null);
  const [liveCharts, setLiveCharts] = useState<LiveChartPayload | null>(null);
  const [liveTables, setLiveTables] = useState<TableStateMap>(emptyLiveTables());
  const [liveAutoRefresh, setLiveAutoRefresh] = useState(false);
  const [liveLastRefreshTs, setLiveLastRefreshTs] = useState('');

  const [liveListLoading, setLiveListLoading] = useState(false);
  const [liveListError, setLiveListError] = useState<string | null>(null);
  const [liveInspectLoading, setLiveInspectLoading] = useState(false);
  const [liveInspectError, setLiveInspectError] = useState<string | null>(null);
  const [liveStatusLoading, setLiveStatusLoading] = useState(false);
  const [liveStatusError, setLiveStatusError] = useState<string | null>(null);
  const [liveChartLoading, setLiveChartLoading] = useState(false);
  const [liveChartError, setLiveChartError] = useState<string | null>(null);
  const [liveTableLoading, setLiveTableLoading] = useState<TableLoadingMap>(emptyLiveTableLoading());
  const [liveTableErrors, setLiveTableErrors] = useState<TableErrorMap>(emptyLiveTableErrors());

  const pollingIntervalMs = useMemo(() => computePollingIntervalMs(inspect?.bar_interval_seconds || runData?.inspect?.bar_interval_seconds || 60), [inspect?.bar_interval_seconds, runData?.inspect?.bar_interval_seconds]);
  const livePollingIntervalMs = useMemo(() => computePollingIntervalMs(liveStatus?.open_legs ? 15 : 20), [liveStatus?.open_legs]);

  const selectedLiveEntry = useMemo(() => liveSessions.find(s => s.path === selectedLivePath) || null, [liveSessions, selectedLivePath]);

  const liveSeries = useMemo(() => {
    const norm = normalizeLiveChartPayload(liveCharts || {});
    return { live: norm.live || [], backtest: norm.backtest || [], distance: norm.distance || [] };
  }, [liveCharts]);

  async function refreshLiveSessions(keepPath = true) {
    setLiveListLoading(true);
    setLiveListError(null);
    try {
      const r = await apiFetch('/api/backtest_live_validation/live_sessions');
      if (!r.ok) throw new Error('live sessions load failed');
      const d = await r.json().catch(() => ({}));
      const sessions = (Array.isArray(d?.sessions) ? d.sessions : []).map(normalizeLiveSessionEntry).filter((s: LiveSessionEntry) => !!s.path);
      setLiveSessions(sessions);
      if (keepPath && selectedLivePath && !sessions.some((s: LiveSessionEntry) => s.path === selectedLivePath)) {
        setSelectedLivePath('');
      }
    } catch (e: any) {
      setLiveListError(e?.message || 'live sessions load failed');
      setLiveSessions([]);
    } finally {
      setLiveListLoading(false);
    }
  }

  useEffect(() => {
    apiFetch('/api/backtest_live_validation/files')
      .then(r => r.json())
      .then(d => setFiles(Array.isArray(d?.files) ? d.files : []))
      .catch(() => setFiles([]));
    refreshLiveSessions(false);
  }, []);

  useEffect(() => {
    if (!runId || !autoRefresh) return;
    const timer = setInterval(() => {
      apiFetch(`/api/backtest_live_validation/run/${runId}/poll`)
        .then(r => r.json())
        .then(d => {
          setRunData((prev: any) => ({
            ...prev,
            ...d,
            pnl_chart: { ...(prev?.pnl_chart || {}), ...(d?.pnl_chart || {}) },
            margin_chart: { ...(prev?.margin_chart || {}), ...(d?.margin_chart || {}) },
            stats: { ...(prev?.stats || {}), ...(d?.stats || {}) },
          }));
          setLastPayload(d);
          setLastRefreshTs(new Date().toISOString());
        })
        .catch(() => undefined);
    }, pollingIntervalMs);
    return () => clearInterval(timer);
  }, [runId, autoRefresh, pollingIntervalMs]);

  useEffect(() => {
    if (!selectedLivePath || !liveAutoRefresh) return;
    const timer = setInterval(() => {
      loadLiveStatus(selectedLivePath, { silent: true });
    }, livePollingIntervalMs);
    return () => clearInterval(timer);
  }, [selectedLivePath, liveAutoRefresh, livePollingIntervalMs]);

  async function inspectPath(path: string) {
    setSelectedPath(path);
    setRunId('');
    setRunData(null);
    if (!path) return;
    setLoading(true);
    setError(null);
    const payload = { path };
    setLastPayload(payload);
    try {
      const r = await apiFetch('/api/backtest_live_validation/inspect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!r.ok) throw new Error('Inspect failed');
      setInspect(await r.json());
    } catch (e: any) {
      setError(e?.message || 'inspect failed');
      setInspect(null);
    } finally {
      setLoading(false);
    }
  }

  async function inspectLiveSession(path: string) {
    const state = selectLiveSessionState({ liveInspect, liveStatus, liveCharts, liveTables, selectedLivePath }, path);
    setSelectedLivePath(state.selectedLivePath);
    setLiveInspect(state.liveInspect);
    setLiveStatus(state.liveStatus);
    setLiveCharts(state.liveCharts);
    setLiveTables(state.liveTables);
    setLiveTableErrors(emptyLiveTableErrors());
    if (!path) return;
    setLiveInspectLoading(true);
    setLiveInspectError(null);
    try {
      const r = await apiFetch('/api/backtest_live_validation/live_session/inspect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      });
      if (!r.ok) throw new Error('Live inspect failed');
      const data = normalizeLiveSessionInspect(await r.json());
      setLiveInspect(data);
      setLiveLastRefreshTs(new Date().toISOString());
    } catch (e: any) {
      setLiveInspectError(e?.message || 'live inspect failed');
    } finally {
      setLiveInspectLoading(false);
    }
  }

  async function loadLiveStatus(path = selectedLivePath, opts: { silent?: boolean } = {}) {
    if (!path) return;
    if (!opts.silent) {
      setLiveStatusLoading(true);
      setLiveStatusError(null);
    }
    try {
      const r = await apiFetch(`/api/backtest_live_validation/live_session/status?path=${encodeURIComponent(path)}`);
      if (!r.ok) throw new Error('Live status failed');
      const d = normalizeLiveSessionStatus(await r.json());
      setLiveStatus(d);
      setLiveLastRefreshTs(new Date().toISOString());
    } catch (e: any) {
      if (!opts.silent) setLiveStatusError(e?.message || 'live status failed');
    } finally {
      if (!opts.silent) setLiveStatusLoading(false);
    }
  }

  async function loadLiveChart(path = selectedLivePath) {
    if (!path) return;
    setLiveChartLoading(true);
    setLiveChartError(null);
    try {
      const r = await apiFetch(`/api/backtest_live_validation/live_session/chart?path=${encodeURIComponent(path)}`);
      if (!r.ok) throw new Error('Live chart failed');
      setLiveCharts(normalizeLiveChartPayload(await r.json()));
      setLiveLastRefreshTs(new Date().toISOString());
    } catch (e: any) {
      setLiveChartError(e?.message || 'live chart failed');
      setLiveCharts(normalizeLiveChartPayload({}));
    } finally {
      setLiveChartLoading(false);
    }
  }

  async function loadLiveTable(kind: LiveTableKind, path = selectedLivePath) {
    if (!path) return;
    setLiveTableLoading(prev => ({ ...prev, [kind]: true }));
    setLiveTableErrors(prev => ({ ...prev, [kind]: null }));
    try {
      const r = await apiFetch(`/api/backtest_live_validation/live_session/table?path=${encodeURIComponent(path)}&kind=${kind}`);
      if (!r.ok) throw new Error(`Live ${kind} failed`);
      const d = await r.json();
      setLiveTables(prev => ({ ...prev, [kind]: normalizeLiveTableRows(d?.rows) }));
      setLiveLastRefreshTs(new Date().toISOString());
    } catch (e: any) {
      setLiveTables(prev => ({ ...prev, [kind]: [] }));
      setLiveTableErrors(prev => ({ ...prev, [kind]: e?.message || `live ${kind} failed` }));
    } finally {
      setLiveTableLoading(prev => ({ ...prev, [kind]: false }));
    }
  }

  async function refreshSelectedLiveSession() {
    const plan = buildRefreshSessionPlan(selectedLivePath);
    if (!plan.length) return;
    await inspectLiveSession(selectedLivePath);
    await loadLiveStatus(selectedLivePath);
  }

  async function onUpload(file: File) {
    setError(null);
    setLoading(true);
    try {
      const fd = new FormData();
      fd.append('file', file);
      const r = await apiFetch('/api/backtest_live_validation/upload', { method: 'POST', body: fd });
      if (!r.ok) {
        const txt = await r.text().catch(() => '');
        throw new Error(txt || `upload failed (HTTP ${r.status})`);
      }
      const d = await r.json();
      setSelectedPath(d.path);
      setInspect(d.inspect);
      setRunId('');
      setRunData(null);
      setLastPayload({ upload: file.name, upload_response: d });
    } catch (e: any) {
      setError(e?.message || 'upload failed');
    } finally {
      setLoading(false);
    }
  }

  async function runComparison() {
    if (!selectedPath) return;
    setLoading(true);
    setError(null);
    const payload = { path: selectedPath, auto_fetch_live: true, run_match: true, debug: debugMode };
    setLastPayload(payload);
    try {
      const r = await apiFetch('/api/backtest_live_validation/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!r.ok) throw new Error('run failed');
      const d = await r.json();
      setRunId(d.run_id);
      const details = await apiFetch(`/api/backtest_live_validation/run/${d.run_id}`).then(x => x.json());
      setRunData(details);
      setLastRefreshTs(new Date().toISOString());
    } catch (e: any) {
      setError(e?.message || 'run failed');
    } finally {
      setLoading(false);
    }
  }

  function statusBadge(status?: string | null) {
    const key = normalizeStatus(status);
    const bg = STATUS_COLOR_MAP[key] || STATUS_COLOR_MAP.unknown;
    return <span style={{ background: `${bg}20`, color: bg, border: `1px solid ${bg}55`, borderRadius: 999, padding: '2px 8px', fontWeight: 700 }}>{key}</span>;
  }

  function renderLiveTable(kind: LiveTableKind, title: string, emptyLabel: string) {
    const rows = liveTables[kind] || [];
    const loading = liveTableLoading[kind];
    const err = liveTableErrors[kind];
    const columns = inferLiveTableColumns(rows);
    return (
      <details key={kind} style={{ border: '1px solid #d1d5db', borderRadius: 8, padding: 10, background: '#fff' }}>
        <summary style={{ cursor: 'pointer', fontWeight: 700 }}>{title}</summary>
        <div style={{ marginTop: 8, marginBottom: 8 }}>
          <button disabled={!selectedLivePath || loading} onClick={() => loadLiveTable(kind)}>{loading ? 'Loading...' : `Refresh ${title.toLowerCase()}`}</button>
        </div>
        {err && <div style={{ color: '#dc2626', fontSize: 12, marginBottom: 6 }}>{err}</div>}
        {!rows.length ? (
          <div style={{ color: '#64748b', fontSize: 13 }}>{emptyLabel}</div>
        ) : (
          <div style={{ maxHeight: 320, overflow: 'auto', border: '1px solid #e2e8f0', borderRadius: 6 }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
              <thead>
                <tr>
                  {columns.map(col => (
                    <th key={col} style={{ textAlign: 'left', background: '#f8fafc', borderBottom: '1px solid #e2e8f0', padding: '6px 8px', position: 'sticky', top: 0 }}>
                      {col}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row: any, rowIdx: number) => (
                  <tr key={`row-${rowIdx}`} style={{ borderBottom: '1px solid #f1f5f9' }}>
                    {columns.map(col => (
                      <td key={`${rowIdx}-${col}`} style={{ padding: '6px 8px', verticalAlign: 'top', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}>
                        {formatLiveTableCell(col, row?.[col])}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </details>
    );
  }

  const avgDiffColor = runData?.stats?.comparison?.avg_price_diff_color || avgPriceDiffColorRule(runData?.stats?.live?.current_average_price, runData?.stats?.backtest?.current_average_price);
  const alignedPnl = preparePnlSeries(runData?.pnl_chart?.backtest || [], runData?.pnl_chart?.live || [], normalizePnl);
  const pnlDomain = computeSharedDomain([{ data: alignedPnl.backtest }, { data: alignedPnl.live }]);

  const liveChartVisibility = getLiveChartVisibility(liveCharts);
  const summaryModel = buildLiveSummaryModel(selectedLivePath, selectedLiveEntry, liveInspect, liveStatus);
  const debugLevelColor = DEBUG_LEVEL_COLOR_MAP[String(summaryModel.debugLevel).toLowerCase()] || '#64748b';

  return (
    <div style={{ maxWidth: 1100, margin: '0 auto', padding: 16, display: 'flex', flexDirection: 'column', gap: 14 }}>
      <h3>Backtest vs Live Validation</h3>

      <div style={{ border: '1px solid #d1d5db', borderRadius: 8, padding: 12 }}>
        <h4>Source selection</h4>
        <select value={selectedPath} onChange={e => inspectPath(e.target.value)} style={{ minWidth: 420 }}>
          <option value="">--select TradingView source--</option>
          {files.map(f => <option key={f.path} value={f.path}>{f.name}</option>)}
        </select>
        <div style={{ marginTop: 10 }}>
          <input type="file" accept=".csv" onChange={e => e.target.files?.[0] && onUpload(e.target.files[0])} />
        </div>
        {inspect && <pre style={{ background: '#f8fafc', padding: 8, borderRadius: 6, overflowX: 'auto' }}>{JSON.stringify(inspect, null, 2)}</pre>}
      </div>

      <div style={{ border: '1px solid #d1d5db', borderRadius: 8, padding: 12, background: '#fff' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <h4 style={{ margin: 0 }}>Live runner sessions</h4>
          <button onClick={() => refreshLiveSessions(false)} disabled={liveListLoading}>{liveListLoading ? 'Refreshing...' : 'Refresh sessions list'}</button>
        </div>
        <div style={{ marginTop: 10, display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <select value={selectedLivePath} onChange={e => inspectLiveSession(e.target.value)} style={{ minWidth: 600 }}>
            <option value="">--select live session from _reports/_live--</option>
            {liveSessions.map(session => (
              <option key={session.path} value={session.path}>
                {session.name || session.path} | ex: {session.exchange || 'n/a'} | tf: {session.timeframe || 'n/a'} | updated: {fmtTs(session.updated_at)} | status: {session.status || 'unknown'}
              </option>
            ))}
          </select>
          <label><input type="checkbox" checked={liveAutoRefresh} onChange={e => setLiveAutoRefresh(applyLiveAutoRefreshToggle({ liveAutoRefresh }, e.target.checked).liveAutoRefresh)} /> Auto refresh live status</label>
        </div>
        {liveListError && <div style={{ marginTop: 8, color: '#dc2626' }}>{liveListError}</div>}
        {shouldShowNoLiveSessions(liveSessions, liveListLoading) && <div style={{ marginTop: 8, color: '#64748b' }}>No live sessions</div>}
      </div>

      {!!selectedLivePath && (
        <>
          <div style={{ border: '1px solid #d1d5db', borderRadius: 8, padding: 12, background: '#fff' }}>
            <h4 style={{ marginTop: 0 }}>Live session summary</h4>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
              <button onClick={refreshSelectedLiveSession} disabled={liveInspectLoading || liveStatusLoading}>Refresh session</button>
              <button onClick={() => loadLiveStatus()} disabled={liveStatusLoading || !selectedLivePath}>{liveStatusLoading ? 'Loading status...' : 'Load session status'}</button>
              <button onClick={() => loadLiveChart()} disabled={liveChartLoading || !selectedLivePath}>{liveChartLoading ? 'Loading charts...' : 'Load live chart overlay'}</button>
            </div>
            {liveInspectError && <p style={{ color: '#dc2626' }}>{liveInspectError}</p>}
            {liveStatusError && <p style={{ color: '#dc2626' }}>{liveStatusError}</p>}
            {liveChartError && <p style={{ color: '#dc2626' }}>{liveChartError}</p>}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, fontSize: 13 }}>
              <div><strong>Session path:</strong> {summaryModel.sessionPath}</div>
              <div><strong>Status:</strong> {statusBadge(summaryModel.status)}</div>
              <div><strong>Started:</strong> {fmtTs(summaryModel.startedAt)}</div>
              <div><strong>Last updated:</strong> {fmtTs(summaryModel.updatedAt)}</div>
              <div><strong>Exchange:</strong> {summaryModel.exchange}</div>
              <div><strong>Timeframe:</strong> {summaryModel.timeframe}</div>
              <div><strong>Current open legs:</strong> {summaryModel.openLegs}</div>
              <div><strong>Total filled orders:</strong> {summaryModel.filledOrders}</div>
              <div>
                <strong>Last debug event:</strong>{' '}
                <span style={{ color: debugLevelColor, fontWeight: 700 }}>{String(summaryModel.debugLevel)} / {summaryModel.debugType}</span>
              </div>
              <div><strong>Last equity timestamp:</strong> {fmtTs(summaryModel.lastEquityTs)}</div>
            </div>
            {liveLastRefreshTs && <div style={{ marginTop: 8, fontSize: 12, color: '#475569' }}>Last live refresh: {liveLastRefreshTs}</div>}
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <h4 style={{ marginBottom: 0 }}>Live charts</h4>
            {liveChartVisibility.live ? <LineChart title="Live equity / PNL" series={[{ name: 'Live equity', color: '#16a34a', data: liveSeries.live }]} /> : <div style={{ border: '1px dashed #cbd5e1', borderRadius: 8, padding: 10, color: '#64748b' }}>No chart data</div>}
            {liveChartVisibility.overlay ? (
              <LineChart title="Live vs Backtest overlay" series={[{ name: 'Live', color: '#16a34a', data: liveSeries.live }, { name: 'Backtest', color: '#2563eb', data: liveSeries.backtest }]} />
            ) : (
              <div style={{ border: '1px dashed #cbd5e1', borderRadius: 8, padding: 10, color: '#64748b' }}>Overlay unavailable (requires both live and backtest series)</div>
            )}
            {liveChartVisibility.distance && <LineChart title="Absolute distance" series={[{ name: 'Absolute distance', color: '#dc2626', data: liveSeries.distance }]} />}
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <h4 style={{ marginBottom: 0 }}>Session details tables</h4>
            {LIVE_TABLE_DEFS.map(def => renderLiveTable(def.kind, def.title, def.emptyLabel))}
          </div>
        </>
      )}

      <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
        <button onClick={runComparison} disabled={!selectedPath || loading}>{loading ? 'Running...' : 'Run / Refresh comparison'}</button>
        <label><input type="checkbox" checked={autoRefresh} onChange={e => setAutoRefresh(e.target.checked)} /> Auto refresh</label>
        <label><input type="checkbox" checked={debugMode} onChange={e => setDebugMode(e.target.checked)} /> Debug mode</label>
        <label><input type="checkbox" checked={normalizePnl} onChange={e => setNormalizePnl(e.target.checked)} /> Normalize both to first common point</label>
        <span>Polling interval: {pollingIntervalMs} ms</span>
        {lastRefreshTs && <span>Last refresh: {lastRefreshTs}</span>}
      </div>

      {error && <p style={{ color: '#dc2626' }}>{error}</p>}

      {runData && (
        <>
          {runData?.fallback_canvas_url && (
            <details style={{ border: '1px solid #d1d5db', borderRadius: 8, padding: 12, background: '#fff' }} open>
              <summary style={{ cursor: 'pointer', fontWeight: 700 }}>Python matcher canvas (reference)</summary>
              <div style={{ marginTop: 10 }}>
                <img src={runData.fallback_canvas_url} style={{ width: '100%', maxWidth: 1200 }} />
              </div>
            </details>
          )}
          <LineChart
            title="PNL comparison"
            series={[
              { name: 'Backtest cumulative PNL', color: '#2563eb', data: alignedPnl.backtest },
              { name: 'Live cumulative PNL', color: '#16a34a', data: alignedPnl.live },
            ]}
          />
          <div style={{ border: '1px dashed #cbd5e1', borderRadius: 8, padding: 8, fontSize: 12, color: '#334155' }}>
            PNL debug — bt points: {alignedPnl.backtest.length}, live points: {alignedPnl.live.length}, xMin: {new Date(pnlDomain.xMin).toISOString()}, xMax: {new Date(pnlDomain.xMax).toISOString()}, yMin: {pnlDomain.yMin.toFixed(6)}, yMax: {pnlDomain.yMax.toFixed(6)}, normalized: {String(normalizePnl)}
          </div>

          <LineChart
            title="Margin usage"
            series={[
              { name: 'Backtest used margin %', color: '#2563eb', data: runData?.margin_chart?.backtest_margin_used_pct || [] },
              { name: 'Live used margin %', color: '#16a34a', data: runData?.margin_chart?.live_margin_used_pct || [] },
              { name: 'Live free margin %', color: '#f59e0b', data: runData?.margin_chart?.live_free_margin_pct || [] },
            ]}
          />

          <LineChart
            title="Slippage"
            series={[
              { name: 'Signed bps', color: '#7c3aed', data: runData?.slippage_chart?.signed_bps || [] },
              { name: 'Abs bps', color: '#dc2626', data: runData?.slippage_chart?.abs_bps || [] },
              { name: 'Rolling signed bps', color: '#0f766e', data: runData?.slippage_chart?.rolling_signed_bps || [] },
            ]}
          />

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
            <pre style={{ border: '1px solid #d1d5db', borderRadius: 8, padding: 10 }}>Backtest\n{JSON.stringify(runData?.stats?.backtest || {}, null, 2)}</pre>
            <pre style={{ border: '1px solid #d1d5db', borderRadius: 8, padding: 10 }}>Live\n{JSON.stringify(runData?.stats?.live || {}, null, 2)}</pre>
          </div>
          <div style={{ border: '1px solid #d1d5db', borderRadius: 8, padding: 10 }}>
            <strong>Comparison</strong>
            <pre>{JSON.stringify(runData?.stats?.comparison || {}, null, 2)}</pre>
            <div style={{ color: avgDiffColor, fontWeight: 700 }}>
              Avg price diff: {Number(runData?.stats?.comparison?.avg_price_diff || 0).toFixed(6)} ({avgDiffColor})
            </div>
          </div>
        </>
      )}

      <div style={{ border: '1px solid #d1d5db', borderRadius: 8, padding: 10 }}>
        <button onClick={() => setDebugOpen(v => !v)}>{debugOpen ? 'Hide debug' : 'Show debug'}</button>
        {debugOpen && (
          <pre style={{ maxHeight: 360, overflow: 'auto' }}>
            {JSON.stringify({
              selected_backtest_file_path: selectedPath,
              selected_live_path: selectedLivePath,
              inspect,
              liveInspect,
              liveStatus,
              liveCharts,
              liveTables,
              api_request_payload: lastPayload,
              last_refresh_time: lastRefreshTs,
              live_last_refresh_time: liveLastRefreshTs,
              current_polling_interval: pollingIntervalMs,
              live_polling_interval: livePollingIntervalMs,
              backend_debug: runData?.debug,
              loading,
              liveListLoading,
              liveInspectLoading,
              liveStatusLoading,
              liveChartLoading,
              liveTableLoading,
            }, null, 2)}
          </pre>
        )}
      </div>
    </div>
  );
}
