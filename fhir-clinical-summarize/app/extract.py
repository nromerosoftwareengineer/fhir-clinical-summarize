"""Turns raw FHIR resources into packet facts, deterministically (no LLM).

This is the half of the service a reviewer has to be able to trust, so nothing
here calls a model and nothing here is approximate: every Fact carries the id of
the resource it came from, and the "missing" notes are computed in code. An LLM
asked to report an absence will sometimes report a presence instead.

It also shapes the payload the model does see. Measured on the busiest patient in
the sample set (14 conditions, 164 medication orders):

    raw FHIR from the two searches   ~45,000 tokens
    one Fact per resource             ~5,000 tokens
    after deduplication                 ~774 tokens
    split into active / past            ~460 tokens

The deduplication step is the important one. Those 164 medication orders are 11
distinct drugs -- 60 cisplatin cycles, 60 paclitaxel, 36 simvastatin -- and
Ollama's default context window is 4096 tokens, so the undeduplicated payload
would be silently truncated mid-record rather than rejected.
"""

from app.models import Fact

# FHIR condition-clinical codes that mean "this is a current problem". A
# recurrence or relapse is active; resolved and remission are history.
ACTIVE_CONDITION_STATUSES = frozenset({"active", "recurrence", "relapse"})

# MedicationRequest.status values that mean the patient is on the drug now.
ACTIVE_MEDICATION_STATUSES = frozenset({"active"})

# Upper bound on how many facts of one kind reach the prompt. Deduplication
# already cuts the realistic worst case to ~26 rows; this only guards against a
# pathological record quietly blowing past the context window.
MAX_PROMPT_FACTS = 40


def _coding(concept: dict) -> dict:
    """First coding of a CodeableConcept, or an empty dict."""
    return (concept.get("coding") or [{}])[0]


def _display(concept: dict, fallback: str) -> str:
    """Human-readable label for a CodeableConcept.

    Prefers the coded display, then the concept's own text, then a placeholder --
    real records are sparse and a missing display should not drop the fact.
    """
    return _coding(concept).get("display") or concept.get("text") or fallback


def condition_to_fact(resource: dict) -> Fact:
    """Map one Condition resource to a Fact."""
    concept = resource.get("code") or {}
    # recordedDate is the fallback because a condition can be recorded without a
    # known onset, and a fact with no date sorts and reads badly.
    date = resource.get("onsetDateTime") or resource.get("recordedDate")

    return Fact(
        display=_display(concept, "Unknown condition"),
        source=f"Condition/{resource['id']}",
        status=_coding(resource.get("clinicalStatus") or {}).get("code"),
        date=date[:10] if date else None,
    )


def medication_to_fact(resource: dict) -> Fact:
    """Map one MedicationRequest resource to a Fact.

    Synthea uses medicationCodeableConcept, but R4 allows medicationReference
    instead, so both are handled: a reference-style record still produces a
    citable fact rather than an exception.
    """
    concept = resource.get("medicationCodeableConcept")
    if concept:
        display = _display(concept, "Unknown medication")
    else:
        reference = resource.get("medicationReference") or {}
        display = reference.get("display") or "Unknown medication"

    date = resource.get("authoredOn")
    return Fact(
        display=display,
        source=f"MedicationRequest/{resource['id']}",
        status=resource.get("status"),
        date=date[:10] if date else None,
    )


def _newest_first(facts: list[Fact]) -> list[Fact]:
    """Sort by date descending, undated facts last."""
    return sorted(facts, key=lambda fact: (fact.date is not None, fact.date or ""), reverse=True)


def _dedupe(facts: list[Fact], key) -> list[Fact]:
    """Collapse facts sharing a key, keeping the newest and counting the rest.

    The surviving fact keeps the newest record's source, so the citation points
    at the most recent evidence rather than an arbitrary one.
    """
    kept: dict[tuple, Fact] = {}
    for fact in _newest_first(facts):
        identity = key(fact)
        if identity in kept:
            kept[identity].count += 1
        else:
            kept[identity] = fact.model_copy()
    return _newest_first(list(kept.values()))


def dedupe_conditions(facts: list[Fact]) -> list[Fact]:
    """Deduplicate conditions, keeping distinct episodes.

    The date is part of the key on purpose. One patient in the sample set has two
    viral sinusitis entries four years apart: those are two separate illnesses,
    and a (display, status) key would silently delete one of them from the
    record. Only exact repeats of the same dated entry collapse.
    """
    return _dedupe(facts, lambda fact: (fact.display, fact.status, fact.date))


def dedupe_medications(facts: list[Fact]) -> list[Fact]:
    """Deduplicate medications by drug and status, ignoring the date.

    Repetition here means repeat dosing rather than a distinct event -- a chemo
    patient has one MedicationRequest per cycle. Collapsing them is what takes
    the prompt from ~5,000 tokens to ~774, and `count` preserves how many
    orders there were.
    """
    return _dedupe(facts, lambda fact: (fact.display, fact.status))


def build_missing(conditions: list[Fact], medications: list[Fact]) -> list[str]:
    """Note what the record does not contain.

    Computed in code, never by the model: asked to report an absence, a small
    model will sometimes invent a presence.
    """
    missing: list[str] = []
    if not conditions:
        missing.append("No conditions on file")
    elif not any(fact.status in ACTIVE_CONDITION_STATUSES for fact in conditions):
        missing.append("No active conditions on file")

    if not medications:
        missing.append("No medications on file")
    elif not any(fact.status in ACTIVE_MEDICATION_STATUSES for fact in medications):
        missing.append("No active medications on file")

    return missing


def _label(fact: Fact) -> str:
    """One prompt line for an active fact, carrying its start date.

    The date matters: the sample set contains a condition still flagged active
    with a 1990 onset, and a model given only the status will write it up as a
    present-day problem.
    """
    label = fact.display
    if fact.date:
        label += f" (since {fact.date})"
    if fact.count > 1:
        label += f" x{fact.count}"
    return label


def to_prompt_facts(conditions: list[Fact], medications: list[Fact]) -> dict:
    """Shape the facts for the model.

    Two deliberate choices:

    - Active and past are split here rather than left to the model. Given a flat
      list with status fields, llama3.2:3b returns a comma-separated dump; given
      pre-grouped lists, it writes prose. Grouping is cheap in Python and
      unreliable in a 3B model.
    - Sources are not included. The model has no use for resource ids, and
      anything in the prompt is something it can mangle. Citations are attached
      deterministically after the summary returns, which makes a fabricated
      source structurally impossible.
    """
    active_conditions = [f for f in conditions if f.status in ACTIVE_CONDITION_STATUSES]
    past_conditions = [f for f in conditions if f.status not in ACTIVE_CONDITION_STATUSES]
    active_medications = [f for f in medications if f.status in ACTIVE_MEDICATION_STATUSES]
    past_medications = [f for f in medications if f.status not in ACTIVE_MEDICATION_STATUSES]

    return {
        "active_conditions": [_label(f) for f in active_conditions[:MAX_PROMPT_FACTS]],
        "active_medications": [_label(f) for f in active_medications[:MAX_PROMPT_FACTS]],
        "past_conditions": [f.display for f in past_conditions[:MAX_PROMPT_FACTS]],
        "past_medications": [f.display for f in past_medications[:MAX_PROMPT_FACTS]],
    }


def build_facts(
    condition_resources: list[dict], medication_resources: list[dict]
) -> tuple[list[Fact], list[Fact]]:
    """Raw FHIR resources in, deduplicated and sorted packet facts out."""
    conditions = dedupe_conditions([condition_to_fact(r) for r in condition_resources])
    medications = dedupe_medications([medication_to_fact(r) for r in medication_resources])
    return conditions, medications
