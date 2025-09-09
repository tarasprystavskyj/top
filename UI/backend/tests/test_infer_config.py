import sys
from pathlib import Path

# Allow importing api_main directly
BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.append(str(BACKEND_DIR))

import api_main

def test_infer_config_from_session():
    cfg = api_main.infer_config_from_session('livecfg_cfg_avaai_t5m5000_3_5m')
    assert cfg and cfg.endswith('cfg_avaai_t5m5000_3.yaml')
