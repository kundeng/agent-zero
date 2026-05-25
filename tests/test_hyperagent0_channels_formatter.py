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


def test_slack_plain_text_passes_through():
    payload = format_for_channel("hello slack", "slack")
    assert isinstance(payload, dict)
    assert payload["text"] == "hello slack"
    assert isinstance(payload["blocks"], list) and payload["blocks"]
    block = payload["blocks"][0]
    assert block["type"] == "section"
    assert block["text"]["type"] == "mrkdwn"
    assert block["text"]["text"] == "hello slack"


def test_slack_bold_double_asterisk_collapses_to_single():
    """Markdown ``**bold**`` must become mrkdwn ``*bold*`` (single asterisk)."""

    text = "Hello **world**, this is **bold**."
    payload = format_for_channel(text, "slack")
    assert "*world*" in payload["text"]
    assert "*bold*" in payload["text"]
    assert "**" not in payload["text"]


def test_slack_italic_single_asterisk_becomes_underscore():
    """Markdown ``*italic*`` must become mrkdwn ``_italic_``."""

    text = "an *italic* word"
    payload = format_for_channel(text, "slack")
    assert "_italic_" in payload["text"]
    # No bare asterisks left (would render literally in Slack).
    assert "*italic*" not in payload["text"]


def test_slack_bold_before_italic_does_not_fragment():
    """``**foo**`` must NOT get caught by the italic pass mid-conversion."""

    text = "**both** and *one*"
    payload = format_for_channel(text, "slack")
    assert "*both*" in payload["text"]
    assert "_one_" in payload["text"]


def test_slack_headings_become_bold():
    """``# Heading`` (any level) becomes ``*Heading*`` since Slack has no headings.

    Inline emphasis inside a heading is stripped — mrkdwn can't render
    nested bold and ``*outer *inner**`` would look broken.
    """

    text = "# H1\n## H2 with **strong**\n### H3"
    payload = format_for_channel(text, "slack")
    out = payload["text"]
    assert "*H1*" in out
    assert "*H2 with strong*" in out  # ** stripped from inside the heading
    assert "*H3*" in out
    # No literal '##' should leak through.
    assert "##" not in out


def test_slack_table_becomes_code_block():
    """Markdown tables render as literal ``|`` text in mrkdwn — wrap in a code block."""

    text = (
        "Here's a table:\n\n"
        "| Property | Value |\n"
        "|----------|-------|\n"
        "| Name | Agent Zero |\n"
        "| Model | sonnet-4-6 |\n"
        "\nAfter the table."
    )
    payload = format_for_channel(text, "slack")
    out = payload["text"]
    # Code-fenced
    assert "```" in out
    # Header + rows survive inside the code block (pipe-aligned).
    assert "Property | Value" in out
    assert "Name | Agent Zero" in out
    assert "Model | sonnet-4-6" in out
    # Separator row stripped
    assert "----------|-------" not in out
    # Surrounding prose preserved
    assert "Here's a table" in out
    assert "After the table." in out


def test_slack_code_fence_content_not_translated():
    """Markdown inside a fenced code block must NOT be touched."""

    text = "Look:\n```\n**this stays as-is**\n## not a heading\n```\nDone."
    payload = format_for_channel(text, "slack")
    out = payload["text"]
    # Inside the code block, the markup is preserved literally.
    assert "**this stays as-is**" in out
    assert "## not a heading" in out


def test_slack_empty_input():
    payload = format_for_channel("", "slack")
    assert payload["text"] == ""
    assert payload["blocks"][0]["text"]["text"] == ""


def test_slack_emoji_underscores_preserved_in_heading():
    """``## :file_folder: Title`` must NOT strip the underscores from the
    emoji shortcode — Slack needs them to render the emoji."""

    payload = format_for_channel("## :file_folder: Current Project", "slack")
    assert "*:file_folder: Current Project*" in payload["text"]


def test_slack_italic_inside_heading_preserved():
    """``*outer _italic_*`` IS legal mrkdwn (bold containing italic).
    The heading body's ``_`` markers must survive."""

    payload = format_for_channel("## Heading with _slant_", "slack")
    assert "*Heading with _slant_*" in payload["text"]


def test_slack_table_strips_bold_markers_in_cells():
    """Inside the code-block fallback for tables, ``**bold**`` cell
    content shouldn't show literal asterisks (code blocks render
    everything as monospaced text — emphasis markup is noise)."""

    text = (
        "| Property | Value |\n"
        "|---|---|\n"
        "| **Name** | Agent Zero |\n"
        "| **Path** | snake_case_keeps |"
    )
    payload = format_for_channel(text, "slack")
    out = payload["text"]
    assert "Name | Agent Zero" in out
    # ** stripped:
    assert "**Name**" not in out
    # internal underscores in identifier-like text preserved:
    assert "snake_case_keeps" in out


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
