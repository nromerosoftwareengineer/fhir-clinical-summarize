"""Tests for the cache key. No Postgres, no FHIR server, no model.

The database read/write path is deliberately not covered here: it needs a live
Postgres, and the existing suite's value is that it runs anywhere in a tenth of
a second. What *is* covered is the part that decides correctness -- whether a
change in the patient's facts is detected, and whether a non-change is not --
because a hash that misses a real change serves stale clinical prose, and one
that fires on an irrelevant change makes the cache useless.
"""

from app import summary_cache


def facts(**overrides) -> dict:
    """A prompt payload shaped like extract.to_prompt_facts returns."""
    base = {
        "active_conditions": ["Obesity (since 1997-08-26)", "Hypertension (since 2001-02-03)"],
        "active_medications": ["Lisinopril 10 mg"],
        "past_conditions": ["Acute bronchitis"],
        "past_medications": ["Amoxicillin 500 mg"],
    }
    base.update(overrides)
    return base


def test_same_facts_same_hash():
    assert summary_cache.fact_hash(facts()) == summary_cache.fact_hash(facts())


def test_hash_is_order_independent():
    """A reindex that reorders same-date facts must not invalidate the cache.

    extract._newest_first is a stable sort over an unsorted FHIR result, so facts
    sharing a date keep whatever order the server returned. That order is not a
    clinical fact and must not count as a change.
    """
    reordered = facts(active_conditions=[
        "Hypertension (since 2001-02-03)", "Obesity (since 1997-08-26)",
    ])
    assert summary_cache.fact_hash(facts()) == summary_cache.fact_hash(reordered)


def test_added_condition_changes_hash():
    more = facts(active_conditions=[
        *facts()["active_conditions"], "Type 2 diabetes (since 2010-05-01)",
    ])
    assert summary_cache.fact_hash(facts()) != summary_cache.fact_hash(more)


def test_removed_medication_changes_hash():
    assert summary_cache.fact_hash(facts()) != summary_cache.fact_hash(
        facts(active_medications=[])
    )


def test_dose_change_changes_hash():
    assert summary_cache.fact_hash(facts()) != summary_cache.fact_hash(
        facts(active_medications=["Lisinopril 20 mg"])
    )


def test_onset_date_change_changes_hash():
    """The date is in the prompt, so a corrected onset must regenerate the prose."""
    assert summary_cache.fact_hash(facts()) != summary_cache.fact_hash(
        facts(active_conditions=["Obesity (since 1998-01-01)",
                                 "Hypertension (since 2001-02-03)"])
    )


def test_active_to_past_changes_hash():
    """The same condition moving from active to history is the change that matters most.

    Nothing about the set of condition names changes here -- only which list they
    sit in -- and that is precisely the distinction a prior-authorization reviewer
    is reading the summary for.
    """
    resolved = facts(active_conditions=["Hypertension (since 2001-02-03)"],
                     past_conditions=["Acute bronchitis", "Obesity"])
    assert summary_cache.fact_hash(facts()) != summary_cache.fact_hash(resolved)


def test_count_suffix_changes_hash():
    """extract._label appends "xN" for deduplicated facts; 60 orders is clinically real."""
    assert summary_cache.fact_hash(facts()) != summary_cache.fact_hash(
        facts(active_medications=["Lisinopril 10 mg x60"])
    )


def test_empty_facts_hash_is_stable():
    empty = {"active_conditions": [], "active_medications": [],
             "past_conditions": [], "past_medications": []}
    assert summary_cache.fact_hash(empty) == summary_cache.fact_hash(dict(empty))
    assert summary_cache.fact_hash(empty) != summary_cache.fact_hash(facts())


def test_hash_is_hex_sha256():
    digest = summary_cache.fact_hash(facts())
    assert len(digest) == 64
    assert all(char in "0123456789abcdef" for char in digest)


def test_model_does_not_affect_hash(monkeypatch):
    """By design: the cache invalidates on patient data, not on the model.

    Documented in app/summary_cache, and scripts/cache_admin.py --clear is the
    lever for a model change. This test exists so the behaviour cannot drift
    silently -- if someone later folds the model into the key, this fails and
    they have to confirm it was deliberate.
    """
    from app.config import settings

    before = summary_cache.fact_hash(facts())
    monkeypatch.setattr(settings, "ollama_model", "some-other-model:1b")
    assert summary_cache.fact_hash(facts()) == before
