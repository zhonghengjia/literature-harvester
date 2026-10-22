"""Approval-specific regression tests; all data and credentials are synthetic."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
from contextlib import nullcontext

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lit_harvest import reading, pipeline, sources, browser
from lit_harvest.article_document import jats_document, element_reading_text, pdf_document
from lit_harvest.html_document import html_document
from lit_harvest.fulltext import FullTextError, extract_pdf_urls
from lit_harvest.http import HttpClient, HttpError, redact_url
from lit_harvest.models import PaperRecord
from lit_harvest.zotero import ZoteroLocalClient, ZoteroError, find_collection_key
from lit_harvest.pmc import _metadata_candidates
from lit_harvest.metered import ContentBudget, content_policy


class ApprovedLimits(unittest.TestCase):
    def test_extensionless_pdf_is_discovered_not_certified(self):
        import io
        from lit_harvest.fulltext import resolve_landing_pdfs
        response = io.BytesIO(b"%PDF-1.7\n" + b"x" * 5000)
        response.url = "https://repository.test/resource/42"
        client = Mock(resource_limits={"html_bytes": 1024})
        client.request.return_value = response
        self.assertEqual(resolve_landing_pdfs(client, response.url), [response.url])

    def test_provider_xml_budget_is_enforced(self):
        import io
        with self.assertRaisesRegex(ValueError, "resource budget"):
            sources._read_xml_response(io.BytesIO(b"x" * 11), Mock(resource_limits={"xml_bytes": 10}))

    def test_core_keeps_earlier_page_when_later_page_fails(self):
        client = Mock()
        item = {"doi": self.paper.doi, "downloadUrl": "https://repository.test/object"}
        client.get_json.side_effect = [{"results": [item] * 50, "totalHits": 100}, HttpError("temporary failure")]
        count, attempted, errors = sources.enrich_core(client, [self.paper], "synthetic", limit=100)
        self.assertEqual((count, attempted, len(errors)), (1, 1, 1))
        self.assertTrue(self.paper.extra["core_search"]["partial"])
        self.assertEqual(self.paper.pdf_candidates[0]["url"], item["downloadUrl"])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = pipeline.load_config()
        self.paper = PaperRecord(title="Synthetic chloride study", doi="10.1234/example", pmcid="PMC123", year=2024, authors=["Jane Smith"])

    def document(self):
        raw = (b'<article><front><article-meta><article-id pub-id-type="pmc">PMC123</article-id>'
               b'<article-id pub-id-type="doi">10.1234/example</article-id></article-meta></front>'
               b'<body><p>Short but actual body.</p><table-wrap><caption>Measurements</caption><table>'
               b'<tr><th colspan="2">Chloride</th></tr><tr><td>101</td><td>mmol/L</td></tr></table>'
               b'<fn>Units validated</fn></table-wrap><disp-formula><tex-math>x = y + 1</tex-math></disp-formula></body></article>')
        doc = jats_document(raw, self.paper)
        artifact = self.root / "source.xml"
        artifact.write_bytes(raw)
        doc["source"] = {"artifact": str(artifact)}
        path = self.root / "document.json"
        reading.write_json(path, doc)
        return path, doc

    def manifest(self, count):
        records = [PaperRecord(title=f"Synthetic paper {i}", doi=f"10.1234/synthetic.{i}") for i in range(count)]
        manifest = pipeline.new_manifest("synthetic", records, {}, {})
        path = self.root / "manifest.json"
        pipeline.save_manifest(path, manifest)
        return path, records

    def test_content_60_records_is_not_capped_at_25(self):
        path, records = self.manifest(60)
        def acquire(record, *args, **kwargs):
            if record.title.endswith("7"):
                raise ValueError("synthetic parser failure")
            return {"candidate_id": reading.candidate_id(record), "status": "content_unavailable"}
        with patch.object(reading, "acquire_document", side_effect=acquire) as called:
            directory, index = reading.prepare_content(path, self.root / "content", self.config, local_only=True, env={})
        self.assertEqual(called.call_count, 60)
        self.assertEqual(index["metrics"]["pending_records"], 0)
        self.assertEqual(index["metrics"]["processed_records"], 60)
        self.assertEqual(index["metrics"]["ai_read_records"], 0)
        self.assertEqual(len(reading.read_json(path)["records"]), 60)

    def test_content_explicit_budget_and_frozen_resume(self):
        path, _ = self.manifest(3)
        before = path.read_bytes()
        _, index = reading.prepare_content(path, self.root / "first", self.config, local_only=True, max_records=1, env={})
        self.assertEqual(index["metrics"]["pending_records"], 2)
        old_index = self.root / "first/content-index.json"
        first_bytes = old_index.read_bytes()
        _, resumed = reading.prepare_content(path, self.root / "second", self.config,
            local_only=True, resume_index=old_index, env={})
        self.assertEqual(resumed["metrics"]["pending_records"], 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(old_index.read_bytes(), first_bytes)

    def test_resume_mismatched_manifest_rejected(self):
        path, _ = self.manifest(1)
        previous = self.root / "wrong.json"
        reading.write_json(previous, {"source_manifest_sha256": "wrong", "records": []})
        with self.assertRaisesRegex(ValueError, "different frozen"):
            reading.prepare_content(path, self.root / "output", self.config, resume_index=previous)
        self.assertFalse((self.root / "output").exists())

    def test_structures_are_in_actual_pack_and_receipt(self):
        path, doc = self.document()
        _, pack = reading.prepare_reading(path, self.root / "pack", self.config, question="Explain units")
        offered = "\n".join(c["text"] for c in pack["chunks"])
        for value in ("colspan", "mmol/L", "Units validated", "tex-math"):
            self.assertIn(value, offered)
        self.assertTrue(any(g["reason"] == "short_body_verify_completeness" for g in doc["gaps"]))

    def test_reading_continuation_verifies_chain(self):
        path, _ = self.document()
        self.config["reading"].update(chunk_chars=70, max_chars=140)
        folder, pack = reading.prepare_reading(path, self.root / "pack1", self.config, question="Explain")
        current_pack = folder / "reading-pack.json"
        raw = self.root / "receipt.json"
        reading.write_json(raw, {"pack_sha256": reading.sha256_file(current_pack), "actor": "synthetic-host",
            "covered_chunk_ids": [c["id"] for c in pack["chunks"]],
            "observations": [{"chunk_id": pack["chunks"][0]["id"], "quote": pack["chunks"][0]["text"], "note": "Synthetic observation"}]})
        folder, verified = reading.verify_receipt(current_pack, raw, self.root / "receipt1", self.config)
        self.assertFalse(verified["whole_article_read_certified"])
        _, next_pack = reading.next_reading(folder / "reading-receipt.json", self.root / "pack2", self.config)
        self.assertEqual(next_pack["start_chunk"], pack["next_start_chunk"])
        self.assertIsNotNone(next_pack["previous_receipt"])
        raw.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "checksum changed"):
            reading.next_reading(folder / "reading-receipt.json", self.root / "pack3", self.config)

    def test_chunks_reconstruct_full_element(self):
        _, doc = self.document()
        chunks = reading._chunks(doc, 70)
        for element in doc["elements"]:
            self.assertEqual("".join(c["text"] for c in chunks if c["element_id"] == element["id"]), element_reading_text(element))

    def test_html_body_identity_and_abstract_boundary(self):
        metadata = '<meta name="citation_title" content="Synthetic chloride study"><meta name="citation_doi" content="10.1234/example">'
        html = metadata + '<nav>Ignore navigation</nav><div class="article-body"><h2>Methods</h2><p>Actual methods.</p><h2>Results</h2><p>Actual results.</p></div>'
        doc = html_document(html.encode(), html, self.paper)
        self.assertEqual([e["text"] for e in doc["elements"]], ["Methods", "Actual methods.", "Results", "Actual results."])
        for wrong in (html.replace("10.1234/example", "10.1234/other"), metadata + '<div class="abstract"><p>Summary only.</p></div>'):
            with self.assertRaises(FullTextError):
                html_document(wrong.encode(), wrong, self.paper)

    def test_http_url_evidence_does_not_require_public_ip_or_port(self):
        for url in ("http://repository:8080/item?id=7", "https://127.0.0.1:444/item", "https://198.18.0.1/a"):
            self.assertEqual(browser._safe_url(url), url)
        self.assertIn("id=7", browser._safe_url("http://repository/a?id=7&token=secret"))
        self.assertNotIn("secret", browser._safe_url("http://repository/a?id=7&token=secret"))

    def test_anonymous_http_not_credential_http(self):
        client = HttpClient(max_retries=0)
        with patch.object(client.opener, "open", side_effect=AssertionError("No network")) as opened:
            with self.assertRaises(HttpError):
                client.request("http://repository/a?api_key=synthetic")
            with self.assertRaises(HttpError):
                client.request("http://repository/a", headers={"Authorization": "synthetic"})
        opened.assert_not_called()

    def test_http_pdf_declaration_preserves_source_scheme(self):
        self.assertEqual(extract_pdf_urls('<a type="application/pdf" href="/download?id=7">PDF</a>', "http://repository/article"),
                         ["http://repository/download?id=7"])

    def test_candidate_id_selects_no_doi_record(self):
        record = PaperRecord(title="No identifier synthetic study")
        key = browser.queue_records([record])["records"][0]["record_id"]
        self.assertTrue(key.startswith("candidate:"))
        self.assertIs(browser.select_record([record], key), record)

    def test_openaire_checks_oa_per_instance_and_identity(self):
        payload = {"results": [{"mainTitle": self.paper.title, "pids": [{"scheme": "doi", "value": self.paper.doi}],
            "instances": [{"accessRight": {"label": "OPEN"}, "urls": ["http://repo/article?id=7", "https://repo/a.pdf"]},
                          {"accessRight": {"label": "CLOSED"}, "urls": ["https://closed/a.pdf"]}]}]}
        client = Mock()
        client.get_json.return_value = payload
        self.assertEqual(sources.enrich_openaire(client, [self.paper]), (1, 1, []))
        self.assertEqual(len(self.paper.pdf_candidates), 2)
        self.assertTrue(all(c["oa_verified"] for c in self.paper.pdf_candidates))
        self.assertEqual(self.paper.pdf_candidates[0]["kind"], "landing")
        self.assertIn("/graph/v3/", client.get_json.call_args.args[0])

    def test_doaj_rejects_wrong_doi_before_links(self):
        client = Mock()
        client.get_json.return_value = {"results": [{"bibjson": {"identifier": [{"type": "doi", "id": "10.1234/other"}],
            "link": [{"url": "https://repo/a.pdf"}]}}]}
        self.assertEqual(sources.enrich_doaj(client, [self.paper]), (0, 1, []))

    def test_core_no_doi_needs_title_author_year(self):
        self.paper.doi = ""
        client = Mock()
        client.get_json.return_value = {"results": [{"title": self.paper.title, "authors": ["Jane Smith"],
            "yearPublished": 2024, "downloadUrl": "https://repo/a.pdf"}]}
        self.assertEqual(sources.enrich_core(client, [self.paper], "synthetic"), (1, 1, []))
        self.assertIn('title:', client.get_json.call_args.args[1]["q"])

    def test_retraction_does_not_remove_permitted_access(self):
        candidates = _metadata_candidates({"pmcid": "PMC123", "version": 1, "is_retracted": True,
            "is_pmc_openaccess": True, "is_manuscript": False, "license_code": "CC BY",
            "pdf_url": "s3://pmc-oa-opendata/PMC123.1/PMC123.1.pdf"}, "PMC123", 1)
        self.assertEqual(candidates[0]["evidence_use"], "retracted_do_not_use_as_active_evidence")

    def test_one_time_zotero_key_is_consumed_no_retry_on_timeout(self):
        client = ZoteroLocalClient(auth_cache=self.root / "unused-auth")
        client.server_id, client.api_key = "synthetic-server", "synthetic-key"
        client.write_authorization_requested = True
        with patch.object(client, "_raw_request", return_value=(b'{}', {}, 200)) as request:
            client._json_request("/users/0/items", method="POST", payload=[], authenticated=True)
            self.assertEqual(request.call_count, 1)
        self.assertEqual(client.api_key, "")
        client.api_key = "synthetic-key2"
        with patch.object(client, "_raw_request", side_effect=ZoteroError("timeout")) as request:
            with self.assertRaises(ZoteroError):
                client._json_request("/users/0/items", method="POST", payload=[], authenticated=True)
            self.assertEqual(request.call_count, 1)
        self.assertEqual(client.api_key, "")

    def test_stable_zotero_collection_key(self):
        collections = [{"key": "AAAA", "data": {"name": "Same"}}, {"key": "BBBB", "data": {"name": "Same"}}]
        self.assertEqual(find_collection_key(collections, "key:BBBB"), "BBBB")
        with self.assertRaises(ZoteroError):
            find_collection_key(collections, "Same")

    def test_metered_opt_in_reserves_before_request_and_never_logs_key(self):
        self.config["openalex_content"].update(enabled=True, free_only=False, allow_paid=True, max_files=5, max_cost_usd=0.01)
        budget = ContentBudget(self.config, "synthetic-secret", self.root)
        with patch.object(budget.client, "request", side_effect=HttpError("synthetic timeout")) as request:
            with self.assertRaises(HttpError):
                budget.request("https://content.openalex.org/works/W123.pdf")
            with self.assertRaisesRegex(HttpError, "budget exhausted"):
                budget.request("https://content.openalex.org/works/W124.pdf")
            self.assertEqual(request.call_count, 1)
        saved = (self.root / "metered-requests.json").read_text()
        self.assertNotIn("synthetic-secret", saved)
        self.assertIn("reserved_may_have_been_charged", saved)
        self.assertEqual(budget.client.max_retries, 0)

    def test_metered_free_only_and_missing_authorization_stay_unspent(self):
        for values in ({"enabled": True, "free_only": True}, {"enabled": True, "free_only": False, "allow_paid": False}):
            policy = content_policy(**values, max_files=10, max_cost_usd=1, api_key_present=True)
            self.assertEqual(policy["status"], "blocked")

    def test_large_pdf_reads_pages_after_500(self):
        from reportlab.pdfgen import canvas
        from lit_harvest.downloader import sha256_file
        path = self.root / "long.pdf"
        writer = canvas.Canvas(str(path))
        writer.drawString(35, 790, self.paper.title)
        writer.drawString(35, 770, self.paper.doi)
        writer.showPage()
        for number in range(2, 502):
            writer.drawString(35, 790, f"Synthetic page {number}")
            writer.showPage()
        writer.save()
        self.paper.sha256 = sha256_file(path)
        doc = pdf_document(path, self.paper, ocr="off")
        self.assertEqual(doc["coverage"]["page_count"], 501)
        self.assertEqual(len(doc["coverage"]["pages"]), 501)
        self.assertEqual(doc["elements"][-1]["page"], 501)

    def test_pubmed_partition_preserves_query_and_count(self):
        original = '(chloride OR hyperchloremia) AND humans'
        class Pacer:
            def slot(self, *_):
                return nullcontext()
        class Client:
            calls = []
            def get_json(self, url, params):
                self.calls.append(params)
                if params["term"] == original:
                    count = 10001
                    ids = []
                elif '(1:999999999[UID])' in params["term"]:
                    count, ids = 10001, []
                elif '(1:500000000[UID])' in params["term"]:
                    count = 6000
                    start = params["retstart"]
                    ids = [str(n) for n in range(100000+start, 100000+start+params["retmax"])]
                else:
                    count = 4001
                    start = params["retstart"]
                    ids = [str(n) for n in range(600000000+start, 600000000+start+params["retmax"])]
                return {"esearchresult": {"count": str(count), "idlist": ids}}
        client = Client()
        status = sources._search_status(11000, "pubmed")
        result = sources._pubmed_partitioned_ids(client, original, 11000, {}, Pacer(), 0, status)
        self.assertEqual(len(set(result)), 10001)
        self.assertTrue(status["exhausted"])
        self.assertTrue(all(original in call["term"] for call in client.calls))
        self.assertTrue(all(call["retstart"] < 10000 for call in client.calls))

    def test_zotero_existing_attachment_requires_real_bytes(self):
        from lit_harvest.zotero import plan_import
        items = [{"key": "PARENT", "data": {"itemType": "journalArticle", "title": self.paper.title, "DOI": self.paper.doi}},
                 {"key": "CHILD", "data": {"itemType": "attachment", "parentItem": "PARENT", "contentType": "application/pdf"}}]
        client = Mock()
        client.verify_existing_attachment.side_effect = ZoteroError("Missing bytes")
        plan_import([self.paper], items, client=client)
        self.assertEqual(self.paper.zotero["status"], "existing_attachment_unverified")
        client.verify_existing_attachment.side_effect = None
        client.verify_existing_attachment.return_value = {"local_bytes_verified": True, "sha256": "synthetic"}
        plan_import([self.paper], items, client=client)
        self.assertEqual(self.paper.zotero["status"], "verified_existing_with_pdf")

    def test_media_download_retains_uninterpreted_gap(self):
        from lit_harvest.assets import acquire_assets
        doc = {"elements": [{"id": "fig1", "kind": "figure", "resource_references": ["image.png"]}],
               "source": {"url": "https://repository/a/article.xml", "candidate": {"oa_route": "open_access"}}}
        raw = b"\x89PNG\r\n\x1a\nsynthetic"
        with patch("lit_harvest.assets._fetch_bounded", return_value=(raw, "https://repository/a/image.png", "")):
            acquire_assets(doc, self.root, Mock(), self.config)
        asset = doc["elements"][0]["assets"][0]
        self.assertEqual(asset["status"], "acquired_not_interpreted")
        self.assertFalse(asset["content_interpreted"])
        self.assertEqual(Path(asset["path"]).read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
