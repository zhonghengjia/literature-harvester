"""Offline identity and request-context regressions with positive controls."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

CANONICAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CANONICAL / "scripts"))
sys.path.insert(0, str(CANONICAL / "tests"))
from lit_harvest import downloader, identity
from lit_harvest.http import HttpError
from test_identity_downloader import Client, Response, TITLE, DOI, paper, good_pdf, pdf_bytes


class RetrievalBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="lh-boundary-test-")
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name) / "pdfs"
        self.folder.mkdir()
        for guard in (
            patch.object(downloader, "is_public_https_url", return_value=True),
            patch("socket.create_connection", side_effect=AssertionError("Network forbidden")),
            patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Network forbidden")),
        ):
            guard.start()
            self.addCleanup(guard.stop)

    def run_record(self, record, client, **kwargs):
        with patch.object(downloader, "HttpClient", return_value=client):
            return downloader.download_record(record, self.folder, **kwargs)

    def test_correction_is_not_original_article(self):
        result = identity.identity_from_first_page(
            f"Correction: {TITLE}\nJane Doe\n10.1234/correction\nCorrection to {DOI}", paper())
        self.assertEqual(result.status, "manual_review")

    def test_correction_can_be_the_requested_article(self):
        title, doi = f"Correction: {TITLE}", "10.1234/correction"
        result = identity.identity_from_first_page(
            f"{title}\nJane Doe\n{doi}\nCorrection to {DOI}", paper(title=title, doi=doi))
        self.assertEqual(result.status, "verified")

    def test_conflicting_arxiv_base_identifier_needs_review(self):
        record = paper(doi="", arxiv_id="2401.00001")
        result = identity.identity_from_first_page(
            f"{TITLE}\nJane Doe\narXiv:2401.99999v2 [cs.LG]", record)
        self.assertEqual(result.status, "manual_review")

    def test_matching_arxiv_base_identifier_keeps_existing_capability(self):
        record = paper(doi="", arxiv_id="2401.00001")
        result = identity.identity_from_first_page(
            f"{TITLE}\nJane Doe\narXiv:2401.00001v2 [cs.LG]", record)
        self.assertEqual(result.status, "verified")

    def test_pypdf_fallback_keeps_identity_verification(self):
        from pypdf import PdfReader
        path = self.folder / "fallback.pdf"
        path.write_bytes(good_pdf())
        with patch.object(identity, "_pdf_backend", return_value=("pypdf", PdfReader)):
            self.assertEqual(identity.verify_pdf_identity(path, paper()).status, "verified")
            self.assertEqual(identity.verify_pdf_identity(path, paper(doi="10.9999/wrong")).status, "manual_review")

    def test_changed_checksum_remains_review_in_second_phase(self):
        record = paper()
        path = self.folder / downloader.filename_for(record)
        path.write_bytes(good_pdf())
        record.local_pdf, record.sha256 = str(path), downloader.sha256_file(path)
        path.write_bytes(pdf_bytes([TITLE, "Jane Doe", DOI, "Changed content."]))
        self.run_record(record, Client([]), allow_fulltext_fallback=False)
        self.assertEqual(record.download_status, "manual_review")
        record = type(record).from_dict(record.to_dict())
        self.run_record(record, Client([]), allow_fulltext_fallback=True)
        self.assertEqual(record.download_status, "manual_review")
        self.assertEqual(record.local_pdf, "")

    def test_landing_referer_allows_distinct_request_context(self):
        record = paper()
        url, landing = "https://synthetic.example/a.pdf", "https://synthetic.example/article"
        record.add_pdf_candidate(url, "openalex")
        record.add_pdf_candidate(landing, "unpaywall_landing", kind="landing")
        class RefererClient(Client):
            def request(self, requested, **kwargs):
                self.calls.append((requested, kwargs))
                if kwargs.get("headers", {}).get("Referer") != landing:
                    raise HttpError("Referer required", 403, requested)
                return Response(good_pdf(), requested)
        client = RefererClient([])
        with patch.object(downloader, "resolve_landing_pdfs", return_value=[url]):
            self.run_record(record, client)
        self.assertEqual(record.download_status, "downloaded")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[-1][1]["headers"]["Referer"], landing)


if __name__ == "__main__":
    unittest.main()
