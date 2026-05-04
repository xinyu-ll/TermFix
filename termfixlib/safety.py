"""Small safety helpers for model context and terminal insertion."""

from __future__ import annotations

import re

REDACTION_PLACEHOLDER = "[REDACTED]"
REDACTION_STATUS_TEXT = "Context redaction active"

_SECRET_LABEL = (
    r"(?:"
    r"api\s*key|"
    r"openai[_\s-]*api[_\s-]*key|"
    r"password|passwd|"
    r"secret|token|"
    r"[A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|PASSWD)[A-Z0-9_]*"
    r")"
)

_BEARER_TOKEN_RE = re.compile(r"(?i)\b(Bearer\s+)([A-Za-z0-9._~+/=-]{6,})")
_AUTHORIZATION_RE = re.compile(
    r"(?i)\b(Authorization\s*[:=]\s*(?:Bearer\s+)?)([^\s'\";&|]+)"
)
_DOUBLE_QUOTED_LABEL_RE = re.compile(
    rf"(?i)(\b{_SECRET_LABEL}\b\s*[:=]\s*\")([^\"]+)(\")"
)
_SINGLE_QUOTED_LABEL_RE = re.compile(rf"(?i)(\b{_SECRET_LABEL}\b\s*[:=]\s*')([^']+)(')")
_UNQUOTED_LABEL_RE = re.compile(
    rf"(?i)(\b{_SECRET_LABEL}\b\s*[:=]\s*)(?!['\"])([^\s'\";&|]+)"
)
_DOUBLE_QUOTED_CLI_SECRET_RE = re.compile(
    r"(?i)((?:^|[\s;&|])--(?:api-key|token|secret|password|passwd)"
    r"\s*(?:=|\s+)\s*\")([^\"]+)(\")"
)
_SINGLE_QUOTED_CLI_SECRET_RE = re.compile(
    r"(?i)((?:^|[\s;&|])--(?:api-key|token|secret|password|passwd)"
    r"\s*(?:=|\s+)\s*')([^']+)(')"
)
_UNQUOTED_CLI_SECRET_RE = re.compile(
    r"(?i)((?:^|[\s;&|])--(?:api-key|token|secret|password|passwd)"
    r"\s*(?:=|\s+)\s*)(?!['\"])([^\s'\";&|]+)"
)
_OPENAI_KEY_RE = re.compile(r"\b(sk-(?:proj-)?[A-Za-z0-9_-]{16,})\b")

_DANGEROUS_COMMANDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)(?:^|[;&|]\s*)(?:sudo\s+)?rm\s+-[^\n;&|]*[rf]"
            r"[^\n;&|]*[rf][^\n;&|]*\s+/(?:\s|$)"
        ),
        "rm -rf / is destructive.",
    ),
    (
        re.compile(r"(?i)(?:^|[;&|]\s*)(?:sudo\s+)?git\s+reset\s+--hard(?:\s|$)"),
        "git reset --hard can discard work.",
    ),
    (
        re.compile(r"(?i)(?:^|[;&|]\s*)(?:sudo\s+)?chmod\s+-R\s+777(?:\s|$)"),
        "chmod -R 777 is unsafe.",
    ),
)


class UnsafeInsertError(ValueError):
    """Raised when a code block is too risky to insert into a terminal."""


def redact_text(value) -> str:  # noqa: ANN001 - terminal payloads may be non-str.
    """Redact common secret shapes from terminal text."""
    text = str(value or "")
    if not text:
        return ""

    text = _AUTHORIZATION_RE.sub(rf"\1{REDACTION_PLACEHOLDER}", text)
    text = _BEARER_TOKEN_RE.sub(rf"\1{REDACTION_PLACEHOLDER}", text)
    text = _DOUBLE_QUOTED_LABEL_RE.sub(rf"\1{REDACTION_PLACEHOLDER}\3", text)
    text = _SINGLE_QUOTED_LABEL_RE.sub(rf"\1{REDACTION_PLACEHOLDER}\3", text)
    text = _UNQUOTED_LABEL_RE.sub(rf"\1{REDACTION_PLACEHOLDER}", text)
    text = _DOUBLE_QUOTED_CLI_SECRET_RE.sub(rf"\1{REDACTION_PLACEHOLDER}\3", text)
    text = _SINGLE_QUOTED_CLI_SECRET_RE.sub(rf"\1{REDACTION_PLACEHOLDER}\3", text)
    text = _UNQUOTED_CLI_SECRET_RE.sub(rf"\1{REDACTION_PLACEHOLDER}", text)
    text = _OPENAI_KEY_RE.sub(REDACTION_PLACEHOLDER, text)
    return text


def redacted_terminal_context(context: dict) -> dict:
    """Return a shallow copy with command/output redacted for model submission."""
    if not isinstance(context, dict):
        return {}
    redacted = dict(context)
    redacted["command"] = redact_text(redacted.get("command", ""))
    redacted["terminal_output"] = redact_text(redacted.get("terminal_output", ""))
    return redacted


def prepare_insert_text(text: str) -> str:
    """Return terminal insert text after blocking risky commands and final enters."""
    prepared = str(text or "").rstrip("\r\n")
    reason = unsafe_insert_reason(prepared)
    if reason:
        raise UnsafeInsertError(f"Insert blocked: {reason} Use Copy and review it manually.")
    return prepared


def unsafe_insert_reason(text: str) -> str:
    """Return a human-readable reason when *text* is unsafe to insert."""
    for line in str(text or "").splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        for pattern, reason in _DANGEROUS_COMMANDS:
            if pattern.search(candidate):
                return reason
    return ""
