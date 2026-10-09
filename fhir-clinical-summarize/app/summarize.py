"""Writes the reviewer summary with a local Ollama model using structured output.

The model's only job is prose. Facts, statuses, dates and source references are
all settled before this module is called, and nothing it returns is allowed to
become a fact: its output lands in Packet.summary and nowhere else. That is what
keeps a hallucinated citation structurally impossible rather than merely
unlikely.

Every failure here degrades to an empty summary. A packet with facts and sources
but no prose is still useful to a reviewer; a 500 is not.
"""

import json
import logging

import httpx
from pydantic import BaseModel, ValidationError

from app.config import settings

logger = logging.getLogger(__name__)

# Two sentences, in prose, grounded in the given facts. The "do not output a
# list" instruction is not decoration: without it llama3.2:3b returns
# "Stroke (active), Osteoporosis (active), ..." instead of a summary. The
# per-sentence structure is there because a 3B model follows positional
# instructions far more reliably than abstract ones.
SYSTEM_PROMPT = """You write a two-sentence clinical summary for a prior-authorization reviewer.

Write exactly two sentences in prose. Do not output a list. Do not use bullet points.
Sentence 1: the patient's active conditions.
Sentence 2: the active medications, and whether any notable history is past.

Use only the facts given. Never invent a condition, medication, or date.
Treat past_conditions and past_medications as history, never as current.
Do not recommend, approve, or deny authorization."""

# Ollama validates the response against this schema, so the reply is always
# parseable JSON rather than prose that has to be scraped.
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}

# llama3.2:3b advertises a 131,072-token context, but Ollama's own default
# num_ctx is 4096 and it truncates silently rather than erroring. The shaped
# payload runs ~460 tokens, so 8192 is ample headroom at negligible cost.
NUM_CTX = 8192


class _SummaryResponse(BaseModel):
    """The model's reply, validated before anything downstream sees it."""

    summary: str


# One pooled client for the process, matching app/fhir_client.py.
_client = httpx.Client(base_url=settings.ollama_base_url, timeout=settings.request_timeout_s)


def close() -> None:
    _client.close()


def write_summary(prompt_facts: dict) -> str:
    """Summarize the shaped facts. Returns "" rather than raising, ever.

    prompt_facts is the dict from extract.to_prompt_facts: pre-grouped active and
    past lists, no source references.
    """
    if not any(prompt_facts.values()):
        # Nothing to summarize. Packet.missing already explains the empty record,
        # and asking the model to describe nothing invites it to invent something.
        return ""

    payload = {
        "model": settings.ollama_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(prompt_facts, indent=2)},
        ],
        "format": SUMMARY_SCHEMA,
        "stream": False,
        # temperature 0 so the same packet summarizes the same way twice, which
        # matters both for the hand review and for demoing under questioning.
        "options": {"temperature": 0, "num_ctx": NUM_CTX},
    }

    try:
        response = _client.post("/api/chat", json=payload)
        response.raise_for_status()
        # The content is a JSON *string* inside the chat response, not an object.
        content = response.json()["message"]["content"]
        return _SummaryResponse.model_validate_json(content).summary.strip()
    except httpx.HTTPError as exc:
        logger.warning("summary unavailable: ollama request failed: %r", exc)
    except (KeyError, json.JSONDecodeError, ValidationError) as exc:
        logger.warning("summary unavailable: unusable model response: %r", exc)

    return ""
