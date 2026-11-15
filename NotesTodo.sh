#!/usr/bin/env sh
# Notes To-Do launcher for Linux/macOS (Windows: NotesTodo.cmd)
exec python3 "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")/notes_todo.py" "$@"
