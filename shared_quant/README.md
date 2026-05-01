# shared_quant

Спільний тонкий шар утиліт для двох незалежних проектів:

```text
obw_platform/    # CEX/futures
dex_platform/    # DEX/LP
shared_quant/    # тільки справді спільне
```

## Правило

`shared_quant` не має знати нічого про futures-стратегії, Uniswap, LP NFT, exchange runners або UI.

Допустимо:
- YAML/JSON config IO
- stable config hash
- час і UTC
- базові метрики equity curve
- CSV/Parquet IO
- env/secrets helpers
- прості shared plots

Недопустимо:
- strategy logic
- live runner logic
- exchange-specific code
- Uniswap-specific math
- wallet/private-key logic
- backtester engines

## Install dependencies

Мінімально:

```bash
python3 -m pip install pyyaml pandas numpy matplotlib pyarrow
```

## Usage

```python
from shared_quant import load_yaml, stable_hash, parse_iso_to_epoch_s
```

## Import rule

Правильно:

```python
# obw_platform code
from shared_quant.config import load_yaml
```

```python
# dex_platform code
from shared_quant.time_utils import parse_iso_to_epoch_s
```

Неправильно:

```python
# shared_quant must never do this
from obw_platform.something import ...
from dex_platform.something import ...
```
