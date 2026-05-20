"""Tests for ``hyperagent0.channels.formatter``.

We exercise the public entry point with representative synthetic
markdown samples — no live API calls. Covers:
  * Telegram HTML conversion (fenced code, inline code, bold, italic,
    links, headings, escaping).
  * Slack Block Kit placeholder shape.
  * Discord passthrough.
  * Chunking behavior at the platform limits.
"""

from __future__ import annotations

import pytest

from hyperagent0.channels.formatter import (
    DISCORD_MAX_CHARS,
    TELEGRAM_MAX_CHARS,
    format_for_channel,
)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def test_telegram_plain_text_passthrough():
    chunks = format_for_channel("hello world", "telegram")
    assert chunks == ["hello world"]


def test_telegram_escapes_angle_brackets():
    chunks = format_for_channel("a < b > c", "telegram")
    assert chunks == ["a &lt; b &gt; c"]


def test_telegram_bold_and_italic():
    chunks = format_for_channel("**bold** and *italic*", "telegram")
    assert chunks == ["<b>bold</b> and <i>italic</i>"]


def test_telegram_inline_code():
    chunks = format_for_channel("use `print(x)` here", "telegram")
    assert chunks == ["use <code>print(x)</code> here"]


def test_telegram_fenced_code_block_with_language():
    chunks = format_for_channel("```python\nprint('hi')\n```", "telegram")
    assert len(chunks) == 1
    out = chunks[0]
    assert "<pre><code class=\"language-python\">" in out
    assert "print(&#x27;hi&#x27;)" in out or "print('hi')" in out
    assert "</code></pre>" in out


def test_telegram_fenced_code_block_no_language():
    chunks = format_for_channel("```\nabc\n```", "telegram")
    assert chunks == ["<pre>abc\n</pre>"]


def test_telegram_link():
    chunks = format_for_channel("see [Anthropic](https://anthropic.com)", "telegram")
    assert chunks == ['see <a href="https://anthropic.com">Anthropic</a>']


def test_telegram_heading_becomes_bold():
    chunks = format_for_channel("# Heading", "telegram")
    assert chunks == ["<b>Heading</b>"]


def test_telegram_code_block_html_inside_is_escaped():
    chunks = format_for_channel("```\n<script>x</script>\n```", "telegram")
    assert chunks == ["<pre>&lt;script&gt;x&lt;/script&gt;\n</pre>"]


def test_telegram_long_message_is_chunked():
    body = ("paragraph one.\n\n" + ("x" * 1000 + "\n\n") * 5).strip()
    chunks = format_for_channel(body, "telegram")
    assert all(len(c) <= TELEGRAM_MAX_CHARS for c in chunks)
    # Reassembled text contains all input characters (after escaping is identity here).
    joined = "\n\n".join(chunks)
    assert "paragraph one" in joined


def test_telegram_empty_input_returns_one_empty_chunk():
    assert format_for_channel("", "telegram") == [""]


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


def test_slack_placeholder_shape():
    payload = format_for_channel("hello slack", "slack")
    assert isinstance(payload, dict)
    assert payload["text"] == "hello slack"
    assert isinstance(payload["blocks"], list) and payload["blocks"]
    block = payload["blocks"][0]
    assert block["type"] == "section"
    assert block["text"]["type"] == "mrkdwn"
    assert block["text"]["text"] == "hello slack"


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------


def test_discord_passthrough_short():
    chunks = format_for_channel("**bold** here", "discord")
    assert chunks == ["**bold** here"]


def test_discord_chunks_at_limit():
    body = "a" * (DISCORD_MAX_CHARS + 50)
    chunks = format_for_channel(body, "discord")
    assert all(len(c) <= DISCORD_MAX_CHARS for c in chunks)
    assert "".join(chunks) == body


# ---------------------------------------------------------------------------
# Unknown channel
# ---------------------------------------------------------------------------


def test_unknown_channel_passthrough():
    assert format_for_channel("hi", "imaginary") == "hi"
