#!/usr/bin/env bash
# Create the project's Python venv in venv (if it doesn't exist yet) and
# install everything in requirements.txt into it.
#
#   ./make_venv.sh                      # use python3 from PATH
#   ./make_venv.sh /path/to/python3.12  # use a specific interpreter
#
# Delete venv and re-run to recreate it with a different interpreter.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

PYTHON="${1:-python3}"
VENV="venv"

if [ ! -x "$VENV/bin/python" ]; then
    echo "Creating $VENV with $PYTHON ($("$PYTHON" --version 2>&1))"
    "$PYTHON" -m venv "$VENV"
else
    echo "Using existing $VENV ($("$VENV/bin/python" --version 2>&1))"
fi

"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r requirements.txt
echo "Done. Run ./logcap, or 'source $VENV/bin/activate' to use the venv directly."
