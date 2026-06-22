#!/usr/bin/env python3
"""Guard the README's "~625 tokens on a bare hey" claim.

The README advertises that a bare `hey` costs about 625 tokens of initial
context. That floor is the whole reason minion exists — it's two orders of
magnitude under the harnesses that spend 20K-50K before you've typed
anything — so this test watches it. If a new tool or a fatter SYSTEM bloats
the payload, we want to know before the README quietly becomes a lie.

WHAT WE MEASURE (the part we control and send to the API):
  - SYSTEM          the system prompt
  - TOOLS           the 5-function tool schema, JSON-encoded
  - "hey"           a minimal first user turn
That's ~574 tokens today (cl100k_base). The server's chat template then
adds ~50 of framing (role tags, <|im_start|>/<|im_end|>, per-tool wrappers)
that varies by backend and ISN'T part of our payload, so we don't measure
it here. The README's ~625 = our ~574 + that ~50.

THRESHOLDS (on the content we control, ~574 today):
  - 800  ->  WARNING   a sixth tool or much fatter descriptions; check the README
  - 1000 ->  FAIL      roughly doubled; the README's claim no longer holds

TOKENIZER: prefers tiktoken (cl100k_base) when installed for a real count;
falls back to a conservative chars/3.5 estimate otherwise so the guard still
runs with zero extra dependencies. minion's only runtime dep is `openai`,
and we don't add one for a test. The fallback over-counts ~7% vs cl100k on
code/JSON, which is the safe direction for a guard — it trips slightly
early, never late.
"""
import json
import os
import sys
import tempfile
import warnings

# Keep minion from touching ~/.minion — same convention as the other tests,
# even though this one never writes a session.
_tmp = tempfile.mkdtemp(prefix="minion-test-")
os.environ["MINION_SESSIONS_DIR"] = _tmp
os.environ["MINION_HOME"] = _tmp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import minion as m  # noqa: E402

# Thresholds on the content token count (SYSTEM + TOOLS JSON + "hey").
# Today this is ~574; the README's ~625 adds ~50 of server-side framing.
WARN_TOKENS = 800   # soft: warn (don't fail) — check the README still holds
FAIL_TOKENS = 1000  # hard: the floor has roughly doubled; README is fiction


def _count_tokens(text):
    """Token count for a string.

    Returns (count, method). Uses tiktoken's cl100k_base when available
    (the closest thing to a shared standard across served models); falls
    back to a conservative chars/3.5 estimate so the test runs with no
    extra deps. The fallback over-counts on code/JSON, which is the safe
    direction for a bloat guard.
    """
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text)), "tiktoken"
    except Exception:
        return max(1, round(len(text) / 3.5)), "chars/3.5"


def test_initial_context_floor():
    # The exact payload a fresh "hey" turn sends: system prompt + 5 tool
    # schemas + a one-token user message. FINAL_ANSWER_TOOL is intentionally
    # excluded — it's only injected on a forced-final retry (minion.py:1847),
    # not on turn 1, so it's not part of the README's ~625.
    system_tokens, method = _count_tokens(m.SYSTEM)
    tools_tokens, _ = _count_tokens(json.dumps(m.TOOLS))
    user_tokens, _ = _count_tokens("hey")
    total = system_tokens + tools_tokens + user_tokens

    breakdown = (
        f"initial context content = {total} tokens ({method}): "
        f"SYSTEM {system_tokens} + TOOLS {tools_tokens} "
        f"({len(m.TOOLS)} funcs) + 'hey' {user_tokens}"
    )
    print(breakdown)

    # Sanity: a broken payload (empty SYSTEM, TOOLS clobbered to []) would
    # make a token-floor test meaningless by passing on a near-zero count.
    assert m.SYSTEM.strip(), "SYSTEM prompt is empty"
    assert len(m.TOOLS) >= 1, "TOOLS is empty — nothing to guard"

    # Hard ceiling: a ~doubled payload means the README's '~625' is fiction.
    assert total < FAIL_TOKENS, (
        f"initial context content is {total} tokens "
        f"(>= {FAIL_TOKENS}) — the README's ~625 claim no longer holds. "
        f"{breakdown}"
    )

    # Soft ceiling: warn (don't fail) past WARN_TOKENS. pytest surfaces
    # warnings in its end-of-run summary, so this is visible without
    # failing the suite.
    if total >= WARN_TOKENS:
        warnings.warn(
            f"initial context content is {total} tokens (>= {WARN_TOKENS}). "
            f"The README claims ~625; a sixth tool or fatter descriptions "
            f"may have pushed the floor up. {breakdown}",
            stacklevel=2,
        )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v", "-s"]))
