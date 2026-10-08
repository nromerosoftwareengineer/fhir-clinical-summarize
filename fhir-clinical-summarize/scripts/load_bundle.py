"""Loads one Synthea transaction bundle into HAPI FHIR with client-assigned ids.

Synthea ships each bundle with every entry set to POST, which lets the server
pick the resource ids. We rewrite them to PUT so the ids in the file survive the
load. That matters because the packet cites resources by id ("Condition/abc123"),
and a citation that changes every time the data is reloaded is not much of a
citation.

Run from the repo root:

    python -m scripts.load_bundle data/fhir/Adolph80_Williamson769_2de9315e-....json

The helpers here are also used by scripts/load_synthea.py to load many bundles,
so they raise BundleError rather than exiting: one bad file should not end a
batch of several hundred.
"""

import argparse
import json
import pathlib
import sys
import time

import httpx

from app.config import settings

# Resource types worth counting after a load -- the ones the packet is built from.
VERIFY_TYPES = ("Patient", "Condition", "MedicationRequest", "Encounter", "Observation")


class BundleError(Exception):
    """A bundle could not be read, validated, or accepted by the server."""


def step(number: int, title: str) -> None:
    print(f"\n[{number}] {title}")


def check_server(client: httpx.Client) -> dict:
    """Confirm the FHIR server is reachable before sending it a few hundred KB."""
    response = client.get("/metadata")
    response.raise_for_status()
    return response.json()


def read_bundle(path: pathlib.Path) -> dict:
    """Parse and sanity-check a bundle file.

    At least one file in the Synthea sample set can arrive (or become) malformed,
    so a parse failure is reported in terms of the file rather than as a traceback.
    """
    try:
        bundle = json.loads(path.read_bytes())
    except json.JSONDecodeError as exc:
        raise BundleError(f"not valid JSON: {exc}") from exc

    if bundle.get("resourceType") != "Bundle":
        raise BundleError(f"is a {bundle.get('resourceType')}, not a Bundle")
    if not bundle.get("entry"):
        raise BundleError("has no entries")

    return bundle


def to_put_bundle(bundle: dict) -> dict:
    """Rewrite every entry from POST to PUT, using the id already on each resource.

    fullUrl is deliberately left alone: the urn:uuid: values are how entries
    reference each other inside the transaction, and the server resolves them
    against fullUrl. Changing them would break every internal reference.
    """
    for entry in bundle["entry"]:
        resource = entry["resource"]
        entry["request"] = {
            "method": "PUT",
            "url": f"{resource['resourceType']}/{resource['id']}",
        }
    return bundle


def validate(bundle: dict) -> None:
    """Check the rewrite before sending it; a bad transaction is rejected whole.

    Three things have to hold for the server to accept a client-assigned id and
    still resolve references correctly:
      - every resource carries an id
      - that id matches the entry's fullUrl, which references point at
      - the id is not purely numeric (HAPI reserves those for itself, HAPI-0960)
    """
    problems: list[str] = []

    for index, entry in enumerate(bundle["entry"]):
        resource = entry["resource"]
        resource_id = resource.get("id")
        full_url_id = entry.get("fullUrl", "").removeprefix("urn:uuid:")

        if not resource_id:
            problems.append(f"entry[{index}] {resource['resourceType']} has no id")
            continue
        if resource_id != full_url_id:
            problems.append(f"entry[{index}] id {resource_id} != fullUrl {full_url_id}")
        if resource_id.isdigit():
            problems.append(f"entry[{index}] id {resource_id} is numeric; server will reject it")

    if problems:
        summary = "; ".join(problems[:3])
        raise BundleError(f"{len(problems)} validation problem(s): {summary}")


def submit(client: httpx.Client, bundle: dict) -> dict:
    """POST the bundle to the base URL and return the transaction-response bundle.

    The HTTP verb here is POST even though the entries are PUTs: a FHIR
    transaction is always POSTed to the base URL, and each entry carries its own
    method. The whole bundle succeeds or fails together.
    """
    response = client.post(
        "",
        content=json.dumps(bundle).encode(),
        headers={"Content-Type": "application/fhir+json"},
    )
    if response.is_error:
        # HAPI explains rejections in an OperationOutcome body; it is far more
        # useful than the status code alone.
        raise BundleError(f"HTTP {response.status_code}: {response.text[:300]}")

    return response.json()


def status_counts(result: dict) -> dict[str, int]:
    """Tally the per-entry response statuses: 201 means created, 200 means updated."""
    counts: dict[str, int] = {}
    for entry in result.get("entry", []):
        status = entry.get("response", {}).get("status", "?")
        counts[status] = counts.get(status, 0) + 1
    return counts


def patient_id_from_result(result: dict) -> str | None:
    """Pull the Patient id out of a transaction-response bundle.

    Each entry's response.location looks like "Patient/<id>/_history/1".
    """
    for entry in result.get("entry", []):
        location = entry.get("response", {}).get("location", "")
        if location.startswith("Patient/"):
            return location.split("/")[1]
    return None


def verify(client: httpx.Client, patient_id: str | None) -> None:
    """Read the patient back at the id from the file, then count what landed."""
    if patient_id:
        response = client.get(f"/Patient/{patient_id}")
        kept = "yes" if response.status_code == 200 else f"no (HTTP {response.status_code})"
        print(f"    GET /Patient/{patient_id} -> client-assigned id kept: {kept}")

    for resource_type in VERIFY_TYPES:
        # HAPI caches search results, so a count taken right after a write can be
        # stale. no-cache plus _total=accurate forces a real count.
        response = client.get(
            f"/{resource_type}",
            params={"_summary": "count", "_total": "accurate"},
            headers={"Cache-Control": "no-cache"},
        )
        total = response.json().get("total", "?") if response.is_success else "error"
        print(f"    {resource_type:20} {total}")


def show_packet_facts(client: httpx.Client, patient_id: str) -> None:
    """Print the conditions and medications with their sources -- the packet, roughly."""
    for resource_type, label in (("Condition", "conditions"), ("MedicationRequest", "medications")):
        response = client.get(f"/{resource_type}", params={"patient": patient_id, "_count": 200})
        entries = response.json().get("entry", [])
        print(f"    {label} ({len(entries)}):")
        for entry in entries:
            resource = entry["resource"]
            if resource_type == "Condition":
                display = resource["code"]["coding"][0].get("display", "?")
                status = resource["clinicalStatus"]["coding"][0]["code"]
            else:
                display = resource["medicationCodeableConcept"]["coding"][0].get("display", "?")
                status = resource.get("status", "?")
            print(f"      {display[:60]:60} [{status:9}] {resource_type}/{resource['id']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", type=pathlib.Path, help="path to a Synthea bundle JSON file")
    parser.add_argument("--base", default=settings.fhir_base_url, help="FHIR base URL")
    parser.add_argument(
        "--post",
        action="store_true",
        help="leave entries as POST (server assigns ids) instead of rewriting to PUT",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="rewrite and validate, but send nothing"
    )
    args = parser.parse_args()

    if not args.path.is_file():
        sys.exit(f"no such file: {args.path}")

    # Transactions this size take a while; a generous timeout beats a retry loop.
    with httpx.Client(base_url=args.base, timeout=600.0) as client:
        try:
            step(1, "Checking the server")
            capability = check_server(client)
            software = capability["software"]
            print(f"    {software['name']} {software['version']}")
            print(f"    FHIR version {capability['fhirVersion']}  at {client.base_url}")

            step(2, "Reading the bundle")
            bundle = read_bundle(args.path)
            print(f"    {args.path.name}")
            print(
                f"    {len(bundle['entry'])} entries, bundle type '{bundle.get('type')}', "
                f"{args.path.stat().st_size / 1000:.0f} KB"
            )

            step(3, "Leaving entries as POST" if args.post else "Rewriting entries")
            if args.post:
                print("    entries unchanged; the server will assign ids")
            else:
                to_put_bundle(bundle)
                validate(bundle)
                print(f"    {len(bundle['entry'])} entries rewritten to PUT, all ids check out")
                for entry in bundle["entry"][:3]:
                    print(f"      {entry['request']['method']} {entry['request']['url']}")

            if args.dry_run:
                print("\n[4] Dry run - nothing sent")
                return

            step(4, "Sending the transaction")
            started = time.monotonic()
            result = submit(client, bundle)
            print(f"    accepted in {time.monotonic() - started:.1f}s")

            step(5, "Per-entry results")
            for status, count in sorted(status_counts(result).items()):
                label = "created" if status.startswith("201") else "updated"
                print(f"    {count:4d} x {status}  ({label})")

            patient_id = patient_id_from_result(result)

            step(6, "Verifying")
            verify(client, patient_id)

            if patient_id:
                step(7, "Packet facts")
                show_packet_facts(client, patient_id)
                print(f"\nDone. Patient id: {patient_id}")

        except BundleError as exc:
            sys.exit(f"\n{args.path.name}: {exc}")


if __name__ == "__main__":
    main()
