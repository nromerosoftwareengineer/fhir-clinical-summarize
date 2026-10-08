"""Loads a random sample of Synthea transaction bundles into HAPI FHIR.

Wraps scripts/load_bundle.py: same PUT rewrite and validation, applied to many
files. The sample is drawn with a fixed seed so the loaded cohort is
reproducible -- a reviewer running this with the same arguments gets the same
patients, which is what makes the hand-review numbers in the README mean
anything.

Run from the repo root:

    python -m scripts.load_synthea -n 25
    python -m scripts.load_synthea -n 200 --seed 7
    python -m scripts.load_synthea -n 10 --dry-run

One bad file does not stop the run; failures are counted and listed at the end.
"""

import argparse
import pathlib
import random
import sys
import time

import httpx

from app.config import settings
from scripts.load_bundle import (
    BundleError,
    check_server,
    patient_id_from_result,
    read_bundle,
    status_counts,
    submit,
    to_put_bundle,
    validate,
)

# Size bounds, in KB. Bundles below the floor hold almost no clinical history
# (no conditions, no medications); the three above the ceiling are ~20k
# resources in a single atomic transaction, which strains the Docker VM without
# teaching the service anything a 1 MB patient does not.
DEFAULT_MIN_KB = 100
DEFAULT_MAX_KB = 15_000


def pick_bundles(
    directory: pathlib.Path, count: int, seed: int, min_kb: float, max_kb: float
) -> list[pathlib.Path]:
    """Choose `count` bundles at random from the size-eligible files.

    sorted() before sampling matters: glob order is filesystem-dependent, so
    without it the same seed would select different files on another machine.
    """
    eligible = [
        path
        for path in sorted(directory.glob("*.json"))
        if min_kb * 1000 <= path.stat().st_size <= max_kb * 1000
    ]
    if not eligible:
        sys.exit(f"no bundles in {directory} between {min_kb:.0f} and {max_kb:.0f} KB")

    if count >= len(eligible):
        return eligible
    return random.Random(seed).sample(eligible, count)


def load_one(client: httpx.Client, path: pathlib.Path, use_put: bool) -> tuple[str | None, int, str]:
    """Load a single bundle. Returns (patient_id, entry_count, status_summary).

    Raises BundleError, which the caller counts rather than dying on.
    """
    bundle = read_bundle(path)
    if use_put:
        to_put_bundle(bundle)
        validate(bundle)

    result = submit(client, bundle)
    counts = status_counts(result)
    summary = " ".join(f"{count}x{status.split()[0]}" for status, count in sorted(counts.items()))
    return patient_id_from_result(result), len(bundle["entry"]), summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "-n", "--count", type=int, default=25, help="how many bundles to load (default 25)"
    )
    parser.add_argument(
        "--dir",
        type=pathlib.Path,
        default=pathlib.Path("data/fhir"),
        help="directory of Synthea bundle JSON files (default data/fhir)",
    )
    parser.add_argument(
        "--seed", type=int, default=0, help="random seed for the sample (default 0)"
    )
    parser.add_argument("--min-kb", type=float, default=DEFAULT_MIN_KB)
    parser.add_argument("--max-kb", type=float, default=DEFAULT_MAX_KB)
    parser.add_argument("--base", default=settings.fhir_base_url, help="FHIR base URL")
    parser.add_argument(
        "--post",
        action="store_true",
        help="leave entries as POST (server assigns ids) instead of rewriting to PUT",
    )
    parser.add_argument(
        "--out",
        type=pathlib.Path,
        default=pathlib.Path("data/patient_ids.txt"),
        help="where to write the loaded patient ids (default data/patient_ids.txt)",
    )
    parser.add_argument("--dry-run", action="store_true", help="list the selection, send nothing")
    args = parser.parse_args()

    if not args.dir.is_dir():
        sys.exit(f"no such directory: {args.dir}")

    selected = pick_bundles(args.dir, args.count, args.seed, args.min_kb, args.max_kb)
    total_kb = sum(path.stat().st_size for path in selected) / 1000
    print(
        f"{len(selected)} bundles selected from {args.dir} "
        f"(seed {args.seed}, {args.min_kb:.0f}-{args.max_kb:.0f} KB, {total_kb / 1000:.1f} MB total)"
    )

    if args.dry_run:
        for index, path in enumerate(selected, 1):
            print(f"  [{index:4d}] {path.name[:70]:70} {path.stat().st_size / 1000:7.0f} KB")
        print("\nDry run - nothing sent")
        return

    patient_ids: list[str] = []
    failures: list[tuple[str, str]] = []
    resources = 0
    run_started = time.monotonic()

    with httpx.Client(base_url=args.base, timeout=600.0) as client:
        capability = check_server(client)
        print(
            f"server: {capability['software']['name']} {capability['software']['version']} "
            f"(FHIR {capability['fhirVersion']}) at {args.base}\n"
        )

        for index, path in enumerate(selected, 1):
            started = time.monotonic()
            try:
                patient_id, entries, summary = load_one(client, path, use_put=not args.post)
            except BundleError as exc:
                failures.append((path.name, str(exc)))
                print(f"[{index:4d}/{len(selected)}] FAIL {path.name[:44]:44} {exc}")
                continue
            except httpx.HTTPError as exc:
                failures.append((path.name, repr(exc)))
                print(f"[{index:4d}/{len(selected)}] FAIL {path.name[:44]:44} {exc!r}")
                continue

            resources += entries
            if patient_id:
                patient_ids.append(patient_id)
            print(
                f"[{index:4d}/{len(selected)}] {path.name[:44]:44} "
                f"{entries:5d} entries  {summary:12} {time.monotonic() - started:5.1f}s  "
                f"Patient/{patient_id}"
            )

    elapsed = time.monotonic() - run_started
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(patient_ids) + "\n")

    print(
        f"\nloaded {len(patient_ids)} patients / {resources:,} resources "
        f"in {elapsed:.0f}s ({elapsed / max(len(selected), 1):.1f}s per bundle)"
    )
    print(f"patient ids -> {args.out}")

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for name, reason in failures:
            print(f"  {name}: {reason}")


if __name__ == "__main__":
    main()
