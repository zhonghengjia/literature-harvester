"""Independent, offline user-workflow acceptance checks.

Run directly with the bundled Python. Only scholarly discovery and network
transport are fixtures; CLI, filtering, retrieval, PDF validation, manifests,
reports, resume, and Zotero planning execute the candidate implementation.
All generated files live in a TemporaryDirectory. No library writes occur.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from reportlab.pdfgen import canvas
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("workflow_cli", ROOT / "scripts/literature_harvester.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
from lit_harvest import pipeline
from lit_harvest.http import HttpClient, HttpError
from lit_harvest.models import PaperRecord


def pdf_bytes(title: str, doi: str, marker: str = "") -> bytes:
    stream = io.BytesIO()
    document = canvas.Canvas(stream, invariant=True)
    document.setTitle(title)
    document.setAuthor("Alice Example")
    text = document.beginText(40, 790)
    for line in [title, "Alice Example", "2024", f"DOI: {doi}", marker,
                 "Synthetic offline study. No patient data. Hyperchloremia analysis."]:
        text.textLine(line)
    document.drawText(text)
    document.showPage()
    document.save()
    result = stream.getvalue()
    assert len(PdfReader(io.BytesIO(result)).pages) == 1
    return result


class Response(io.BytesIO):
    def __init__(self, content: bytes):
        super().__init__(content)
        self.headers = {"Content-Type": "application/pdf", "Content-Length": str(len(content))}
        self.status = 200


class ReadOnlyZotero:
    def __init__(self, items=()):
        self.items = list(items)
        self.calls = []

    def status(self):
        self.calls.append("status")
        return SimpleNamespace(reachable=True, message="offline fixture")

    def get_items(self):
        self.calls.append("get_items")
        return self.items

    def get_collections(self):
        self.calls.append("get_collections")
        return []

    def __getattr__(self, name):
        raise AssertionError(f"Unexpected Zotero operation: {name}")


class OfflineWorkflowAcceptance(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="lit-workflow-")
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.config = copy.deepcopy(pipeline.DEFAULT_CONFIG)
        self.config["sources"] = {name: False for name in self.config["sources"]}
        self.config["sources"]["pubmed"] = True
        self.config["general"]["contact_email"] = ""
        self.config.setdefault("openalex_content", {})["enabled"] = False
        self.pool = []
        self.responses = {}
        self.requests = []
        self.search_calls = []
        self.stack.enter_context(patch.object(cli, "load_config", return_value=self.config))
        self.stack.enter_context(patch.object(pipeline, "_search_one", side_effect=self.discovery))
        self.stack.enter_context(patch.object(HttpClient, "request", side_effect=self.transport))
        self.stack.enter_context(patch("socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]))
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("Real network forbidden")))
        self.stack.enter_context(patch.dict("os.environ", {name: "" for name in [
            "OPENALEX_API_KEY", "S2_API_KEY", "NCBI_API_KEY", "CORE_API_KEY",
            "UNPAYWALL_EMAIL", "LITERATURE_HARVESTER_CONTACT_EMAIL"]}))

    def discovery(self, source, query, limit, config, env):
        self.search_calls.append((source, query, limit))
        return copy.deepcopy(self.pool[:limit])

    def transport(self, url, **kwargs):
        self.requests.append(url)
        if kwargs.get("method", "GET") != "GET":
            raise AssertionError("Network mutation forbidden")
        if url not in self.responses:
            raise AssertionError(f"Unregistered offline URL: {url}")
        data = self.responses[url]
        if isinstance(data, Exception):
            raise data
        return Response(data)

    def add_record(self, number, *, kind="research", title=None, outcome="pdf"):
        title = title or f"Hyperchloremia and renal recovery in synthetic cohort {number}"
        doi = f"10.9999/offline.{number}"
        url = f"https://example.org/oa/{number}.pdf"
        kinds = {"research": "Observational Study", "review": "Systematic Review",
                 "unknown": "Journal Article", "case_report": "Case Reports"}
        record = PaperRecord(title=title, authors=["Alice Example"], year=2024,
            doi=doi, sources=["pubmed"], is_oa=True, license="cc-by",
            relevance_score=1000-number, extra={"publication_types": [kinds[kind]]})
        if outcome != "none":
            record.add_pdf_candidate(url=url, source="openalex", license="cc-by", version="publishedVersion")
        if outcome == "pdf":
            self.responses[url] = pdf_bytes(title, doi, str(number))
        elif outcome == "wrong":
            self.responses[url] = pdf_bytes("Unrelated marine geology article", "10.9999/unrelated")
        elif outcome == "html":
            self.responses[url] = b"<html>Subscription access required</html>"
        elif outcome == "404":
            self.responses[url] = HttpError("Fixture missing article", status=404, url=url)
        self.pool.append(record)
        return record

    def invoke(self, *arguments, expected_code=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(list(arguments))
        self.assertEqual(code, expected_code, stderr.getvalue() or stdout.getvalue())
        self.assertTrue(stdout.getvalue(), stderr.getvalue())
        return json.loads(stdout.getvalue())

    def read(self, folder, name):
        return json.loads((folder / name).read_text(encoding="utf-8"))

    def assert_inventory(self, folder, count, pdfs):
        manifest = self.read(folder, "manifest.json")
        summary = self.read(folder, "run_summary.json")
        coverage = self.read(folder, "coverage.json")
        records = manifest["records"]
        self.assertEqual(len(records), count)
        self.assertEqual(summary["record_count"], count)
        self.assertEqual(coverage["record_count"], count)
        self.assertEqual(summary["pdf_count"], pdfs)
        self.assertEqual(coverage["downloaded"], pdfs)
        self.assertEqual(sum(summary["download_status_counts"].values()), count)
        self.assertEqual(summary["attempted_records"] + summary["pending_records"], count)
        self.assertEqual(summary["download_rate"], round(pdfs/count, 4) if count else 0)
        jsonlines = [json.loads(line) for line in (folder/"metadata.jsonl").read_text(encoding="utf-8").splitlines()]
        with (folder/"literature.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        expected = {record["doi"] for record in records}
        self.assertEqual({row["doi"] for row in rows}, expected)
        self.assertEqual({row["doi"] for row in jsonlines}, expected)
        self.assertEqual(len(rows), count)
        self.assertEqual(len(jsonlines), count)
        bib = (folder/"references.bib").read_text(encoding="utf-8")
        for doi in expected:
            self.assertIn(doi, bib)
        for name in ["literature.md", "coverage.md", "failed_downloads.md"]:
            self.assertTrue((folder/name).is_file(), name)
        return manifest, summary, coverage

    def test_topic_ten_actual_pdfs_and_export_conservation(self):
        self.add_record(1, outcome="404")
        self.add_record(2, outcome="wrong")
        self.add_record(3, outcome="html")
        self.add_record(4, outcome="none")
        for number in range(5, 17):
            self.add_record(number)
        output = self.folder/"ten"
        result = self.invoke("run", "--query", "hyperchloremia", "--target-pdfs", "10",
            "--required-term", "hyperchloremia", "--output-dir", str(output))
        manifest, summary, coverage = self.assert_inventory(output, 16, 10)
        self.assertTrue(result["target_met"])
        self.assertTrue(summary["target_met"])
        self.assertEqual(summary["pending_records"], 2)
        successes = [r for r in manifest["records"] if r["download_status"] in {"downloaded", "already_downloaded"}]
        self.assertEqual(len(successes), 10)
        hashes = set()
        for record in successes:
            path = Path(record["local_pdf"])
            raw = path.read_bytes()
            hashes.add(hashlib.sha256(raw).hexdigest())
            self.assertEqual(record["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertIn(record["doi"], " ".join(p.extract_text() for p in PdfReader(path).pages))
        self.assertEqual(len(hashes), 10)
        self.assertNotIn("https://example.org/oa/15.pdf", self.requests)

    def test_shortfall_is_visible_and_not_relabelled_complete(self):
        self.add_record(1, outcome="none")
        self.add_record(2, outcome="pdf")
        output = self.folder/"shortfall"
        result = self.invoke("run", "--query", "hyperchloremia", "--target-pdfs", "10", "--output-dir", str(output), expected_code=2)
        _, summary, _ = self.assert_inventory(output, 2, 1)
        self.assertFalse(result["target_met"])
        self.assertFalse(summary["target_met"])
        self.assertEqual(summary["task_completion_rate"], .1)

    def test_explicit_article_types_before_selected_cap(self):
        self.add_record(1, kind="unknown")
        self.add_record(2, kind="case_report")
        self.add_record(3, kind="review")
        self.add_record(4, kind="research")
        self.add_record(5, kind="research", title="Normochloremia and kidney injury in a synthetic cohort")
        for kind, doi in [("review", "10.9999/offline.3"), ("research", "10.9999/offline.4")]:
            with self.subTest(article_type=kind):
                folder = self.folder/kind
                self.invoke("run", "--query", "chloride", "--article-type", kind,
                    "--required-term", "hyperchloremia", "--max-results", "1", "--max-candidates", "10",
                    "--no-download", "--output-dir", str(folder))
                manifest, _, _ = self.assert_inventory(folder, 1, 0)
                self.assertEqual(manifest["records"][0]["doi"], doi)
                self.assertEqual(manifest["records"][0]["download_status"], "pending")
        self.assertEqual(self.requests, [])

    def test_jats_reader_remains_separate_from_requested_pdfs(self):
        reader = self.add_record(1, outcome="none")
        reader.pmcid = "PMC9999991"
        url = "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC9999991/fullTextXML"
        reader.add_pdf_candidate(url=url, source="europe_pmc", kind="jats", license="cc-by",
                                 access_basis="license", pmcid=reader.pmcid)
        xml = (f'<article><front><article-meta><article-id pub-id-type="pmc">9999991</article-id>'
               f'<article-id pub-id-type="doi">{reader.doi}</article-id>'
               f'<title-group><article-title>{reader.title}</article-title></title-group>'
               f'</article-meta></front><body><sec><title>Results</title><p>'
               + 'Hyperchloremia results from a synthetic offline experiment. ' * 15
               + '</p></sec></body></article>')
        self.responses[url] = xml.encode("utf-8")
        self.add_record(2)
        folder = self.folder/"jats"
        result = self.invoke("run", "--query", "hyperchloremia", "--target-pdfs", "2",
                             "--output-dir", str(folder), expected_code=2)
        manifest, summary, coverage = self.assert_inventory(folder, 2, 1)
        self.assertFalse(result["target_met"])
        self.assertEqual(summary["fulltext_only_count"], 1)
        self.assertEqual(summary["readable_fulltext_count"], 2)
        self.assertEqual(coverage["fulltext_only"], 1)
        self.assertEqual(coverage["readable_fulltext"], 2)
        record = next(r for r in manifest["records"] if r["doi"] == reader.doi)
        self.assertEqual(record["download_status"], "fulltext_only")
        self.assertTrue(Path(record["local_fulltext"]).is_file())
        self.assertFalse(record["local_pdf"])

    def test_resume_legacy_attempts_inventory_is_preserved_once(self):
        self.add_record(1)
        self.add_record(2)
        source = self.folder/"legacy.json"
        legacy = {"schema_version": 1, "run_id": "legacy-fixture", "query": "hyperchloremia",
            "settings": {"target_pdfs": 2, "download": True}, "source_status": {},
            "records": [self.pool[0].to_dict()],
            "download_attempts": [self.pool[1].to_dict(), self.pool[1].to_dict()]}
        source.write_text(json.dumps(legacy), encoding="utf-8")
        original = source.read_bytes()
        folder = self.folder/"legacy-resumed"
        self.invoke("resume", "--manifest", str(source), "--output-dir", str(folder))
        manifest, _, _ = self.assert_inventory(folder, 2, 2)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(self.search_calls, [])
        self.assertEqual({r["doi"] for r in manifest["records"]}, {r.doi for r in self.pool})

    def test_resume_is_frozen_population_without_search_and_preserves_source(self):
        self.add_record(1, outcome="pdf")
        self.add_record(2, outcome="404")
        source = self.folder/"first"
        self.invoke("run", "--query", "hyperchloremia", "--target-pdfs", "2", "--output-dir", str(source), expected_code=2)
        original = (source/"manifest.json").read_bytes()
        first_manifest = self.read(source, "manifest.json")
        first_pdf = Path(first_manifest["records"][0]["local_pdf"])
        first_hash = hashlib.sha256(first_pdf.read_bytes()).hexdigest()
        self.responses["https://example.org/oa/2.pdf"] = pdf_bytes(self.pool[1].title, self.pool[1].doi)
        self.search_calls.clear()
        self.requests.clear()
        resumed = self.folder/"resumed"
        result = self.invoke("resume", "--manifest", str(source/"manifest.json"), "--output-dir", str(resumed))
        manifest, summary, _ = self.assert_inventory(resumed, 2, 2)
        self.assertEqual(self.search_calls, [])
        self.assertEqual((source/"manifest.json").read_bytes(), original)
        self.assertEqual(hashlib.sha256(first_pdf.read_bytes()).hexdigest(), first_hash)
        self.assertNotIn("https://example.org/oa/1.pdf", self.requests)
        self.assertEqual(manifest["parent_manifest_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(manifest["parent_run_id"], first_manifest["run_id"])
        self.assertNotEqual(manifest["run_id"], first_manifest["run_id"])
        self.assertTrue(summary["target_met"])
        self.assertTrue(result["target_met"])

    def test_zotero_pdf_only_revalidates_local_files_without_writes(self):
        self.add_record(1)
        self.add_record(2)
        self.add_record(3, outcome="none")
        folder = self.folder/"zotero"
        self.invoke("run", "--query", "hyperchloremia", "--output-dir", str(folder))
        manifest = self.read(folder, "manifest.json")
        # A valid but different article is substituted after download. A preview
        # must revalidate the bytes rather than trust an old success status.
        Path(manifest["records"][1]["local_pdf"]).write_bytes(pdf_bytes("Unrelated replacement article", "10.9999/other"))
        client = ReadOnlyZotero([{"key": "EXACT001", "data": {"itemType": "journalArticle",
            "title": self.pool[0].title, "DOI": self.pool[0].doi}}])
        with patch.object(cli, "_client", return_value=client):
            result = self.invoke("zotero-plan", "--manifest", str(folder/"manifest.json"),
                "--collection", "Hyperchloremia", "--pdf-only")
        self.assertFalse(result["write_performed"])
        self.assertEqual(result["selected_records"], 1)
        self.assertEqual(result["excluded_records"], 2)
        self.assertEqual(result["actions"].get("attach_existing"), 1)
        self.assertEqual(result["actions"].get("create", 0), 0)
        self.assertEqual(set(client.calls), {"status", "get_items", "get_collections"})
        self.assertTrue((folder/"zotero_plan.md").is_file())
        self.assertEqual(len(self.read(folder, "manifest.json")["records"]), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
