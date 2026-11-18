#!/bin/bash
# Double-click to start Shir. Uses the floating window if pywebview is installed, else your browser.
cd "$(dirname "$0")"
if python3 -c "import webview" 2>/dev/null; then python3 shir.py --float; else python3 shir.py; fi
