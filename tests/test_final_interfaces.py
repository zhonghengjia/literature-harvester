"""Independent offline interface checks against the integrated candidate."""
from pathlib import Path
import sys
import tempfile
import unittest
from io import BytesIO
from http.client import IncompleteRead
from unittest.mock import Mock, patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from lit_harvest import downloader, http, pipeline, outcomes, zotero
from lit_harvest.models import PaperRecord
from test_identity_downloader import good_pdf, paper


class Response(BytesIO):
    def __init__(self, content=b"", broken=False):
        super().__init__(content)
        self.broken = broken
        self.url = "https://example.org/paper.pdf"
        self.headers = {}

    def read1(self, count=-1):
        if self.broken:
            raise IncompleteRead(b"partial")
        return super().read(count)


class FinalInterfaceChecks(unittest.TestCase):
    def setUp(self):
        http._HOST_COOLDOWNS.clear()
        http._ARXIV_COMPLETED = 0.0

    @patch("lit_harvest.downloader.is_public_https_url", return_value=True)
    @patch("lit_harvest.downloader.time.sleep")
    def test_wrapped_stream_failure_recovers(self, sleep, public):
        client = Mock()
        client.request.side_effect = [
            http._SafeResponse(Response(broken=True), "https://example.org/paper.pdf"),
            http._SafeResponse(Response(good_pdf()), "https://example.org/paper.pdf"),
        ]
        record = paper()
        record.add_pdf_candidate("https://example.org/paper.pdf", "openalex", "cc-by")
        with tempfile.TemporaryDirectory() as directory, patch.object(downloader, "HttpClient", return_value=client):
            downloader.download_record(record, Path(directory) / "pdfs")
            self.assertEqual(record.download_status, "downloaded")
            self.assertEqual(record.identity_status, "verified")
            self.assertEqual(client.request.call_count, 2)
            self.assertTrue(any(item["outcome"] == "retry_body" for item in record.attempts))
            self.assertFalse(list(Path(directory).rglob("*.part")))

    def test_resume_prevalidates_later_success_before_new_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_pdf = root / "old.pdf"
            old_pdf.write_bytes(good_pdf())
            existing = paper()
            existing.local_pdf = str(old_pdf)
            existing.sha256 = downloader.sha256_file(old_pdf)
            existing.download_status = "downloaded"
            existing.identity_status = "verified"
            pending = PaperRecord("Untried preceding candidate with a different identity", doi="10.1234/pending")
            pending.add_pdf_candidate("https://example.org/not-requested.pdf", "openalex", "cc-by")
            manifest = pipeline.new_manifest("frozen", [pending, existing], {}, {"target_pdfs": 1})
            original = old_pdf.read_bytes()
            with patch.object(http.HttpClient, "request", side_effect=AssertionError("network forbidden")):
                pipeline.retrieve_population(root / "new", manifest, [pending, existing], pipeline.load_config(), {})
            self.assertEqual(pending.download_status, "pending")
            self.assertEqual(manifest["metrics"]["pdf_count"], 1)
            self.assertEqual(manifest["metrics"]["record_count"], 2)
            self.assertEqual(old_pdf.read_bytes(), original)

    def test_legacy_conflict_population_is_retained(self):
        first = paper(pmid="1")
        conflict = paper(pmid="2")
        conflict.download_status = "dead_link"
        population = outcomes.collect_population({"download_attempts": [conflict.to_dict()]}, [first])
        self.assertEqual(len(population), 2)
        self.assertTrue(all(item.extra["identity_conflict"] for item in population))

    def test_content_blocked_status_is_a_dictionary_without_network(self):
        config = pipeline.load_config()
        config["sources"] = {name: False for name in config["sources"]}
        config["openalex_content"] = {"enabled": True, "free_only": True, "max_files": 2}
        with patch.object(http.HttpClient, "request", side_effect=AssertionError("network forbidden")):
            records, statuses = pipeline.search_all("synthetic", config, 2, {})
        self.assertEqual(records, [])
        self.assertIsInstance(statuses["openalex_content"], dict)
        self.assertTrue(statuses["openalex_content"]["status"].startswith("blocked"))
        self.assertEqual(statuses["openalex_content"]["attempted"], 0)

    def test_consecutive_arxiv_requests_are_paced_after_close(self):
        client = http.HttpClient(validate_redirects=lambda url: True)
        client.opener = Mock()
        client.opener.open.side_effect = [Response(), Response()]
        with patch.object(http.time, "monotonic", return_value=100.0), patch.object(http.time, "sleep") as sleep:
            with client.request("https://arxiv.org/pdf/2401.12345"):
                pass
            with client.request("https://arxiv.org/pdf/2401.12346"):
                pass
        self.assertEqual(client.opener.open.call_count, 2)
        sleep.assert_called_once_with(3.0)

    def test_redirect_rate_limit_defers_actual_host(self):
        client = http.HttpClient(validate_redirects=lambda url: True)
        client.opener = Mock()
        client.opener.open.side_effect = HTTPError("https://publisher.example/paper", 429, "slow", {"Retry-After": "3600"}, None)
        with self.assertRaises(http.HttpError):
            client.request("https://doi.org/10.1234/synthetic")
        self.assertIn("publisher.example", http.host_deferrals())
        self.assertNotIn("doi.org", http.host_deferrals())
        other = http.HttpClient(validate_redirects=lambda url: True)
        other.opener = Mock()
        with self.assertRaises(http.HttpError) as caught:
            other.request("https://publisher.example/another")
        self.assertTrue(caught.exception.deferred)
        other.opener.open.assert_not_called()

    def test_direct_zotero_import_refuses_unbound_attachment(self):
        record = PaperRecord("Different synthetic paper identity not in supplied PDF", doi="10.1234/wrong")
        client = Mock()
        client.get_items.return_value = []
        client.get_collections.return_value = []
        client.get_item.return_value = {"key": "SYNTHETIC", "data": {
            "itemType": "journalArticle", "title": record.title, "DOI": record.doi}}
        def plan(records, items):
            records[0].zotero = {"action": "attach_existing", "match_key": "SYNTHETIC"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unbound.pdf"
            path.write_bytes(good_pdf())
            record.local_pdf = str(path)
            with patch.object(zotero, "plan_import", side_effect=plan):
                result = zotero.import_records(client, [record], query="synthetic")
        client.upload_attachment.assert_not_called()
        self.assertEqual(result["parent_created_attachment_unverified"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
