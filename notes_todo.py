"""Notes To-Do - a local web view of every `- [ ]` across a folder of markdown files.

Run:  python notes_todo.py [--root PATH] [--port 8765] [--no-browser]
      (Linux/macOS: python3, or ./NotesTodo.sh)

Standard library only. Binds to 127.0.0.1. Check-offs are written back to the
source line; before writing, the line is verified to still match what was
scanned (if it moved, it is relocated by exact text; if it changed, the write is
refused and the page rescans).
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Default folder when no root is configured: the current directory.
# Override in the settings dialog (gear), settings.json, --root or NOTES_ROOT.
DEFAULT_ROOT = Path.cwd()
TYPORA_CANDIDATES = [
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Typora" / "Typora.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Typora" / "Typora.exe",
]
IS_WINDOWS = sys.platform == "win32"

# ---------------------------------------------------------------- settings (settings.json beside this script)

SETTINGS_FILE = Path(__file__).resolve().with_name("settings.json")
DEFAULT_SETTINGS = {
    "app_title": "Markdown To-Do",
    "root": "",                         # "" = auto (see DEFAULT_ROOT); --root / NOTES_ROOT override it for one run
    "extensions": [".md"],              # file types to scan and edit
    "skip_dirs": [".git", ".obsidian", "node_modules", ".claude"],   # hidden (.x) dirs always skipped
    "use_heading_dates": True,          # take an item's date from the nearest heading containing YYYY-MM-DD
    "year_from_filename": True,         # cap a file's modified date at a year found in its name (journal_2023.md)
    "done_stamp": "m/d",                # appended when ticking: "m/d" -> (11/5), "iso" -> (2026-11-05), "none"
    "drop_strikethrough": True,         # dropping writes - [-] ~~text~~ (off: only the [-] marker)
    "editor_command": "",               # "" = Typora if found, else the system default; e.g. "code {file}"
    "research_enabled": True,           # show the Research button (needs the Claude Code CLI)
    "research_model": "sonnet",
    "research_timeout": 300,            # seconds
    "research_tools": [],               # extra read-only tools for research, e.g. MCP tool names
    "research_context": "The current folder is a collection of markdown notes. Search it with Grep/Glob/Read for "
                        "related notes. Prefer the most recent relevant material.",
}
SETTINGS = dict(DEFAULT_SETTINGS)
ROOT_OVERRIDE = None                    # set from --root / NOTES_ROOT in main()
SKIP_DIRS = set(DEFAULT_SETTINGS["skip_dirs"])
EXTENSIONS = tuple(DEFAULT_SETTINGS["extensions"])


def clean_settings(raw: dict) -> dict:
    """Validate/normalise a settings dict (unknown keys dropped, bad values raise ValueError)."""
    out = dict(DEFAULT_SETTINGS)
    def lst(v):
        if isinstance(v, str):
            v = re.split(r"[,\n]", v)
        return [x.strip() for x in v if isinstance(x, str) and x.strip()]
    for k, d in DEFAULT_SETTINGS.items():
        if k not in raw:
            continue
        v = raw[k]
        if isinstance(d, bool):
            out[k] = bool(v)
        elif isinstance(d, int):
            out[k] = int(v)
        elif isinstance(d, list):
            out[k] = lst(v)
        else:
            out[k] = str(v).strip()
    if out["done_stamp"] not in ("m/d", "iso", "none"):
        raise ValueError("done_stamp must be m/d, iso or none")
    if not 10 <= out["research_timeout"] <= 3600:
        raise ValueError("Research timeout must be 10-3600 seconds")
    exts = [("." + e.lstrip(".")).lower() for e in out["extensions"]]
    out["extensions"] = exts or [".md"]
    if not out["app_title"]:
        out["app_title"] = DEFAULT_SETTINGS["app_title"]
    return out


def effective_root(st: dict) -> Path:
    if ROOT_OVERRIDE:
        return Path(ROOT_OVERRIDE).expanduser().resolve()
    return Path(st["root"]).expanduser().resolve() if st["root"] else DEFAULT_ROOT


def apply_settings(st: dict, check_root: bool = True):
    global SETTINGS, SKIP_DIRS, EXTENSIONS, ROOT, RESEARCH_MODEL, RESEARCH_TIMEOUT
    root = effective_root(st)
    if check_root and not root.is_dir():
        raise ValueError(f"Folder not found: {root}")
    SETTINGS = st
    ROOT = root
    SKIP_DIRS = set(st["skip_dirs"])
    EXTENSIONS = tuple(st["extensions"])
    RESEARCH_MODEL = st["research_model"]
    RESEARCH_TIMEOUT = st["research_timeout"]


def load_settings():
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return clean_settings(raw if isinstance(raw, dict) else {})
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)


def save_settings(st: dict):
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, SETTINGS_FILE)


def settings_payload() -> dict:
    return {"settings": SETTINGS, "defaults": DEFAULT_SETTINGS, "root": str(ROOT),
            "rootOverridden": bool(ROOT_OVERRIDE)}

TASK_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<bullet>[-*+]|\d+[.)])[ \t]+\[(?P<mark>[ xX-])\][ \t]+(?P<text>.*?)[ \t]*$")
BULLET_RE = re.compile(r"^(?P<indent>[ \t]*)(?:[-*+]|\d+[.)])[ \t]+(?P<text>.*?)[ \t]*$")
HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})[ \t]+(?P<text>.*?)[ \t]*#*[ \t]*$")
FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")
ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
DONE_DATE_RE = re.compile(r"[ \t]*\((?:\d{1,2}/\d{1,2}(?:/\d{2,4})?|\d{4}-\d{2}-\d{2})\)$")
STRUCK_RE = re.compile(r"^~~")                 # text starts struck = dropped (a note may follow)
STRUCK_SPAN_RE = re.compile(r"^~~(.+?)~~(.*)$")

ROOT: Path = DEFAULT_ROOT
PAGES = {}                      # page id -> last poll time; lets a 2nd launch know a page is already open
PAGE_TTL = 90                   # seconds (background tabs are throttled, so be generous)
WRITE_LOCK = threading.Lock()


# ---------------------------------------------------------------- file I/O

def read_lines(path: Path):
    """Return (lines_without_eol, eols, has_bom)."""
    raw = path.read_bytes()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig" if has_bom else "utf-8", errors="surrogateescape")
    parts = text.splitlines(keepends=True)
    lines, eols = [], []
    for p in parts:
        body = p.rstrip("\r\n")
        lines.append(body)
        eols.append(p[len(body):])
    return lines, eols, has_bom


def write_lines(path: Path, lines, eols, has_bom):
    text = "".join(l + e for l, e in zip(lines, eols))
    data = text.encode("utf-8", errors="surrogateescape")
    if has_bom:
        data = b"\xef\xbb\xbf" + data
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".todo-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def indent_width(s: str) -> int:
    return len(s.replace("\t", "    "))


# ---------------------------------------------------------------- scanning

def scan_file(path: Path):
    rel = path.relative_to(ROOT).as_posix()
    try:
        lines, _, _ = read_lines(path)
    except (OSError, UnicodeError):
        return []
    mtime = dt.datetime.fromtimestamp(path.stat().st_mtime).date()
    ym = re.search(r"(?<!\d)(20\d\d)(?!\d)", path.stem) if SETTINGS["year_from_filename"] else None
    if ym:                                                       # e.g. journal_2023: cap at that year's end
        mtime = min(mtime, dt.date(int(ym.group(1)), 12, 31))
    mtime = mtime.isoformat()
    tasks = []
    headings = []          # [(level, text)]
    stack = []             # [(indent, task_id or None, bullet_text)]
    in_fence = False
    for i, line in enumerate(lines):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        h = HEADING_RE.match(line)
        if h:
            level = len(h["hashes"])
            headings = [x for x in headings if x[0] < level] + [(level, h["text"])]
            stack = []
            continue
        t = TASK_RE.match(line)
        b = t or BULLET_RE.match(line)
        if not b:
            if line.strip() and indent_width(line[: len(line) - len(line.lstrip())]) == 0:
                stack = []   # a paragraph at column 0 ends the list
            continue
        ind = indent_width(b["indent"])
        while stack and stack[-1][0] >= ind:
            stack.pop()
        if not t:
            stack.append((ind, None, b["text"]))
            continue
        tid = f"{rel}:{i + 1}"
        parent_task = next((s[1] for s in reversed(stack) if s[1]), None)
        context = stack[-1][2] if stack and stack[-1][1] is None else None
        heading_path = [x[1] for x in headings]
        date, date_src = None, "file"
        for _, htext in (reversed(headings) if SETTINGS["use_heading_dates"] else ()):
            m = ISO_DATE_RE.search(htext)
            if m:
                date, date_src = m.group(0), "heading"
                break
        text = t["text"]
        tasks.append({
            "id": tid,
            "file": rel,
            "folder": rel.split("/")[0] if "/" in rel else "(root)",
            "line": i + 1,
            "raw": line,
            "text": text,
            "done": t["mark"] in "xX",
            # [-] is the cancelled marker (Obsidian Tasks et al.); legacy: unchecked + text struck through
            "dropped": t["mark"] == "-" or (t["mark"] == " " and bool(STRUCK_RE.match(text))),
            "depth": len([s for s in stack]),
            "parent": parent_task,
            "context": context,
            "headings": heading_path,
            "date": date or mtime,
            "dateSource": date_src,
        })
        stack.append((ind, tid, text))
    return tasks


def scan_all():
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.lower().endswith(EXTENSIONS):
                out.extend(scan_file(Path(dirpath) / fn))
    return out


def open_pages() -> int:
    now = time.time()
    for k in [k for k, t in PAGES.items() if now - t > PAGE_TTL]:
        PAGES.pop(k, None)
    return len(PAGES)


def page_already_open(port: int) -> bool:
    """Ask the running instance whether a browser page is currently polling it."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/pages", timeout=2) as r:
            return json.load(r).get("open", 0) > 0
    except Exception:  # noqa: BLE001 — old instance or not ours: just open a page
        return False


def notify(msg: str):
    import shutil
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-i", "text-x-generic", "Notes To-Do", msg], check=False)


# ---------------------------------------------------------------- editing

def safe_path(rel: str) -> Path:
    p = (ROOT / rel).resolve()
    if ROOT.resolve() not in p.parents or p.suffix.lower() not in EXTENSIONS:
        raise ValueError("path outside the scanned folder")
    return p


def transform(line: str, action: str, today: dt.date) -> str:
    t = TASK_RE.match(line)
    if not t:
        raise ValueError("not a task line")
    head = line[: t.start("mark")]           # indent + bullet + " ["
    text = t["text"]
    mark = t["mark"]
    if action == "complete":
        mark = "x"
        stamp = SETTINGS["done_stamp"]
        if stamp != "none" and not DONE_DATE_RE.search(text):
            text = f"{text} ({today.isoformat()})" if stamp == "iso" else f"{text} ({today.month}/{today.day})"
    elif action == "reopen":
        mark = " "
        text = DONE_DATE_RE.sub("", text)
    elif action == "drop":
        mark = "-"
        if SETTINGS["drop_strikethrough"] and not STRUCK_RE.match(text):
            text = f"~~{text}~~"
    elif action == "undrop":
        mark = " "
        m = STRUCK_SPAN_RE.match(text)
        if m:
            text = m.group(1) + m.group(2)
        elif STRUCK_RE.match(text):
            raise ValueError("That strikethrough continues onto the next line; restore it in Typora.")
    else:
        raise ValueError("unknown action")
    return f"{head}{mark}] {text}"


def apply_action(rel: str, line_no: int, expect: str, action: str):
    path = safe_path(rel)
    with WRITE_LOCK:
        lines, eols, bom = read_lines(path)
        idx = line_no - 1
        if not (0 <= idx < len(lines) and lines[idx] == expect):
            hits = [k for k, l in enumerate(lines) if l == expect]
            if len(hits) != 1:
                return {"ok": False, "error": "The line changed since the last scan — refreshed. Try again."}
            idx = hits[0]
        new = transform(lines[idx], action, dt.date.today())
        lines[idx] = new
        write_lines(path, lines, eols, bom)
    return {"ok": True, "line": idx + 1, "raw": new}


def open_in_editor(rel: str):
    path = safe_path(rel)
    custom = SETTINGS["editor_command"]
    quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if custom:
        import shlex
        argv = shlex.split(custom, posix=not IS_WINDOWS)
        if any("{file}" in a for a in argv):
            argv = [a.replace("{file}", str(path)) for a in argv]
        else:
            argv.append(str(path))
        subprocess.Popen(argv, start_new_session=not IS_WINDOWS, **quiet)
        return
    if IS_WINDOWS:
        for exe in TYPORA_CANDIDATES:
            if exe.is_file():
                subprocess.Popen([str(exe), str(path)])
                return
        os.startfile(str(path))  # default .md handler
        return
    import shutil
    typora = shutil.which("typora")
    cmd = [typora] if typora else (["open"] if sys.platform == "darwin" else ["xdg-open"])
    subprocess.Popen(cmd + [str(path)], start_new_session=True, **quiet)


# ---------------------------------------------------------------- research (claude -p)

RESEARCH_BASE_TOOLS = ["Read", "Grep", "Glob", "ToolSearch"]   # plus Settings > research_tools
RESEARCH_TIMEOUT = 300
RESEARCH_MODEL = "sonnet"                   # settings.json, or --research-model / NOTES_TODO_RESEARCH_MODEL
RESEARCH_CACHE = Path(__file__).resolve().with_name("research_cache.json")
RESEARCH_LOCK = threading.Lock()
RESEARCH_SLOTS = threading.Semaphore(2)     # at most two runs at once
RESEARCH = {}                               # key -> {status, result, error, finished, seconds, cost}


def research_key(rel: str, text: str) -> str:
    return f"{rel}|{DONE_DATE_RE.sub('', text).strip()}"


def load_research_cache():
    try:
        data = json.loads(RESEARCH_CACHE.read_text(encoding="utf-8"))
        RESEARCH.update({k: v for k, v in data.items() if v.get("status") == "done"})
    except (OSError, ValueError):
        pass


def save_research_cache():
    done = {k: v for k, v in RESEARCH.items() if v.get("status") == "done"}
    tmp = RESEARCH_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(done, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, RESEARCH_CACHE)


def find_claude() -> str:
    import shutil
    exe = shutil.which("claude")
    if exe:
        return exe
    fallback = Path.home() / ".local" / "bin" / ("claude.exe" if IS_WINDOWS else "claude")
    if fallback.exists():
        return str(fallback)
    raise FileNotFoundError("Claude Code CLI ('claude') not found on PATH")


def research_prompt(task: dict) -> str:
    try:
        lines, _, _ = read_lines(safe_path(task["file"]))
        i = task["line"] - 1
        lo, hi = max(0, i - 12), min(len(lines), i + 13)
        excerpt = "\n".join(("> " if k == i else "  ") + lines[k] for k in range(lo, hi))
    except (OSError, ValueError):
        excerpt = "(unavailable)"
    where = " › ".join(task.get("headings") or []) or "(no heading)"
    ctx = f'\nParent bullet: "{task["context"]}"' if task.get("context") else ""
    return f"""You are helping me recall the background of a to-do item from my notes. Today is {dt.date.today().isoformat()}.
Research only: do not edit, create, send or delete anything.

To-do: "{task["text"]}"
Location: {task["file"]}, line {task["line"]}, under: {where}{ctx}
Surrounding lines (the item is marked >):
{excerpt}

{SETTINGS["research_context"]}

Reply in markdown, under 180 words, exactly these sections:
**Background**: 2-4 sentences: where this came from, who asked, why.
**Sources**: bullets: note file paths, or calendar event or task with date.
**Open questions / next step**: 1-3 bullets.
If you find nothing relevant, say so plainly instead of guessing."""


def run_research(key: str, task: dict):
    import time
    started = time.time()
    try:
        with RESEARCH_SLOTS:
            cmd = [find_claude(), "-p", "--output-format", "json", "--permission-mode", "dontAsk",
                   "--permission-prompts", "none", "--no-session-persistence", "--allowedTools", ",".join(RESEARCH_BASE_TOOLS + SETTINGS["research_tools"])]
            if RESEARCH_MODEL:
                cmd += ["--model", RESEARCH_MODEL]
            proc = subprocess.run(cmd, input=research_prompt(task), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", cwd=str(ROOT), timeout=RESEARCH_TIMEOUT,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = json.loads(proc.stdout) if proc.stdout.strip() else {}
        if proc.returncode != 0 or out.get("is_error") or not out.get("result"):
            raise RuntimeError(out.get("result") or proc.stderr.strip()[-400:] or f"claude exited {proc.returncode}")
        entry = {"status": "done", "result": out["result"], "finished": dt.datetime.now().isoformat(timespec="minutes"),
                 "seconds": round(time.time() - started), "cost": out.get("total_cost_usd")}
    except subprocess.TimeoutExpired:
        entry = {"status": "error", "error": f"Timed out after {RESEARCH_TIMEOUT // 60} minutes."}
    except Exception as e:  # noqa: BLE001
        entry = {"status": "error", "error": str(e)}
    with RESEARCH_LOCK:
        RESEARCH[key] = entry
        if entry["status"] == "done":
            save_research_cache()


def start_research(task: dict, force: bool) -> dict:
    key = research_key(task["file"], task["text"])
    with RESEARCH_LOCK:
        cur = RESEARCH.get(key)
        if cur and (cur["status"] == "running" or (cur["status"] == "done" and not force)):
            return {"key": key, **cur}
        RESEARCH[key] = {"status": "running"}
    threading.Thread(target=run_research, args=(key, task), daemon=True).start()
    return {"key": key, "status": "running"}


# ---------------------------------------------------------------- HTTP

def esc_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, '{"error":"forbidden"}')
        route = urlparse(self.path).path
        if route == "/":
            return self._send(200, PAGE.replace("{{TITLE}}", esc_html(SETTINGS["app_title"])), "text/html; charset=utf-8")
        if route in ("/icon.png", "/favicon.ico"):
            try:
                data = Path(__file__).resolve().with_name("NotesTodo.png").read_bytes()
            except OSError:
                return self._send(404, '{"error":"not found"}')
            return self._send(200, data, "image/png")
        if route == "/api/pages":
            return self._send(200, json.dumps({"open": open_pages()}))
        if route == "/api/tasks":
            pid = parse_qs(urlparse(self.path).query).get("pid", [""])[0]
            if pid:
                PAGES[pid] = time.time()
            return self._send(200, json.dumps({"root": str(ROOT), "today": dt.date.today().isoformat(),
                                               "relaunch": protocol_registered(), "title": SETTINGS["app_title"],
                                               "researchEnabled": SETTINGS["research_enabled"],
                                               "researchTools": bool(SETTINGS["research_tools"]),
                                               "doneStamp": SETTINGS["done_stamp"],
                                               "dropStrike": SETTINGS["drop_strikethrough"], "tasks": scan_all()}))
        if route == "/api/settings":
            return self._send(200, json.dumps(settings_payload()))
        if route == "/api/research":
            with RESEARCH_LOCK:
                return self._send(200, json.dumps(RESEARCH))
        self._send(404, '{"error":"not found"}')

    def do_POST(self):
        origin = self.headers.get("Origin")
        if not self._host_ok() or (origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost")):
            return self._send(403, '{"error":"forbidden"}')
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._send(415, '{"error":"json required"}')
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            route = urlparse(self.path).path
            if route == "/api/action":
                res = apply_action(body["file"], int(body["line"]), body["raw"], body["action"])
                return self._send(200 if res["ok"] else 409, json.dumps(res))
            if route == "/api/settings":
                st = clean_settings(body.get("settings") or {})
                if body.get("reset"):
                    st = dict(DEFAULT_SETTINGS)
                prev = SETTINGS
                apply_settings(st)
                try:
                    save_settings(st)
                except OSError as e:
                    apply_settings(prev, check_root=False)
                    raise ValueError(f"Couldn't write settings.json: {e}") from e
                return self._send(200, json.dumps({"ok": True, **settings_payload()}))
            if route == "/api/research":
                if not SETTINGS["research_enabled"]:
                    raise ValueError("Research is turned off in Settings")
                task = {k: body.get(k) for k in ("file", "line", "text", "headings", "context")}
                safe_path(task["file"])
                task["line"] = int(task["line"])
                return self._send(200, json.dumps(start_research(task, bool(body.get("force")))))
            if route == "/api/bye":
                PAGES.pop(str(body.get("pid", "")), None)
                return self._send(200, '{"ok":true}')
            if route == "/api/stop":
                self._send(200, '{"ok":true}')
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if route == "/api/open":
                open_in_editor(body["file"])
                return self._send(200, '{"ok":true}')
            self._send(404, '{"error":"not found"}')
        except Exception as e:  # noqa: BLE001 — surface to the page
            self._send(400, json.dumps({"ok": False, "error": str(e)}))


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{TITLE}}</title>
<link rel="icon" type="image/png" href="/icon.png">
<style>
:root{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1f;--mute:#6b6b70;--line:#e3e3e0;--accent:#2f6fde;--chip:#ececea;--warn:#b4441c}
@media (prefers-color-scheme:dark){:root{--bg:#18181a;--card:#222225;--fg:#e8e8ea;--mute:#9a9aa0;--line:#333338;--accent:#6c9cff;--chip:#2e2e33;--warn:#ff8a5c}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 "Segoe UI",system-ui,sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 20px}
.bar{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;max-width:1100px;margin:0 auto}
h1{font-size:17px;margin:0 8px 0 0;font-weight:600}
.count{color:var(--mute);font-variant-numeric:tabular-nums}
input[type=search]{flex:1 1 220px;min-width:160px;padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);font:inherit}
select,button{font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:6px;padding:5px 8px;cursor:pointer}
label.tog{display:flex;gap:5px;align-items:center;color:var(--mute);cursor:pointer;user-select:none}
.chips{display:flex;flex-wrap:wrap;gap:6px;max-width:1100px;margin:8px auto 0}
.chip{border:1px solid var(--line);background:var(--chip);border-radius:999px;padding:2px 10px;font-size:12.5px}
.chip[aria-pressed=false]{background:transparent;color:var(--mute);text-decoration:line-through}
main{max-width:1100px;margin:0 auto;padding:12px 20px 60px}
.file{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:12px 0;overflow:hidden}
.fhead{display:flex;gap:10px;align-items:baseline;padding:9px 14px;border-bottom:1px solid var(--line)}
.fname{font-weight:600}
.fpath{color:var(--mute);font-size:12px}
.fhead .n{margin-left:auto;color:var(--mute);font-size:12px}
.fhead,.htitle{cursor:pointer;user-select:none}
.fhead::before,.htitle::before{content:"▾";display:inline-block;width:1em;color:var(--mute)}
.collapsed>.fhead::before,.collapsed>.htitle::before{content:"▸"}
.file.collapsed>.fhead{border-bottom:0}
.collapsed>:not(.fhead):not(.htitle){display:none}
.htitle .n{margin-left:8px;opacity:.7}
.sec{padding:4px 14px 8px}
.sec+.sec{border-top:1px dashed var(--line)}
.htitle{color:var(--mute);font-size:12px;margin:6px 0 2px}
.htitle b{color:var(--fg);font-weight:600}
.task{display:flex;gap:8px;align-items:flex-start;padding:3px 0}
.task input{margin-top:3px;flex:none;width:15px;height:15px;accent-color:var(--accent);cursor:pointer}
.task .txt{flex:1;overflow-wrap:anywhere}
.task.done .txt{color:var(--mute)}
.task.dropped .txt{color:var(--mute)}
.strikeall{text-decoration:line-through}
.ctx{color:var(--mute);font-size:12px}
.acts{display:flex;gap:0;flex:none;opacity:.35;transition:opacity .1s}
.task:hover .acts,.task:focus-within .acts{opacity:1}
.acts button{display:flex;align-items:center;justify-content:center;width:22px;height:21px;border:none;border-radius:4px;background:none;color:var(--mute);padding:0}
.acts button:hover{color:var(--accent);background:var(--chip)}
.acts button:disabled{visibility:hidden}
.acts svg{width:15px;height:15px;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}
.age{color:var(--mute);font-size:11.5px;white-space:nowrap;margin-top:2px}
.acts button.has{color:var(--accent)}
.acts button.busy svg{animation:pulse 1s ease-in-out infinite}
@keyframes pulse{50%{opacity:.25}}
.research{margin:2px 0 8px;padding:10px 14px;border-left:3px solid var(--accent);background:var(--bg);border-radius:0 6px 6px 0;font-size:13.5px}
.research p{margin:0 0 6px}
.research ul{margin:0 0 8px;padding-left:20px}
.research li{margin:2px 0}
.rstatus{color:var(--mute);display:flex;align-items:center;gap:8px}
.rstatus.err{color:var(--warn)}
.spin{width:12px;height:12px;border:2px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.rfoot{display:flex;gap:10px;align-items:center;color:var(--mute);font-size:12px;margin-top:4px}
.rfoot span{margin-right:auto}
.rbtn{border:none;background:none;color:var(--accent);padding:0;font-size:12px;cursor:pointer}
code{background:var(--chip);border-radius:3px;padding:0 4px;font-size:12.5px}
a{color:var(--accent)}
.empty{color:var(--mute);text-align:center;padding:40px}
#toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--fg);color:var(--bg);padding:8px 14px;border-radius:6px;opacity:0;transition:opacity .2s;pointer-events:none}
#toast.show{opacity:.92;pointer-events:auto}
#offline{position:fixed;inset:auto 0 0 0;z-index:3;display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;justify-content:center;padding:12px 20px;background:var(--card);border-top:2px solid var(--warn);box-shadow:0 -4px 16px rgba(0,0,0,.08)}
#offline #relaunch{background:var(--accent);color:#fff;border-color:var(--accent);padding:5px 14px;font-weight:600}
#offstatus{color:var(--mute);font-size:12.5px}
body.isoff main{opacity:.45;pointer-events:none}
[hidden]{display:none!important}
#toast .undo{margin-left:12px;padding:1px 8px;background:transparent;color:inherit;border-color:currentColor}
#toast.err{background:var(--warn);color:#fff}
dialog{border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--fg);padding:0;width:min(640px,94vw);max-height:90vh}
dialog::backdrop{background:rgba(0,0,0,.4)}
#sform{display:flex;flex-direction:column;max-height:90vh}
#sform h2{margin:0;padding:14px 20px;font-size:16px;border-bottom:1px solid var(--line)}
.sbody{padding:6px 20px 12px;overflow:auto}
.sbody h3{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mute);margin:16px 0 4px}
.sf{display:grid;grid-template-columns:170px 1fr;gap:4px 12px;align-items:start;padding:6px 0}
.sf>label{padding-top:5px;font-weight:600}
.sf input[type=text],.sf input[type=number],.sf select,.sf textarea{width:100%;padding:5px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
.sf textarea{min-height:64px;resize:vertical}
.sf .hint{grid-column:2;color:var(--mute);font-size:12px;margin-top:-2px}
.sf .chk{display:flex;gap:6px;align-items:center;padding-top:5px;font-weight:400}
.sfoot{display:flex;gap:8px;align-items:center;padding:12px 20px;border-top:1px solid var(--line)}
.sfoot .msg{margin-right:auto;color:var(--warn);font-size:12.5px}
.sfoot .primary{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600}
</style></head><body>
<header>
  <div class="bar">
    <h1 id="title">{{TITLE}}</h1><span class="count" id="count"></span><button id="colall" title="Collapse all files / expand all">⇕</button>
    <input type="search" id="q" placeholder="Filter text, file or heading…  ( / )">
    <select id="age" title="Age (week heading date, else file modified)">
      <option value="0">Any age</option><option value="14">Last 2 weeks</option><option value="31">Last month</option>
      <option value="92">Last 3 months</option><option value="365" selected>Last year</option>
    </select>
    <select id="sort"><option value="recent">Newest first</option><option value="file">By file name</option></select>
    <label class="tog"><input type="checkbox" id="showDone">Done</label>
    <label class="tog"><input type="checkbox" id="showDropped">Dropped</label>
    <button id="refresh" title="Rescan (also happens when the window regains focus)">↻</button>
    <button id="gear" title="Settings" aria-label="Settings"><svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="2.2"/><path d="M8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M12.6 3.4l-1.1 1.1M4.5 11.5l-1.1 1.1"/></svg></button>
    <button id="stop" title="Stop the app (Relaunch from the banner or the taskbar pin)" aria-label="Stop app"><svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M8 2v5.5"/><path d="M4.6 4.2a5 5 0 1 0 6.8 0"/></svg></button>
  </div>
  <div class="chips" id="chips"></div>
</header>
<main id="list"></main>
<div id="toast"></div>
<dialog id="settings"><form id="sform" method="dialog">
  <h2>Settings</h2>
  <div class="sbody">
    <h3>Source</h3>
    <div class="sf"><label for="s_root">Folder to scan</label><input type="text" id="s_root" placeholder="(auto)"><div class="hint" id="rootHint"></div></div>
    <div class="sf"><label for="s_extensions">File extensions</label><input type="text" id="s_extensions"><div class="hint">Comma separated, e.g. .md, .markdown</div></div>
    <div class="sf"><label for="s_skip_dirs">Skip folders</label><textarea id="s_skip_dirs"></textarea><div class="hint">Folder names, one per line or comma separated (matched at any depth). Folders starting with “.” are always skipped.</div></div>
    <div class="sf"><label for="s_app_title">App title</label><input type="text" id="s_app_title"></div>
    <h3>Dates</h3>
    <div class="sf"><span></span><label class="chk"><input type="checkbox" id="s_use_heading_dates">Use a date in the nearest heading (YYYY-MM-DD) as the item’s date</label></div>
    <div class="sf"><span></span><label class="chk"><input type="checkbox" id="s_year_from_filename">Cap file modified date at a year in the file name (e.g. journal_2023.md)</label></div>
    <div class="sf"><label for="s_done_stamp">Completion stamp</label><select id="s_done_stamp"><option value="m/d">(11/5) month/day</option><option value="iso">(2026-11-05) ISO date</option><option value="none">None</option></select><div class="hint">Appended to the item when you tick it.</div></div>
    <h3>Behaviour</h3>
    <div class="sf"><span></span><label class="chk"><input type="checkbox" id="s_drop_strikethrough">Drop also strikes the text through (~~text~~)</label></div>
    <div class="sf"><label for="s_editor_command">Editor command</label><input type="text" id="s_editor_command" placeholder="(Typora if found, else system default)"><div class="hint">Use {file} for the path, e.g. <code>code {file}</code> or <code>obsidian</code>. The file is appended if there is no {file}.</div></div>
    <h3>Research (Claude Code)</h3>
    <div class="sf"><span></span><label class="chk"><input type="checkbox" id="s_research_enabled">Show the Research button (needs the <code>claude</code> CLI)</label></div>
    <div class="sf"><label for="s_research_model">Model</label><input type="text" id="s_research_model"></div>
    <div class="sf"><label for="s_research_timeout">Timeout (seconds)</label><input type="number" id="s_research_timeout" min="10" max="3600"></div>
    <div class="sf"><label for="s_research_tools">Extra tools</label><textarea id="s_research_tools"></textarea><div class="hint">Additional read-only tools Claude may use, e.g. MCP tool names (one per line). Read, Grep, Glob are always allowed.</div></div>
    <div class="sf"><label for="s_research_context">Instructions</label><textarea id="s_research_context" rows="4"></textarea><div class="hint">Describes your collection to Claude: what the folders are, where to look, what else to check.</div></div>
  </div>
  <div class="sfoot"><span class="msg" id="smsg"></span><button type="button" id="sreset">Reset to defaults</button><button type="button" id="scancel">Cancel</button><button type="submit" class="primary" id="ssave">Save</button></div>
</form></dialog>
<div id="offline" hidden><span id="offmsg"></span><button id="relaunch">Relaunch</button><span id="offstatus"></span></div>
<script>
const $=s=>document.querySelector(s);
const state={tasks:[],today:null,folders:{},research:{},panels:new Set(),title:'Notes To-Do',researchEnabled:true,researchTools:false,doneStamp:'m/d',dropStrike:true};
const store={get(k,d){try{const v=localStorage.getItem('jt.'+k);return v==null?d:JSON.parse(v)}catch{return d}},set(k,v){try{localStorage.setItem('jt.'+k,JSON.stringify(v))}catch{}}};
for(const id of ['age','sort']){const el=$('#'+id);el.value=store.get(id,el.value);el.onchange=()=>{store.set(id,el.value);render()}}
for(const id of ['showDone','showDropped']){const el=$('#'+id);el.checked=store.get(id,false);el.onchange=()=>{store.set(id,el.checked);render()}}
$('#q').oninput=render; $('#refresh').onclick=load;
addEventListener('focus',load);
addEventListener('keydown',e=>{if(e.key==='/'&&document.activeElement!==$('#q')){e.preventDefault();$('#q').focus()}});

function toast(msg,err,undo){const t=$('#toast');t.innerHTML='';t.append(msg);if(undo){const b=document.createElement('button');b.textContent='Undo';b.className='undo';b.onclick=undo;t.append(b)}t.className='show'+(err?' err':'');clearTimeout(t._h);t._h=setTimeout(()=>t.className='',err?4000:undo?8000:1600)}
function esc(s){return s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function md(s){
  s=esc(s);
  s=s.replace(/`([^`]+)`/g,'<code>$1</code>')
     .replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>')
     .replace(/~~(.+?)~~/g,'<s>$1</s>')
     .replace(/(^|[^*])\*([^*\s][^*]*)\*/g,'$1<i>$2</i>')
     .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>')
     .replace(/\[([^\]]+)\]\(([^)]+)\)/g,'$1');
  return s;
}
function daysOld(d){return Math.round((new Date(state.today)-new Date(d))/864e5)}
function ageLabel(t){const n=daysOld(t.date);const s=n<1?'today':n<14?n+'d':n<60?Math.round(n/7)+'w':n<730?Math.round(n/30)+'mo':Math.round(n/365)+'y';return (t.dateSource==='heading'?'':'~')+s}

const PID=Math.random().toString(36).slice(2);
addEventListener('pagehide',()=>navigator.sendBeacon('/api/bye',new Blob([JSON.stringify({pid:PID})],{type:'application/json'})));
async function load(){
  try{
    const r=await fetch('/api/tasks?pid='+PID);const j=await r.json();
    state.tasks=j.tasks;state.today=j.today;applyMeta(j);await loadResearch();
    if(Object.values(state.research).some(r=>r.status==='running'))watchResearch();
    const saved=store.get('folders',{});
    const names=[...new Set(j.tasks.map(t=>t.folder))].sort();
    state.folders=Object.fromEntries(names.map(n=>[n,saved[n]!==false]));
    store.set('relaunch',!!j.relaunch);setOffline(false);
    renderChips();render();
  }catch(e){setOffline(true)}
}
function applyMeta(j){
  state.title=j.title||state.title;state.researchEnabled=j.researchEnabled!==false;state.researchTools=!!j.researchTools;state.doneStamp=j.doneStamp||'m/d';state.dropStrike=j.dropStrike!==false;
  document.title=state.title;$('#title').textContent=state.title;
}
// --- offline banner + relaunch via the notestodo:// handler (registered with --register-protocol)
let pollTimer=null;
function setOffline(off){
  const b=$('#offline');if(b.hidden===!off)return;
  b.hidden=!off;document.body.classList.toggle('isoff',off);
  if(off){
    const can=store.get('relaunch',false);
    $('#relaunch').hidden=!can;
    $('#offmsg').textContent=can?state.title+' isn’t running.':state.title+' isn’t running. Start NotesTodo.sh, or run it once with --register-protocol to get a Relaunch button here.';
    $('#offstatus').textContent='';
    startPoll(4000);
  }else{clearInterval(pollTimer);pollTimer=null}
}
function startPoll(ms){clearInterval(pollTimer);pollTimer=setInterval(load,ms)}
$('#stop').onclick=async()=>{await post('/api/stop',{});setTimeout(()=>setOffline(true),300)};
$('#relaunch').onclick=()=>{
  location.href='notestodo://start';
  $('#offstatus').textContent='Starting… (approve the browser prompt if one appears)';
  startPoll(1000);
  clearTimeout(window._giveUp);
  window._giveUp=setTimeout(()=>{if(!$('#offline').hidden){startPoll(4000);$('#offstatus').textContent='Didn’t start. Try again, or run NotesTodo.sh.'}},30000);
};
let chipTimer=null;
function renderChips(){
  const c=$('#chips');c.innerHTML='';
  for(const [n,on] of Object.entries(state.folders)){
    const open=state.tasks.filter(t=>t.folder===n&&!t.done&&!t.dropped&&visible(t,true)).length;
    const b=document.createElement('button');b.className='chip';b.setAttribute('aria-pressed',on);
    b.textContent=`${n} ${open}`;b.title='Click to toggle; double-click to show only this folder';
    // A double-click fires two clicks first; wait briefly so it isn't also treated as two toggles.
    b.onclick=e=>{
      clearTimeout(chipTimer);
      if(e.detail>1){
        const only=Object.keys(state.folders).every(k=>state.folders[k]===(k===n));
        for(const k in state.folders)state.folders[k]=only||k===n;  // double-click again = show all
        store.set('folders',state.folders);render();return;
      }
      chipTimer=setTimeout(()=>{state.folders[n]=!state.folders[n];store.set('folders',state.folders);render()},250);
    };
    c.appendChild(b);
  }
}
function visible(t,ignoreFolder){
  if(!ignoreFolder&&!state.folders[t.folder])return false;
  if(t.dropped&&!$('#showDropped').checked)return false;
  if(t.done&&!t.dropped&&!$('#showDone').checked)return false;
  const age=+$('#age').value; if(age&&daysOld(t.date)>age)return false;
  const q=$('#q').value.trim().toLowerCase();
  if(q){const hay=(t.text+' '+t.file+' '+t.headings.join(' ')+' '+(t.context||'')).toLowerCase();if(!q.split(/\s+/).every(w=>hay.includes(w)))return false}
  return true;
}
const collapsed=new Set(store.get('collapsed',[]));
function toggleCollapsed(key,el){collapsed.has(key)?collapsed.delete(key):collapsed.add(key);el.classList.toggle('collapsed',collapsed.has(key));store.set('collapsed',[...collapsed])}
$('#colall').onclick=()=>{const keys=[...document.querySelectorAll('.file')].map(f=>f.dataset.key);const all=keys.every(k=>collapsed.has(k));keys.forEach(k=>all?collapsed.delete(k):collapsed.add(k));store.set('collapsed',[...collapsed]);render()};
function render(){
  renderChips();
  const vis=state.tasks.filter(t=>visible(t));
  const open=vis.filter(t=>!t.done&&!t.dropped).length;
  $('#count').textContent=`${open} open`+(vis.length>open?` · ${vis.length-open} other`:'');
  const byFile=new Map();
  for(const t of vis){if(!byFile.has(t.file))byFile.set(t.file,[]);byFile.get(t.file).push(t)}
  let files=[...byFile.entries()];
  if($('#sort').value==='recent')files.sort((a,b)=>Math.max(...b[1].map(t=>+new Date(t.date)))-Math.max(...a[1].map(t=>+new Date(t.date)))||a[0].localeCompare(b[0]));
  else files.sort((a,b)=>a[0].localeCompare(b[0]));
  const L=$('#list');L.innerHTML='';
  if(!files.length){L.innerHTML='<div class="empty">Nothing matches. 🎉 or loosen the filters.</div>';return}
  const recent=$('#sort').value==='recent';
  for(const [file,ts] of files){
    const box=document.createElement('section');box.className='file'+(collapsed.has(file)?' collapsed':'');box.dataset.key=file;
    const parts=file.split('/');const name=parts.pop().replace(/\.md$/i,'');
    box.innerHTML=`<div class="fhead"><span class="fname">${esc(name)}</span><span class="fpath">${esc(parts.join(' / '))}</span><span class="n">${ts.filter(t=>!t.done&&!t.dropped).length} open</span></div>`;
    box.querySelector('.fhead').onclick=()=>toggleCollapsed(file,box);
    // group consecutive tasks by heading path; newest sections first in "recent" mode
    const secs=[];for(const t of ts){const k=t.headings.join(' › ');const last=secs[secs.length-1];if(last&&last.k===k)last.ts.push(t);else secs.push({k,ts:[t],h:t.headings})}
    if(recent&&ts.some(t=>t.dateSource==='heading'))secs.sort((a,b)=>+new Date(b.ts[0].date)-+new Date(a.ts[0].date));
    for(const s of secs){
      const skey=file+'|'+s.k;const sec=document.createElement('div');sec.className='sec'+(collapsed.has(skey)?' collapsed':'');
      if(s.h.length){const h=s.h.slice(-3);sec.innerHTML=`<div class="htitle">${h.slice(0,-1).map(esc).join(' › ')}${h.length>1?' › ':''}<b>${esc(h[h.length-1])}</b><span class="n">${s.ts.filter(t=>!t.done&&!t.dropped).length} open</span></div>`;sec.firstChild.onclick=()=>toggleCollapsed(skey,sec)}
      let lastCtx=null;
      for(const t of s.ts){
        if(t.context&&t.context!==lastCtx){const c=document.createElement('div');c.className='ctx';c.style.paddingLeft=(Math.max(t.depth-1,0)*20)+'px';c.innerHTML=md(t.context);sec.appendChild(c)}
        lastCtx=t.context;
        sec.appendChild(taskRow(t));
        if(state.panels.has(rkey(t)))sec.appendChild(researchPanel(t));
      }
      box.appendChild(sec);
    }
    L.appendChild(box);
  }
}
const ICON={
  drop:'<svg viewBox="0 0 16 16"><rect x="2" y="2" width="12" height="12" rx="2"/><path d="M5 8h6"/></svg>',
  restore:'<svg viewBox="0 0 16 16"><path d="M3 5.5h7a3.5 3.5 0 0 1 0 7H6"/><path d="M5.5 3 3 5.5 5.5 8"/></svg>',
  open:'<svg viewBox="0 0 16 16"><path d="M9 2.5h4.5V7"/><path d="M13.5 2.5 7.5 8.5"/><path d="M12 9.5v3a1 1 0 0 1-1 1H3.5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h3"/></svg>',
  research:'<svg viewBox="0 0 16 16"><circle cx="7" cy="7" r="4.5"/><path d="M10.4 10.4 14 14"/></svg>'
};
function taskRow(t){
  const row=document.createElement('div');row.className='task'+(t.done?' done':'')+(t.dropped?' dropped':'');
  row.style.paddingLeft=(t.depth*20)+'px';
  const cb=document.createElement('input');cb.type='checkbox';cb.checked=t.done;cb.disabled=t.dropped;
  cb.title=t.done?'Reopen':state.doneStamp==='none'?'Mark done':'Mark done (adds today’s date)';
  cb.onchange=()=>act(t,t.done?'reopen':'complete');
  const txt=document.createElement('div');txt.className='txt';const struck=/^~~/.test(t.text),unclosed=t.dropped&&struck&&!/^~~.+?~~/.test(t.text);txt.innerHTML=md(unclosed?t.text.slice(2):t.text);if(unclosed||(t.dropped&&!struck))txt.classList.add('strikeall');
  const acts=document.createElement('div');acts.className='acts';
  const drop=document.createElement('button');drop.innerHTML=t.dropped?ICON.restore:ICON.drop;
  drop.title=t.dropped?(unclosed?'Strikethrough spans lines; restore in your editor':'Restore to open'):(t.done?'Done items can’t be dropped':'Drop (out of scope): writes - [-]'+(state.dropStrike?' ~~text~~':''));
  drop.setAttribute('aria-label',t.dropped?'Restore':'Drop');drop.disabled=t.done&&!t.dropped||unclosed;
  drop.onclick=()=>act(t,t.dropped?'undrop':'drop');
  const op=document.createElement('button');op.innerHTML=ICON.open;op.title=`Open ${t.file} in your editor (line ${t.line})`;op.setAttribute('aria-label','Open in editor');
  op.onclick=()=>post('/api/open',{file:t.file}).then(()=>toast(`Opened ${t.file.split('/').pop()} — line ${t.line}`));
  const r=state.research[rkey(t)];
  const rs=document.createElement('button');rs.innerHTML=ICON.research;rs.setAttribute('aria-label','Research background');
  if(r&&r.status==='done')rs.classList.add('has');
  if(r&&r.status==='running')rs.classList.add('busy');
  rs.title=r&&r.status==='done'?'Show researched background':r&&r.status==='running'?'Researching…':'Research background (Claude Code searches your notes'+(state.researchTools?' and connected tools':'')+')';
  rs.onclick=()=>toggleResearch(t);
  acts.append(drop,op);if(state.researchEnabled)acts.append(rs);
  const age=document.createElement('span');age.className='age';age.textContent=ageLabel(t);age.title=(t.dateSource==='heading'?'From heading date ':'From file modified date ')+t.date;
  row.append(cb,acts,txt,age);return row;
}
// --- research: claude -p in the background, result shown in a panel under the row
function rkey(t){return t.file+'|'+t.text.replace(/[ \t]*\(\d{1,2}\/\d{1,2}(?:\/\d{2,4})?\)$/,'').trim()}
async function loadResearch(){try{state.research=await (await fetch('/api/research')).json()}catch{}}
async function toggleResearch(t,force){
  const k=rkey(t),r=state.research[k];
  if(!force&&state.panels.has(k)&&(!r||r.status!=='error')){state.panels.delete(k);return render()}
  state.panels.add(k);
  if(force||!r||r.status==='error'){
    const [ok,j]=await post('/api/research',{file:t.file,line:t.line,text:t.text,headings:t.headings,context:t.context,force:!!force});
    if(!ok){toast(j.error||'Research failed to start',true);return}
    state.research[k]=j;
  }
  render();watchResearch();
}
let researchTimer=null;
function watchResearch(){
  if(researchTimer)return;
  researchTimer=setInterval(async()=>{
    const before=JSON.stringify(state.research);await loadResearch();
    if(JSON.stringify(state.research)!==before)render();
    if(!Object.values(state.research).some(r=>r.status==='running')){clearInterval(researchTimer);researchTimer=null}
  },2500);
}
function mdBlock(s){
  const out=[];let list=null;
  for(const raw of s.split(/\r?\n/)){
    const line=raw.trimEnd();
    const li=line.match(/^\s*[-*]\s+(.*)$/);
    if(li){if(!list){list=[];out.push(list)}list.push(md(li[1]));continue}
    list=null;
    if(!line.trim())continue;
    out.push('<p>'+md(line.replace(/^#+\s*/,''))+'</p>');
  }
  return out.map(x=>Array.isArray(x)?'<ul>'+x.map(i=>'<li>'+i+'</li>').join('')+'</ul>':x).join('');
}
function researchPanel(t){
  const k=rkey(t),r=state.research[k]||{status:'running'};
  const p=document.createElement('div');p.className='research';p.style.marginLeft=(t.depth*20+60)+'px';
  const close=`<button class="rbtn" data-act="close">Close</button>`;
  if(r.status==='running')p.innerHTML=`<div class="rstatus"><span class="spin"></span>Researching your notes${state.researchTools?' and connected tools':''}… usually under a minute.</div>`;
  else if(r.status==='error')p.innerHTML=`<div class="rstatus err">Research failed: ${esc(r.error||'unknown error')}</div><div class="rfoot"><button class="rbtn" data-act="rerun">Retry</button>${close}</div>`;
  else p.innerHTML=`<div class="rbody">${mdBlock(r.result)}</div><div class="rfoot"><span>Researched ${esc((r.finished||'').replace('T',' '))}${r.seconds?` · ${r.seconds}s`:''}</span><button class="rbtn" data-act="rerun">Re-run</button>${close}</div>`;
  p.onclick=e=>{const a=e.target.dataset&&e.target.dataset.act;if(a==='close'){state.panels.delete(k);render()}else if(a==='rerun')toggleResearch(t,true)};
  return p;
}
async function post(url,body){let r;try{r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})}catch(e){setOffline(true);return [false,{error:state.title+' isn’t running'}]}return [r.ok,await r.json()]}
async function act(t,action){
  const [ok,j]=await post('/api/action',{file:t.file,line:t.line,raw:t.raw,action});
  if(!ok){toast(j.error||'Write failed',true);return load()}
  const msgs={complete:'Done ✓',reopen:'Reopened',drop:'Dropped as [-] (hidden unless “Dropped” is ticked)',undrop:'Restored'};
  const inverse={complete:'reopen',reopen:'complete',drop:'undrop',undrop:'drop'};
  const nt={...t,line:j.line,raw:j.raw};
  toast(msgs[action],false,()=>{$('#toast').className='';act(nt,inverse[action])});await load();
}
// --- settings dialog (gear): edits settings.json on the server; applied live
const SF={text:['root','app_title','editor_command','research_model','research_context'],num:['research_timeout'],chk:['use_heading_dates','year_from_filename','drop_strikethrough','research_enabled'],sel:['done_stamp'],list:['extensions','skip_dirs','research_tools']};
const dlg=$('#settings');let sdata=null;
function fillForm(st){
  for(const k of SF.text.concat(SF.num,SF.sel))$('#s_'+k).value=st[k]??'';
  for(const k of SF.chk)$('#s_'+k).checked=!!st[k];
  for(const k of SF.list)$('#s_'+k).value=(st[k]||[]).join(k==='extensions'?', ':'\n');
}
function readForm(){
  const o={};
  for(const k of SF.text.concat(SF.sel))o[k]=$('#s_'+k).value;
  for(const k of SF.num)o[k]=+$('#s_'+k).value;
  for(const k of SF.chk)o[k]=$('#s_'+k).checked;
  for(const k of SF.list)o[k]=$('#s_'+k).value;
  return o;
}
$('#gear').onclick=async()=>{
  try{sdata=await (await fetch('/api/settings')).json()}catch{return toast('Couldn’t load settings',true)}
  fillForm(sdata.settings);$('#smsg').textContent='';
  $('#rootHint').textContent=sdata.rootOverridden?`Scanning ${sdata.root} — set by --root / NOTES_ROOT for this run, which overrides this field.`:`Scanning ${sdata.root}. Leave empty for the automatic default.`;
  dlg.showModal();
};
$('#scancel').onclick=()=>dlg.close();
dlg.addEventListener('click',e=>{if(e.target===dlg)dlg.close()});
$('#sreset').onclick=()=>{if(sdata)fillForm(sdata.defaults);$('#smsg').textContent='Defaults loaded — click Save to apply.'};
$('#sform').onsubmit=async ev=>{
  ev.preventDefault();
  const [ok,j]=await post('/api/settings',{settings:readForm()});
  if(!ok){$('#smsg').textContent=j.error||'Save failed';return}
  dlg.close();toast('Settings saved');await load();
};
load();
</script></body></html>
"""


# ---------------------------------------------------------------- list mode (CLI)

def list_mode(args):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    today = dt.date.today()
    folders = {f.lower() for f in (args.folder or [])}
    words = (args.grep or "").lower().split()

    def keep(t):
        status = "dropped" if t["dropped"] else "done" if t["done"] else "open"
        if args.status != "all" and status != args.status:
            return False
        if folders and t["folder"].lower() not in folders:
            return False
        if args.since and (today - dt.date.fromisoformat(t["date"])).days > args.since:
            return False
        if words:
            hay = " ".join([t["text"], t["file"], " ".join(t["headings"]), t["context"] or ""]).lower()
            if not all(w in hay for w in words):
                return False
        return True

    tasks = [dict(t, status="dropped" if t["dropped"] else "done" if t["done"] else "open")
             for t in scan_all() if keep(t)]
    newest = {}
    for t in tasks:
        newest[t["file"]] = max(newest.get(t["file"], ""), t["date"])
    tasks.sort(key=lambda t: (-dt.date.fromisoformat(newest[t["file"]]).toordinal(), t["file"],
                              -dt.date.fromisoformat(t["date"]).toordinal() if t["dateSource"] == "heading" else 0,
                              t["line"]))

    if args.format == "json":
        keys = ("file", "line", "status", "text", "headings", "context", "depth", "date", "dateSource")
        print(json.dumps({"root": str(ROOT), "today": today.isoformat(), "count": len(tasks),
                          "tasks": [{k: t[k] for k in keys} for t in tasks]}, indent=1, ensure_ascii=False))
        return

    def age(t):
        n = (today - dt.date.fromisoformat(t["date"])).days
        s = "today" if n < 1 else f"{n}d" if n < 14 else f"{round(n / 7)}w" if n < 60 else f"{round(n / 30)}mo"
        return s if t["dateSource"] == "heading" else "~" + s

    box = {"open": "[ ]", "done": "[x]", "dropped": "[-]"}
    md = args.format == "md"
    scope = [args.status] + ([f"last {args.since} days"] if args.since else []) + \
            ([", ".join(args.folder)] if args.folder else []) + ([f'matching "{args.grep}"'] if args.grep else [])
    print(f"{'# ' if md else ''}{len(tasks)} to-dos ({'; '.join(scope)}) as of {today.isoformat()}")
    cur_file = cur_head = None
    for t in tasks:
        if t["file"] != cur_file:
            cur_file, cur_head = t["file"], None
            print(f"\n{'## ' if md else ''}{t['file']}")
        head = " › ".join(t["headings"]) or "(no heading)"
        if head != cur_head:
            cur_head = head
            print(f"{'### ' if md else '  '}{head}")
        pad = "" if md else "  " * t["depth"]           # md: indenting a bare item would render as code
        ctx = f"  (under: {t['context']})" if t["context"] and t["depth"] else ""
        if md:
            print(f"{pad}- {box[t['status']]} {t['text']}  `L{t['line']}` · {age(t)}{ctx}")
        else:
            print(f"    {pad}L{t['line']:<5} {box[t['status']]} {t['text']}  · {age(t)}{ctx}")


class ExclusiveServer(ThreadingHTTPServer):
    # The default allow_reuse_address maps to SO_REUSEADDR, which on Windows lets a second
    # instance bind the same port. Hold it exclusively so a second launch hits the "busy" path.
    # On Linux/macOS SO_REUSEADDR does not allow two live listeners, but it does let a restart bind
    # while the previous run's connections sit in TIME_WAIT (otherwise "busy" is reported falsely).
    allow_reuse_address = not IS_WINDOWS

    def server_bind(self):
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


PROTOCOL = "notestodo"
PROTOCOL_KEY = rf"Software\Classes\{PROTOCOL}"


def protocol_command(port: int) -> str:
    if not IS_WINDOWS:
        return desktop_exec(login_env_wrap([sys.executable, str(Path(__file__).resolve()), "--no-browser",
                                            "--root", str(ROOT), "--port", str(port)]))
    # pythonw: no console window; stop the app with the page's Stop button
    return f'"{pythonw_path()}" "{Path(__file__).resolve()}" --no-browser --root "{ROOT}" --port {port}'


def pythonw_path() -> Path:
    # Prefer the Python install manager's launcher: a real exe (pins reliably) that survives Python upgrades
    pm = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "PyManager" / "pythonw.exe"
    if pm.exists():
        return pm
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return w if w.exists() else exe


# ---------------------------------------------------------------- Linux (freedesktop) launcher + protocol

XDG_APPS = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "applications"
DESKTOP_APP = "notes-todo.desktop"               # menu entry (--make-shortcut)
DESKTOP_HANDLER = "notes-todo-handler.desktop"   # hidden notestodo:// handler (--register-protocol)


def desktop_exec(argv: list) -> str:
    """Quote argv for a .desktop Exec= line (freedesktop spec: double quotes; escape " ` $ \\; % doubled)."""
    def q(a):
        a = str(a).replace("%", "%%")
        if not any(c in a for c in ' \t"`$\\\'><~|&;*?#()'):
            return a
        for c in '\\"`$':
            a = a.replace(c, "\\" + c)
        return f'"{a}"'
    return " ".join(q(a) for a in argv)


def login_env_wrap(argv: list) -> list:
    """Swap `python notes_todo.py ARGS` for NotesTodo-desktop.sh ARGS, which loads ~/.bashrc first
    (ANTHROPIC_API_KEY for the Research button). A script keeps the Exec= line free of quoting."""
    here = Path(__file__).resolve().parent
    return [str(here / "NotesTodo-desktop.sh")] + [str(a) for a in argv[2:]]


def icon_png() -> Path:
    """Largest PNG frame from NotesTodo.ico, written beside it (.desktop files want PNG)."""
    import struct
    here = Path(__file__).resolve().parent
    png = here / "NotesTodo.png"
    if png.exists():
        return png
    data = (here / "NotesTodo.ico").read_bytes()
    best = None
    for i in range(struct.unpack("<H", data[4:6])[0]):
        w, _, _, _, _, _, size, off = struct.unpack("<BBBBHHII", data[6 + 16 * i:22 + 16 * i])
        frame = data[off:off + size]
        if frame[:4] == b"\x89PNG" and (best is None or (w or 256) > best[0]):
            best = (w or 256, frame)
    if best:
        png.write_bytes(best[1])
    return png


def write_desktop(name: str, lines: list) -> Path:
    XDG_APPS.mkdir(parents=True, exist_ok=True)
    f = XDG_APPS / name
    f.write_text("[Desktop Entry]\nType=Application\n" + "\n".join(lines) + "\n", encoding="utf-8")
    f.chmod(0o755)
    refresh_desktop_db()
    return f


def refresh_desktop_db():
    import shutil
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(XDG_APPS)], capture_output=True)


def make_shortcut_linux(port: int) -> Path:
    here = Path(__file__).resolve().parent
    lines = [
        "Name=Notes To-Do",
        "Comment=Notes To-Do: cross-file to-do list for a markdown folder",
        "Exec=" + desktop_exec(login_env_wrap([sys.executable, str(Path(__file__).resolve()),
                                               "--root", str(ROOT), "--port", str(port)])),
        "Path=" + str(here),
        "Icon=" + str(icon_png()),
        "Terminal=false",
        "Categories=Office;Utility;",
        "StartupNotify=false",
    ]
    app = write_desktop(DESKTOP_APP, lines)
    # Also drop a copy on the Desktop when there is one (GNOME: right-click > Allow Launching)
    import shutil
    desktop = None
    if shutil.which("xdg-user-dir"):
        r = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True)
        desktop = Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
    desktop = desktop or Path.home() / "Desktop"
    if desktop.is_dir() and desktop != Path.home():
        dst = desktop / DESKTOP_APP
        shutil.copyfile(app, dst)
        dst.chmod(0o755)
    return app


def register_protocol_linux(port: int) -> str:
    import shutil
    cmd = protocol_command(port)
    write_desktop(DESKTOP_HANDLER, [
        "Name=Notes To-Do (relaunch handler)",
        "Exec=" + cmd,  # no %u: the app takes no URL argument
        "Icon=" + str(icon_png()),
        "Terminal=false",
        "NoDisplay=true",
        f"MimeType=x-scheme-handler/{PROTOCOL};",
    ])
    if shutil.which("xdg-mime"):
        subprocess.run(["xdg-mime", "default", DESKTOP_HANDLER, f"x-scheme-handler/{PROTOCOL}"], check=False)
    else:
        print("note: xdg-mime not found; install xdg-utils so browsers can find the handler")
    return cmd


def unregister_protocol_linux():
    (XDG_APPS / DESKTOP_HANDLER).unlink(missing_ok=True)
    mimeapps = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "mimeapps.list"
    if mimeapps.exists():
        lines = mimeapps.read_text(encoding="utf-8").splitlines()
        keep = [l for l in lines if not l.startswith(f"x-scheme-handler/{PROTOCOL}=")]
        if keep != lines:
            mimeapps.write_text("\n".join(keep) + "\n", encoding="utf-8")
    refresh_desktop_db()


def make_shortcut(port: int) -> Path:
    """Desktop shortcut that runs the app without a console, so a taskbar pin stays a pure launcher."""
    if not IS_WINDOWS:
        return make_shortcut_linux(port)
    import subprocess as sp
    here = Path(__file__).resolve().parent
    args = f'"{Path(__file__).resolve()}" --root "{ROOT}" --port {port}'
    ps = (
        "$d=[Environment]::GetFolderPath('Desktop');$l=Join-Path $d 'Notes To-Do.lnk';"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($l);"
        f"$s.TargetPath='{pythonw_path()}';$s.Arguments='{args.replace(chr(39), chr(39) * 2)}';"
        f"$s.WorkingDirectory='{here}';$s.IconLocation='{here / 'NotesTodo.ico'},0';"
        "$s.Description='Notes To-Do: cross-file to-do list for a markdown folder';$s.Save();$l"
    )
    out = sp.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, check=True)
    return Path(out.stdout.strip())


def register_protocol(port: int) -> str:
    if not IS_WINDOWS:
        return register_protocol_linux(port)
    import winreg
    cmd = protocol_command(port)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, PROTOCOL_KEY) as k:
        winreg.SetValueEx(k, None, 0, winreg.REG_SZ, "URL:Notes To-Do")
        winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, PROTOCOL_KEY + r"\shell\open\command") as k:
        winreg.SetValueEx(k, None, 0, winreg.REG_SZ, cmd)
    return cmd


def unregister_protocol():
    if not IS_WINDOWS:
        return unregister_protocol_linux()
    import winreg
    for sub in (r"\shell\open\command", r"\shell\open", r"\shell", ""):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, PROTOCOL_KEY + sub)
        except OSError:
            pass


def protocol_registered() -> bool:
    if not IS_WINDOWS:
        return (XDG_APPS / DESKTOP_HANDLER).exists()
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PROTOCOL_KEY + r"\shell\open\command") as k:
            return bool(winreg.QueryValue(k, None))
    except (OSError, ImportError):
        return False


def main():
    global ROOT
    ap = argparse.ArgumentParser(
        description="Notes To-Do: a local web page listing every markdown to-do across a folder.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "conventions (read and written):\n"
            "  - [ ] text            open\n"
            "  - [x] text (11/5)     done; ticking in the app adds today's date\n"
            "  - [-] ~~text~~        dropped / out of scope\n"
            "\n"
            "Serves http://127.0.0.1:PORT/ (this machine only). Skips the configured folders\n"
            "and fenced code blocks. Before writing, each line is checked against the last\n"
            "scan; changed lines are refused, not overwritten. Stop it with the page's power button."
        ),
    )
    ap.add_argument("--root", default=os.environ.get("NOTES_ROOT"),
                    help="folder to scan; overrides the Settings value for this run "
                         f"(default: NOTES_ROOT env var, else the 'root' setting, else {DEFAULT_ROOT})")
    ap.add_argument("--port", type=int, default=8765,
                    help="local port to serve on (default: %(default)s); if busy, opens the running instance")
    ap.add_argument("--no-browser", action="store_true", help="don't open the page in a browser on start")
    ap.add_argument("--register-protocol", action="store_true",
                    help="register notestodo:// for this user (Windows: HKCU; Linux: xdg-mime; no admin) so the page's "
                         "Relaunch button can restart the app; re-run after moving the files")
    ap.add_argument("--unregister-protocol", action="store_true", help="remove the notestodo:// registration")
    ap.add_argument("--make-shortcut", action="store_true",
                    help="(re)create the 'Notes To-Do' desktop shortcut (no console; pin it to the taskbar). "
                         "Linux: an app-menu entry plus a Desktop copy")
    ap.add_argument("--research-model", default=os.environ.get("NOTES_TODO_RESEARCH_MODEL"),
                    help="model for the Research button's claude -p runs, e.g. sonnet or haiku "
                         "(default: NOTES_TODO_RESEARCH_MODEL env var, else the Settings value)")
    lg = ap.add_argument_group("list mode (prints to-dos and exits; no server; for agents and scripts)")
    lg.add_argument("--list", action="store_true", help="print to-dos grouped by file and heading, newest first")
    lg.add_argument("--status", choices=["open", "done", "dropped", "all"], default="open", help="default: open")
    lg.add_argument("--since", type=int, default=0, metavar="DAYS",
                    help="only items dated within DAYS (week heading date, else file modified); 0 = any age")
    lg.add_argument("--folder", action="append", metavar="NAME",
                    help="limit to a top-level folder (e.g. Projects, Work); repeatable")
    lg.add_argument("--grep", metavar="WORDS", help="only items whose text, file, headings or parent contain all WORDS")
    lg.add_argument("--format", choices=["text", "md", "json"], default="text", help="default: text")
    args = ap.parse_args()
    global ROOT_OVERRIDE, RESEARCH_MODEL
    ROOT_OVERRIDE = args.root
    apply_settings(load_settings(), check_root=False)
    if args.research_model:
        RESEARCH_MODEL = args.research_model
    if args.list:
        if not ROOT.is_dir():
            sys.exit(f"Folder not found: {ROOT} (pass --root or set it in settings.json)")
        list_mode(args)
        return
    load_research_cache()
    if args.unregister_protocol:
        unregister_protocol()
        print(f"Removed {PROTOCOL}:// registration.")
        return
    if not ROOT.is_dir():
        sys.exit(f"Folder not found: {ROOT}")
    if args.make_shortcut:
        print(f"Shortcut: {make_shortcut(args.port)}")
        return
    if args.register_protocol:
        cmd = register_protocol(args.port)
        print(f"Registered {PROTOCOL}:// for this user. Relaunch runs:\n  {cmd}")
        return
    url = f"http://127.0.0.1:{args.port}/"
    try:
        srv = ExclusiveServer(("127.0.0.1", args.port), Handler)
    except OSError:
        print(f"Port {args.port} busy — assuming Notes To-Do is already running.")
        if page_already_open(args.port):
            print("A page is already open; not opening another.")
            notify("Already open in your browser — switch to its tab.")
        elif not args.no_browser:
            webbrowser.open(url)
        return
    print(f"Notes To-Do serving {ROOT}\n  {url}\nCtrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.4, webbrowser.open, [url]).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
