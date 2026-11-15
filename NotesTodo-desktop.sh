#!/usr/bin/env bash
# Desktop launcher helper: runs the app through an interactive bash so ~/.bashrc is loaded
# (the Research button's `claude -p` needs ANTHROPIC_API_KEY, which desktop launches lack).
exec bash -ic 'exec "$0" "$@"' python3 "$(dirname "$(readlink -f "$0")")/notes_todo.py" "$@"
