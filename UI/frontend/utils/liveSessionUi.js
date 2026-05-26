import { sortSeriesByTs } from './backtestValidation.js';

export function normalizeLiveTableRows(raw) {
  if (!Array.isArray(raw)) return [];
  return raw.map((row, idx) => (row && typeof row === 'object' && !Array.isArray(row) ? row : { value: row, index: idx }));
}

export function inferLiveTableColumns(rows, maxCols = 10) {
  const normalized = normalizeLiveTableRows(rows);
  const score = new Map();
  for (const row of normalized) {
    Object.keys(row).forEach((k, i) => {
      const weight = i === 0 ? 3 : 1;
      score.set(k, (score.get(k) || 0) + weight);
    });
  }
  return Array.from(score.entries())
    .sort((a, b) => b[1] - a[1])
    .map(([k]) => k)
    .slice(0, maxCols);
}

export function formatLiveTableCell(key, value) {
  if (value == null) return '—';
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(6);
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  const k = String(key || '').toLowerCase();
  if (/(^|_)(ts|time|timestamp|date)$/.test(k) || k.includes('time')) {
    const dt = new Date(text);
    if (Number.isFinite(dt.getTime())) return dt.toISOString().replace('T', ' ').slice(0, 19);
  }
  return text;
}

export function normalizeChartSeries(points) {
  return sortSeriesByTs(Array.isArray(points) ? points : [])
    .map((p) => ({ ts: String(p?.ts || ''), value: Number(p?.value) || 0 }))
    .filter((p) => p.ts);
}

export function normalizeLiveChartPayload(raw) {
  const sources = raw && typeof raw.sources === 'object' && !Array.isArray(raw.sources) ? raw.sources : {};
  const warnings = Array.isArray(raw?.warnings) ? raw.warnings.map((w) => String(w)).filter(Boolean) : [];
  return {
    live: normalizeChartSeries(raw?.live || []),
    backtest: normalizeChartSeries(raw?.backtest || []),
    distance: normalizeChartSeries(raw?.distance || []),
    sources,
    warnings,
    approximate: !!raw?.approximate || sources.live === 'session.sqlite:orders',
  };
}

export function getLiveChartVisibility(payload) {
  const data = normalizeLiveChartPayload(payload || {});
  return {
    live: data.live.length > 0,
    overlay: data.live.length > 0 && data.backtest.length > 0,
    distance: data.distance.length > 0,
  };
}

export function selectLiveSessionState(prev, nextLivePath) {
  return {
    ...prev,
    selectedLivePath: nextLivePath,
    liveInspect: null,
    liveStatus: null,
    liveCharts: null,
    liveTables: { open_positions: [], orders: [], debug_events: [], stdio: [] },
  };
}

export function buildRefreshSessionPlan(selectedLivePath) {
  if (!selectedLivePath) return [];
  return [
    { method: 'POST', url: '/api/backtest_live_validation/live_session/inspect', body: { path: selectedLivePath } },
    { method: 'GET', url: `/api/backtest_live_validation/live_session/status?path=${encodeURIComponent(selectedLivePath)}` },
  ];
}

export function shouldShowNoLiveSessions(sessions, loading) {
  return !loading && (!Array.isArray(sessions) || sessions.length === 0);
}

export function buildLiveSummaryModel(selectedLivePath, selectedLiveEntry, inspect, status) {
  const src = status || inspect || selectedLiveEntry || {};
  const debug = src.last_debug_event && typeof src.last_debug_event === 'object' ? src.last_debug_event : {};
  return {
    sessionPath: selectedLivePath || '—',
    status: src.status || 'unknown',
    startedAt: src.started_at || null,
    updatedAt: src.updated_at || selectedLiveEntry?.updated_at || null,
    exchange: src.exchange || selectedLiveEntry?.exchange || '—',
    timeframe: src.timeframe || selectedLiveEntry?.timeframe || '—',
    openLegs: Number.isFinite(Number(src.open_legs)) ? Number(src.open_legs) : 0,
    filledOrders: Number.isFinite(Number(src.filled_orders)) ? Number(src.filled_orders) : 0,
    debugLevel: debug.level || 'unknown',
    debugType: debug.event_type || 'unknown',
    lastEquityTs: src.last_equity_ts || null,
  };
}

export function applyLiveAutoRefreshToggle(state, enabled) {
  return { ...state, liveAutoRefresh: !!enabled };
}
