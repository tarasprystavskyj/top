import test from 'node:test';
import assert from 'node:assert/strict';
import {
  avgPriceDiffColorRule,
  computePollingIntervalMs,
  computeSharedDomain,
  preparePnlSeries,
  seriesToSvgPath,
} from '../utils/backtestValidation.js';

test('average price diff color rule', () => {
  assert.equal(avgPriceDiffColorRule(99, 100), 'red');
  assert.equal(avgPriceDiffColorRule(101, 100), 'green');
  assert.equal(avgPriceDiffColorRule(100, 100), 'neutral');
});

test('polling interval is bar-aligned with delay and floor', () => {
  assert.equal(computePollingIntervalMs(15), 17000);
  assert.equal(computePollingIntervalMs(300), 302000);
  assert.equal(computePollingIntervalMs(1), 5000);
});

test('series path is generated for chart rendering', () => {
  const domain = { xMin: 0, xMax: 10, yMin: 0, yMax: 10 };
  const path = seriesToSvgPath([{ x: 0, y: 1 }, { x: 10, y: 2 }], 100, 80, 10, domain);
  assert.ok(path.startsWith('M'));
  assert.ok(path.includes('L'));
});

test('computeSharedDomain uses one domain for both series', () => {
  const domain = computeSharedDomain([
    { data: [{ ts: '2026-01-01T00:00:00Z', value: 5 }, { ts: '2026-01-01T00:02:00Z', value: 8 }] },
    { data: [{ ts: '2026-01-01T00:01:00Z', value: -2 }, { ts: '2026-01-01T00:03:00Z', value: 1 }] },
  ]);
  assert.equal(domain.yMin, -2);
  assert.equal(domain.yMax, 8);
  assert.ok(domain.xMin < domain.xMax);
});

test('preparePnlSeries does not normalize by default', () => {
  const back = [{ ts: '2026-01-01T00:00:00Z', value: 2 }, { ts: '2026-01-01T00:01:00Z', value: 3 }];
  const live = [{ ts: '2026-01-01T00:00:30Z', value: 4 }, { ts: '2026-01-01T00:01:30Z', value: 7 }];
  const out = preparePnlSeries(back, live, false);
  assert.equal(out.backtest[0].value, 2);
  assert.equal(out.live[0].value, 4);
});
