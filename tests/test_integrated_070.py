"""Fixed-input integration contracts; no live services or external model calls."""
import copy
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lit_harvest import article_document, content_store, evaluation, http, pipeline, reading, sources
from lit_harvest.html_document import html_document
from lit_harvest.models import PaperRecord
from lit_harvest.query_lint import inspect_query
from lit_harvest.query_plan import candidate_id
from test_reading_upgrade import record, xml


class Client:
    def __init__(self, data):
        self.data = data
    def get_json(self, *args, **kwargs):
        return self.data


def html(rec, body=None, extra_meta=""):
    body = body if body is not None else '''<h2>Methods</h2><p>Design.</p><h3>Population</h3>
      <p>Chloride measured at admission. See <a href="#t1">Table 1</a>.</p>
      <table id="t1"><caption>Chloride levels</caption><thead><tr><th>Group</th><th>Value</th></tr></thead>
      <tbody><tr><td rowspan="2">A</td><td>117.2</td></tr><tr><td>118</td></tr></tbody>
      <tfoot><tr><td colspan="2">Unit mmol/L</td></tr></tfoot></table>
      <figure id="f1"><img src="fig1.png"><figcaption>Study flow</figcaption></figure>
      <p>Formula <math><mi>x</mi><mo>=</mo><mn>2</mn></math>.</p><h2>Results</h2><p>Result.</p>'''
    return (f'<html><head><meta name="citation_title" content="{rec.title}">'
            f'<meta name="citation_doi" content="{rec.doi}">{extra_meta}</head><body>'
            '<nav>Not source text</nav><div class="article-body">' + body + '</div></body></html>')


class IntegratedUpgrade(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = pipeline.load_config()
        self.enterContext(patch("socket.getaddrinfo", side_effect=AssertionError("Offline fixture")))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("Offline fixture")))
        self.enterContext(patch.dict(http._HOST_COOLDOWNS, {}, clear=True))

    def save(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def manifest(self, rows):
        return self.save("manifest.json", pipeline.new_manifest("synthetic", rows, {}, {}))

    def test_distinct_provider_ids_do_not_overwrite_screening(self):
        a = PaperRecord("Editorial", year=2025, openalex_id="W101")
        b = PaperRecord("Editorial", year=2025, openalex_id="https://openalex.org/W202")
        rows = pipeline.merge_records([a, b])
        self.assertEqual(len(rows), 2)
        ids = [candidate_id(r) for r in rows]
        self.assertEqual(len(set(ids)), 2)
        decisions = self.save("decisions.json", [{"candidate_id": ids[0], "decision": "include", "reason": "fixture",
            "basis": "synthetic metadata", "actor": "test"}])
        _, ledger = reading.screen_candidates(self.manifest(rows), decisions, self.root / "screen", self.config)
        self.assertEqual(ledger["unreviewed_ids"], [ids[1]])

    def test_frozen_colliding_ids_fail_before_writing(self):
        rows = [PaperRecord("Editorial", openalex_id=i, extra={"candidate_id": "old-collision"}) for i in ("W1", "W2")]
        decisions = self.save("decisions.json", [])
        with self.assertRaisesRegex(ValueError, "Ambiguous candidate"):
            reading.screen_candidates(self.manifest(rows), decisions, self.root / "screen", self.config)
        self.assertFalse((self.root / "screen").exists())

    def test_title_only_conflicts_are_retained(self):
        for kwargs in ({"year": 2020}, {"authors": ["Other Person"]}, {"openalex_id": "W2"}):
            with self.subTest(kwargs=kwargs):
                a = PaperRecord("Synthetic long title for unrelated studies", year=2025, authors=["Alice"], openalex_id="W1")
                b = PaperRecord(a.title, **kwargs)
                rows = pipeline.merge_records([a, b])
                self.assertEqual(len(rows), 2)
                self.assertTrue(all(r.extra["identity_conflict"] for r in rows))

    def test_shared_primary_id_preserves_provider_aliases(self):
        a, b = record(openalex_id="W1"), record(openalex_id="W2")
        rows = pipeline.merge_records([a, b])
        self.assertEqual(len(rows), 1)
        self.assertTrue({"openalex:w1", "openalex:w2"} <= set(rows[0].identity_keys))

    def test_provider_alias_case_and_url_normalize(self):
        a = PaperRecord("First title", openalex_id="https://openalex.org/W12")
        b = PaperRecord("Renamed title", openalex_id="w12")
        self.assertEqual(len(pipeline.merge_records([a, b])), 1)
        self.assertEqual(PaperRecord.from_dict(a.to_dict()).identity_keys, a.identity_keys)

    def test_identifier_free_candidate_urls_distinguish(self):
        a = PaperRecord("Editorial", url="https://example.org/a")
        b = PaperRecord("Editorial", url="https://example.org/b")
        self.assertNotEqual(candidate_id(a), candidate_id(b))

    def test_evaluation_provider_alias_and_stored_candidate(self):
        rec = PaperRecord("Provider-only", openalex_id="https://openalex.org/W501", extra={"candidate_id": "frozen-id"})
        manifest = self.manifest([rec])
        for alias in ("openalex:W501", "frozen-id"):
            benchmark = self.save("benchmark.json", {"schema_version": 1, "benchmark_id": "synthetic", "frozen_at": "2026-10-22",
                "judgments": [{"ids": [alias], "relevant": True, "basis": "synthetic fixture"}]})
            out = evaluation.evaluate(benchmark, manifest, k=1)
            self.assertEqual(out["unknown_at_k"], 0)
            self.assertEqual(out["recall_of_judged_relevant_at_k"], 1)

    def test_bad_rows_do_not_discard_valid_rows(self):
        fixtures = [
            (sources.search_openalex, {"results": [{"title": "Good", "id": "W1"}, {"title": "Bad", "authorships": [None]}, None], "meta": {}}, "Good"),
            (sources.search_crossref, {"message": {"items": [{"title": ["Good"]}, {"title": ["Bad"], "author": [None]}, None]}}, "Good"),
            (sources.search_europe_pmc, {"resultList": {"result": [{"title": "Good"}, {"title": "Bad", "authorList": {"author": [None]}}, None]}}, "Good"),
            (sources.search_semantic_scholar, {"data": [{"title": "Good"}, {"title": "Bad", "authors": [None]}, None]}, "Good"),
        ]
        for function, payload, title in fixtures:
            with self.subTest(function=function.__name__):
                found = function(Client(payload), "chloride", 10)
                self.assertEqual([r.title for r in found], [title])
                self.assertEqual(found.source_status["status"], "partial")
                self.assertEqual(found.source_status["raw_rows"], 3)
                self.assertEqual(found.source_status["skipped_rows"], 2)

    def test_missing_title_and_all_bad_rows_are_not_empty_success(self):
        found = sources.search_openalex(Client({"results": [{}, None], "meta": {}}), "x", 2)
        self.assertEqual(found.source_status["status"], "error")
        self.assertEqual(found.source_status["skipped_rows"], 2)

    def test_bad_numeric_row_is_isolated(self):
        found = sources.search_openalex(Client({"results": [{"title": "Good"}, {"title": "Bad", "cited_by_count": "invalid"}], "meta": {}}), "x", 2)
        self.assertEqual([r.title for r in found], ["Good"])

    def test_pubmed_errorlist_is_failure_not_exhaustion(self):
        with patch.object(sources._PROVIDER_PACERS["pubmed"], "slot", return_value=nullcontext()):
            found = sources.search_pubmed(Client({"esearchresult": {"idlist": [], "count": "0", "errorlist": {"fieldnotfound": ["BAD"]}}}), "chloride[BAD]", 10)
        self.assertEqual(found.source_status["status"], "error")
        self.assertFalse(found.source_status["exhausted"])
        self.assertIn("errorlist", found.source_status["query_trace"][0])

    def test_s2_effective_mode_is_disclosed(self):
        for limit, mode in ((2, "relevance"), (1001, "bulk")):
            found = sources.search_semantic_scholar(Client({"data": []}), "chloride", limit)
            self.assertEqual(found.source_status["effective_mode"], mode)
            self.assertEqual(found.source_status["submitted_query"], "chloride")

    def test_query_warnings_do_not_rewrite_or_reject(self):
        query = "chloride[tiab] AND mortality[tiab]"
        result = inspect_query(query, "crossref", "text")
        self.assertEqual(result["query"], query)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["warnings"][0]["code"], "native_syntax_in_text_route")
        self.assertEqual(inspect_query("x AND y OR z", "pubmed")["warnings"][0]["code"], "mixed_boolean_precedence_requires_review")

    def test_content_free_enrichment_feeds_html(self):
        rec = record()
        original = rec.to_dict()
        text = html(rec)
        def enrich(rows, config, env, **kwargs):
            rows[0].add_pdf_candidate("https://example.org/oa", "unpaywall", "CC-BY", kind="landing")
            return {"unpaywall": {"status": "ok", "count": 1}}
        with patch.object(reading, "enrich_records", side_effect=enrich) as call, patch.object(reading, "_fetch_bounded", return_value=(text.encode(), "https://example.org/oa", text)):
            result = reading.acquire_document(rec, self.root / "article", self.config, {})
        self.assertEqual(call.call_count, 1)
        self.assertEqual(result["status"], "extracted")
        self.assertEqual(rec.to_dict(), original)
        self.assertEqual(result["ai_reading_status"], "not_submitted")

    def test_local_only_never_enriches(self):
        with patch.object(reading, "enrich_records", side_effect=AssertionError("must not enrich")):
            result = reading.acquire_document(record(), self.root / "article", self.config, {}, local_only=True)
        self.assertEqual(result["status"], "content_unavailable")

    def test_existing_html_success_does_not_enrich(self):
        rec = record()
        rec.add_pdf_candidate("https://example.org/oa", "unpaywall", "CC-BY", kind="landing")
        value = html(rec)
        with patch.object(reading, "enrich_records", side_effect=AssertionError("unnecessary enrichment")), patch.object(reading, "_fetch_bounded", return_value=(value.encode(), "https://example.org/oa", value)):
            out = reading.acquire_document(rec, self.root / "article", self.config, {})
        self.assertEqual(out["status"], "extracted")

    def test_resume_restores_latest_deadline(self):
        rec = record()
        manifest = self.manifest([rec])
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        previous = self.save("index.json", {"source_manifest_sha256": reading.sha256_file(manifest), "records": [], "host_retry_at": {"example.org": future}})
        with patch.object(reading, "acquire_document", return_value={"candidate_id": candidate_id(rec), "status": "content_unavailable"}):
            _, out = reading.prepare_content(manifest, self.root / "retry", self.config, local_only=True, resume_index=previous, env={})
        self.assertGreaterEqual(datetime.fromisoformat(out["host_retry_at"]["example.org"]), datetime.fromisoformat(future).replace(microsecond=0))

    def test_nested_jats_groups_keep_structured_objects(self):
        raw = xml().replace(b'<table-wrap id="t1">', b'<table-wrap-group><table-wrap id="t1">').replace(b'</table-wrap>', b'</table-wrap></table-wrap-group>')
        raw = raw.replace(b'<fig id="fig1">', b'<fig-group><fig id="fig1">').replace(b'</fig>', b'</fig></fig-group>')
        raw = raw.replace(b'</sec>', b'<disp-formula-group><disp-formula id="f2"><m:math><m:mi>y</m:mi></m:math></disp-formula></disp-formula-group></sec>')
        doc = article_document.jats_document(raw, record(pmcid="PMC123"))
        self.assertEqual(len([e for e in doc["elements"] if e["kind"] == "table"]), 1)
        self.assertEqual(len([e for e in doc["elements"] if e["kind"] == "figure"]), 1)
        self.assertEqual(len([e for e in doc["elements"] if e["kind"] == "formula"]), 2)
        self.assertEqual(len({e["xml_path"] for e in doc["elements"]}), len(doc["elements"]))

    def test_html_preserves_sections_cells_formula_and_links(self):
        rec = record()
        value = html(rec)
        doc = html_document(value.encode(), value, rec)
        table = next(e for e in doc["elements"] if e["kind"] == "table")
        self.assertEqual(table["section"], ["Methods", "Population"])
        self.assertEqual(table["rows"][1][0]["rowspan"], "2")
        self.assertEqual(table["rows"][1][1]["text"], "117.2")
        self.assertEqual(table["footnotes"], ["Unit mmol/L"])
        self.assertEqual(table["caption"], "Chloride levels")
        self.assertTrue(table["dom_path"].endswith("/table[1]"))
        self.assertEqual(next(e for e in doc["elements"] if e["kind"] == "figure")["resource_references"], ["fig1.png"])
        self.assertIn("<math>", next(e for e in doc["elements"] if e["kind"] == "formula")["source_xml"])
        self.assertNotIn("Not source text", article_document.render_document(doc))
        self.assertEqual(doc["elements"][-1]["section"], ["Results"])

    def test_abstract_or_conflicting_pmid_does_not_pass(self):
        rec = record(pmid="12345")
        for body, meta in (("<section class='abstract'><p>Abstract only.</p></section>", ""), ("<p>Body.</p>", '<meta name="citation_pmid" content="67890">')):
            value = html(rec, body, meta)
            with self.assertRaises(Exception):
                html_document(value.encode(), value, rec)

    def document(self):
        rec = record()
        value = html(rec)
        source = self.root / "source.html"
        source.write_bytes(value.encode())
        doc = html_document(value.encode(), value, rec)
        doc["source"] = {"artifact": str(source)}
        return self.save("document.json", doc), doc

    def test_find_and_reading_pack_share_bound_locator(self):
        path, doc = self.document()
        found = content_store.find_content(path, "117.2")
        hit = found["hits"][0]
        self.assertEqual(hit["locator"]["source_sha256"], doc["source_sha256"])
        for start, end in hit["match_spans"]:
            self.assertIn(hit["text"][start:end], {"117", "2"})
        _, pack = reading.prepare_reading(path, self.root / "pack", self.config, question="What chloride values?")
        self.assertEqual(pack["schema_version"], 3)
        self.assertTrue(all(c["locator"]["char_start"] == c["start"] for c in pack["chunks"]))
        self.assertFalse(found["full_paper_read"])

    def test_legacy_v2_pack_is_still_verifiable(self):
        path, doc = self.document()
        directory, pack = reading.prepare_reading(path, self.root / "pack", self.config, question="What values?")
        pack["schema_version"] = 2
        pack["chunks"] = reading._chunks(doc, pack["chunk_chars"], version=2)[:len(pack["chunks"])]
        pack_path = directory / "reading-pack.json"
        pack_path.write_text(json.dumps(pack), encoding="utf-8")
        first = pack["chunks"][0]
        receipt = self.save("receipt.json", {"pack_sha256": reading.sha256_file(pack_path), "actor": "synthetic-test",
            "observations": [{"chunk_id": first["id"], "quote": first["text"], "note": "fixture only"}]})
        _, out = reading.verify_receipt(pack_path, receipt, self.root / "verified", self.config)
        self.assertTrue(out["quote_integrity_verified"])
        self.assertFalse(out["whole_article_read_certified"])

    def test_locator_changes_with_parser_options_and_text(self):
        _, doc = self.document()
        element = doc["elements"][0]
        before = article_document.element_locator(doc, element)
        changed = copy.deepcopy(doc)
        changed["extraction_options"] = {"different": True}
        self.assertNotEqual(before["extraction_sha256"], article_document.element_locator(changed, element)["extraction_sha256"])
        with self.assertRaises(ValueError):
            article_document.element_locator(doc, element, 0, len(element["text"]) + 10000)

    def test_explicit_local_html_cli_is_offline_and_preserves_inputs(self):
        from literature_harvester import main
        rec = record()
        source = self.root / "已保存.html"
        source.write_bytes(html(rec).encode())
        manifest = self.manifest([rec])
        before = [reading.sha256_file(p) for p in (source, manifest)]
        with patch.object(reading, "enrich_records", side_effect=AssertionError("local file must not enrich")):
            code = main(["content", "--manifest", str(manifest), "--local-html", str(source), "--output-dir", str(self.root / "content")])
        self.assertEqual(code, 0)
        self.assertEqual(before, [reading.sha256_file(p) for p in (source, manifest)])
        index = reading.read_json(self.root / "content/content-index.json")
        self.assertTrue(index["local_only"])
        self.assertEqual(index["metrics"]["extracted_records"], 1)

    def test_local_html_wrong_identity_is_not_replaced_by_other_source(self):
        source = self.root / "wrong.html"
        source.write_text(html(record("other")), encoding="utf-8")
        result = reading.acquire_document(record(), self.root / "content", self.config, {}, local_html=source)
        self.assertEqual(result["status"], "content_unavailable")
        self.assertEqual([a["stage"] for a in result["attempts"]], ["local_html"])

    def test_local_html_multi_record_ambiguity_fails_before_output(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            reading.prepare_content(self.manifest([record(), record("b")]), self.root / "content", self.config, local_html=self.root / "anything.html")
        self.assertFalse((self.root / "content").exists())

    def test_declared_local_html_charset(self):
        rec = record()
        value = html(rec, "<p>氯离子测量方法</p>", '<meta charset="gbk">')
        source = self.root / "gbk.html"
        source.write_bytes(value.encode("gbk"))
        out = reading.acquire_document(rec, self.root / "content", self.config, {}, local_html=source)
        self.assertEqual(out["status"], "extracted")
        doc = reading.read_json(out["document"])
        self.assertEqual(doc["source"]["encoding"], "gbk")
        self.assertIn("氯离子", doc["elements"][0]["text"])


if __name__ == "__main__":
    unittest.main()
