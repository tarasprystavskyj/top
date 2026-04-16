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

export function seriesToSvgPath(points, width, height, padding) {
  if (!Array.isArray(points) || points.length === 0) return '';
  const xs = points.map(p => Number(p.x));
  const ys = points.map(p => Number(p.y));
  const xMin = Math.min(...xs);
  const xMax = Math.max(...xs);
  const yMin = Math.min(...ys);
  const yMax = Math.max(...ys);
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
