#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON="$PROJECT_DIR/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
    if command -v zenity >/dev/null 2>&1; then
        zenity --error --title="MySQL Anonymized Copy" --text="Project environment not found. Open a terminal in the project folder and run:\n\npython3 -m venv .venv\n.venv/bin/python -m pip install -e ." || true
    else
        printf '%s\n' "Project environment not found. Run: python3 -m venv .venv && .venv/bin/python -m pip install -e ." >&2
    fi
    exit 1
fi

exec "$PYTHON" -m mysql_anonymizer.gui
