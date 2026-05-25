#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python research/research_strategy_versions_relative.py
