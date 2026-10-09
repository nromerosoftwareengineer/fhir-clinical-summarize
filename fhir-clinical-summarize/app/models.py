from pydantic import BaseModel, Field

# The identifier type code for a medical record number, from
# http://terminology.hl7.org/CodeSystem/v2-0203. It is the identifier a caller
# would realistically have, so it is the one we surface.
MRN_TYPE_CODE = "MR"


class Fact(BaseModel):
    """One clinical fact, always traceable to the FHIR resource it came from.

    The route serializes packets with exclude_defaults, so the optional fields
    below only appear when they carry information: no `status: null` noise, and
    no `count: 1` on the overwhelming majority of facts that were never
    deduplicated. Fields without a default (display, source) are always present.
    """

    display: str
    source: str = Field(description='FHIR reference, e.g. "Condition/1a2b"')
    status: str | None = None
    date: str | None = None
    count: int = Field(
        default=1,
        description="how many records collapsed into this fact; 60 cisplatin orders is "
        "clinically meaningful and would otherwise be lost to deduplication. Omitted "
        "when 1.",
    )


class Packet(BaseModel):
    """The deliverable: facts a reviewer can check, plus prose they can scan.

    None of these fields carry a default, so exclude_defaults never drops one.
    An empty `medications` list and an empty `summary` are both meaningful --
    "no medications on file" and "the model had nothing to say" are findings,
    not absences.
    """

    patient_id: str
    conditions: list[Fact]
    medications: list[Fact]
    summary: str
    missing: list[str]


class PatientSummary(BaseModel):
    """Enough of a Patient to pick one out of a list, plus its source reference."""

    id: str
    name: str | None = None
    family: str | None = None
    given: str | None = None
    birth_date: str | None = None
    gender: str | None = None
    mrn: str | None = Field(default=None, description="Medical record number, if the record has one")
    source: str = Field(description='FHIR reference, e.g. "Patient/1a2b"')

    @classmethod
    def from_resource(cls, resource: dict) -> "PatientSummary":
        """Build a summary from a FHIR Patient resource.

        Every lookup is defensive: real patient records are sparse, and a
        KeyError here would turn a usable record into a 500.
        """
        names = resource.get("name") or [{}]
        # Prefer the official name when the record carries several.
        name = next((n for n in names if n.get("use") == "official"), names[0])
        family = name.get("family")
        given = " ".join(name.get("given", [])) or None

        mrn = None
        for identifier in resource.get("identifier") or []:
            codings = identifier.get("type", {}).get("coding") or [{}]
            if codings[0].get("code") == MRN_TYPE_CODE:
                mrn = identifier.get("value")
                break

        return cls(
            id=resource["id"],
            name=" ".join(part for part in (given, family) if part) or None,
            family=family,
            given=given,
            birth_date=resource.get("birthDate"),
            gender=resource.get("gender"),
            mrn=mrn,
            source=f"Patient/{resource['id']}",
        )


class PatientPage(BaseModel):
    """One page of patient search results."""

    total: int = Field(description="total matches on the server, not just this page")
    count: int = Field(description="how many patients are in this page")
    offset: int
    patients: list[PatientSummary] = []
