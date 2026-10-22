"""Offline behavioral checks for 0.8 discovery, including protected frozen replay."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lit_harvest.coverage_plan import build_coverage_plan
from lit_harvest.models import PaperRecord
from lit_harvest.query_plan import validate_plan
from lit_harvest.sources import _parse_pubmed_xml, _retain_version_metadata, _strip_tags, search_pubmed, search_crossref, search_europe_pmc


def journal(pmid="10001", title="Chloride outcomes"):
    return f'<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article><ArticleTitle>{title}</ArticleTitle><Journal><Title>Journal</Title><JournalIssue><PubDate><MedlineDate>2022 Jan-Feb</MedlineDate></PubDate></JournalIssue></Journal></Article></MedlineCitation></PubmedArticle>'


BOOK = '''<PubmedBookArticle><BookDocument><PMID>10002</PMID>
<ArticleIdList><ArticleId IdType="bookaccession">NBK12345</ArticleId><ArticleId IdType="doi">10.1234/chapter</ArticleId></ArticleIdList>
<Book><Publisher><PublisherName>Publisher</PublisherName><PublisherLocation>London</PublisherLocation></Publisher>
<BookTitle>Neonatal nutrition</BookTitle><PubDate><Year>2020</Year></PubDate>
<AuthorList Type="authors"><Author><CollectiveName>Book Alliance</CollectiveName></Author></AuthorList>
<Isbn>1234567890</Isbn><ELocationID EIdType="doi">10.1234/book</ELocationID></Book>
<ArticleTitle>Acetate and chloride</ArticleTitle><AuthorList Type="authors"><Author><Initials>JD</Initials><LastName>Doe</LastName></Author><Author><CollectiveName>Chapter Group</CollectiveName></Author></AuthorList>
<PublicationType>Review</PublicationType><Abstract><AbstractText Label="RESULTS">Acetate reduces hyperchloraemia.</AbstractText></Abstract>
</BookDocument><PubmedBookData><ArticleIdList><ArticleId IdType="pubmed">10002</ArticleId></ArticleIdList></PubmedBookData></PubmedBookArticle>'''


class Response:
    def __init__(self, body): self.body = body
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def read(self, size=-1): return self.body if size < 0 else self.body[:size]


class Client:
    def __init__(self, raw, ids): self.raw, self.ids = raw, ids
    def get_json(self, *_): return {"esearchresult": {"count": str(len(self.ids)), "idlist": self.ids}}
    def request(self, *_args, **_kwargs): return Response(self.raw)


def plan():
    return {"schema_version": 1, "question": "Chloride and neonatal nutrition",
            "concepts": [{"id": "chloride", "terms": ["hyperchloremia", "hyperchloraemia", "high serum chloride"]},
                         {"id": "nutrition", "terms": ["parenteral nutrition", "acetate"]}],
            "routes": [{"id": "pubmed", "source": "pubmed", "concept_ids": ["chloride", "nutrition"], "limit": 100},
                       {"id": "epmc", "source": "europe_pmc", "concept_ids": ["chloride", "nutrition"], "limit": 100}]}


class Discovery080Tests(unittest.TestCase):
    def test_abstract_numeric_comparisons_survive_real_fragment_shapes(self):
        # Minimal fragments of three observed forms, not complete abstracts.
        fixtures = [
            ('<h4>Results</h4><i>P</i> < 0.001. Chloride elevation.<h4>Conclusion</h4>Stable electrolytes.',
             'Results P < 0.001. Chloride elevation. Conclusion Stable electrolytes.'),
            ('<h4>Results</h4><i>P</i> < 0.001. Serum chlorides were comparable between groups on day 4.<h4>Conclusion</h4>RLS.',
             'Results P < 0.001. Serum chlorides were comparable between groups on day 4. Conclusion RLS.'),
            ('<h4>Results</h4>chloride was < 107 mmol/L; p < 0.001; chloride was ≥ 113 mmol/L.<h4>Conclusions</h4>Survival.',
             'Results chloride was < 107 mmol/L; p < 0.001; chloride was ≥ 113 mmol/L. Conclusions Survival.'),
        ]
        for raw, expected in fixtures:
            with self.subTest(raw=raw):
                self.assertEqual(_strip_tags(raw), expected)

    def test_metadata_markup_and_entities_do_not_reparse_comparisons(self):
        self.assertEqual(_strip_tags('<p>Cl &lt; 107 &amp; p &lt; 0.05; Cl > 113.</p><p>Next</p>'),
                         'Cl < 107 & p < 0.05; Cl > 113. Next')
        self.assertEqual(_strip_tags('Cl<107; Cl>113; x < y and z > q'),
                         'Cl<107; Cl>113; x < y and z > q')
        self.assertEqual(_strip_tags('<p>hyper<i>chloremia</i> &lt;b&gt;literal&lt;/b&gt;</p>'),
                         'hyperchloremia <b>literal</b>')
        self.assertEqual(_strip_tags(''), '')
        self.assertEqual(_strip_tags(None), '')

    def test_interleaved_book_chapter_authors_identifiers_no_oa_inference(self):
        raw = ("<PubmedArticleSet>" + journal() + BOOK + journal("10003") + "</PubmedArticleSet>").encode()
        status = {}
        rows = _parse_pubmed_xml(raw, 10, status=status)
        self.assertEqual([r.pmid for r in rows], ["10001", "10002", "10003"])
        self.assertEqual(rows[0].year, 2022)
        book = rows[1]
        self.assertEqual(book.item_type, "bookSection")
        self.assertEqual(book.authors, ["JD Doe", "Chapter Group"])
        self.assertEqual(book.extra["book_authors"], ["Book Alliance"])
        self.assertEqual(book.doi, "10.1234/chapter")
        self.assertEqual(book.extra["book_doi"], "10.1234/book")
        self.assertEqual(book.extra["isbn"], ["1234567890"])
        self.assertIn("NBK12345", book.extra["declared_links"][0]["url"])
        self.assertIsNone(book.is_oa)
        self.assertEqual(book.pdf_candidates, [])
        self.assertEqual(status["raw_rows"], 3)

    def test_whole_book_fallback_and_standalone_book_set(self):
        raw = BOOK.replace("<ArticleTitle>Acetate and chloride</ArticleTitle>", "")
        raw = raw.replace('<AuthorList Type="authors"><Author><Initials>JD</Initials><LastName>Doe</LastName></Author><Author><CollectiveName>Chapter Group</CollectiveName></Author></AuthorList>', '')
        book = _parse_pubmed_xml(("<PubmedBookArticleSet>" + raw + "</PubmedBookArticleSet>").encode(), 1)[0]
        self.assertEqual((book.title, book.item_type, book.authors), ("Neonatal nutrition", "book", ["Book Alliance"]))

    def test_bad_row_preserves_page_and_reports_missing_ids(self):
        raw = ("<PubmedArticleSet>" + journal() + journal("10002", "") + journal("10003") + "</PubmedArticleSet>").encode()
        with patch("lit_harvest.sources.time.sleep"):
            rows = search_pubmed(Client(raw, ["10001", "10002", "10003", "10004"]), "chloride", 4)
        self.assertEqual([r.pmid for r in rows], ["10001", "10003"])
        status = rows.source_status
        self.assertEqual((status["raw_rows"], status["parsed_rows"], status["skipped_rows"]), (3, 2, 1))
        self.assertEqual(status["missing_requested_ids"], ["10002", "10004"])
        self.assertEqual(status["missing_response_ids"], ["10004"])
        self.assertEqual(status["row_error_count"], 1)
        self.assertEqual(status["status"], "partial")

    def test_coverage_immutable_fixed_budgets_and_concept_boolean(self):
        original = plan()
        before = deepcopy(original)
        result = build_coverage_plan(original, 101, mode="fixed-budget")
        self.assertEqual(original, before)
        self.assertEqual(result["original_plan"], before)
        self.assertEqual(result["experiment_type"], "same_budget_strategy_experiment")
        routes = validate_plan(result["query_plan"])["routes"]
        for source in ("pubmed", "europe_pmc"):
            selected = [r for r in routes if r["source"] == source]
            self.assertEqual([r["limit"] for r in selected], [51, 50])
            self.assertIn(" AND ", selected[1]["query"])
            self.assertIn(" OR ", selected[1]["query"])
            self.assertNotIn("Title/Abstract", selected[1]["query"])
            self.assertNotIn("TITLE_ABS", selected[1]["query"])
        self.assertNotIn('"hyperchloremia"', routes[1]["query"])
        self.assertIn('"hyperchloremia"', routes[3]["query"])

    def test_normalized_exact_plan_supported_but_arbitrary_native_rejected(self):
        self.assertEqual(len(build_coverage_plan(validate_plan(plan()), mode="fixed-budget")["query_plan"]["routes"]), 4)
        changed = plan()
        changed["routes"][0].pop("concept_ids")
        changed["routes"][0]["query"] = "chloride AND infant"
        with self.assertRaisesRegex(ValueError, "not reverse-parsed"):
            build_coverage_plan(changed, mode="fixed-budget")
        for budget in (True, 1, 0, 1.5):
            with self.assertRaises(ValueError): build_coverage_plan(plan(), budget, mode="fixed-budget")

    def test_multiple_routes_and_unsafe_atm_terms_fail_closed(self):
        original = plan()
        original["routes"].append({"id": "pm_second", "source": "pubmed", "concept_ids": ["chloride"], "limit": 10})
        result = build_coverage_plan(original, 101, mode="fixed-budget")
        self.assertEqual(sum(r["limit"] for r in result["query_plan"]["routes"] if r["source"] == "pubmed"), 101)
        original["concepts"][0]["terms"] = ["chloride OR sodium"]
        with self.assertRaisesRegex(ValueError, "plain words"):
            build_coverage_plan(original, mode="fixed-budget")

    def test_pipeline_uses_route_quotas_without_source_recapping(self):
        from lit_harvest.pipeline import search_all
        from lit_harvest.settings import DEFAULT_CONFIG
        config = deepcopy(DEFAULT_CONFIG)
        config["sources"] = {"pubmed": True, "europe_pmc": True}
        calls = []
        def search(source, query, limit, *_args, **_kwargs):
            calls.append((source, limit))
            return [PaperRecord(title=f"{source} {query}", doi=f"10.1234/{source}{limit}{len(query)}", sources=[source])]
        with patch("lit_harvest.pipeline._search_one", side_effect=search), patch("lit_harvest.pipeline.enrich_openalex_content", return_value=(0, 0, [])):
            rows, status = search_all("chloride", config, 1, env={}, query_plan=build_coverage_plan(plan(), 101, mode="fixed-budget")["query_plan"])
        self.assertEqual(sorted(calls), [("europe_pmc", 50), ("europe_pmc", 51), ("pubmed", 50), ("pubmed", 51)])
        self.assertEqual(status["selection"]["raw_records"], 4)
        self.assertEqual(len(rows.candidates), 4)
        self.assertEqual(len(rows), 1)

    def test_relation_shapes_and_evidence_hash(self):
        item = {"DOI": "10.1234/pre", "relation": {"is-preprint-of": [{"id": "10.1234/final", "id-type": "doi", "asserted-by": "subject"}], "is-comment-on": [{"id": "10.1234/comment", "id-type": "doi"}]}}
        record = PaperRecord(title="Preprint", doi=item["DOI"])
        _retain_version_metadata(record, item, "crossref")
        edges = record.extra["version_relations"]
        self.assertEqual([e["relation"] for e in edges], ["is-preprint-of", "is-comment-on"])
        self.assertEqual(edges[0]["source_record_id"], "doi:10.1234/pre")
        expected = hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(edges[0]["source_metadata_sha256"], expected)
        epmc = {"id": "PPR2", "source": "PPR", "versionList": {"version": [{"id": "PPR1", "source": "PPR", "versionNumber": 1}, {"id": "PPR2", "source": "PPR", "versionNumber": 2}]}, "commentCorrectionList": {"commentCorrection": [{"type": "Comment in", "id": "333"}]}}
        record = PaperRecord(title="Preprint")
        _retain_version_metadata(record, epmc, "europe_pmc")
        self.assertEqual(len(record.extra["version_relations"]), 1)
        edge = record.extra["version_relations"][0]
        self.assertEqual((edge["target_id"], edge["target_id_type"]), ("PPR:PPR1", "europe_pmc"))
        self.assertIn("provider_comment_corrections", record.extra)

    def test_protected_frozen_pubmed_book_recovered(self):
        configured_path = os.environ.get("LITERATURE_HARVESTER_TEST_PUBMED_XML")
        if not configured_path:
            self.skipTest("Set LITERATURE_HARVESTER_TEST_PUBMED_XML for optional protected replay")
        path = Path(configured_path)
        self.assertTrue(path.is_file(), "Configured protected replay response is not a file")
        raw = path.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), "bc06e29476f8e8b307ac9d94052255a5b4b331c93a00e2c8e0659f7436e26fd3")
        status = {}
        rows = _parse_pubmed_xml(raw, 100, status=status)
        self.assertEqual(len(rows), 100)
        self.assertEqual(status["raw_rows"], 100)
        book = next(r for r in rows if r.pmid == "32282161")
        self.assertEqual(book.title, "Acetate: Neonatal parenteral nutrition: Evidence review D6")
        self.assertEqual(book.authors, ["National Guideline Alliance (UK)"])
        self.assertEqual((book.item_type, book.year), ("book", 2020))
        self.assertIn("Hyperchloraemia and metabolic acidosis", book.abstract)
        self.assertIsNone(book.is_oa)
        self.assertEqual(book.pdf_candidates, [])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), hashlib.sha256(raw).hexdigest())
        esearch = path.parent / "630778a221d85a3531e5d57773f0af6cf66f69a5af2a07d1ad59fbc7ae833a7e.bin"
        if esearch.exists():
            ids = json.loads(esearch.read_bytes())["esearchresult"]["idlist"]
            with patch("lit_harvest.sources.time.sleep"):
                replay = search_pubmed(Client(raw, ids), "hyperchloremia", 100)
            self.assertEqual(len(replay), 100)
            self.assertEqual(replay.source_status["missing_requested_ids"], [])
            self.assertEqual(replay.source_status["skipped_rows"], 0)
            self.assertEqual(replay.source_status["status"], "ok")

    def test_adapters_retain_explicit_relations_without_extra_requests(self):
        cross = {"DOI": "10.1234/pre", "title": ["Preprint"],
                 "relation": {"is-preprint-of": [{"id": "10.1234/final", "id-type": "doi"}]}}
        class CrossClient:
            def get_json(self, *_args, **_kwargs): return {"message": cross}
        rows = search_crossref(CrossClient(), "10.1234/pre", 1)
        self.assertEqual(rows[0].extra["version_relations"][0]["relation"], "is-preprint-of")
        epmc = {"id": "PPR2", "source": "PPR", "title": "Preprint",
                "versionList": {"version": [{"id": "PPR1", "source": "PPR", "versionNumber": 1}]}}
        class EpmcClient:
            def get_json(self, *_args, **_kwargs): return {"resultList": {"result": [epmc]}}
        rows = search_europe_pmc(EpmcClient(), "chloride", 1)
        self.assertEqual(rows[0].extra["version_relations"][0]["target_id"], "PPR:PPR1")


if __name__ == "__main__":
    unittest.main()
