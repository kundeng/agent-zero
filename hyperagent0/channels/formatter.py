"""Markdown → channel-specific formatting (spec 04, task 1.4).

Agent monologues land in :func:`format_for_channel` as
GitHub-flavored markdown. Each platform expects something a little
different:

* **Telegram** — a restricted HTML subset (``<b>``, ``<i>``, ``<code>``,
  ``<pre>``, ``<a>``). Other markup must be HTML-escaped to avoid the
  Bot API rejecting the message.
* **Slack** — Block Kit JSON for rich formatting. P1 ships a minimal
  placeholder that wraps the message in a single ``section`` block; the
  full Block Kit translation lands with the Slack adapter (P2 task 2.1).
* **Discord** — Discord's markdown is close to GitHub's; we passthrough
  with a small length guard.

Telegram-specific notes
-----------------------
Telegram's HTML mode disallows nested formatting and most HTML tags.
We deliberately implement only what the agent commonly emits
(headings → bold lines, fenced code blocks → ``<pre><code>``, inline
code → ``<code>``, bold/italic, links). Anything we don't understand
goes through HTML-escape so it renders as literal text rather than
breaking the message.
"""

from __future__ import annotations

import html
import re
from typing import Any

# Telegram caps Bot API ``sendMessage`` at 4096 chars. We chunk to a
# little below that to leave room for HTML overhead.
TELEGRAM_MAX_CHARS = 4000
DISCORD_MAX_CHARS = 1900  # Discord limit is 2000; leave headroom.


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def format_for_channel(text: str, channel_type: str) -> Any:
    """Convert ``text`` (markdown) to the markup ``channel_type`` expects.

    Return type varies by platform:
      * ``"telegram"`` → ``list[str]`` of HTML chunks
      * ``"slack"`` → ``dict`` with ``"text"`` and ``"blocks"`` keys
      * ``"discord"`` → ``list[str]`` of markdown chunks
      * anything else → the original string
    """

    if channel_type == "telegram":
        return _format_telegram(text)
    if channel_type == "slack":
        return _format_slack(text)
    if channel_type == "discord":
        return _format_discord(text)
    return text


# ---------------------------------------------------------------------------
# Telegram (HTML)
# ---------------------------------------------------------------------------


_FENCED_CODE_RE = re.compile(r"```([A-Za-z0-9_+-]*)\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def _format_telegram(text: str) -> list[str]:
    """Convert markdown to Telegram-flavored HTML.

    Strategy: pull fenced code blocks out first (they're sensitive to
    HTML-escaping rules), HTML-escape the surrounding prose, then
    re-inject inline markers (bold, italic, inline code, links).
    Finally chunk to Telegram's 4096-char message limit.
    """

    if not text:
        return [""]

    # Step 1: extract fenced code blocks and replace with sentinels so
    # they survive the inline-marker pass intact.
    placeholders: list[str] = []

    def _stash_code(match: re.Match[str]) -> str:
        lang = match.group(1) or ""
        body = match.group(2)
        escaped = html.escape(body)
        if lang:
            block = (
                f'<pre><code class="language-{html.escape(lang)}">{escaped}</code></pre>'
            )
        else:
            block = f"<pre>{escaped}</pre>"
        placeholders.append(block)
        return f"\x00CODEBLOCK{len(placeholders) - 1}\x00"

    stripped = _FENCED_CODE_RE.sub(_stash_code, text)

    # Step 2: HTML-escape everything else so stray ``<`` / ``>`` don't
    # break Telegram's parser.
    escaped = html.escape(stripped, quote=False)

    # Step 3: re-introduce inline markers. Order matters: bold before
    # italic (``**`` before ``*``), inline code before links.
    escaped = _INLINE_CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", escaped)
    escaped = _BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", escaped)
    escaped = _ITALIC_RE.sub(lambda m: f"<i>{m.group(1)}</i>", escaped)
    escaped = _LINK_RE.sub(
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', escaped
    )
    # Headings → bold lines (Telegram has no native heading).
    escaped = _HEADING_RE.sub(lambda m: f"<b>{m.group(2)}</b>", escaped)

    # Step 4: re-insert the stashed code blocks.
    def _restore(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        return placeholders[idx]

    final = re.sub(r"\x00CODEBLOCK(\d+)\x00", _restore, escaped)

    return _chunk(final, TELEGRAM_MAX_CHARS)


# ---------------------------------------------------------------------------
# Slack (Block Kit placeholder)
# ---------------------------------------------------------------------------


def _format_slack(text: str) -> dict[str, Any]:
    """P1 placeholder: a single ``section`` block with mrkdwn text.

    The P2 Slack adapter will expand this to honor code blocks and
    headings via richer Block Kit primitives.
    """

    safe = text or ""
    return {
        "text": safe,
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": safe},
            }
        ],
    }


# ---------------------------------------------------------------------------
# Discord (markdown passthrough)
# ---------------------------------------------------------------------------


def _format_discord(text: str) -> list[str]:
    """Passthrough with chunking. Discord understands markdown natively."""

    if not text:
        return [""]
    return _chunk(text, DISCORD_MAX_CHARS)


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def _chunk(text: str, max_len: int) -> list[str]:
    """Split ``text`` into ``max_len``-bounded chunks at natural breaks.

    Prefers blank lines, then newlines, then a hard split. Never returns
    an empty list — callers can iterate fearlessly.
    """

    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_len:
        window = remaining[:max_len]
        # Prefer breaking on a blank line, then any newline.
        split_at = window.rfind("\n\n")
        if split_at < max_len // 2:
            split_at = window.rfind("\n")
        if split_at < max_len // 2:
            split_at = max_len
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks
