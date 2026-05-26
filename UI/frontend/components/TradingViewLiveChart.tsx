import { type CSSProperties, useEffect, useMemo, useRef, useState } from 'react';
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  LineSeries,
  PriceScaleMode,
  createChart,
  createSeriesMarkers,
} from 'lightweight-charts';

type Point = { ts: string; value: number };
type PriceBar = { ts: string; open: number; high: number; low: number; close: number };
type ChartMarker = {
  id: string;
  time: string;
  price: number;
  text: string;
  kind: string;
  layer: string;
  color: string;
  shape: string;
  position: string;
};
type ChartLabel = {
  id: string;
  time: string;
  price: number;
  text: string;
  layer: string;
  color: string;
};
type LiveChartPayload = {
  live?: Point[];
  backtest?: Point[];
  live_realized?: Point[];
  backtest_realized?: Point[];
  backtest_price?: Point[];
  price_bars?: PriceBar[];
  distance?: Point[];
  mark?: Point[];
  markers?: ChartMarker[];
  labels?: ChartLabel[];
  sources?: Record<string, string | null>;
  warnings?: string[];
  approximate?: boolean;
};

type BarSize = '1m' | '5m' | '15m' | '1h';
type PriceLayerKey = 'livePrice' | 'backtestPrice' | 'telemetryMark' | 'events' | 'labels' | 'parameters';
type PnlLayerKey = 'livePnl' | 'backtestPnl' | 'liveRealized' | 'backtestRealized';

const colors = {
  bg: '#0B1220',
  panel: '#111827',
  border: '#263246',
  text: '#F8FAFC',
  muted: '#94A3B8',
  accent: '#22D3EE',
  green: '#22C55E',
  red: '#EF4444',
  amber: '#F59E0B',
  violet: '#A78BFA',
};

const BAR_SECONDS: Record<BarSize, number> = {
  '1m': 60,
  '5m': 300,
  '15m': 900,
  '1h': 3600,
};

const controlStyle: CSSProperties = {
  height: 32,
  borderRadius: 6,
  border: `1px solid ${colors.border}`,
  background: '#0F172A',
  color: colors.text,
  padding: '0 10px',
  fontWeight: 800,
  fontSize: 12,
};

const labelStyle: CSSProperties = {
  color: colors.muted,
  fontSize: 12,
  display: 'flex',
  alignItems: 'center',
  gap: 5,
  whiteSpace: 'nowrap',
};

function toUnixTime(ts: string, fallback: number) {
  const parsed = Date.parse(String(ts || ''));
  return Number.isFinite(parsed) ? Math.floor(parsed / 1000) : fallback;
}

function toLineData(points: Point[], bucketSeconds?: number) {
  const byTime = new Map<number, { time: number; value: number; rawTime: number }>();
  points.forEach((point, idx) => {
    const value = Number(point.value);
    if (!Number.isFinite(value)) return;
    const rawTime = toUnixTime(point.ts, idx + 1);
    const time = bucketSeconds ? Math.floor(rawTime / bucketSeconds) * bucketSeconds : rawTime;
    const existing = byTime.get(time);
    if (!existing || rawTime >= existing.rawTime) {
      byTime.set(time, { time, value, rawTime });
    }
  });
  return Array.from(byTime.values())
    .map(({ time, value }) => ({ time, value }))
    .sort((a, b) => a.time - b.time);
}

function toBarData(points: PriceBar[]) {
  const byTime = new Map<number, { time: number; open: number; high: number; low: number; close: number }>();
  points.forEach((point, idx) => {
    const open = Number(point.open);
    const high = Number(point.high);
    const low = Number(point.low);
    const close = Number(point.close);
    if (![open, high, low, close].every(Number.isFinite)) return;
    const time = toUnixTime(point.ts, idx + 1);
    byTime.set(time, { time, open, high, low, close });
  });
  return Array.from(byTime.values()).sort((a, b) => a.time - b.time);
}

function aggregateBars(points: PriceBar[], seconds: number) {
  const buckets = new Map<number, { time: number; open: number; high: number; low: number; close: number }>();
  toBarData(points).forEach((point) => {
    const bucket = Math.floor(Number(point.time) / seconds) * seconds;
    const existing = buckets.get(bucket);
    if (!existing) {
      buckets.set(bucket, { ...point, time: bucket });
      return;
    }
    existing.high = Math.max(existing.high, point.high);
    existing.low = Math.min(existing.low, point.low);
    existing.close = point.close;
  });
  return Array.from(buckets.values()).sort((a, b) => a.time - b.time);
}

function timeForBar(ts: string, fallback: number, barSize: BarSize) {
  const raw = toUnixTime(ts, fallback);
  const seconds = BAR_SECONDS[barSize];
  return (Math.floor(raw / seconds) * seconds) as any;
}

function regularTimeScale(series: Array<Array<{ time: number }>>, seconds: number) {
  let from = Number.POSITIVE_INFINITY;
  let to = Number.NEGATIVE_INFINITY;
  series.forEach((items) => {
    items.forEach((item) => {
      const time = Number(item.time);
      if (!Number.isFinite(time)) return;
      from = Math.min(from, time);
      to = Math.max(to, time);
    });
  });
  if (!Number.isFinite(from) || !Number.isFinite(to)) return [];
  const start = Math.floor(from / seconds) * seconds;
  const end = Math.ceil(to / seconds) * seconds;
  const out: Array<{ time: any; value: number }> = [];
  for (let time = start; time <= end; time += seconds) {
    out.push({ time: time as any, value: 0 });
  }
  return out;
}

function createBaseChart(el: HTMLDivElement, height: number, barSize: BarSize, autoScale = true) {
  return createChart(el, {
    autoSize: true,
    height,
    layout: {
      background: { type: ColorType.Solid, color: colors.bg },
      textColor: colors.muted,
      fontFamily: 'Inter, ui-sans-serif, system-ui, sans-serif',
    },
    grid: {
      vertLines: { color: '#1E293B' },
      horzLines: { color: '#1E293B' },
    },
    crosshair: {
      mode: CrosshairMode.Normal,
      vertLine: { color: 'rgba(148, 163, 184, 0.5)', labelBackgroundColor: colors.panel },
      horzLine: { color: 'rgba(148, 163, 184, 0.5)', labelBackgroundColor: colors.panel },
    },
    rightPriceScale: {
      autoScale,
      mode: PriceScaleMode.Normal,
      invertScale: false,
      alignLabels: true,
      borderVisible: true,
      borderColor: '#334155',
      visible: true,
      scaleMargins: { top: 0.12, bottom: 0.18 },
    },
    leftPriceScale: { visible: false },
    timeScale: { borderColor: '#334155', timeVisible: true, secondsVisible: barSize === '1m', minBarSpacing: 0.05 },
    handleScale: {
      axisPressedMouseMove: { time: true, price: true },
      axisDoubleClickReset: { time: true, price: true },
      mouseWheel: true,
      pinch: true,
    },
    handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
  });
}

function fullRangeBarSpacing(el: HTMLDivElement, seriesLengths: number[]) {
  const longest = Math.max(1, ...seriesLengths);
  const drawableWidth = Math.max(240, el.clientWidth - 90);
  return Math.max(0.08, Math.min(8, drawableWidth / longest));
}

function addHiddenTimeScale(chart: any, data: Array<{ time: any; value: number }>) {
  if (!data.length) return null;
  const series = chart.addSeries(LineSeries, {
    priceScaleId: 'left',
    color: 'rgba(0, 0, 0, 0)',
    lineWidth: 1,
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false,
  } as any);
  series.setData(data as any);
  return series;
}

function fullLogicalRange(length: number) {
  if (length <= 1) return null;
  return { from: 0 as any, to: (length - 1) as any };
}

export default function TradingViewLiveChart({ payload }: { payload: LiveChartPayload }) {
  const priceRef = useRef<HTMLDivElement | null>(null);
  const pnlRef = useRef<HTMLDivElement | null>(null);
  const [barSize, setBarSize] = useState<BarSize>('1m');
  const [syncTime, setSyncTime] = useState(true);
  const [fitSignal, setFitSignal] = useState(0);
  const [priceLayers, setPriceLayers] = useState<Record<PriceLayerKey, boolean>>({
    livePrice: true,
    backtestPrice: true,
    telemetryMark: true,
    events: true,
    labels: true,
    parameters: false,
  });
  const [pnlLayers, setPnlLayers] = useState<Record<PnlLayerKey, boolean>>({
    livePnl: true,
    backtestPnl: true,
    liveRealized: true,
    backtestRealized: true,
  });

  const priceBars = Array.isArray(payload?.price_bars) ? payload.price_bars : [];
  const mark = Array.isArray(payload?.mark) ? payload.mark : [];
  const backtestPrice = Array.isArray(payload?.backtest_price) ? payload.backtest_price : [];
  const live = Array.isArray(payload?.live) ? payload.live : [];
  const backtest = Array.isArray(payload?.backtest) ? payload.backtest : [];
  const liveRealized = Array.isArray(payload?.live_realized) ? payload.live_realized : [];
  const backtestRealized = Array.isArray(payload?.backtest_realized) ? payload.backtest_realized : [];
  const markers = Array.isArray(payload?.markers) ? payload.markers : [];
  const labels = Array.isArray(payload?.labels) ? payload.labels : [];

  const liveCandles = useMemo(() => aggregateBars(priceBars, BAR_SECONDS[barSize]), [priceBars, barSize]);
  const barSeconds = BAR_SECONDS[barSize];
  const markLine = useMemo(() => toLineData(mark, barSeconds), [barSeconds, mark]);
  const backtestPriceLine = useMemo(() => toLineData(backtestPrice, barSeconds), [backtestPrice, barSeconds]);
  const liveLine = useMemo(() => toLineData(live, barSeconds), [barSeconds, live]);
  const backtestLine = useMemo(() => toLineData(backtest, barSeconds), [backtest, barSeconds]);
  const liveRealizedLine = useMemo(() => toLineData(liveRealized, barSeconds), [barSeconds, liveRealized]);
  const backtestRealizedLine = useMemo(() => toLineData(backtestRealized, barSeconds), [backtestRealized, barSeconds]);
  const eventTimes = useMemo(
    () => markers.map((marker, idx) => ({ time: timeForBar(marker.time, idx + 1, barSize) as number })),
    [barSize, markers],
  );
  const labelTimes = useMemo(
    () => labels.map((label, idx) => ({ time: timeForBar(label.time, idx + 1, barSize) as number })),
    [barSize, labels],
  );
  const sharedTimeScale = useMemo(
    () => regularTimeScale([liveCandles, markLine, backtestPriceLine, liveLine, backtestLine, liveRealizedLine, backtestRealizedLine, eventTimes, labelTimes], barSeconds),
    [barSeconds, liveCandles, markLine, backtestPriceLine, liveLine, backtestLine, liveRealizedLine, backtestRealizedLine, eventTimes, labelTimes],
  );
  const sharedLogicalRange = useMemo(() => fullLogicalRange(sharedTimeScale.length), [sharedTimeScale.length]);
  const hasPriceData = liveCandles.length > 0 || backtestPriceLine.length > 0 || markLine.length > 0;
  const hasPnlData = liveLine.length > 0 || backtestLine.length > 0 || liveRealizedLine.length > 0 || backtestRealizedLine.length > 0;

  useEffect(() => {
    if (!priceRef.current || !pnlRef.current || (!hasPriceData && !hasPnlData)) return;
    const priceEl = priceRef.current;
    const pnlEl = pnlRef.current;
    priceEl.replaceChildren();
    pnlEl.replaceChildren();

    let disposed = false;
    let manager: any = null;
    const priceChart = createBaseChart(priceEl, 520, barSize, true);
    const pnlChart = createBaseChart(pnlEl, 320, barSize, true);
    addHiddenTimeScale(priceChart, sharedTimeScale);
    addHiddenTimeScale(pnlChart, sharedTimeScale);

    const updateAxisDebugAttrs = () => {
      const priceLogical = priceChart.timeScale().getVisibleLogicalRange?.();
      const pnlLogical = pnlChart.timeScale().getVisibleLogicalRange?.();
      const priceTime = priceChart.timeScale().getVisibleRange?.();
      const pnlTime = pnlChart.timeScale().getVisibleRange?.();
      priceEl.dataset.sharedTimePoints = String(sharedTimeScale.length);
      pnlEl.dataset.sharedTimePoints = String(sharedTimeScale.length);
      priceEl.dataset.visibleLogicalFrom = priceLogical?.from == null ? '' : String(priceLogical.from);
      priceEl.dataset.visibleLogicalTo = priceLogical?.to == null ? '' : String(priceLogical.to);
      pnlEl.dataset.visibleLogicalFrom = pnlLogical?.from == null ? '' : String(pnlLogical.from);
      pnlEl.dataset.visibleLogicalTo = pnlLogical?.to == null ? '' : String(pnlLogical.to);
      priceEl.dataset.visibleTimeFrom = priceTime?.from == null ? '' : String(priceTime.from);
      priceEl.dataset.visibleTimeTo = priceTime?.to == null ? '' : String(priceTime.to);
      pnlEl.dataset.visibleTimeFrom = pnlTime?.from == null ? '' : String(pnlTime.from);
      pnlEl.dataset.visibleTimeTo = pnlTime?.to == null ? '' : String(pnlTime.to);
    };

    let anchorSeries: any = null;
    if (priceLayers.livePrice && liveCandles.length) {
      anchorSeries = priceChart.addSeries(CandlestickSeries, {
        priceScaleId: 'right',
        upColor: '#16A34A',
        downColor: '#DC2626',
        borderUpColor: '#22C55E',
        borderDownColor: '#EF4444',
        wickUpColor: '#22C55E',
        wickDownColor: '#EF4444',
        priceLineVisible: true,
        lastValueVisible: true,
        title: `Live HYPE ${barSize}`,
      } as any);
      anchorSeries.setData(liveCandles as any);
    }
    if (priceLayers.backtestPrice && backtestPriceLine.length) {
      const series = priceChart.addSeries(LineSeries, {
        priceScaleId: 'right',
        color: 'rgba(34, 211, 238, 0.86)',
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: true,
        title: 'Backtest price',
      } as any);
      series.setData(backtestPriceLine as any);
      if (!anchorSeries) anchorSeries = series;
    }
    if (priceLayers.telemetryMark && markLine.length) {
      const series = priceChart.addSeries(LineSeries, {
        priceScaleId: 'right',
        color: 'rgba(167, 139, 250, 0.72)',
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: true,
        title: 'Telemetry mark',
      } as any);
      series.setData(markLine as any);
      if (!anchorSeries) anchorSeries = series;
    }

    const addPnlLine = (enabled: boolean, data: any[], color: string, title: string, width = 1) => {
      if (!enabled || !data.length) return;
      const series = pnlChart.addSeries(LineSeries, {
        priceScaleId: 'right',
        color,
        lineWidth: width,
        priceLineVisible: false,
        lastValueVisible: true,
        title,
      } as any);
      series.setData(data as any);
    };
    addPnlLine(pnlLayers.livePnl, liveLine, '#10B981', 'Live total PNL', 1);
    addPnlLine(pnlLayers.backtestPnl, backtestLine, '#22D3EE', 'Backtest total PNL', 1);
    addPnlLine(pnlLayers.liveRealized, liveRealizedLine, '#F59E0B', 'Live realized PNL', 2);
    addPnlLine(pnlLayers.backtestRealized, backtestRealizedLine, '#A78BFA', 'Backtest realized PNL', 2);

    if (anchorSeries && priceLayers.events && markers.length) {
      createSeriesMarkers(
        anchorSeries,
        markers.map((marker, idx) => ({
          id: marker.id || `event-${idx}`,
          time: timeForBar(marker.time, idx + 1, barSize),
          position: marker.position as any,
          price: Number(marker.price),
          color: marker.color || colors.accent,
          shape: marker.shape as any,
          text: marker.text,
          size: 1.35,
        })) as any,
        { zOrder: 'top' } as any,
      );
    }

    const visibleLabels = labels.filter((label) => (label.layer === 'parameters' ? priceLayers.parameters : priceLayers.labels));
    if (anchorSeries && visibleLabels.length) {
      import('lightweight-charts-drawing')
        .then((drawing) => {
          if (disposed || !anchorSeries) return;
          manager = new drawing.DrawingManager();
          manager.attach(priceChart as any, anchorSeries, priceEl);
          visibleLabels.slice(0, 12).forEach((label, idx) => {
            const text = drawing.TextAnnotation.create(
              label.id || `label-${idx}`,
              { time: timeForBar(label.time, idx + 1, barSize), price: Number(label.price) } as any,
              label.text,
              { lineColor: label.color || colors.accent, labelColor: label.color || colors.accent },
              {
                fontSize: 11,
                backgroundColor: 'rgba(15, 23, 42, 0.92)',
                borderColor: label.color || colors.accent,
                padding: 5,
                visible: true,
                locked: true,
              } as any,
            );
            manager.addDrawing(text);
          });
        })
        .catch(() => undefined);
    }

    let syncing = false;
    const syncFromPrice = (range: any) => {
      if (!syncTime || syncing || !range) return;
      syncing = true;
      try {
        pnlChart.timeScale().setVisibleLogicalRange(range);
      } finally {
        syncing = false;
        updateAxisDebugAttrs();
      }
    };
    const syncFromPnl = (range: any) => {
      if (!syncTime || syncing || !range) return;
      syncing = true;
      try {
        priceChart.timeScale().setVisibleLogicalRange(range);
      } finally {
        syncing = false;
        updateAxisDebugAttrs();
      }
    };
    priceChart.timeScale().subscribeVisibleLogicalRangeChange(syncFromPrice);
    pnlChart.timeScale().subscribeVisibleLogicalRangeChange(syncFromPnl);

    const applySharedTimeRange = () => {
      if (!syncTime || !sharedLogicalRange) return;
      try {
        const spacing = fullRangeBarSpacing(priceEl, [sharedTimeScale.length]);
        priceChart.timeScale().applyOptions({ barSpacing: spacing, minBarSpacing: 0.05 });
        pnlChart.timeScale().applyOptions({ barSpacing: spacing, minBarSpacing: 0.05 });
        priceChart.timeScale().fitContent();
        pnlChart.timeScale().fitContent();
        priceChart.timeScale().setVisibleLogicalRange(sharedLogicalRange);
        pnlChart.timeScale().setVisibleLogicalRange(sharedLogicalRange);
        updateAxisDebugAttrs();
      } catch {}
    };

    if (syncTime && sharedLogicalRange) {
      applySharedTimeRange();
      window.setTimeout(applySharedTimeRange, 80);
      window.setTimeout(applySharedTimeRange, 300);
      window.setTimeout(applySharedTimeRange, 900);
      window.setTimeout(applySharedTimeRange, 1800);
    } else {
      priceChart.timeScale().fitContent();
      pnlChart.timeScale().fitContent();
    }

    return () => {
      disposed = true;
      try {
        priceChart.timeScale().unsubscribeVisibleLogicalRangeChange(syncFromPrice);
        pnlChart.timeScale().unsubscribeVisibleLogicalRangeChange(syncFromPnl);
        manager?.detach?.();
      } catch {}
      priceChart.remove();
      pnlChart.remove();
    };
  }, [
    backtestLine,
    backtestPriceLine,
    backtestRealizedLine,
    barSize,
    fitSignal,
    hasPnlData,
    hasPriceData,
    labels,
    liveCandles,
    liveLine,
    liveRealizedLine,
    markLine,
    markers,
    pnlLayers,
    priceLayers,
    sharedLogicalRange,
    sharedTimeScale,
    syncTime,
  ]);

  function togglePriceLayer(key: PriceLayerKey) {
    setPriceLayers((prev) => ({ ...prev, [key]: !prev[key] }));
  }

  function togglePnlLayer(key: PnlLayerKey) {
    setPnlLayers((prev) => ({ ...prev, [key]: !prev[key] }));
  }

  if (!hasPriceData && !hasPnlData) {
    return (
      <div style={{ border: `1px dashed ${colors.border}`, borderRadius: 8, padding: 14, color: colors.muted, background: colors.bg }}>
        No chart data
      </div>
    );
  }

  return (
    <div style={{ border: `1px solid ${colors.border}`, borderRadius: 8, background: colors.bg, overflow: 'hidden' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', padding: 10, borderBottom: `1px solid ${colors.border}`, background: '#0F172A' }}>
        <select value={barSize} onChange={(event) => setBarSize(event.target.value as BarSize)} style={controlStyle} title="Bar size">
          <option value="1m">1m</option>
          <option value="5m">5m</option>
          <option value="15m">15m</option>
          <option value="1h">1h</option>
        </select>
        <button style={controlStyle} onClick={() => setFitSignal((v) => v + 1)} title="Fit content">Fit</button>
        <label style={labelStyle}>
          <input type="checkbox" checked={syncTime} onChange={(event) => setSyncTime(event.target.checked)} />
          sync time
        </label>
        {([
          ['livePrice', 'live price'],
          ['backtestPrice', 'backtest price'],
          ['telemetryMark', 'mark'],
          ['events', 'events'],
          ['labels', 'labels'],
          ['parameters', 'parameters'],
        ] as [PriceLayerKey, string][]).map(([key, label]) => (
          <label key={key} style={{ ...labelStyle, color: priceLayers[key] ? colors.text : colors.muted }}>
            <input type="checkbox" checked={priceLayers[key]} onChange={() => togglePriceLayer(key)} />
            {label}
          </label>
        ))}
        <span style={{ marginLeft: 'auto', color: colors.muted, fontSize: 12 }}>
          interval: {barSize} | events: {markers.length} | labels: {labels.length}
        </span>
      </div>
      <div ref={priceRef} data-testid="live-price-chart" style={{ width: '100%', height: 520, minWidth: 0 }} />
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', padding: 10, borderTop: `1px solid ${colors.border}`, borderBottom: `1px solid ${colors.border}`, background: '#0F172A' }}>
        <strong style={{ color: colors.text, fontSize: 13, marginRight: 4 }}>PNL</strong>
        {([
          ['livePnl', 'live total'],
          ['backtestPnl', 'backtest total'],
          ['liveRealized', 'live realized'],
          ['backtestRealized', 'backtest realized'],
        ] as [PnlLayerKey, string][]).map(([key, label]) => (
          <label key={key} style={{ ...labelStyle, color: pnlLayers[key] ? colors.text : colors.muted }}>
            <input type="checkbox" checked={pnlLayers[key]} onChange={() => togglePnlLayer(key)} />
            {label}
          </label>
        ))}
      </div>
      <div ref={pnlRef} data-testid="live-pnl-chart" style={{ width: '100%', height: 320, minWidth: 0 }} />
    </div>
  );
}
