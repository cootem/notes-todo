# Notes To-Do: Requirements

## 1. Purpose

Provide one place to see and act on every markdown task across a folder of notes, without moving the tasks
out of the files they live in. The markdown files remain the single source of truth.

## 2. Scope

In scope: scanning, display, filtering, check-off/drop/reopen written back to files, opening files in an editor,
optional AI "research" of an item, configuration, CLI listing.
Out of scope: creating tasks, due dates/reminders, multi-user sync, remote access, rich markdown rendering.

## 3. Functional requirements

### 3.1 Scanning
- F1. Recursively scan the configured root for files with configured extensions (default `.md`).
- F2. Skip configured folder names at any depth and always skip folders starting with `.`.
- F3. Recognise task lines: bullet (`-`, `*`, `+`, or `1.`/`1)`) + `[ ]`, `[x]`, `[X]` or `[-]`.
- F4. Ignore content inside fenced code blocks (``` or ~~~).
- F5. Preserve the heading path (up to six levels), the parent bullet text and nesting depth for each task.
- F6. Tolerate UTF-8 with/without BOM, LF/CRLF line endings, and undecodable bytes (round-trip safe).
- F7. A legacy item (`[ ]` whose text starts with `~~`) counts as dropped.

### 3.2 Dates
- F8. An item's date is the `YYYY-MM-DD` in the nearest enclosing heading (if enabled), else the file's modified date.
- F9. If enabled, a year in the file name caps the modified date at that year's end.
- F10. Dates derived from file modification are marked approximate (`~`) in the UI and CLI.

### 3.3 Display and filtering (web UI)
- F11. Group tasks by file, then by heading section; nested items are indented with their parent bullet shown as context.
- F12. Free-text filter across item text, file path, headings and parent bullet (all words must match); `/` focuses it.
- F13. Age filter (any, 2 weeks, month, 3 months, year), sort (newest first / by file name), toggles for done and dropped.
- F14. Per-top-level-folder chips with open counts: click toggles, double-click isolates.
- F15. Collapsible files and sections; collapse-all. Filter, sort, folder and collapse state persist in the browser.
- F16. Rescan on window focus and via a refresh button; light and dark themes follow the OS.

### 3.4 Editing
- F17. Complete: set `[x]` and append a completion stamp (`m/d`, ISO or none; never duplicated).
- F18. Reopen: set `[ ]` and remove a trailing stamp.
- F19. Drop: set `[-]` and, if enabled, wrap the text in `~~`. Restore reverses this. Items whose strikethrough spans multiple lines can't be restored automatically.
- F20. Before every write, verify the target line equals the scanned text; if moved, relocate by exact unique match; otherwise refuse, report, and rescan.
- F21. Writes are atomic (temp file + replace) and serialised; line endings and BOM are preserved.
- F22. Every change offers an Undo in the toast.
- F23. Open the file in an editor: the configured command (`{file}` placeholder), else Typora, else the OS default.

### 3.5 Research (optional)
- F24. Per-item button runs the Claude Code CLI non-interactively (`claude -p`) with a read-only tool allowlist (Read, Grep, Glob, ToolSearch + configured extras) and no permission prompts.
- F25. The prompt includes the item, its location, surrounding lines, and user-supplied collection instructions.
- F26. Runs in the background (max two concurrent, configurable timeout); result appears in a panel under the item; successful results are cached on disk and can be re-run.
- F27. Can be disabled entirely; the button is then hidden and the API refuses requests.

### 3.6 Settings
- F28. A gear button opens a dialog to edit all settings (see README); saving validates, writes `settings.json`, and applies without restart.
- F29. Invalid values (e.g. missing folder, bad timeout) are rejected with a message and leave the running configuration unchanged.
- F30. "Reset to defaults" loads built-in defaults into the form; they apply on Save.
- F31. Precedence for the folder: `--root` > `NOTES_ROOT` > settings value > current directory. The dialog says when an override is active.

### 3.7 Command line
- F32. `--list` prints tasks and exits, with `--status`, `--since`, `--folder` (repeatable), `--grep` and `--format text|md|json`.
- F33. `--make-shortcut`, `--register-protocol`, `--unregister-protocol` integrate with Windows and Linux desktops.
- F34. If the port is busy the app assumes an instance is running and opens (or reuses) its page instead of starting a second server.

## 4. Non-functional requirements

- N1. Python 3 standard library only; single script plus assets; runs on Linux, macOS and Windows.
- N2. Bind to `127.0.0.1` only. Reject requests whose `Host` is not loopback; POSTs require a loopback `Origin` (if sent) and `application/json`.
- N3. Writes are limited to files under the root with a configured extension (path traversal refused).
- N4. No telemetry or network calls other than the optional Claude CLI invocation.
- N5. Scans of a few thousand files complete in about a second; the UI stays responsive.
- N6. Personal data is never stored in the repo: `settings.json` and `research_cache.json` are git-ignored.

## 5. Configuration reference

`settings.json` keys and defaults are listed in `settings.example.json`:
`app_title`, `root`, `extensions`, `skip_dirs`, `use_heading_dates`, `year_from_filename`, `done_stamp`,
`drop_strikethrough`, `editor_command`, `research_enabled`, `research_model`, `research_timeout`, `research_tools`,
`research_context`.

## 6. HTTP API (internal)

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | the single-page UI |
| `/api/tasks` | GET | scan result plus UI metadata |
| `/api/settings` | GET / POST | read / validate-and-save settings |
| `/api/action` | POST | complete, reopen, drop, undrop a line |
| `/api/open` | POST | open a file in the editor |
| `/api/research` | GET / POST | research results / start a run |
| `/api/pages`, `/api/bye`, `/api/stop` | GET / POST | page tracking and shutdown |

## 7. Open ideas

- Optional due-date syntax (`📅 YYYY-MM-DD`), tag filters, multiple roots, a port setting in the dialog.
