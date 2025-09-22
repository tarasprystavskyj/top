import { useEffect, useState } from 'react';
import { useRouter } from 'next/router';
import { apiFetch } from '../utils/api';

type CacheDbOption = {
  name: string;
  path: string;
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
};

const spinnerFrames = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];

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

  const isReadOnly = typeof router.query.id === 'string';

  const tradesData = Array.isArray(res?.trades) ? res.trades : [];
  const renderTradesTable = (
    title: string,
    rows: any[],
    highlightKey?: string
  ) => {
    if (!Array.isArray(rows) || rows.length === 0) return null;
    const firstRow = rows.find(row => row && typeof row === 'object');
    if (!firstRow) return null;
    const headers = Object.keys(firstRow);
    if (headers.length === 0) return null;

    return (
      <div style={{ flex: '1 1 360px', minWidth: 320 }}>
        <h4>
          {title} ({rows.length})
        </h4>
        <div style={{ maxHeight: '200px', overflow: 'auto' }}>
          <table border={1}>
            <thead>
              <tr>
                {headers.map(k => (
                  <th key={k}>{k}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => {
                const highlightValue = highlightKey ? Number(row?.[highlightKey]) : NaN;
                const rowStyle = !isNaN(highlightValue) && highlightValue > 0 ? { backgroundColor: '#d4edda' } : undefined;
                return (
                  <tr key={i} style={rowStyle}>
                    {headers.map(k => (
                      <td key={k}>{formatVal(row?.[k])}</td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    );
  };

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
          .map((item: any) => ({ name: item.name, path: item.path }));
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
    const j = await apiFetch('/api/backtest', {
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
    }).then(r => r.json());
    setJob(j);
    setJobStatus(null);
    setSpinnerIndex(0);
    setRes(null);
    setErrMsg(null);
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
  ];
  const plotUrls = plotNames
    .map(n => res?.artifacts?.[n])
    .filter(Boolean) as string[];

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

  return (
    <div>
      <button
        onClick={() => setShowConfigPanel(prev => !prev)}
        style={{
          position: 'fixed',
          top: '10px',
          right: '10px',
          zIndex: 1100,
          padding: '6px 12px',
        }}
      >
        {showConfigPanel ? 'Hide Config Editor' : 'Edit Config'}
      </button>
      {showConfigPanel && (
        <div
          style={{
            position: 'fixed',
            top: 0,
            right: 0,
            width: 'min(420px, 90vw)',
            height: '100%',
            background: '#fff',
            borderLeft: '1px solid #ddd',
            boxShadow: '-2px 0 8px rgba(0,0,0,0.15)',
            zIndex: 1050,
            display: 'flex',
            flexDirection: 'column',
            padding: '16px',
            gap: '8px',
            boxSizing: 'border-box',
          }}
        >
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
            }}
          >
            <h4 style={{ margin: 0 }}>Config Editor</h4>
            <button onClick={() => setShowConfigPanel(false)}>Close</button>
          </div>
          {cfg ? (
            configLoading ? (
              <div>Loading config...</div>
            ) : (
              <>
                <div style={{ fontSize: '0.9em', color: '#555' }}>
                  Editing: <code>{cfg}</code>
                </div>
                {configError ? (
                  <div style={{ color: 'red' }}>
                    {configError}
                    <div style={{ marginTop: '6px' }}>
                      <button onClick={reloadConfig} disabled={configSaving}>
                        Retry
                      </button>
                    </div>
                  </div>
                ) : (
                  <>
                    <textarea
                      value={configText}
                      onChange={e => {
                        setConfigText(e.target.value);
                        setConfigSaveError(null);
                        setConfigSaveSuccess(null);
                      }}
                      style={{
                        flexGrow: 1,
                        width: '100%',
                        minHeight: '200px',
                        fontFamily: 'monospace',
                        fontSize: '0.9em',
                        lineHeight: 1.4,
                        padding: '8px',
                        boxSizing: 'border-box',
                      }}
                    />
                    <div
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        flexWrap: 'wrap',
                        gap: '8px',
                      }}
                    >
                      <button onClick={saveConfig} disabled={!configDirty || configSaving}>
                        Save
                      </button>
                      <button onClick={resetConfig} disabled={!configDirty || configSaving}>
                        Reset
                      </button>
                      <button onClick={reloadConfig} disabled={configSaving || configLoading}>
                        Reload
                      </button>
                      {configSaving && <span>Saving...</span>}
                      {configSaveSuccess && (
                        <span style={{ color: 'green' }}>{configSaveSuccess}</span>
                      )}
                      {configSaveError && (
                        <span style={{ color: 'red' }}>{configSaveError}</span>
                      )}
                    </div>
                  </>
                )}
              </>
            )
          ) : (
            <div>Select a config to view and edit.</div>
          )}
        </div>
      )}
      <h3>Run Backtest</h3>
      {!isReadOnly && (
        <div>
          <div>
            <select value={cfg} onChange={e => setCfg(e.target.value)}>
              <option value=''>--pick config--</option>
              {cfgs.map((c: any) => (
                <option key={c.name} value={c.name}>
                  {c.name}
                </option>
              ))}
            </select>
            <select value={universe} onChange={e => setUniverse(e.target.value)}>
              <option value=''>--no universe--</option>
              {universes.map(u => (
                <option key={u} value={u}>
                  {u}
                </option>
              ))}
            </select>
            <input
              type='number'
              value={bars}
              onChange={e => setBars(parseInt(e.target.value || '0'))}
            />
            <select
              value={cacheDb}
              onChange={e => setCacheDb(e.target.value)}
              style={{ width: '300px' }}
            >
              <option value=''>--select cache db--</option>
              {displayedCacheDbs.map(opt => (
                <option key={opt.path} value={opt.path}>
                  {opt.name}
                </option>
              ))}
            </select>
            <label>
              <input
                type='checkbox'
                checked={debug}
                onChange={e => setDebug(e.target.checked)}
              />
              Debug
            </label>
            <label>
              <input
                type='checkbox'
                checked={showTrades}
                onChange={e => setShowTrades(e.target.checked)}
              />
              trades
            </label>
            <button onClick={start}>Start</button>
          </div>
          <div>
            <select value={backtester} onChange={e => setBacktester(e.target.value)}>
              {backtesters.map(b => (
                <option key={b} value={b}>
                  {b}
                </option>
              ))}
            </select>
          </div>
        </div>
      )}
      {job && (
        <div style={{ marginTop: '10px', marginBottom: '10px' }}>
          <p style={{ marginBottom: '6px' }}>
            Job: <code>{job.job_id}</code>
          </p>
          {jobStatus ? (
            <div
              style={{
                border: '1px solid #ddd',
                padding: '10px',
                borderRadius: '6px',
                maxWidth: '440px',
              }}
            >
              <div style={{ fontWeight: 600 }}>
                Status: {jobStatus.status || 'unknown'}
                {jobStatus.status === 'error' && jobStatus.message && (
                  <span style={{ color: 'red' }}> ({jobStatus.message})</span>
                )}
              </div>
              {shouldShowSpinner && (
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '8px',
                    fontFamily: 'monospace',
                    marginTop: '6px',
                  }}
                >
                  <span>{spinnerFrames[spinnerIndex % spinnerFrames.length]}</span>
                  <span>Backtest running...</span>
                </div>
              )}
              {progressPercent !== null && (
                <div style={{ marginTop: '8px' }}>
                  <div
                    style={{
                      height: '8px',
                      background: '#eee',
                      borderRadius: '4px',
                      overflow: 'hidden',
                    }}
                  >
                    <div
                      style={{
                        width: `${Math.min(100, Math.max(0, progressPercent))}%`,
                        background: '#4a90e2',
                        height: '100%',
                        transition: 'width 0.4s ease',
                      }}
                    />
                  </div>
                  <div style={{ marginTop: '4px', fontSize: '0.9em' }}>
                    Progress: {Math.min(100, Math.max(0, progressPercent))}%
                  </div>
                </div>
              )}
              <div
                style={{
                  display: 'flex',
                  flexWrap: 'wrap',
                  gap: '10px',
                  marginTop: '8px',
                  fontSize: '0.85em',
                }}
              >
                {jobStatus.cfg_name && (
                  <span>
                    Config: <code>{jobStatus.cfg_name}</code>
                  </span>
                )}
                {jobUniverseName && (
                  <span>
                    Universe: <code>{jobUniverseName}</code>
                  </span>
                )}
                {jobCacheLabel && (
                  <span>
                    Cache DB: <code>{jobCacheLabel}</code>
                  </span>
                )}
                {jobStatus.backtester && <span>Backtester: {jobStatus.backtester}</span>}
                {jobStatus.limit_bars != null && <span>Bars: {jobStatus.limit_bars}</span>}
                {jobStatus.symbol_count != null && <span>Symbols: {jobStatus.symbol_count}</span>}
                {jobStatus.elapsed_seconds != null && (
                  <span>Elapsed: {formatDuration(jobStatus.elapsed_seconds)}</span>
                )}
                {jobStatus.eta_seconds != null &&
                  (jobStatus.status === 'running' || jobStatus.status === 'queued') && (
                    <span>ETA: {formatDuration(jobStatus.eta_seconds)}</span>
                  )}
                {totalExpectedSeconds != null && totalExpectedSeconds > 0 && (
                  <span>Estimated total: {formatDuration(totalExpectedSeconds)}</span>
                )}
              </div>
            </div>
          ) : (
            <div style={{ color: '#666' }}>Loading status...</div>
          )}
        </div>
      )}
      {res && (
        <div>
          {errMsg && <pre style={{ color: 'red' }}>Error: {errMsg}</pre>}
          <pre>{JSON.stringify(res.summary, null, 2)}</pre>
          {plotUrls.length > 0 && (
            <div>
              <img
                src={plotUrls[slide]}
                style={{ maxWidth: '600px', display: 'block' }}
              />
              <div>
                <button
                  onClick={() =>
                    setSlide((slide - 1 + plotUrls.length) % plotUrls.length)
                  }
                >
                  Prev
                </button>
                <button onClick={() => setSlide((slide + 1) % plotUrls.length)}>
                  Next
                </button>
              </div>
            </div>
          )}
          {vizUrls.length > 0 && (
            <div
              style={{
                display: 'flex',
                gap: '10px',
                flexWrap: 'wrap',
                marginTop: '10px',
              }}
            >
              {vizUrls.map((u, i) => (
                <img key={i} src={u} style={{ maxWidth: '400px' }} />
              ))}
            </div>
          )}
          {res.debug && (
            <div style={{ marginTop: '10px' }}>
              <strong>Debug info</strong>
              {res.debug.cmd && (
                <pre
                  style={{
                    whiteSpace: 'pre-wrap',
                    background: '#f5f5f5',
                    padding: '6px',
                    overflowX: 'auto',
                  }}
                >
                  {res.debug.cmd}
                </pre>
              )}
              {res.debug.cache_db && (
                <div>
                  Cache DB: <code>{res.debug.cache_db}</code>
                  {res.debug.cache_db_exists === false && (
                    <span style={{ color: 'red' }}> (missing)</span>
                  )}
                </div>
              )}
              {res.debug.cache_db_label &&
                (!res.debug.cache_db || res.debug.cache_db_label !== res.debug.cache_db) && (
                  <div>
                    Selected value: <code>{res.debug.cache_db_label}</code>
                  </div>
                )}
            </div>
          )}
          {logs && (
            <pre style={{ maxHeight: '200px', overflowY: 'auto' }}>{logs}</pre>
          )}
          {showTrades && tradesData.length > 0 && (
            <div style={{ display: 'flex', gap: 20, flexWrap: 'wrap', alignItems: 'flex-start' }}>
              {renderTradesTable('Backtest trades', tradesData)}
            </div>
          )}
          {showTrades && res?.files?.bt_trades && (
            <div style={{ marginTop: '10px' }}>
              <em>Backtest trades file:</em>{' '}
              <code>{res.files.bt_trades}</code>
            </div>
          )}
        </div>
      )}
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

