#!/usr/bin/env python3
"""minion — a deliberately tiny coding agent for self-hosted or remote models.

One file, one dep (`openai`), no TUI framework. Points at any OpenAI-compatible
endpoint (vLLM / llama.cpp / SGLang / Z.ai / OpenAI itself). Survives models
whose native tool-calling isn't wired up yet by falling back to parsing
<tool_call>...</tool_call> tags out of the text — the convention most open
models (Hermes/Qwen/Nemotron) emit.

  pip install openai
  export MINION_BASE_URL=http://localhost:8000/v1   # your served endpoint
  export MINION_MODEL=your-model-name
  export MINION_API_KEY=sk-noop                    # any string; local servers ignore it
  python minion.py

Multiple sources — define named endpoints and switch between them at runtime:

  MINION_SOURCES=local,zai
  MINION_SOURCE_LOCAL_BASE_URL=http://localhost:8080/v1
  MINION_SOURCE_ZAI_BASE_URL=https://api.z.ai/api/paas/v4
  MINION_SOURCE_ZAI_API_KEY=***                   # $name = key from env / ~/.env
  MINION_SOURCE_ZAI_MODEL=glm-x-preview

  python minion.py --source zai                     # start on Z.ai

Toggles in-session: /source [name]  /yolo  /approval [level]  /compress  /compact  /reset  /quit
Flags: --yolo  --approval <low|medium|high>  --source <name>  --no-scroll-bottom
"""
import json
import os
import random
import re
import select
import shutil
import subprocess
import sys
import termios
import threading
import time

from openai import OpenAI, APIConnectionError

# --- env file ---------------------------------------------------------------
# Load ~/.env (or MINION_ENV_FILE) into os.environ without clobbering vars
# already set in the shell. Lets source config / API keys live in one place
# instead of being exported in every terminal.
_ENV_FILE = os.path.expanduser(os.environ.get("MINION_ENV_FILE", "~/.env"))


def _load_env_file():
    try:
        with open(_ENV_FILE, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k.startswith("export "):
                    k = k[len("export "):].strip()
                if not k or k in os.environ:
                    continue
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                os.environ[k] = v
    except (OSError, IOError):
        pass


_load_env_file()


# --- model sources ----------------------------------------------------------
# minion talks to any OpenAI-compatible endpoint. A "source" bundles a
# base_url, api_key, and model name. Define sources with env vars:
#
#   MINION_SOURCES=local,zai
#   MINION_SOURCE_LOCAL_BASE_URL=http://localhost:8080/v1
#   MINION_SOURCE_ZAI_BASE_URL=https://api.z.ai/api/paas/v4
#   MINION_SOURCE_ZAI_API_KEY=$zai_test        ← $name = look up env/file key
#   MINION_SOURCE_ZAI_MODEL=glm-x-preview
#
# If no MINION_SOURCE_* vars are present, a single "local" source is built
# from the legacy MINION_BASE_URL / MINION_API_KEY / MINION_MODEL vars
# (same defaults as before, so existing setups keep working).
# Switch at runtime with /source.

class Source:
    def __init__(self, name, base_url, api_key, model=None):
        self.name = name
        self.base_url = base_url
        self.api_key = api_key or "sk-noop"
        self.model = model or None  # None → ask the server at resolve time
        self.client = OpenAI(base_url=base_url, api_key=self.api_key)

    def resolve_model(self):
        if self.model:
            return self.model
        try:
            return self.client.models.list(timeout=10).data[0].id
        except Exception:
            return "local-model"

    def display_model(self):
        return self.model or "auto"


def _resolve_api_key(val):
    """$name → look up env var (populated from ~/.env if present); else literal."""
    if val and val.startswith("$"):
        return os.environ.get(val[1:], "")
    return val


def _discover_sources():
    """Build SOURCES + SOURCE_ORDER from MINION_SOURCE_* env vars, falling
    back to a single 'local' source from the legacy MINION_* vars."""
    names = []
    raw = os.environ.get("MINION_SOURCES", "")
    if raw:
        names = [n.strip() for n in raw.split(",") if n.strip()]
    # auto-discover from MINION_SOURCE_<NAME>_BASE_URL if MINION_SOURCES absent
    if not names:
        prefix = "MINION_SOURCE_"
        found = []
        for k in os.environ:
            if k.startswith(prefix) and k.endswith("_BASE_URL"):
                found.append(k[len(prefix):-len("_BASE_URL")].lower())
        names = sorted(found)
    for name in names:
        p = f"MINION_SOURCE_{name.upper()}_"
        base_url = os.environ.get(p + "BASE_URL")
        if not base_url:
            continue
        api_key = _resolve_api_key(os.environ.get(p + "API_KEY"))
        model = os.environ.get(p + "MODEL")
        src = Source(name, base_url, api_key, model)
        SOURCES[name] = src
        SOURCE_ORDER.append(name)
    if not SOURCES:
        # legacy fallback: one source from MINION_BASE_URL etc.
        src = Source(
            "local",
            os.environ.get("MINION_BASE_URL", "http://localhost:8080/v1"),
            os.environ.get("MINION_API_KEY", "sk-noop"),
            os.environ.get("MINION_MODEL"),
        )
        SOURCES["local"] = src
        SOURCE_ORDER.append("local")


SOURCES = {}        # name → Source
SOURCE_ORDER = []   # preserve definition order for /source listing
ACTIVE = None       # current Source

# `client` and `MODEL` are bare globals read throughout the file. They always
# mirror the active source; switch_source() reassigns both. Every function that
# needs them (open_stream, _assess_risk, compress, …) does a call-time global
# lookup, so a mid-session swap is picked up instantly — same pattern /yolo
# already uses for its own globals.
client = None
MODEL = None


def switch_source(name):
    """Swap the active source. Reassigns client + MODEL globals. Returns True
    on success, False (with a message) if the name is unknown."""
    global ACTIVE, client, MODEL
    src = SOURCES.get(name)
    if not src:
        print(f"{RED}  ✗ unknown source {name!r}{RESET}")
        return False
    ACTIVE = src
    client = src.client
    MODEL = src.resolve_model()
    return True


_discover_sources()

# Pick the starting source: --source flag, then MINION_ACTIVE env, then first.
_start = None
for _i, _arg in enumerate(sys.argv):
    if _arg == "--source" and _i + 1 < len(sys.argv):
        _start = sys.argv[_i + 1]
        break
_start = _start or os.environ.get("MINION_ACTIVE") or (SOURCE_ORDER[0] if SOURCE_ORDER else None)
if not _start or _start not in SOURCES:
    _start = SOURCE_ORDER[0] if SOURCE_ORDER else None
if _start:
    switch_source(_start)


# --- approval gating --------------------------------------------------------
# Three risk levels (low < medium < high) plus an implicit "yolo" mode that
# never prompts. APPROVE_LEVEL is the maximum risk level to AUTO-APPROVE:
# actions classified at ≤ APPROVE_LEVEL run without prompting; anything
# strictly above prompts.
#   "low"    → auto-approve low (reads, grep, wc); prompt medium + high
#   "medium" → auto-approve low + medium (edits, cp, mv, tests); prompt high
#   "high"   → auto-approve everything (never prompts)
# YOLO=True short-circuits entirely (skips even the risk-classifier call).
LEVEL_ORDER = {"low": 0, "medium": 1, "high": 2}

# Accept full words and common abbreviations (med, hi, lo, m, h, l …).
_LEVEL_ALIASES = {
    "l": "low", "lo": "low", "low": "low",
    "m": "medium", "med": "medium", "mid": "medium", "medium": "medium",
    "h": "high", "hi": "high", "high": "high",
}


def _normalize_level(arg):
    """Resolve 'med' → 'medium', 'hi' → 'high', etc. Returns the canonical
    level name or None if the input isn't recognised."""
    return _LEVEL_ALIASES.get(arg.lower().strip())
YOLO = "--yolo" in sys.argv
APPROVE_LEVEL = "low"
for _i, _arg in enumerate(sys.argv):
    if _arg == "--approval" and _i + 1 < len(sys.argv):
        _lvl = _normalize_level(sys.argv[_i + 1])
        if _lvl:
            APPROVE_LEVEL = _lvl
        else:
            print(f"  ✗ unknown --approval level {sys.argv[_i + 1]!r} (want low|medium|high); using default 'low'")
if YOLO:
    APPROVE_LEVEL = None  # yolo overrides --approval; never prompt

# --- base-level traffic log -------------------------------------------------
# Append-only JSONL record of every byte we ship to / receive from the server.
# Lives next to this script so it's easy to find; rotate by hand if it gets big.
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llamacpp.log")
_llog = open(LOG_PATH, "a", buffering=1)  # line-buffered; flushes per write


def _log_event(direction, payload):
    """direction: 'req' (outgoing) or 'resp' (incoming SSE chunk)."""
    _llog.write(json.dumps({"ts": time.time(), "dir": direction, "data": payload}) + "\n")

# --- ANSI -------------------------------------------------------------------
DIM, CYAN, GREEN, YELLOW, RED, MAGENTA, BOLD, RESET = (
    "\033[2m", "\033[36m", "\033[32m", "\033[33m", "\033[31m", "\033[35m",
    "\033[1m", "\033[0m",
)
CLEAR_LINE = "\033[2K\r"   # erase entire line, return cursor to col 0
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"


# --- waiting animation (tiny Conway's Game of Life) -------------------------
# A spinner is boring. A 1-row toroidal Game of Life is the same shape on screen
# (one line of cells) but actually does something — patterns glide, blinkers
# flash, gliders crawl. Runs in a background thread; the main loop kills it
# the instant the first token arrives.
_GOL_W = 24
_GOL_ALIVE = "█"
_GOL_DEAD = "·"
_GOL_GLIDER = {(0, 0), (1, 1), (2, 1), (0, 2), (1, 0)}  # 5-cell, period-4


class LifeSpinner:
    def __init__(self, width=_GOL_W, tick_ms=90, label="thinking"):
        self.w = width
        self.tick = tick_ms / 1000
        self.label = label  # shown before the cells ("thinking" / "running" / ...)
        self._stop = threading.Event()
        self._t = None

    def _seed(self):
        row = [0] * self.w
        x = random.randrange(self.w)
        for dx, _ in _GOL_GLIDER:
            row[(x + dx) % self.w] = 1
        for _ in range(2):
            x = random.randrange(self.w)
            row[x] = row[(x + 1) % self.w] = row[(x + 2) % self.w] = 1
        for _ in range(self.w // 6):
            row[random.randrange(self.w)] = 1
        return row

    def _step(self, row):
        # A 1-row GoL is degenerate (cells have only 2 neighbors). Cheat: treat
        # the row as the middle of a 3-row toroidal world where the rows above
        # and below mirror the current one. Gives every cell the standard 8
        # neighbors, so gliders/blinkers/etc. actually work.
        w, above, below, nxt = self.w, row, row, [0] * self.w
        for x in range(w):
            n = (above[(x - 1) % w] + above[x] + above[(x + 1) % w] +
                 row[(x - 1) % w]                   + row[(x + 1) % w] +
                 below[(x - 1) % w] + below[x] + below[(x + 1) % w])
            cur = row[x]
            nxt[x] = 1 if (cur and n in (2, 3)) or (not cur and n == 3) else 0
        return nxt

    def _run(self):
        sys.stdout.write(HIDE_CURSOR)
        try:
            row = self._seed()
            # initial render — also reserve the line so subsequent prints don't shift things
            sys.stdout.write(CLEAR_LINE + "  " + DIM + f"{self.label} " + RESET +
                             "".join(_GOL_ALIVE if c else _GOL_DEAD for c in row))
            sys.stdout.flush()
            while not self._stop.is_set():
                time.sleep(self.tick)
                if self._stop.is_set():
                    break
                row = self._step(row)
                sys.stdout.write(CLEAR_LINE + "  " + DIM + f"{self.label} " + RESET +
                                 "".join(_GOL_ALIVE if c else _GOL_DEAD for c in row))
                sys.stdout.flush()
        finally:
            # wipe the spinner line and restore cursor
            sys.stdout.write(CLEAR_LINE + SHOW_CURSOR)
            sys.stdout.flush()

    def start(self):
        self._stop.clear()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def stop(self):
        self._stop.set()
        if self._t:
            self._t.join(timeout=0.5)
            self._t = None


# --- interrupt watcher ------------------------------------------------------
# Lets the user press Esc during model generation to stop the stream and drop
# back to the prompt. Runs in a daemon thread for the lifetime of model_turn;
# the main loop checks _INTERRUPT_EVENT between chunks and closes the stream
# on interrupt. Tools are NOT cancelled — run_bash etc. run to completion.
# (Hard-cancelling a tool mid-flight is a separate follow-up.)
#
# Two events, two purposes:
#   _INTERRUPT_EVENT    — "watcher should exit / main loop should check"
#                          set by main on cleanup, set by watcher on user Esc
#   _USER_INTERRUPTED   — "the user actually pressed Esc" (not just cleanup)
#                          only the watcher sets this; main reads it after join
_INTERRUPT_EVENT = threading.Event()
_USER_INTERRUPTED = threading.Event()


def _interrupt_watcher():
    """Daemon: watch stdin for bare Esc during model generation.

    Puts stdin into raw mode (ISIG off so Ctrl+C doesn't kill the process)
    so we can read without echo. A bare Esc (not the start of an arrow-key /
    bracketed-paste / etc. CSI sequence) sets _USER_INTERRUPTED and
    _INTERRUPT_EVENT, then returns. Exits when _INTERRUPT_EVENT is set by
    main's cleanup. Restores termios on exit.
    """
    fd = sys.stdin.fileno()
    if not os.isatty(fd):
        return
    try:
        old = termios.tcgetattr(fd)
    except Exception:
        return
    new = old[:]
    new[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
    new[0] &= ~termios.ICRNL
    new[6][termios.VMIN] = 0
    new[6][termios.VTIME] = 0
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, new)
    except Exception:
        return
    try:
        last_fire = 0.0
        while not _INTERRUPT_EVENT.is_set():
            r, _, _ = select.select([fd], [], [], 0.1)
            if not r:
                continue
            try:
                c = os.read(fd, 1)
            except OSError:
                return
            if c != b"\x1b":
                continue  # discard anything that isn't Esc
            # Could be bare Esc OR the lead byte of an escape sequence
            # (arrow keys, Home/End, bracketed paste, etc.). Wait up to 50ms
            # for more bytes; if none arrive, it's a bare Esc.
            r2, _, _ = select.select([fd], [], [], 0.05)
            if r2:
                try:
                    os.read(fd, 1)  # swallow the rest of the sequence
                except OSError:
                    pass
                continue
            now = time.time()
            if now - last_fire < 0.25:  # debounce — don't fire twice in a row
                continue
            last_fire = now
            _USER_INTERRUPTED.set()
            _INTERRUPT_EVENT.set()
            return
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass


# --- tools ------------------------------------------------------------------
def read_file(path, **_):
    with open(path) as f:
        return f.read()


def write_file(path, content, **_):
    if not _confirm(f"write {path} ({len(content)} bytes)"):
        return "DENIED by user"
    with open(path, "w") as f:
        f.write(content)
    return f"wrote {len(content)} bytes to {path}"


def edit_file(path, old, new, **_):
    with open(path) as f:
        src = f.read()
    if src.count(old) != 1:
        return f"ERROR: `old` matched {src.count(old)} times (need exactly 1)"
    if not _confirm(f"edit {path}"):
        return "DENIED by user"
    with open(path, "w") as f:
        f.write(src.replace(old, new))
    return f"edited {path}"


def list_dir(path=".", **_):
    return "\n".join(sorted(os.listdir(path)))


def run_bash(command, **_):
    if not _confirm(f"run: {command}"):
        return "DENIED by user"
    r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=120)
    out = (r.stdout or "") + (r.stderr or "")
    return f"[exit {r.returncode}]\n{out[:8000]}"


DISPATCH = {
    "read_file": read_file, "write_file": write_file, "edit_file": edit_file,
    "list_dir": list_dir, "run_bash": run_bash,
}

TOOLS = [
    {"type": "function", "function": {"name": "read_file", "description": "Read a file's contents",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Write (overwrite) a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "edit_file", "description": "Replace one exact occurrence of `old` with `new` in a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}, "required": ["path", "old", "new"]}}},
    {"type": "function", "function": {"name": "list_dir", "description": "List a directory",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "run_bash", "description": "Run a shell command",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
]

SYSTEM = """You are a terminal coding agent working in the user's current directory.
Use the provided tools to inspect and modify code. Take one concrete step at a time.

If your runtime does NOT support native tool calls, emit a call as text exactly like:
<tool_call>{"name": "read_file", "arguments": {"path": "foo.py"}}</tool_call>
Emit nothing after a tool call; wait for the Observation. When the task is done, reply in plain prose."""


def _env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _abbr(n):
    """Compact token count for the stats footer: 832 → '832', 1500 → '1.5K',
    78825 → '78K', 1234567 → '1.2M'. Keeps the footer tidy once context grows."""
    n = int(n)
    if n < 1000:
        return str(n)
    if n < 10_000:
        return f"{n / 1000:.1f}K"
    if n < 1_000_000:
        return f"{n // 1000}K"
    if n < 10_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n // 1_000_000}M"

REASONING_LOOP_SIGNALS = (
    "start coding",
    "let me implement",
    "let's implement",
    "now implement",
    "i'll implement",
    "i will implement",
    "write the code",
    "let me write",
    "start with the code",
)
REASONING_LOOP_SIGNAL_LIMIT = _env_int("MINION_REASONING_LOOP_SIGNALS", 10)
REASONING_LOOP_NUDGES = (
    "You are looping in reasoning after repeatedly deciding to start implementation. "
    "Stop planning now. Take the next concrete action: either call the appropriate tool "
    "or give the final answer. Do not continue private reasoning.",
    "The previous runtime nudge did not work. Your next assistant turn must contain "
    "exactly one concrete action: either a tool call or the final answer. Do not "
    "explain, plan, or continue reasoning. If you need file context, call read_file "
    "or list_dir now.",
    "Hard stop. Emit only a tool call now. If native tool calls are unavailable, "
    "emit exactly one <tool_call>{...}</tool_call> block and nothing else. For code "
    "edits, read the target file first unless you already know the exact replacement.",
)
REASONING_LOOP_NUDGE = REASONING_LOOP_NUDGES[0]
REASONING_LOOP_RETRY_LIMIT = _env_int(
    "MINION_REASONING_LOOP_RETRIES", len(REASONING_LOOP_NUDGES))
RUNTIME_NOTE_RE = re.compile(r"\n\n\[Runtime note: .*?\]\s*$", re.DOTALL)


def _nudge_current_user_turn(messages, nudge):
    note = f"[Runtime note: {nudge}]"
    for msg in reversed(messages):
        if msg.get("role") != "user" or not isinstance(msg.get("content"), str):
            continue
        content = RUNTIME_NOTE_RE.sub("", msg["content"]).rstrip()
        msg["content"] = f"{content}\n\n{note}" if content else note
        return
    messages.append({"role": "user", "content": note})


# --- risk classifier --------------------------------------------------------
# One cheap non-streaming call per write/bash action. Same model, tiny prompt,
# expects {"level": "low|medium|high", "reason": "<short>"}. Defensive parse —
# if the model rambles or returns garbage we fall back to "high" so we err on
# the side of asking. Skipped entirely in YOLO mode (no point paying for a
# call we won't act on) and for read-only tools (already implicitly safe).

RISK_SYSTEM = (
    "You are a risk classifier for a coding agent's tool calls. "
    "Given one tool action, respond with ONLY a JSON object of the form "
    '{"level": "low"|"medium"|"high", "reason": "<one short sentence>"}.\n'
    "Levels:\n"
    '- low: read-only or trivially reversible (ls, cat, grep, git status, mkdir, touch, file reads).\n'
    '- medium: modifies state but contained/reversible (writing a single file, editing a file, cp, mv, '
    'pip install in a venv, running tests, git commit).\n'
    '- high: destructive, hard to reverse, or broad scope (rm -rf, git push --force, git reset --hard, '
    'dd, chmod -R, writing outside the project, network sends to external hosts, killing processes, '
    'system-level changes, anything touching dotfiles in $HOME).\n'
    "When in doubt, classify higher. Output ONLY the JSON, no preamble."
)


def _assess_risk(action):
    """Return (level, reason). level is one of LEVEL_ORDER; reason is a short
    string. On any failure (server down, bad JSON, unknown level) returns
    ("high", "<error>") so the caller falls through to the prompt path."""
    try:
        payload = [
            {"role": "system", "content": RISK_SYSTEM},
            {"role": "user", "content": action},
        ]
        _log_event("req", {"model": MODEL, "messages": payload, "stream": False, "_purpose": "risk"})
        resp = client.chat.completions.create(
            model=MODEL, messages=payload, stream=False, timeout=15)
        try:
            _log_event("resp", {"_purpose": "risk", "data": resp.model_dump()})
        except Exception:
            pass
        text = (resp.choices[0].message.content or "").strip()
        # Try JSON first; fall back to scanning for a level word.
        level, reason = None, ""
        try:
            obj = json.loads(text)
            level = (obj.get("level") or "").strip().lower()
            reason = (obj.get("reason") or "").strip()
        except (json.JSONDecodeError, AttributeError, TypeError):
            m = re.search(r'\b(low|medium|high)\b', text, re.IGNORECASE)
            if m:
                level = m.group(1).lower()
            reason = text[:120]
        if level not in LEVEL_ORDER:
            return ("high", f"unparseable risk response: {text[:80]!r}")
        return (level, reason or level)
    except APIConnectionError:
        return ("high", "server unreachable; defaulting to high")
    except Exception as e:
        return ("high", f"risk call failed: {type(e).__name__}")


# A special exception that propagates up from _confirm (via the tool function
# body → run_tool → model_turn) when the user presses Esc at an approval prompt.
# It's not a real error — it's a control-flow signal meaning "stop this turn and
# drop the user back to the chat input so they can add more guidance."
class _EscToChat(Exception):
    pass


_ACTIVE_SPINNER = None  # set by run_tool() while a tool body is executing


def _ask_approval(prompt):
    """Read a single keypress for a Y/n/esc approval prompt. Returns one of
    'y', 'n', 'esc'. Esc is accepted both as the bare Esc key (read in raw
    mode) and as the letter 'e'/'E' for terminals where raw mode isn't
    usable. Falls back to line-based `input()` when stdin isn't a TTY."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        ans = input(prompt).strip().lower()
        if ans in ("esc", "e", "\x1b"):
            return "esc"
        if ans == "n":
            return "n"
        return "y"
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    new = old[:]
    new[3] &= ~(termios.ECHO | termios.ICANON)
    new[6][termios.VMIN] = 1
    new[6][termios.VTIME] = 0
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, new)
        sys.stdout.write(prompt)
        sys.stdout.flush()
        c = os.read(fd, 1).decode("utf-8", "replace")
        if c == "\x1b":
            # Could be bare Esc OR the lead byte of an escape sequence
            # (arrow keys, Home/End, bracketed paste, …). Wait briefly; if
            # no more bytes arrive, it's a bare Esc.
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                try:
                    os.read(fd, 1)  # swallow the rest of the sequence
                except OSError:
                    pass
                # Any non-bare-Esc sequence (arrow keys etc.) → default proceed.
                sys.stdout.write("\n")
                sys.stdout.flush()
                return "y"
            sys.stdout.write(f"{DIM}esc{RESET}\n")
            sys.stdout.flush()
            return "esc"
        # Echo the choice for feedback
        if c in ("\r", "\n"):
            shown, result = "Y (default)", "y"
        elif c in ("y", "Y"):
            shown, result = "Y", "y"
        elif c in ("n", "N"):
            shown, result = "n", "n"
        elif c in ("e", "E"):
            shown, result = "esc", "esc"
        else:
            shown, result = f"{c} → Y (default)", "y"
        sys.stdout.write(f"{DIM}{shown}{RESET}\n")
        sys.stdout.flush()
        return result
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass


def _confirm(action):
    """Decide whether to run `action`. Returns True to proceed, False to deny.
    Raises `_EscToChat` if the user presses Esc — propagating up so the turn
    stops and the REPL drops back to the chat input.

    Flow:
      1. YOLO → True (no call, no prompt).
      2. Ask the model for a risk level (skipped in step 1).
      3. If level ≤ APPROVE_LEVEL → auto-allow (printed as a one-liner).
      4. Otherwise prompt (Y/n/esc), showing the level + reason so the user
         has context.

    Reads module globals YOLO and APPROVE_LEVEL at call time — /yolo and
    /approval reassign them mid-session, and we must always see the latest
    value, not a snapshot from when the tool function was defined.
    """
    if YOLO:
        return True
    # If a tool spinner is running, pause it around our own I/O so the
    # auto-allow line / Y/n prompt aren't immediately overwritten by the
    # next animation tick. (The spinner redraws ~11×/s — without this the
    # prompt would flicker badly or get erased.)
    sp = _ACTIVE_SPINNER
    if sp is not None:
        sp.stop()
    try:
        level, reason = _assess_risk(action)
        # APPROVE_LEVEL is the max level to AUTO-APPROVE. level ≤ threshold → run.
        if APPROVE_LEVEL is not None and LEVEL_ORDER[level] <= LEVEL_ORDER[APPROVE_LEVEL]:
            # Auto-allow. Show the assessment so the user has a paper trail.
            short = reason if len(reason) <= 80 else reason[:77] + "..."
            print(f"{DIM}  ↳ auto-allow [{level}] {action}  ({short}){RESET}")
            return True
        short = reason if len(reason) <= 80 else reason[:77] + "..."
        lvl_color = {"low": DIM, "medium": YELLOW, "high": RED}[level]
        choice = _ask_approval(
            f"{YELLOW}  allow {action}? {lvl_color}[risk: {level.upper()} — {short}]{RESET} "
            f"{YELLOW}[Y/n/esc] {RESET}")
        if choice == "esc":
            raise _EscToChat(action)
        return choice != "n"
    finally:
        if sp is not None:
            sp.start()


# --- text-fallback parsing --------------------------------------------------
TOOL_TAG = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def parse_text_calls(content):
    """Pull <tool_call>{...}</tool_call> blocks out of model text."""
    calls = []
    for m in TOOL_TAG.finditer(content or ""):
        try:
            obj = json.loads(m.group(1))
            calls.append((obj["name"], obj.get("arguments", {})))
        except (json.JSONDecodeError, KeyError):
            pass
    return calls


def run_tool(name, args):
    fn = DISPATCH.get(name)
    if not fn:
        return f"ERROR: unknown tool {name}"
    # newline so the tool arrow gets its own line — streamed text uses end=""
    # and would otherwise run straight into the indicator
    arg_preview = json.dumps(args)
    if len(arg_preview) > 120:
        arg_preview = arg_preview[:117] + "..."
    print(f"\n{CYAN}  ┌─ {name}{RESET}")
    print(f"{CYAN}  │ {RESET}{DIM}{arg_preview}{RESET}")
    # Animate the gap between "cyan args line" and "cyan result line". Tool
    # bodies can take a while — _confirm makes a network round-trip to the
    # risk classifier, run_bash can run for tens of seconds, write_file on a
    # big payload takes a beat — and without this the user just sees a frozen
    # screen after the green model output finishes. _confirm pauses/resumes
    # us around its own I/O so the auto-allow line / Y/n prompt aren't clobbered.
    spinner = LifeSpinner(label="running")
    spinner.start()
    global _ACTIVE_SPINNER
    _ACTIVE_SPINNER = spinner
    try:
        result = fn(**args)
    except _EscToChat:
        # Close the tool box before propagating so it isn't left visually open.
        print(f"{CYAN}  └─{RESET} {DIM}(escaped){RESET}")
        raise  # not an error — control-flow signal back to model_turn/REPL
    except Exception as e:  # noqa: BLE001 — surface any tool error back to the model
        result = f"ERROR: {type(e).__name__}: {e}"
    finally:
        _ACTIVE_SPINNER = None
        spinner.stop()
    # box the result; truncate absurdly long output for readability (model still
    # gets the full thing via the messages array)
    preview = result if len(result) < 800 else result[:800] + f"\n... [{len(result) - 800} more chars]"
    for line in preview.splitlines():
        print(f"{CYAN}  │ {RESET}{line}")
    print(f"{CYAN}  └─{RESET}")
    return result


def open_stream(messages):
    """Open a streaming completion. Retries without tools= if the server rejects
    that param; returns None (after a friendly message) on connection/API failure."""
    try:
        try:
            _log_event("req", {"model": MODEL, "messages": messages, "tools": TOOLS, "stream": True})
            stream = client.chat.completions.create(
                model=MODEL, messages=messages, tools=TOOLS, stream=True,
                stream_options={"include_usage": True})
        except APIConnectionError:
            raise  # server unreachable — don't bother retrying without tools
        except Exception:  # reachable but rejected tools= → text-protocol fallback
            _log_event("req", {"model": MODEL, "messages": messages, "stream": True, "_fallback": "no-tools"})
            stream = client.chat.completions.create(
                model=MODEL, messages=messages, stream=True,
                stream_options={"include_usage": True})
        # Wrap the stream so every chunk is captured to the log on its way out.
        return _LoggingStream(stream, _llog)
    except APIConnectionError:
        print(f"{RED}  ✗ can't reach {client.base_url} — is the server up? "
              f"Set MINION_BASE_URL (and MINION_MODEL) to point at it.{RESET}")
    except Exception as e:
        print(f"{RED}  ✗ API error: {type(e).__name__}: {e}{RESET}")
    return None


# --- context compression ----------------------------------------------------
# Summarize the older turns of `messages` into a single user-role turn, keeping
# the system prompt and the last K turns verbatim. Frees context without losing
# the model's grip on what it was just doing.
COMPRESS_KEEP = 2  # how many recent turns to leave untouched


def compress(messages, keep=COMPRESS_KEEP):
    """Ask the model to summarize everything except system + last `keep` turns.

    Mutates `messages` in place on success: replaces the middle slice with a
    single user-role summary turn. Returns (kept_n, summarized_n, summary_chars)
    or None on failure (in which case `messages` is untouched).

    Non-streaming on purpose — we want the whole summary before splicing it in,
    and a spinner for a one-shot summary would be visual noise.
    """
    # Layout: [system?, ..., user, assistant, tool, ..., user, assistant(tool_calls)?, ...]
    # We assume messages[0] is the system prompt (matches how main() builds it).
    # Anything before the "tail" we want to summarize; the tail stays verbatim.
    if len(messages) <= 1 + keep:
        return None  # nothing to compress

    sys_msg = messages[0] if messages and messages[0].get("role") == "system" else None
    body = messages[1:] if sys_msg else messages
    if len(body) <= keep:
        return None

    head, tail = body[:-keep], body[-keep:]
    summarized_n = len(head)

    # The tail must start on a turn the chat template can render. A `tool` turn
    # with no preceding assistant(tool_calls) parent — or an assistant(tool_calls)
    # turn whose result got cut off into `head` — makes llama.cpp's Jinja template
    # raise "Message has tool role, but there was no previous assistant message
    # with a tool call!". Walk from the front of the tail and drop any leading
    # tool/half-tool-call turns until we land on something safe (user, plain
    # assistant, or system). Bump `summarized_n` so the user-visible count stays
    # honest about how many turns actually got folded into the summary.
    while tail and tail[0].get("role") in ("tool", "assistant"):
        first = tail[0]
        if first.get("role") == "tool":
            tail = tail[1:]
            summarized_n += 1
            continue
        # assistant: only safe if it has NO tool_calls, OR every tool_call has
        # its matching tool result later in the tail
        if first.get("tool_calls"):
            ids = {tc["id"] for tc in first["tool_calls"]}
            seen = set()
            for m in tail[1:]:
                tcid = m.get("tool_call_id")
                if m.get("role") == "tool" and tcid:
                    seen.add(tcid)
            if ids - seen:
                tail = tail[1:]
                summarized_n += 1
                continue
        break

    # Render the head as plain text the model can summarize. Tool outputs are
    # the bulkiest part of a real session — include them but truncate each one
    # so a single huge read_file doesn't blow up the summary prompt itself.
    def _render(msgs):
        out = []
        for m in msgs:
            role = m.get("role", "?")
            content = m.get("content")
            if content is None and m.get("tool_calls"):
                # assistant tool-call turn — show the calls so the summary knows what ran
                calls = ", ".join(
                    f"{c['function']['name']}({c['function']['arguments']})"
                    for c in m["tool_calls"]
                )
                out.append(f"[{role}] → {calls}")
            elif isinstance(content, list):
                # some servers return content as a list of parts; flatten it
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
                out.append(f"[{role}] {content[:2000]}")
            else:
                out.append(f"[{role}] {(content or '')[:2000]}")
        return "\n\n".join(out)

    summary_prompt = (
        "Summarize the following conversation history for context retention. "
        "Preserve: the original user goal/task, key decisions made, file paths "
        "and identifiers touched, current state of any in-progress work, and "
        "any unresolved questions. Drop: raw tool outputs, full file contents, "
        "and verbose back-and-forth — keep it dense and information-rich. "
        "Write in the same language as the conversation. Output ONLY the "
        "summary, no preamble.\n\n"
        f"---\n{_render(head)}\n---"
    )

    payload = [{"role": "user", "content": summary_prompt}]
    try:
        _log_event("req", {"model": MODEL, "messages": payload, "stream": False, "_purpose": "compress"})
        resp = client.chat.completions.create(model=MODEL, messages=payload, stream=False)
        try:
            _log_event("resp", {"_purpose": "compress", "data": resp.model_dump()})
        except Exception:
            pass  # never let logging break the summary call
    except APIConnectionError:
        print(f"{RED}  ✗ can't reach {client.base_url} — context unchanged{RESET}")
        return None
    except Exception as e:
        print(f"{RED}  ✗ compress failed: {type(e).__name__}: {e}{RESET}")
        return None

    summary = (resp.choices[0].message.content or "").strip()
    if not summary:
        print(f"{RED}  ✗ compress returned empty summary — context unchanged{RESET}")
        return None

    header = f"[Compressed context — {summarized_n} earlier turns summarized; last {keep} turns kept verbatim]"
    new_mid = [{"role": "user", "content": f"{header}\n\n{summary}"}]
    messages[:] = ([sys_msg] if sys_msg else []) + new_mid + tail
    return len(tail), summarized_n, len(summary)


class _LoggingStream:
    """Iterator wrapper that tees each SSE chunk to llamacpp.log before yielding.
    Uses model_dump so we capture the chunk's full structure (incl. reasoning_content)."""
    def __init__(self, inner, log_file):
        self._inner = inner
        self._log = log_file

    def __iter__(self):
        for chunk in self._inner:
            try:
                self._log.write(json.dumps({"ts": time.time(), "dir": "resp",
                                             "data": chunk.model_dump()}) + "\n")
            except Exception:
                pass  # never let logging break the stream
            yield chunk

    def close(self):
        close = getattr(self._inner, "close", None)
        if close:
            try:
                close()
            except Exception:
                pass


class _ReasoningLoopSignalCounter:
    """Counts repeated "ready to act" phrases across streamed reasoning chunks."""
    def __init__(self, phrases):
        self.phrases = tuple(p.lower() for p in phrases)
        self.tail = ""
        self.hits = 0
        self.max_phrase_len = max((len(p) for p in self.phrases), default=1)

    def feed(self, chunk):
        if not self.phrases:
            return self.hits
        old_len = len(self.tail)
        text = self.tail + chunk.lower()
        scan_start = max(0, old_len - self.max_phrase_len + 1)
        for phrase in self.phrases:
            start = text.find(phrase, scan_start)
            while start != -1:
                if start + len(phrase) > old_len:
                    self.hits += 1
                start = text.find(phrase, start + 1)
        self.tail = text[-(self.max_phrase_len - 1):]
        return self.hits


TURN_DONE = "done"
TURN_TOOL = "tool"
TURN_LOOP_CUT = "loop_cut"
TURN_ESC = "esc"  # user pressed Esc at an approval prompt → drop to chat input


# --- one model turn (streamed), returns TURN_* status -----------------------
def model_turn(messages, reasoning_loop_cut_count=0):
    # Start the spinner BEFORE open_stream() so the HTTP-handshake + interrupt-
    # watcher-setup window (which can be tens of ms on a warm local server but
    # seconds on a cold/wake-from-sleep one) isn't a frozen green-text gap.
    # The spinner is still killed by the first SSE chunk in the loop below.
    # t0 must be set BEFORE open_stream() — the HTTP request is sent inside
    # open_stream() (TCP/TLS handshake, request bytes, etc.), and all of that
    # latency is part of TTFT. If we set t0 after, the server has already been
    # processing and the first token may be sitting in the buffer by the time
    # we start the clock, making TTFT look implausibly tiny.
    t0 = time.time()
    spinner = LifeSpinner(label="thinking · esc to interrupt")
    spinner.start()
    try:
        stream = open_stream(messages)
    except Exception:
        spinner.stop()
        raise
    if stream is None:
        spinner.stop()
        return TURN_DONE  # error already reported; REPL continues

    # Interrupt watcher: a daemon thread watches stdin for bare Esc. On hit,
    # it sets _USER_INTERRUPTED and closes the stream; the main loop breaks
    # out of the chunk loop on its next iteration. We start it BEFORE the
    # spinner so the user always has a moment to interrupt even if the first
    # token takes a while to arrive.
    _INTERRUPT_EVENT.clear()
    _USER_INTERRUPTED.clear()
    watcher = threading.Thread(target=_interrupt_watcher, daemon=True)
    watcher.start()
    content, tcs, mode = [], {}, None
    timings = None
    usage = None
    t_first = None   # time of first output token (for TTFT)
    loop_signals = _ReasoningLoopSignalCounter(REASONING_LOOP_SIGNALS)
    loop_cut = False
    interrupted = False
    try:
        for chunk in stream:
            if _USER_INTERRUPTED.is_set():
                interrupted = True
                # close() makes the next iteration raise StopIteration / a
                # connection error; we're breaking anyway, but be tidy
                close = getattr(stream, "close", None)
                if close:
                    try:
                        close()
                    except Exception:
                        pass
                break
            # first byte in: kill the spinner, let the real output take this line
            if spinner._t is not None:
                spinner.stop()
            # Capture streaming usage (OpenAI/Z.ai send a final chunk with
            # usage populated when stream_options.include_usage is True).
            # This chunk has an empty choices array, so check before the
            # choices guard below.
            if chunk.usage:
                usage = chunk.usage
            # The final usage-only chunk has an empty choices array — nothing
            # else to do with it.
            if not chunk.choices:
                continue
            d = chunk.choices[0].delta
            # Capture TTFT on the first chunk carrying real output (reasoning,
            # content, or tool calls).
            if t_first is None:
                rc_peek = getattr(d, "reasoning_content", None) or \
                          (getattr(d, "model_extra", None) or {}).get("reasoning_content")
                if d.content or d.tool_calls or rc_peek:
                    t_first = time.time() - t0
            # llama.cpp attaches a `timings` object to the final chunk — grab it
            # for the stats footer. It's the only place we get real tok/s numbers
            # (streaming `usage` is always null on llama.cpp).
            extra = getattr(chunk, "model_extra", None) or {}
            if "timings" in extra:
                timings = extra["timings"]
            # reasoning models (e.g. MiniMax-M3) stream a separate reasoning_content
            # field before content/tool_calls. Header + dim text, then a blank line
            # so the green "actual response" always lands on its own row (reasoning
            # from the model often doesn't end in \n — without the gap it would
            # run straight into the answer).
            rc = getattr(d, "reasoning_content", None) or (d.model_extra or {}).get("reasoning_content")
            if rc:
                if mode != "think":
                    print(f"{DIM}  ── reasoning ──{RESET}")
                    mode = "think"
                print(f"{DIM}{rc}{RESET}", end="", flush=True)
                if REASONING_LOOP_SIGNAL_LIMIT > 0 and not content and not tcs:
                    prev_hits = loop_signals.hits
                    hits = loop_signals.feed(rc)
                    # Print a loud, obvious counter on threshold crossings so the
                    # user can see the model spiraling before it gets cut. We
                    # fire at the first hit, then at 25/50/75/100% of the limit
                    # (clamped so milestones never exceed the limit). Keeps the
                    # noise down to ≤5 lines per turn while still being impossible
                    # to miss in the dim reasoning stream.
                    _limit = REASONING_LOOP_SIGNAL_LIMIT
                    _milestones = sorted(set(min(_limit, v) for v in (
                        1,                                       # first hit
                        (_limit + 3) // 4,                       # 25%
                        (_limit + 1) // 2,                       # 50%
                        (3 * _limit + 3) // 4,                   # 75%
                        _limit,                                  # 100% — the cut itself
                    ) if 0 < v <= _limit))
                    crossed_milestones = [m for m in _milestones if prev_hits < m <= hits]
                    for milestone in crossed_milestones:
                        # Break out of the dim inline stream so the warning
                        # lands on its own line, then reopen reasoning mode
                        # below so subsequent chunks (if any) keep streaming
                        # cleanly. The end-of-loop divider in the finally-ish
                        # block below will close it out properly when we break.
                        print()
                        if milestone >= _limit:
                            print(f"{RED}  ⚠ REASONING LOOP LIMIT HIT — {hits}/{_limit} ready-to-act signals "
                                  f"(“{loop_signals.phrases[0]}”, etc.) — cutting stream now{RESET}")
                        else:
                            pct = (milestone * 100) // _limit if _limit else 0
                            print(f"{YELLOW}  ⚠ REASONING LOOP WARNING — {milestone}/{_limit} ready-to-act signals "
                                  f"({pct}% of cut threshold); model keeps re-deciding to start coding{RESET}")
                        print(f"{DIM}  ── reasoning ──{RESET}")
                    if hits >= REASONING_LOOP_SIGNAL_LIMIT:
                        loop_cut = True
                        close = getattr(stream, "close", None)
                        if close:
                            close()
                        break
            if d.content:
                if mode == "think":
                    # close out the reasoning block; newline guarantees the green
                    # answer starts on a fresh line below the dim text
                    print()  # end the current reasoning line
                    print(f"{DIM}  ──────────────{RESET}")
                print(f"{GREEN}", end="")
                mode = "say"
                print(d.content, end="", flush=True)
                content.append(d.content)
            for tc in (d.tool_calls or []):
                # if we were mid-reasoning when tools kicked in, close it out so
                # the cyan tool box (which starts with its own \n) gets a clean line
                if mode == "think":
                    print()
                    print(f"{DIM}  ──────────────{RESET}")
                    mode = None
                s = tcs.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                if tc.id:
                    s["id"] = tc.id
                if tc.function and tc.function.name:
                    s["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    s["args"] += tc.function.arguments
    finally:
        spinner.stop()
        # signal the watcher to exit (it'll restore termios in its own finally)
        _INTERRUPT_EVENT.set()
        watcher.join(timeout=0.5)
        _INTERRUPT_EVENT.clear()
    # reasoning-only turn (no content, no tool_calls) — close out the block so
    # the stats footer doesn't run straight into the dim reasoning text
    if mode == "think":
        print()
        print(f"{DIM}  ──────────────{RESET}")
    print(RESET)
    text = "".join(content)
    elapsed = time.time() - t0

    if interrupted:
        print(f"{YELLOW}  ↳ interrupted by user (Esc) after {elapsed:4.1f}s, "
              f"{len(content)} chars streamed{RESET}")
        # Discard partial content — it's almost certainly a half-formed
        # sentence / tool-call args. Append a synthetic user turn so the
        # model has context for what just happened, then return False so the
        # REPL drops to the prompt instead of looping into another turn.
        messages.append({"role": "user", "content":
            "[User interrupted your previous response with Esc. "
            "Acknowledge briefly and wait for their next message.]"})
        return TURN_DONE

    if loop_cut:
        retry_limit = max(0, REASONING_LOOP_RETRY_LIMIT)
        if reasoning_loop_cut_count >= retry_limit:
            print(f"{RED}  ✂ REASONING LOOP MAX RETRIES HIT — gave up after "
                  f"{reasoning_loop_cut_count} cut{'s' if reasoning_loop_cut_count != 1 else ''} "
                  f"× {loop_signals.hits} ready-to-act signals each; "
                  f"waiting for user input{RESET}")
            return TURN_DONE
        nudge = REASONING_LOOP_NUDGES[
            min(reasoning_loop_cut_count, len(REASONING_LOOP_NUDGES) - 1)]
        print(f"{YELLOW}  ✂ REASONING LOOP CUT — {loop_signals.hits} ready-to-act signals "
              f"(limit {REASONING_LOOP_SIGNAL_LIMIT}); nudging implementation "
              f"(retry {reasoning_loop_cut_count + 1}/{retry_limit}){RESET}")
        _nudge_current_user_turn(messages, nudge)
        return TURN_LOOP_CUT

    # stats footer — prefer llama.cpp timings if present; otherwise fall back
    # to the standard streaming `usage` object (OpenAI, Z.ai, etc.); otherwise
    # fall back to wall-clock only. TTFT (time to first token) is shown when
    # available — for local it comes from llama.cpp timings, for remote we
    # measure it client-side.
    if timings and timings.get("predicted_n"):
        prompt_n = timings.get("prompt_n", 0)
        cache_n = timings.get("cache_n", 0)
        gen_n = timings["predicted_n"]
        tps = timings.get("predicted_per_second", 0)
        ctx = f"ctx {_abbr(prompt_n)}+{_abbr(cache_n)} cached" if cache_n else f"ctx {_abbr(prompt_n)}"
        prompt_ms = timings.get("prompt_ms", 0)
        ttft = (prompt_ms / 1000.0) if prompt_ms else t_first
        parts = [f"{gen_n} tok", f"{tps:5.1f} tok/s", ctx]
        if ttft:
            parts.append(f"{ttft*1000:4.0f}ms ttft")
        parts.append(f"{elapsed:4.1f}s wall")
        print(f"{DIM}  └ {' · '.join(parts)}{RESET}")
    elif usage and (usage.completion_tokens or 0):
        gen_n = usage.completion_tokens or 0
        prompt_n = usage.prompt_tokens or 0
        cache_n = getattr(usage, "prompt_tokens_details", None)
        cached = None
        if cache_n is not None:
            cached = getattr(cache_n, "cached_tokens", None)
        tps = gen_n / elapsed if elapsed > 0 else 0
        if cached:
            ctx = f"ctx {_abbr(prompt_n)}+{_abbr(cached)} cached"
        else:
            ctx = f"ctx {_abbr(prompt_n)}"
        parts = [f"{gen_n} tok", f"{tps:5.1f} tok/s", ctx]
        if t_first:
            parts.append(f"{t_first*1000:4.0f}ms ttft")
        parts.append(f"{elapsed:4.1f}s wall")
        print(f"{DIM}  └ {' · '.join(parts)}{RESET}")
    elif text or tcs:
        print(f"{DIM}  └ {elapsed:4.1f}s wall{RESET}")

    if tcs:  # native tool-calling path
        ordered = [tcs[i] for i in sorted(tcs)]
        messages.append({"role": "assistant", "content": text or None, "tool_calls": [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["args"]}}
            for c in ordered]})
        esc_action = None
        for idx, c in enumerate(ordered):
            try:
                args = json.loads(c["args"] or "{}")
            except json.JSONDecodeError:
                args = {}
            try:
                result = run_tool(c["name"], args)
            except _EscToChat as e:
                esc_action = e.args[0] if e.args else c["name"]
                # Record this call as cancelled, and fill in results for any
                # remaining tool_calls so the message history stays valid
                # (every assistant tool_call needs a matching tool result,
                # or the chat template rejects the context on the next turn).
                messages.append({"role": "tool", "tool_call_id": c["id"],
                                 "content": "CANCELLED by user (Esc) — returned to chat input"})
                for c2 in ordered[idx + 1:]:
                    messages.append({"role": "tool", "tool_call_id": c2["id"],
                                     "content": "SKIPPED (user pressed Esc at an earlier approval)"})
                break
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
        if esc_action is not None:
            print(f"{YELLOW}  ↳ escaped approval of {esc_action!r} — back to chat input{RESET}")
            messages.append({"role": "user", "content":
                "[User pressed Esc at a tool approval prompt and returned to chat to "
                "add more input. Acknowledge briefly and wait for their next message.]"})
            return TURN_ESC
        return TURN_TOOL

    calls = parse_text_calls(text)  # text-fallback path
    if calls:
        messages.append({"role": "assistant", "content": text})
        obs = []
        esc_action = None
        for n, a in calls:
            try:
                obs.append(f"Observation ({n}): {run_tool(n, a)}")
            except _EscToChat as e:
                esc_action = e.args[0] if e.args else n
                obs.append(f"Observation ({n}): CANCELLED by user (Esc)")
                break
        if esc_action is not None:
            obs.append("[User pressed Esc at a tool approval prompt and returned to chat "
                       "to add more input. Acknowledge briefly and wait for their next message.]")
            print(f"{YELLOW}  ↳ escaped approval of {esc_action!r} — back to chat input{RESET}")
            messages.append({"role": "user", "content": "\n".join(obs)})
            return TURN_ESC
        messages.append({"role": "user", "content": "\n".join(obs)})
        return TURN_TOOL

    messages.append({"role": "assistant", "content": text})
    return TURN_DONE


# --- multi-line chatbox input ---------------------------------------------
# Replaces the bare `input()` prompt with a framed, multi-line editor:
#   • Enter submits, Alt+Enter (or Ctrl+J) inserts a newline
#   • Paste (bracketed-paste mode) inserts its text verbatim — newlines stay,
#     a trailing newline at the end of paste is stripped so pasting never
#     accidentally submits
#   • Up/Down navigate history, Left/Right move within the current line,
#     Home/End jump to line start/end, Ctrl+U clears the line, Ctrl+C cancels
#   • Long lines word-wrap visually inside the box; the buffer stays one
#     logical string (newlines preserved) so the model sees the real text
# Falls back to plain `input()` when stdin/stdout is not a TTY.

def _chatbox_fallback(prompt):
    """Plain `input()` fallback used when raw terminal mode isn't usable. Single-line only;
    newlines must be typed as the literal `\\n` (rare in practice)."""
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        raise


def _raw_read_key(fd):
    return os.read(fd, 1).decode("utf-8", "replace")


def _raw_read_available(fd, timeout=0.02):
    parts = []
    while True:
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            break
        parts.append(os.read(fd, 1).decode("utf-8", "replace"))
        timeout = 0
    return "".join(parts)


def _chatbox_raw(initial="", history=None):
    """Normal-scrollback multi-line editor.

    This does not enter the alternate screen. The prompt, streamed model output,
    tool confirmations, and the next prompt all stay in one terminal mode, which
    avoids garbling the REPL after submit.
    """
    history = history or []
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    new = old[:]
    new[3] &= ~(termios.ECHO | termios.ICANON)
    new[0] &= ~termios.ICRNL
    new[6][termios.VMIN] = 1
    new[6][termios.VTIME] = 0

    buf = initial.split("\n") if initial else [""]
    row, col = len(buf) - 1, len(buf[-1])
    h_idx = len(history)
    rendered_lines = 0
    cursor_line = 0

    def move_up(n):
        return f"\x1b[{n}A" if n > 0 else ""

    def move_down(n):
        return f"\x1b[{n}B" if n > 0 else ""

    def move_right(n):
        return f"\x1b[{n}C" if n > 0 else ""

    def build_visual(inner_w):
        visual = []
        for bi, line in enumerate(buf):
            if not line:
                visual.append((bi, 0, ""))
                continue
            for start in range(0, len(line), inner_w):
                visual.append((bi, start, line[start:start + inner_w]))
            if col == len(line) and row == bi and len(line) % inner_w == 0:
                visual.append((bi, len(line), ""))
        return visual

    def render():
        nonlocal rendered_lines, cursor_line
        width = shutil.get_terminal_size((80, 24)).columns
        box_w = max(20, min(width - 2, 100))
        inner_w = max(1, box_w - 2)
        visual = build_visual(inner_w)

        cur_vrow = 0
        cur_vcol = 0
        for i, (bi, start, seg) in enumerate(visual):
            if bi != row:
                continue
            if start <= col <= start + len(seg):
                cur_vrow = i
                cur_vcol = col - start
                break

        hints = "Enter send · Alt+Enter / ^J newline · ^C cancel"
        max_hints = max(0, box_w - 11)
        if len(hints) > max_hints:
            hints = hints[:max_hints - 1] + "…" if max_hints > 1 else "…"
        top_fill = max(0, box_w - 11 - len(hints))
        stats = f"{len(buf)} line{'s' if len(buf) != 1 else ''} · {sum(len(l) for l in buf)} chars"
        max_stats = max(0, box_w - 6)
        if len(stats) > max_stats:
            stats = stats[:max_stats - 1] + "…" if max_stats > 1 else "…"
        bot_fill = max(0, box_w - 4 - len(stats))

        lines = [
            f"{DIM}╭─ {RESET}{CYAN}you{RESET}{DIM} · {hints} {'─' * top_fill}╮{RESET}",
            *[f"{DIM}│{RESET}{seg:<{inner_w}}{DIM}│{RESET}" for _, _, seg in visual],
            f"{DIM}╰─ {stats}{' ' * bot_fill}╯{RESET}",
        ]

        redraw_rows = max(rendered_lines, len(lines))
        out = "\r" + move_up(cursor_line)
        for i in range(redraw_rows):
            out += "\x1b[2K"
            if i < len(lines):
                out += lines[i]
            if i != redraw_rows - 1:
                out += "\n"
        out += "\r" + move_up((redraw_rows - 1) - (cur_vrow + 1)) + move_right(cur_vcol + 1)
        sys.stdout.write(out)
        sys.stdout.flush()
        rendered_lines = len(lines)
        cursor_line = cur_vrow + 1

    def finish():
        sys.stdout.write("\r" + move_down((rendered_lines - 1) - cursor_line) + "\n")
        sys.stdout.write("\x1b[?25h\x1b[?2004l")
        sys.stdout.flush()

    def insert_text(s):
        nonlocal row, col
        if not s:
            return
        parts = s.split("\n")
        cur = buf[row]
        tail = cur[col:]
        if len(parts) == 1:
            buf[row] = cur[:col] + parts[0] + tail
            col += len(parts[0])
            return
        buf[row] = cur[:col] + parts[0]
        new_lines = list(parts[1:-1]) + [parts[-1] + tail]
        buf[row + 1:row + 1] = new_lines
        row += len(new_lines)
        col = len(parts[-1])

    def backspace():
        nonlocal row, col
        if col > 0:
            buf[row] = buf[row][:col - 1] + buf[row][col:]
            col -= 1
        elif row > 0:
            prev = buf[row - 1]
            col = len(prev)
            buf[row - 1] = prev + buf[row]
            del buf[row]
            row -= 1

    def delete_forward():
        line = buf[row]
        if col < len(line):
            buf[row] = line[:col] + line[col + 1:]
        elif row < len(buf) - 1:
            buf[row] = line + buf[row + 1]
            del buf[row + 1]

    def load_from_history(hist_text):
        nonlocal row, col
        buf[:] = hist_text.split("\n") if hist_text else [""]
        row = len(buf) - 1
        col = len(buf[-1])

    def handle_escape(seq):
        nonlocal row, col, h_idx
        if seq.startswith("[200~"):
            paste = seq[5:]
            while "\x1b[201~" not in paste:
                paste += _raw_read_key(fd)
            paste = paste.split("\x1b[201~", 1)[0]
            if paste.endswith("\n") or paste.endswith("\r"):
                paste = paste[:-1]
            insert_text(paste)
            return
        if seq in ("[A", "OA"):
            if history and h_idx > 0:
                h_idx -= 1
                load_from_history(history[h_idx])
            return
        if seq in ("[B", "OB"):
            if history and h_idx < len(history):
                h_idx += 1
                load_from_history("" if h_idx == len(history) else history[h_idx])
            return
        if seq in ("[C", "OC"):
            if col < len(buf[row]):
                col += 1
            elif row < len(buf) - 1:
                row += 1
                col = 0
            return
        if seq in ("[D", "OD"):
            if col > 0:
                col -= 1
            elif row > 0:
                row -= 1
                col = len(buf[row])
            return
        if seq in ("[H", "OH"):
            col = 0
            return
        if seq in ("[F", "OF"):
            col = len(buf[row])
            return
        if seq == "[3~":
            delete_forward()
            return
        if seq in ("\r", "\n"):
            insert_text("\n")

    termios.tcsetattr(fd, termios.TCSADRAIN, new)
    sys.stdout.write("\x1b[?25h\x1b[?2004h\n")
    sys.stdout.flush()
    try:
        render()
        while True:
            c = _raw_read_key(fd)
            if c == "\x1b":
                handle_escape(_raw_read_available(fd))
            elif c == "\r":
                finish()
                return "\n".join(buf)
            elif c == "\n":
                insert_text("\n")
            elif c == "\x03":
                raise KeyboardInterrupt
            elif c == "\x04":
                if not any(buf):
                    raise EOFError
            elif c in ("\x7f", "\x08"):
                backspace()
            elif c == "\x15":
                buf[row] = ""
                col = 0
            elif c >= " " or c == "\t":
                insert_text(c)
            render()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\x1b[?25h\x1b[?2004l")
        sys.stdout.flush()


def read_multiline(initial="", history=None):
    """Public entry point. Returns the entered text, or '' on empty submit.
    Raises EOFError / KeyboardInterrupt to match `input()`'s contract."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        # Fallback path — single-line input. Same UX as before this feature.
        return _chatbox_fallback(f"{CYAN}you ›{RESET} ")
    try:
        return _chatbox_raw(initial, history)
    except (EOFError, KeyboardInterrupt):
        raise
    except (OSError, termios.error):
        return _chatbox_fallback(f"{CYAN}you ›{RESET} ")


# --- repl -------------------------------------------------------------------
# The descriptor (URL, commands, log path) used to live here in the banner.
# Now that the status bar at row 1 carries it permanently, the banner is
# just a one-line welcome at the bottom of the scroll region — model name
# only, since everything else is already pinned at the top.
def _banner():
    """One-line welcome shown after the status bar is set up. Rebuilt on
    each call so it reflects the current source after a /source switch."""
    src_tag = f" {DIM}·{RESET} {MAGENTA}{ACTIVE.name}{RESET}" if len(SOURCES) > 1 else ""
    return f"{BOLD}minion{RESET} {DIM}·{RESET} {CYAN}{MODEL}{RESET}{src_tag}"


_STATUS_BAR_ACTIVE = False


def _build_status_bar(cols):
    """Compose the status-bar string for the given terminal width. Builds
    left-to-right, dropping less-important pieces (log path, URL, commands)
    when the terminal is too narrow. Adds the source name in magenta when
    more than one source is configured."""
    sep = f" {DIM}·{RESET} "
    def _vis(s):
        return len(re.sub(r'\033\[[0-9;]*m', '', s))

    parts = [f"{BOLD}minion{RESET}", f"{CYAN}{MODEL}{RESET}"]
    if len(SOURCES) > 1:
        parts.append(f"{MAGENTA}{ACTIVE.name}{RESET}")
    # Show the approval mode so it's always visible at a glance. Green when
    # nothing will prompt (high / yolo), yellow when medium, dim for the
    # default low.
    if YOLO or APPROVE_LEVEL is None:
        parts.append(f"{GREEN}yolo{RESET}")
    elif APPROVE_LEVEL == "high":
        parts.append(f"{GREEN}auto:high{RESET}")
    elif APPROVE_LEVEL == "medium":
        parts.append(f"{YELLOW}auto:medium{RESET}")
    else:
        parts.append(f"{DIM}auto:low{RESET}")
    used = sum(_vis(p) for p in parts) + _vis(sep) * (len(parts) - 1)
    for piece in (str(client.base_url),
                  "/source /yolo /approval /compress /compact /reset /quit",
                  "log → llamacpp.log"):
        extra = _vis(sep) + _vis(piece)
        if used + extra <= cols - 2:
            parts.append(piece)
            used += extra
        else:
            break
    return sep.join(parts)


def _setup_status_bar():
    """Pin a one-line status bar at the top of the terminal and confine the
    rest of the session to a scroll region below it (DECSTBM, same primitive
    tmux / vim / less use for their status lines). Returns True if installed;
    False if skipped (non-TTY, terminal too short, or --no-scroll-bottom).
    Sets _STATUS_BAR_ACTIVE so _paint_status_bar knows it can repaint."""
    global _STATUS_BAR_ACTIVE
    if not sys.stdout.isatty() or "--no-scroll-bottom" in sys.argv:
        return False
    rows, cols = shutil.get_terminal_size((80, 24))
    if rows < 5:
        return False

    status = _build_status_bar(cols)
    sys.stdout.write(f"\033[2;{rows}r")
    sys.stdout.write(f"\033[1;1H\033[2K{status}")
    # Wipe the scroll region so stale terminal content (previous shell
    # output, a prior minion session, etc.) doesn't linger below the bar.
    # \033[J erases from the cursor (now at row 2) to the end of the display,
    # which is exactly rows 2..rows — the scroll region we just set. Row 1
    # (the freshly-painted status bar) is untouched since the cursor starts
    # the erase at row 2.
    sys.stdout.write(f"\033[2;1H\033[J")
    sys.stdout.write(f"\033[{rows};1H")
    sys.stdout.flush()
    _STATUS_BAR_ACTIVE = True
    return True


def _paint_status_bar():
    """Repaint row 1 after a /source switch. No-op if the bar was never
    installed. Returns cursor to the bottom of the scroll region."""
    if not _STATUS_BAR_ACTIVE:
        return
    sz = shutil.get_terminal_size((80, 24))
    status = _build_status_bar(sz.columns)
    sys.stdout.write(f"\033[1;1H\033[2K{status}")
    sys.stdout.write(f"\033[{sz.lines};1H")
    sys.stdout.flush()


def main():
    global YOLO, APPROVE_LEVEL
    # Status bar at top + scroll region for the rest. The bar (model, URL,
    # commands, log path) stays pinned at row 1 no matter how long the session
    # runs; everything else — banner, chat, model output, tool boxes — lives
    # in rows 2..rows and scrolls within that region. This replaces the old
    # "print N blank lines to push the banner to the bottom" trick, which was
    # both fragile (depends on banner height) and one-shot (the banner
    # scrolled away the moment anything was printed). Skipped when stdout
    # isn't a TTY (piped/redirected — would inject escape codes into a log
    # file) or when the user passes --no-scroll-bottom.
    _setup_status_bar()
    print(_banner())
    print()
    messages = [{"role": "system", "content": SYSTEM}]
    history = []  # past user submissions, newest last; Up/Down navigates
    while True:
        try:
            user = read_multiline(history=history)
        except (EOFError, KeyboardInterrupt):
            print()
            break
        user = user.strip()
        if not user:
            continue
        if user == "/quit":
            break
        if user == "/source" or user.startswith("/source "):
            sp = user.split()
            if len(sp) == 1:
                print(f"{DIM}  sources:{RESET}")
                for sname in SOURCE_ORDER:
                    src = SOURCES[sname]
                    mark = f"{GREEN}★{RESET}" if sname == ACTIVE.name else f"{DIM} ·{RESET}"
                    m = src.display_model()
                    print(f"  {mark} {CYAN}{sname:<12}{RESET} {DIM}{m} @ {src.base_url}{RESET}")
                print(f"{DIM}  /source <name> to switch · context preserved (use /reset to clear){RESET}")
            else:
                target = sp[1]
                if target not in SOURCES:
                    avail = ", ".join(SOURCE_ORDER)
                    print(f"{RED}  ✗ unknown source {target!r} — available: {avail}{RESET}")
                    continue
                if target == ACTIVE.name:
                    print(f"{DIM}  already on {target}{RESET}")
                    continue
                switch_source(target)
                src = SOURCES[target]
                print(f"{BOLD}{YELLOW}  → switched to {target}{RESET} {DIM}({MODEL} @ {src.base_url}){RESET}")
                _paint_status_bar()
            continue
        if user == "/yolo":
            YOLO = not YOLO
            if YOLO:
                APPROVE_LEVEL = None  # never prompt
            else:
                APPROVE_LEVEL = "low"  # back to default
            print(f"{DIM}  yolo={YOLO}  approval={('off' if YOLO else APPROVE_LEVEL)}{RESET}")
            _paint_status_bar()
            continue
        if user.startswith("/approval"):
            parts = user.split()
            if len(parts) == 1:
                # /approval with no arg → show current setting
                cur = "off (yolo)" if YOLO else (APPROVE_LEVEL or "off")
                print(f"{DIM}  approval={cur}  (low|medium|high|yolo){RESET}")
                continue
            arg = parts[1]
            resolved = _normalize_level(arg)
            if resolved:
                YOLO = False
                APPROVE_LEVEL = resolved
                print(f"{DIM}  approval={resolved} (auto-allow ≤ {resolved}){RESET}")
                _paint_status_bar()
            elif arg.lower() == "yolo":
                YOLO = True
                APPROVE_LEVEL = None
                print(f"{DIM}  approval=off (yolo — never prompt){RESET}")
                _paint_status_bar()
            else:
                print(f"{YELLOW}  unknown level {arg!r} — want low|medium|high|yolo{RESET}")
            continue
        if user == "/reset":
            messages = [{"role": "system", "content": SYSTEM}]
            print(f"{DIM}  context cleared{RESET}")
            continue
        if user in ("/compress", "/compact"):
            # nothing to compress if we're under (system + KEEP) turns
            body_len = len(messages) - (1 if messages and messages[0].get("role") == "system" else 0)
            if body_len <= COMPRESS_KEEP:
                print(f"{DIM}  nothing to compress ({body_len} turn{'s' if body_len != 1 else ''} in context){RESET}")
                continue
            try:
                ok = _confirm(f"compress {body_len - COMPRESS_KEEP} older turns (keep last {COMPRESS_KEEP})")
            except _EscToChat:
                print(f"{YELLOW}  ↳ escaped compress approval — back to chat input{RESET}")
                continue
            if not ok:
                print(f"{DIM}  cancelled{RESET}")
                continue
            print(f"{DIM}  compressing…{RESET}")
            result = compress(messages)
            if result is None:
                continue  # error already printed
            kept_n, summarized_n, summary_chars = result
            print(f"{DIM}  └ compressed {summarized_n} turns → 1 summary "
                  f"({summary_chars} chars), kept last {kept_n} verbatim{RESET}")
            continue
        # record for history (skip duplicates of the very last entry so
        # Up doesn't immediately re-show what was just submitted)
        if not history or history[-1] != user:
            history.append(user)
        print()  # breathing room before the spinner/text starts
        messages.append({"role": "user", "content": user})
        steps = 0
        reasoning_loop_cuts = 0
        while steps < 25:  # cap runaway tool/retry loops
            status = model_turn(messages, reasoning_loop_cuts)
            if status == TURN_DONE:
                break
            if status == TURN_ESC:
                break  # user pressed Esc at an approval → drop to chat input
            steps += 1
            if status == TURN_LOOP_CUT:
                reasoning_loop_cuts += 1
                continue
            if status == TURN_TOOL:
                reasoning_loop_cuts = 0
                continue


if __name__ == "__main__":
    main()
