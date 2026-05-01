"""
shared_quant

Small shared utility layer for CEX/futures and DEX/LP projects.

Rule:
- Keep only genuinely shared, dependency-light helpers here.
- Do not import obw_platform or dex_platform from this package.
"""

__version__ = "0.1.0"

from .config import load_yaml, save_yaml, deep_merge, stable_hash
from .time_utils import parse_iso_to_epoch_s, epoch_s_to_utc_iso, utc_now_iso
from .metrics import max_drawdown, equity_curve_stats
from .io_utils import ensure_dir, read_table, write_table
from .env import load_dotenv_file, require_env, redact_secret
