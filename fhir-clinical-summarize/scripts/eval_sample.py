"""Runs a pinned sample of patients through the packet endpoint and saves output for hand review.

The split this script is built around: **what a string comparison can settle, it
settles; what needs a clinician's judgment it stages and leaves blank.** Scoring
"is this summary fair" with a second model would undercut the one claim this
project makes -- that Python settles the facts and the model only writes prose --
so there is no LLM judge here, and there should not be one.

Checked mechanically, reported as counts:

- every ``Fact.source`` reads back from FHIR and still carries the ``display``
  the packet cites it for. This is the "10/10 sources resolved" number, and it
  is a real read against the server rather than a plausibility check.
- the summary names no dose the prompt payload did not carry.
- the summary names no dosing *frequency* at all unless the payload carried one.
  gemma3:4b turned ``"Lisinopril 10 mg"`` into "prescribed Lisinopril 10 mg
  daily"; nothing upstream emits a frequency, so any is fabricated.
- the summary contains no approve/deny/authorize language.
- the summary is two sentences of prose with no bullet characters.

Left to the reviewer, because no string check catches it:

- whether the prose is *fair* about what is active. Both candidate models have
  been seen writing "has a history of X" for a condition sitting in
  ``active_conditions``. The markdown sheet puts the payload and the summary
  side by side with a blank verdict line per patient.

The sample is pinned, not random: three patients cover known edges, the rest are
drawn with a fixed seed, so two runs differ only by the model under test. With
``temperature: 0`` upstream that makes the comparison a diff:

    OLLAMA_MODEL=llama3.2:3b python -m scripts.eval_sample
    OLLAMA_MODEL=gemma3:4b   python -m scripts.eval_sample
    diff eval_output/eval-llama3.2-3b.md eval_output/eval-gemma3-4b.md

Run from the repo root, with the FHIR stack up:

    python -m scripts.eval_sample
    python -m scripts.eval_sample -n 20 --seed 7
    python -m scripts.eval_sample --model llama3.2:3b

Exits non-zero if any mechanical check failed, so it can gate a model change.
"""

import argparse
import datetime
import json
import pathlib
import re
import sys

from fastapi.testclient import TestClient

from app import extract, summary_cache
from app.config import settings
from app.fhir_client import FhirError, fhir
from app.main import app
from app.models import Fact

# Patients worth including whatever the seed selects, each covering an edge the
# deterministic layer has to get right. Verified against the loaded cohort; note
# these three are on the server but absent from data/patient_ids.txt, which is
# why they are pinned here by full id rather than sampled.
EDGE_CASES: dict[str, str] = {
    "3f8be6b0-15a6-43f4-87f3-1adb737fa598":
        "every condition and medication resolved/stopped -- 0 active, exercises `missing`",
    "632b7c1c-9545-4aa7-9fd5-07683139ef13":
        "two active conditions, zero medications -- exercises `missing`",
    "5e4b559d-6999-4c91-9256-7c9845d6e167":
        "14 conditions, 164 medication orders -> 12 distinct -- exercises deduplication",
}

DEFAULT_IDS_FILE = pathlib.Path("data/patient_ids.txt")
# .gitignore already reserves eval_output/, so worksheets stay local artifacts
# and data/ stays reserved for the Synthea download.
DEFAULT_OUT_DIR = pathlib.Path("eval_output")
DEFAULT_SAMPLE = 10
DEFAULT_SEED = 0

# The service assembles evidence and never issues a decision, so any of these in
# a summary is a prompt regression, not a style quibble.
DECISION_TERMS = (
    "approve", "approved", "approval", "deny", "denied", "denial",
    "authorize", "authorized", "authorization", "recommend", "recommended",
    "medically necessary", "eligible", "qualifies",
)

# No upstream code path emits a dosing frequency: extract._label appends an onset
# date and a count, nothing more. So a frequency in the prose came from the model.
FREQUENCY_TERMS = (
    "daily", "nightly", "weekly", "monthly", "hourly", "twice", "thrice",
    "once a", "per day", "a day", "every day", "every morning", "every night",
    "as needed", "prn", "bid", "tid", "qid", "qd", "hs",
    "two times", "three times", "four times",
)

# "10 mg", "0.5 ML", "100 unit" -- anything the model could have rounded or invented.
DOSE_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:mg|mcg|ug|g|ml|unit|units|%)\b", re.IGNORECASE)

SENTENCE_END_RE = re.compile(r"[.!?](?:\s|$)")
BULLET_RE = re.compile(r"(?m)^\s*[-*•]|•")

# Words a reviewer should eyeball: capitalised mid-sentence tokens are usually
# drug or condition names, and anything here that is not in the payload is worth
# a second look even when the mechanical checks pass.
TERM_RE = re.compile(r"\b[A-Z][a-z]{3,}\b")
TERM_STOPWORDS = frozenset({
    "The", "Their", "They", "This", "That", "There", "Notably", "Patient",
    "Sentence", "Both", "Past", "Active", "History", "Currently", "Additionally",
    "Prior", "Also", "Other", "These", "Those", "While", "With", "Although",
})


def _stamp() -> str:
    """Local wall-clock time, timezone-aware, for the run header."""
    return datetime.datetime.now(datetime.UTC).astimezone().isoformat(timespec="seconds")


def _normalise(text: str) -> str:
    """Lowercase and collapse whitespace, so "10  MG" and "10 mg" compare equal."""
    return re.sub(r"\s+", " ", text).strip().lower()


def _payload_text(prompt_facts: dict) -> str:
    """Everything the model was shown, as one normalised haystack."""
    return _normalise(" ".join(item for items in prompt_facts.values() for item in items))


def _sentence_count(summary: str) -> int:
    return len([part for part in SENTENCE_END_RE.split(summary) if part.strip()])


def check_sources(facts: list[Fact]) -> tuple[int, list[str]]:
    """Read every cited source back from FHIR and confirm it still says what the packet claims.

    Returns (resolved count, failure descriptions). A citation that 404s or that
    points at a record whose display has drifted is the single worst failure this
    service could have, so it is checked by reading the resource, not by parsing
    the reference string.
    """
    resolved = 0
    failures: list[str] = []

    for fact in facts:
        resource_type, _, resource_id = fact.source.partition("/")
        try:
            resource = fhir.read(resource_type, resource_id)
        except FhirError as exc:
            failures.append(f"{fact.source} did not read back: {exc}")
            continue

        # Re-derive the fact from the re-read resource and compare displays, so
        # the check uses the same extraction the packet used.
        if resource_type == "Condition":
            rederived = extract.condition_to_fact(resource)
        else:
            rederived = extract.medication_to_fact(resource)

        if _normalise(rederived.display) == _normalise(fact.display):
            resolved += 1
        else:
            failures.append(
                f"{fact.source} cites {fact.display!r} but reads back as {rederived.display!r}"
            )

    return resolved, failures


# Clauses that introduce something as past. An active condition named inside one
# is the error both candidate models make, so it is surfaced -- but never failed
# on, because it is sometimes right: the cohort contains an `active` miscarriage
# with a 1990 onset, where "history of" is the honest reading and the FHIR status
# is the thing that is wrong.
HISTORY_CLAUSE_RE = re.compile(
    r"(?:history of|previously|prior to|past medical history(?:\s+\w+)?)([^.;]{0,140})",
    re.IGNORECASE,
)


def history_framing_candidates(summary: str, prompt_facts: dict) -> list[str]:
    """Active conditions the prose introduces inside a history clause.

    A review aid, not a verdict. The reviewer decides; this only points.
    """
    candidates: list[str] = []
    clauses = [match.group(1).lower() for match in HISTORY_CLAUSE_RE.finditer(summary)]
    if not clauses:
        return candidates

    for label in prompt_facts.get("active_conditions", []):
        # Drop the "(disorder)" qualifier and the "(since ...)" suffix _label adds.
        head = re.sub(r"\s*\(.*", "", label).strip().lower()
        if not head:
            continue
        lead = head.split()[0]
        if any(lead in clause for clause in clauses):
            candidates.append(head)

    return candidates


def check_summary(summary: str, prompt_facts: dict) -> dict:
    """Run every mechanical check on the prose. Judgment calls are not in here."""
    payload = _payload_text(prompt_facts)
    low = _normalise(summary)
    has_facts = any(prompt_facts.values())

    # An empty summary is correct when there is nothing to summarise, and a
    # degraded-but-honest outcome otherwise -- write_summary never raises.
    if not summary:
        return {
            "empty": True,
            "empty_is_correct": not has_facts,
            "decision_terms": [],
            "invented_frequency": [],
            "ungrounded_doses": [],
            "sentences": 0,
            "has_bullets": False,
            "unmatched_terms": [],
            "history_framing": [],
        }

    invented_frequency = [
        term for term in FREQUENCY_TERMS if term in low and term not in payload
    ]
    ungrounded_doses = [
        dose for dose in DOSE_RE.findall(summary) if _normalise(dose) not in payload
    ]
    unmatched_terms = sorted({
        term for term in TERM_RE.findall(summary)
        if term not in TERM_STOPWORDS and term.lower() not in payload
    })

    return {
        "empty": False,
        "empty_is_correct": True,
        "decision_terms": [term for term in DECISION_TERMS if term in low],
        "invented_frequency": invented_frequency,
        "ungrounded_doses": ungrounded_doses,
        "sentences": _sentence_count(summary),
        "has_bullets": bool(BULLET_RE.search(summary)),
        "unmatched_terms": unmatched_terms,
        "history_framing": history_framing_candidates(summary, prompt_facts),
    }


def mechanical_failures(checks: dict, source_failures: list[str]) -> list[str]:
    """The subset of findings that are objectively wrong, not merely worth reviewing."""
    failures = list(source_failures)

    if checks["empty"]:
        if not checks["empty_is_correct"]:
            failures.append("summary empty although the patient has facts")
        return failures

    for term in checks["decision_terms"]:
        failures.append(f"decision language: {term!r}")
    for term in checks["invented_frequency"]:
        failures.append(f"invented dosing frequency: {term!r}")
    for dose in checks["ungrounded_doses"]:
        failures.append(f"dose not in payload: {dose!r}")
    if checks["sentences"] != 2:
        failures.append(f"expected 2 sentences, got {checks['sentences']}")
    if checks["has_bullets"]:
        failures.append("summary contains bullet characters")

    return failures


def select_patients(ids_file: pathlib.Path, sample: int, seed: int) -> list[tuple[str, str]]:
    """The pinned edge cases first, then a seeded fill to `sample`.

    Fixed seed for the same reason scripts/load_synthea.py uses one: a reviewer
    running this with the same arguments must score the same patients, or the
    counts in the README mean nothing.
    """
    import random

    selected = [(pid, reason) for pid, reason in EDGE_CASES.items()]
    if len(selected) >= sample:
        return selected[:sample]

    if not ids_file.is_file():
        print(f"warning: {ids_file} not found -- edge cases only", file=sys.stderr)
        return selected

    pool = sorted({
        line.strip() for line in ids_file.read_text().splitlines()
        if line.strip() and line.strip() not in EDGE_CASES
    })
    fill = random.Random(seed).sample(pool, min(sample - len(selected), len(pool)))
    return selected + [(pid, f"seeded sample (seed={seed})") for pid in fill]


def write_markdown(path: pathlib.Path, model: str, results: list[dict], totals: dict) -> None:
    """The reviewer's worksheet: payload beside prose, one blank verdict per patient."""
    lines = [
        f"# Hand review -- `{model}`",
        "",
        (f"Generated {_stamp()} · "
         f"{totals['patients']} patients · "
         f"{totals['sources_resolved']}/{totals['sources_total']} sources resolved · "
         f"{totals['clean']}/{totals['patients']} summaries passed every mechanical check"),
        "",
        "Fill in each **Verdict** line. The question the checks cannot answer: is the prose",
        "fair about what is *active*, or does it frame an active problem as history?",
        "",
    ]

    for result in results:
        checks = result["checks"]
        lines += [
            "---",
            "",
            f"## `{result['patient_id']}`",
            "",
            f"*{result['reason']}*",
            "",
            "### Payload the model saw",
            "",
        ]
        for key, items in result["prompt_facts"].items():
            lines.append(f"- **{key}**: {', '.join(items) if items else '_(none)_'}")
        lines += [
            "",
            f"`missing`: {', '.join(result['missing']) if result['missing'] else '_(none)_'}",
            "",
            "### Summary",
            "",
            f"> {result['summary'] or '_(empty)_'}",
            "",
            "### Mechanical checks",
            "",
            f"- sources resolved: **{result['sources_resolved']}/{result['sources_total']}**",
            (f"- sentences: {checks['sentences']}"
             f"{' ⚠️' if not checks['empty'] and checks['sentences'] != 2 else ''}"),
            f"- decision language: {checks['decision_terms'] or 'none'}",
            f"- invented frequency: {checks['invented_frequency'] or 'none'}",
            f"- ungrounded doses: {checks['ungrounded_doses'] or 'none'}",
            f"- terms to eyeball: {checks['unmatched_terms'] or 'none'}",
            (f"- **active conditions framed as history**: "
             f"{checks['history_framing'] or 'none'}  ← the judgment call"),
            "",
        ]
        if result["failures"]:
            lines += ["**Mechanical failures:**", ""]
            lines += [f"- {failure}" for failure in result["failures"]] + [""]
        lines += ["**Verdict (fair / unfair):** ", "", "**Notes:** ", ""]

    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-n", type=int, default=DEFAULT_SAMPLE, dest="sample",
                        help=f"patients to review, edge cases included (default {DEFAULT_SAMPLE})")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED,
                        help=f"seed for the fill beyond the edge cases (default {DEFAULT_SEED})")
    parser.add_argument("--ids", type=pathlib.Path, default=DEFAULT_IDS_FILE,
                        help=f"patient ids to draw the fill from (default {DEFAULT_IDS_FILE})")
    parser.add_argument("--model", default=None,
                        help="override OLLAMA_MODEL for this run, e.g. llama3.2:3b")
    parser.add_argument("--use-cache", action="store_true",
                        help="score whatever the summary cache holds instead of "
                             "regenerating (default: invalidate first, so the run "
                             "measures the model)")
    parser.add_argument("--out", type=pathlib.Path, default=None,
                        help="output path without extension (default eval_output/eval-<model>)")
    args = parser.parse_args()

    if args.model:
        # write_summary reads settings.ollama_model per call, so this takes effect
        # without re-importing; the pooled client's base_url is unaffected.
        settings.ollama_model = args.model

    model = settings.ollama_model
    out_base = args.out or DEFAULT_OUT_DIR / f"eval-{model.replace(':', '-')}"
    patients = select_patients(args.ids, args.sample, args.seed)

    print(f"model: {model}")
    print(f"fhir:  {settings.fhir_base_url}")
    print(f"cache: {'scoring stored summaries' if args.use_cache else 'invalidated per patient'}")
    print(f"reviewing {len(patients)} patients\n")

    client = TestClient(app)
    results: list[dict] = []
    totals = {"patients": 0, "sources_total": 0, "sources_resolved": 0, "clean": 0}

    for index, (patient_id, reason) in enumerate(patients, 1):
        # A cached summary was written by whichever model held the default at
        # the time, so scoring it would silently make a model comparison
        # meaningless. Dropping the row first forces a regeneration through the
        # ordinary request path -- the route has no bypass to offer, by design.
        if not args.use_cache:
            summary_cache.invalidate(patient_id)
        response = client.get(f"/patients/{patient_id}/packet")
        if response.status_code != 200:
            print(f"[{index:2d}/{len(patients)}] FAIL {patient_id[:8]} "
                  f"HTTP {response.status_code}")
            continue

        packet = response.json()
        facts = [Fact.model_validate(f) for f in packet["conditions"] + packet["medications"]]
        resolved, source_failures = check_sources(facts)

        # The payload the model saw, rebuilt independently of the route so the
        # grounding checks compare against the real prompt rather than the packet.
        conditions, medications = extract.build_facts(
            fhir.get_conditions(patient_id), fhir.get_medications(patient_id)
        )
        prompt_facts = extract.to_prompt_facts(conditions, medications)

        checks = check_summary(packet["summary"], prompt_facts)
        failures = mechanical_failures(checks, source_failures)

        totals["patients"] += 1
        totals["sources_total"] += len(facts)
        totals["sources_resolved"] += resolved
        totals["clean"] += not failures

        results.append({
            "patient_id": patient_id,
            "reason": reason,
            "prompt_facts": prompt_facts,
            "missing": packet["missing"],
            "summary": packet["summary"],
            "sources_total": len(facts),
            "sources_resolved": resolved,
            "checks": checks,
            "failures": failures,
        })

        flag = "ok  " if not failures else "FAIL"
        print(f"[{index:2d}/{len(patients)}] {flag} {patient_id[:8]}  "
              f"sources {resolved}/{len(facts):<3}  "
              f"{len(failures)} finding(s)")
        for failure in failures:
            print(f"           {failure}")

    out_base.parent.mkdir(parents=True, exist_ok=True)
    # Not with_suffix(): a model name like llama3.2-3b leaves ".2-3b" looking
    # like an extension, and with_suffix would eat it.
    json_path = out_base.parent / f"{out_base.name}.json"
    json_path.write_text(json.dumps(
        {"model": model,
         "generated": _stamp(),
         "seed": args.seed,
         "totals": totals,
         "patients": results},
        indent=2,
    ) + "\n")
    md_path = out_base.parent / f"{out_base.name}.md"
    write_markdown(md_path, model, results, totals)

    print(f"\n{totals['sources_resolved']}/{totals['sources_total']} sources resolved")
    print(f"{totals['clean']}/{totals['patients']} summaries passed every mechanical check")
    print(f"\nreview sheet -> {md_path}")
    print(f"raw results  -> {json_path}")

    # Non-zero so a model swap can be gated on this rather than on reading output.
    return 0 if totals["clean"] == totals["patients"] else 1


if __name__ == "__main__":
    sys.exit(main())
