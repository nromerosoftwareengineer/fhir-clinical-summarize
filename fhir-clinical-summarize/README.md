# fhir-clinical-summarize

Assembles a source-cited clinical packet from a FHIR server, summarized by a local LLM.

Before a prior-authorization request can be reviewed, someone has to pull the patient's
history together out of scattered records. This service does that assembly: it reads a
patient's conditions and medications from a FHIR server, turns them into facts that each
carry the id of the record they came from, and asks a local 3B model to write a few sentences
a reviewer can scan.

**Every fact is traceable.** A reviewer can take any `source` in the response and read the
original record:

```
"display": "Hypertension",  "source": "Condition/4821"
                                       └─> GET /fhir/Condition/4821
```

**This service assembles evidence. It never issues an authorization decision.**

---

## Data flow

```mermaid
flowchart LR
    S[("Synthea<br/>1,180 bundles")]
    H[("HAPI FHIR 8.12<br/>+ Postgres<br/>(Docker)")]
    C["fhir_client.py<br/>search + paging"]
    E["extract.py<br/>facts · sources · dedupe"]
    L["summarize.py<br/>gemma3:4b via Ollama<br/>JSON schema output"]
    K["link.py<br/>prose → facts<br/>exact, deterministic"]
    P["GET /patients/{id}/packet"]

    S -->|"load_synthea.py<br/>PUT transaction"| H
    H -->|"Patient · Condition<br/>MedicationRequest"| C
    C --> E
    E -->|"facts + sources<br/>(deterministic)"| P
    E -->|"display strings only,<br/>no sources"| L
    L -->|"summary prose"| K
    E -->|"facts + sources"| K
    K -->|"citations<br/>(offsets + source)"| P
```

The split in the middle is the point of the design. Facts, statuses, dates and source
references are settled by deterministic Python before the model is called, and the model's
output is only ever assigned to `summary`. The model never sees a resource id, so it cannot
fabricate a citation, and an LLM failure costs the prose rather than the evidence.

`link.py` is what closes the loop: it reads the prose the model just wrote, matches terms in
it against facts that already carry their sources, and returns character offsets. So the
sentences are cited too, not just the tables — and still without the model ever emitting a
reference.

---

## Quickstart

Requires Docker (or Colima), Python 3.11+, and [Ollama](https://ollama.com).

```bash
# 1. FHIR server
docker compose up -d
until curl -sf http://localhost:8080/fhir/metadata >/dev/null; do sleep 5; done

# 2. Local model
ollama pull gemma3:4b

# 3. Python environment
python -m venv .venv && ./.venv/bin/pip install -e ".[dev]"

# 3b. Summary cache (optional -- skip it and every packet generates fresh).
# The database is created automatically on a fresh volume; an existing one
# needs it once by hand, because postgres only runs init hooks when empty.
docker compose exec db psql -U hapi -d postgres -c "CREATE DATABASE summaries OWNER hapi;"
export SUMMARY_DB_URL="postgresql://hapi:hapi@localhost:55432/summaries"

# 4. Synthetic data (1.3 GB, gitignored -- not in this repo)
mkdir -p data && cd data
curl -LO https://synthetichealth.github.io/synthea-sample-data/downloads/synthea_sample_data_fhir_r4_sep2019.zip
unzip -q synthea_sample_data_fhir_r4_sep2019.zip && cd ..

# 5. Load a reproducible sample of patients
./.venv/bin/python -m scripts.load_synthea -n 300

# 6. Run the service
./.venv/bin/uvicorn app.main:app --reload
```

Then open **http://localhost:8000/ui/** for the dashboard, or
**http://localhost:8000/docs** for the API.

```bash
# find a patient
curl -s "localhost:8000/patients?family=Runte676" | jq .

# build their packet
curl -s "localhost:8000/patients/632b7c1c-9545-4aa7-9fd5-07683139ef13/packet" | jq .

# verify a citation by hand -- what a reviewer would do
curl -s "localhost:8080/fhir/Condition/ce034b73-37e8-489c-bd6f-9c52a08ff605" | jq .

# or follow the prose: every underlined term in the summary carries its source
curl -s "localhost:8000/patients/78378d37-92b5-4d2e-8f6c-b8f5e79f3999/packet" \
  | jq -r '.citations[] | "\(.text)  ->  \(.source)"'
```

---

## API

| Endpoint | |
|---|---|
| `GET /health` | liveness |
| `GET /patients` | search by **any** FHIR `Patient` search parameter |
| `GET /patients/{id}` | one patient summary |
| `GET /patients/{id}/packet` | **the deliverable** — facts, sources, summary, citations, missing |
| `GET /ui/` | React dashboard |
| `GET /docs` | OpenAPI |

### Search is validated against the server, not a hardcoded list

`GET /patients` forwards any subset of parameters after checking them against the FHIR
server's own `CapabilityStatement`, so all 34 parameters this HAPI instance supports work,
and an unknown one fails with the valid list rather than confusing the server downstream:

```bash
curl "localhost:8000/patients?address-city=Boston"              # 24 matches
curl "localhost:8000/patients?gender=female&birthdate=ge1980-01-01"
curl -G localhost:8000/patients \
  --data-urlencode "identifier=http://hospital.smarthealthit.org|792be1e3-..."   # by MRN
```

```jsonc
// GET /patients?lastname=Smith  ->  400
{
  "detail": {
    "message": "unsupported search parameter(s): lastname",
    "supported": ["_id", "active", "address", "address-city", "birthdate", "family",
                  "gender", "given", "identifier", "name", "organization", ...]
  }
}
```

### Example packet

```json
{
  "patient_id": "3f8be6b0-15a6-43f4-87f3-1adb737fa598",
  "conditions": [
    { "display": "Viral sinusitis (disorder)",
      "source": "Condition/04a5822d-8a5c-4f0c-a900-035574d639cb",
      "status": "resolved", "date": "2018-02-11" }
  ],
  "medications": [
    { "display": "Acetaminophen / Dextromethorphan / doxylamine Oral Solution",
      "source": "MedicationRequest/7371bd7b-0b35-42a1-8b1d-30a3565a5189",
      "status": "stopped", "date": "2012-08-14" }
  ],
  "summary": "The patient has a history of viral sinusitis and acute bronchitis, but currently has no active conditions. The patient has previously taken acetaminophen for symptom relief...",
  "citations": [
    { "start": 29, "end": 44, "text": "viral sinusitis",
      "source": "Condition/04a5822d-8a5c-4f0c-a900-035574d639cb" },
    { "start": 160, "end": 173, "text": "acetaminophen",
      "source": "MedicationRequest/7371bd7b-0b35-42a1-8b1d-30a3565a5189" }
  ],
  "missing": ["No active conditions on file", "No active medications on file"]
}
```

---

## Technical decisions

### Resource ids are client-assigned (`PUT`, not `POST`)

Synthea ships each bundle with every entry set to `POST`, which lets the server pick ids.
`scripts/load_bundle.py` rewrites them to `PUT <Type>/<id>` using the id already on each
resource. Three consequences:

- **Citations survive a reload.** With server-assigned ids, re-loading the data renumbers
  everything and every previously emitted `source` points at the wrong record. A citation
  that breaks when the database is rebuilt is a weak citation.
- **Loading is idempotent.** First run returns `201 Created` for every entry, a second run
  returns `200 OK` and the patient count does not change.
- **Shared resources deduplicate for free.** Synthea derives `Organization` ids
  deterministically, so the same hospital `PUT`s to the same id across bundles. 301 patients
  produced **370 organizations** rather than one per bundle.

`fullUrl` is deliberately left untouched — the `urn:uuid:` values are how entries reference
each other inside the transaction, and the server resolves references against them.

One constraint: HAPI rejects purely numeric client-assigned ids (`HAPI-0960`), reserving
them for itself. Synthea's UUIDs are unaffected.

### Two deduplication rules, because repetition means two different things

| | Key | Why |
|---|---|---|
| Medications | `(display, status)` | Repetition is repeat dosing — one `MedicationRequest` per chemo cycle |
| Conditions | `(display, status, date)` | Repetition is separate episodes |

The condition rule exists because of a specific patient: Adolph has **viral sinusitis in
2014 and again in 2018**. Those are two illnesses, and a `(display, status)` key silently
deletes one of them from the record. The surviving fact keeps the newest record's source and
a `count` of how many collapsed, so 60 cisplatin cycles stay visible as `count: 60` rather
than being thrown away.

Packets are serialized with `exclude_defaults`, so a `Fact` carries only the fields that say
something: `display` and `source` always, `status` and `date` when present, and `count` only
when records were actually collapsed. `Packet`'s own fields have no defaults, so an empty
`medications` list and an empty `summary` survive — "no medications on file" and "the model
returned nothing" are both findings, not absences.

### Prompt shaping — measured, not guessed

The busiest loaded patient has 14 conditions and **164 medication orders** (which are 11
distinct drugs: 60 cisplatin, 60 paclitaxel, 36 simvastatin).

| Payload sent to the model | ~Tokens |
|---|---|
| Raw FHIR from the two searches | **45,000** |
| One `Fact` per resource | 5,000 |
| After deduplication | 774 |
| Split into active / past | **460** |

Deduplication is not an optimization here, it is a correctness requirement: **Ollama's
default `num_ctx` is 4096 and it truncates silently** rather than erroring, so the
undeduplicated payload would have produced a confident summary of partial data.
`num_ctx` is set explicitly to 8192.

**Active and past are split in Python, not left to the model.** Given a flat list with
`status` fields, an early run returned a comma-separated dump — `"Stroke (active),
Osteoporosis (active), ..."`. Given pre-grouped lists and an explicit "do not output a
list", it writes prose. Grouping is trivial in Python and unreliable in a small model.

**Active facts carry their onset date** (`"Obesity (since 1997-08-26)"`). One patient has a
condition still flagged `active` with a **1990** onset — a Synthea artifact, but a model
given only the status writes it up as a present-day problem.

### `missing` is computed in code

Asked to report an absence, a small model will sometimes report a presence. The deterministic
layer emits `"No medications on file"` / `"No active conditions on file"`.

### Citations are matched, never generated

The prose is the one part of a packet a model wrote, so the links out of it are resolved the
same way as everything else: deterministically, afterwards, from facts that already carry
their sources. `app/link.py` offers each fact a few names a summary might use for it, finds
those names in the text, and returns `{start, end, text, source}` per mention.

The names come from how the two code systems actually write a display, measured against the
summaries in `eval_output/`:

| Record | Terms offered |
|---|---|
| `Viral sinusitis (disorder)` | the SNOMED semantic tag is dropped |
| `Body mass index 30+ - obesity (finding)` | **both** halves; one code naming two things |
| `Non-small cell carcinoma of lung, TNM stage 1` | the head too, since prose drops staging |
| `Sertraline 100MG` | the drug name without its strength |
| `Acetaminophen 325 MG / HYDROcodone Bitartrate 7.5 MG` | each ingredient, and each one's first word |
| `NDA020800 0.3 ML Epinephrine 1 MG/ML Auto-Injector` | the product code **and** `Epinephrine` |
| `Yaz 28 Day Pack` | the brand alone |

Matching is case-insensitive (RxNorm ships tall-man casing — `PACLitaxel`, `DOCEtaxel` — that
no summary reproduces), tolerates a hyphen where the record has a space (`third-degree burn`
vs `Third degree burn`), an article the display does not carry (`fracture of the ankle` vs
`Fracture of ankle`), and an English plural (`normal pregnancies` vs `Normal pregnancy`).

**What it deliberately will not do is guess.** `prostate neoplasm` and the record's
`Neoplasm of prostate` are the same condition, and linking them means reordering words;
`Simvastatin` in prose against `Simvistatin 10 MG` on file is a one-letter difference that
could as easily be a different drug. Both are left as plain text. An unlinked word costs a
reviewer a glance at the table below; a wrong link costs them the review.

Two ambiguities had to be resolved rather than avoided, because the sample set contains both:

- **Two episodes of one illness** share every term, so every mention cites the newest — the
  same record `_dedupe` kept the source of.
- **One ingredient in two products** (a plain acetaminophen tablet and an
  acetaminophen/oxycodone tablet) means successive mentions cite successive products, so the
  sentence "Acetaminophen, Ibuprofen, and Acetaminophen / oxyCODONE" cites each record once
  instead of citing the tablet twice.

Spans are recomputed on every request, including cache hits, rather than stored next to the
prose. They are a pure function of this summary and these facts, cost well under a
millisecond, and keeping them derived means there is no second artefact that can drift out of
step with the text it points into.

### Failure modes degrade rather than propagate

- `summarize.write_summary()` **never raises.** Timeout, malformed JSON, or schema violation
  all return `""`, and the packet ships with facts and sources intact.
- A missing patient is a `404`; an unreachable or broken FHIR server is a `502`. Those are
  different failures and a caller needs to tell them apart.
- `_summary=count` requests send `Cache-Control: no-cache` and `_total=accurate`, because
  HAPI caches search results and returns stale counts immediately after a write.

### Infrastructure

- **HAPI image pinned** to `hapiproject/hapi:v8.12.0-2` so the demo cannot change underneath
  it. The image is distroless (no shell, no curl), so readiness is polled from outside via
  `GET /fhir/metadata` rather than with a container healthcheck. Both it and `postgres:14`
  publish native `arm64`, so there is no emulation on Apple silicon.
- **Postgres rather than the default H2**, so the loaded dataset survives a restart.
- **The dashboard is served by FastAPI itself** at `/ui`, same-origin with the API. That
  avoids CORS entirely, and avoids the mixed-content block a hosted `https://` page would hit
  calling a local `http://` backend. React and Babel load from a CDN, so there is no build
  step and no `node_modules` in the repo.

---

## Model choice

**`gemma3:4b`** (Google, 3.9B parameters, 3.4 GB, Q4_K_M) served by Ollama, using
[structured outputs](https://ollama.com/blog/structured-outputs) — a JSON schema in the
`format` field — so the reply is always parseable rather than prose to be scraped.
`temperature: 0` so the same packet summarizes identically twice, which matters for both the
hand review and for demoing under questioning.

Reasons a small local model fits the job:

- The task after shaping is **writing, not reasoning**. The hard decisions — what is active,
  what deduplicates, what is missing — are made in Python. The model is being asked to turn
  four short lists into two to four sentences.
- **~460 prompt tokens** means a small model is the right one; nothing here benefits from a
  larger context or deeper reasoning. It advertises a 131,072-token context against
  Ollama's 4096 default, so `num_ctx` is set explicitly rather than inherited.
- It runs alongside HAPI and Postgres on an 8 GB laptop, with no paid API.

What is measured so far, same facts through `write_summary`, Apple M2 / 8 GB:

| | measured |
|---|---|
| Warm call | 3.8 s |
| Cold call (model load) | 27.7 s |
| Resident at `num_ctx=8192` | 3.6 GB |
| Placement | 22% / 78% CPU/GPU |

Two costs worth naming, both measured rather than assumed:

- At `num_ctx=8192` it no longer fits entirely in Metal alongside HAPI and Postgres, so 22%
  of it runs on CPU. It is still fast warm, but it is no longer GPU-resident.
- It **failed one mechanical check** on the 10-patient review: a single-sentence answer where
  the prompt requires two. See [Hand review of 10 patients](#hand-review-of-10-patients) —
  9/10 mechanical, with sources resolving 126/126.

One claim worth retracting, since it drove the decision to look harder: on a *synthetic*
one-medication payload the model turned `"Lisinopril 10 mg"` into "prescribed Lisinopril
10 mg **daily**", a frequency nothing upstream emits. It reproduces on that payload, but it
**did not recur anywhere in the 10-patient run** — zero invented frequencies and zero
ungrounded doses on real records. `eval_sample.py` checks for it on every run, so a
regression would be caught rather than assumed absent.

---

## Summary cache

Inference is the entire cost of a packet. On the busiest patient in the sample set
(`5e4b559d`, 26 facts) the FHIR fetch and all the deterministic shaping take **0.21 s** and
the model takes **31 s**. So one summary per patient is cached in Postgres, invalidated by a
hash of that patient's facts:

| | measured |
|---|---|
| Cache hit (warm) | **0.7–0.9 s** |
| Generate + store | 8–40 s, depending on payload size and whether the model is resident |
| Unavoidable per request (FHIR + shaping) | 0.21 s |

**What the hash covers:** the shaped prompt payload from `to_prompt_facts`, and nothing else.
Not the raw FHIR resources — those carry `meta.lastUpdated` and `meta.versionId`, which HAPI
rewrites whenever a bundle is reloaded, so a raw hash would report "changed" on reloads where
nothing clinically moved. The lists are sorted before hashing, because `_newest_first` is a
stable sort over an unsorted FHIR result and the order of same-date facts is not a clinical
fact.

**What it deliberately does not cover: the model.** Switching `OLLAMA_MODEL` invalidates
nothing, so each patient keeps the previous model's prose until their data changes. Data
changes constantly; the model changes when an operator decides. `python -m
scripts.cache_admin --clear` is the lever for that case, and the model that wrote each row is
recorded in a column so a surprising summary can be traced to its author.

**Every failure is non-fatal.** Database down, table missing, write rejected: each logs a
warning and the packet is served with a freshly generated summary. Verified by pointing
`SUMMARY_DB_URL` at a dead port — two warnings, summary still produced. An optimisation that
can take the service down with it is a liability, which is the same rule `summarize.py`
already follows for the model.

Separate database, same Postgres container. HAPI owns 60 tables in its own database and runs
its own migrations on every startup; a separate database is a hard boundary for free, and
sharing the volume means `docker compose down -v` wipes cache and records together — correct,
since cached prose must never outlive the records it describes.

**The cache is always consulted; there is no per-request opt-out.** A row for this patient
whose `fact_hash` still matches is returned as-is, and anything else -- no row, or a row from
a different clinical picture -- generates fresh prose and upserts it under the new hash. A
caller who wants the model rather than the cache wants the patient's data to look new, which
is what `summary_cache.invalidate()` says: `eval_sample.py` calls it per patient before
requesting their packet, so a run measures the model through the ordinary request path rather
than through a bypass the route would otherwise never use.

> **Note for this machine:** the container publishes Postgres on **55432**, not 5432. A
> native Postgres already owns the host's 5432 here and answers before Colima's forward does,
> so a host client aiming at 5432 authenticates against the wrong server and fails with
> `password authentication failed for user "hapi"` while HAPI itself is perfectly happy on
> the internal network.

---

## Demo patients

Ten loaded patients chosen to cover every branch in the deterministic layer — ages 7 to 115,
empty to dense charts, allergy-heavy, zero-meds, everything-resolved, and the deduplication
extreme. `id` is what `GET /patients/{id}/packet` takes; MRN is the Synthea `MR` identifier
inside the bundle. Counts are read from the bundles, not from a summary.

| # | Name | Patient id | MRN | Age / sex | Cond (act/past) | Med orders → distinct | Allergies |
|---|---|---|---|---|---|---|---|
| 1 | Ardelia Larson | `664632b2-1503-42f3-921d-2fa78912f35c` | `68b95ae3-8732-4948-9345-f3d490bc4ae5` | 7 F | 0 / 0 | 0 → 0 | 0 |
| 2 | Joaquin Ortiz | `cbe3e2d8-dc40-40f1-ac21-c7ab36f4bdb8` | `ddd7655c-895e-4024-8938-77324122f34b` | 19 M | 0 / 10 | 10 → 6 (0 active) | 0 |
| 3 | Kerri Reilly | `f643310d-5ca3-4d9b-8fc6-0cad519f761d` | `4c00db79-84b6-4915-aaf7-b4fbdc164858` | 24 F | 2 / 3 | 7 → 7 | **10** |
| 4 | Adolph Williamson | `3f8be6b0-15a6-43f4-87f3-1adb737fa598` | `2de9315e-3784-4802-944a-926bcd8febd4` | 33 M | 0 / 3 | 1 → 1 (0 active) | 0 |
| 5 | María Soledad Sanches | `7f89f693-7205-4bd6-bba1-a2645876ecee` | `a8148b4d-2f27-4192-9d82-0d089f830c4a` | 45 F, deceased | 4 / 4 | 15 → 15 | 0 |
| 6 | Ernestina Donnelly | `78378d37-92b5-4d2e-8f6c-b8f5e79f3999` | `a1e2a560-efee-4735-8d42-d7132e55d1fe` | 50 F | 6 / 4 | 12 → 9 | 7 |
| 7 | Otis Johns | `1691a21e-867b-44f2-8e22-00013398cb7b` | `18f154cc-ec1c-427c-9ecc-48bae325c4d6` | 70 M | 3 / 1 | 0 → 0 | 0 |
| 8 | Coleman Huel | `8143897c-e650-4e55-b08d-8306e2f424bb` | `ad7f38c5-8a53-4a6c-9b6d-7ad0637164d7` | 95 M | **18** / 6 | 14 → 13 (11 active) | 0 |
| 9 | Antonio Mayer | `5e4b559d-6999-4c91-9256-7c9845d6e167` | `89b6a6af-8f08-4341-9dad-e42eb45308cd` | 102 M, deceased | 10 / 4 | **164 → 11** | 0 |
| 10 | Rory Bauch | `a4b0697c-4c06-41f0-ada5-d17517850127` | `3c2086bb-32e7-4d82-ba8d-fd981a57123f` | 115 M, deceased | 11 / 0 | 6 → 6 | 0 |

What each one is for:

1. **Ardelia Larson (7)** — a completely empty chart. The only patient that fires every
   `missing` note at once, and proof the service returns facts with a blank summary rather
   than an error.
2. **Joaquin Ortiz (19)** — 10 conditions, all resolved; 10 medication orders, none active.
   The active/past split has to be right or the packet reads as a sick teenager.
3. **Kerri Reilly (24)** — 10 allergies, the cohort maximum. The risk lives in the allergy
   list, not the problem list.
4. **Adolph Williamson (33)** — one of the three edge cases pinned in `eval_sample.py`:
   nothing active at all.
5. **María Soledad Sanches (45)** — non-ASCII name, deceased, 15 distinct drugs with no
   repetition to collapse. Encoding-safe end to end.
6. **Ernestina Donnelly (50)** — the ordinary packet: 6 active problems, 7 allergies, 12
   orders deduping to 9. The one to click through to `source` on.
7. **Otis Johns (70)** — active problems, zero medications: the literal
   `"No medications on file"` case.
8. **Coleman Huel (95)** — 18 active conditions, 11 active drugs, a 6.7 MB bundle. Worst-case
   prompt that still has to fit `num_ctx`.
9. **Antonio Mayer (102)** — 164 medication orders collapsing to 11 distinct drugs. The
   deduplication argument in one request.
10. **Rory Bauch (115)** — oldest in the cohort, deceased, 11 active conditions and nothing
    resolved. The date/age counterpart to Ardelia.

Nine are in `data/patient_ids.txt`; `3f8be6b0-…` is one of the three pinned edge patients
that are on the server but outside that file (the 301-vs-299 gap).

---

## Results and observations

### Measured on this machine (Apple M2, 8 GB, Colima 4 CPU / 8 GB)

| | |
|---|---|
| Dataset | 1,180 bundles, 1.34 GB JSON, ~629,000 FHIR resources |
| Loaded | **301 patients** → 2,185 conditions, 2,356 medication requests, 53,785 observations |
| Postgres size | **832 MB** for 301 patients (~2.8 MB/patient → ~3.3 GB for all 1,180) |
| Load throughput | ~2.1 s per bundle → the full 1,180 would take roughly 40 minutes |
| Packet latency | **3–8 s** warm, ~28 s on the first call while `gemma3:4b` loads |
| Tests | 73, all deterministic (no FHIR server, no model) |

A representative sample was loaded rather than all 1,180: the hand review covers 10
patients, the endpoint serves one at a time, and clinical variety saturates well before 300.
`scripts/load_synthea.py -n 1180` runs the full set if wanted. The sample is drawn with a
fixed seed over a sorted file list, so the same `-n` and `--seed` reproduce the same cohort
on any machine — otherwise the review numbers below would mean nothing.

### Findings from the data

- **Status is the main correctness risk.** Three of the first patients inspected had
  *everything* resolved or stopped. A summary that reports "patient has bronchitis and
  sinusitis" for someone whose infections cleared in 2012 is wrong in the way that matters.
  This drove `status` into the prompt, the active/past split, and the `missing` entries.
- **`clinicalStatus: active` does not mean clinically current.** One patient carries an
  `active` "Miscarriage in first trimester" with a 1990 onset. FHIR's `active` means the
  problem-list entry is open, not that the event is ongoing. Dates are in the prompt for
  this reason; weighting by recency is left as future work rather than decided silently.
- **Repetition is extreme.** 164 medication orders → 11 distinct drugs. Any system reading
  this data naively will blow its context window on duplicates.
- **`MedicationRequest.reasonReference` links a prescription to the condition it was written
  for.** For prior authorization that linkage *is* the decision — the reviewer's question is
  rarely "does the patient take this drug" but "is there a documented indication." Not yet
  surfaced in the packet; see below.
- **A malformed bundle in the working copy was skipped cleanly** by the loader rather than
  ending the run. Partial-failure handling is not speculative defensiveness here.

### Hand review of 10 patients

`scripts/eval_sample.py` runs a pinned sample through the packet endpoint, scores what a
string comparison can settle, and stages the rest on a worksheet with a blank verdict line
per patient. There is deliberately **no LLM judge**: grading the prose with a second model
would undercut the one claim this project makes.

The sample is three pinned edge cases plus a seeded fill, so the cohort is identical from
run to run. All three edge patients are on the server but absent from
`data/patient_ids.txt`, which is the 301-vs-299 gap — they are pinned by full id:

| patient | edge it covers | verified |
|---|---|---|
| `3f8be6b0-…` | every condition and medication resolved/stopped | 0 active, 3 past conditions, 1 past med |
| `632b7c1c-…` | active conditions, **zero** medications | 2 active, 0 meds, `missing` fires |
| `5e4b559d-…` | deduplication under load | 14 conditions, **164 med orders → 12 distinct** |

Mechanically checked: every `Fact.source` is **read back from FHIR** and re-extracted to
confirm it still carries the `display` the packet cites it for; doses in the prose must
appear in the payload; dosing *frequencies* must not appear at all, since nothing upstream
emits one; no approve/deny/authorize language; two to four sentences, no bullets.

**Results, 10 patients, `gemma3:4b` at `temperature: 0`:**

| | result |
|---|---|
| Sources resolved | **126/126** |
| Passed every mechanical check | **9/10** |
| Invented doses / frequencies | 0 |
| Decision language | 0 |
| Active conditions framed as history *(flagged for review)* | 7/10 patients |

The single mechanical failure is `a166eeaa-…`, which answered in **one** sentence where the
prompt requires two, and framed the one active condition as history:

> *"The patient presents with a history of chronic sinusitis dating back to March 8, 2001,
> alongside a past medical history including viral sinusitis…"*

**The active-as-history framing is a prompt problem, not a model problem.** The last line of
the system prompt guards the past→present direction and nothing guards present→past. It is
reported but never failed on, because it is sometimes *right*: this cohort contains an `active` miscarriage with
a 1990 onset, where "history of" is the honest reading and the FHIR status is what is wrong.
That is exactly the call a string check cannot make, which is why the worksheet leaves it to
a person.

> **Still open:** the `Verdict` lines on the worksheet are blank. The counts above are the
> mechanical half; a clinician reading the 10 summaries is the half that decides.

### Citation coverage, measured against a labelled set

The linker was built against a hand-labelled set rather than by eye:
**13 real summaries** (the 10 in `eval-llama3.2-3b.json` plus the 3 in `eval-gemma3-4b.json`)
with **117 mentions labelled by hand** with the fact each one should cite, plus phrases that
must stay unlinked. `python -m scripts.eval_citations` reproduces the run.

| | |
|---|---|
| Links produced | **115** |
| …pointing at the wrong fact | **0** |
| …on a phrase that names no fact ("antibiotics", "various medications") | **0** |
| Labelled mentions linked | **113 / 117** (96.6%) |
| Links whose span is wider than the phrase | **0** |
| Cost | well under 1 ms per summary |

The four misses are the linker refusing rather than failing:

| Prose | Record | Why it stays unlinked |
|---|---|---|
| "prostate neoplasm" ×2 | `Neoplasm of prostate` | resolving it means reordering words |
| "viral pharyngitis" | `Acute viral pharyngitis` | dropping a leading qualifier would also merge `Chronic sinusitis` with `Sinusitis`, which are two different facts on one patient |
| "sprains" | `Sprain of ankle` | plural *and* the site dropped |

Checked against the live stack as well: on `78378d37-…` all **8** citations were re-fetched
from HAPI and still carried the display the packet cited them for, and the rendered summary
concatenates back to a string byte-identical to `summary`, so no link is displaced.

> **Caveat worth stating plainly:** those rules were tightened against this set's own errors,
> so the precision figure is in-sample. The gold set and the scorer are in the repo precisely
> so it can be re-run on fresh summaries, which is what would make it a clean number.

---

## What I would do next, and why

Ranked by value to a reviewer.

1. **Surface `reasonReference` — drug-to-indication linkage.** Emit
   `"Metformin 500 MG" → indication: "Diabetes" (Condition/4821)`. This answers the actual
   prior-authorization question instead of leaving the reviewer to infer it, it is entirely
   deterministic, and the data is already in the record. Highest value per hour of work.
2. **Finish the 10-patient evaluation**, then replace it with something repeatable: assert
   that every `display` in a summary appears in the facts given to the model. That turns
   hallucination from something you notice into something CI catches.
3. **Add `AllergyIntolerance`.** 40 of them in the sample, and a documented allergy is often
   the whole reason an alternative drug was requested. One more search, same `Fact` shape.
4. **Benchmark the three other candidate models** and put the numbers in this file.
5. **Recency weighting.** A condition with a 1990 onset should not read like a current
   problem. This is a clinical judgment call, which is why it is listed rather than
   implemented — it belongs in a conversation with the customer, not in a silent heuristic.
6. **Persist the packet.** A `packet_request` table storing `{patient_id, requested_by,
   requested_at, model, prompt_version, packet JSONB}`. In prior authorization you must be
   able to reconstruct what the reviewer saw six months ago, and re-running the endpoint
   cannot do that: the record has changed, and possibly the model and prompt too. HAPI
   preserves *record* history for free; it has no idea what this service rendered.
7. **Field-level citation.** `source` points at a resource; pointing at the exact element
   (`Condition/4821#code.coding[0]`) would let a UI highlight the precise evidence.
8. **Operational hardening** for a real deployment: auth, multi-tenancy, retries with
   backoff on the FHIR client, structured request logging.

Deliberately **not** done: caching packets (a cached clinical packet can show a reviewer a
medication list that has since changed — if ever added, it needs a short TTL and a visible
`as_of` timestamp), and `Observation` (53,785 of them, ~60% of all resources, and they would
swamp both the packet and the model's context for marginal gain).

---

## Code provenance

The assignment asks where the code came from. Honestly:

**Machine-generated.** Effectively all of the Python, the shell script, and the React page
were written by **Claude (Claude Code, Opus)** in an interactive session, under my direction
— I specified the goals, chose between the options it presented, and reviewed the output.
That includes `app/extract.py`, `app/summarize.py`, `app/link.py`, `app/fhir_client.py`,
`app/main.py`, `app/models.py`, `scripts/load_bundle.py`, `scripts/load_synthea.py`,
`scripts/eval_citations.py`, `scripts/show_conditions.sh`, `tests/test_extract.py`,
`tests/test_link.py`, `tests/test_packet_route.py`, `ui/index.html`, and this README.

**Hand-labelled, not generated.** `tests/data/citation_gold.json` is the one artefact in the
repo that is a judgment rather than code: 117 mentions across 13 real summaries, each labelled
with the fact it should cite, including the calls that went the other way (a bare "sinusitis"
on a patient with four sinusitis facts links to nothing). Every case carries a `note` saying
why, so the labelling is reviewable rather than taken on trust.

**Human-authored.** `docker-compose.yml`, `pyproject.toml`, `app/config.py` and the original
module scaffolding predate that session.

**Design decisions driven by measurement, not by the model's defaults.** Nearly every choice
documented above came from probing the running system — the token counts, the 164-to-11
deduplication, Ollama's silent 4096-token truncation, HAPI's numeric-id rejection, HAPI's
search-result caching, the `arm64` manifests, the two-sinusitis-episodes dedupe key. Those
were found by querying this stack, not recalled from documentation.

**Third-party sources consulted** (no code copied verbatim):
[HAPI FHIR JPA starter](https://github.com/hapifhir/hapi-fhir-jpaserver-starter) ·
[HAPI JPA schema docs](https://hapifhir.io/hapi-fhir/docs/server_jpa/schema.html) ·
[FHIR R4 spec](https://hl7.org/fhir/R4/) (`http.html`, `search.html`, `condition.html`,
`medicationrequest.html`) ·
[Ollama API](https://github.com/ollama/ollama/blob/main/docs/api.md) and
[structured outputs](https://ollama.com/blog/structured-outputs) ·
[Synthea sample data](https://synthetichealth.github.io/synthea-sample-data/)

**Dependencies:** FastAPI, Pydantic, pydantic-settings, HTTPX, uvicorn, pytest, ruff.
React 18 and Babel are loaded from cdnjs at runtime.

---

## Project layout

```
app/
  config.py        settings, env-overridable (FHIR_BASE_URL, OLLAMA_MODEL, ...)
  models.py        Fact, Span, Packet, PatientSummary, PatientPage -- the response contract
  fhir_client.py   HTTP to HAPI: reads, searches, paging, capability lookup
  extract.py       FHIR -> facts + sources + dedupe + missing + prompt shaping (no LLM)
  summarize.py     Ollama structured output; never raises
  link.py          summary prose -> the facts it cites, as offsets (no LLM)
  summary_cache.py one cached summary per patient, keyed on a hash of their facts
  main.py          FastAPI routes
scripts/
  load_bundle.py   load one bundle, PUT semantics, narrated step by step
  load_synthea.py  load a reproducible random sample of N bundles
  cache_admin.py   --stats / --clear for the summary cache
  eval_sample.py   10-patient hand review: resolves every citation against FHIR,
                   scores the checkable, stages the judgment calls. No LLM judge
  eval_citations.py    scores app/link.py against the labelled citation set
  show_conditions.sh   spot-check one patient's conditions and sources from the CLI
tests/
  test_extract.py  20 tests over the deterministic layer
  test_link.py     42 tests over the citation linker -- terms, spans, invariants
  test_packet_route.py   6 tests over how the route assembles facts, prose, citations
  test_summary_cache.py  11 tests over the cache key -- no Postgres, no model
  data/citation_gold.json  117 mentions in 13 real summaries, labelled by hand
ui/index.html      single-file React dashboard
docker-compose.yml HAPI FHIR 8.12 + Postgres 14
```

The service is **stateless** — it owns no database. HAPI's Postgres is the source of truth,
reached over the FHIR API, which is both how a real deployment would work (you do not get to
copy a hospital's clinical data into a side table) and what keeps `source` meaningful.
`models.py` is a response contract, not a storage schema.

```bash
./.venv/bin/pytest tests/ -q        # 20 tests, no server or model needed
./.venv/bin/ruff check app/ tests/ scripts/
```

---

## Deployment

The service is container-ready; see **[deploy/README.md](deploy/README.md)** for the
Azure plan.

```bash
docker build -t fhir-summarize:local .
docker run --rm -p 8001:8000 \
  -e FHIR_BASE_URL=http://host.docker.internal:8080/fhir \
  -e FHIR_PUBLIC_URL=http://localhost:8080/fhir \
  -e OLLAMA_BASE_URL=http://host.docker.internal:11434 \
  fhir-summarize:local
```

Four things were built for this rather than assumed:

- **Nothing is baked in.** Every setting is an environment variable with a working
  local default, so the same image runs on a laptop and in a cluster.
- **`/health` and `/ready` are different endpoints.** Liveness checks nothing
  external, because restarting the process cannot fix an unreachable FHIR server;
  readiness reports each dependency so an operator can see which one is at fault.
  The model is reported but not required — a packet without prose is still evidence.
- **`FHIR_AUTH_SCOPE` switches on Entra ID auth** via `DefaultAzureCredential`,
  which resolves to a container's managed identity in Azure and to `az login`
  locally. No secret is ever stored. `azure-identity` is an optional extra, so a
  local HAPI deployment does not carry cloud SDKs.
- **`FHIR_PUBLIC_URL` is separate from `FHIR_BASE_URL`.** The dashboard links each
  citation to the record it came from, and behind private networking the address
  the service uses is not one a browser can resolve. Unset, the dashboard renders
  references as text rather than dead links.

`LOG_JSON=true` (the image default) switches logging to JSON lines so Log Analytics
or App Insights parses the fields instead of grepping strings.

The open items — persisting Ollama's model cache, converting the loader to Azure's
NDJSON `$import`, private endpoints, and Bicep — are listed in `deploy/README.md`.
The Azure CLI commands there are the plan; they have not been run against a live
subscription.

## Scope notes

The assignment asks for one endpoint. The patient search endpoints and the React dashboard
are beyond that ask — they exist because a demo needs a way to find a patient before building
their packet. The packet endpoint and the hand review are the deliverable; the dashboard is
not a substitute for either.

Synthea data is entirely synthetic. No real patient data is involved anywhere in this
project. (Incidentally, "HAPI" is HL7's Java API — unrelated to HIPAA.)
