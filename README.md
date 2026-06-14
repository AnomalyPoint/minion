# minion

![minion](minion.png)

A single-file, single-dependency terminal coding agent. One Python file, one
`pip install openai`, no framework, no bloat. Point it at any
OpenAI-compatible endpoint — a local llama.cpp / vLLM / SGLang server, or a
remote API like Z.ai or OpenAI itself — and start chatting with an agent that
can read, write, edit, and run shell commands in your project.

The whole thing is one file (`minion.py`, ~1500 lines). No TUI framework, no
plugin system, no config file format. It reads from environment variables (and
`~/.env`), talks directly to the OpenAI SDK, and uses raw terminal escapes for
its interface. If you want to understand or modify how it works, you read one
file. That's the whole pitch.

It's built to survive the rough edges of self-hosted and open models: if the
server doesn't support native tool-calling, it falls back to parsing
`<tool_call>…</tool_call>` tags out of the model's text. If the server streams a
separate `reasoning_content` field (MiniMax-M3, DeepSeek-R1, etc.), it renders
that as a dim "thinking" block above the answer. It degrades gracefully rather
than demanding a perfect server.

## Quick start

```
pip install openai
export MINION_BASE_URL=http://localhost:8080/v1
export MINION_MODEL=your-model-name
export MINION_API_KEY=sk-noop        # any string; local servers ignore it
python minion.py
```

If `MINION_MODEL` is unset, minion asks the server what it's serving.

## Install as a command

If you'd rather have a `minion` command on your `$PATH`, install from this
repo:

```
pip install -e .
```

That registers a `minion` console script pointing at this checkout — edits you
make here are picked up immediately. Use `pip install .` (no `-e`) for a
non-editable install instead.

## Configuration

minion reads configuration from environment variables, and automatically loads
`~/.env` at startup (so you don't have to export things in every terminal).

### Single source (simple)

```
MINION_BASE_URL=http://localhost:8080/v1
MINION_MODEL=your-model-name
MINION_API_KEY=sk-noop
```

### Multiple sources

Define named endpoints and switch between them at runtime:

```
MINION_SOURCES=local,zai

MINION_SOURCE_LOCAL_BASE_URL=http://localhost:8080/v1
MINION_SOURCE_LOCAL_API_KEY=sk-noop

MINION_SOURCE_ZAI_BASE_URL=https://api.z.ai/api/paas/v4
MINION_SOURCE_ZAI_API_KEY=$zai_test         # $name = look up a key from env / ~/.env
MINION_SOURCE_ZAI_MODEL=glm-x-preview
```

See [`sources.example.env`](sources.example.env) for a full annotated example.
Switch at runtime with `/source [name]`. The conversation context is preserved
across switches (use `/reset` if you want a clean slate).

### Flags

| flag                          | what it does                                              |
| ----------------------------- | -------------------------------------------------------- |
| `--yolo`                      | start in never-prompt mode (auto-approve everything)      |
| `--approval <low\|medium\|high>` | start with a non-default approval threshold            |
| `--source <name>`             | start on a specific source                                |
| `--no-scroll-bottom`          | disable the pinned status bar / scroll-region setup       |

## Commands

| command             | what it does                                            |
| ------------------- | ------------------------------------------------------ |
| `/source [name]`    | list sources or switch to one (context preserved)       |
| `/yolo`             | toggle auto-approve for writes and bash                 |
| `/approval [level]` | show or set risk threshold (`low`/`medium`/`high`/`yolo`) |
| `/compress`         | summarize older turns into one, keep last 2 verbatim     |
| `/compact`          | alias for `/compress`                                    |
| `/reset`            | clear conversation, keep system prompt                   |
| `/quit`             | exit                                                     |

## Input

The prompt is a multi-line editor with a framed box:

- **Enter** submits; **Alt+Enter** or **Ctrl+J** inserts a newline
- **Paste** (bracketed-paste) inserts text verbatim, including newlines
- **Up/Down** navigate history; **Left/Right** move within the line
- **Home/End** jump to line start/end; **Ctrl+U** clears; **Ctrl+C** cancels
- Long lines word-wrap inside the box

Falls back to plain `input()` when stdin/stdout isn't a TTY.

## Interrupting the model

Press **Esc** at any point during generation to stop the model and drop back to
the prompt. The stream is closed, partial output is discarded, and a synthetic
"you were interrupted" note is appended to context so the model knows what
happened. In-flight tool calls (e.g. a running `run_bash`) are **not**
cancelled — they run to completion. Ctrl+C kills the whole process if you need
a hard stop.

## Approval modes

Every write / edit / bash call is risk-classified by a single cheap model call
before it runs. Levels: `low` (read-only or trivially reversible), `medium`
(modifies state but contained/reversible), `high` (destructive, hard to
reverse, or broad scope). The threshold is the minimum level that requires
approval:

| flag                    | prompts at          | auto-allows       |
| ----------------------- | ------------------- | ----------------- |
| _(default — low)_       | low + medium + high | —                 |
| `--approval medium`     | medium + high       | low               |
| `--approval high`       | high only           | low + medium      |
| `--yolo`                | _(never)_           | everything        |

The risk assessment is shown in brackets next to the prompt, so you have
context for the decision:

```
allow rm -rf /tmp/foo? [risk: HIGH — recursive force delete in /tmp] [Y/n/esc]
```

At the prompt, press:

- **Y** (or Enter) to approve
- **n** to deny — the model is told the action was refused and can adapt
- **Esc** to stop the turn and drop back to the chat input so you can add more
  guidance. The escaped action is recorded as cancelled; if the model emitted
  multiple tool calls, any remaining ones are marked skipped so the context
  stays valid. A note is left so the model knows you pulled it back.

Auto-allowed calls print a one-liner:

```
↳ auto-allow [low] ls -la (read-only listing)
```

YOLO mode skips the classifier entirely. If the classifier call fails or returns
garbage, the action defaults to `high` (always prompts) so it errs on the side
of asking.

## Reasoning-loop guard

Reasoning models sometimes spin in place — they keep saying "let me implement…"
without actually doing anything. minion counts those "ready to act" phrases
during the reasoning phase and, after `MINION_REASONING_LOOP_SIGNALS` (default
**10**) of them, cuts the stream and nudges the model to take a concrete action.
Set the env var to `0` to disable, or lower it (e.g. `5`) for a more aggressive
cut.

## Tools

| tool        | args                  | notes                           |
| ----------- | --------------------- | ------------------------------- |
| `read_file` | `path`                |                                 |
| `write_file`| `path`, `content`     | overwrites; requires confirmation |
| `edit_file` | `path`, `old`, `new`  | `old` must match exactly once   |
| `list_dir`  | `path`                |                                 |
| `run_bash`  | `command`             | requires confirmation           |

## Status bar

When running in a terminal, minion pins a one-line status bar at the top of the
screen (model name, source, approval mode, endpoint, available commands) using a
DECSTBM scroll region — the same primitive tmux and vim use for their status
lines. The chat output scrolls in the region below it. Pass
`--no-scroll-bottom` to disable.

> **Note:** The pinned status bar relies on terminal scroll-region support.
> This works in most terminals (and under tmux), but some multiplexers like
> Zellij don't fully implement it — in those, the bar may scroll away after
> enough output. Not a bug in minion; just a terminal-emulation gap.

## Log

Every request and streamed SSE chunk is appended to `llamacpp.log` next to the
script (JSONL). Useful for debugging what the model actually saw and returned.
