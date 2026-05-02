#!/usr/bin/env python3
from __future__ import annotations

"""
dex_platform/patches/patch_bsc_poa_middleware_v1.py

Problem:
  BSC is a PoA-style chain. web3.py raises:
    ExtraDataLengthError: The field extraData is 280/517 bytes, but should be 32.

Fix:
  Inject web3 geth_poa_middleware / ExtraDataToPOAMiddleware immediately after Web3 provider creation.

This patcher modifies:
  - dex_platform/data_collectors/fetch_aerodrome_slipstream_events_v2.py
  - dex_platform/data_collectors/check_evm_rpc_pool_v1.py
  - dex_platform/data_collectors/debug_evm_pool_logs_topics_v1.py
"""

from pathlib import Path
import re
import sys


TARGETS = [
    Path("dex_platform/data_collectors/fetch_aerodrome_slipstream_events_v2.py"),
    Path("dex_platform/data_collectors/check_evm_rpc_pool_v1.py"),
    Path("dex_platform/data_collectors/debug_evm_pool_logs_topics_v1.py"),
]


HELPER = r