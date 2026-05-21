#!/usr/bin/env python3
"""Update the margin-zero basket report with extrapolated profitability.

This is report-artifact maintenance only. It does not run backtests, tune
configs, or touch production configuration.
"""

from __future__ import annotations

import json
from pathlib import Path


REPORT_DIR = Path(__file__).resolve().parent
JSON_PATH = REPORT_DIR / "latest_margin_zero_codex.json"
MD_PATH = REPORT_DIR / "latest_margin_zero_codex.md"
CHART_PATH = REPORT_DIR / "basket_mtm_endpoint_summary.png"


def pct(value: float) -> str:
    return f"{value:.2f}%"


def make_chart(terminal_return_pct: float) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 5))
    months = [0, 12]
    basket = [0.0, terminal_return_pct]
    ax.plot(months, basket, color="#2563eb", linewidth=2.5, marker="o", label="Equal-weight basket")
    ax.axhline(0, color="#6b7280", linewidth=0.9)
    ax.fill_between(months, 0, basket, color="#93c5fd", alpha=0.25)
    ax.set_title("Four-Symbol Basket MTM - endpoint summary")
    ax.set_xlabel("Full-year interval assumption (months)")
    ax.set_ylabel("MTM return on start, %")
    ax.set_xticks([0, 3, 6, 9, 12])
    ax.grid(True, alpha=0.25)
    ax.legend(loc="upper left")
    ax.text(
        0.01,
        -0.22,
        "Summary-derived placeholder: no per-bar/per-symbol MTM curves were present in report artifacts.",
        transform=ax.transAxes,
        fontsize=9,
        color="#374151",
    )
    ax.annotate(
        pct(terminal_return_pct),
        xy=(12, terminal_return_pct),
        xytext=(-58, 10),
        textcoords="offset points",
        fontsize=10,
        color="#111827",
        arrowprops={"arrowstyle": "->", "color": "#374151", "linewidth": 0.8},
    )
    fig.tight_layout()
    fig.savefig(CHART_PATH, dpi=160)
    plt.close(fig)


def replace_basket_table(md: str, monthly_pct: float, yearly_pct: float) -> str:
    if "| extrapolated monthly basket profitability |" in md:
        return md
    old = """| metric | value |
| --- | ---: |
| symbols | 4 |
| equal-weight terminal return approximation | 57.45% |
| worst single-symbol MTM drawdown | -50.20% |
| total trades | 19730 |
| total margin-call events | 0 |
| total bars in margin call | 0 |
"""
    new = f"""| metric | value |
| --- | ---: |
| symbols | 4 |
| equal-weight terminal return approximation | 57.45% |
| extrapolated monthly basket profitability | {pct(monthly_pct)} |
| extrapolated yearly basket profitability | {pct(yearly_pct)} |
| worst single-symbol MTM drawdown | -50.20% |
| total trades | 19730 |
| total margin-call events | 0 |
| total bars in margin call | 0 |
"""
    if old not in md:
        raise RuntimeError("Basket validation table block not found")
    return md.replace(old, new, 1)


def insert_note_and_chart(md: str) -> str:
    marker = (
        "Conservative fallback basket: replacing `SUP` with "
        "`V21_sup_margin_zero_budget32_fast_exit.yaml`"
    )
    note = (
        "Profitability extrapolation note: using the existing equal-weight terminal return "
        "approximation `R = 57.45029183355874%` and the report's full-year context, "
        "yearly profitability is `R`; monthly profitability is compounded as "
        "`(1 + R)^(1/12) - 1 = 3.8553%`.\n\n"
        "Basket MTM chart: ![Four-symbol basket MTM endpoint summary](basket_mtm_endpoint_summary.png)\n\n"
        "Chart limitation: no full-interval per-symbol MTM curve files were present in "
        "`reports/` or `_reports/`, and the non-SUP NPZ caches needed to regenerate exact "
        "curves are missing in this checkout. The PNG is therefore an explicitly "
        "summary-derived endpoint placeholder, not an actual per-bar MTM time-series.\n\n"
    )
    if note in md:
        return md
    idx = md.find(marker)
    if idx < 0:
        raise RuntimeError("Fallback paragraph marker not found")
    return md[:idx] + note + md[idx:]


def main() -> None:
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    basket = data["basket_validation"]
    terminal_pct = float(basket["equal_weight_terminal_return_pct"])
    terminal = terminal_pct / 100.0
    monthly_pct = ((1.0 + terminal) ** (1.0 / 12.0) - 1.0) * 100.0
    yearly_pct = terminal_pct

    new_fields = {
        "extrapolated_monthly_basket_profitability_pct": monthly_pct,
        "extrapolated_yearly_basket_profitability_pct": yearly_pct,
        "profitability_extrapolation_interval_assumption": "full-year report context; exact timestamps unavailable in summary artifacts",
        "profitability_extrapolation_formula": "monthly=(1+equal_weight_terminal_return_pct/100)^(1/12)-1; yearly=equal_weight_terminal_return_pct/100",
        "basket_mtm_chart": str(CHART_PATH.relative_to(REPORT_DIR)).replace("\\", "/"),
        "basket_mtm_chart_type": "summary-derived endpoint placeholder",
        "basket_mtm_chart_limitation": "No full-interval per-symbol MTM curve files were present in reports/_reports, and non-SUP NPZ caches required to regenerate exact curves are missing.",
        "source_curve_files_found": False,
    }
    if any(basket.get(key) != value for key, value in new_fields.items()):
        basket.update(new_fields)
        JSON_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    md = MD_PATH.read_text(encoding="utf-8")
    md = replace_basket_table(md, monthly_pct, yearly_pct)
    md = insert_note_and_chart(md)
    MD_PATH.write_text(md, encoding="utf-8")

    make_chart(terminal_pct)
    print(json.dumps({"json": str(JSON_PATH), "markdown": str(MD_PATH), "chart": str(CHART_PATH)}, indent=2))


if __name__ == "__main__":
    main()
