import test from 'node:test';
import assert from 'node:assert/strict';
import { avgPriceDiffColorRule, computePollingIntervalMs, seriesToSvgPath } from '../utils/backtestValidation.js';

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
  const path = seriesToSvgPath([{ x: 0, y: 1 }, { x: 1, y: 2 }], 100, 80, 10);
  assert.ok(path.startsWith('M'));
  assert.ok(path.includes('L'));
});
