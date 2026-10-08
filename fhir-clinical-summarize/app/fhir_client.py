"""Fetches resources from the HAPI FHIR server.

Thin wrapper over httpx: it knows how FHIR searches page, how to tell "no
results" from "no such patient", and which search parameters the server actually
supports. It deliberately returns raw FHIR dicts -- turning those into packet
facts is app/extract.py's job, so the two concerns stay separately testable.
"""

from __future__ import annotations

import httpx

from app.config import settings

# Result parameters that shape the response rather than filter it. They are not
# listed as search parameters in the CapabilityStatement, so they need their own
# allowance when validating caller input.
RESULT_PARAMS = frozenset({"_sort"})


class FhirError(Exception):
    """The FHIR server rejected a request or could not be reached."""


class FhirNotFound(FhirError):
    """The requested resource does not exist (or was deleted)."""


class FhirClient:
    """A client for one FHIR server."""

    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        self._client = httpx.Client(
            base_url=base_url or settings.fhir_base_url,
            timeout=timeout or settings.request_timeout_s,
            headers={"Accept": "application/fhir+json"},
        )
        # The supported search parameters per resource type, read from the
        # server's own CapabilityStatement on first use and cached. Asking the
        # server beats hardcoding a list that drifts when the server changes.
        self._search_params: dict[str, frozenset[str]] = {}

    def close(self) -> None:
        self._client.close()

    # -- low level ---------------------------------------------------------

    def _get(self, url: str, params: dict | list | None = None) -> dict:
        try:
            response = self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise FhirError(f"could not reach the FHIR server: {exc!r}") from exc

        if response.status_code in (404, 410):
            # 410 Gone is a deleted resource; both mean "not available" to a caller.
            raise FhirNotFound(f"{url} returned HTTP {response.status_code}")
        if response.is_error:
            raise FhirError(f"{url} returned HTTP {response.status_code}: {response.text[:300]}")

        return response.json()

    # -- capability --------------------------------------------------------

    def supported_search_params(self, resource_type: str) -> frozenset[str]:
        """The search parameter names this server accepts for a resource type."""
        if resource_type not in self._search_params:
            capability = self._get("/metadata")
            for resource in capability.get("rest", [{}])[0].get("resource", []):
                names = {param["name"] for param in resource.get("searchParam", [])}
                self._search_params[resource["type"]] = frozenset(names)
        return self._search_params.get(resource_type, frozenset())

    # -- reads and searches ------------------------------------------------

    def read(self, resource_type: str, resource_id: str) -> dict:
        """Read one resource by id. Raises FhirNotFound if it is not there."""
        return self._get(f"/{resource_type}/{resource_id}")

    def search(self, resource_type: str, params: dict | list) -> dict:
        """Run one search and return the raw searchset bundle (one page)."""
        return self._get(f"/{resource_type}", params=params)

    def search_all(self, resource_type: str, params: dict | None = None) -> list[dict]:
        """Run a search and follow paging links until every match is collected.

        _count is a hint, not a promise -- the server decides page size. Relying
        on a single large _count silently truncates results, so follow the
        "next" link the server supplies instead.
        """
        bundle = self.search(resource_type, {**(params or {}), "_count": 200})
        resources = [entry["resource"] for entry in bundle.get("entry", [])]

        while True:
            next_url = next(
                (link["url"] for link in bundle.get("link", []) if link.get("relation") == "next"),
                None,
            )
            if not next_url:
                return resources
            bundle = self._get(next_url)
            resources.extend(entry["resource"] for entry in bundle.get("entry", []))

    # -- convenience for the packet ---------------------------------------

    def get_patient(self, patient_id: str) -> dict:
        return self.read("Patient", patient_id)

    def get_conditions(self, patient_id: str) -> list[dict]:
        return self.search_all("Condition", {"patient": patient_id})

    def get_medications(self, patient_id: str) -> list[dict]:
        return self.search_all("MedicationRequest", {"patient": patient_id})


# One client for the process. FastAPI runs sync endpoints in a threadpool and
# httpx.Client is thread-safe, so a single pooled client is both correct and
# cheaper than building one per request.
fhir = FhirClient()
