"""Score app/link.py against the hand-labelled citation set.

    python -m scripts.eval_citations [--verbose]

The set is `tests/data/citation_gold.json`: every mention in 13 real summaries
labelled with the fact it should cite, the phrases that must stay unlinked, and
the ones that are defensible either way. Each case carries its own prose and the
facts that prose was written from, so this runs with no FHIR server, no model and
no `eval_output/` worksheet -- and a later eval run, or a prompt change, cannot
silently invalidate the labelling.

Precision and recall are reported separately and precision is the one that
matters: an unlinked term costs a reviewer a glance at the fact table, while a
term linked to the wrong record costs them the review. The run exits non-zero
on any wrong link, so a regression fails rather than being averaged away.
"""

import argparse
import json
import pathlib
import re

from app.link import find_spans
from app.models import Fact

GOLD_FILE = pathlib.Path("tests/data/citation_gold.json")

# prompt_facts labels carry what extract added for the prompt, not the display:
# " (since 2001-03-08)" and a repeat count. Both come back off here.
DECORATION = re.compile(r"\s*\(since \d{4}-\d{2}-\d{2}\)|\s*x\d+$")
ONSET = re.compile(r"\(since (\d{4}-\d{2}-\d{2})\)")


def rebuild_facts(prompt_facts: dict) -> tuple[list[Fact], list[Fact], dict[str, str]]:
    """prompt_facts -> the Facts it was shaped from, plus source -> display.

    Sources are synthetic: the worksheets record what the model saw, not the
    resource ids, and the labels are displays. Comparing by display is also what
    makes two episodes of one condition comparable at all.
    """
    conditions: list[Fact] = []
    medications: list[Fact] = []
    owner: dict[str, str] = {}

    for group, labels in prompt_facts.items():
        is_condition = "condition" in group
        kind = "Condition" if is_condition else "MedicationRequest"
        active = group.startswith("active")
        for label in labels:
            onset = ONSET.search(label)
            display = DECORATION.sub("", label).strip()
            source = f"{kind}/{len(owner)}"
            owner[source] = display
            fact = Fact(
                display=display,
                source=source,
                status="active" if active else ("resolved" if is_condition else "stopped"),
                date=onset.group(1) if onset else None,
            )
            (conditions if is_condition else medications).append(fact)

    return conditions, medications, owner


def locate(summary: str, phrase: str, occurrence: int) -> tuple[int, int]:
    """Offsets of the nth (1-based) case-insensitive occurrence of a phrase."""
    lowered, at = summary.casefold(), -1
    for _ in range(occurrence):
        at = lowered.find(phrase.casefold(), at + 1)
        if at < 0:
            raise SystemExit(f"gold phrase not in summary (#{occurrence}): {phrase!r}")
    return at, at + len(phrase)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verbose", action="store_true", help="list every miss and bad link")
    args = parser.parse_args()

    gold = json.loads(GOLD_FILE.read_text())

    correct = wrong = unlinkable = ignored = wide = linked = labelled = 0
    bad: list[tuple] = []
    missed: list[tuple] = []

    for case in gold["cases"]:
        summary = case["summary"]
        conditions, medications, owner = rebuild_facts(case["prompt_facts"])

        expected = [(*locate(summary, p, o), fact) for p, o, fact in case["links"]]
        optional = [locate(summary, p, o) for p, o in case.get("ignore", [])]
        forbidden = [locate(summary, p, 1) for p in case.get("forbid", [])]
        labelled += len(expected)

        spans = find_spans(summary, conditions, medications)
        for span in spans:
            # The invariant the renderer depends on, checked on every span.
            if summary[span.start : span.end] != span.text:
                raise SystemExit(f"{case['case']}: span text disagrees with its offsets: {span}")

            display = owner[span.source]
            overlaps = [g for g in expected if span.start < g[1] and g[0] < span.end]
            if overlaps:
                hits = [g for g in overlaps if display == g[2]]
                if hits:
                    correct += 1
                    if (span.end - span.start) > 2 * max(g[1] - g[0] for g in hits):
                        wide += 1
                else:
                    wrong += 1
                    bad.append((case["case"], span.text, display, "wrong fact"))
            elif any(span.start < end and start < span.end for start, end in optional):
                ignored += 1
            elif any(span.start < end and start < span.end for start, end in forbidden):
                wrong += 1
                bad.append((case["case"], span.text, display, "phrase names no fact"))
            else:
                unlinkable += 1
                bad.append((case["case"], span.text, display, "unlabelled phrase"))

        for start, end, fact in expected:
            if any(
                span.start < end and start < span.end and owner[span.source] == fact
                for span in spans
            ):
                linked += 1
            else:
                missed.append((case["case"], summary[start:end], fact))

    produced = correct + wrong + unlinkable
    print(f"{len(gold['cases'])} summaries, {labelled} labelled mentions, {produced} links")
    print(f"  precision {correct / produced if produced else 0:.3f}  "
          f"recall {linked / labelled if labelled else 0:.3f}")
    print(f"  correct {correct}   wrong {wrong}   unlabelled {unlinkable}   "
          f"over-wide {wide}   defensible {ignored}   missed {len(missed)}")

    if args.verbose:
        for row in bad:
            print("  BAD ", row)
        for row in missed:
            print("  MISS", row)
    elif bad:
        print("\n  re-run with --verbose for the offending links")

    if wrong:
        raise SystemExit(f"\n{wrong} link(s) cite the wrong record")


if __name__ == "__main__":
    main()
