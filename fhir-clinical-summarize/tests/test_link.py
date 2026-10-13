"""Tests for the citation linker -- no FHIR server, no model.

The prose is taken from eval_output/eval-gemma3-4b.json and the displays from
what the FHIR server actually returns for the same patient, typos and tall-man
capitalisation included ("Simvistatin 10 MG" is how Synthea spells simvastatin,
4612 times to 114; "PACLitaxel", "DOCEtaxel" are RxNorm's own casing). Inventing
tidier fixtures here would test a record shape that does not exist.

The cases that must *not* link matter as much as the ones that must: the prose
"prostate neoplasm" and the record "Neoplasm of prostate" are the same thing,
and resolving that would mean reordering words and guessing.
"""

from app.link import condition_terms, find_spans, medication_terms
from app.models import Fact


def cond(display, source="Condition/c1", status="active", date="2015-03-01"):
    return Fact(display=display, source=source, status=status, date=date)


def med(display, source="MedicationRequest/m1", status="active", date="2015-03-01"):
    return Fact(display=display, source=source, status=status, date=date)


def linked(summary, conditions=(), medications=()):
    """The (text, source) pairs the linker resolved, in reading order."""
    spans = find_spans(summary, list(conditions), list(medications))
    return [(span.text, span.source) for span in spans]


class TestConditionTerms:
    def test_strips_the_snomed_semantic_tag(self):
        assert "viral sinusitis" in condition_terms("Viral sinusitis (disorder)")

    def test_keeps_an_unparenthesised_display_whole(self):
        assert condition_terms("Normal pregnancy") == ["normal pregnancy"]

    def test_splits_a_hyphenated_display_into_both_halves(self):
        # One code, two things a model may name; both cite the same fact.
        terms = condition_terms("Body mass index 30+ - obesity (finding)")
        assert "body mass index 30+" in terms
        assert "obesity" in terms

    def test_offers_the_head_of_a_staged_display(self):
        # The prose drops the staging suffix, so without the head as its own
        # term the whole phrase goes unlinked.
        terms = condition_terms("Non-small cell carcinoma of lung, TNM stage 1 (disorder)")
        assert "non-small cell carcinoma of lung" in terms

    def test_orders_longest_first(self):
        terms = condition_terms("Body mass index 30+ - obesity (finding)")
        assert terms == sorted(terms, key=len, reverse=True)


class TestMedicationTerms:
    def test_drops_the_dose_and_form_tail(self):
        assert "acetaminophen" in medication_terms("Acetaminophen 160 MG Oral Tablet")

    def test_splits_a_combination_product_into_ingredients(self):
        terms = medication_terms("Acetaminophen 325 MG / Hydrocodone bitartrate 7.5 MG Oral Tablet")
        assert "acetaminophen" in terms
        assert "hydrocodone bitartrate" in terms
        assert "hydrocodone" in terms  # how the summary actually wrote it

    def test_strips_a_leading_actuation_count(self):
        terms = medication_terms(
            "60 ACTUAT Fluticasone propionate 0.25 MG/ACTUAT / salmeterol 0.05 MG/ACTUAT "
            "Dry Powder Inhaler"
        )
        assert "fluticasone propionate" in terms
        assert "salmeterol" in terms

    def test_dose_form_words_are_not_offered_as_drug_names(self):
        terms = medication_terms(
            "60 ACTUAT Fluticasone propionate 0.25 MG/ACTUAT Dry Powder Inhaler"
        )
        assert not any(term.startswith(("actuat", "dry", "oral")) for term in terms)

    def test_keeps_a_brand_pack_name(self):
        assert "levora" in medication_terms("Levora 0.15/30 28 Day Pack")

    def test_keeps_an_alphanumeric_product_code_whole(self):
        # The only name a summary used for this auto-injector. Splitting the
        # ingredient at a digit with optional whitespace truncates it to "NDA".
        terms = medication_terms("NDA020800 0.3 ML Epinephrine 1 MG/ML Auto-Injector")
        assert "nda020800" in terms
        assert "epinephrine" in terms

    def test_keeps_a_three_letter_brand(self):
        # A brand is a proper noun, so the condition-side minimum would lose it.
        assert "yaz" in medication_terms("Yaz 28 Day Pack")


class TestTermLength:
    def test_a_short_condition_fragment_is_not_offered(self):
        # The drug-side minimum is one character lower; conditions keep theirs,
        # because a three-letter fragment of a condition display is ordinary
        # English and would fire on any prose containing the word.
        assert "ear" not in condition_terms("Ear, nose")


class TestFindSpans:
    def test_links_a_condition_through_its_snomed_tag(self):
        summary = "The patient has a history of viral sinusitis and acute bronchitis."
        fact = cond("Viral sinusitis (disorder)", source="Condition/sinus-1")
        assert linked(summary, [fact]) == [("viral sinusitis", "Condition/sinus-1")]

    def test_links_a_drug_by_ingredient_name(self):
        summary = "Their past medications include acetaminophen and dextromethorphan."
        facts = [
            med("Acetaminophen 160 MG Oral Tablet", source="MedicationRequest/apap"),
            med("Dextromethorphan hydrobromide 5 MG/ML Oral Solution",
                source="MedicationRequest/dxm"),
        ]
        assert linked(summary, medications=facts) == [
            ("acetaminophen", "MedicationRequest/apap"),
            ("dextromethorphan", "MedicationRequest/dxm"),
        ]

    def test_links_both_ingredients_of_a_combination_product(self):
        summary = "The patient is taking Acetaminophen and Hydrocodone."
        fact = med(
            "Acetaminophen 325 MG / Hydrocodone bitartrate 7.5 MG Oral Tablet",
            source="MedicationRequest/combo",
        )
        assert linked(summary, medications=[fact]) == [
            ("Acetaminophen", "MedicationRequest/combo"),
            ("Hydrocodone", "MedicationRequest/combo"),
        ]

    def test_leaves_a_spelling_the_record_does_not_share_unlinked(self):
        # Linking across a spelling difference would mean guessing, and a
        # guessed citation is the one thing this module exists to prevent. The
        # fact table below the summary remains the record either way.
        summary = "The patient is currently taking Simvastatin."
        fact = med("Simvistatin 10 MG", source="MedicationRequest/simva")
        assert linked(summary, medications=[fact]) == []

    def test_leaves_a_reordered_phrase_unlinked(self):
        # "prostate neoplasm" and "Neoplasm of prostate" are the same thing;
        # resolving it means reordering words, which is a guess.
        summary = "Active conditions include prostate neoplasm."
        assert linked(summary, [cond("Neoplasm of prostate", source="Condition/prostate")]) == []

    def test_ignores_rxnorm_tall_man_capitalisation(self):
        # RxNorm's own casing, which no summary reproduces.
        summary = "Prior use of paclitaxel and docetaxel."
        facts = [
            med("PACLitaxel 100 MG Injection", source="MedicationRequest/pac"),
            med("1 ML DOCEtaxel 20 MG/ML Injection", source="MedicationRequest/doce"),
        ]
        assert linked(summary, medications=facts) == [
            ("paclitaxel", "MedicationRequest/pac"),
            ("docetaxel", "MedicationRequest/doce"),
        ]

    def test_prefers_the_longer_of_two_overlapping_conditions(self):
        summary = "Active problems include chronic kidney disease stage 1."
        facts = [
            cond("Chronic kidney disease stage 1 (disorder)", source="Condition/ckd1"),
            cond("Kidney disease", source="Condition/kd"),
        ]
        assert linked(summary, facts) == [
            ("chronic kidney disease stage 1", "Condition/ckd1")
        ]

    def test_does_not_fire_inside_a_longer_word(self):
        summary = "The patient has prediabetes."
        assert linked(summary, [cond("Diabetes", source="Condition/dm")]) == []

    def test_trailing_punctuation_does_not_block_a_match(self):
        # "Body mass index 30+" ends in "+", where an unconditional trailing \b
        # would demand a following word character and never match.
        summary = "Active findings include a body mass index 30+ due to obesity."
        fact = cond("Body mass index 30+ - obesity (finding)", source="Condition/bmi")
        assert linked(summary, [fact]) == [
            ("body mass index 30+", "Condition/bmi"),
            ("obesity", "Condition/bmi"),
        ]

    def test_matches_across_an_irregular_gap(self):
        summary = "History of viral  sinusitis."
        fact = cond("Viral sinusitis (disorder)", source="Condition/sinus-1")
        assert linked(summary, [fact]) == [("viral  sinusitis", "Condition/sinus-1")]

    def test_ambiguous_term_cites_the_newest_episode(self):
        # extract hands facts over newest-first; two episodes four years apart
        # are both on file and the link points at the most recent evidence.
        summary = "History of viral sinusitis."
        facts = [
            cond("Viral sinusitis (disorder)", source="Condition/sinus-2019", date="2019-01-02"),
            cond("Viral sinusitis (disorder)", source="Condition/sinus-2015", date="2015-01-02"),
        ]
        assert linked(summary, facts) == [("viral sinusitis", "Condition/sinus-2019")]

    def test_links_across_an_article_the_record_does_not_carry(self):
        # Three patients in the sample set are written up this way.
        summary = "A history of miscarriage in the first trimester, and a fracture of the ankle."
        facts = [
            cond("Miscarriage in first trimester", source="Condition/misc"),
            cond("Fracture of ankle", source="Condition/ankle", status="resolved"),
        ]
        assert linked(summary, facts) == [
            ("miscarriage in the first trimester", "Condition/misc"),
            ("fracture of the ankle", "Condition/ankle"),
        ]

    def test_links_a_pluralised_mention(self):
        # Coded displays are singular; prose is not.
        summary = "A notable history of normal pregnancies."
        assert linked(summary, [cond("Normal pregnancy", source="Condition/preg")]) == [
            ("normal pregnancies", "Condition/preg")
        ]

    def test_links_across_a_hyphen(self):
        summary = "The patient has a third-degree burn."
        assert linked(summary, [cond("Third degree burn", source="Condition/burn")]) == [
            ("third-degree burn", "Condition/burn")
        ]

    def test_a_plural_rule_does_not_apply_to_a_display_ending_in_punctuation(self):
        # "Body mass index 30+" must stay exactly itself.
        summary = "Findings include a body mass index 30+ today."
        fact = cond("Body mass index 30+ - obesity (finding)", source="Condition/bmi")
        assert linked(summary, [fact]) == [("body mass index 30+", "Condition/bmi")]

    def test_successive_mentions_of_one_ingredient_cite_different_products(self):
        # Both records contain acetaminophen, so handing every mention to the
        # first of them cites one record twice and the other never.
        summary = "Past medications such as Acetaminophen, Ibuprofen, and Acetaminophen / oxyCODONE."
        facts = [
            med("Acetaminophen 325 MG Oral Tablet", source="MedicationRequest/apap"),
            med("Ibuprofen 200 MG Oral Tablet", source="MedicationRequest/ibu"),
            med("Acetaminophen 325 MG / oxyCODONE Hydrochloride 5 MG Oral Tablet",
                source="MedicationRequest/apap-oxy"),
        ]
        assert linked(summary, medications=facts) == [
            ("Acetaminophen", "MedicationRequest/apap"),
            ("Ibuprofen", "MedicationRequest/ibu"),
            ("Acetaminophen", "MedicationRequest/apap-oxy"),
            ("oxyCODONE", "MedicationRequest/apap-oxy"),
        ]

    def test_empty_summary_yields_no_spans(self):
        assert find_spans("", [cond("Hypertension")], []) == []

    def test_unmatched_summary_yields_no_spans(self):
        summary = "The patient has no notable active problems."
        assert find_spans(summary, [cond("Hypertension")], []) == []


# The busiest patient in the sample set, as the stack actually serves them:
# prose from eval_output/eval-gemma3-4b.json, displays copied from a live
# GET /patients/5e4b559d-.../packet. Module-level so the invariants share one
# corpus.
REAL_SUMMARY = (
    "The patient\u2019s active conditions include stroke, non-small cell carcinoma of lung, "
    "and prostate neoplasm, alongside a history of osteoporosis and hyperlipidemia. The "
    "patient is currently taking Simvistatin, Docetaxel, Leuprolide Acetate, Sertraline, "
    "and Acetaminophen/Hydrocodone, with prior use of Cisplatin, Paclitaxel, and Clopidogrel."
)

REAL_CONDITIONS = [
    cond("Stroke", source="Condition/stroke"),
    cond("Non-small cell carcinoma of lung, TNM stage 1 (disorder)", source="Condition/nsclc-tnm"),
    cond("Non-small cell lung cancer (disorder)", source="Condition/nsclc"),
    cond("Suspected lung cancer (situation)", source="Condition/susp-lung"),
    cond("Neoplasm of prostate", source="Condition/prostate"),
    cond("Carcinoma in situ of prostate (disorder)", source="Condition/prostate-cis"),
    cond("Osteoporosis (disorder)", source="Condition/osteo"),
    cond("Hyperlipidemia", source="Condition/lipid"),
    cond("Body mass index 30+ - obesity (finding)", source="Condition/bmi"),
    cond("Chronic sinusitis (disorder)", source="Condition/sinus"),
    cond("Fracture of forearm", source="Condition/forearm", status="resolved"),
    cond("Pathological fracture due to osteoporosis (disorder)",
         source="Condition/path-fx", status="resolved"),
    cond("Acute viral pharyngitis (disorder)", source="Condition/pharyn", status="resolved"),
    cond("Streptococcal sore throat (disorder)", source="Condition/strep", status="resolved"),
]

REAL_MEDICATIONS = [
    med("Simvistatin 10 MG", source="MedicationRequest/simva"),
    med("1 ML DOCEtaxel 20 MG/ML Injection", source="MedicationRequest/doce"),
    med("0.25 ML Leuprolide Acetate 30 MG/ML Prefilled Syringe", source="MedicationRequest/leup"),
    med("Sertraline 100MG", source="MedicationRequest/sert"),
    med("Acetaminophen/Hydrocodone", source="MedicationRequest/apap-hydro"),
    med("Cisplatin 50 MG Injection", source="MedicationRequest/cis"),
    med("PACLitaxel 100 MG Injection", source="MedicationRequest/pac"),
    med("Clopidogrel 75 MG Oral Tablet", source="MedicationRequest/clop"),
    med("Meperidine Hydrochloride 50 MG Oral Tablet", source="MedicationRequest/meper"),
    med("Ibuprofen 200 MG Oral Tablet", source="MedicationRequest/ibu"),
    med("Alteplase 100 MG Injection", source="MedicationRequest/alte"),
]


def real_spans():
    return find_spans(REAL_SUMMARY, REAL_CONDITIONS, REAL_MEDICATIONS)


class TestInvariants:
    """The properties that make a span safe to render as a citation."""

    def test_every_span_slices_back_to_its_own_text(self):
        # The UI renders by slicing on these offsets; if one disagrees with its
        # text, every link after it is displaced.
        for span in real_spans():
            assert REAL_SUMMARY[span.start : span.end] == span.text

    def test_every_span_source_is_a_fact_in_the_packet(self):
        # No fabricated citation is structurally possible: a span can only
        # carry a source that came off a Fact.
        sources = {fact.source for fact in REAL_CONDITIONS + REAL_MEDICATIONS}
        assert {span.source for span in real_spans()} <= sources

    def test_spans_never_overlap_and_are_in_reading_order(self):
        last = 0
        for span in real_spans():
            assert span.start >= last
            assert span.end > span.start
            last = span.end

    def test_resolves_every_mention_the_record_spells_the_same_way(self):
        texts = {span.text.casefold() for span in real_spans()}
        assert texts == {
            "stroke",
            "non-small cell carcinoma of lung",
            "osteoporosis",
            "hyperlipidemia",
            "simvistatin",
            "docetaxel",
            "leuprolide acetate",
            "sertraline",
            "acetaminophen/hydrocodone",
            "cisplatin",
            "paclitaxel",
            "clopidogrel",
        }

    def test_the_only_miss_is_the_reordered_phrase(self):
        # "prostate neoplasm" vs the record's "Neoplasm of prostate". Everything
        # else the summary names is cited.
        linked_text = " ".join(span.text for span in real_spans())
        assert "prostate" not in linked_text

    def test_a_cited_term_does_not_leak_into_a_more_specific_sibling(self):
        # Three lung-cancer conditions are on file. The prose phrase matches the
        # staged one exactly, so that is the one cited -- not the vaguer
        # "Suspected lung cancer".
        lung = [s for s in real_spans() if "carcinoma of lung" in s.text]
        assert [s.source for s in lung] == ["Condition/nsclc-tnm"]
