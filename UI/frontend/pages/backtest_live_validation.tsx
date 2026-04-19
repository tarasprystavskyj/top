import { useEffect, useMemo, useState } from 'react';
import { apiFetch } from '../utils/api';
import {
  avgPriceDiffColorRule,
  computePollingIntervalMs,
  computeSharedDomain,
  preparePnlSeries,
  seriesToSvgPath,
  sortSeriesByTs,
} from '../utils/backtestValidation';

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
        <text x={padding} y={padding - 10} fontSize="11" fill="#334155">
          Y: value
        </text>
        <text x={width - padding} y={height - 8} textAnchor="end" fontSize="11" fill="#334155">
          X: {hasTimeX ? 'timestamp' : 'index'}
        </text>
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

function normalizeAndSortSeries(points: any): Point[] {
  return sortSeriesByTs(Array.isArray(points) ? points : [])
    .map((p: any) => ({ ts: String(p?.ts || ''), value: Number(p?.value) || 0 }))
    .filter(p => p.ts);
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
  const [liveStatus, setLiveStatus] = useState<any>(null);
  const [liveCharts, setLiveCharts] = useState<LiveChartPayload | null>(null);
  const [liveTables, setLiveTables] = useState<Record<LiveTableKind, any[]>>({
    open_positions: [],
    orders: [],
    debug_events: [],
    stdio: [],
  });
  const [liveLoading, setLiveLoading] = useState(false);
  const [liveError, setLiveError] = useState<string | null>(null);
  const [liveAutoRefresh, setLiveAutoRefresh] = useState(false);
  const [liveLastRefreshTs, setLiveLastRefreshTs] = useState('');

  const pollingIntervalMs = useMemo(() => {
    return computePollingIntervalMs(inspect?.bar_interval_seconds || runData?.inspect?.bar_interval_seconds || 60);
  }, [inspect?.bar_interval_seconds, runData?.inspect?.bar_interval_seconds]);

  const livePollingIntervalMs = useMemo(() => {
    return computePollingIntervalMs(liveStatus?.bar_interval_seconds || 20);
  }, [liveStatus?.bar_interval_seconds]);

  const selectedLiveEntry = useMemo(() => liveSessions.find(s => s.path === selectedLivePath), [liveSessions, selectedLivePath]);

  const liveSeries = useMemo(() => {
    const live = normalizeAndSortSeries(liveCharts?.live || []);
    const backtest = normalizeAndSortSeries(liveCharts?.backtest || []);
    const distance = normalizeAndSortSeries(liveCharts?.distance || []);
    return { live, backtest, distance };
  }, [liveCharts]);

  useEffect(() => {
    apiFetch('/api/backtest_live_validation/files')
      .then(r => r.json())
      .then(d => setFiles(Array.isArray(d?.files) ? d.files : []))
      .catch(() => setFiles([]));

    apiFetch('/api/backtest_live_validation/live_sessions')
      .then(r => r.json())
      .then(d => setLiveSessions(Array.isArray(d?.sessions) ? d.sessions : []))
      .catch(() => setLiveSessions([]));
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
            pnl_chart: {
              ...(prev?.pnl_chart || {}),
              ...(d?.pnl_chart || {}),
            },
            margin_chart: {
              ...(prev?.margin_chart || {}),
              ...(d?.margin_chart || {}),
            },
            stats: {
              ...(prev?.stats || {}),
              ...(d?.stats || {}),
            },
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

  async function refreshLiveSessions(keepPath = true) {
    try {
      const r = await apiFetch('/api/backtest_live_validation/live_sessions');
      const d = await r.json().catch(() => ({}));
      const sessions = Array.isArray(d?.sessions) ? d.sessions : [];
      setLiveSessions(sessions);
      if (keepPath && selectedLivePath && !sessions.some((s: LiveSessionEntry) => s.path === selectedLivePath)) {
        setSelectedLivePath('');
      }
    } catch {
      setLiveSessions([]);
    }
  }

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
      const data = await r.json();
      setInspect(data);
    } catch (e: any) {
      setError(e?.message || 'inspect failed');
      setInspect(null);
    } finally {
      setLoading(false);
    }
  }

  async function inspectLiveSession(path: string) {
    setSelectedLivePath(path);
    setLiveInspect(null);
    setLiveStatus(null);
    setLiveCharts(null);
    setLiveTables({ open_positions: [], orders: [], debug_events: [], stdio: [] });
    if (!path) return;
    setLiveLoading(true);
    setLiveError(null);
    try {
      const r = await apiFetch('/api/backtest_live_validation/live_session/inspect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      });
      if (!r.ok) throw new Error('Live inspect failed');
      const data = await r.json();
      setLiveInspect(data || null);
      setLiveLastRefreshTs(new Date().toISOString());
    } catch (e: any) {
      setLiveError(e?.message || 'live inspect failed');
    } finally {
      setLiveLoading(false);
    }
  }

  async function loadLiveStatus(path = selectedLivePath, opts: { silent?: boolean } = {}) {
    if (!path) return;
    if (!opts.silent) {
      setLiveLoading(true);
      setLiveError(null);
    }
    try {
      const r = await apiFetch(`/api/backtest_live_validation/live_session/status?path=${encodeURIComponent(path)}`);
      if (!r.ok) throw new Error('Live status failed');
      const d = await r.json();
      setLiveStatus(d || null);
      setLiveLastRefreshTs(new Date().toISOString());
    } catch (e: any) {
      if (!opts.silent) setLiveError(e?.message || 'live status failed');
    } finally {
      if (!opts.silent) setLiveLoading(false);
    }
  }

  async function loadLiveChart(path = selectedLivePath) {
    if (!path) return;
    setLiveLoading(true);
    setLiveError(null);
    try {
      const r = await apiFetch(`/api/backtest_live_validation/live_session/chart?path=${encodeURIComponent(path)}`);
      if (!r.ok) throw new Error('Live chart failed');
      const d = await r.json();
      setLiveCharts(d || {});
      setLiveLastRefreshTs(new Date().toISOString());
    } catch (e: any) {
      setLiveError(e?.message || 'live chart failed');
      setLiveCharts(null);
    } finally {
      setLiveLoading(false);
    }
  }

  async function loadLiveTable(kind: LiveTableKind, path = selectedLivePath) {
    if (!path) return;
    setLiveLoading(true);
    try {
      const r = await apiFetch(`/api/backtest_live_validation/live_session/table?path=${encodeURIComponent(path)}&kind=${kind}`);
      if (!r.ok) throw new Error(`Live ${kind} failed`);
      const d = await r.json();
      const rows = Array.isArray(d?.rows) ? d.rows : [];
      setLiveTables(prev => ({ ...prev, [kind]: rows }));
      setLiveLastRefreshTs(new Date().toISOString());
    } catch {
      setLiveTables(prev => ({ ...prev, [kind]: [] }));
    } finally {
      setLiveLoading(false);
    }
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
    const key = String(status || 'unknown').toLowerCase();
    const bg = STATUS_COLOR_MAP[key] || STATUS_COLOR_MAP.unknown;
    return (
      <span style={{ background: `${bg}20`, color: bg, border: `1px solid ${bg}55`, borderRadius: 999, padding: '2px 8px', fontWeight: 700 }}>
        {key}
      </span>
    );
  }

  function renderLiveTable(kind: LiveTableKind, title: string, emptyLabel: string) {
    const rows = Array.isArray(liveTables[kind]) ? liveTables[kind] : [];
    return (
      <details key={kind} style={{ border: '1px solid #d1d5db', borderRadius: 8, padding: 10, background: '#fff' }}>
        <summary style={{ cursor: 'pointer', fontWeight: 700 }}>{title}</summary>
        <div style={{ marginTop: 8, marginBottom: 8 }}>
          <button disabled={!selectedLivePath || liveLoading} onClick={() => loadLiveTable(kind)}>
            {liveLoading ? 'Loading...' : `Refresh ${title.toLowerCase()}`}
          </button>
        </div>
        {!rows.length ? (
          <div style={{ color: '#64748b', fontSize: 13 }}>{emptyLabel}</div>
        ) : (
          <pre style={{ margin: 0, maxHeight: 260, overflow: 'auto', background: '#f8fafc', borderRadius: 6, padding: 8 }}>{JSON.stringify(rows, null, 2)}</pre>
        )}
      </details>
    );
  }

  const avgDiffColor = runData?.stats?.comparison?.avg_price_diff_color || avgPriceDiffColorRule(runData?.stats?.live?.current_average_price, runData?.stats?.backtest?.current_average_price);
  const alignedPnl = preparePnlSeries(runData?.pnl_chart?.backtest || [], runData?.pnl_chart?.live || [], normalizePnl);
  const pnlDomain = computeSharedDomain([
    { data: alignedPnl.backtest },
    { data: alignedPnl.live },
  ]);

  const debugLevel = liveInspect?.last_debug_event?.level || liveStatus?.last_debug_event?.level || 'unknown';
  const debugLevelColor = DEBUG_LEVEL_COLOR_MAP[String(debugLevel).toLowerCase()] || '#64748b';

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
        {inspect && (
          <pre style={{ background: '#f8fafc', padding: 8, borderRadius: 6, overflowX: 'auto' }}>{JSON.stringify(inspect, null, 2)}</pre>
        )}
      </div>

      <div style={{ border: '1px solid #d1d5db', borderRadius: 8, padding: 12, background: '#fff' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <h4 style={{ margin: 0 }}>Live runner sessions</h4>
          <button onClick={() => refreshLiveSessions(false)}>Refresh sessions</button>
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
          <label><input type="checkbox" checked={liveAutoRefresh} onChange={e => setLiveAutoRefresh(e.target.checked)} /> Auto refresh live status</label>
        </div>
        {!liveSessions.length && <div style={{ marginTop: 8, color: '#64748b' }}>No live sessions</div>}
      </div>

      {!!selectedLivePath && (
        <>
          <div style={{ border: '1px solid #d1d5db', borderRadius: 8, padding: 12, background: '#fff' }}>
            <h4 style={{ marginTop: 0 }}>Live session summary</h4>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
              <button onClick={() => refreshLiveSessions()} disabled={liveLoading}>Refresh session list</button>
              <button onClick={() => loadLiveStatus()} disabled={liveLoading || !selectedLivePath}>Load session status</button>
              <button onClick={() => loadLiveChart()} disabled={liveLoading || !selectedLivePath}>Load live chart overlay</button>
            </div>
            {liveError && <p style={{ color: '#dc2626' }}>{liveError}</p>}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, fontSize: 13 }}>
              <div><strong>Session path:</strong> {selectedLivePath}</div>
              <div><strong>Status:</strong> {statusBadge(liveStatus?.status || liveInspect?.status || selectedLiveEntry?.status)}</div>
              <div><strong>Started:</strong> {fmtTs(liveInspect?.started_at || liveStatus?.started_at)}</div>
              <div><strong>Last updated:</strong> {fmtTs(liveStatus?.updated_at || liveInspect?.updated_at || selectedLiveEntry?.updated_at)}</div>
              <div><strong>Exchange:</strong> {liveStatus?.exchange || liveInspect?.exchange || selectedLiveEntry?.exchange || '—'}</div>
              <div><strong>Timeframe:</strong> {liveStatus?.timeframe || liveInspect?.timeframe || selectedLiveEntry?.timeframe || '—'}</div>
              <div><strong>Current open legs:</strong> {Number(liveStatus?.open_legs ?? liveInspect?.open_legs ?? 0)}</div>
              <div><strong>Total filled orders:</strong> {Number(liveStatus?.filled_orders ?? liveInspect?.filled_orders ?? 0)}</div>
              <div>
                <strong>Last debug event:</strong>{' '}
                <span style={{ color: debugLevelColor, fontWeight: 700 }}>
                  {String(debugLevel)} / {liveInspect?.last_debug_event?.event_type || liveStatus?.last_debug_event?.event_type || 'unknown'}
                </span>
              </div>
              <div><strong>Last equity timestamp:</strong> {fmtTs(liveInspect?.last_equity_ts || liveStatus?.last_equity_ts)}</div>
            </div>
            {liveLastRefreshTs && <div style={{ marginTop: 8, fontSize: 12, color: '#475569' }}>Last live refresh: {liveLastRefreshTs}</div>}
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <h4 style={{ marginBottom: 0 }}>Live charts</h4>
            {liveSeries.live.length ? (
              <LineChart title="Live equity / PNL" series={[{ name: 'Live equity', color: '#16a34a', data: liveSeries.live }]} />
            ) : (
              <div style={{ border: '1px dashed #cbd5e1', borderRadius: 8, padding: 10, color: '#64748b' }}>No chart data</div>
            )}

            {liveSeries.live.length && liveSeries.backtest.length ? (
              <LineChart
                title="Live vs Backtest overlay"
                series={[
                  { name: 'Live', color: '#16a34a', data: liveSeries.live },
                  { name: 'Backtest', color: '#2563eb', data: liveSeries.backtest },
                ]}
              />
            ) : (
              <div style={{ border: '1px dashed #cbd5e1', borderRadius: 8, padding: 10, color: '#64748b' }}>Overlay unavailable (requires both live and backtest series)</div>
            )}

            {liveSeries.distance.length > 0 && (
              <LineChart title="Absolute distance" series={[{ name: 'Absolute distance', color: '#dc2626', data: liveSeries.distance }]} />
            )}
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
              liveLoading,
            }, null, 2)}
          </pre>
        )}
      </div>
    </div>
  );
}
