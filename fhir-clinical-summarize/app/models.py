from pydantic import BaseModel, Field


class Fact(BaseModel):
    """One clinical fact, always traceable to the FHIR resource it came from."""

    display: str
    source: str = Field(description='FHIR reference, e.g. "Condition/1a2b"')
    status: str | None = None
    date: str | None = None


class Packet(BaseModel):
    patient_id: str
    conditions: list[Fact] = []
    medications: list[Fact] = []
    summary: str = ""
    missing: list[str] = []
