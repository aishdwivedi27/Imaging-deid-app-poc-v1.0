#!/usr/bin/env bash
# ./start_mac_linux.sh   - needs Python 3.10, 3.11 or 3.12 (3.13+ is too new for rapidocr-onnxruntime)
set -e
cd "$(dirname "$0")"
pick() { for v in 3.12 3.11 3.10; do command -v "python$v" >/dev/null 2>&1 && { echo "python$v"; return; }; done; }
if [ -x .venv/bin/python ]; then
  v=$(.venv/bin/python -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
  case "$v" in 3.10|3.11|3.12) ;; *) echo "Existing .venv uses Python $v (not supported). Removing it..."; rm -rf .venv;; esac
fi
if [ ! -x .venv/bin/python ]; then
  PY=$(pick)
  if [ -z "$PY" ]; then echo "Python 3.12 not found. Install it (e.g. 'brew install python@3.12' or 'sudo apt install python3.12 python3.12-venv') and re-run."; exit 1; fi
  echo "First run: creating virtual environment with $PY and installing packages (5-10 minutes)..."
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt || { echo "Package install failed - see above."; rm -rf .venv; exit 1; }
  .venv/bin/python -m spacy download en_core_web_sm || echo "spaCy model download failed - name detection will be switched off."
fi
if [ -f ohif-local/viewer/index.html ]; then
  .venv/bin/python serve_ohif.py --no-browser & OHIF_PID=$!
  trap 'kill $OHIF_PID 2>/dev/null; echo "OHIF viewer stopped."' EXIT
  echo "OHIF viewer: http://localhost:3000/local"
else
  echo "OHIF viewer not installed (extract ohif-local-viewer-part1/2.zip here to enable it)."
fi
.venv/bin/python -m app.server
