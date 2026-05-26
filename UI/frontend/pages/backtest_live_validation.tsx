import { type CSSProperties, useEffect, useMemo, useRef, useState } from 'react';
import { ColorType, CrosshairMode, createChart, LineSeries } from 'lightweight-charts';
import TradingViewLiveChart from '../components/TradingViewLiveChart';
import { apiFetch } from '../utils/api';
import {
  avgPriceDiffColorRule,
  computePollingIntervalMs,
  computeSharedDomain,
  preparePnlSeries,
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
  display_name?: string | null;
  path: string;
  root_label?: string | null;
  stale_duplicate?: boolean;
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
type LiveChartPayload = {
  live?: Point[];
  backtest?: Point[];
  live_realized?: Point[];
  backtest_realized?: Point[];
  backtest_price?: Point[];
  price_bars?: { ts: string; open: number; high: number; low: number; close: number }[];
  distance?: Point[];
  mark?: Point[];
  markers?: any[];
  labels?: any[];
  sources?: Record<string, string | null>;
  warnings?: string[];
  approximate?: boolean;
};
type LiveTableKind = 'open_positions' | 'orders' | 'debug_events' | 'stdio';
type TableStateMap = Record<LiveTableKind, any[]>;
type TableLoadingMap = Record<LiveTableKind, boolean>;
type TableErrorMap = Record<LiveTableKind, string | null>;

const colors = {
  bg: '#070B16',
  sidebar: 'rgba(10, 16, 30, 0.92)',
  card: '#111827',
  card2: '#151C2E',
  border: '#263246',
  accent: '#22D3EE',
  accentHover: '#06B6D4',
  text: '#F8FAFC',
  muted: '#94A3B8',
  danger: '#EF4444',
  success: '#10B981',
  warning: '#FDBA74',
};

const shellStyle: CSSProperties = {
  minHeight: '100vh',
  width: '100%',
  maxWidth: '100%',
  background: colors.bg,
  color: colors.text,
  fontFamily: 'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
  overflowX: 'hidden',
};

const sidebarStyle: CSSProperties = {
  position: 'fixed',
  left: 0,
  top: 0,
  bottom: 0,
  width: 64,
  background: colors.sidebar,
  borderRight: `1px solid ${colors.border}`,
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'center',
  padding: '18px 0',
  zIndex: 20,
  backdropFilter: 'blur(12px)',
};

const logoStyle: CSSProperties = {
  width: 34,
  height: 34,
  borderRadius: 8,
  background: `linear-gradient(135deg, ${colors.accent}, #0EA5E9 54%, #8B5CF6)`,
  color: '#03131A',
  display: 'grid',
  placeItems: 'center',
  fontWeight: 1000,
  letterSpacing: 0,
  boxShadow: '0 0 28px rgba(34, 211, 238, 0.28)',
};

const navIconStyle: CSSProperties = {
  width: 34,
  height: 34,
  borderRadius: 8,
  display: 'grid',
  placeItems: 'center',
  color: colors.muted,
  fontSize: 12,
  fontWeight: 900,
};

const activeIconStyle: CSSProperties = {
  ...navIconStyle,
  color: colors.accent,
  background: 'rgba(34, 211, 238, 0.12)',
  boxShadow: '0 0 22px rgba(34, 211, 238, 0.18)',
};

const topbarStyle: CSSProperties = {
  height: 60,
  borderBottom: `1px solid ${colors.border}`,
  background: 'rgba(10, 16, 30, 0.84)',
  display: 'flex',
  alignItems: 'center',
  padding: '0 28px 0 92px',
  gap: 24,
  backdropFilter: 'blur(16px)',
};

const navLinkStyle: CSSProperties = {
  color: colors.muted,
  fontSize: 13,
  fontWeight: 700,
  textDecoration: 'none',
};

const activeNavLinkStyle: CSSProperties = {
  ...navLinkStyle,
  color: colors.accent,
  borderBottom: `2px solid ${colors.accent}`,
  padding: '22px 0 18px',
};

const mainStyle: CSSProperties = {
  width: '100%',
  maxWidth: '100%',
  minWidth: 0,
  overflowX: 'hidden',
};

const labelStyle: CSSProperties = {
  display: 'block',
  color: '#CBD5E1',
  fontSize: 12,
  fontWeight: 800,
  marginBottom: 8,
};

const sectionTitleStyle: CSSProperties = {
  margin: 0,
  color: colors.text,
  fontSize: 16,
  fontWeight: 950,
};

const fieldHintStyle: CSSProperties = {
  marginTop: 8,
  color: colors.muted,
  fontSize: 12,
  lineHeight: 1.45,
};

const cardStyle: CSSProperties = {
  background: 'linear-gradient(180deg, rgba(17, 24, 39, 0.98), rgba(13, 20, 34, 0.98))',
  border: `1px solid ${colors.border}`,
  borderRadius: 8,
  boxShadow: '0 22px 60px rgba(0, 0, 0, 0.32)',
  width: '100%',
  maxWidth: '100%',
  minWidth: 0,
  overflow: 'hidden',
};

const inputStyle: CSSProperties = {
  width: '100%',
  height: 40,
  borderRadius: 6,
  border: `1px solid ${colors.border}`,
  background: '#0B1220',
  color: colors.text,
  padding: '0 12px',
  outline: 'none',
  boxSizing: 'border-box',
};

const buttonStyle: CSSProperties = {
  height: 40,
  borderRadius: 6,
  border: `1px solid ${colors.border}`,
  background: colors.card2,
  color: colors.text,
  padding: '0 16px',
  fontWeight: 800,
  cursor: 'pointer',
};

const primaryButtonStyle: CSSProperties = {
  ...buttonStyle,
  border: `1px solid ${colors.accent}`,
  background: `linear-gradient(180deg, ${colors.accent}, ${colors.accentHover})`,
  color: '#041018',
  boxShadow: '0 0 28px rgba(34, 211, 238, 0.22)',
};

const preStyle: CSSProperties = {
  color: '#CBD5E1',
  background: '#0B1220',
  border: `1px solid ${colors.border}`,
  padding: 12,
  borderRadius: 6,
  overflow: 'auto',
};

function toTradingViewLineData(points: Point[]) {
  const byTime = new Map<number, { time: number; value: number }>();
  points.forEach((point, index) => {
    const parsed = Date.parse(String(point.ts || ''));
    const time = Number.isFinite(parsed) ? Math.floor(parsed / 1000) : index + 1;
    const value = Number(point.value);
    if (Number.isFinite(value)) byTime.set(time, { time, value });
  });
  return Array.from(byTime.values()).sort((a, b) => a.time - b.time);
}

function LineChart({ title, series }: { title: string; series: { name: string; color: string; data: Point[] }[] }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const domain = computeSharedDomain(series);
  const hasData = domain.pointCount > 0;

  useEffect(() => {
    if (!containerRef.current || !hasData) return;
    const el = containerRef.current;
    el.replaceChildren();

    const chart = createChart(el, {
      autoSize: true,
      height: 340,
      layout: {
        background: { type: ColorType.Solid, color: '#0B1220' },
        textColor: colors.muted,
      },
      grid: {
        vertLines: { color: '#1E293B' },
        horzLines: { color: '#1E293B' },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: 'rgba(148, 163, 184, 0.55)', labelBackgroundColor: '#111827' },
        horzLine: { color: 'rgba(148, 163, 184, 0.55)', labelBackgroundColor: '#111827' },
      },
      rightPriceScale: {
        borderColor: '#334155',
        scaleMargins: { top: 0.12, bottom: 0.12 },
      },
      timeScale: {
        borderColor: '#334155',
        timeVisible: true,
        secondsVisible: false,
      },
      handleScale: {
        axisPressedMouseMove: true,
        mouseWheel: true,
        pinch: true,
      },
      handleScroll: {
        mouseWheel: true,
        pressedMouseMove: true,
        horzTouchDrag: true,
        vertTouchDrag: false,
      },
    });

    series.forEach((item) => {
      const tvSeries = chart.addSeries(LineSeries, {
        color: item.color,
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: true,
        title: item.name,
      });
      tvSeries.setData(toTradingViewLineData(item.data) as any);
    });
    chart.timeScale().fitContent();

    return () => {
      chart.remove();
    };
  }, [hasData, title, series]);

  if (!hasData) return <div style={{ border: `1px dashed ${colors.border}`, borderRadius: 8, padding: 14, color: colors.muted, background: '#0B1220' }}>{title}: No data</div>;

  return (
    <div style={{ ...cardStyle, padding: 16 }}>
      <h4 style={{ marginTop: 0, color: colors.text }}>{title}</h4>
      <div ref={containerRef} style={{ width: '100%', height: 340, minWidth: 0 }} />
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
const PAGE_VERSION = 'v2026.05.26-zeno3-chart';

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
    display_name: raw?.display_name != null ? String(raw.display_name) : null,
    path: String(raw?.path || ''),
    root_label: raw?.root_label != null ? String(raw.root_label) : null,
    stale_duplicate: !!raw?.stale_duplicate,
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
  const autoLoadedLivePathRef = useRef('');
  const autoChartRetryRef = useRef<Record<string, number>>({});

  const pollingIntervalMs = useMemo(() => computePollingIntervalMs(inspect?.bar_interval_seconds || runData?.inspect?.bar_interval_seconds || 60), [inspect?.bar_interval_seconds, runData?.inspect?.bar_interval_seconds]);
  const livePollingIntervalMs = useMemo(() => computePollingIntervalMs(liveStatus?.open_legs ? 15 : 20), [liveStatus?.open_legs]);

  const selectedLiveEntry = useMemo(() => liveSessions.find(s => s.path === selectedLivePath) || null, [liveSessions, selectedLivePath]);

  const liveSeries = useMemo(() => {
    const norm = normalizeLiveChartPayload(liveCharts || {});
    return { live: norm.live || [], backtest: norm.backtest || [], distance: norm.distance || [] };
  }, [liveCharts]);
  const liveChartMeta = useMemo(() => normalizeLiveChartPayload(liveCharts || {}), [liveCharts]);
  const liveChartHasAnyData = !!(
    liveChartMeta.live?.length ||
    liveChartMeta.backtest?.length ||
    liveChartMeta.price_bars?.length ||
    liveChartMeta.mark?.length
  );

  async function refreshLiveSessions(keepPath = true) {
    setLiveListLoading(true);
    setLiveListError(null);
    try {
      const r = await apiFetch('/api/backtest_live_validation/live_sessions');
      if (!r.ok) throw new Error('live sessions load failed');
      const d = await r.json().catch(() => ({}));
      const sessions = (Array.isArray(d?.sessions) ? d.sessions : []).map(normalizeLiveSessionEntry).filter((s: LiveSessionEntry) => !!s.path);
      setLiveSessions(sessions);
      const hasSelectedPath = selectedLivePath && sessions.some((s: LiveSessionEntry) => s.path === selectedLivePath);
      if (!selectedLivePath || !hasSelectedPath) {
        setSelectedLivePath(sessions[0]?.path || '');
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
      .then(d => {
        const nextFiles = Array.isArray(d?.files) ? d.files : [];
        setFiles(nextFiles);
        if (!selectedPath && nextFiles[0]?.path) inspectPath(nextFiles[0].path);
      })
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

  useEffect(() => {
    if (!selectedLivePath || autoLoadedLivePathRef.current === selectedLivePath) return;
    autoLoadedLivePathRef.current = selectedLivePath;
    autoChartRetryRef.current[selectedLivePath] = 0;
    Promise.allSettled([
      inspectLiveSession(selectedLivePath),
      loadLiveStatus(selectedLivePath, { silent: true }),
    ]).finally(() => {
      loadLiveChart(selectedLivePath);
    });
  }, [selectedLivePath]);

  useEffect(() => {
    if (!selectedLivePath || liveChartHasAnyData || liveChartLoading) return;
    const attempts = autoChartRetryRef.current[selectedLivePath] || 0;
    if (attempts >= 3) return;
    autoChartRetryRef.current[selectedLivePath] = attempts + 1;
    const timer = setTimeout(() => loadLiveChart(selectedLivePath), 1200);
    return () => clearTimeout(timer);
  }, [selectedLivePath, liveChartHasAnyData, liveChartLoading]);

  useEffect(() => {
    if (!selectedPath || !selectedLivePath || !liveCharts) return;
    loadLiveChart(selectedLivePath);
  }, [selectedPath]);

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
      const backtestParam = selectedPath ? `&backtest_path=${encodeURIComponent(selectedPath)}` : '';
      const r = await apiFetch(`/api/backtest_live_validation/live_session/chart?path=${encodeURIComponent(path)}${backtestParam}`);
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
    const payload = {
      path: selectedPath,
      auto_fetch_live: !selectedLivePath,
      live_path: selectedLivePath || undefined,
      run_match: true,
      debug: debugMode,
    };
    setLastPayload(payload);
    try {
      const r = await apiFetch('/api/backtest_live_validation/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        let detail = '';
        try {
          const body = await r.json();
          const d = body?.detail ?? body;
          detail = typeof d === 'string' ? d : JSON.stringify(d);
        } catch {
          detail = await r.text().catch(() => '');
        }
        throw new Error(detail || `run failed (HTTP ${r.status})`);
      }
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
    return <span style={{ background: `${bg}22`, color: bg, border: `1px solid ${bg}66`, borderRadius: 999, padding: '3px 9px', fontWeight: 900, textTransform: 'uppercase', fontSize: 11 }}>{key}</span>;
  }

  function renderLiveTable(kind: LiveTableKind, title: string, emptyLabel: string) {
    const rows = liveTables[kind] || [];
    const loading = liveTableLoading[kind];
    const err = liveTableErrors[kind];
    const columns = inferLiveTableColumns(rows);
    return (
      <details key={kind} style={{ ...cardStyle, padding: 14 }}>
        <summary style={{ cursor: 'pointer', fontWeight: 900, color: colors.text }}>{title}</summary>
        <div style={{ marginTop: 8, marginBottom: 8 }}>
          <button style={buttonStyle} disabled={!selectedLivePath || loading} onClick={() => loadLiveTable(kind)}>{loading ? 'Loading...' : `Refresh ${title.toLowerCase()}`}</button>
        </div>
        {err && <div style={{ color: colors.danger, fontSize: 12, marginBottom: 6 }}>{err}</div>}
        {!rows.length ? (
          <div style={{ color: colors.muted, fontSize: 13 }}>{emptyLabel}</div>
        ) : (
          <div className="run-table-wrapper" style={{ maxHeight: 320, overflow: 'auto', border: `1px solid ${colors.border}`, borderRadius: 6 }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
              <thead>
                <tr>
                  {columns.map(col => (
                    <th key={col} style={{ textAlign: 'left', background: colors.card2, color: colors.muted, borderBottom: `1px solid ${colors.border}`, padding: '8px 10px', position: 'sticky', top: 0 }}>
                      {col}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row: any, rowIdx: number) => (
                  <tr key={`row-${rowIdx}`} style={{ borderBottom: `1px solid ${colors.border}` }}>
                    {columns.map(col => (
                      <td key={`${rowIdx}-${col}`} style={{ padding: '8px 10px', color: '#CBD5E1', verticalAlign: 'top', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}>
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
  const liveChartSourceLabel = liveChartMeta.sources?.live ? `Source: ${liveChartMeta.sources.live}` : null;
  const summaryModel = buildLiveSummaryModel(selectedLivePath, selectedLiveEntry, liveInspect, liveStatus);
  const debugLevelColor = DEBUG_LEVEL_COLOR_MAP[String(summaryModel.debugLevel).toLowerCase()] || '#64748b';

  return (
    <div style={shellStyle}>
      <aside style={sidebarStyle}>
        <a href="/run" style={{ textDecoration: 'none' }} title="Run Backtest">
          <div style={logoStyle}>S</div>
        </a>
        <div style={{ marginTop: 36, display: 'flex', flexDirection: 'column', gap: 16 }}>
          <a href="/run" style={{ textDecoration: 'none' }} title="Run Backtest"><div style={navIconStyle}>RN</div></a>
          <a href="/backtest_live_validation" style={{ textDecoration: 'none' }} title="Backtest vs Live"><div style={activeIconStyle}>VL</div></a>
          <div style={navIconStyle}>CR</div>
          <div style={navIconStyle}>PR</div>
        </div>
        <div style={{ marginTop: 'auto', ...navIconStyle }}>ST</div>
      </aside>

      <header style={topbarStyle}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginRight: 4 }}>
          <span style={{ ...logoStyle, width: 26, height: 26, borderRadius: 7, fontSize: 13 }}>S</span>
          <span style={{ color: colors.text, fontWeight: 950, fontSize: 15 }}>Step</span>
        </div>
        <a href="/run" style={navLinkStyle}>Run Backtest</a>
        <a href="/backtest_live_validation" style={activeNavLinkStyle}>Backtest vs Live</a>
        <span style={navLinkStyle}>Circulators</span>
        <span style={navLinkStyle}>Product</span>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 18, alignItems: 'center', color: colors.muted }}>
          <span>Globe</span>
          <span>Alerts</span>
          <span style={{ width: 30, height: 30, borderRadius: '50%', background: colors.card2, display: 'grid', placeItems: 'center', border: `1px solid ${colors.border}` }}>U</span>
        </div>
      </header>

      <main className="run-page-content" style={mainStyle}>
      <div style={{ marginBottom: 26 }}>
        <div style={{ color: colors.accent, fontSize: 12, fontWeight: 900, textTransform: 'uppercase', letterSpacing: 0 }}>
          Backtests
        </div>
        <h1 style={{ margin: '8px 0 8px', fontSize: 30, lineHeight: 1.1, color: colors.text }}>
          Backtest vs Live Validation
        </h1>
        <p style={{ margin: 0, color: colors.muted, maxWidth: 720, lineHeight: 1.55 }}>
          Порівняння результатів історичного backtest із live runner session: графіки PNL, margin usage, slippage, статуси і raw debug для перевірки розбіжностей.
        </p>
        <div style={{ marginTop: 8, color: colors.muted, fontSize: 12 }}>Page version: <strong>{PAGE_VERSION}</strong></div>
      </div>

      <div className="run-backtest-grid">
        <section style={{ display: 'flex', flexDirection: 'column', gap: 16, minWidth: 0 }}>

      <div style={{ ...cardStyle, padding: 16 }}>
        <h4 style={sectionTitleStyle}>Source selection</h4>
        <div style={fieldHintStyle}>Оберіть TradingView CSV, з якого будується порівняння з live сесією.</div>
        <label style={{ ...labelStyle, marginTop: 14 }}>TradingView source</label>
        <select value={selectedPath} onChange={e => inspectPath(e.target.value)} style={inputStyle}>
          <option value="">--select TradingView source--</option>
          {files.map(f => <option key={f.path} value={f.path}>{f.name}</option>)}
        </select>
        <div style={{ marginTop: 10 }}>
          <input style={{ ...inputStyle, height: 'auto', padding: 10 }} type="file" accept=".csv" onChange={e => e.target.files?.[0] && onUpload(e.target.files[0])} />
        </div>
        {inspect && <pre style={preStyle}>{JSON.stringify(inspect, null, 2)}</pre>}
      </div>

      <div style={{ ...cardStyle, padding: 16 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <h4 style={sectionTitleStyle}>Live runner sessions</h4>
          <button style={buttonStyle} onClick={() => refreshLiveSessions(false)} disabled={liveListLoading}>{liveListLoading ? 'Refreshing...' : 'Refresh sessions list'}</button>
        </div>
        <div style={fieldHintStyle}>Показує тільки локальні runner sessions із `_reports/_live`; статус та час оновлення допомагають вибрати актуальну сесію.</div>
        <div style={{ marginTop: 14, display: 'flex', flexDirection: 'column', gap: 12 }}>
          <select value={selectedLivePath} onChange={e => inspectLiveSession(e.target.value)} style={inputStyle}>
            <option value="">--select live session from _reports/_live--</option>
            {liveSessions.map(session => (
              <option key={session.path} value={session.path}>
                {session.display_name || session.name || session.path}{session.stale_duplicate ? ' | stale duplicate' : ''} | ex: {session.exchange || 'n/a'} | tf: {session.timeframe || 'n/a'} | updated: {fmtTs(session.updated_at)} | status: {session.status || 'unknown'}
              </option>
            ))}
          </select>
          <label style={{ color: '#CBD5E1', fontSize: 13 }}><input type="checkbox" checked={liveAutoRefresh} onChange={e => setLiveAutoRefresh(applyLiveAutoRefreshToggle({ liveAutoRefresh }, e.target.checked).liveAutoRefresh)} /> Auto refresh live status</label>
        </div>
        {liveListError && <div style={{ marginTop: 8, color: colors.danger }}>{liveListError}</div>}
        {shouldShowNoLiveSessions(liveSessions, liveListLoading) && <div style={{ marginTop: 8, color: colors.muted }}>No live sessions</div>}
      </div>

        </section>

        <section className="run-right-panel" style={{ display: 'flex', flexDirection: 'column', gap: 16, minWidth: 0 }}>
      {!!selectedLivePath && (
        <>
          <div style={{ ...cardStyle, padding: 16 }}>
            <h4 style={sectionTitleStyle}>Live session summary</h4>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
              <button style={buttonStyle} onClick={refreshSelectedLiveSession} disabled={liveInspectLoading || liveStatusLoading}>Refresh session</button>
              <button style={buttonStyle} onClick={() => loadLiveStatus()} disabled={liveStatusLoading || !selectedLivePath}>{liveStatusLoading ? 'Loading status...' : 'Load session status'}</button>
              <button style={primaryButtonStyle} onClick={() => loadLiveChart()} disabled={liveChartLoading || !selectedLivePath}>{liveChartLoading ? 'Loading charts...' : 'Load live chart overlay'}</button>
            </div>
            {liveInspectError && <p style={{ color: colors.danger }}>{liveInspectError}</p>}
            {liveStatusError && <p style={{ color: colors.danger }}>{liveStatusError}</p>}
            {liveChartError && <p style={{ color: colors.danger }}>{liveChartError}</p>}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 10, fontSize: 13, color: '#CBD5E1' }}>
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
            {liveLastRefreshTs && <div style={{ marginTop: 8, fontSize: 12, color: colors.muted }}>Last live refresh: {liveLastRefreshTs}</div>}
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <h4 style={sectionTitleStyle}>Live charts</h4>
            {(liveChartMeta.approximate || liveChartMeta.warnings?.length || liveChartSourceLabel) && (
              <div style={{ border: `1px solid ${liveChartMeta.approximate ? colors.warning : colors.border}`, borderRadius: 8, padding: 10, color: liveChartMeta.approximate ? colors.warning : colors.muted, background: '#0B1220', fontSize: 12, lineHeight: 1.45 }}>
                {liveChartMeta.approximate ? 'Orders-only fallback: approximate notional chart, not real PnL.' : liveChartSourceLabel}
                {liveChartMeta.warnings?.length ? ` ${liveChartMeta.warnings.join(' ')}` : ''}
              </div>
            )}
            {liveChartHasAnyData ? (
              <TradingViewLiveChart payload={liveChartMeta} />
            ) : (
              <div style={{ border: `1px dashed ${colors.border}`, borderRadius: 8, padding: 10, color: colors.muted, background: '#0B1220' }}>No chart data</div>
            )}
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <h4 style={sectionTitleStyle}>Session details tables</h4>
            {LIVE_TABLE_DEFS.map(def => renderLiveTable(def.kind, def.title, def.emptyLabel))}
          </div>
        </>
      )}

      <div style={{ ...cardStyle, padding: 16, display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
        <button style={primaryButtonStyle} onClick={runComparison} disabled={!selectedPath || loading}>{loading ? 'Running...' : 'Run / Refresh comparison'}</button>
        <label style={{ color: '#CBD5E1', fontSize: 13 }}><input type="checkbox" checked={autoRefresh} onChange={e => setAutoRefresh(e.target.checked)} /> Auto refresh</label>
        <label style={{ color: '#CBD5E1', fontSize: 13 }}><input type="checkbox" checked={debugMode} onChange={e => setDebugMode(e.target.checked)} /> Debug mode</label>
        <label style={{ color: '#CBD5E1', fontSize: 13 }}><input type="checkbox" checked={normalizePnl} onChange={e => setNormalizePnl(e.target.checked)} /> Normalize both to first common point</label>
        <span style={{ color: colors.muted, fontSize: 12 }}>Polling interval: {pollingIntervalMs} ms</span>
        {lastRefreshTs && <span style={{ color: colors.muted, fontSize: 12 }}>Last refresh: {lastRefreshTs}</span>}
      </div>

      {error && <p style={{ color: colors.danger }}>{error}</p>}

      {runData && (
        <>
          {runData?.fallback_canvas_url && (
            <details style={{ ...cardStyle, padding: 16 }} open>
              <summary style={{ cursor: 'pointer', fontWeight: 900, color: colors.text }}>Python matcher canvas (reference)</summary>
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
          <div style={{ border: `1px dashed ${colors.border}`, borderRadius: 8, padding: 10, fontSize: 12, color: colors.muted, background: '#0B1220' }}>
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

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 12 }}>
            <pre style={preStyle}>Backtest\n{JSON.stringify(runData?.stats?.backtest || {}, null, 2)}</pre>
            <pre style={preStyle}>Live\n{JSON.stringify(runData?.stats?.live || {}, null, 2)}</pre>
          </div>
          <div style={{ ...cardStyle, padding: 16 }}>
            <strong>Comparison</strong>
            <pre style={preStyle}>{JSON.stringify(runData?.stats?.comparison || {}, null, 2)}</pre>
            <div style={{ color: avgDiffColor, fontWeight: 700 }}>
              Avg price diff: {Number(runData?.stats?.comparison?.avg_price_diff || 0).toFixed(6)} ({avgDiffColor})
            </div>
          </div>
        </>
      )}

      <div style={{ ...cardStyle, padding: 16 }}>
        <button style={buttonStyle} onClick={() => setDebugOpen(v => !v)}>{debugOpen ? 'Hide debug' : 'Show debug'}</button>
        {debugOpen && (
          <pre style={{ ...preStyle, maxHeight: 360 }}>
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
        </section>
      </div>
      </main>
    </div>
  );
}
