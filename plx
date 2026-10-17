#!/bin/sh
# PredixLab local staging CLI (Apple container). See README.md.
here="$(cd "$(dirname "$0")" && pwd)"
PYTHONPATH="$here/local${PYTHONPATH:+:$PYTHONPATH}" exec "${PLX_PYTHON:-python3}" -m plx "$@"
