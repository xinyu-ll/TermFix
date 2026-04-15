"""
LLM client for TermFix.

Uses the OpenAI-compatible endpoint exposed by Anthropic
(https://api.anthropic.com/v1/) so that any OpenAI-SDK-compatible
client can talk to Claude models without the Anthropic SDK.

  • Streaming — collects chunks to avoid HTTP timeouts on long outputs
  • System message — stable SYSTEM_PROMPT sent as role:system turn
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

import openai

from config import DEFAULT_MODEL, SYSTEM_PROMPT
from context import build_user_message

logger = logging.getLogger(__name__)

# Anthropic's OpenAI-compatible base URL
_API_BASE_URL = "https://api.anthropic.com/v1/"

# ── Type alias for the structured result ──────────────────────────────────

AnalysisResult = dict  # {"cause": str, "fix_commands": list[str], "explanation": str}

_EMPTY_RESULT: AnalysisResult = {
    "cause": "Analysis unavailable.",
    "fix_commands": [],
    "explanation": "Could not contact the API. Check your API key and network.",
}


# ── Public API ─────────────────────────────────────────────────────────────

async def analyze_error(
    context: dict,
    api_key: str,
    model: str = DEFAULT_MODEL,
) -> AnalysisResult:
    """Call Claude via the OpenAI-compatible endpoint and return a structured result.

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
        result = await _call_api(api_key, model, user_message)
        return result
    except openai.AuthenticationError:
        logger.error("Authentication failed — check API key")
        return {**_EMPTY_RESULT, "cause": "Authentication failed. Verify your Anthropic API key."}
    except openai.RateLimitError:
        logger.warning("Rate limit hit")
        return {**_EMPTY_RESULT, "cause": "Rate limited by API. Please wait and try again."}
    except openai.APIConnectionError as exc:
        logger.error("Network error reaching API: %s", exc)
        return {**_EMPTY_RESULT, "cause": f"Network error: {exc}"}
    except openai.APIStatusError as exc:
        logger.error("API status error %s: %s", exc.status_code, exc.message)
        return {**_EMPTY_RESULT, "cause": f"API error {exc.status_code}: {exc.message}"}
    except Exception as exc:
        logger.exception("Unexpected error during LLM analysis")
        return {**_EMPTY_RESULT, "cause": f"Unexpected error: {exc}"}


# ── Private helpers ────────────────────────────────────────────────────────

async def _call_api(
    api_key: str,
    model: str,
    user_message: str,
) -> AnalysisResult:
    """Make the actual streaming API call and parse the JSON response."""
    client = openai.AsyncOpenAI(
        api_key=api_key,
        base_url=_API_BASE_URL,
    )

    # Collect streamed chunks to avoid HTTP timeouts on long outputs.
    stream = await client.chat.completions.create(
        model=model,
        max_tokens=1024,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        stream=True,
    )

    text = ""
    async for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            text += delta

    logger.debug("LLM response received (%d chars)", len(text))
    return _parse_json(text)


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
