"""
LLM client for TermFix.

Uses an OpenAI-compatible chat completions endpoint so TermFix can work with
providers like DeepSeek, OpenAI, Anthropic-compatible gateways, or self-hosted
proxies without changing the rest of the plugin.

  • Streaming — collects chunks to avoid HTTP timeouts on long outputs
  • System message — stable SYSTEM_PROMPT sent as role:system turn
"""

from __future__ import annotations

import json
import logging
import re

try:
    import openai
except ModuleNotFoundError:  # pragma: no cover - depends on runtime environment
    openai = None

from config import DEFAULT_BASE_URL, DEFAULT_MODEL, SYSTEM_PROMPT
from context import build_user_message

logger = logging.getLogger(__name__)

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
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
) -> AnalysisResult:
    """Call an OpenAI-compatible endpoint and return a structured result.

    Args:
        context:  Dict produced by context.collect_context().
        api_key:  API key read from the StatusBar knob at call time.
        base_url: Base URL for the compatible API endpoint.
        model:    Model ID string; defaults to deepseek-chat.

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
                     "configure the 'API Key' knob with your provider key.",
        }

    user_message = build_user_message(context)

    if openai is None:
        logger.error("openai package is not installed in the active Python runtime")
        return {
            **_EMPTY_RESULT,
            "cause": "The `openai` package is not installed in iTerm2's Python runtime.",
            "explanation": (
                "Open iTerm2 -> Scripts -> Manage Dependencies and install `openai`, "
                "then restart the TermFix script."
            ),
        }

    try:
        result = await _call_api(api_key, base_url, model, user_message)
        return result
    except openai.AuthenticationError:
        logger.error("Authentication failed — check API key")
        return {**_EMPTY_RESULT, "cause": "Authentication failed. Verify your API key."}
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
    base_url: str,
    model: str,
    user_message: str,
) -> AnalysisResult:
    """Make the actual streaming API call and parse the JSON response."""
    client = openai.AsyncOpenAI(
        api_key=api_key,
        base_url=_normalise_base_url(base_url),
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
            "cause": "Could not parse a structured response from the model.",
            "fix_commands": [],
            "explanation": raw,
        }

    # Normalise — ensure expected keys exist
    return {
        "cause": str(data.get("cause", "Unknown cause.")),
        "fix_commands": [str(c) for c in data.get("fix_commands", [])],
        "explanation": str(data.get("explanation", "")),
    }


def _normalise_base_url(base_url: str) -> str:
    """Accept provider URLs with or without a trailing slash."""
    cleaned = (base_url or "").strip()
    if not cleaned:
        return DEFAULT_BASE_URL
    return cleaned.rstrip("/")
