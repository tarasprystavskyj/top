import test from 'node:test';
import assert from 'node:assert/strict';
import {
  applyLiveAutoRefreshToggle,
  buildLiveSummaryModel,
  buildRefreshSessionPlan,
  formatLiveTableCell,
  getLiveChartVisibility,
  inferLiveTableColumns,
  normalizeLiveChartPayload,
  normalizeLiveTableRows,
  selectLiveSessionState,
  shouldShowNoLiveSessions,
} from '../utils/liveSessionUi.js';

test('live session selection does not overwrite selectedPath', () => {
  const prev = {
    selectedPath: '/tmp/backtest.csv',
    selectedLivePath: '',
    liveInspect: { a: 1 },
    liveStatus: { b: 2 },
    liveCharts: { live: [{ ts: '2026-01-01T00:00:00Z', value: 1 }] },
    liveTables: { open_positions: [{ x: 1 }], orders: [{}], debug_events: [{}], stdio: [{}] },
  };
  const next = selectLiveSessionState(prev, '/live/a');
  assert.equal(next.selectedPath, '/tmp/backtest.csv');
  assert.equal(next.selectedLivePath, '/live/a');
  assert.equal(next.liveInspect, null);
  assert.equal(next.liveStatus, null);
});

test('live source block empty state helper', () => {
  assert.equal(shouldShowNoLiveSessions([], false), true);
  assert.equal(shouldShowNoLiveSessions([{ path: 'x' }], false), false);
  assert.equal(shouldShowNoLiveSessions([], true), false);
});

test('live summary model handles missing fields safely', () => {
  const model = buildLiveSummaryModel('/live/a', null, null, null);
  assert.equal(model.sessionPath, '/live/a');
  assert.equal(model.status, 'unknown');
  assert.equal(model.exchange, '—');
  assert.equal(model.timeframe, '—');
  assert.equal(model.openLegs, 0);
  assert.equal(model.filledOrders, 0);
  assert.equal(model.debugType, 'unknown');
});

test('live chart visibility rules', () => {
  assert.deepEqual(getLiveChartVisibility({ live: [{ ts: '2026-01-01T00:00:00Z', value: 1 }] }), { live: true, overlay: false, distance: false });
  assert.deepEqual(
    getLiveChartVisibility({ live: [{ ts: '2026-01-01T00:00:00Z', value: 1 }], backtest: [{ ts: '2026-01-01T00:00:00Z', value: 2 }] }),
    { live: true, overlay: true, distance: false },
  );
  assert.deepEqual(getLiveChartVisibility({ distance: [{ ts: '2026-01-01T00:00:00Z', value: 0.2 }] }), { live: false, overlay: false, distance: true });
});

test('live chart payload preserves source metadata and approximate fallback flag', () => {
  const normalized = normalizeLiveChartPayload({
    live: [{ ts: '2026-01-01T00:00:00Z', value: 1 }],
    sources: { live: 'session.sqlite:orders' },
    warnings: ['approximate'],
  });
  assert.equal(normalized.approximate, true);
  assert.equal(normalized.sources.live, 'session.sqlite:orders');
  assert.deepEqual(normalized.warnings, ['approximate']);
});

test('live table panels empty and tabular helpers', () => {
  assert.deepEqual(normalizeLiveTableRows(null), []);
  const rows = normalizeLiveTableRows([{ symbol: 'ENA', qty: 1 }, { symbol: 'BTC', price: 2 }, 'bad-row']);
  const columns = inferLiveTableColumns(rows);
  assert.ok(columns.includes('symbol'));
  assert.ok(columns.includes('qty') || columns.includes('price') || columns.includes('value'));
});

test('formatLiveTableCell formats numbers and timestamps safely', () => {
  assert.equal(formatLiveTableCell('ts', '2026-04-10T00:01:02Z'), '2026-04-10 00:01:02');
  assert.equal(formatLiveTableCell('price', 1.23456789), '1.234568');
  assert.equal(formatLiveTableCell('x', null), '—');
});

test('live auto-refresh toggle does not alter backtest polling state', () => {
  const next = applyLiveAutoRefreshToggle({ autoRefresh: true, liveAutoRefresh: false }, true);
  assert.equal(next.liveAutoRefresh, true);
  assert.equal(next.autoRefresh, true);
});

test('refresh session plan reloads inspect and status for selected session', () => {
  const plan = buildRefreshSessionPlan('/reports/live/session_a');
  assert.equal(plan.length, 2);
  assert.equal(plan[0].url, '/api/backtest_live_validation/live_session/inspect');
  assert.ok(plan[1].url.includes('/api/backtest_live_validation/live_session/status?path='));
});

test('malformed live table rows do not crash helper pipeline', () => {
  const rows = normalizeLiveTableRows([undefined, 1, 'x', ['nested'], { ok: true }]);
  const columns = inferLiveTableColumns(rows);
  assert.ok(columns.length > 0);
});
