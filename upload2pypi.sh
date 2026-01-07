#!/usr/bin/env bash
set -euo pipefail

rm -rf dist
python3 -m build
python3 -m twine upload dist/*
