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

# Two to four sentences, in prose, grounded in the given facts. Every clause in
# here is scar tissue from an observed failure:
#
# - "Do not output a list": without it llama3.2:3b returned "Stroke (active),
#   Osteoporosis (active), ..." instead of a summary.
# - The numbered structure, because a small model follows positional
#   instructions far more reliably than abstract ones.
# - "Write nothing after those sentences", because an open-ended final slot
#   ("any relevant information") gets filled by restating the active conditions
#   as history once the model has run out of facts. Measured on a patient with
#   three active conditions and one past: it wrote "The patient's history
#   includes prediabetes, anemia, and chronic sinusitis" as a fifth sentence.
# - "Never describe an active condition ... as past or resolved", which is the
#   direction the prompt used to leave unguarded. The reverse rule below has
#   been here since the first version; this one closes the pair.
#
# A bounded length is also what keeps inference affordable: an unbounded "up to
# five sentences" measured 19-26 s warm against 3.8 s for two, which on CPU runs
# past a reviewer's patience and close to request_timeout_s.
SYSTEM_PROMPT = """You write a short clinical summary for a prior-authorization reviewer.

Write two to four sentences of prose. Do not output a list. Do not use bullet points.
Sentence 1: the active conditions, in the present tense.
Sentence 2: the active medications in the present tense, or that there are none.
Sentence 3: the past conditions and past medications, if the record has any.
Write nothing after those sentences.

Use only the facts given. Never invent a condition, medication, or date.
Treat past_conditions and past_medications as history, never as current.
Never describe an active condition or active medication as past or resolved.
Do not recommend, approve, or deny authorization."""

# Ollama validates the response against this schema, so the reply is always
# parseable JSON rather than prose that has to be scraped.
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}

# gemma3:4b advertises a 131,072-token context, but Ollama's own default
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
