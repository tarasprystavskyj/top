#!/usr/bin/env python3
import argparse, sys, yaml
STATIC = {
 'fee_rate': 0.0005,
 'funding_rate_per_8h_bps': 0.0,
 'initial_equity_per_leg': 100.0,
 'slippage_per_side': 9.2387,
 'autoMerge': 0.0,
 'baseOrderPctEq': 1.5,
 'equityForSizingUSDT': 335,
 'minOrderUSDT': 2.0,
 'requireCloseAboveFullTP': 1.0,
 'requireCloseBelowDcaLevel': 1.0,
 'subSellCloseConfirmMode': 'breakeven',
 'useEquityPctBase': 1.0,
 'useEvenBars': 0,
 'useHighLowTouch': 0.0,
 'useLiveSyncStart': 0.0,
 'useTrendAdaptiveSizing': 1.0,
}

def get_params(cfg, side):
    keys=[f'strategy_params_{side}', side, f'{side}_params']
    for k in keys:
        if isinstance(cfg.get(k), dict): return cfg[k]
    return {}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('cfg'); args=ap.parse_args()
    with open(args.cfg) as f: cfg=yaml.safe_load(f)
    bad=[]
    for side in ['long','short']:
        p=get_params(cfg, side)
        for k,v in STATIC.items():
            if k in p and p[k] != v:
                bad.append((side,k,p[k],v))
    if bad:
        print('STATIC LOCK VIOLATIONS')
        for row in bad: print(row)
        sys.exit(1)
    print('OK static locks:', args.cfg)
if __name__=='__main__': main()
