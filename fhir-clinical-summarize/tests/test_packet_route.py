"""Tests for how the packet route assembles a response -- no FHIR server, no model.

The route is the only place where facts, prose and citations meet, and the
property that matters there is an ordering one: citations are resolved from
facts that already carry their sources, after the model has returned, so a
source can only ever be one the packet already contained.

The FHIR client and the summary cache are patched rather than mocked loosely:
each returns exactly what the real one returns, so these tests fail if the
assembly changes shape, not if an implementation detail moves.
"""

import pytest
from fastapi.testclient import TestClient

from app import main

SUMMARY = "The patient has active hypertension. They are currently taking Lisinopril."

PATIENT = {"resourceType": "Patient", "id": "p1", "name": [{"family": "Donnelly"}]}

CONDITION = {
    "resourceType": "Condition",
    "id": "c1",
    "code": {"coding": [{"display": "Hypertension"}]},
    "clinicalStatus": {"coding": [{"code": "active"}]},
    "onsetDateTime": "1994-04-09T10:00:00-05:00",
}

MEDICATION = {
    "resourceType": "MedicationRequest",
    "id": "m1",
    "status": "active",
    "medicationCodeableConcept": {"coding": [{"display": "Lisinopril 10 MG Oral Tablet"}]},
    "authoredOn": "2015-03-01T10:00:00-05:00",
}


@pytest.fixture
def client(monkeypatch):
    """A client whose FHIR server and model are replaced by fixed returns."""
    monkeypatch.setattr(main.fhir, "get_patient", lambda patient_id: PATIENT)
    monkeypatch.setattr(main.fhir, "get_conditions", lambda patient_id: [CONDITION])
    monkeypatch.setattr(main.fhir, "get_medications", lambda patient_id: [MEDICATION])
    monkeypatch.setattr(main.summary_cache, "get_or_write", lambda facts, patient_id: SUMMARY)
    return TestClient(main.app)


def packet(client):
    response = client.get("/patients/p1/packet")
    assert response.status_code == 200
    return response.json()


def test_citations_are_serialised_with_the_packet(client):
    body = packet(client)
    assert [(cite["text"], cite["source"]) for cite in body["citations"]] == [
        ("hypertension", "Condition/c1"),
        ("Lisinopril", "MedicationRequest/m1"),
    ]


def test_every_citation_slices_back_to_its_own_text(client):
    # The browser renders by slicing the summary on these offsets. If one
    # disagrees with its text, every link after it is displaced.
    body = packet(client)
    for cite in body["citations"]:
        assert body["summary"][cite["start"] : cite["end"]] == cite["text"]


def test_no_citation_names_a_source_outside_the_packet(client):
    body = packet(client)
    sources = {fact["source"] for fact in body["conditions"] + body["medications"]}
    assert {cite["source"] for cite in body["citations"]} <= sources


def test_a_missing_summary_costs_the_citations_and_nothing_else(client, monkeypatch):
    # write_summary degrades to "" when the model is down. The facts, their
    # sources and the missing notes all still have to arrive.
    monkeypatch.setattr(main.summary_cache, "get_or_write", lambda facts, patient_id: "")
    body = packet(client)
    assert body["summary"] == ""
    assert body["citations"] == []
    assert body["conditions"][0]["source"] == "Condition/c1"
    assert body["medications"][0]["source"] == "MedicationRequest/m1"


def test_citations_are_in_reading_order_and_never_overlap(client):
    body = packet(client)
    last = 0
    for cite in body["citations"]:
        assert cite["start"] >= last
        assert cite["end"] > cite["start"]
        last = cite["end"]
