export function avgPriceDiffColorRule(liveAvg, backtestAvg) {
  const live = Number(liveAvg);
  const back = Number(backtestAvg);
  if (!Number.isFinite(live) || !Number.isFinite(back) || live <= 0 || back <= 0) return 'neutral';
  if (live < back) return 'red';
  if (live > back) return 'green';
  return 'neutral';
}

export function computePollingIntervalMs(intervalSeconds) {
  const base = Math.max(1, Number(intervalSeconds) || 60);
  return Math.max(5000, Math.round(base * 1000 + 2000));
}

function toMs(ts, fallback) {
  const ms = Date.parse(String(ts || ''));
  return Number.isFinite(ms) ? ms : fallback;
}

export function sortSeriesByTs(points) {
  return (Array.isArray(points) ? points : [])
    .map((p, idx) => ({ ...p, __ms: toMs(p?.ts, idx) }))
    .sort((a, b) => a.__ms - b.__ms)
    .map(({ __ms, ...p }) => p);
}

export function preparePnlSeries(backtest, live, normalize = false) {
  const bt = sortSeriesByTs(backtest);
  const lv = sortSeriesByTs(live);
  if (!normalize) return { backtest: bt, live: lv };

  // Optional mode: normalize both to first common timestamp if possible.
  const btByTs = new Map(bt.map(p => [String(p.ts), Number(p.value)]));
  const firstCommon = lv.find(p => btByTs.has(String(p.ts)));
  const btBase = firstCommon ? Number(btByTs.get(String(firstCommon.ts))) : Number(bt[0]?.value || 0);
  const lvBase = firstCommon ? Number(firstCommon.value || 0) : Number(lv[0]?.value || 0);
  return {
    backtest: bt.map(p => ({ ...p, value: Number(p.value) - btBase })),
    live: lv.map(p => ({ ...p, value: Number(p.value) - lvBase })),
  };
}

export function computeSharedDomain(seriesList) {
  const merged = (Array.isArray(seriesList) ? seriesList : [])
    .flatMap(s => (Array.isArray(s?.data) ? s.data : []))
    .map((p, idx) => ({ x: toMs(p?.ts, idx), y: Number(p?.value) || 0 }));
  if (!merged.length) {
    return { xMin: 0, xMax: 1, yMin: 0, yMax: 1, pointCount: 0 };
  }
  return {
    xMin: Math.min(...merged.map(p => p.x)),
    xMax: Math.max(...merged.map(p => p.x)),
    yMin: Math.min(...merged.map(p => p.y)),
    yMax: Math.max(...merged.map(p => p.y)),
    pointCount: merged.length,
  };
}

export function seriesToSvgPath(points, width, height, padding, domain) {
  if (!Array.isArray(points) || points.length === 0) return '';
  const xMin = Number(domain?.xMin);
  const xMax = Number(domain?.xMax);
  const yMin = Number(domain?.yMin);
  const yMax = Number(domain?.yMax);
  const xRange = xMax - xMin || 1;
  const yRange = yMax - yMin || 1;
  const usableW = width - padding * 2;
  const usableH = height - padding * 2;
  return points
    .map((p, idx) => {
      const x = padding + ((Number(p.x) - xMin) / xRange) * usableW;
      const y = padding + ((yMax - Number(p.y)) / yRange) * usableH;
      return `${idx === 0 ? 'M' : 'L'}${x.toFixed(2)} ${y.toFixed(2)}`;
    })
    .join(' ');
}
