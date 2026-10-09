"""Tests for the deterministic layer -- no FHIR server, no model.

The resources below are trimmed copies of real Synthea records, including the
awkward ones found in the sample set: a resolved condition, repeat dosing, two
episodes of the same illness years apart, and a record with no coded display.
"""

from app.extract import (
    build_facts,
    build_missing,
    condition_to_fact,
    dedupe_conditions,
    dedupe_medications,
    medication_to_fact,
    to_prompt_facts,
)


def condition(code="59621000", display="Hypertension", status="active", onset="2015-03-01"):
    return {
        "resourceType": "Condition",
        "id": f"cond-{code}-{onset}",
        "code": {"coding": [{"system": "http://snomed.info/sct", "code": code, "display": display}]},
        "clinicalStatus": {"coding": [{"code": status}]},
        "onsetDateTime": f"{onset}T10:00:00-05:00",
    }


def medication(display="Simvistatin 10 MG", status="active", authored="2015-03-01", rx="312961"):
    return {
        "resourceType": "MedicationRequest",
        "id": f"med-{rx}-{authored}",
        "status": status,
        "medicationCodeableConcept": {
            "coding": [
                {"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": rx,
                 "display": display}
            ]
        },
        "authoredOn": f"{authored}T10:00:00-05:00",
    }


class TestConditionToFact:
    def test_maps_the_fields_a_reviewer_checks(self):
        fact = condition_to_fact(condition())
        assert fact.display == "Hypertension"
        assert fact.source == "Condition/cond-59621000-2015-03-01"
        assert fact.status == "active"
        assert fact.date == "2015-03-01"  # truncated to a date, not a timestamp

    def test_falls_back_to_concept_text_when_there_is_no_coded_display(self):
        resource = {"resourceType": "Condition", "id": "c1", "code": {"text": "Sore throat"}}
        assert condition_to_fact(resource).display == "Sore throat"

    def test_sparse_record_does_not_raise(self):
        # A Condition with nothing but an id still has to produce a citable fact.
        fact = condition_to_fact({"resourceType": "Condition", "id": "c2"})
        assert fact.display == "Unknown condition"
        assert fact.source == "Condition/c2"
        assert fact.status is None and fact.date is None

    def test_uses_recorded_date_when_onset_is_absent(self):
        resource = {"resourceType": "Condition", "id": "c3", "recordedDate": "2019-07-04T00:00:00Z"}
        assert condition_to_fact(resource).date == "2019-07-04"


class TestMedicationToFact:
    def test_maps_a_codeable_concept(self):
        fact = medication_to_fact(medication())
        assert fact.display == "Simvistatin 10 MG"
        assert fact.source == "MedicationRequest/med-312961-2015-03-01"
        assert fact.status == "active"
        assert fact.date == "2015-03-01"

    def test_falls_back_to_medication_reference(self):
        # R4 allows a reference instead of an inline concept; Synthea does not use
        # it, but a real EHR might, and the fact still has to be usable.
        resource = {
            "resourceType": "MedicationRequest",
            "id": "m1",
            "status": "active",
            "medicationReference": {"reference": "Medication/abc", "display": "Metformin 500 MG"},
        }
        assert medication_to_fact(resource).display == "Metformin 500 MG"


class TestDedupe:
    def test_medications_collapse_by_drug_and_count_the_orders(self):
        # A chemo patient has one order per cycle; collapsing them is what keeps
        # the prompt inside the context window.
        facts = [medication(authored=f"2020-01-{day:02d}") for day in range(1, 11)]
        deduped = dedupe_medications([medication_to_fact(f) for f in facts])
        assert len(deduped) == 1
        assert deduped[0].count == 10

    def test_dedupe_keeps_the_newest_source(self):
        facts = [medication(authored="2018-01-01"), medication(authored="2021-06-15")]
        deduped = dedupe_medications([medication_to_fact(f) for f in facts])
        assert deduped[0].date == "2021-06-15"
        assert deduped[0].source == "MedicationRequest/med-312961-2021-06-15"

    def test_same_drug_with_different_status_stays_separate(self):
        facts = [medication(status="active"), medication(status="stopped", authored="2014-01-01")]
        assert len(dedupe_medications([medication_to_fact(f) for f in facts])) == 2

    def test_conditions_keep_distinct_episodes(self):
        # One patient in the sample set has viral sinusitis in 2014 and again in
        # 2018. Those are two illnesses; a (display, status) key would delete one.
        facts = [
            condition(code="444814009", display="Viral sinusitis", status="resolved",
                      onset="2014-11-01"),
            condition(code="444814009", display="Viral sinusitis", status="resolved",
                      onset="2018-02-11"),
        ]
        deduped = dedupe_conditions([condition_to_fact(f) for f in facts])
        assert len(deduped) == 2

    def test_conditions_collapse_exact_repeats(self):
        facts = [condition(), condition()]
        assert len(dedupe_conditions([condition_to_fact(f) for f in facts])) == 1

    def test_sorted_newest_first(self):
        facts = [condition(onset="2001-01-01"), condition(onset="2020-01-01")]
        deduped = dedupe_conditions([condition_to_fact(f) for f in facts])
        assert [f.date for f in deduped] == ["2020-01-01", "2001-01-01"]


class TestBuildMissing:
    def test_empty_record(self):
        assert build_missing([], []) == ["No conditions on file", "No medications on file"]

    def test_no_medications_on_file(self):
        conditions = [condition_to_fact(condition())]
        assert build_missing(conditions, []) == ["No medications on file"]

    def test_all_history_is_reported_as_nothing_active(self):
        # Adolph: three resolved conditions, one stopped medication.
        conditions = [condition_to_fact(condition(status="resolved"))]
        medications = [medication_to_fact(medication(status="stopped"))]
        assert build_missing(conditions, medications) == [
            "No active conditions on file",
            "No active medications on file",
        ]

    def test_silent_when_something_is_active(self):
        conditions = [condition_to_fact(condition())]
        medications = [medication_to_fact(medication())]
        assert build_missing(conditions, medications) == []


class TestToPromptFacts:
    def test_splits_active_from_past_and_omits_sources(self):
        conditions = [
            condition_to_fact(condition(display="Hypertension")),
            condition_to_fact(condition(code="10509002", display="Bronchitis", status="resolved")),
        ]
        medications = [medication_to_fact(medication(status="stopped"))]
        prompt = to_prompt_facts(conditions, medications)

        assert prompt["active_conditions"] == ["Hypertension (since 2015-03-01)"]
        assert prompt["past_conditions"] == ["Bronchitis"]
        assert prompt["active_medications"] == []
        assert prompt["past_medications"] == ["Simvistatin 10 MG"]
        # Nothing the model sees may contain a source reference.
        assert "Condition/" not in str(prompt)
        assert "MedicationRequest/" not in str(prompt)

    def test_active_labels_carry_the_date(self):
        # The sample set has a condition still flagged active with a 1990 onset;
        # without the date the model writes it up as a present-day problem.
        facts = [condition_to_fact(condition(display="Obesity", onset="1990-05-29"))]
        assert to_prompt_facts(facts, [])["active_conditions"] == ["Obesity (since 1990-05-29)"]

    def test_repeat_count_reaches_the_prompt(self):
        facts = [medication_to_fact(medication(authored=f"2020-01-{d:02d}")) for d in range(1, 4)]
        prompt = to_prompt_facts([], dedupe_medications(facts))
        assert prompt["active_medications"] == ["Simvistatin 10 MG (since 2020-01-03) x3"]


class TestBuildFacts:
    def test_end_to_end_shaping(self):
        conditions, medications = build_facts(
            [condition(), condition(code="10509002", display="Bronchitis", status="resolved")],
            [medication(), medication(authored="2016-01-01")],
        )
        assert len(conditions) == 2
        assert len(medications) == 1  # same drug, same status -> collapsed
        assert medications[0].count == 2
        assert all(fact.source.startswith(("Condition/", "MedicationRequest/"))
                   for fact in conditions + medications)
