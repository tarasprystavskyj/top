#!/usr/bin/env python3
from __future__ import annotations

"""
Hard patch for BSC/PoA web3.py ExtraDataLengthError.

Why v1 may not have worked:
- It patched only by pattern and may not have been run.
- Different web3.py versions expose PoA middleware under different names.
- If no "[web3] injected ..." line appears before get_block(), the collector is still unpatched.

This patch inserts:
  - a version-tolerant _inject_poa_middleware()
  - a mandatory call immediately after every:
      w3 = Web3(Web3.HTTPProvider(...))

Targets:
  dex_platform/data_collectors/fetch_aerodrome_slipstream_events_v2.py
  dex_platform/data_collectors/check_evm_rpc_pool_v1.py
  dex_platform/data_collectors/debug_evm_pool_logs_topics_v1.py
"""

from pathlib import Path
import re


TARGETS = [
    Path("dex_platform/data_collectors/fetch_aerodrome_slipstream_events_v2.py"),
    Path("dex_platform/data_collectors/check_evm_rpc_pool_v1.py"),
    Path("dex_platform/data_collectors/debug_evm_pool_logs_topics_v1.py"),
]


HELPER = r