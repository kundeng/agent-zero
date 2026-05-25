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
    """Translate GitHub-flavored markdown to Slack mrkdwn.

    Slack's mrkdwn supports a small, opinionated subset:

    * ``*bold*`` (single asterisk, NOT ``**double**``)
    * ``_italic_`` (underscore, NOT ``*single*``)
    * ``~strike~``, ```` `code` ```` , triple-backtick code fences
    * Bulleted / numbered lists are passed through as plain text
    * No native headings, no native tables

    So the translator:

    1. Stashes fenced code blocks behind sentinels so their contents
       aren't touched.
    2. Detects markdown tables (``| col | col |`` with a ``|---|---|``
       separator row) and rewrites them as a fenced code block so they
       render monospaced and column-aligned in Slack.
    3. Converts ``# Heading`` (any level) to a ``*Heading*`` line.
    4. Converts ``**bold**`` → ``*bold*`` BEFORE single-asterisk italic
       so ``**`` doesn't fragment into ``*<italic>*``.
    5. Converts remaining ``*italic*`` → ``_italic_``.
    6. Restores the stashed code blocks.

    The result is wrapped in a Block Kit ``section`` with type
    ``mrkdwn``. Slack's 3000-char per-section cap could be exceeded by
    long agent replies, but that's an edge case we'll handle when it
    bites.
    """

    if not text:
        return {"text": "", "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": ""}}]}

    # Step 1: stash code fences so their contents (which may contain
    # ``**`` / ``*`` / ``|`` chars) survive the substitution passes.
    code_stash: list[str] = []

    def _stash_code(match: re.Match[str]) -> str:
        lang = match.group(1) or ""
        body = match.group(2)
        # Slack mrkdwn doesn't render language hints; just preserve the
        # body inside triple backticks.
        block = f"```{body}```" if not lang else f"```\n{body}\n```"
        code_stash.append(block)
        return f"\x00SLACKCODE{len(code_stash) - 1}\x00"

    working = _FENCED_CODE_RE.sub(_stash_code, text)

    # Step 2: convert markdown tables to code-block tables.
    working = _slack_tables_to_codeblocks(working, code_stash)

    # Step 3: italic FIRST (``*x*`` → ``_x_``). The italic regex uses
    # negative lookaround so ``**bold**`` is skipped. Doing italic
    # before bold prevents bold's resulting single-asterisk pair from
    # being misread as italic in step 4.
    working = _ITALIC_RE.sub(lambda m: f"_{m.group(1)}_", working)

    # Step 4: bold (``**x**`` → ``*x*``).
    working = _BOLD_RE.sub(lambda m: f"*{m.group(1)}*", working)

    # Step 5: headings → ``*bold*`` lines. Slack mrkdwn can't render
    # nested bold, so inner ``*`` markers inside a heading get stripped
    # (otherwise we'd emit ``*outer *inner**`` which renders broken).
    # Underscores are NOT stripped — Slack emoji shortcodes like
    # ``:file_folder:`` use underscores legitimately, and italic-inside-
    # bold (``*outer _italic_*``) is mrkdwn-legal.
    def _heading_to_bold(m: re.Match[str]) -> str:
        body = m.group(2).strip().replace("*", "")
        return f"*{body}*"

    working = _HEADING_RE.sub(_heading_to_bold, working)

    # Step 6: restore stashed code blocks.
    def _restore(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        return code_stash[idx]

    final = re.sub(r"\x00SLACKCODE(\d+)\x00", _restore, working)

    return {
        "text": final,
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": final},
            }
        ],
    }


# Markdown table = at least 2 lines:
#   | h1 | h2 |
#   |----|----|
#   | r1 | r2 |
# Detect by requiring a separator row.
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")


def _slack_tables_to_codeblocks(text: str, code_stash: list[str]) -> str:
    """Find markdown tables and replace each with a fenced code block.

    The code block contains the original row text (pipes and all), so
    columns align visually in monospace. The new block is stashed into
    ``code_stash`` immediately so subsequent inline-marker passes don't
    rewrite its contents.
    """

    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        # A table candidate is a header row (contains '|') followed by
        # a separator row of dashes-and-pipes.
        if "|" in lines[i] and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1]):
            header = lines[i]
            j = i + 2
            rows: list[str] = []
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                rows.append(lines[j])
                j += 1
            # Build the code block. Strip a single leading/trailing pipe
            # and surrounding whitespace from each cell for cleaner display.
            table_lines = [header] + rows
            normalized = "\n".join(_normalize_table_row(r) for r in table_lines)
            code_stash.append(f"```\n{normalized}\n```")
            out.append(f"\x00SLACKCODE{len(code_stash) - 1}\x00")
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def _normalize_table_row(row: str) -> str:
    """Trim ``| a | b |`` → ``a | b`` with consistent spacing.

    Strips markdown emphasis markers (``**`` and lone ``*``) from each
    cell — they don't render inside Slack code blocks, so leaving them
    in would just print literal asterisks.
    """

    cells = [c.strip() for c in row.split("|")]
    # Split produces empty strings for leading/trailing pipes; drop them.
    cells = [c for c in cells if c != ""] or [""]
    cells = [_strip_inline_emphasis(c) for c in cells]
    return " | ".join(cells)


_EMPH_STRIP_RE = re.compile(r"\*+|(?<![A-Za-z0-9_])_+|_+(?![A-Za-z0-9_])")


def _strip_inline_emphasis(s: str) -> str:
    """Remove ``*`` markers and standalone ``_`` markers from a cell.

    ``_`` only inside word characters (e.g. ``snake_case``) is left
    alone; ``_italic_`` markers around a word are stripped. ``*`` is
    always stripped since it never has a non-emphasis use inline.
    """

    return _EMPH_STRIP_RE.sub("", s)


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
