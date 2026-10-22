"""Offline positive/negative contracts for the non-destructive family layer."""
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


TITLE = "Distinctive physiological predictors of recovery following experimental electrolyte perturbation"
ABSTRACT = """Background Our prospective investigation examined electrolyte trajectories following a controlled perturbation.
Methods Participants underwent baseline sodium potassium chloride bicarbonate magnesium calcium phosphate glucose lactate
creatinine albumin urea osmolality hemoglobin platelet leukocyte measurements. Investigators recorded temperature pressure
ventilation oxygenation perfusion filtration diuresis hydration nutrition medication exposure severity comorbidity duration
admission discharge survival recovery relapse complications independently. Recruitment involved 427 participants across
three regional hospitals during consecutive winter months. Electronic records provided hourly laboratory values until
normalization. Results Persistent biochemical discordance predicted delayed functional recovery after adjustment for
demographic characteristics and concurrent treatments. Trajectories separated into four reproducible patterns including
rapid normalization sustained elevation oscillating concentrations and gradual improvement. External validation preserved
discrimination calibration stability robustness precision sensitivity specificity accuracy interpretability generalizability
and reproducibility. Conclusions Repeated physiological measurements characterized heterogeneous recovery and supported
independent prospective replication without establishing therapeutic efficacy or causal effects."""


def record(cid, role="journal", year=2025, doi=None, **kwargs):
    data = dict(title=TITLE, authors=["Smith AB", "Jones CD", "Martin E"], year=year,
                abstract=ABSTRACT, doi=doi or f"10.5555/{cid}",
                item_type="preprint" if role == "preprint" else "journalArticle",
                extra={"candidate_id": cid, "publication_types": ["preprint" if role == "preprint" else "journal article"]})
    data.update(kwargs)
    return PaperRecord(**data)


def declaration(subject, target, relation="is-preprint-of", **changes):
    edge = {"relation": relation, "target_id": target.doi, "target_id_type": "doi",
            "source": "crossref", "source_record_id": f"doi:{subject.doi}",
            "evidence_url": f"https://api.crossref.org/works/{subject.doi}",
            "source_metadata_sha256": "a" * 64,
            "raw_relation": {"id": target.doi, "id-type": "doi", "asserted-by": "subject"}}
    edge.update(changes)
    subject.extra.setdefault("version_relations", []).append(edge)
    return edge


class WorkFamilies080Tests(unittest.TestCase):
    def test_empty_contract(self):
        result = build_work_families([])
        self.assertEqual(result["counts"]["record_count"], 0)
        self.assertEqual(result["families"], [])
        self.assertFalse(result["pdf_identity_shared"])

    def test_explicit_provider_relation_confirmed_without_title_inference(self):
        a, b = record("p", "preprint", 2022), record("j", title="A substantially revised publication title")
        declaration(a, b)
        out = build_work_families([a, b])
        self.assertEqual(out["counts"]["confirmed_relation_families"], 1)
        self.assertEqual(out["counts"]["high_confidence_metadata_families"], 0)
        self.assertEqual(out["families"][0]["representative_candidate_id"], "j")

    def test_metadata_has_separate_confirmed_denominator(self):
        a, b = record("p", "preprint", 2023), record("j")
        out = build_work_families([a, b])
        self.assertEqual(out["counts"]["work_family_count"], 1)
        self.assertEqual(out["counts"]["confirmed_layer_family_count"], 2)
        self.assertEqual(out["counts"]["confirmed_relation_families"], 0)
        self.assertEqual(out["counts"]["high_confidence_metadata_families"], 1)
        self.assertEqual(out["families"][0]["classification"], "high_confidence_metadata")

    def test_does_not_mutate_or_bind_pdf_or_screening(self):
        a, b = record("p", "preprint", 2023), record("j")
        a.local_pdf, a.sha256 = "preprint.pdf", "preprint-hash"
        b.local_pdf, b.sha256 = "published.pdf", "published-hash"
        a.extra["screening"] = {"decision": "exclude"}
        b.extra["screening"] = {"decision": "include"}
        before = deepcopy([a.to_dict(), b.to_dict()])
        out = build_work_families([a, b])
        self.assertEqual(before, [a.to_dict(), b.to_dict()])
        self.assertEqual({m["candidate_id"] for m in out["members"]}, {"p", "j"})
        self.assertNotIn("local_pdf", json.dumps(out))
        self.assertNotIn("screening", json.dumps(out))

    def test_order_invariance_for_every_permutation(self):
        a, b, c = record("p", "preprint", 2023), record("j"), record("other", title="An unrelated independent experiment")
        expected = build_work_families([a, b, c])
        for permutation in itertools.permutations([a, b, c]):
            self.assertEqual(build_work_families(permutation), expected)

    def test_same_title_without_abstract_only_review(self):
        a, b = record("p", "preprint", 2023, abstract=""), record("j", abstract="")
        out = build_work_families([a, b])
        self.assertEqual(out["counts"]["work_family_count"], 2)
        self.assertTrue(out["review_candidates"])

    def test_same_title_and_abstract_without_compatible_authors_only_review(self):
        out = build_work_families([record("p", "preprint", 2023), record("j", authors=["Other A", "Different B"])])
        self.assertEqual(out["counts"]["work_family_count"], 2)

    def test_changed_first_author_can_match_multiple_other_compatible_authors(self):
        a, b = record("p", "preprint", 2023), record("j", authors=["New F", "Jones C", "Martin EF", "Smith A"])
        out = build_work_families([a, b])
        self.assertEqual(out["counts"]["high_confidence_metadata_families"], 1)

    def test_generic_title_same_role_or_implausible_time_not_auto_merged(self):
        pairs = [(record("p", "preprint", 2023, title="Electrolyte disorders"), record("j", title="Electrolyte disorders")),
                 (record("j1"), record("j2")), (record("p", "preprint", 2010), record("j")),
                 (record("p", "preprint", 2027), record("j", year=2024))]
        for pair in pairs:
            with self.subTest(ids=[r.extra["candidate_id"] for r in pair]):
                self.assertEqual(build_work_families(pair)["counts"]["work_family_count"], 2)

    def test_notice_comment_reply_and_translation_not_strong_merged(self):
        for restricted in ("comment", "letter", "correction", "published erratum", "retraction of publication", "translation", "editorial", "preprint-withdrawal", "preprint-removal"):
            a, b = record("p", "preprint", 2023), record("j")
            b.extra["publication_types"].append(restricted)
            declaration(a, b)
            out = build_work_families([a, b])
            self.assertEqual(out["counts"]["work_family_count"], 2, restricted)
        a, b = record("p", "preprint", 2023), record("j", title="Reply: " + TITLE)
        declaration(a, b)
        self.assertEqual(build_work_families([a, b])["counts"]["work_family_count"], 2)

    def test_nonversion_relation_blocks_metadata_merge(self):
        a, b = record("p", "preprint", 2023), record("j")
        declaration(a, b, "isTranslationOf")
        out = build_work_families([a, b])
        self.assertEqual(out["counts"]["work_family_count"], 2)
        self.assertEqual(out["relations"][0]["relation"], "is-translation-of")

    def test_host_true_labels_and_unbound_provider_not_confirmation(self):
        for changes in ({"source": "host-ai", "confirmed": True}, {"source_record_id": "doi:10.5555/not-subject"},
                        {"source_metadata_sha256": ""}, {"source": "host-ai", "confirmed": False},
                        {"evidence_url": "https://evil.example/evidence"}, {"evidence_url": "https://[invalid"}):
            a, b = record("p", "preprint", 2023, abstract=""), record("j", abstract="")
            declaration(a, b, **changes)
            out = build_work_families([a, b])
            self.assertEqual(out["counts"]["confirmed_relation_families"], 0, changes)
            self.assertEqual(out["counts"]["work_family_count"], 2)

    def test_arbitrary_boolean_labels_do_not_override_provider_declaration(self):
        for value in (False, True):
            a, b = record("p", "preprint", 2023, abstract=""), record("j", abstract="")
            declaration(a, b, confirmed=value, accepted=value)
            self.assertEqual(build_work_families([a, b])["counts"]["confirmed_relation_families"], 1)

    def test_shared_background_without_shared_results_is_not_distinctive_identity(self):
        common = ABSTRACT.split(" Results ")[0]
        ending_a = " Results " + " ".join(f"experiment{i}" for i in range(70))
        ending_b = " Results " + " ".join(f"validation{i}" for i in range(70))
        a, b = record("p", "preprint", 2023, abstract=common + ending_a), record("j", abstract=common + ending_b)
        self.assertEqual(build_work_families([a, b])["counts"]["work_family_count"], 2)

    def test_target_missing_or_ambiguous_not_silently_guessed(self):
        a, b = record("p", "preprint", 2023, abstract=""), record("j", abstract="")
        declaration(a, b)
        self.assertEqual(build_work_families([a])["relations"][0]["reason"], "target_not_in_inventory")
        c = record("j2", doi=b.doi, abstract="")
        out = build_work_families([a, b, c])
        self.assertEqual(out["counts"]["confirmed_relation_families"], 0)
        self.assertEqual(out["relations"][0]["reason"], "ambiguous_target")

    def test_duplicate_candidate_ids_raise_instead_of_dropping_records(self):
        with self.assertRaisesRegex(ValueError, "Duplicate candidate IDs"):
            build_work_families([record("same"), record("same", doi="10.5555/other")])

    def test_epmc_source_alias_and_self_edges(self):
        a, b = record("p1", "preprint", 2023), record("p2", "preprint", 2024)
        a.url, b.url = "https://europepmc.org/article/PPR/PPR100", "https://europepmc.org/article/PPR/PPR200"
        declaration(a, b, "is-version-of", target_id_type="europe_pmc", target_id="PPR:PPR200",
                    source="europe_pmc", source_record_id="europe_pmc:PPR:PPR100",
                    evidence_url="https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=EXT_ID:PPR100&format=json")
        declaration(b, b, "is-version-of")
        out = build_work_families([a, b])
        self.assertEqual(out["counts"]["confirmed_relation_families"], 1)
        self.assertIn("self_relation", {e["reason"] for e in out["relations"]})
        self.assertNotIn("?", next(e for e in out["relations"] if e["source"] == "europe_pmc")["evidence_url"])

    def test_contradictory_preprint_direction_not_confirmed(self):
        a, b = record("p", "preprint", 2023, abstract=""), record("j", abstract="")
        declaration(a, b, "has-preprint")
        self.assertEqual(build_work_families([a, b])["relations"][0]["reason"], "contradictory_preprint_direction")

    def test_explicit_bridge_to_two_unlinked_journal_dois_quarantined(self):
        a, b, c = record("p", "preprint", 2023, abstract=""), record("j1", abstract=""), record("j2", abstract="")
        declaration(a, b)
        declaration(a, c)
        out = build_work_families([c, b, a])
        self.assertEqual(out["counts"]["work_family_count"], 3)
        self.assertEqual(out["counts"]["blocked_relations"], 2)
        self.assertEqual(out, build_work_families([a, b, c]))

    def test_metadata_bridge_does_not_revoke_confirmed_family(self):
        a, b, c = record("p", "preprint", 2023), record("j1"), record("j2")
        declaration(a, b)
        out = build_work_families([a, b, c])
        self.assertEqual(out["counts"]["confirmed_relation_families"], 1)
        self.assertEqual(out["counts"]["work_family_count"], 2)
        self.assertEqual(out["counts"]["high_confidence_metadata_families"], 0)

    def test_quarantined_provider_bridge_cannot_reenter_through_metadata(self):
        a, b, c = record("p", "preprint", 2023), record("j1"), record("j2", title="Another published work")
        declaration(a, b)
        declaration(a, c)
        out = build_work_families([a, b, c])
        self.assertEqual(out["counts"]["work_family_count"], 3)
        self.assertEqual(out["counts"]["high_confidence_metadata_families"], 0)

    def test_three_confirmed_versions_with_direct_journal_version_relation(self):
        a, b, c = record("p", "preprint", 2023, abstract=""), record("j1", abstract=""), record("j2", abstract="")
        declaration(a, b)
        declaration(b, c, "is-version-of")
        declaration(a, c)
        out = build_work_families([a, b, c])
        self.assertEqual(out["counts"]["confirmed_relation_families"], 1)
        self.assertEqual(out["counts"]["work_family_count"], 1)

    def test_mapping_inputs_and_derived_ids(self):
        a, b = record("p", "preprint", 2023).to_dict(), record("j").to_dict()
        del a["extra"]["candidate_id"]
        before = deepcopy([a, b])
        out = build_work_families([a, b])
        self.assertEqual([a, b], before)
        self.assertEqual(len(out["members"]), 2)
        self.assertIn("derived", {m["candidate_id_origin"] for m in out["members"]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
