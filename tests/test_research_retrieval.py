"""Research-selected candidate and host-isolation behavior, entirely offline."""
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from lit_harvest import downloader, http, pipeline
from lit_harvest.models import PaperRecord
from test_identity_downloader import Client, Response, good_pdf, paper


class ResearchRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = copy.deepcopy(pipeline.DEFAULT_CONFIG)
        self.config["sources"] = {key: False for key in self.config["sources"]}
        self.enterContext(patch.dict(http._HOST_COOLDOWNS, {}, clear=True))
        self.enterContext(patch.object(downloader, "is_public_https_url", return_value=True))

    def retrieve(self, record, client, enrich=None, hosts=None):
        manifest = pipeline.new_manifest("synthetic", [record], {}, {"target_pdfs": 1})
        manifest["host_retry_at"] = hosts or {}
        with patch.object(downloader, "HttpClient", return_value=client), patch.object(
                pipeline, "enrich_records", side_effect=enrich or (lambda *a, **kw: {})):
            pipeline.retrieve_population(self.root, manifest, [record], self.config, {})
        return manifest

    def test_candidate_roundtrip_preserves_distinct_referers_but_not_exact_duplicates(self):
        record = paper()
        for referer in ("", "https://example.org/a", "https://example.org/b", "https://example.org/b"):
            record.add_pdf_candidate("https://example.org/file", "openalex", referer=referer)
        copied = PaperRecord.from_dict(record.to_dict())
        self.assertEqual(len(copied.pdf_candidates), 3)
        self.assertEqual({c.get("referer", "") for c in copied.pdf_candidates},
                         {"", "https://example.org/a", "https://example.org/b"})

    def test_enrichment_retries_new_context_on_same_url(self):
        record = paper()
        url, referer = "https://example.org/file", "https://example.org/article"
        record.add_pdf_candidate(url, "openalex")
        class RefClient(Client):
            def request(self, requested, **kwargs):
                self.calls.append((requested, kwargs))
                if kwargs.get("headers", {}).get("Referer") != referer:
                    raise http.HttpError("referer required", 403, requested)
                return Response(good_pdf(), requested)
        client = RefClient([])
        def enrich(records, *args, **kwargs):
            records[0].add_pdf_candidate(url, "unpaywall", referer=referer)
            return {}
        manifest = self.retrieve(record, client, enrich=enrich)
        self.assertEqual(manifest["metrics"]["pdf_count"], 1)
        self.assertEqual(len(client.calls), 2)

    def test_resume_delayed_host_does_not_block_available_copy(self):
        record = paper()
        deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        record.retry_at = deadline
        record.download_status = "deferred"
        record.add_pdf_candidate("https://delayed.example/a.pdf", "openalex")
        record.add_pdf_candidate("https://ready.example/b.pdf", "unpaywall")
        record.attempts = [{"url": "https://delayed.example/a.pdf", "retry_at": deadline, "outcome": "deferred"}]
        client = Client([Response(good_pdf(), "https://ready.example/b.pdf")])
        manifest = self.retrieve(record, client, hosts={"delayed.example": deadline})
        self.assertEqual(manifest["metrics"]["pdf_count"], 1)
        self.assertEqual([url for url, _ in client.calls], ["https://ready.example/b.pdf"])
        self.assertEqual(manifest["host_retry_at"]["delayed.example"], deadline)

    def test_all_delayed_hosts_are_not_contacted(self):
        record = paper()
        deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        record.retry_at = deadline
        record.download_status = "deferred"
        record.add_pdf_candidate("https://delayed.example/a.pdf", "openalex")
        record.attempts = [{"url": "https://delayed.example/a.pdf", "retry_at": deadline, "outcome": "deferred"}]
        client = Client([])
        manifest = self.retrieve(record, client, hosts={"delayed.example": deadline})
        self.assertEqual(client.calls, [])
        self.assertEqual(manifest["metrics"]["pdf_count"], 0)
        self.assertEqual(record.download_status, "deferred")

    def test_legacy_deadline_without_host_evidence_is_not_bypassed(self):
        record = paper()
        record.retry_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        record.download_status = "deferred"
        record.add_pdf_candidate("https://unknown.example/a.pdf", "openalex")
        client = Client([])
        self.retrieve(record, client)
        self.assertEqual(client.calls, [])
        self.assertEqual(record.download_status, "deferred")


if __name__ == "__main__":
    unittest.main()
