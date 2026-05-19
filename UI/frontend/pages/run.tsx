import { type CSSProperties, useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/router';
import { apiFetch } from '../utils/api';

type CacheDbOption = {
  name: string;
  path: string;
  kind?: string;
  tables?: string[];
};

type BacktesterCapability = Record<string, boolean>;

type ErrorDetail = {
  kind?: string;
  title?: string;
  message?: string;
  advice?: string;
  exit_code?: string;
};

type JobStatus = {
  status?: string;
  message?: string;
  progress?: number;
  expected_duration_seconds?: number;
  eta_seconds?: number;
  elapsed_seconds?: number;
  symbol_count?: number;
  limit_bars?: number;
  cfg_name?: string;
  override?: Record<string, any>;
  cache_db_label?: string;
  cache_db?: string;
  backtester?: string;
  universe_file?: string;
  error_detail?: ErrorDetail;
};

const spinnerFrames = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];

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
  warningBg: 'rgba(239, 68, 68, 0.12)',
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
  gap: 26,
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

const labelStyle: CSSProperties = {
  display: 'block',
  color: '#CBD5E1',
  fontSize: 12,
  fontWeight: 800,
  marginBottom: 8,
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

const disabledButtonStyle: CSSProperties = {
  ...primaryButtonStyle,
  opacity: 0.48,
  cursor: 'not-allowed',
  boxShadow: 'none',
};

const toggleTrack = (checked: boolean): CSSProperties => ({
  width: 40,
  height: 22,
  borderRadius: 999,
  background: checked ? colors.accent : '#334155',
  border: `1px solid ${checked ? '#67E8F9' : '#475569'}`,
  position: 'relative',
  display: 'inline-block',
});

const toggleKnob = (checked: boolean): CSSProperties => ({
  width: 16,
  height: 16,
  borderRadius: '50%',
  background: '#F8FAFC',
  position: 'absolute',
  top: 2,
  left: checked ? 20 : 3,
});

function Toggle({ checked, onChange }: { checked: boolean; onChange: (checked: boolean) => void }) {
  return (
    <button type="button" onClick={() => onChange(!checked)} style={{ border: 0, background: 'transparent', padding: 0, cursor: 'pointer' }}>
      <span style={toggleTrack(checked)}>
        <span style={toggleKnob(checked)} />
      </span>
    </button>
  );
}

function RealResultChart({ plotUrl }: { plotUrl?: string }) {
  if (plotUrl) {
    return (
      <div className="run-chart-wrapper" style={{ width: '100%', overflow: 'hidden', borderRadius: 6, background: '#0B1220' }}>
        <img src={plotUrl} style={{ width: '100%', maxHeight: 420, objectFit: 'contain', display: 'block', borderRadius: 6 }} />
      </div>
    );
  }
  return (
    <div style={{
      minHeight: 300,
      borderRadius: 8,
      border: `1px dashed ${colors.border}`,
      background: '#0B1220',
      display: 'grid',
      placeItems: 'center',
      textAlign: 'center',
      color: colors.muted,
      padding: 24,
    }}>
      <div>
        <div style={{ color: colors.text, fontWeight: 900, marginBottom: 8 }}>No backtest results yet</div>
        <div>Run a backtest to see equity curve, drawdown and trades visualization.</div>
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status?: string }) {
  const normalized = String(status || 'pending').toLowerCase();
  const color =
    normalized === 'done' || normalized === 'success'
      ? colors.success
      : normalized === 'running'
        ? colors.accent
        : normalized === 'error' || normalized === 'blocked'
          ? colors.danger
          : '#64748B';
  const label = normalized === 'done' ? 'success' : normalized;
  return (
    <span style={{
      display: 'inline-flex',
      alignItems: 'center',
      borderRadius: 999,
      padding: '3px 9px',
      background: `${color}22`,
      border: `1px solid ${color}55`,
      color,
      fontSize: 11,
      fontWeight: 900,
      textTransform: 'uppercase',
    }}>
      {label}
    </span>
  );
}

function ErrorInsight({ detail, fallback }: { detail?: ErrorDetail; fallback?: string | null }) {
  if (!detail && !fallback) return null;
  return (
    <div style={{
      marginTop: 12,
      padding: 14,
      borderRadius: 8,
      border: '1px solid rgba(239,68,68,0.35)',
      background: colors.warningBg,
      color: '#FCA5A5',
      fontSize: 13,
      lineHeight: 1.45,
    }}>
      <div style={{ color: '#FECACA', fontWeight: 950, marginBottom: 6 }}>
        {detail?.title || 'Backtester error'}
      </div>
      <div>{detail?.message || fallback}</div>
      {detail?.advice && (
        <div style={{ marginTop: 8, color: '#FDBA74' }}>
          {detail.advice}
        </div>
      )}
      {detail?.exit_code && (
        <div style={{ marginTop: 8, color: colors.muted }}>
          Exit code: <code>{detail.exit_code}</code>
        </div>
      )}
    </div>
  );
}

function cacheKind(cache?: CacheDbOption | null, rawPath = '') {
  return cache?.kind || (rawPath.toLowerCase().endsWith('.npz') ? 'npz' : rawPath ? 'db' : '');
}

function backtesterNeedsPriceIndicators(name: string) {
  return name.startsWith('backtester_core_') || name === 'backtester_dual_long_short_mtm.py';
}

function cacheWorksWithBacktester(cache: CacheDbOption, backtesterName: string, caps: BacktesterCapability) {
  const kind = cacheKind(cache, cache.path);
  if (kind === 'npz') return !!caps.npz;
  if (caps.npz && !caps.cache_db) return false;
  if (backtesterNeedsPriceIndicators(backtesterName)) {
    return Array.isArray(cache.tables) && cache.tables.includes('price_indicators');
  }
  return !!caps.cache_db || !caps.npz;
}

function cacheAdvice(cache: CacheDbOption, backtesterName: string, caps: BacktesterCapability) {
  const kind = cacheKind(cache, cache.path);
  if (!backtesterName) {
    return kind === 'npz'
      ? 'NPZ cache. Choose an NPZ-capable backtester next.'
      : 'SQLite cache DB. Choose a DB-capable backtester next.';
  }
  if (cacheWorksWithBacktester(cache, backtesterName, caps)) {
    return `Compatible with ${backtesterName}.`;
  }
  if (kind === 'npz') return `${backtesterName} does not support NPZ input.`;
  if (caps.npz && !caps.cache_db) return `${backtesterName} requires NPZ input, not SQLite DB.`;
  if (backtesterNeedsPriceIndicators(backtesterName)) return 'This DB is missing required table price_indicators.';
  return `This cache is not compatible with ${backtesterName}.`;
}

function backtesterAdvice(backtesterName: string, caps: BacktesterCapability, cache?: CacheDbOption | null) {
  if (!cache) {
    if (caps.npz && !caps.cache_db) return 'Requires selecting an NPZ cache.';
    if (caps.cache_db) return 'Works with compatible SQLite DB cache.';
    return 'Works when config provides the data expected by this script.';
  }
  return cacheWorksWithBacktester(cache, backtesterName, caps)
    ? `Compatible with ${cache.name}.`
    : cacheAdvice(cache, backtesterName, caps);
}

export default function Run() {
  const router = useRouter();
  const [cfgs, setCfgs] = useState<any[]>([]);
  const [cfg, setCfg] = useState('');
  const [universes, setUniverses] = useState<string[]>([]);
  const [universe, setUniverse] = useState('');
  const [bars, setBars] = useState(5000);
  const [job, setJob] = useState<any>(null);
  const [jobStatus, setJobStatus] = useState<JobStatus | null>(null);
  const [res, setRes] = useState<any>(null);
  const [slide, setSlide] = useState(0);
  const [debug, setDebug] = useState(false);
  const [errMsg, setErrMsg] = useState<string | null>(null);
  const [showTrades, setShowTrades] = useState(true);
  const [logs, setLogs] = useState('');
  const [backtesters, setBacktesters] = useState<string[]>([]);
  const [backtester, setBacktester] = useState('');
  const [backtesterCaps, setBacktesterCaps] = useState<Record<string, BacktesterCapability>>({});
  const [backtesterFiles, setBacktesterFiles] = useState<Record<string, { path?: string; url?: string }>>({});
  const [cacheDbs, setCacheDbs] = useState<CacheDbOption[]>([]);
  const [cacheDb, setCacheDb] = useState('');
  const [spinnerIndex, setSpinnerIndex] = useState(0);
  const [showConfigPanel, setShowConfigPanel] = useState(false);
  const [configLoading, setConfigLoading] = useState(false);
  const [configText, setConfigText] = useState('');
  const [configOriginalText, setConfigOriginalText] = useState('');
  const [configError, setConfigError] = useState<string | null>(null);
  const [configSaveError, setConfigSaveError] = useState<string | null>(null);
  const [configSaveSuccess, setConfigSaveSuccess] = useState<string | null>(null);
  const [configSaving, setConfigSaving] = useState(false);
  const [configReloadKey, setConfigReloadKey] = useState(0);
  const lastSuggestedCfgRef = useRef('');

  const isReadOnly = typeof router.query.id === 'string';

  // if ?id=JOB_ID is present load that job's result
  useEffect(() => {
    const qid = router.query.id;
    if (qid && typeof qid === 'string') {
      setJob({ job_id: qid });
    }
    const qcache = router.query.cache_db;
    if (qcache && typeof qcache === 'string') {
      setCacheDb(qcache);
    }
  }, [router.query.id, router.query.cache_db]);

  useEffect(() => {
    apiFetch('/api/configs')
      .then(r => r.json())
      .then(setCfgs);
  }, []);

  useEffect(() => {
    apiFetch('/api/universes')
      .then(r => r.json())
      .then(data => {
        if (Array.isArray(data)) setUniverses(data);
        else setUniverses([]);
      })
      .catch(() => setUniverses([]));
  }, []);

  useEffect(() => {
    apiFetch('/api/backtesters')
      .then(r => r.json())
      .then(data => {
        setBacktesters(data.versions || []);
        setBacktesterCaps(data.capabilities || {});
        setBacktesterFiles(data.files || {});
        if (data.current) setBacktester(data.current);
      });
  }, []);

  useEffect(() => {
    apiFetch('/api/cache_dbs')
      .then(r => r.json())
      .then(data => {
        if (!Array.isArray(data)) {
          setCacheDbs([]);
          return;
        }
        const normalized: CacheDbOption[] = data
          .filter((item: any) => item && typeof item.name === 'string' && typeof item.path === 'string')
          .map((item: any) => ({ name: item.name, path: item.path, kind: item.kind, tables: Array.isArray(item.tables) ? item.tables : undefined }));
        setCacheDbs(normalized);
      })
      .catch(() => setCacheDbs([]));
  }, []);

  useEffect(() => {
    if (!cfg) {
      setConfigLoading(false);
      setConfigText('');
      setConfigOriginalText('');
      setConfigError(null);
      setConfigSaveError(null);
      setConfigSaveSuccess(null);
      return;
    }
    let cancelled = false;
    async function loadConfig() {
      setConfigLoading(true);
      setConfigError(null);
      setConfigSaveError(null);
      setConfigSaveSuccess(null);
      try {
        const resp = await apiFetch(`/api/configs/${encodeURIComponent(cfg)}`);
        let payload: any = null;
        try {
          payload = await resp.json();
        } catch (parseErr) {
          payload = null;
        }
        if (!resp.ok) {
          const detail =
            (payload && typeof payload.detail === 'string' && payload.detail) ||
            (payload && typeof payload.message === 'string' && payload.message) ||
            `Failed to load config (HTTP ${resp.status})`;
          throw new Error(detail);
        }
        if (cancelled) return;
        const text = typeof payload?.yaml_text === 'string' ? payload.yaml_text : '';
        setConfigText(text);
        setConfigOriginalText(text);
      } catch (err) {
        if (cancelled) return;
        setConfigError(err instanceof Error ? err.message : 'Failed to load config');
        setConfigText('');
        setConfigOriginalText('');
      } finally {
        if (!cancelled) {
          setConfigLoading(false);
        }
      }
    }
    loadConfig();
    return () => {
      cancelled = true;
    };
  }, [cfg, configReloadKey]);

  useEffect(() => {
    if (!cfg || isReadOnly || lastSuggestedCfgRef.current === cfg) return;
    const selectedCfg = cfgs.find((item: any) => item?.name === cfg);
    const suggestedCache = selectedCfg?.cache_db || '';
    if (selectedCfg?.backtester_file) {
      lastSuggestedCfgRef.current = cfg;
      setBacktester(selectedCfg.backtester_file);
      setCacheDb(suggestedCache);
    } else if (selectedCfg) {
      lastSuggestedCfgRef.current = cfg;
      setCacheDb(suggestedCache);
      if (suggestedCache) {
        const cacheOpt = cacheDbs.find(opt => opt.path === suggestedCache);
        const compatible = backtesters.filter(name =>
          cacheOpt ? cacheWorksWithBacktester(cacheOpt, name, backtesterCaps[name] || {}) : true
        );
        if (!compatible.includes(backtester)) {
          setBacktester(compatible[0] || '');
        }
      }
    }
  }, [cfg, cfgs, isReadOnly, cacheDbs, backtesters, backtesterCaps, backtester]);

  async function start() {
    const override: Record<string, any> = {};
    if (universe) {
      const universePath = `universe/${universe}`;
      override.symbols_file = universePath;
      override.universe_file = universePath;
      override.universe = { file: universePath };
    }
    const payloadOverride = Object.keys(override).length > 0 ? override : undefined;
    // Trim any stray whitespace. Backend resolves the value relative to the repository.
    const cacheDbPath = cacheDb.trim();
    setErrMsg(null);
    const resp = await apiFetch('/api/backtest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        cfg_name: cfg,
        limit_bars: bars,
        debug,
        override: payloadOverride,
        backtester,
        cache_db: cacheDbPath || undefined,
      }),
    });
    const j = await resp.json();
    if (!resp.ok) {
      setErrMsg(j?.detail || j?.message || `Failed to start backtest (HTTP ${resp.status})`);
      return;
    }
    setJob(j);
    setJobStatus(null);
    setSpinnerIndex(0);
    setRes(null);
    setLogs('');
  }

  async function saveConfig() {
    if (!cfg) return;
    setConfigSaving(true);
    setConfigSaveError(null);
    setConfigSaveSuccess(null);
    try {
      const resp = await apiFetch(`/api/configs/${encodeURIComponent(cfg)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ yaml_text: configText }),
      });
      let payload: any = null;
      try {
        payload = await resp.json();
      } catch (parseErr) {
        payload = null;
      }
      if (!resp.ok) {
        const detail =
          (payload && typeof payload.detail === 'string' && payload.detail) ||
          (payload && typeof payload.message === 'string' && payload.message) ||
          `Failed to save config (HTTP ${resp.status})`;
        throw new Error(detail);
      }
      setConfigOriginalText(configText);
      setConfigSaveSuccess('Saved');
    } catch (err) {
      setConfigSaveError(err instanceof Error ? err.message : 'Failed to save config');
    } finally {
      setConfigSaving(false);
    }
  }

  function resetConfig() {
    setConfigText(configOriginalText);
    setConfigSaveError(null);
    setConfigSaveSuccess(null);
  }

  function reloadConfig() {
    if (!cfg) return;
    setConfigReloadKey(prev => prev + 1);
  }

  useEffect(() => {
    if (!job) return;
    let cancelled = false;
    let interval: ReturnType<typeof setInterval> | undefined;
    const fetchStatus = async () => {
      try {
        const st: JobStatus = await apiFetch('/api/jobs/' + job.job_id + '/status').then(r =>
          r.json()
        );
        if (cancelled) return;
        setJobStatus(st);
        if (st.status === 'done' || st.status === 'error') {
          if (interval) clearInterval(interval);
          const rs = await apiFetch('/api/jobs/' + job.job_id + '/result').then(r =>
            r.json()
          );
          if (cancelled) return;
          setRes(rs);
          if (st.status === 'error') setErrMsg(st.message || 'error');
        }
      } catch (err) {
        if (!cancelled) {
          console.error('Failed to fetch job status', err);
        }
      }
    };
    fetchStatus();
    interval = setInterval(fetchStatus, 1000);
    return () => {
      cancelled = true;
      if (interval) clearInterval(interval);
    };
  }, [job]);

  useEffect(() => {
    if (res && (debug || errMsg) && res.artifacts?.['logs.txt']) {
      fetch(res.artifacts['logs.txt'])
        .then(r => r.text())
        .then(setLogs)
        .catch(() => {});
    }
  }, [res, debug, errMsg]);

  useEffect(() => {
    if (!jobStatus) return;
    const statusCfg = jobStatus.cfg_name;
    if (statusCfg) {
      if (!cfg || (isReadOnly && cfg !== statusCfg)) {
        setCfg(statusCfg);
      }
    }
    if (isReadOnly) {
      const statusCache = jobStatus.cache_db_label || jobStatus.cache_db;
      if (statusCache && cacheDb !== statusCache) {
        setCacheDb(statusCache);
      }
      const statusUniverse =
        extractUniverseName(jobStatus.override) || extractUniverseName(jobStatus.universe_file);
      if (statusUniverse && universe !== statusUniverse) {
        setUniverse(statusUniverse);
      }
      if (jobStatus.backtester && backtester !== jobStatus.backtester) {
        setBacktester(jobStatus.backtester);
      }
    }
  }, [jobStatus, isReadOnly, cfg, cacheDb, universe, backtester]);

  useEffect(() => {
    if (!res) return;
    const resultCfg = res.cfg_name;
    if (resultCfg) {
      if (!cfg || (isReadOnly && cfg !== resultCfg)) {
        setCfg(resultCfg);
      }
    }
    if (isReadOnly) {
      const resultCache = res.cache_db_label || res.cache_db;
      if (resultCache && cacheDb !== resultCache) {
        setCacheDb(resultCache);
      }
      const resultUniverse =
        extractUniverseName(res.override) || extractUniverseName(res.universe_file);
      if (resultUniverse && universe !== resultUniverse) {
        setUniverse(resultUniverse);
      }
      if (res.backtester && backtester !== res.backtester) {
        setBacktester(res.backtester);
      }
    }
  }, [res, isReadOnly, cfg, cacheDb, universe, backtester]);

  useEffect(() => {
    if (!jobStatus) {
      setSpinnerIndex(0);
      return;
    }
    const totalExpected =
      jobStatus.expected_duration_seconds ??
      (jobStatus.elapsed_seconds != null && jobStatus.eta_seconds != null
        ? jobStatus.elapsed_seconds + jobStatus.eta_seconds
        : undefined);
    const fallbackElapsed = jobStatus.elapsed_seconds ?? 0;
    const shouldAnimate =
      (jobStatus.status === 'running' || jobStatus.status === 'queued') &&
      ((totalExpected ?? fallbackElapsed) > 5);
    if (!shouldAnimate) {
      setSpinnerIndex(0);
      return;
    }
    const id = setInterval(() => {
      setSpinnerIndex(prev => (prev + 1) % spinnerFrames.length);
    }, 120);
    return () => clearInterval(id);
  }, [jobStatus]);

  const plotNames = [
    'equity_by_time.png',
    'returns_hist.png',
    'equity_by_trade.png',
    'drawdown_by_trade.png',
    'dual_equity_curve.png',
    'dual_mtm_pnl.png',
    'dual_pnl_panels_all.png',
    'dual_margin_call_excess.png',
  ];
  const namedPlotUrls = plotNames
    .map(n => res?.artifacts?.[n])
    .filter(Boolean) as string[];
  const extraPlotUrls = Object.entries(res?.artifacts || {})
    .filter(([name, url]) => name.endsWith('.png') && typeof url === 'string')
    .map(([, url]) => url as string);
  const plotUrls = Array.from(new Set([...namedPlotUrls, ...extraPlotUrls]));

  const vizNames = [
    'viz_equity_vs_trade.png',
    'viz_dd_vs_trade.png',
    'viz_equity_vs_time.png',
  ];
  const vizUrls = vizNames
    .map(n => res?.artifacts?.[n])
    .filter(Boolean) as string[];

  const progressValue =
    typeof jobStatus?.progress === 'number'
      ? Math.min(Math.max(jobStatus.progress, 0), 1)
      : null;
  const progressPercent =
    progressValue !== null ? Math.round(progressValue * 100) : null;
  const totalExpectedSeconds =
    jobStatus?.expected_duration_seconds ??
    (jobStatus?.elapsed_seconds != null && jobStatus?.eta_seconds != null
      ? jobStatus.elapsed_seconds + jobStatus.eta_seconds
      : undefined);
  const shouldShowSpinner =
    !!jobStatus &&
    (jobStatus.status === 'running' || jobStatus.status === 'queued') &&
    ((totalExpectedSeconds ?? jobStatus.elapsed_seconds ?? 0) > 5);
  const jobUniverseName =
    extractUniverseName(jobStatus?.override) || extractUniverseName(jobStatus?.universe_file);
  const jobCacheLabel = jobStatus?.cache_db_label || jobStatus?.cache_db || null;
  const configDirty = cfg !== '' && configText !== configOriginalText;

  const hasCustomCacheDb = cacheDb !== '' && !cacheDbs.some(opt => opt.path === cacheDb);
  const displayedCacheDbs: CacheDbOption[] = hasCustomCacheDb
    ? [...cacheDbs, { name: `Custom: ${cacheDb}`, path: cacheDb }]
    : cacheDbs;
  const selectedCache = displayedCacheDbs.find(opt => opt.path === cacheDb);
  const filteredBacktesters = backtesters.filter(name => {
    const caps = backtesterCaps[name] || {};
    if (selectedCache) {
      return cacheWorksWithBacktester(selectedCache, name, caps);
    }
    if (caps.npz && !caps.cache_db) {
      return displayedCacheDbs.some(opt => cacheWorksWithBacktester(opt, name, caps));
    }
    if (backtesterNeedsPriceIndicators(name)) {
      return displayedCacheDbs.some(opt => cacheWorksWithBacktester(opt, name, caps));
    }
    return true;
  });
  const selectedBacktesterCaps = backtesterCaps[backtester] || {};
  const filteredCacheDbs = displayedCacheDbs.filter(opt => {
    if (!backtester) {
      return backtesters.some(name => cacheWorksWithBacktester(opt, name, backtesterCaps[name] || {}));
    }
    return cacheWorksWithBacktester(opt, backtester, selectedBacktesterCaps);
  });
  const selectedCacheKind =
    cacheKind(selectedCache, cacheDb);
  const cacheUnsupported =
    selectedCacheKind === 'npz'
      ? !!backtester && !selectedBacktesterCaps.npz
      : selectedCacheKind === 'db'
        ? !!backtester && !cacheWorksWithBacktester(selectedCache || { name: cacheDb, path: cacheDb, kind: 'db' }, backtester, selectedBacktesterCaps)
        : false;
  const cacheRequired =
    !!backtester && !!selectedBacktesterCaps.npz && !selectedBacktesterCaps.cache_db && !cacheDb;
  const selectedCfgMeta = cfgs.find((item: any) => item?.name === cfg);
  const selectedCfgMissingCache = selectedCfgMeta?.cache_db_missing;
  const selectedBacktesterFile = backtesterFiles[backtester];
  const startDisabled = cacheUnsupported || cacheRequired || !cfg || !backtester;
  const startTooltip = cacheRequired ? 'Select an NPZ cache to start this backtest' : undefined;
  const cacheSelectAdvice = backtester
    ? `Showing only caches compatible with ${backtester}. Hover options for details.`
    : 'Showing caches that have at least one compatible backtester.';
  const backtesterSelectAdvice = selectedCache
    ? `Showing only backtesters compatible with ${selectedCache.name}. Hover options for details.`
    : 'Showing backtesters that can run with available local cache data.';
  const previewPlot = plotUrls[slide];
  const plotNameByUrl = new Map(
    Object.entries(res?.artifacts || {})
      .filter(([name, url]) => name.endsWith('.png') && typeof url === 'string')
      .map(([name, url]) => [url as string, name])
  );
  const previewPlotName = previewPlot ? plotNameByUrl.get(previewPlot) || `Chart ${slide + 1}` : null;
  const errorDetail = (jobStatus?.error_detail || res?.error_detail) as ErrorDetail | undefined;
  const hasRunRecord = !!job || !!jobStatus || !!res;

  useEffect(() => {
    if (cacheDb && filteredCacheDbs.length > 0 && !filteredCacheDbs.some(opt => opt.path === cacheDb)) {
      setCacheDb('');
    }
    if (backtester && filteredBacktesters.length > 0 && !filteredBacktesters.includes(backtester)) {
      setBacktester(filteredBacktesters[0] || '');
    }
  }, [cacheDb, backtester, filteredCacheDbs, filteredBacktesters]);

  const runRows = hasRunRecord
    ? [
        {
          config: jobStatus?.cfg_name || res?.cfg_name || cfg || 'Not selected',
          universe: jobUniverseName || universe || 'No universe',
          backtester: jobStatus?.backtester || res?.backtester || backtester || 'Not selected',
          cache: jobCacheLabel || cacheDb || 'No cache',
          status: jobStatus?.status || (res ? 'success' : 'pending'),
          pnl: res?.summary?.total_pnl ?? res?.summary?.total_pnl_mtm ?? res?.summary?.return_mtm_pct_on_start ?? null,
          trades: res?.summary?.trades_total ?? res?.trades?.length ?? null,
        },
      ]
    : [];

  return (
    <div style={shellStyle}>
      <aside style={sidebarStyle}>
        <div style={{
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
        }}>
          S
        </div>
        <div style={{ display: 'grid', gap: 16, marginTop: 18 }}>
          <div style={navIconStyle}>DB</div>
          <div style={activeIconStyle}>BT</div>
          <div style={navIconStyle}>CR</div>
          <div style={navIconStyle}>PR</div>
        </div>
        <div style={{ marginTop: 'auto', ...navIconStyle }}>ST</div>
      </aside>

      <header style={topbarStyle}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginRight: 4 }}>
          <span style={{
            width: 26,
            height: 26,
            borderRadius: 7,
            background: `linear-gradient(135deg, ${colors.accent}, #0EA5E9 54%, #8B5CF6)`,
            color: '#03131A',
            display: 'grid',
            placeItems: 'center',
            fontSize: 13,
            fontWeight: 1000,
          }}>
            S
          </span>
          <span style={{ color: colors.text, fontWeight: 950, fontSize: 15 }}>Step</span>
        </div>
        <a href="/run" style={activeNavLinkStyle}>Run Backtest</a>
        <a href="/backtest_live_validation" style={navLinkStyle}>Backtest vs Live</a>
        <span style={navLinkStyle}>Circulators</span>
        <span style={navLinkStyle}>Product</span>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 18, alignItems: 'center', color: colors.muted }}>
          <span>Globe</span>
          <span>Alerts</span>
          <span style={{ width: 30, height: 30, borderRadius: '50%', background: colors.card2, display: 'grid', placeItems: 'center', border: `1px solid ${colors.border}` }}>U</span>
        </div>
      </header>

      <main className="run-page-content" style={mainStyle}>
        <div style={{ marginBottom: 28 }}>
          <h1 style={{ margin: 0, fontSize: 34, lineHeight: 1.1, color: colors.text, letterSpacing: 0 }}>Run Backtest</h1>
          <p style={{ margin: '10px 0 0', color: colors.muted, fontSize: 15 }}>
            Configure and launch a strategy backtest using cached market data.
          </p>
        </div>

        <div className="run-backtest-grid">
          <section className="run-card" style={{ ...cardStyle, padding: 22 }}>
            <h2 style={{ margin: '0 0 20px', fontSize: 18, color: colors.text }}>Backtest Configuration</h2>
            {!isReadOnly && (
              <div style={{ display: 'grid', gap: 17 }}>
                <div>
                  <label style={labelStyle}>Config</label>
                  <select value={cfg} onChange={e => setCfg(e.target.value)} style={inputStyle} title="Choose a strategy config. The following selectors will narrow to compatible local inputs.">
                    <option value="">-- pick config --</option>
                    {cfgs.map((c: any) => (
                      <option
                        key={c.name}
                        value={c.name}
                        title={c.cache_db_missing ? `This config references missing cache: ${c.cache_db_missing}. Select an available compatible cache after choosing it.` : c.cache_db ? `Suggested cache: ${c.cache_db}` : 'No fixed cache requirement detected.'}
                      >
                        {c.name}
                      </option>
                    ))}
                  </select>
                  {selectedCfgMissingCache && (
                    <div style={{ marginTop: 8, color: '#FDBA74', fontSize: 12 }}>
                      This config references missing cache <code>{selectedCfgMissingCache}</code>. Choose one of the compatible local caches below.
                    </div>
                  )}
                </div>

                <div>
                  <label style={labelStyle}>Universe</label>
                  <select value={universe} onChange={e => setUniverse(e.target.value)} style={inputStyle} title="Optional universe override. Use a universe that matches symbols present in the selected cache.">
                    <option value="">-- no universe --</option>
                    {universes.map(u => <option key={u} value={u} title="Universe is compatible when these symbols exist in the selected cache.">{u}</option>)}
                  </select>
                  <div style={{ marginTop: 8, color: colors.muted, fontSize: 12 }}>
                    Optional. Leave empty to use config symbols; choose only if the selected cache contains this universe.
                  </div>
                </div>

                <div>
                  <label style={labelStyle}>Capital / Limit</label>
                  <input type="number" value={bars} onChange={e => setBars(parseInt(e.target.value || '0'))} style={inputStyle} />
                </div>

                <div>
                  <label style={labelStyle}>Cache DB / NPZ</label>
                  <select value={cacheDb} onChange={e => setCacheDb(e.target.value)} style={inputStyle} title={cacheSelectAdvice}>
                    <option value="">-- select cache DB / NPZ --</option>
                    {filteredCacheDbs.map(opt => (
                      <option key={opt.path} value={opt.path} title={cacheAdvice(opt, backtester, selectedBacktesterCaps)}>
                        {opt.name}
                      </option>
                    ))}
                  </select>
                  <div style={{ marginTop: 8, color: colors.muted, fontSize: 12 }}>
                    {filteredCacheDbs.length > 0 ? cacheSelectAdvice : 'No compatible cache is available for the current selection.'}
                  </div>
                  {cacheRequired && (
                    <div style={{ marginTop: 10, padding: '10px 12px', borderRadius: 6, border: '1px solid rgba(239,68,68,0.35)', background: colors.warningBg, color: '#FCA5A5', fontSize: 12, fontWeight: 700 }}>
                      Warning: This backtester requires selecting an NPZ cache
                    </div>
                  )}
                  {cacheUnsupported && (
                    <div style={{ marginTop: 10, padding: '10px 12px', borderRadius: 6, border: '1px solid rgba(239,68,68,0.35)', background: colors.warningBg, color: '#FCA5A5', fontSize: 12, fontWeight: 700 }}>
                      Selected cache type is unsupported by this backtester
                    </div>
                  )}
                  {selectedCfgMeta?.cache_db && (
                    <div style={{ marginTop: 8, color: colors.muted, fontSize: 12 }}>Suggested cache: <code>{selectedCfgMeta.cache_db}</code></div>
                  )}
                </div>

                <div style={{ display: 'grid', gap: 12 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', gap: 16, alignItems: 'center' }}>
                    <div>
                      <div style={{ color: colors.text, fontSize: 13, fontWeight: 800 }}>Debug trades</div>
                      {debug && <div style={{ color: colors.muted, fontSize: 12, marginTop: 3 }}>Debug mode will include detailed trade logs.</div>}
                    </div>
                    <Toggle checked={debug} onChange={setDebug} />
                  </div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', gap: 16, alignItems: 'center' }}>
                    <div style={{ color: colors.text, fontSize: 13, fontWeight: 800 }}>Show trades</div>
                    <Toggle checked={showTrades} onChange={setShowTrades} />
                  </div>
                </div>

                <div>
                  <label style={labelStyle}>Backtester File</label>
                  <select value={backtester} onChange={e => setBacktester(e.target.value)} style={inputStyle} title={backtesterSelectAdvice}>
                    <option value="">-- pick backtester --</option>
                    {filteredBacktesters.map(b => (
                      <option key={b} value={b} title={backtesterAdvice(b, backtesterCaps[b] || {}, selectedCache)}>
                        {b}
                      </option>
                    ))}
                  </select>
                  <div style={{ marginTop: 8, color: colors.muted, fontSize: 12 }}>
                    {filteredBacktesters.length > 0 ? backtesterSelectAdvice : 'No compatible backtester is available for the selected cache.'}
                  </div>
                  {selectedBacktesterFile?.url && (
                    <a href={selectedBacktesterFile.url} target="_blank" rel="noreferrer" style={{ color: colors.accent, display: 'inline-block', marginTop: 8, fontSize: 12, fontWeight: 800 }}>
                      backtester file
                    </a>
                  )}
                </div>

                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                  <button onClick={start} disabled={startDisabled} title={startTooltip} style={startDisabled ? disabledButtonStyle : primaryButtonStyle}>
                    Start Backtest
                  </button>
                  <button onClick={() => setShowConfigPanel(prev => !prev)} style={buttonStyle}>
                    Edit Config
                  </button>
                </div>

                {errMsg && <div style={{ color: '#FCA5A5', background: colors.warningBg, border: '1px solid rgba(239,68,68,0.35)', borderRadius: 6, padding: 10, fontSize: 12 }}>{errMsg}</div>}
              </div>
            )}
          </section>

          <section className="run-right-panel" style={{ display: 'grid', gap: 18, width: '100%', minWidth: 0 }}>
            <div className="run-card" style={{ ...cardStyle, padding: 20 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 16, marginBottom: 12 }}>
                <div>
                  <h2 style={{ margin: 0, fontSize: 18 }}>Backtest Preview</h2>
                  {previewPlotName && (
                    <div style={{ marginTop: 5, color: colors.muted, fontSize: 12 }}>
                      {previewPlotName} · {slide + 1} / {plotUrls.length}
                    </div>
                  )}
                </div>
                <div style={{ display: 'flex', gap: 10, color: colors.muted, fontSize: 12, alignItems: 'center', flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                  {plotUrls.length > 1 && (
                    <>
                      <button style={buttonStyle} onClick={() => setSlide((slide - 1 + plotUrls.length) % plotUrls.length)}>Prev</button>
                      <button style={buttonStyle} onClick={() => setSlide((slide + 1) % plotUrls.length)}>Next</button>
                    </>
                  )}
                  <span><span style={{ color: colors.accent }}>■</span> Backtest</span>
                </div>
              </div>
              <RealResultChart plotUrl={previewPlot} />
            </div>

            {job && (
              <div className="run-card" style={{ ...cardStyle, padding: 18 }}>
                <div style={{ fontWeight: 900, marginBottom: 10 }}>Job <code>{job.job_id}</code></div>
                {jobStatus ? (
                  <>
                    <div style={{ color: colors.muted }}>
                      Status: <StatusBadge status={jobStatus.status || 'unknown'} /> {jobStatus.message && <span style={{ color: colors.danger }}>{jobStatus.message}</span>}
                    </div>
                    <ErrorInsight detail={jobStatus.error_detail} fallback={jobStatus.status === 'error' ? jobStatus.message || null : null} />
                    {shouldShowSpinner && <div style={{ marginTop: 8, color: colors.accent, fontFamily: 'monospace' }}>{spinnerFrames[spinnerIndex % spinnerFrames.length]} Backtest running...</div>}
                    {progressPercent !== null && (
                      <div style={{ marginTop: 12 }}>
                        <div style={{ height: 8, background: '#0B1220', borderRadius: 999, overflow: 'hidden' }}>
                          <div style={{ width: `${Math.min(100, Math.max(0, progressPercent))}%`, background: colors.accent, height: '100%', transition: 'width 0.4s ease' }} />
                        </div>
                        <div style={{ color: colors.muted, fontSize: 12, marginTop: 6 }}>Progress: {Math.min(100, Math.max(0, progressPercent))}%</div>
                      </div>
                    )}
                  </>
                ) : <div style={{ color: colors.muted }}>Loading status...</div>}
              </div>
            )}

            <div className="run-card" style={{ ...cardStyle }}>
              <div style={{ padding: '16px 18px', borderBottom: `1px solid ${colors.border}`, fontWeight: 900 }}>Recent Backtest Runs</div>
              <div className="run-table-wrapper">
                {runRows.length > 0 ? (
                  <table style={{ width: '100%', minWidth: 720, borderCollapse: 'collapse', fontSize: 13 }}>
                    <thead>
                      <tr style={{ background: colors.card2 }}>
                        {['Config', 'Universe', 'Backtester', 'Cache', 'Status', 'P&L', 'Trades', 'Actions'].map(col => (
                          <th key={col} style={{ color: colors.muted, textAlign: 'left', padding: '13px 16px', borderBottom: `1px solid ${colors.border}` }}>{col}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {runRows.map((row, idx) => (
                        <tr key={idx} style={{ borderBottom: `1px solid ${colors.border}` }}>
                          <td style={{ padding: '13px 16px' }}>{row.config}</td>
                          <td style={{ padding: '13px 16px', color: colors.muted }}>{row.universe}</td>
                          <td style={{ padding: '13px 16px', color: colors.muted }}>{row.backtester}</td>
                          <td style={{ padding: '13px 16px', color: colors.muted }}>{row.cache}</td>
                          <td style={{ padding: '13px 16px' }}><StatusBadge status={row.status} /></td>
                          <td style={{ padding: '13px 16px', color: row.pnl == null ? colors.muted : Number(row.pnl) >= 0 ? colors.success : colors.danger, fontWeight: 900 }}>{row.pnl == null ? '—' : formatVal(row.pnl)}</td>
                          <td style={{ padding: '13px 16px' }}>{row.trades == null ? '—' : row.trades}</td>
                          <td style={{ padding: '13px 16px' }}>{res?.artifacts ? <a href={res.artifacts['logs.txt'] || '#'} style={{ color: colors.accent }}>View</a> : <span style={{ color: colors.muted }}>—</span>}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <div style={{ padding: 18, color: colors.muted, fontSize: 13 }}>
                    Run a backtest to populate this table with real backend results.
                  </div>
                )}
              </div>
            </div>

            {res && (
              <div className="run-card" style={{ ...cardStyle, padding: 18 }}>
                <h3 style={{ marginTop: 0 }}>Backtest Result</h3>
                <ErrorInsight detail={errorDetail} fallback={errMsg} />
                <div className="run-logs-wrapper">
                  <pre style={{ maxHeight: 260, overflow: 'auto', color: '#CBD5E1', background: '#0B1220', padding: 12, borderRadius: 6 }}>{JSON.stringify(res.summary, null, 2)}</pre>
                </div>
                {vizUrls.length > 0 && (
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: 12, marginTop: 14 }}>
                    {vizUrls.map((url, idx) => (
                      <div key={url} className="run-chart-wrapper" style={{ border: `1px solid ${colors.border}`, background: '#0B1220', borderRadius: 6, overflow: 'hidden' }}>
                        <img src={url} alt={`Backtest visualization ${idx + 1}`} style={{ width: '100%', display: 'block', objectFit: 'contain' }} />
                      </div>
                    ))}
                  </div>
                )}
                {showTrades && Array.isArray(res.trades) && res.trades.length > 0 && (
                  <div style={{ marginTop: 14 }}>
                    <div style={{ fontWeight: 900, marginBottom: 8 }}>Trades</div>
                    <div className="run-table-wrapper" style={{ border: `1px solid ${colors.border}`, borderRadius: 6 }}>
                      <table style={{ width: '100%', minWidth: 680, borderCollapse: 'collapse', fontSize: 12 }}>
                        <thead>
                          <tr style={{ background: colors.card2 }}>
                            {Object.keys(res.trades[0]).slice(0, 8).map(key => (
                              <th key={key} style={{ color: colors.muted, textAlign: 'left', padding: '10px 12px', borderBottom: `1px solid ${colors.border}` }}>{key}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {res.trades.slice(0, 80).map((trade: any, idx: number) => (
                            <tr key={idx} style={{ borderBottom: `1px solid ${colors.border}` }}>
                              {Object.keys(res.trades[0]).slice(0, 8).map(key => (
                                <td key={key} style={{ padding: '10px 12px', color: colors.muted, whiteSpace: 'nowrap' }}>{String(trade[key] ?? '')}</td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
                {logs && (
                  <div className="run-logs-wrapper" style={{ marginTop: 14 }}>
                    <pre style={{ maxHeight: 200, overflow: 'auto', color: '#CBD5E1', background: '#0B1220', padding: 12, borderRadius: 6 }}>{logs}</pre>
                  </div>
                )}
              </div>
            )}
          </section>
        </div>

        {showConfigPanel && (
          <div style={{ position: 'fixed', top: 0, right: 0, width: 'min(520px, 94vw)', height: '100%', background: '#0B1220', borderLeft: `1px solid ${colors.border}`, boxShadow: '-24px 0 70px rgba(0,0,0,0.4)', zIndex: 50, display: 'flex', flexDirection: 'column', padding: 18, gap: 10, boxSizing: 'border-box' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <h3 style={{ margin: 0 }}>Config Editor</h3>
              <button onClick={() => setShowConfigPanel(false)} style={buttonStyle}>Close</button>
            </div>
            {cfg ? configLoading ? (
              <div style={{ color: colors.muted }}>Loading config...</div>
            ) : configError ? (
              <div style={{ color: colors.danger }}>{configError}<div style={{ marginTop: 8 }}><button onClick={reloadConfig} style={buttonStyle}>Retry</button></div></div>
            ) : (
              <>
                <div style={{ color: colors.muted, fontSize: 13 }}>Editing: <code>{cfg}</code></div>
                <textarea value={configText} onChange={e => { setConfigText(e.target.value); setConfigSaveError(null); setConfigSaveSuccess(null); }} style={{ flexGrow: 1, width: '100%', minHeight: 220, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 13, lineHeight: 1.45, padding: 12, boxSizing: 'border-box', color: colors.text, background: '#070B16', border: `1px solid ${colors.border}`, borderRadius: 6 }} />
                <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
                  <button onClick={saveConfig} disabled={!configDirty || configSaving} style={buttonStyle}>Save</button>
                  <button onClick={resetConfig} disabled={!configDirty || configSaving} style={buttonStyle}>Reset</button>
                  <button onClick={reloadConfig} disabled={configSaving || configLoading} style={buttonStyle}>Reload</button>
                  {configSaving && <span style={{ color: colors.muted }}>Saving...</span>}
                  {configSaveSuccess && <span style={{ color: colors.success }}>{configSaveSuccess}</span>}
                  {configSaveError && <span style={{ color: colors.danger }}>{configSaveError}</span>}
                </div>
              </>
            ) : <div style={{ color: colors.muted }}>Select a config to view and edit.</div>}
          </div>
        )}
      </main>
    </div>
  );

}

function formatDuration(seconds?: number | null) {
  if (seconds == null || !isFinite(seconds)) return '';
  const total = Math.max(0, Math.round(seconds));
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  if (mins > 0) {
    return `${mins}m ${secs}s`;
  }
  return `${secs}s`;
}

function formatVal(v: any) {
  const num = Number(v);
  return isNaN(num) ? v : num.toFixed(3);
}

function extractUniverseName(source: any): string | null {
  if (!source) return null;
  const candidates: string[] = [];
  if (typeof source === 'string') {
    candidates.push(source);
  } else if (typeof source === 'object') {
    if (typeof source.symbols_file === 'string') {
      candidates.push(source.symbols_file);
    }
    if (typeof source.universe_file === 'string') {
      candidates.push(source.universe_file);
    }
    const nested = (source as any).universe;
    if (nested && typeof nested === 'object') {
      if (typeof nested.file === 'string') {
        candidates.push(nested.file);
      }
      if (typeof nested.path === 'string') {
        candidates.push(nested.path);
      }
    }
  }
  for (const candidate of candidates) {
    if (!candidate) continue;
    const normalized = String(candidate).replace(/\\/g, '/');
    const parts = normalized.split('/');
    const last = parts[parts.length - 1];
    if (last) {
      return last;
    }
  }
  return null;
}
