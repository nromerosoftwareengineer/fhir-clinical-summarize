"""Links terms in the summary prose back to the facts they cite.

Deterministic, like app/extract.py and for the same reason: a link is a
citation, and the thing that wrote the prose is a 4B model paraphrasing under
instruction. Matching here is exact after normalisation and never approximate.
A term that will not resolve is left as plain text, which is the honest outcome
-- the fact tables below the summary are still the record, and an unlinked word
costs a reviewer a glance down the page where a wrong link costs them the
review.

Case is ignored, which is doing real work on this data rather than being a
convenience: RxNorm ships tall-man capitalisation ("PACLitaxel 100 MG
Injection", "1 ML DOCEtaxel 20 MG/ML Injection") that no summary reproduces.

What normalisation covers, taken from the summaries in eval_output/:

    "Viral sinusitis (disorder)"              -> viral sinusitis
    "Acetaminophen 160 MG Oral Tablet"        -> acetaminophen
    "Body mass index 30+ - obesity (finding)" -> body mass index 30+, obesity
    "Acetaminophen 325 MG / Hydrocodone bitartrate 7.5 MG Oral Tablet"
        -> acetaminophen, hydrocodone bitartrate, hydrocodone
    "60 ACTUAT Fluticasone propionate ..."    -> fluticasone propionate, fluticasone
    "Levora 0.15/30 28 Day Pack"              -> levora
    "Yaz 28 Day Pack"                         -> yaz
    "NDA020800 0.3 ML Epinephrine 1 MG/ML Auto-Injector"
        -> nda020800, epinephrine
    "Non-small cell carcinoma of lung, TNM stage 1 (disorder)"
        -> non-small cell carcinoma of lung, tnm stage 1

And what the prose is allowed to differ by, same source -- each of these is a
difference between a coded display and the sentence written from it, and each is
still an exact match rather than a similarity score:

    a hyphen for a space     "third-degree burn"  vs "Third degree burn"
    an inserted article      "fracture of the ankle" vs "Fracture of ankle"
    an English plural        "normal pregnancies" vs "Normal pregnancy"

Cost is ~26 facts x ~3 terms of cached, compiled regex over ~300 characters of
prose: well under a millisecond against the ~31 s of inference it annotates.
"""

import functools
import re

from app.models import Fact, Span

# SNOMED display strings carry a semantic tag in trailing parentheses. It is
# part of the code's display, never part of how anyone writes the condition.
SEMANTIC_TAG = re.compile(
    r"\s*\((?:disorder|finding|situation|procedure|observable entity|"
    r"morphologic abnormality|regime/therapy|substance|body structure|"
    r"qualifier value)\)\s*$",
    re.IGNORECASE,
)

# RxNorm displays lead with a pack size or actuation count -- "60 ACTUAT ...",
# "24 HR Metformin ..." -- so the drug name is not first until this is gone.
DOSE_PREFIX = re.compile(r"^\s*\d+(?:\.\d+)?\s*(?:HR|ACTUAT|ML|MG|MEQ|UNT)\s+", re.IGNORECASE)

# A regulatory product code can lead a display as well: "NDA020800 0.3 ML
# Epinephrine 1 MG/ML Auto-Injector" is cited in the sample set's prose as
# "NDA020800", and the drug's own name does not appear until both the code and
# the dose are gone. Three letters and five or more digits, so no drug name can
# match it.
CODE_PREFIX = re.compile(r"^\s*[A-Z]{3}\d{5,}\s+", re.IGNORECASE)

# An ingredient name runs until its strength: "Hydrocodone bitartrate 7.5 MG".
# The whitespace is required, not optional: with `\s*` the split also fires
# inside an alphanumeric code, truncating "NDA020800 0.3 ML Epinephrine ..." to
# "NDA" and losing the only name that summary ever used for the auto-injector.
STRENGTH = re.compile(r"\s+\d")

# How SNOMED hangs a qualifier off a condition display: " - " for a second way
# of naming it, "," for a stage or site that prose routinely drops.
QUALIFIER = re.compile(r"\s+-\s+|,")

# Dose-form, route and unit words that survive splitting a combination product
# but name no drug: "ACTUAT Dry Powder Inhaler" is the tail of a fluticasone
# display, not something a summary would ever cite.
NON_DRUG_LEAD = frozenset(
    {
        "actuat", "hr", "ml", "mg", "meq", "unt", "oral", "dry", "day", "pack",
        "tablet", "capsule", "injection", "solution", "suspension", "powder",
        "inhaler", "implant", "patch", "cream", "ointment", "prefilled",
    }
)

# Shortest term worth matching, so a sub-term like "ear" cannot fire in prose.
MIN_TERM_LEN = 4

# Drug names get one character of slack, because a brand is a proper noun that
# does not collide with ordinary English the way a three-letter fragment of a
# condition display does: "Yaz 28 Day Pack" is cited in prose as "Yaz", and
# word-bounded matching means that can only ever hit the word itself.
MIN_DRUG_TERM_LEN = 3

# Articles a summary inserts and a coded display never carries: the record says
# "Fracture of ankle" and "Miscarriage in first trimester", the prose says
# "fracture of the ankle" and "miscarriage in the first trimester".
ARTICLES = ("the", "a", "an")


def condition_terms(display: str) -> list[str]:
    """Terms a model might use for this condition, longest first.

    Split on both separators SNOMED uses to hang a qualifier off a condition:

    - "Body mass index 30+ - obesity" is one code naming two things, either of
      which a summary may pick up, and both cite the same fact.
    - "Non-small cell carcinoma of lung, TNM stage 1" is one code whose staging
      suffix the prose drops. Without the head as its own term the whole phrase
      goes unlinked, because the record's string is the longer of the two.
    """
    base = SEMANTIC_TAG.sub("", display).strip()
    return _usable([base, *(part.strip() for part in QUALIFIER.split(base))])


def medication_terms(display: str) -> list[str]:
    """Terms a model might use for this drug, longest first.

    Combination products split on "/": gemma3:4b wrote "Acetaminophen/Hydrocodone"
    for a two-ingredient tablet, naming each ingredient and neither in full.
    """
    base = DOSE_PREFIX.sub("", display).strip()
    terms = [base]
    for part in base.split("/"):
        part = part.strip()
        # The code is a name in its own right -- one summary cited the
        # auto-injector by nothing else -- and cites the same fact as the drug.
        code = CODE_PREFIX.match(part)
        if code:
            terms.append(code.group(0).strip())
        ingredient = STRENGTH.split(_strip_lead(part), maxsplit=1)[0].strip()
        words = ingredient.split()
        if not words or words[0].casefold() in NON_DRUG_LEAD:
            continue
        # RxNorm writes "Hydrocodone bitartrate"; the summary writes "hydrocodone".
        terms += [ingredient, words[0]]
    return _usable(terms, MIN_DRUG_TERM_LEN)


def _strip_lead(text: str) -> str:
    """Remove leading product codes and doses until a drug name is first.

    Looped because they stack: "NDA020800 0.3 ML Epinephrine ..." carries both.
    """
    while True:
        stripped = DOSE_PREFIX.sub("", CODE_PREFIX.sub("", text)).strip()
        if stripped == text:
            return stripped
        text = stripped


def _usable(terms, min_len: int = MIN_TERM_LEN) -> list[str]:
    """Casefold, deduplicate, drop the too-short, order longest first.

    Longest first is what makes the greedy pass in find_spans prefer
    "chronic kidney disease stage 1" over "kidney disease".
    """
    seen = {}
    for term in terms:
        stripped = term.strip()
        if len(stripped) >= min_len:
            seen.setdefault(stripped.casefold(), None)
    return sorted(seen, key=len, reverse=True)


def _fact_terms(conditions: list[Fact], medications: list[Fact]):
    """Pair each fact with the terms that should link to it."""
    for fact in conditions:
        yield fact, condition_terms(fact.display)
    for fact in medications:
        yield fact, medication_terms(fact.display)


def _owners(conditions: list[Fact], medications: list[Fact]) -> dict[str, list[Fact]]:
    """term -> the facts it could cite, in the order extract handed them over.

    A term with more than one owner is the normal case, not an edge: two
    episodes of the same illness share every term, and an ingredient term
    belongs both to the single-ingredient product and to every combination
    containing it.
    """
    owners: dict[str, list[Fact]] = {}
    for fact, terms in _fact_terms(conditions, medications):
        for term in terms:
            owners.setdefault(term, []).append(fact)
    return owners


def _plural(word: str) -> str:
    """Allow the English plural of a term's last word.

    Coded displays are singular ("Normal pregnancy", "Sprain of ankle") and a
    summary pluralises freely ("a notable history of normal pregnancies"). Only
    the last word is inflected, and only when it ends in a letter, so
    "Body mass index 30+" is left exactly as it is.
    """
    escaped = re.escape(word)
    if not word[-1:].isalpha():
        return escaped
    if word.endswith("y"):
        return re.escape(word[:-1]) + "(?:y|ies)"
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return escaped + "(?:es)?"
    return escaped + "s?"


@functools.lru_cache(maxsize=512)
def _pattern(term: str) -> re.Pattern:
    """Word-bounded, case-insensitive, punctuation-tolerant match for one term.

    Escaped first: displays contain "+", "/" and "." ("Body mass index 30+",
    "Levora 0.15/30"), which would otherwise compile as regex operators.

    Three allowances, each one a difference observed between a coded display and
    the prose written from it, and each one still exact rather than approximate:

    - a hyphen and a space are interchangeable, because the record writes
      "Third degree burn" where the summary writes "third-degree burn" (and
      "Non-small cell ..." the other way round)
    - an article may appear between words, because the record writes
      "Fracture of ankle" and the summary writes "fracture of the ankle"
    - the last word may be plural, because displays are singular and prose is
      not: "Normal pregnancy" vs "normal pregnancies"

    The boundaries are conditional because \\b only fires between a word and a
    non-word character. "Body mass index 30+" ends in "+", so a trailing \\b
    there would demand a following word character and the term would never
    match the prose "index 30+ due to obesity".
    """
    words = [word for word in re.split(r"[\s-]+", term) if word]
    gap = rf"[\s-]+(?:(?:{'|'.join(ARTICLES)})[\s-]+)?"
    body = gap.join(
        [*(re.escape(word) for word in words[:-1]), _plural(words[-1])] if words else []
    )
    lead = r"\b" if term[:1].isalnum() else ""
    trail = r"\b" if term[-1:].isalnum() else ""
    return re.compile(lead + body + trail, re.IGNORECASE)


def find_spans(summary: str, conditions: list[Fact], medications: list[Fact]) -> list[Span]:
    """Resolve mentions in `summary` to the facts they cite.

    Longest-term-first and non-overlapping: once a character sits inside a link
    it cannot be claimed again, so "diabetes" never fires inside "prediabetes"
    and the longer of two overlapping conditions wins.

    Facts arrive newest-first from extract and the sort below is stable, so when
    one term matches two facts -- the sample set has two viral sinusitis
    episodes four years apart -- the link points at the most recent evidence,
    which is the record _dedupe kept the source of.
    """
    if not summary:
        return []

    owners = _owners(conditions, medications)

    spans: list[Span] = []
    taken = [False] * len(summary)
    for term in sorted(owners, key=len, reverse=True):
        holders = owners[term]
        # Successive mentions of a term go to successive owners, so a sentence
        # naming "Acetaminophen ... Acetaminophen / oxyCODONE" cites the plain
        # tablet once and the combination once, rather than citing the tablet
        # twice. Same-display owners are two episodes of one illness instead,
        # where every mention should cite the newest evidence -- which is
        # holders[0], because extract sorts newest-first.
        episodes = len({fact.display for fact in holders}) == 1
        for index, match in enumerate(_pattern(term).finditer(summary)):
            start, end = match.span()
            if any(taken[start:end]):
                continue
            fact = holders[0] if episodes else holders[min(index, len(holders) - 1)]
            spans.append(Span(start=start, end=end, text=match.group(0), source=fact.source))
            taken[start:end] = [True] * (end - start)

    return sorted(spans, key=lambda span: span.start)
