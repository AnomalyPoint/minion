# Changelog

All notable changes to `minion.py` from this point forward.

> **Note:** the file was previously called `miniagent.py` and configured via
> `AGENT_BASE_URL` / `AGENT_MODEL` / `AGENT_API_KEY`. Renamed to `minion` /
> `MINION_*` for clarity. The old env vars are silently ignored — set the new
> ones.

### Changed — abbreviated token counts in stats footer
Context and cache token counts in the per-turn stats footer are now
abbreviated for readability: `832` stays as `832`, `1500` → `1.5K`,
`78825` → `78K`, `1234567` → `1.2M`. The footer (model-generated token
counts, prompt context, and cached tokens) is the noisiest line once a
session grows long — whole-word counts like `78825` are hard to scan at a
glance.

- New `_abbr(n)` helper. Cutoffs: raw under 1K, one-decimal `X.YK` from
  1K–10K, whole `NNK` up to 1M, then the same split at millions.
- Applied in both footer branches: the llama.cpp `timings` path
  (`ctx P+C cached`) and the standard OpenAI `usage` path.

### Added — Esc at approval prompts (back to chat)
The `Y/n` approval prompt for writes/edits/bash now also accepts **Esc**.
Pressing Esc stops the current turn and drops you back to the chat input so
you can add more guidance — useful when the model is about to do something
slightly off and you'd rather redirect it than approve/deny in isolation.

- New `TURN_ESC` turn status and new `_EscToChat` exception. The exception is
  a control-flow signal (not a real error): it's raised by `_confirm`, left
  to propagate through `run_tool`, caught in `model_turn`.
- New `_ask_approval(prompt)` helper. Reads a single keypress in raw terminal
  mode (rather than line-based `input()`, which can't detect a bare Esc),
  disambiguating bare Esc from arrow-key / bracketed-paste escape sequences
  via a 50ms wait for trailing bytes. Returns `y` / `n` / `esc`, echoes the
  choice, and falls back to line input (`esc`/`e`/`\x1b` accepted) when
  stdin isn't a TTY. The prompt now reads `[Y/n/esc]`.
- `_confirm` raises `_EscToChat(action)` on Esc; Y/n behavior is unchanged.
- `run_tool` re-raises `_EscToChat` instead of swallowing it as an error
  string, and closes the cyan tool box with a `(escaped)` marker first.
- `model_turn` catches `_EscToChat` in both the native tool-call path and the
  text-fallback path. The escaped call is recorded as `CANCELLED by user
  (Esc)`; if the assistant emitted multiple tool calls, the remaining ones
  are marked `SKIPPED` so every `tool_call` still has a matching `tool`
  result (otherwise the chat template rejects the context on the next
  request). A synthetic user note is appended and `TURN_ESC` is returned.
- `main`'s REPL breaks the inner turn loop on `TURN_ESC`, returning to the
  chat input. The `/compress` approval also honors Esc now.
- New `tests/test_esc_approval.py` (5 tests): `_confirm` raises on esc,
  Y/n passthrough, `run_tool` propagation, `model_turn` history-validity on
  a multi-tool escape, and the REPL loop break.

### Added — pinned status bar (DECSTBM scroll region)
A one-line status bar pinned at row 1 of the terminal (model name, source,
approval mode, endpoint, command hints) using DECSTBM — the same scroll-region
primitive tmux / vim / less use for their status lines. Chat output scrolls
in the region below it (rows 2..bottom) without disturbing the bar.

- New `_setup_status_bar()` — emits `\033[2;{rows}r` to set the scroll
  region, paints row 1 with `_build_status_bar()`, erases stale content
  below the bar (`\033[2;1H\033[J`), and parks the cursor at the bottom.
  Sets `_STATUS_BAR_ACTIVE` so `_paint_status_bar` knows it can repaint.
- New `_build_status_bar(cols)` — composes the status string left-to-right,
  dropping less-important pieces (log path, URL, commands) when the terminal
  is too narrow. Adds the source name in magenta when more than one source is
  configured. Shows the approval mode color-coded (green for yolo/high,
  yellow for medium, dim for low).
- New `_paint_status_bar()` — repaints row 1 after a `/source` or `/yolo` /
  `/approval` switch so the bar always reflects current state.
- New `--no-scroll-bottom` flag to disable the whole feature (useful for
  piped/redirected output or terminals that don't implement DECSTBM).
- `_banner()` simplified to a one-line welcome (model name + source) since
  the bar now carries the endpoint/commands. Rebuilt on each call so it
  reflects the active source after a switch.
- Stale terminal content (previous shell output, prior sessions) is now
  cleared on startup so it doesn't linger below the bar.

> **Known limitation:** the pinned bar relies on terminal scroll-region
> support. Works in most terminals and under tmux, but some multiplexers
> (notably Zellij) don't fully implement DECSTBM — in those the bar may
> scroll away after enough output.

### Added — multi-source system (`MINION_SOURCES`)
Define multiple named endpoints and switch between them at runtime without
restarting. Each "source" bundles a base URL, API key, and optional model
name. Conversation context is preserved across switches (use `/reset` for a
clean slate).

- New `MINION_SOURCES=local,zai` env var (comma-separated source names;
  the first is the default at startup).
- Per-source config via `MINION_SOURCE_<NAME>_BASE_URL`,
  `MINION_SOURCE_<NAME>_API_KEY`, `MINION_SOURCE_<NAME>_MODEL`.
- **`$key` indirection** — `MINION_SOURCE_ZAI_API_KEY=$zai_test` looks up
  the env var (or `~/.env` key) named `zai_test` rather than taking the
  literal string. Avoids duplicating keys that already live somewhere.
- **Auto-discovery** — if `MINION_SOURCES` is unset, minion scans for
  `MINION_SOURCE_*_BASE_URL` vars and builds sources from those.
- New `Source` class (`resolve_model()` queries `/v1/models` if no model is
  set; `display_model()` shows `auto` when unset). Each source has its own
  `OpenAI` client.
- New `switch_source(name)` — reassigns the global `client` / `MODEL` /
  `ACTIVE` so a mid-session swap is picked up instantly by every function
  that reads them.
- New `--source <name>` flag and `MINION_ACTIVE` env var to pick the
  starting source.
- New `/source` REPL command — bare lists all sources with model + URL;
  `/source <name>` switches and repaints the status bar.
- New `sources.example.env` with an annotated multi-source template.
- Backward compatible: if no `MINION_SOURCE_*` vars are present, a single
  `local` source is built from the legacy `MINION_BASE_URL` /
  `MINION_API_KEY` / `MINION_MODEL` vars.

### Added — `~/.env` auto-loading (`MINION_ENV_FILE`)
minion now reads `~/.env` at startup (before source discovery) and populates
`os.environ` from it, without clobbering vars already set in the shell.
Sources, API keys, and other config can live in one place instead of being
exported in every terminal.

- New `_load_env_file()` — parses `KEY=VALUE` lines (handles `export`
  prefix, quoted values, comments, blank lines). Points at
  `MINION_ENV_FILE` if set, otherwise `~/.env`.
- Existing `export`-based setups keep working unchanged.

### Added — escalating reasoning-loop nudges + retry limit
The reasoning-loop guard now escalates across multiple cuts instead of
firing the same nudge repeatedly.

- `REASONING_LOOP_NUDGES` — a tuple of 3 escalating nudges: first is a
  gentle "stop planning, take action"; second is stricter ("exactly one
  concrete action"); third is a hard stop ("emit only a tool call now").
  The nudge index is clamped to `len(nudges) - 1`.
- New `MINION_REASONING_LOOP_RETRIES` env var (default: number of nudges)
  — after this many cuts, minion stops retrying, prints a "max retries hit"
  message, and drops back to the prompt for user input.
- New `TURN_LOOP_CUT` return status from `model_turn`; the REPL increments
  `reasoning_loop_cuts` and retries with the next nudge (resets to 0 on a
  successful tool turn).
- New `_nudge_current_user_turn(messages, nudge)` — strips any prior
  `[Runtime note: …]` from the latest user turn and appends the new nudge.
- New `RUNTIME_NOTE_RE` to clean up stale runtime notes before re-nudging.

### Added — reasoning-loop milestone warnings
As "ready-to-act" signals accumulate during reasoning, minion now prints
visible warnings at 25 / 50 / 75 / 100% of the cut threshold — so you can
see the model spiraling before it gets cut, not just at the cut itself.

- `_ReasoningLoopSignalCounter.feed()` returns the running hit count.
- Milestones computed as clamped fractions of `REASONING_LOOP_SIGNAL_LIMIT`;
  warnings are yellow (`⚠ REASONING LOOP WARNING — M/N signals (XX%)`),
  the threshold-cross is red (`⚠ REASONING LOOP LIMIT HIT`).
- Fires at most ~5 lines per turn (first hit + 4 milestones) to keep noise
  down while remaining impossible to miss in the dim reasoning stream.

### Added — streaming usage + TTFT in stats footer
The stats footer at the end of each turn now works with servers that send
the standard OpenAI `usage` object (not just llama.cpp's `timings`).

- `stream_options={"include_usage": True}` sent on all streaming requests so
  the final SSE chunk carries token counts (OpenAI, Z.ai, vLLM, etc.).
- **TTFT** (time-to-first-token) measured client-side on the first chunk
  carrying real output; shown when available (`Tms ttft`).
- Footer now shows cached-token counts when the server reports them
  (`ctx P+C cached`).
- Three-tier fallback: llama.cpp `timings` → standard `usage` object →
  wall-clock only.

### Added — `pip install` support
- New `setup.py` / `pyproject.toml` — registers a `minion` console script
  pointing at `minion:main()`. `pip install -e .` for editable (picks up
  edits immediately), `pip install .` for non-editable.
- New `requirements.txt` (`openai>=1.0`).

### Changed — `model_turn` takes `reasoning_loop_cut_count`
Now accepts a second parameter so the REPL can track how many times the
reasoning loop has been cut in the current user turn and escalate nudges
accordingly. The stub signature change is what broke the old compact-alias
test.

### Changed — open_stream sends `stream_options`
Both the tools-enabled and fallback (no-tools) streaming requests now send
`stream_options={"include_usage": True}`.

---

### Added — Esc-to-interrupt during model generation
Press **Esc** while the model is streaming (or during the spinner wait
before the first token) to drop the current stream and return to the
prompt. Partial content is discarded and a synthetic user turn is appended
to context so the model knows what happened on its next turn.

- New `_interrupt_watcher()` daemon: puts stdin in raw mode (ISIG off so
  Ctrl+C still kills the process), polls for bare Esc with a 50ms wait
  for trailing bytes (so it doesn't fire on arrow-key / bracketed-paste
  escape sequences), debounces at 250ms, restores termios on exit.
  Started by `model_turn` before the spinner; signaled to exit in the
  `finally` block. Two events (`_INTERRUPT_EVENT`, `_USER_INTERRUPTED`)
  separate "watcher should exit" from "user actually pressed Esc" so
  cleanup doesn't get confused with a real interrupt.
- `model_turn` checks `_USER_INTERRUPTED` between chunks; on hit it closes
  the stream, prints `↳ interrupted by user (Esc) after N.Ns, N chars
  streamed`, appends a `[User interrupted your previous response with
  Esc. Acknowledge briefly and wait for their next message.]` user turn,
  and returns `TURN_DONE` so the REPL drops to the prompt instead of
  looping into another turn.
- Spinner label changed to `"thinking · esc to interrupt"` so the
  affordance is visible at the wait-for-first-token moment.
- In-flight tool calls (`run_bash`, `write_file`, etc.) are **not**
  cancelled — they run to completion. Hard-stop with Ctrl+C if you need
  one. (Cancel-a-running-tool is a separate follow-up.)

### Added — reasoning-loop guard (`MINION_REASONING_LOOP_SIGNALS`)
Reasoning models sometimes spin in place — they keep saying "let me
implement…" / "start coding…" / "now I'll write the code…" without ever
emitting content or a tool call, burning tokens and stalling the turn.
minion counts how many of those "ready to act" phrases appear during the
reasoning phase and, after the threshold, cuts the stream and appends a
one-shot nudge to the latest user turn telling the model to stop planning
and take a concrete action.

- `REASONING_LOOP_SIGNALS` (tuple of 9 phrases) and
  `REASONING_LOOP_SIGNALS_LIMIT` (default 10, override with
  `MINION_REASONING_LOOP_SIGNALS` env var; `0` disables) module constants.
- New `_ReasoningLoopSignalCounter` class — sliding-window phrase counter
  that scans each streamed `reasoning_content` chunk for new occurrences
  (only counts matches that extend past the previous boundary so we don't
  double-count a phrase split across two chunks).
- `model_turn` instantiates one per turn; on threshold hit it closes the
  stream and appends the `REASONING_LOOP_NUDGE` text to the most recent
  user turn (via `_nudge_current_user_turn`, which creates one if there
  isn't one yet). Prints `↳ cut reasoning loop after N ready-to-act
  signals; nudging implementation` so the cut is visible in the log.
- Only active during the reasoning phase (skipped once `content` /
  `tool_calls` start arriving), so a model that legitimately says "let me
  implement" once before doing it isn't tripped up.

### Added — tool-running spinner
The same `LifeSpinner` that animates during model streaming now also runs
between the cyan `┌─ name` / `│ args` lines and the cyan `└─ result`
lines, with label `"running"`. Without it the screen freezes for the
duration of a slow `run_bash` / `_assess_risk` round-trip / large
`write_file` — the user just sees the green model output end and then
nothing until the result box pops in.

- `LifeSpinner` gained a `label=` constructor arg (`"thinking"` by
  default; `model_turn` uses `"thinking · esc to interrupt"`; tool bodies
  use `"running"`).
- New `_ACTIVE_SPINNER` module-level pointer set by `run_tool()` around
  the tool body. `_confirm()` pauses/resumes it around its own I/O so the
  auto-allow line / `Y/n` prompt don't get clobbered by an animation tick.

### Added — risk-gated approval (`--approval` / `/approval`)
Sits between today's "ask for everything" default and `--yolo`'s "ask for
nothing". Every write / edit / bash call is risk-classified by a single
cheap non-streaming call to the same model before it runs. Levels:
`low` (read-only or trivially reversible), `medium` (modifies state but
contained/reversible), `high` (destructive, hard to reverse, or broad
scope). `APPROVE_LEVEL` is the minimum level that requires approval:

| flag                    | prompts at        | auto-allows       |
| ----------------------- | ----------------- | ----------------- |
| _(default)_             | low + medium + high | —               |
| `--approval medium`     | medium + high     | low               |
| `--approval high`       | high only         | low + medium      |
| `--yolo`                | _(never)_         | everything        |

In `--approval high` mode, `ls`, `cat`, single-file writes, `pip install`,
etc. run without asking; only `rm -rf`, `git push --force`, broad
destructive ops, etc. need a yes/no. The assessment is shown in brackets
next to the prompt (`[risk: HIGH — recursive force delete in /tmp]`) so
the user has context for the decision, and auto-allowed calls print a
one-liner (`↳ auto-allow [low] ls -la (read-only listing)`).

- New module-level `LEVEL_ORDER = {"low": 0, "medium": 1, "high": 2}` and
  `APPROVE_LEVEL` (string or `None` for yolo). CLI flag `--approval <level>`
  parsed in the config block; `--yolo` overrides it and sets `APPROVE_LEVEL
  = None` (no risk call, no prompt).
- New `RISK_SYSTEM` prompt + `_assess_risk(action)` function. Non-streaming
  call with a 15s timeout; logs to `llamacpp.log` under `_purpose: risk`.
  Defensive parse: tries JSON first, falls back to regex-matching a level
  word, falls back to `("high", "<error>")` on any failure — so a broken
  classifier can never silently auto-approve a dangerous command.
- `_confirm(action)` now takes the assessment, shows it inline at the
  prompt (color-coded: dim/yellow/red), and auto-allows with a one-liner
  when `LEVEL_ORDER[assessed] < LEVEL_ORDER[APPROVE_LEVEL]`. YOLO
  short-circuits before the call (no point paying for a call we won't
  act on).
- New REPL command `/approval [level]` — bare shows current setting,
  with arg sets it (`low`/`medium`/`high`/`yolo`; unknown values print a
  yellow warning and leave state unchanged). `/yolo` now prints both
  `yolo=` and `approval=` so the relationship is obvious.
- README updated with the approval-modes table; banner updated to list
  `/approval`.

### Added — base-level traffic log (`llamacpp.log`)
Append-only JSONL record of every byte shipped to / received from the llama.cpp
endpoint. Lives next to `minion.py`.
- `req` events: full outgoing request body (model, messages, tools, stream
  flag) logged before the HTTP call. Fallback (no-tools) requests are logged
  with a `_fallback` marker.
- `resp` events: every raw SSE chunk captured via `model_dump()` as it
  streams in — preserves `reasoning_content`, tool-call deltas, etc. at the
  literal ground-truth level, before any parsing/rendering.
- File opened once at module load with `buffering=1` (line-buffered) so each
  event is flushed immediately; survives crashes without losing recent turns.
- Stream wrapped in a `_LoggingStream` iterator; logging errors are swallowed
  so a disk-full / permission error can never break the agent's response.
- New `import time`; new `_log_event()` helper and `LOG_PATH` constant.

### Added — terminal UI polish
- `LifeSpinner` — a 1-row Conway's Game of Life that runs on a background
  thread while waiting for the first token. Gliders/blinkers actually evolve
  (rows above/below mirror the current row so each cell gets the standard
  8-neighbor count, otherwise a 1-row CA is degenerate). Uses `\033[2K\r` to
  overwrite its own line and `\033[?25l/h` to hide/show the cursor.
- Stats footer at the end of every turn, pulled from llama.cpp's `timings`
  object on the final SSE chunk: `N tok · X.X tok/s · ctx P+C cached · T.Ts wall`.
  Falls back to wall-clock only if the server doesn't send timings.
- Banner at startup with model name, endpoint, and command hints.
- Tool calls now render as a small box (`┌─ name` / `│ args` / `│ output` /
  `└─`) instead of a bare arrow. Output truncated to 800 chars in the display;
  the model still receives the full result via the messages array.
- New ANSI helpers: `MAGENTA`, `BOLD`, `CLEAR_LINE`, `HIDE_CURSOR`,
  `SHOW_CURSOR`.

### Changed
- `model_turn` now starts the spinner before opening the stream and stops it
  the moment the first chunk arrives (or in a `finally` if the stream errors).
- Tool output is line-wrapped into the box rather than dumped raw.
- `_log_event("resp", ...)` in `compress()` wraps the `model_dump()` call in
  `try/except` to match the streaming path's "never let logging break the
  agent" pattern — a non-pydantic response object won't crash the summary
  call anymore.

### Fixed — `/compress` could leave an orphan tool message in the kept tail
If the last `COMPRESS_KEEP` turns ended with a half-finished tool-call
sequence (e.g. the assistant called a tool but the result was the last turn,
or — more commonly — the assistant tool-call turn landed in `head` and only
its `tool` result made it into `tail`), llama.cpp's chat template would raise
`Message has tool role, but there was no previous assistant message with a
tool call!` on the very next request. `compress()` now walks the front of
the tail and drops any leading `tool` or unmatched `assistant(tool_calls)` turn
before splicing the summary in. The `summarized_n` count is bumped by the
number of extra turns absorbed so the user-visible footer stays honest.

### Added — multi-line chatbox input
Replaces the bare `input()` prompt with a framed, multi-line editor in the
terminal. Prompt, streamed model output, tool confirmations, and the next
prompt all stay in the normal terminal scrollback (no alternate screen) to
avoid garbling the REPL after submit.
- Enter submits; Alt+Enter / Ctrl+J insert newlines.
- Bracketed-paste mode preserves pasted newlines verbatim and strips a
  trailing newline so pasting never accidentally submits.
- Up/Down navigate past submissions; Left/Right move within the current
  line; Home/End jump to line start/end; Ctrl+U clears the line;
  Ctrl+C cancels.
- Long lines word-wrap visually inside the box; the buffer stays one logical
  string (newlines preserved) so the model sees the real text.
- Falls back to plain `input()` when stdin/stdout is not a TTY.
- New `read_multiline()` public entry point and `_chatbox_raw()` /
  `_chatbox_fallback()` helpers; new imports `select`, `shutil`, `termios`.

### Added — `/compress` context summarization
New REPL command. Asks the model to summarize everything except the system
prompt and the last `COMPRESS_KEEP=2` turns, then splices the summary in as a
single labeled user turn. Useful when the context window is filling up but
you want to keep working without `/reset`.
- Non-streaming summary call (spinner would be visual noise for a one-shot;
  one `model_dump` of the response is logged to `llamacpp.log` with a
  `_purpose: compress` marker so the summary is recoverable from the log).
- Confirmation prompt (skipped under `/yolo`).
- One-line stats footer: `compressed N turns → 1 summary (X chars), kept last K verbatim`.
- Header on the summary turn: `[Compressed context — N earlier turns
  summarized; last K turns kept verbatim]` so the model knows what it's
  reading on subsequent turns.
- Nothing-to-compress short-circuit: if the body has ≤ `COMPRESS_KEEP` turns,
  prints `nothing to compress (N turns in context)` and bails.
- Failure modes (APIConnectionError, generic API error, empty summary) leave
  `messages` untouched and print a one-line error — never half-compress.
- Tool-call turns and tool-result turns are rendered into the summary prompt
  with their content truncated to 2k chars each, so a giant `read_file`
  doesn't blow up the summarization call itself. Assistant tool-call turns
  are rendered as `[assistant] → tool_name(args)` for readability.
- New `compress(messages, keep=COMPRESS_KEEP)` function (returns
  `(kept_n, summarized_n, summary_chars)` or `None` on failure).
- New `COMPRESS_KEEP = 2` module constant.
- Banner and module docstring updated to mention `/compress`.
