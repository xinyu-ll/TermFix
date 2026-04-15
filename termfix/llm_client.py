"""
LLM client for TermFix.

Uses the official Anthropic async SDK with:
  • Streaming  — avoids SDK HTTP timeouts on long outputs
  • Prompt caching — stable system prompt marked with cache_control so
    repeated calls pay ~10 % of the input-token cost after the first hit
  • Adaptive thinking — claude-opus-4-6 decides internally how much
    reasoning to invest; no budget_tokens needed
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

import anthropic

from config import DEFAULT_MODEL, SYSTEM_PROMPT
from context import build_user_message

logger = logging.getLogger(__name__)

# ── Type alias for the structured result ──────────────────────────────────

AnalysisResult = dict  # {"cause": str, "fix_commands": list[str], "explanation": str}

_EMPTY_RESULT: AnalysisResult = {
    "cause": "Analysis unavailable.",
    "fix_commands": [],
    "explanation": "Could not contact the Claude API. Check your API key and network.",
}


# ── Public API ─────────────────────────────────────────────────────────────

async def analyze_error(
    context: dict,
    api_key: str,
    model: str = DEFAULT_MODEL,
) -> AnalysisResult:
    """Call Claude to analyse a failed command and return a structured result.

    Args:
        context:  Dict produced by context.collect_context().
        api_key:  Anthropic API key (read from StatusBar knob at call time).
        model:    Model ID string; defaults to claude-opus-4-6.

    Returns:
        AnalysisResult dict with keys "cause", "fix_commands", "explanation".
        Never raises — on any error returns _EMPTY_RESULT with an explanatory
        cause string.
    """
    if not api_key:
        logger.warning("No API key configured — skipping LLM analysis")
        return {
            **_EMPTY_RESULT,
            "cause": "No API key set. Click the TermFix status bar component → "
                     "configure the 'API Key' knob with your Anthropic key.",
        }

    user_message = build_user_message(context)

    try:
        result = await _call_claude(api_key, model, user_message)
        return result
    except anthropic.AuthenticationError:
        logger.error("Anthropic authentication failed — check API key")
        return {**_EMPTY_RESULT, "cause": "Authentication failed. Verify your Anthropic API key."}
    except anthropic.RateLimitError:
        logger.warning("Anthropic rate limit hit")
        return {**_EMPTY_RESULT, "cause": "Rate limited by Anthropic API. Please wait and try again."}
    except anthropic.APIConnectionError as exc:
        logger.error("Network error reaching Anthropic API: %s", exc)
        return {**_EMPTY_RESULT, "cause": f"Network error: {exc}"}
    except anthropic.APIStatusError as exc:
        logger.error("Anthropic API status error %s: %s", exc.status_code, exc.message)
        return {**_EMPTY_RESULT, "cause": f"API error {exc.status_code}: {exc.message}"}
    except Exception as exc:
        logger.exception("Unexpected error during LLM analysis")
        return {**_EMPTY_RESULT, "cause": f"Unexpected error: {exc}"}


# ── Private helpers ────────────────────────────────────────────────────────

async def _call_claude(
    api_key: str,
    model: str,
    user_message: str,
) -> AnalysisResult:
    """Make the actual streaming API call and parse the JSON response."""
    client = anthropic.AsyncAnthropic(api_key=api_key)

    # System prompt is stable → mark for prefix caching.
    # If the prompt is shorter than the model's minimum cacheable prefix
    # (~4096 tokens for Opus 4.6), the cache_control is silently ignored —
    # no error is raised, the call just doesn't benefit from caching.
    system_blocks = [
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    # Use streaming so large outputs don't hit HTTP timeouts.
    # get_final_message() waits for the complete response without needing
    # to handle individual delta events.
    async with client.messages.stream(
        model=model,
        max_tokens=1024,
        thinking={"type": "adaptive"},
        system=system_blocks,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        message = await stream.get_final_message()

    raw_text = _extract_text(message)
    logger.debug(
        "LLM response — input=%d cached_read=%d output=%d tokens",
        message.usage.input_tokens,
        getattr(message.usage, "cache_read_input_tokens", 0),
        message.usage.output_tokens,
    )

    return _parse_json(raw_text)


def _extract_text(message: anthropic.types.Message) -> str:
    """Pull the first TextBlock from a Message (thinking blocks come first)."""
    for block in message.content:
        if block.type == "text":
            return block.text
    return ""


def _parse_json(raw: str) -> AnalysisResult:
    """Parse the LLM's JSON response, with a tolerant fallback for fenced code."""
    text = raw.strip()

    # Strip optional ``` fences the model might add despite instructions
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("LLM returned non-JSON response; wrapping as explanation")
        return {
            "cause": "Could not parse structured response from Claude.",
            "fix_commands": [],
            "explanation": raw,
        }

    # Normalise — ensure expected keys exist
    return {
        "cause": str(data.get("cause", "Unknown cause.")),
        "fix_commands": [str(c) for c in data.get("fix_commands", [])],
        "explanation": str(data.get("explanation", "")),
    }
