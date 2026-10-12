"""One cached summary per patient, invalidated by a hash of the patient's facts.

Inference is the whole cost of a packet. On the busiest patient in the sample
set the FHIR fetch and the deterministic shaping take 0.21 s and the model takes
31 s, so a cache hit removes 99% of the wait. The citations and facts are still
assembled fresh on every request -- they are the deliverable, and the hash cannot
be computed without them -- so what is cached is only ever the prose.

**What the hash covers.** The shaped prompt payload from
``extract.to_prompt_facts``, and nothing else. Not the raw FHIR resources: those
carry ``meta.lastUpdated`` and ``meta.versionId``, which HAPI rewrites whenever a
bundle is reloaded, so a raw hash would report "changed" on reloads where nothing
clinically moved. The shaped payload is the same data with the bookkeeping
stripped and the deduplication already applied, so hash equality means what it
should: this patient's clinical picture is the one the stored summary describes.

The lists are sorted before hashing. ``extract._newest_first`` is a stable sort
over an unsorted FHIR result, so facts sharing a date keep whatever order the
server returned; sorting makes a reindex that reorders ties a cache hit rather
than a pointless regeneration.

**What the hash deliberately does not cover: the model.** Switching
``OLLAMA_MODEL`` does not invalidate anything, so after a switch each patient
keeps the prose the previous model wrote until their data changes. That is the
intended trade -- data changes constantly, models change when an operator
decides -- and ``scripts/cache_admin.py --clear`` is the lever for the rare case.
The model that wrote each row is recorded in a column so a surprising summary
can be traced to its author.

**There is no per-request bypass.** The cache is consulted on every packet, and a
caller who needs fresh prose says so by invalidating the patient rather than by
asking the route to skip the lookup. One code path means a cache hit and a cache
miss are the same request, differing only in what the database had.

**Every failure here is non-fatal.** A database that is down, a table that will
not create, a write that is rejected: each logs a warning and the packet is
served with a freshly generated summary. The cache is an optimisation, and an
optimisation that can take the service down with it is a liability. This mirrors
app/summarize.py, where a model failure costs the prose and never the packet.
"""

import hashlib
import json
import logging
import time

import psycopg

from app import summarize
from app.config import settings

logger = logging.getLogger(__name__)

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS summary_cache (
    patient_id  TEXT PRIMARY KEY,
    fact_hash   TEXT NOT NULL,
    summary     TEXT NOT NULL,
    model       TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    latency_ms  INTEGER
)
"""

# patient_id is the primary key, so this only ever touches one row per patient
# and the table cannot grow beyond the patient count.
UPSERT_SQL = """
INSERT INTO summary_cache (patient_id, fact_hash, summary, model, latency_ms)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (patient_id) DO UPDATE SET
    fact_hash  = EXCLUDED.fact_hash,
    summary    = EXCLUDED.summary,
    model      = EXCLUDED.model,
    latency_ms = EXCLUDED.latency_ms,
    updated_at = now()
"""

LOOKUP_SQL = "SELECT summary FROM summary_cache WHERE patient_id = %s AND fact_hash = %s"


def enabled() -> bool:
    """Whether a cache is configured at all. Unset SUMMARY_DB_URL means no database."""
    return bool(settings.summary_db_url)


def fact_hash(prompt_facts: dict) -> str:
    """A stable digest of the patient's clinical picture as the model will see it.

    Pure and database-free, so it is unit-testable without Postgres: the same
    facts always produce the same digest, a reordering never changes it, and any
    clinical change always does.
    """
    canonical = {key: sorted(values) for key, values in sorted(prompt_facts.items())}
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _connect() -> psycopg.Connection:
    """A short-lived connection, opened per operation.

    Not a module-level connection like the httpx clients: a long-lived one goes
    stale across a Postgres restart or an idle timeout, and reconnect logic is
    not worth writing for something that costs about a millisecond locally
    against the seconds a hit saves.
    """
    return psycopg.connect(
        settings.summary_db_url,
        connect_timeout=int(settings.summary_db_timeout_s),
        autocommit=True,
    )


def ensure_schema() -> None:
    """Create the table if it is missing. Called once at startup, never required.

    The *database* has to exist already -- deploy/init-summaries.sql creates it
    on a fresh volume, and an existing volume needs it created by hand once,
    because the postgres image only runs its init hooks on an empty data
    directory. Only the table is managed here, which is why no migration tool is
    involved.
    """
    if not enabled():
        logger.info("summary cache disabled: SUMMARY_DB_URL is unset")
        return

    try:
        with _connect() as conn:
            conn.execute(TABLE_DDL)
        logger.info("summary cache ready")
    except (psycopg.Error, OSError) as exc:
        logger.warning("summary cache unavailable, packets will not be cached: %r", exc)


def status() -> str:
    """One line for /ready. Never raises."""
    if not enabled():
        return "disabled"
    try:
        with _connect() as conn:
            conn.execute("SELECT 1 FROM summary_cache LIMIT 1")
        return "ok"
    except (psycopg.Error, OSError) as exc:
        return f"unavailable: {exc!r}"


def _lookup(patient_id: str, digest: str) -> str | None:
    """The stored summary if the patient's facts still hash to `digest`, else None."""
    try:
        with _connect() as conn:
            row = conn.execute(LOOKUP_SQL, (patient_id, digest)).fetchone()
    except (psycopg.Error, OSError) as exc:
        logger.warning("summary cache read failed, generating fresh: %r", exc)
        return None

    return row[0] if row else None


def _store(patient_id: str, digest: str, summary: str, latency_ms: int) -> None:
    try:
        with _connect() as conn:
            conn.execute(
                UPSERT_SQL, (patient_id, digest, summary, settings.ollama_model, latency_ms)
            )
    except (psycopg.Error, OSError) as exc:
        logger.warning("summary cache write failed, summary still served: %r", exc)


def invalidate(patient_id: str) -> None:
    """Drop this patient's cached summary, so the next request regenerates it.

    Used by scripts/eval_sample.py, which must score the model rather than
    whatever is stored: it invalidates each patient before requesting their
    packet, which forces a regeneration through the ordinary request path
    instead of giving the route a bypass it would otherwise never use.
    """
    if not enabled():
        return
    try:
        with _connect() as conn:
            conn.execute("DELETE FROM summary_cache WHERE patient_id = %s", (patient_id,))
    except (psycopg.Error, OSError) as exc:
        logger.warning("summary cache invalidate failed: %r", exc)


def get_or_write(prompt_facts: dict, patient_id: str) -> str:
    """Return this patient's summary, from cache when their facts are unchanged.

    The cache is always consulted: a row for this patient whose fact_hash still
    matches is returned as-is, and anything else -- no row, or a row from a
    different clinical picture -- generates fresh prose and upserts it under the
    new hash. There is no per-request opt-out, because a caller who wants the
    model rather than the cache wants the patient's data to look new, which is
    what invalidate() says.
    """
    if not enabled():
        return summarize.write_summary(prompt_facts)

    digest = fact_hash(prompt_facts)
    cached = _lookup(patient_id, digest)
    if cached is not None:
        logger.info("summary cache hit", extra={"patient_id": patient_id})
        return cached

    started = time.monotonic()
    summary = summarize.write_summary(prompt_facts)
    latency_ms = int((time.monotonic() - started) * 1000)

    # An empty summary is either "nothing to summarise", which costs nothing to
    # recompute, or a model failure, which must not be persisted -- caching a
    # transient Ollama outage would outlive the outage.
    if summary:
        _store(patient_id, digest, summary, latency_ms)

    return summary
