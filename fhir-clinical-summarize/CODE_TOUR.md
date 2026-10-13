# Code tour — what every file does, in plain language

A companion to the README. The README explains *decisions*; this explains
*which file to open*. No prior knowledge of FHIR or FastAPI assumed.

## The 30-second version

The service answers one question: **"show me what's in this patient's chart,
with a short written summary, and let me click through to prove every claim."**

It does that in two halves, and the split is the whole design:

| Half | Who does it | Can it be wrong? |
|---|---|---|
| The **facts** — conditions, medications, dates, links | plain Python, no AI | No. It's copied straight from the medical record. |
| The **prose** — a few sentences of summary | a local AI model | Yes, which is why it's quarantined. |

The AI never touches the facts. Its text goes into one field called `summary`
and nowhere else. So even if the model writes nonsense, the clickable evidence
underneath is still correct. If the model is *down*, you get the facts with a
blank summary instead of an error page.

## How a single request flows

Someone asks for `GET /patients/abc-123/packet`:

```
browser or curl
      │
      ▼
app/main.py ............ receives the request, decides what to call
      │
      ▼
app/fhir_client.py ..... downloads the raw medical records
      │
      ▼
app/extract.py ......... cleans them up into tidy "facts"
      │
      ▼
app/summary_cache.py ... "have I already written this patient up?"
      │                      yes → reuse the stored sentences (instant)
      ▼                      no  ↓
app/summarize.py ....... asks the AI model for 2-4 sentences (~15 sec)
      │
      ▼
app/link.py ............ reads those sentences back and works out which
      │                  words came from which record (under 1 ms)
      ▼
app/models.py .......... packs everything into the response shape
      │
      ▼
back to the browser
```

---

# The app/ folder — the actual service

These ten files are what runs in production. Everything else in the repo is
tooling around them.

### `app/main.py` — the front door *(232 lines)*

Defines every URL the service answers. Think of it as the switchboard: it takes
a request, calls the other modules in the right order, and hands back JSON.

The URLs:

- **`/health`** — "is the process alive?" Answers instantly and checks nothing
  else on purpose. It's what Docker pings every few seconds. Restarting the
  app can't fix a broken database, so there's no point reporting a broken
  database here.
- **`/ready`** — "can I actually do my job right now?" This one *does* check
  the FHIR server, the AI model, and the cache, and reports each one
  separately so you know which thing broke.
- **`/config`** — hands the web page two settings it needs (which model, and
  which FHIR address the *browser* should link to — sometimes different from
  the one the server uses).
- **`/patients`** — search for people. Supports paging (`limit`/`offset`).
- **`/patients/{id}`** — one person's name, birth date, MRN.
- **`/patients/{id}/packet`** — **the main event.** The full chart summary.
- **`/ui/`** — serves the web dashboard (the HTML file).

Two things worth knowing:

1. Before forwarding a search to the medical records server, it *asks that
   server what it supports* and rejects unknown search fields with a helpful
   list. So you get "unsupported parameter: foo, here are the valid ones"
   instead of a cryptic failure further down.
2. The patient is read **first** in the packet route. That way a typo'd ID
   returns a clean "404 no such patient" rather than an empty-looking packet
   that's indistinguishable from a real patient with an empty chart.

### `app/config.py` — all the knobs *(63 lines)*

Every setting in one place: addresses, the model name, timeouts, the cache
database. Each has a working default, which is why you can just run the thing
on a laptop with zero configuration.

Settings can be overridden by environment variables (`OLLAMA_MODEL=...`) or a
`.env` file. Precedence: environment variable beats `.env` beats the default
written in this file.

### `app/fhir_client.py` — the downloader *(179 lines)*

Talks to the medical records server (HAPI FHIR) over HTTP. Its whole job is
fetching raw data; it doesn't interpret anything.

What it handles so nobody else has to:

- **Paging.** Medical record servers return results in pages and *ignore* your
  requested page size if they feel like it. `search_all()` follows the "next"
  link until there's genuinely nothing left, so you never silently lose records.
- **"Not found" vs. "broken".** A missing patient (404) and an unreachable
  server are different problems with different fixes, so they're different
  exception types (`FhirNotFound` vs. `FhirError`).
- **Cloud authentication.** If you point this at Microsoft's hosted FHIR
  service it will fetch and refresh an access token. Left off for local use.

### `app/extract.py` — the cleanup crew *(200 lines)* ⭐ most important

This is where messy real-world records become tidy facts. **No AI here at all**,
which is the point — this is the part a reviewer has to be able to trust.

Four jobs:

1. **Translate.** A FHIR `Condition` record is a deeply nested blob. This pulls
   out the name, the status, the date, and the record's ID, and throws the rest
   away. Defensively — real records are full of missing fields, and a missing
   field should drop a label, not crash a request.

2. **Deduplicate — with two different rules, on purpose.** One chemotherapy
   patient in the test data has **164 medication orders that are really only 11
   drugs** (60 cisplatin cycles, 60 paclitaxel, 36 simvastatin). Those collapse
   into one row each with a count of `x60`.

   But conditions use a *different* rule that includes the date, because one
   patient has two sinus infections four years apart — those are two separate
   illnesses, and merging them would delete history.

   > Rule of thumb: repeated medication = repeat dosing (merge it).
   > Repeated condition on a different date = a second illness (keep both).

3. **Shrink the prompt.** The AI has a limited reading capacity, and Ollama
   *silently cuts off* anything too long rather than complaining. The measured
   reduction chain:

   ```
   raw records from the server   ~45,000 units of text
   one fact per record            ~5,000
   after deduplication              ~774
   split into active vs. past       ~460   ← what the AI actually reads
   ```

   That 100x reduction is what makes a small model on a laptop work at all.

4. **Say what's *missing*.** "No active medications on file" is written by
   Python, never by the AI — because an AI asked to confirm an absence will
   sometimes hallucinate a presence instead.

It also splits facts into **active** vs. **past** before the AI sees them, and
tags active ones with their start date. Both are there because of observed
failures: given a flat list the model dumped commas instead of writing prose,
and given no dates it described a condition that started in 1990 as a
present-day problem.

### `app/models.py` — the shapes *(119 lines)*

Defines what the JSON looks like, enforced automatically by Pydantic. Three
shapes:

- **`Fact`** — one clinical item. Always has a `display` (the name) and a
  `source` (e.g. `"Condition/1a2b"`, the receipt you can click). Optionally a
  status, a date, and a count.
- **`Packet`** — the full response: patient ID, conditions, medications,
  summary, and a `missing` list.
- **`PatientSummary` / `PatientPage`** — search results, with the logic for
  digging a person's name and medical record number out of a FHIR record.

Small but real detail: optional fields are hidden when they're boring, so the
JSON isn't 90% `"count": 1` and `"status": null`.

### `app/summarize.py` — the AI call *(102 lines)*

Sends the tidied facts to Ollama (an AI model running on the same machine — no
data leaves the box) and gets back two to four sentences.

Three things make it reliable rather than a coin flip:

- **A forced output shape.** The request includes a schema saying "reply must
  be JSON with a `summary` string." Ollama *enforces* this, so you never have
  to fish prose out of a chatty response.
- **Temperature 0.** Same input → same output, every time. Essential for
  demoing and for the eval harness to mean anything.
- **A very bossy prompt.** Literally spelled out: "Write two to four sentences.
  Do not output a list. Sentence 1: active conditions. Sentence 2: medications.
  Sentence 3: history, if there is any. Write nothing after those sentences."
  Every clause in there is scar tissue from a specific failure — the last one
  because an open-ended final sentence gets filled by restating the *active*
  conditions as history, which is the one error a reviewer must never be handed.

**It never raises an exception.** Model down, gibberish response, timeout — all
return an empty string and log a warning. A packet with evidence and no prose
is useful; a 500 error page is not.

### `app/link.py` — the citation finder *(267 lines)*

Turns the AI's sentences into clickable evidence. The model wrote
*"the patient has active hypertension"*; this file works out that the word
`hypertension` came from `Condition/c420bace` and hands back the position of
that word in the sentence, so the page can turn it into a link.

Nothing here is clever, which is the point. It does three things:

- **Gives every fact a few nicknames.** The record says `Sertraline 100MG`, but
  a summary writes `Sertraline`. So each fact gets a short list of names to look
  for: the display with the medical jargon stripped (`(disorder)`, `(finding)`),
  a drug name without its dose, each ingredient of a combination pill, a brand
  name on its own.
- **Looks for those names in the sentence.** Case-insensitively, because RxNorm
  writes `PACLitaxel` and no summary copies that. It also forgives a hyphen
  where the record has a space, an inserted "the", and an English plural.
- **Refuses when it is not sure.** The prose `prostate neoplasm` and the record
  `Neoplasm of prostate` are the same thing, but matching them means reordering
  words — a guess. So that word stays plain text. The fact table below the
  summary is still the record, and a missing link costs a reviewer a glance
  while a wrong link costs them the review.

Measured against 117 mentions labelled by hand (`tests/data/citation_gold.json`,
scored by `python -m scripts.eval_citations`): **113 linked, zero wrong**.

Why this file exists at all, rather than letting the AI say where each phrase
came from: because then the AI would be producing citations, and a citation it
invents is exactly the failure this project is built to make impossible. Here
the model never sees a record id. The links are attached afterwards, by code, to
records that were already in the packet before the model ran.

### `app/summary_cache.py` — the "don't redo work" layer *(215 lines)*

The AI call is ~99% of the time a request takes (0.2 seconds to fetch and tidy
the records, 15–31 seconds for the model). So once a summary is written, it's
stored in Postgres and reused.

**The clever bit — how it knows when to redo it.** It takes the tidied facts,
turns them into text in a guaranteed-consistent order, and runs SHA-256 over it
to get a 64-character fingerprint. Stored alongside the summary.

Next request: fingerprint the facts again. Same fingerprint → the chart hasn't
changed → reuse the stored sentences. Different → something changed → write new
ones.

Why fingerprint the *tidied* facts and not the raw records? Because raw records
carry a "last updated" timestamp that changes every time data is reloaded even
when nothing medical changed. Fingerprinting those would mean constantly
regenerating identical summaries. The tidied version has that bookkeeping
stripped out, so a change in fingerprint means a change in *medicine*.

Three deliberate decisions, each of which looks like a bug until explained:

- **One row per patient.** Patient ID is the primary key, so the table can
  never grow bigger than your patient count.
- **The model name is NOT in the fingerprint.** Switching models does *not*
  wipe the cache — old summaries stay until that patient's data changes. Use
  `scripts/cache_admin.py --clear` to force it. (There's a test specifically
  guarding this so nobody "fixes" it by accident.)
- **No way to skip the cache per-request.** It's always consulted. If you need
  fresh text you call `invalidate(patient_id)` first. One code path means a hit
  and a miss are genuinely the same request.

And as everywhere else here: **every failure is survivable.** Database down,
table won't create, write rejected — log a warning, generate fresh, serve the
packet. A cache that can take the service down isn't an optimization, it's a
liability.

### `app/logging_config.py` — log formatting *(56 lines)*

Switches between two styles. On your laptop: readable lines for humans. In a
container: one JSON object per line, because log tools can search JSON fields
but can only grep text.

Nice touch — anything passed as `extra={"patient_id": x}` becomes a real
top-level field in the JSON, so you can filter by patient instead of grepping
for a substring.

### `app/__init__.py` — empty *(0 lines)*

Exists purely to tell Python "this folder is a package." Normal and correct.

---

# The ui/ folder — the web page

### `ui/index.html` — the whole dashboard, one file *(742 lines)*

A React app with **no build step**: React and Babel load from a CDN, the JSX
compiles in the browser, and there's no `node_modules` anywhere. One file you
can open and read.

It's served by the Python app itself at `/ui/` rather than from a separate dev
server, which avoids two classic headaches: no CORS configuration, and no
browser blocking a secure page from calling an insecure local backend.

Inside, top to bottom:

- **CSS variables** at the top (`--bg`, `--text`, …) redefined under a dark-mode
  media query, so it follows your system theme.
- **`SearchForm`** — the search fields. Every one maps to a real FHIR search
  parameter taken from the server's own capability list, not guessed. Blank
  fields are dropped, so any combination works.
- **`Results`** — the hit list, with paging.
- **`Detail`** / **`PacketPanel`** — the selected patient and their packet.
- **`FactTable`** — renders conditions and medications, and turns each fact's
  `source` into a **clickable link to the actual record** on the FHIR server.
  This is the feature the whole architecture exists to support: you can verify
  any line without trusting the AI.
- **`LinkedSummary`** — the same idea applied to the prose. The underlined words
  in the summary are the ones `app/link.py` traced back to a record; clicking
  one scrolls to that row in the table below and flashes it, so a reviewer goes
  from a claim in a sentence to the evidence for it in one click. It renders by
  cutting the summary at the character offsets the API returned rather than
  searching the text again in the browser — a second implementation of the
  matching could disagree with the one that was tested. Any citation whose
  offsets no longer slice back to their own text is dropped rather than drawn,
  because one displaced link would shift every link after it.
- **`highlightJson`** — colorizes the raw JSON view for anyone who wants to see
  exactly what the API returned.

---

# The scripts/ folder — tools you run by hand

**Important:** these are *not* in the Docker image. They're laptop tools. On the
server, use `psql` or `docker exec` directly instead.

### `scripts/load_bundle.py` — load one patient *(261 lines)*

Pushes one Synthea (fake-patient-generator) file into the FHIR server.

The one thing it does that matters: Synthea's files say "POST" for every record,
which lets the server assign its own random IDs. This **rewrites them to PUT**
so the IDs in the file are kept. Without that, every reload would change every
ID — and a citation link that breaks on reload isn't a citation.

Prints numbered progress steps and verifies the counts afterwards.

### `scripts/load_synthea.py` — load many patients *(181 lines)*

Runs the above across hundreds of files. Two deliberate features:

- **A fixed random seed**, so "load 25 patients" gives you the *same* 25
  patients on any machine. That's what makes the README's measurements
  reproducible.
- **Size filters.** Tiny files have no clinical history worth testing; three
  huge ones are ~20,000 records in a single transaction and just strain Docker.

One bad file doesn't stop the run — failures are counted and listed at the end.

### `scripts/eval_sample.py` — the quality checker *(465 lines)* ⭐ the honest one

The most interesting script here. It answers: **"is the AI actually telling the
truth?"** — mechanically, instead of by vibes.

It picks 10 patients (fixed seed, plus 3 hand-picked hard cases that are pinned
by ID), requests each packet, and runs automated checks:

- **Do the citations resolve?** It fetches every cited record back from the
  server and re-derives the fact from it, confirming the link points at
  something that actually says what the fact claims.
- **Did the model invent specifics?** Dosing frequencies and doses that don't
  appear anywhere in the input are flagged.
- **Did it overstep?** Words like "approve" or "deny" are flagged — this
  service assembles evidence, it does not make authorization decisions.
- **Did it follow the format?** Two to four sentences, prose, no bullets.
- **Did it confuse past with present?** Flagged for human review rather than
  auto-failed, because this one needs judgment.

It calls `summary_cache.invalidate()` before each request, so it scores *the
model* rather than whatever happened to be cached.

Output is a Markdown file in `eval_output/` with blank `Verdict:` lines for a
human to fill in. That's the part no script can do.

### `scripts/eval_citations.py` — the citation scorer *(158 lines)*

Replays 13 real summaries from `eval_output/` through `app/link.py` and compares
every link against `tests/data/citation_gold.json`, a by-hand labelling of which
word should cite which record. Reports precision and recall separately and exits
non-zero if any link points at the wrong record, so a regression fails the run
instead of being averaged into a percentage.

### `scripts/cache_admin.py` — cache inspector *(75 lines)*

`--stats` shows how many summaries are stored, by which model, and the average
time each took to generate. `--clear` empties the table — the lever for after a
model change, since a model switch deliberately doesn't invalidate anything.

---

# The tests/ folder

Both test files run in about a tenth of a second with **no FHIR server, no
database, and no AI model**. That's intentional: the parts worth testing are the
deterministic ones, and tests that need three services running are tests nobody
runs.

### `tests/test_extract.py` — tests the cleanup *(191 lines)*

Six groups covering the translation, both deduplication rules, the `missing`
notes, and the prompt shaping. The sample records are trimmed copies of real
Synthea data, deliberately including the awkward ones: a resolved condition,
repeat dosing, two episodes years apart, and a record with no name coded.

### `tests/test_link.py` — tests the citation finder *(351 lines)*

Half of these tests check that something **does not** link: a one-letter
spelling difference (`Simvastatin` vs the record's `Simvistatin`), a reordered
phrase, a word inside a longer word (`diabetes` must not fire inside
`prediabetes`). The other half are invariants over one real patient's packet —
every span slices back to its own text, every source belongs to a fact that is
in the packet, no two spans overlap.

### `tests/test_packet_route.py` — tests the assembly *(94 lines)*

The route is the only place facts, prose and citations meet. These tests patch
out the FHIR server and the model, then check the ordering property that makes
the whole design work: when the model returns nothing, the facts and their
sources still arrive and the citation list is simply empty.

### `tests/test_summary_cache.py` — tests the fingerprint *(114 lines)*

Eleven tests on one question: **does the fingerprint change exactly when it
should?** A fingerprint that misses a real change serves stale medical text; one
that fires on an irrelevant change makes the cache pointless.

So: reordering facts must *not* change it. Adding a condition, stopping a drug,
a dose change, a corrected date, a condition moving from active to history, a
changed repeat count — each *must* change it.

Plus `test_model_does_not_affect_hash`, which exists so that if someone later
decides the model *should* be in the fingerprint, this test fails and they have
to confirm it was on purpose.

---

# Everything else at a glance

| File | What it is |
|---|---|
| `Dockerfile` | Builds the API container. Note: no `scripts/` inside. |
| `docker-compose.yml` | FHIR server + Postgres. The laptop setup. |
| `docker-compose.prod.yml` | Adds the API, the AI model, and nginx. Layered on top of the file above — you need **both** `-f` flags or you'll operate on the wrong set of containers. |
| `deploy/init-summaries.sql` | Creates the cache database. Only runs on a brand-new volume. |
| `deploy/aws-setup.md` | Step-by-step EC2 deployment. |
| `pyproject.toml` | Dependencies and tool config. |
| `data/` | Synthetic patients (1.3 GB, not in git). |
| `eval_output/` | Generated review files. |

## If you only read three files

1. **`app/extract.py`** — the trustworthy half, and where the real cleverness is.
2. **`app/main.py`** — see how a request actually flows.
3. **`app/summary_cache.py`** — the fingerprinting trick, written down.

## The one idea behind all of it

**The AI is treated as an unreliable narrator with a very small job.** It gets
pre-chewed input, a schema it can't break out of, and a single output field that
nothing downstream depends on. Everything a reviewer would actually rely on —
the facts, the dates, the links, the statements about what's missing — is
computed in plain Python and can be checked by hand.
