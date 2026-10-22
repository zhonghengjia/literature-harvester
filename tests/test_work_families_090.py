"""Changed-title version families: public aggregate abstracts, no private paths.

The two regression abstracts preserve the publicly reported aggregate evidence
from doi:10.21203/rs.3.rs-5012106/v1 and doi:10.1186/s12887-025-05815-1.
Fixture author names, candidate IDs and DOI bindings are synthetic.  No patient
records, locally downloaded objects or private replay paths are required.
"""
from copy import deepcopy
import itertools
import json
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lit_harvest.models import PaperRecord
from lit_harvest.work_families import build_work_families


PREPRINT_ABSTRACT = """Abstract Background The use of sodium bicarbonate to correct metabolic acidosis in patients is common in clinical practice, but the efficacy is still controversial. The aim of the study is to assess the efficacy of sodium bicarbonate in treating non-lactic and lactic metabolic acidosis, as well as its effectiveness at various chloride levels. Methods A retrospective cohort study was conducted by screening pediatric patients diagnosed with metabolic acidosis from PIC database. Patients were categorized into two groups: lactate patients (lactate > 2 mmol/L) and non-lactate patients (lactate ≤ 2 mmol/L). The risk of in-hospital and 28-day mortality for sodium bicarbonate treatment was assessed. Results A comprehensive analysis was conducted on a total of 5814 patients, with 3184 (54.76%) being lactate patients. In lactate patients, when chloride < 107 mmol/L, those who received sodium bicarbonate therapy exhibited a significantly elevated risk of both in-hospital mortality (adjusted OR = 2.195, 95% CI: 1.536–3.135, p < 0.001) and 28-day mortality (adjusted OR = 2.195, 95% CI: 1.434–2.989, p < 0.001). Conversely, when chloride ≥ 113 mmol/L, patients who received sodium bicarbonate had a decreased risk of in-hospital mortality and 28-day mortality, with adjusted OR values of 0.365 (95% CI: 0.217–0.614, p < 0.001) and 0.396 (95% CI: 0.232–0.675, p = 0.001), respectively. Similar findings were observed in non-lactate patients. Conclusions Regardless of lactate levels, sodium bicarbonate was effective in reducing the risk of death in patients with hyperchloremia, while increasing the risk of death in non-hyperchloremic patients."""
JOURNAL_ABSTRACT = """Background Sodium bicarbonate is commonly used to correct metabolic acidosis in pediatric patients, yet its efficacy remains controversial. This study aims to assess its effectiveness in treating non-lactic and lactic metabolic acidosis and its impact at various chloride levels. Methods A retrospective cohort study was conducted by screening pediatric patients diagnosed with metabolic acidosis from a paediatric intensive care database. Patients were categorized into two groups: lactate patients (lactate > 2.0 mmol/L) and non-lactate patients (lactate ≤ 2.0 mmol/L). The risk of death in patients who received sodium bicarbonate was assessed. Results Sodium bicarbonate treatment did not significantly affect in-hospital mortality in either overall lactate patients or non-lactate patients, with adjusted OR of 1.044 (95% CI: 0.829-1.315, p = 0.714) and 0.838 (95% CI: 0.548-1.281, p = 0.414), respectively. In lactate patients, those receiving sodium bicarbonate had a higher risk of in-hospital death when chloride was < 107 mmol/L (adjusted OR = 2.195, 95% CI: 1.536-3.135, p < 0.001), whereas the risk of in-hospital death decreased when chloride was ≥ 113 mmol/L (adjusted OR = 0.365, 95% CI: 0.217-0.614, p < 0.001). Similar findings were observed in non-lactate patients. Conclusions Sodium bicarbonate treatment does not improve the survival of pediatric patients with lactate or non-lactate metabolic acidosis. However, it can reduce the odds of mortality in pediatric patients with hyperchloremia, regardless of their lactate levels. Prospective studies are needed to further confirm these findings."""


def pair():
    shared = {"authors": ["Ames D", "Baker L", "Chen X", "Dawson S", "Evans H"]}
    a = PaperRecord(**shared, title="Efficacy of sodium bicarbonate in treating lactic and non-lactic metabolic acidosis at different chloride levels",
                    year=2024, doi="10.5555/draft", abstract=PREPRINT_ABSTRACT, item_type="preprint",
                    extra={"candidate_id": "draft", "publication_types": ["preprint"]})
    b = PaperRecord(**shared, title="Sodium bicarbonate in treating lactic and non-lactic metabolic acidosis at different chloride levels: a retrospective study",
                    year=2025, doi="10.5555/published", abstract=JOURNAL_ABSTRACT, item_type="journalArticle",
                    extra={"candidate_id": "published", "publication_types": ["journal article"]})
    return a, b


def provider_edge(subject, target, relation="is-preprint-of"):
    subject.extra.setdefault("version_relations", []).append({
        "relation": relation, "target_id_type": "doi", "target_id": target.doi,
        "source": "crossref", "source_record_id": "doi:" + subject.doi,
        "source_metadata_sha256": "b" * 64,
        "evidence_url": "https://api.crossref.org/works/" + subject.doi})


class WorkFamilies090Tests(unittest.TestCase):
    def assert_separate(self, records):
        result = build_work_families(records)
        self.assertEqual(result["counts"]["work_family_count"], len(records))
        self.assertEqual(result["counts"]["accepted_metadata_relations"], 0)
        return result

    def test_real_edited_abstracts_have_two_contextual_fingerprints_not_external_confirmation(self):
        result = build_work_families(pair())
        self.assertEqual(result["counts"]["work_family_count"], 1)
        self.assertEqual(result["counts"]["confirmed_layer_family_count"], 2)
        self.assertEqual(result["counts"]["confirmed_relation_families"], 0)
        self.assertEqual(result["counts"]["high_confidence_metadata_families"], 1)
        self.assertEqual(result["families"][0]["representative_candidate_id"], "published")
        evidence = result["relations"][0]["evidence"]
        self.assertEqual(evidence["metadata_method"], "changed_title_multievidence")
        self.assertFalse(evidence["abstracts"]["strong"])
        fingerprint = evidence["clinical_fingerprint"]
        self.assertEqual(fingerprint["shared_result_group_count"], 2)
        self.assertEqual({g["point"] for g in fingerprint["shared_result_groups"]}, {"0.365", "2.195"})
        self.assertEqual(fingerprint["conflicts"], [])
        self.assertEqual(fingerprint["sample_size_status"], "missing_on_one_or_both_sides_not_confirmed")
        self.assertFalse(result["pdf_identity_shared"])

    def test_different_topic_and_numbers_are_not_hard_coded(self):
        records = pair()
        replacements = (("sodium bicarbonate", "intravenous antioxidant"), ("Sodium bicarbonate", "Intravenous antioxidant"),
                        ("chloride", "glucose"), ("lactate", "ketone"), ("hyperchloremia", "hyperglycemia"),
                        ("2.195", "1.875"), ("1.536", "1.436"), ("0.365", "0.465"),
                        ("0.217", "0.317"), ("5814", "4267"))
        for record in records:
            for old, new in replacements:
                record.abstract = record.abstract.replace(old, new)
                record.title = record.title.replace(old, new)
        self.assertEqual(build_work_families(records)["counts"]["work_family_count"], 1)

    def test_completely_reworded_specific_titles_can_be_blocked_by_authors_and_abstract(self):
        a, b = pair()
        b.title = "Mortality associations after biochemical therapy across distinct pediatric intensive care profiles"
        result = build_work_families([a, b])
        self.assertEqual(result["counts"]["work_family_count"], 1)
        self.assertFalse(result["relations"][0]["evidence"]["title_evidence"]["candidate"])

    def test_no_record_cid_screening_doi_or_pdf_binding_is_changed(self):
        a, b = pair()
        a.extra["screening"] = {"decision": "uncertain"}
        b.extra["screening"] = {"decision": "include"}
        a.local_pdf, a.sha256 = "draft.pdf", "draft-sha"
        b.local_pdf, b.sha256 = "journal.pdf", "journal-sha"
        before = deepcopy([a.to_dict(), b.to_dict()])
        result = build_work_families([a, b])
        self.assertEqual([a.to_dict(), b.to_dict()], before)
        self.assertEqual({m["candidate_id"] for m in result["members"]}, {"draft", "published"})
        self.assertNotIn("local_pdf", json.dumps(result))

    def test_output_and_family_representative_are_order_invariant(self):
        a, b = pair()
        c = PaperRecord(title="An unrelated source", doi="10.5555/unrelated", extra={"candidate_id": "other"})
        expected = build_work_families([a, b, c])
        for records in itertools.permutations([a, b, c]):
            self.assertEqual(build_work_families(records), expected)

    def test_known_total_sample_conflict_is_not_hidden_by_shared_result_values(self):
        a, b = pair()
        b.abstract = b.abstract.replace("Methods A retrospective", "Methods A total of 6000 patients were enrolled. A retrospective")
        result = self.assert_separate([a, b])
        fingerprint = result["review_candidates"][0]["evidence"]["clinical_fingerprint"]
        self.assertEqual(fingerprint["sample_size_status"], "explicit_conflict")

    def test_explicit_cohort_n_notation_conflict(self):
        a, b = pair()
        a.abstract = a.abstract.replace("total of 5814 patients", "cohort (N=5814) patients")
        b.abstract = b.abstract.replace("Methods A retrospective", "Methods Cohort (N=7000). A retrospective")
        self.assert_separate([a, b])

    def test_same_team_but_adult_instead_of_pediatric_cohort_is_separate(self):
        a, b = pair()
        b.abstract = b.abstract.replace("pediatric", "adult").replace("paediatric", "adult")
        result = self.assert_separate([a, b])
        conflicts = result["review_candidates"][0]["evidence"]["clinical_fingerprint"]["conflicts"]
        self.assertIn("population_age", {c["field"] for c in conflicts})

    def test_same_team_but_different_recruited_condition_is_separate(self):
        a, b = pair()
        b.abstract = b.abstract.replace("diagnosed with metabolic acidosis", "diagnosed with respiratory failure")
        result = self.assert_separate([a, b])
        conflicts = result["review_candidates"][0]["evidence"]["clinical_fingerprint"]["conflicts"]
        self.assertIn("cohort_condition", {c["field"] for c in conflicts})

    def test_conflicting_recruitment_windows_design_or_trial_id_stay_separate(self):
        for left, right in (("between 2010 and 2014", "between 2015 and 2019"),
                            ("trial NCT00000001", "trial NCT00000002")):
            a, b = pair()
            a.abstract = a.abstract.replace("Methods ", "Methods " + left + ". ")
            b.abstract = b.abstract.replace("Methods ", "Methods " + right + ". ")
            with self.subTest(left=left):
                self.assert_separate([a, b])
        a, b = pair()
        b.abstract = b.abstract.replace("retrospective", "prospective")
        self.assert_separate([a, b])

    def test_point_or_ci_conflict_cannot_pass_with_one_remaining_shared_group(self):
        for old, new in (("0.365", "0.565"), ("1.536-3.135", "1.636-3.235")):
            a, b = pair()
            b.abstract = b.abstract.replace(old, new)
            with self.subTest(changed=old):
                result = self.assert_separate([a, b])
                fingerprint = result["review_candidates"][0]["evidence"]["clinical_fingerprint"]
                self.assertIn("stratified_effect_ci", {c["field"] for c in fingerprint["conflicts"]})

    def test_same_effects_assigned_to_different_strata_are_not_agreement(self):
        a, b = pair()
        b.abstract = b.abstract.replace("< 107", "< 103").replace("≥ 113", "≥ 118")
        self.assert_separate([a, b])

    def test_missing_short_or_no_result_abstracts_only_review(self):
        for abstract in ("", "Background Metabolic acidosis is a common clinical problem.",
                         JOURNAL_ABSTRACT.split(" Results ")[0] + " Results No detailed outcomes were reported."):
            a, b = pair()
            b.abstract = abstract
            with self.subTest(abstract=abstract[:20]):
                result = self.assert_separate([a, b])
                self.assertTrue(result["review_candidates"])

    def test_one_repeated_numeric_tuple_is_not_two_independent_fingerprints(self):
        a, b = pair()
        b.abstract = b.abstract.replace("0.365", "0.565")
        b.abstract += " In the same analysis, adjusted OR = 2.195, 95% CI: 1.536-3.135."
        self.assert_separate([a, b])

    def test_incompatible_sparse_authors_roles_or_dates_do_not_merge(self):
        for change in ("authors", "single_author", "year", "role"):
            a, b = pair()
            if change == "authors":
                b.authors = ["Other Z", "Different Q", "Unique W"]
            elif change == "single_author":
                a.authors = b.authors = ["Ames D"]
            elif change == "year":
                a.year = 2010
            else:
                a.item_type = "journalArticle"
                a.extra["publication_types"] = ["journal article"]
            with self.subTest(change=change):
                self.assert_separate([a, b])

    def test_bound_negative_provider_relation_overrides_changed_title_metadata(self):
        for relation in ("is-translation-of", "is-comment-on", "is-correction-of", "is-retraction-of"):
            a, b = pair()
            provider_edge(a, b, relation)
            with self.subTest(relation=relation):
                result = self.assert_separate([a, b])
                self.assertEqual(result["counts"]["accepted_provider_relations"], 0)

    def test_notice_and_restricted_types_not_overridden_by_numeric_similarity(self):
        for restricted in ("comment", "letter", "correction", "translation", "retraction"):
            a, b = pair()
            b.extra["publication_types"].append(restricted)
            self.assert_separate([a, b])

    def test_changed_title_bridge_to_two_journals_is_quarantined(self):
        a, b = pair()
        c = deepcopy(b)
        c.doi, c.extra["candidate_id"] = "10.5555/second-journal", "second"
        c.title = "Retrospective outcomes of biochemical therapy among pediatric acidosis patients with altered electrolytes"
        result = self.assert_separate([a, b, c])
        self.assertEqual(result["counts"]["blocked_relations"], 2)
        self.assertEqual(result, build_work_families([c, a, b]))

    def test_missing_n_bridge_cannot_connect_two_conflicting_known_cohorts(self):
        a, b = pair()
        c = deepcopy(a)
        c.doi, c.extra["candidate_id"] = "10.5555/second-draft", "other-draft"
        c.abstract = c.abstract.replace("5814", "6000")
        c.title = "Retrospective mortality evidence for biochemical treatment across pediatric acidosis profiles"
        self.assert_separate([a, b, c])

    def test_changed_title_bridge_preserves_confirmed_provider_component(self):
        a, b = pair()
        provider_edge(a, b)
        c = deepcopy(b)
        c.doi, c.extra["candidate_id"] = "10.5555/second-journal", "second"
        c.title = "Retrospective outcomes of biochemical therapy among pediatric acidosis patients with altered electrolytes"
        result = build_work_families([a, b, c])
        self.assertEqual(result["counts"]["confirmed_relation_families"], 1)
        self.assertEqual(result["counts"]["work_family_count"], 2)
        self.assertEqual(result["counts"]["accepted_metadata_relations"], 0)

    def test_decimal_format_and_unicode_dashes_do_not_change_result_identity(self):
        a, b = pair()
        b.abstract = b.abstract.replace("2.195", "2.1950").replace("1.536-3.135", "1.536 to 3.135")
        self.assertEqual(build_work_families([a, b])["counts"]["work_family_count"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
