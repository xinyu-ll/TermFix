"""Safe Markdown rendering helpers for TermFix popovers."""

from __future__ import annotations

import html
import re
from typing import Optional

_CODE_BLOCK_COPY_CSS = """\
    .code-block {
      position: relative;
      margin: 7px 0 10px;
      border-radius: 8px;
      background: #1d1d1b;
      overflow: hidden;
    }
    .code-actions {
      position: absolute;
      top: 7px;
      right: 7px;
      z-index: 1;
      display: flex;
      gap: 5px;
    }
    .code-action {
      height: 24px;
      min-width: 50px;
      padding: 0 8px;
      border: 1px solid rgba(255, 255, 255, 0.14);
      border-radius: 6px;
      background: rgba(255, 255, 255, 0.08);
      color: rgba(255, 255, 255, 0.78);
      font: 700 11px var(--sans, -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif);
      cursor: pointer;
    }
    .code-action:hover {
      background: rgba(255, 255, 255, 0.14);
      color: #ffffff;
    }
    .code-action.copied,
    .code-action.inserted {
      color: #ffffff;
      background: rgba(36, 165, 121, 0.5);
    }
    .code-action.needs-manual-copy,
    .code-action.error {
      color: #ffffff;
      background: rgba(191, 90, 90, 0.55);
    }
    .markdown pre {
      margin: 0;
      padding: 9px 11px;
      padding-right: 132px;
      overflow-x: auto;
      background: #1d1d1b;
    }
"""
_CODE_BLOCK_COPY_JS = """\
    async function copyText(text) {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        return;
      }
      const copyTarget = document.createElement("textarea");
      copyTarget.value = text;
      copyTarget.setAttribute("readonly", "");
      copyTarget.style.position = "fixed";
      copyTarget.style.left = "-9999px";
      document.body.appendChild(copyTarget);
      copyTarget.select();
      try {
        if (!document.execCommand("copy")) {
          throw new Error("Clipboard copy failed.");
        }
      } finally {
        document.body.removeChild(copyTarget);
      }
    }

    function selectCodeText(code) {
      const selection = window.getSelection ? window.getSelection() : null;
      const range = document.createRange ? document.createRange() : null;
      if (!selection || !range) {
        return false;
      }
      range.selectNodeContents(code);
      selection.removeAllRanges();
      selection.addRange(range);
      return true;
    }

    function resetCodeButton(button, label, className, delay) {
      setTimeout(() => {
        button.textContent = label;
        button.removeAttribute("title");
        if (className) {
          button.classList.remove(className);
        }
      }, delay);
    }

    async function insertCodeText(text) {
      if (typeof insertEndpoint !== "string" || !insertEndpoint) {
        throw new Error("Insert endpoint is unavailable.");
      }
      const response = await fetch(insertEndpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text })
      });
      const data = await response.json();
      if (!data.ok) {
        throw new Error(data.error || "Insert failed.");
      }
    }

    function handleCodeBlockCopy(event) {
      const target = event.target && event.target.closest ? event.target : null;
      const copyButton = target ? target.closest("[data-copy-code]") : null;
      const insertButton = target ? target.closest("[data-insert-code]") : null;
      const actionButton = copyButton || insertButton;
      if (!actionButton) {
        return false;
      }

      const block = actionButton.closest(".code-block");
      const code = block ? block.querySelector("code") : null;
      if (!code) {
        return true;
      }

      if (insertButton) {
        insertCodeText(code.textContent || "").then(() => {
          insertButton.textContent = "Inserted";
          insertButton.classList.add("inserted");
          resetCodeButton(insertButton, "Insert", "inserted", 1400);
        }).catch((error) => {
          const message = error.message || "Insert failed.";
          const blocked = message.startsWith("Insert blocked:");
          const className = blocked ? "needs-manual-copy" : "error";
          insertButton.textContent = blocked ? "Use Copy" : "Insert failed";
          insertButton.title = message;
          insertButton.classList.add(className);
          resetCodeButton(insertButton, "Insert", className, 2600);
        });
        return true;
      }

      copyText(code.textContent || "").then(() => {
        copyButton.textContent = "Copied";
        copyButton.classList.add("copied");
        resetCodeButton(copyButton, "Copy", "copied", 1200);
      }).catch(() => {
        const selected = selectCodeText(code);
        copyButton.textContent = selected ? "Selected - Cmd+C" : "Select code";
        copyButton.classList.remove("copied");
        copyButton.classList.add("needs-manual-copy");
        resetCodeButton(copyButton, "Copy", "needs-manual-copy", 2800);
      });
      return true;
    }
"""
_CODE_BLOCK_ACTIONS_HTML = (
    '<div class="code-actions">'
    '<button class="code-action copy-code" type="button" data-copy-code>Copy</button>'
    '<button class="code-action insert-code" type="button" data-insert-code>Insert</button>'
    "</div>"
)


def _compact_text(text: str) -> str:
    """Collapse markdown-ish text into a single plain preview line."""
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        cleaned = line.strip()
        cleaned = re.sub(r"^>+\s*", "", cleaned)
        cleaned = re.sub(r"^#{1,6}\s*", "", cleaned)
        cleaned = re.sub(r"^(```+|~~~+)\s*\w*\s*$", "", cleaned)
        cleaned = re.sub(r"^[-+*]\s+", "", cleaned)
        cleaned = re.sub(r"^\d+[.)]\s+", "", cleaned)
        cleaned = cleaned.replace("`", "")
        cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
        cleaned = re.sub(r"__(.*?)__", r"\1", cleaned)
        if cleaned:
            cleaned_lines.append(cleaned)
    cleaned = " ".join(cleaned_lines)
    return " ".join(cleaned.split())


def _plain_text_to_html(text: str) -> str:
    """Render user-authored prompt text without interpreting Markdown."""
    return html.escape(text).replace("\n", "<br>")


def _markdown_to_html(markdown: str) -> str:
    """Render a small, safe Markdown subset to HTML."""
    lines = markdown.splitlines()
    blocks: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    list_tag: Optional[str] = None
    in_code = False
    code_lines: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            blocks.append(f"<p>{_inline_markdown(' '.join(paragraph))}</p>")
            paragraph = []

    def flush_list() -> None:
        nonlocal list_items, list_tag
        if list_items:
            tag = list_tag or "ul"
            blocks.append(
                f"<{tag}>"
                + "".join(f"<li>{item}</li>" for item in list_items)
                + f"</{tag}>"
            )
            list_items = []
            list_tag = None

    def append_list_item(tag: str, item: str) -> None:
        nonlocal list_tag
        if list_tag is not None and list_tag != tag:
            flush_list()
        list_tag = tag
        list_items.append(item)

    def code_block_html(lines: list[str]) -> str:
        code = html.escape("\n".join(lines))
        return (
            '<div class="code-block">'
            f"{_CODE_BLOCK_ACTIONS_HTML}"
            f"<pre><code>{code}</code></pre>"
            "</div>"
        )

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("```"):
            if in_code:
                blocks.append(code_block_html(code_lines))
                code_lines = []
                in_code = False
            else:
                flush_paragraph()
                flush_list()
                in_code = True
            continue

        if in_code:
            code_lines.append(line)
            continue

        if not stripped:
            flush_paragraph()
            flush_list()
            continue

        if stripped.startswith("### "):
            flush_paragraph()
            flush_list()
            blocks.append(f"<h3>{html.escape(stripped[4:].strip())}</h3>")
        elif stripped.startswith("## "):
            flush_paragraph()
            flush_list()
            blocks.append(f"<h2>{html.escape(stripped[3:].strip())}</h2>")
        elif stripped.startswith("# "):
            flush_paragraph()
            flush_list()
            blocks.append(f"<h1>{html.escape(stripped[2:].strip())}</h1>")
        elif stripped.startswith(("- ", "* ")):
            flush_paragraph()
            append_list_item("ul", _inline_markdown(stripped[2:].strip()))
        elif re.match(r"^\d+[.)]\s+", stripped):
            flush_paragraph()
            append_list_item(
                "ol", _inline_markdown(re.sub(r"^\d+[.)]\s+", "", stripped).strip())
            )
        else:
            flush_list()
            paragraph.append(stripped)

    if in_code:
        blocks.append(code_block_html(code_lines))
    flush_paragraph()
    flush_list()

    return "\n".join(blocks)


def _inline_markdown(text: str) -> str:
    """Render inline code and bold markers."""
    parts = text.split("`")
    rendered: list[str] = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            rendered.append(f"<code>{html.escape(part)}</code>")
        else:
            rendered.append(_inline_bold_to_html(part))
    return "".join(rendered)


def _inline_bold_to_html(text: str) -> str:
    """Render non-empty **bold** spans while preserving literal asterisks."""
    rendered: list[str] = []
    pos = 0

    while pos < len(text):
        start = text.find("**", pos)
        if start == -1:
            rendered.append(html.escape(text[pos:]))
            break

        end = text.find("**", start + 2)
        if end == -1:
            rendered.append(html.escape(text[pos:]))
            break

        content = text[start + 2 : end]
        rendered.append(html.escape(text[pos:start]))
        if content.strip():
            rendered.append(f"<strong>{html.escape(content)}</strong>")
        else:
            rendered.append(html.escape(text[start : end + 2]))
        pos = end + 2

    return "".join(rendered)
