from __future__ import annotations
from copy import deepcopy
from io import BytesIO
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lit_harvest import pipeline, sources, reading
from lit_harvest.article_document import jats_document, pdf_document
from lit_harvest.fulltext import FullTextError, jats_to_markdown
from lit_harvest.models import PaperRecord
from lit_harvest.outcomes import summarize_outcomes
from lit_harvest.query_plan import validate_plan, concept_evidence, candidate_id


def plan():
    return {"schema_version": 1, "question": "Chloride outcomes",
            "concepts": [{"id": "chloride", "terms": ["hyperchloremia", "elevated serum chloride"]}],
            "routes": [{"id": "p", "source": "pubmed", "concept_ids": ["chloride"], "limit": 10},
                       {"id": "e", "source": "europe_pmc", "concept_ids": ["chloride"], "limit": 10}]}


def record(suffix="a", **kwargs):
    return PaperRecord(title="Synthetic chloride outcomes detailed clinical study " + suffix,
                       doi="10.1234/" + suffix, authors=["Alice Smith"], **kwargs)


def xml(pmcid="PMC123", doi="10.1234/a"):
    return (f'<article xmlns:m="http://www.w3.org/1998/Math/MathML"><front><article-meta>'
            f'<article-id pub-id-type="pmcid">{pmcid}</article-id><article-id pub-id-type="doi">{doi}</article-id>'
            '<title-group><article-title>Chloride synthetic paper</article-title></title-group></article-meta></front>'
            '<body><sec id="s1"><title>Results</title><p>' + "Actual synthetic body text. " * 30 +
            '<xref ref-type="bibr" rid="ref1">[1]</xref><inline-formula id="f1"><m:math><m:mi>x</m:mi>'
            '<m:mo>=</m:mo><m:mn>2</m:mn></m:math></inline-formula></p>'
            '<table-wrap id="t1"><caption><p>Chloride values</p></caption><table><tr><th>Group</th><th>mmol/L</th></tr>'
            '<tr><td rowspan="2">A</td><td>117.2</td></tr></table><table-wrap-foot><fn id="fn1"><p>Measured at admission.</p></fn></table-wrap-foot></table-wrap>'
            '<fig id="fig1"><caption><p>Adjusted survival curves.</p></caption><graphic href="fig1.png"/></fig>'
            '<supplementary-material href="supp.pdf"><p>Supplement</p></supplementary-material>'
            '</sec></body><back><ref-list><ref id="ref1">Verified synthetic reference</ref></ref-list></back></article>').encode()


def make_pdf(path, rec, blank_second=False):
    output = BytesIO()
    pdf = canvas.Canvas(output, invariant=1)
    pdf.drawString(36, 780, rec.title)
    pdf.drawString(36, 760, "DOI: " + rec.doi)
    pdf.drawString(36, 740, "First page methods.")
    pdf.showPage()
    if not blank_second:
        pdf.drawString(36, 780, "Second page result was 117.2 mmol/L.")
    pdf.showPage()
    pdf.save()
    path.write_bytes(output.getvalue())
    rec.local_pdf = str(path.resolve())
    rec.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    rec.identity_status = "verified"
    rec.download_status = "downloaded"
    return rec


class Response(BytesIO):
    def __init__(self, raw, url="https://example.org"):
        super().__init__(raw)
        self.headers = {}
        self.url = url


class Offline(unittest.TestCase):
    def setUp(self):
        dns = patch("socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
        dns.start()
        self.addCleanup(dns.stop)
        for name in ("socket.create_connection", "socket.socket.connect"):
            stub = patch(name, side_effect=AssertionError("Network forbidden in offline tests"))
            stub.start()
            self.addCleanup(stub.stop)
        self.config = pipeline.load_config()


class SearchPlanTests(Offline):
    def test_compilation_uses_provider_dialects(self):
        value = plan()
        value["routes"].append({"id": "a", "source": "arxiv", "concept_ids": ["chloride"], "limit": 1})
        compiled = validate_plan(value)
        self.assertIn('"hyperchloremia"[Title/Abstract]', compiled["routes"][0]["query"])
        self.assertIn('TITLE_ABS:"hyperchloremia"', compiled["routes"][1]["query"])
        self.assertIn('all:"hyperchloremia"', compiled["routes"][2]["query"])
        self.assertEqual(validate_plan(compiled), compiled)

    def test_bad_plan_is_rejected_before_search(self):
        for mutation in (lambda p: p["routes"][0].update(source="unknown"),
                         lambda p: p["routes"][0].update(limit=True),
                         lambda p: p["routes"][1].update(id="p"),
                         lambda p: p["routes"][0].update(id="selection"),
                         lambda p: p["routes"][0].update(headers={"Authorization": "no"})):
            value = plan()
            mutation(value)
            with patch("lit_harvest.pipeline._search_one") as search, self.assertRaises(ValueError):
                pipeline.search_all("topic", self.config, 2, env={}, query_plan=value)
            search.assert_not_called()

    def test_no_silent_boolean_compiler_for_crossref(self):
        value = plan()
        value["routes"][0]["source"] = "crossref"
        with self.assertRaises(ValueError):
            validate_plan(value)

    def test_budget_is_bounded(self):
        value = plan()
        value["routes"] = [{"id": str(i), "source": "pubmed", "query": "test", "limit": 1000} for i in range(11)]
        with self.assertRaises(ValueError):
            validate_plan(value)

    def test_all_candidates_hits_and_rrf_survive_cap(self):
        def search(source, *args, **kwargs):
            a, b = record("a", citation_count=0), record("b", citation_count=999999)
            a.sources = b.sources = [source]
            return [a, b] if source == "pubmed" else [a, record("c")]
        with patch("lit_harvest.pipeline._search_one", side_effect=search):
            chosen, states = pipeline.search_all("topic", self.config, 1, env={}, query_plan=plan())
        self.assertEqual(len(chosen), 1)
        self.assertEqual(len(chosen.candidates), 3)
        self.assertEqual(chosen[0].doi, "10.1234/a")
        self.assertEqual(len(chosen[0].extra["discovery_hits"]), 2)
        self.assertEqual(chosen[0].extra["screening"]["decision"], "uncertain")
        self.assertEqual(states["selection"]["outside_selected_cap"], 2)

    def test_disabled_and_failure_not_reported_zero_success(self):
        self.config["sources"]["pubmed"] = False
        with patch("lit_harvest.pipeline._search_one", side_effect=RuntimeError("synthetic outage")):
            chosen, states = pipeline.search_all("topic", self.config, 1, env={}, query_plan=plan())
        self.assertEqual(states["p"]["status"], "disabled")
        self.assertEqual(states["e"]["status"], "error")
        self.assertFalse(chosen)

    def test_concept_matching_and_no_automatic_exclusion(self):
        rec = record()
        rec.title = "Elevated serum chloride in a synthetic cohort"
        evidence = concept_evidence(rec, plan()["concepts"])
        self.assertEqual(evidence[0]["term"], "elevated serum chloride")
        rec.title = "Not hyperchloremiax"
        self.assertEqual(concept_evidence(rec, plan()["concepts"]), [])

    def test_conflicting_ids_have_distinct_queue_ids(self):
        a, b = record(pmid="1"), record(pmid="2")
        values = pipeline.merge_records([a, b], ranking="routes")
        self.assertEqual(len(values), 2)
        self.assertNotEqual(candidate_id(values[0]), candidate_id(values[1]))

    def test_arxiv_native_and_legacy_are_distinct(self):
        urls = []
        class Client:
            def request(self, url, **kwargs):
                urls.append(url)
                return Response(b'<feed xmlns="http://www.w3.org/2005/Atom"/>', url)
        with patch.object(sources._PROVIDER_PACERS["arxiv"], "slot", return_value=__import__('contextlib').nullcontext()):
            sources.search_arxiv(Client(), "all:chloride", 1, native_query=True)
            sources.search_arxiv(Client(), "chloride", 1)
        self.assertEqual(parse_qs(urlparse(urls[0]).query)["search_query"], ["all:chloride"])
        self.assertEqual(parse_qs(urlparse(urls[1]).query)["search_query"], ['all:"chloride"'])

    def test_pubmed_translation_and_warning_preserved(self):
        class Client:
            def get_json(self, *args, **kwargs):
                return {"esearchresult": {"idlist": [], "count": "0", "querytranslation": "translated term", "warninglist": {"phrasesignored": ["test"]}}}
        found = sources.search_pubmed(Client(), "test", 1)
        trace = found.source_status["query_trace"][0]
        self.assertEqual(trace["querytranslation"], "translated term")
        self.assertIn("warninglist", trace)

    def test_plan_inventory_resume_and_integrity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("lit_harvest.pipeline._search_one", return_value=[record("a"), record("b")]):
                folder, manifest, selected = pipeline.run_pipeline("topic", self.config, 1, str(root/"run"), False, env={}, query_plan=plan())
            original = (folder/"manifest.json").read_bytes()
            with patch("lit_harvest.pipeline.search_all", side_effect=AssertionError("resume searched")), patch("lit_harvest.pipeline.retrieve_population"):
                _, resumed, _ = pipeline.resume_pipeline(folder/"manifest.json", self.config, str(root/"resume"), env={})
            self.assertEqual(resumed["research_context"], manifest["research_context"])
            self.assertEqual(original, (folder/"manifest.json").read_bytes())
            (folder/"candidates.json").write_text("{}")
            with self.assertRaises(ValueError):
                pipeline.load_manifest(folder/"manifest.json")


class ArticleTests(Offline):
    def test_jats_structure_and_namespaces(self):
        doc = jats_document(xml(), record(pmcid="PMC123"))
        by_kind = {e["kind"]: e for e in doc["elements"]}
        self.assertEqual(by_kind["table"]["rows"][1][1]["text"], "117.2")
        self.assertEqual(by_kind["table"]["rows"][1][0]["rowspan"], "2")
        self.assertIn("mmol/L", by_kind["table"]["text"])
        self.assertIn("Measured at admission.", by_kind["table"]["footnotes"])
        self.assertIn("Adjusted survival", by_kind["figure"]["text"])
        self.assertIn("http://www.w3.org/1998/Math/MathML", by_kind["formula"]["source_xml"])
        self.assertIn("/table-wrap[1]", by_kind["table"]["xml_path"])
        self.assertEqual(by_kind["reference"]["source_id"], "ref1")
        self.assertTrue(any(e.get("xrefs") for e in doc["elements"]))
        self.assertEqual(doc["ai_reading_status"], "not_submitted")
        self.assertTrue(doc["gaps"])

    def test_jats_identity_and_entity_boundaries(self):
        for raw in (xml("PMC999"), xml(doi="10.1234/wrong"), b'<!DOCTYPE article [<!ENTITY x "hello">]><article/>'):
            with self.assertRaises(FullTextError):
                jats_document(raw, record(pmcid="PMC123"))

    def test_legacy_reader_contract_is_unchanged(self):
        value = jats_to_markdown(xml(), "PMC123")
        self.assertNotIn("117.2", value)
        self.assertNotIn("Adjusted survival", value)

    def test_pdf_reads_second_page_without_mutating_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            rec = make_pdf(Path(temp)/"paper.pdf", record())
            before = rec.to_dict()
            doc = pdf_document(Path(rec.local_pdf), rec)
            self.assertTrue(any("117.2" in e["text"] and e["page"] == 2 for e in doc["elements"]))
            self.assertEqual(doc["coverage"]["page_count"], 2)
            self.assertEqual(rec.to_dict(), before)
            self.assertEqual(doc["coverage"]["extraction_status"], "partial")

    def test_blank_pdf_page_is_visible(self):
        with tempfile.TemporaryDirectory() as temp:
            rec = make_pdf(Path(temp)/"paper.pdf", record(), blank_second=True)
            doc = pdf_document(Path(rec.local_pdf), rec)
            self.assertTrue(any(g.get("page") == 2 for g in doc["gaps"]))

    def test_pdf_page_budget_and_hash_conflict(self):
        with tempfile.TemporaryDirectory() as temp:
            rec = make_pdf(Path(temp)/"paper.pdf", record())
            doc = pdf_document(Path(rec.local_pdf), rec, max_pages=1)
            self.assertTrue(any(g["reason"] == "page_limit" for g in doc["gaps"]))
            rec.sha256 = "0"*64
            with self.assertRaises(FullTextError):
                pdf_document(Path(rec.local_pdf), rec)


class ReadingWorkflowTests(Offline):
    def _manifest(self, root, records):
        path = root/"manifest.json"
        pipeline.save_manifest(path, pipeline.new_manifest("test", records, {}, {"target_pdfs": None}))
        return path

    def test_pdf_content_independent_and_original_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rec = make_pdf(root/"input.pdf", record())
            path = self._manifest(root, [rec, record("b")])
            original, before = path.read_bytes(), summarize_outcomes([rec])
            folder, index = reading.prepare_content(path, root/"content", self.config, local_only=True, env={})
            self.assertEqual(index["metrics"]["extracted_records"], 1)
            self.assertEqual(index["metrics"]["ai_read_records"], 0)
            self.assertEqual(index["records"][1]["status"], "content_unavailable")
            self.assertEqual(original, path.read_bytes())
            self.assertEqual(before, summarize_outcomes([rec]))

    def test_content_budget_preserves_pending_population(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self._manifest(root, [record("a"), record("b")])
            _, index = reading.prepare_content(path, root/"content", self.config, local_only=True, max_records=1, env={})
            self.assertEqual(len(index["records"]), 2)
            self.assertEqual(index["records"][1]["status"], "pending_budget")

    def test_content_cli_preserves_partial_result_exit_code(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rec = make_pdf(root/"input.pdf", record())
            path = self._manifest(root, [rec, record("b")])
            cli = Path(__file__).resolve().parents[1]/"scripts"/"literature_harvester.py"
            result = subprocess.run([sys.executable, "-X", "utf8", "-B", str(cli), "content",
                "--manifest", str(path), "--output-dir", str(root/"content"), "--local-only"],
                capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(json.loads(result.stdout)["extracted_records"], 1)
            self.assertTrue((root/"content"/"content-index.json").is_file())

    def test_jats_content_uses_current_resolver_and_preserves_pdf_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rec = make_pdf(root/"input.pdf", record(pmcid="PMC123"))
            before = rec.to_dict()
            candidate = {"url": "https://pmc-oa-opendata.s3.amazonaws.com/PMC123.1/PMC123.1.xml", "source": "pmc_oa_cloud", "pmcid": "PMC123", "article_version": 1,
                         "kind": "jats", "license": "TDM", "oa_route": "author_manuscript_tdm"}
            class Client:
                def request(self, url, **kwargs):
                    return Response(xml(), url)
            with patch("lit_harvest.reading.resolve_pmc_candidates", return_value=[candidate]), patch("lit_harvest.reading.HttpClient", return_value=Client()):
                result = reading.acquire_document(rec, root/"content", self.config, {})
            self.assertEqual(result["status"], "extracted")
            self.assertEqual(reading.read_json(result["document"])["format"], "jats", result["attempts"])
            self.assertEqual(before, rec.to_dict())

    def test_bare_or_mismatched_jats_route_never_fetched(self):
        with tempfile.TemporaryDirectory() as temp:
            rec = record(pmcid="PMC123")
            fake = {"kind": "jats", "url": "https://example.org/a.xml", "pmcid": "PMC999"}
            with patch("lit_harvest.reading.resolve_pmc_candidates", return_value=[fake]), patch("lit_harvest.reading._fetch_bounded") as fetch:
                result = reading.acquire_document(rec, Path(temp)/"content", self.config, {})
            fetch.assert_not_called()
            self.assertEqual(result["status"], "content_unavailable")

    def test_screen_includes_candidate_outside_initial_queue(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("lit_harvest.pipeline._search_one", return_value=[record("a"), record("b")]):
                folder, _, selected = pipeline.run_pipeline("test", self.config, 1, root/"run", False, env={}, query_plan=plan())
            other = next(r for r in selected.candidates if r.doi != selected[0].doi)
            decisions = [{"candidate_id": other.extra["candidate_id"], "decision": "include", "reason": "Relevant population", "basis": "title and abstract", "actor": "test-reviewer"}]
            reading.write_json(root/"decisions.json", decisions)
            original = (folder/"manifest.json").read_bytes()
            new, _ = reading.screen_candidates(folder/"manifest.json", root/"decisions.json", root/"screen", self.config)
            _, records = pipeline.load_manifest(new/"manifest.json")
            self.assertEqual([r.doi for r in records], [other.doi])
            self.assertEqual(original, (folder/"manifest.json").read_bytes())

    def _document(self, root):
        rec = record(pmcid="PMC123")
        raw = xml()
        (root/"source.xml").write_bytes(raw)
        doc = jats_document(raw, rec)
        doc["source"] = {"artifact": str(root/"source.xml")}
        reading.write_json(root/"document.json", doc)
        return root/"document.json"

    def test_pack_preparation_is_not_reading(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            document = self._document(root)
            directory, pack = reading.prepare_reading(document, root/"pack", self.config, question="What were the outcomes?")
            self.assertEqual(pack["state"], "prepared_not_read")
            self.assertIn("117.2", (directory/"reading-pack.md").read_text(encoding="utf-8"))
            self.assertTrue(pack["gaps"])

    def test_receipt_rejects_unseen_or_fabricated_quotes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory, pack = reading.prepare_reading(self._document(root), root/"pack", self.config, question="Results?")
            receipt = {"pack_sha256": reading.sha256_file(directory/"reading-pack.json"), "actor": "test-agent",
                       "observations": [{"chunk_id": pack["chunks"][0]["id"], "quote": "fabricated quote", "note": "Claim"}]}
            reading.write_json(root/"receipt.json", receipt)
            with self.assertRaises(ValueError):
                reading.verify_receipt(directory/"reading-pack.json", root/"receipt.json", root/"verified", self.config)

    def test_valid_receipt_distinguishes_quote_integrity_and_understanding(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory, pack = reading.prepare_reading(self._document(root), root/"pack", self.config, question="Results?")
            first = pack["chunks"][0]
            receipt = {"pack_sha256": reading.sha256_file(directory/"reading-pack.json"), "actor": "test-agent",
                       "observations": [{"chunk_id": first["id"], "quote": first["text"], "note": "Title inspected"}]}
            reading.write_json(root/"receipt.json", receipt)
            _, result = reading.verify_receipt(directory/"reading-pack.json", root/"receipt.json", root/"verified", self.config)
            self.assertTrue(result["quote_integrity_verified"])
            self.assertFalse(result["semantic_accuracy_verified"])
            self.assertFalse(result["whole_article_read_certified"])
            self.assertLess(result["article_text_coverage"], 1)

    def test_modified_original_invalidates_reading(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self._document(root)
            (root/"source.xml").write_bytes(b"changed")
            with self.assertRaises(ValueError):
                reading.prepare_reading(path, root/"pack", self.config, question="Results?")

    def test_fulltext_truncation_has_continuation_not_full_read_claim(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self._document(root)
            doc = reading.read_json(path)
            doc["elements"][0]["text"] = "long synthetic passage "*600
            path.write_text(json.dumps(doc), encoding="utf-8")
            _, pack = reading.prepare_reading(path, root/"pack", self.config, question="Results?", max_chars=4000)
            self.assertGreater(pack["remaining_chunks"], 0)
            self.assertEqual(pack["next_start_chunk"], 1)
            _, later = reading.prepare_reading(path, root/"later", self.config, question="Results?", max_chars=4000, start_chunk=1)
            self.assertEqual(later["prior_chunks_not_in_this_pack"], 1)


if __name__ == "__main__":
    unittest.main()
