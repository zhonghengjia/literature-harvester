"""Regression cases for the offline run triage and syntax/file integrity fixes."""
from contextlib import redirect_stdout
import hashlib
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import literature_harvester as cli
from lit_harvest import downloader
from lit_harvest.models import PaperRecord
from lit_harvest.pipeline import new_manifest, save_manifest
from lit_harvest.query_lint import inspect_query
from lit_harvest.run_check import check_run
from test_identity_downloader import Client, Response, good_pdf, paper
from test_coverage_fulltext import _jats_fixture


def emit(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RunCheckTests(unittest.TestCase):
    def test_query_check_persists_all_grouped_operator_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            plan = root / "query.json"
            emit(plan, {"schema_version": 1, "question": "Chloride", "routes": [
                {"id": "pm", "source": "pubmed", "syntax": "native", "query": "(chloride AND)"},
                {"id": "epmc", "source": "europe_pmc", "syntax": "native", "query": '(TITLE_ABS:"chloride" OR)'},
            ]})
            old = plan.read_bytes()
            with redirect_stdout(StringIO()), patch("socket.socket.connect", side_effect=AssertionError):
                rc = cli.main(["query-check", "--plan", str(plan), "--output-dir", str(root / "check")])
            self.assertEqual(rc, 2)
            result = json.loads((root / "check/query-validation.json").read_text(encoding="utf8"))
            self.assertEqual([r["route_id"] for r in result["routes"]], ["pm", "epmc"])
            self.assertEqual([r["errors"][0]["code"] for r in result["routes"]], ["trailing_boolean_operator"] * 2)
            self.assertEqual(plan.read_bytes(), old)
        valid = '(chloride OR "AND") AND (2020:2024[dp] OR "OR")'
        self.assertEqual(inspect_query(valid, "pubmed")["errors"], [])

    def test_check_run_reports_separate_denominators_and_preserves_source_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pdf = root / "existing.pdf"
            pdf.write_bytes(b"changed after acceptance")
            article = root / "source.xml"
            article.write_bytes(b"accepted source artifact")
            document = root / "document.json"
            doc_hash = emit(document, {"format": "jats", "source": {"artifact": str(article)},
                "source_sha256": hashlib.sha256(article.read_bytes()).hexdigest(), "elements": [],
                "coverage": {"body_text_chars": 500}, "gaps": [{"reason": "page_limit"}]})
            rows = [
                PaperRecord(title="changed file", pmid="10001", local_pdf=str(pdf), sha256="0" * 64,
                            download_status="downloaded", identity_status="verified", extra={"candidate_id":"a"}),
                PaperRecord(title="deferred record", pmid="10002", download_status="deferred",
                            retry_at="2030-01-01T00:00:00+00:00", extra={"candidate_id":"b"},
                            attempts=[{"stage":"file", "outcome":"deferred", "source":"pmc_oa_cloud"}]),
            ]
            manifest = root / "manifest.json"
            save_manifest(manifest, new_manifest("chloride", rows, {
                "pubmed": {"status":"partial", "count":1, "missing_requested_ids":["10003"]},
                "europe_pmc": {"status":"ok", "count":4, "effective_limit":4, "exhausted":False},
                "crossref": {"status":"ok", "count":0, "exhausted":True},
                "selection": {"deduplicated_records":6, "outside_selected_cap":4},
            }, {}))
            index = root / "content-index.json"
            emit(index, {"schema_version":1, "source_manifest_sha256":hashlib.sha256(manifest.read_bytes()).hexdigest(),
                         "records":[{"candidate_id":"a","status":"extracted","document":str(document),
                                     "document_sha256":doc_hash}, {"candidate_id":"b","status":"pending_budget"}]})
            before = {path: path.read_bytes() for path in (pdf, article, document, manifest, index)}
            with patch("socket.socket.connect", side_effect=AssertionError):
                result = check_run(manifest, index)
                with redirect_stdout(StringIO()):
                    rc = cli.main(["run-check", "--manifest", str(manifest), "--content-index",
                                   str(index), "--output-dir", str(root / "check")])
            self.assertEqual(rc, 2)
            self.assertEqual(result["search_status_counts"], {"partial":1, "quota_reached":1, "zero_results":1})
            self.assertEqual(result["outside_selected_queue"], 4)
            self.assertEqual(result["selected_records"], 2)
            self.assertEqual(result["pdf_status_counts"], {"changed_pdf":1,"deferred":1})
            self.assertEqual(result["content_status_counts"], {"extracted_with_gaps":1, "pending_budget":1})
            self.assertEqual(result["records"][0]["content"]["next_actions"], ["content-next"])
            self.assertEqual(result["failed_pdf_attempt_counts"][0]["attempts"], 1)
            self.assertEqual(before, {path: path.read_bytes() for path in before})
            article.write_bytes(b"source modified")
            self.assertEqual(check_run(manifest, index)["records"][0]["content"]["status"],
                             "content_integrity_error")

    def test_official_md5_checks_pdf_before_commit_then_allows_alternative(self):
        raw = good_pdf()
        url = "https://example.org/provider.pdf"
        alternative = "https://example.org/alternate.pdf"
        record = paper()
        record.add_pdf_candidate(url, "pmc_oa_cloud", expected_md5="0" * 32)
        record.add_pdf_candidate(alternative, "unpaywall")
        with tempfile.TemporaryDirectory() as folder:
            client = Client([Response(raw, url), Response(raw, alternative)])
            with patch.object(downloader, "HttpClient", return_value=client):
                downloader.download_record(record, Path(folder) / "pdfs")
            self.assertEqual(record.download_status, "downloaded", record.failure_reason)
            self.assertEqual(record.download_url, alternative)
            self.assertIn("checksum_conflict", [a["outcome"] for a in record.attempts])
            self.assertEqual(len(list((Path(folder) / "pdfs").glob("*.pdf"))), 1)

    def test_official_md5_checks_legacy_jats_before_deriving_text(self):
        raw = _jats_fixture("PMC1234567")
        url = "https://example.org/provider.xml"
        for expected, status in [("0" * 32, "checksum_conflict"),
                                 (hashlib.md5(raw, usedforsecurity=False).hexdigest(), "fulltext_only")]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as folder:
                record = paper(pmcid="PMC1234567")
                record.add_pdf_candidate(url, "pmc_oa_cloud", kind="jats", pmcid=record.pmcid,
                                         expected_md5=expected)
                with patch.object(downloader, "HttpClient", return_value=Client([Response(raw, url)])):
                    downloader.download_record(record, Path(folder) / "pdfs")
                self.assertEqual(record.download_status, status, record.failure_reason)
                self.assertEqual(bool(record.local_fulltext), status == "fulltext_only")
