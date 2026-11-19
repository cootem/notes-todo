# Notes To-Do

A local, single-file web app that gathers every markdown checkbox (`- [ ]`) from a folder of
notes into one searchable, filterable page, and writes check-offs straight back to the source files.
Works with any collection of markdown files: personal notes, a project wiki, an Obsidian/Typora vault.

- Python 3 standard library only, no install step, no database
- Serves on `127.0.0.1` only; nothing leaves your machine
- Safe edits: a line is re-verified before it is written, and refused if it changed underneath you
- Optional "Research" button that asks the [Claude Code](https://claude.com/claude-code) CLI for the background of an item

## Quick start

```bash
python3 notes_todo.py --root ~/path/to/notes      # opens http://127.0.0.1:8765/
```

Then click the gear in the header to set the folder, skipped directories, editor and more.
Settings are saved to `settings.json` next to the script (copy `settings.example.json` to start from the defaults).

Launchers: `NotesTodo.sh` (Linux/macOS), `NotesTodo.cmd` (Windows). `--make-shortcut` creates a desktop/app-menu
entry and `--register-protocol` enables the page's Relaunch button.
On Linux the entry runs `/bin/bash NotesTodo-desktop.sh`, so it keeps working if a sync tool (e.g. Nextcloud)
strips the scripts' exec bit. Re-run `--make-shortcut` after moving or redeploying the files.

## Checkbox conventions

| Markdown | Meaning |
|---|---|
| `- [ ] text` | open |
| `- [x] text (11/5)` | done, with a completion stamp (`m/d`, ISO date or none; configurable) |
| `- [-] ~~text~~` | dropped / out of scope (strikethrough optional) |
| `    - text` under an item | a note on that item, shown beneath it in the app |

Bullets `-`, `*`, `+` and numbered lists are supported, including nested items. Fenced code blocks are ignored.

To add a note without opening the file, click the note button on an item, or "Add note" in the toast after ticking it.
The note is written as an indented `- text` line after the item and anything already nested under it, and Undo removes it.

## Settings

Open with the gear button. Stored in `settings.json`; applied immediately.

| Setting | Purpose |
|---|---|
| Folder to scan | Root of the markdown collection (empty = current directory; a relative path is resolved from the folder holding `settings.json`, e.g. `../..`) |
| File extensions / skip folders | What to scan; dot-folders are always skipped |
| App title | Header and tab title |
| Date options | Use `YYYY-MM-DD` from the nearest heading; cap file date by a year in the file name |
| Completion stamp | `(m/d)`, `(YYYY-MM-DD)` or none |
| Drop strikethrough | Whether dropping also writes `~~text~~` |
| Note on tick | Open the note box automatically when you tick an item |
| Editor command | e.g. `code {file}`; empty = Typora if found, else system default |
| Research | Enable, model, timeout, extra read-only tools (e.g. MCP), instructions describing your collection |

`--root` and `NOTES_ROOT` override the folder for one run; `--research-model` / `NOTES_TODO_RESEARCH_MODEL` override the model.

## Command line

`python3 notes_todo.py --list [--status open|done|dropped|all] [--since DAYS] [--folder NAME] [--grep WORDS] [--format text|md|json]`
prints the same parsed list without starting the server, which is handy for scripts and AI assistants.

## Docs

See [`docs/requirements.md`](docs/requirements.md) for the detailed functional and non-functional requirements.
