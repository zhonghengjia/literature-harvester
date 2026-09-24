from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http.client import IncompleteRead
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reportlab.pdfgen import canvas

from lit_harvest import downloader, identity
from lit_harvest.http import HttpError
from lit_harvest.models import PaperRecord, normalize_pmcid, normalize_pmid


TITLE = "Synthetic clinical outcomes after a randomized intervention"
DOI = "10.1234/synthetic"


def pdf_bytes(lines: list[str], second_page: list[str] | None = None) -> bytes:
    buffer = BytesIO()
    writer = canvas.Canvas(buffer)
    for page in [lines] + ([second_page] if second_page is not None else []):
        for index, line in enumerate(page):
            writer.drawString(35, 790 - index * 20, line)
        writer.showPage()
    writer.save()
    return buffer.getvalue()


def paper(**kwargs) -> PaperRecord:
    return PaperRecord(**{"title": TITLE, "doi": DOI, "authors": ["Jane Doe"], "year": 2024, **kwargs})


def good_pdf() -> bytes:
    return pdf_bytes([TITLE, "Jane Doe", f"DOI: {DOI}", "Abstract", "This is a synthetic study, not patient data."])


class Response:
    def __init__(self, body: bytes, url: str, failure: Exception | None = None,
                 length: int | None = None) -> None:
        self.body, self.url, self.failure = body, url, failure
        self.headers = {"Content-Length": str(len(body) if length is None else length)}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read1(self, size=-1):
        if self.failure:
            raise self.failure
        if size < 0:
            size = len(self.body)
        chunk, self.body = self.body[:size], self.body[size:]
        return chunk

    def read(self, size=-1):
        return self.read1(size)


class Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get_json(self, url, **kwargs):
        raise AssertionError("Unexpected metadata request")


class IdentityTests(unittest.TestCase):
    def test_null_identifiers_never_produce_shared_keys(self):
        for value in (None, "None", "null", "NaN", "", "undefined", False):
            record = paper(pmid=value, pmcid=value)
            self.assertEqual(record.pmid, "")
            self.assertEqual(record.pmcid, "")
            self.assertFalse(any(key.startswith(("pmid:", "pmcid:")) for key in record.identity_keys))
        self.assertEqual(normalize_pmid("https://pubmed.ncbi.nlm.nih.gov/12345/"), "12345")
        self.assertEqual(normalize_pmcid("https://pmc.ncbi.nlm.nih.gov/articles/PMC123/"), "PMC123")
        self.assertEqual(normalize_pmcid("None"), "")

    def test_conflicts_are_rejected_without_mutating_existing_record(self):
        existing, incoming = paper(pmid="123"), paper(doi="10.1234/other", pmid="123")
        before = existing.to_dict()
        with self.assertRaisesRegex(ValueError, "Conflicting doi"):
            existing.merge(incoming)
        self.assertEqual(existing.to_dict(), before)

    def test_new_fields_and_eligibility_survive_round_trip(self):
        record = paper(identity_status="manual_review", retry_at="2030-01-01T00:00:00+00:00")
        record.add_pdf_candidate("https://example.test/a.xml", "pmc_oa_cloud", "TDM", "acceptedVersion",
                                 kind="jats", pmcid="PMC123", oa_route="author_manuscript_tdm",
                                 article_version=2, headers={"Authorization": "not-persistent"})
        record.attempts.append({"outcome": "deferred", "retry_at": record.retry_at})
        loaded = PaperRecord.from_dict(record.to_dict())
        self.assertEqual(loaded.to_dict(), record.to_dict())
        self.assertNotIn("headers", loaded.pdf_candidates[0])
        self.assertEqual(loaded.pdf_candidates[0]["oa_route"], "author_manuscript_tdm")

    def test_identity_digest_prevents_long_filename_collision(self):
        first = paper(title="A" * 180 + " first", doi="10.1234/first")
        second = paper(title="A" * 180 + " second", doi="10.1234/second")
        self.assertNotEqual(downloader.filename_for(first), downloader.filename_for(second))
        self.assertEqual(first.identity_digest, PaperRecord.from_dict(first.to_dict()).identity_digest)
        self.assertLess(len(downloader.filename_for(first)), 155)

    def test_real_pdf_first_page_binds_title_and_doi(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.pdf"
            path.write_bytes(good_pdf())
            downloader.validate_pdf(path)
            result = identity.verify_pdf_identity(path, paper())
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.evidence["matching_dois"], [DOI])

    def test_reference_only_or_second_page_match_is_not_identity(self):
        record = paper()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "references.pdf"
            path.write_bytes(pdf_bytes(["An unrelated article", "References", TITLE, DOI], [TITLE, DOI]))
            result = identity.verify_pdf_identity(path, record)
        self.assertEqual(result.status, "manual_review")
        self.assertFalse(result.evidence["title_match"])
        self.assertFalse(result.evidence["matching_dois"])

    def test_supplement_and_doi_only_are_not_accepted(self):
        self.assertEqual(identity.identity_from_first_page(f"Supplementary information\n{TITLE}\n{DOI}", paper()).status, "mismatch")
        self.assertEqual(identity.identity_from_first_page(f"Unrelated title\n{DOI}", paper()).status, "manual_review")

    def test_related_preprint_identifier_needs_explicit_relation(self):
        text = f"{TITLE}\nJane Doe\nDOI: 10.1101/2024.01.01.123456"
        record = paper()
        self.assertEqual(identity.identity_from_first_page(text, record).status, "manual_review")
        record.extra.update(preprint_doi="10.1101/2024.01.01.123456", version_relation_confidence="high")
        self.assertEqual(identity.identity_from_first_page(text, record).status, "verified")

    def test_parser_missing_is_explicit_and_never_signature_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "valid.pdf"
            path.write_bytes(good_pdf())
            error = identity.PdfValidationError("validation_unavailable", "No parser")
            with patch.object(identity, "_pdf_backend", side_effect=error):
                with self.assertRaises(identity.PdfValidationError):
                    downloader.validate_pdf(path)
                result = identity.verify_pdf_identity(path, paper())
        self.assertEqual(result.status, "manual_review")


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pdf_dir = Path(self.tmp.name) / "pdfs"
        self.addCleanup(patch.stopall)
        patch.object(downloader, "is_public_https_url", return_value=True).start()
        patch.object(downloader.time, "sleep").start()
        # An unexpected network connection is a test failure.
        patch("socket.create_connection", side_effect=AssertionError("Network forbidden")).start()

    def run_record(self, record, client, **kwargs):
        with patch.object(downloader, "HttpClient", return_value=client):
            return downloader.download_record(record, self.pdf_dir, **kwargs)

    def test_download_accepts_actual_identity_verified_pdf(self):
        record = paper()
        url = "https://repo.test/paper.pdf"
        record.add_pdf_candidate(url, "openalex")
        self.run_record(record, Client([Response(good_pdf(), url)]))
        self.assertEqual(record.download_status, "downloaded")
        self.assertEqual(record.identity_status, "verified")
        self.assertTrue(Path(record.local_pdf).is_file())
        self.assertTrue(any(item["stage"] == "file" and item["outcome"] == "downloaded" for item in record.attempts))

    def test_existing_wrong_pdf_is_preserved_not_bound(self):
        self.pdf_dir.mkdir()
        record = paper()
        path = self.pdf_dir / downloader.filename_for(record)
        body = pdf_bytes(["An unrelated publication title", "DOI: 10.1234/other"])
        path.write_bytes(body)
        self.run_record(record, Client([]))
        self.assertEqual(record.download_status, "manual_review")
        self.assertEqual(record.local_pdf, "")
        self.assertEqual(path.read_bytes(), body)

    def test_existing_valid_pdf_is_revalidated_without_network(self):
        self.pdf_dir.mkdir()
        record = paper()
        path = self.pdf_dir / downloader.filename_for(record)
        path.write_bytes(good_pdf())
        client = Client([])
        self.run_record(record, client)
        self.assertEqual(record.download_status, "already_downloaded")
        self.assertEqual(record.identity_status, "verified")
        self.assertEqual(client.calls, [])

    def test_changed_recorded_checksum_requires_review(self):
        self.pdf_dir.mkdir()
        record = paper()
        path = self.pdf_dir / downloader.filename_for(record)
        path.write_bytes(good_pdf())
        record.local_pdf, record.sha256 = str(path), "old-different-checksum"
        self.run_record(record, Client([]))
        self.assertEqual(record.download_status, "manual_review")
        self.assertEqual(record.local_pdf, "")
        self.assertIn("checksum changed", record.failure_reason)

    def test_unrelated_download_is_reviewed_then_next_candidate_can_succeed(self):
        record = paper()
        first, second = "https://repo.test/first.pdf", "https://other.test/second.pdf"
        record.add_pdf_candidate(first, "openalex")
        record.add_pdf_candidate(second, "unpaywall")
        client = Client([Response(pdf_bytes(["Unrelated research paper", "DOI: 10.1234/wrong"]), first), Response(good_pdf(), second)])
        self.run_record(record, client)
        self.assertEqual(record.download_status, "downloaded")
        self.assertEqual(record.download_url, second)
        self.assertEqual(len(record.extra["identity_review_files"]), 1)
        self.assertEqual(list(self.pdf_dir.glob("*.part")), [])

    def test_truncated_body_retries_once_then_tries_second_url(self):
        record = paper()
        first, second = "https://repo.test/first.pdf", "https://other.test/second.pdf"
        record.add_pdf_candidate(first, "openalex")
        record.add_pdf_candidate(second, "unpaywall")
        client = Client([Response(b"", first, IncompleteRead(b"x", 10)),
                         Response(b"", first, IncompleteRead(b"x", 10)), Response(good_pdf(), second)])
        self.run_record(record, client)
        self.assertEqual(record.download_status, "downloaded")
        self.assertEqual([call[0] for call in client.calls], [first, first, second])
        self.assertEqual(sum(item["outcome"] == "retry_body" for item in record.attempts), 1)

    def test_length_mismatch_cannot_be_accepted_as_repaired_pdf(self):
        record = paper()
        url = "https://repo.test/truncated.pdf"
        record.add_pdf_candidate(url, "openalex")
        body = good_pdf()
        client = Client([Response(body, url, length=len(body) + 100), Response(body, url, length=len(body) + 100)])
        self.run_record(record, client)
        self.assertEqual(record.download_status, "download_failed")
        self.assertEqual(record.local_pdf, "")
        self.assertEqual(len(client.calls), 2)

    def test_landing_fallback_tries_all_declared_urls(self):
        record = paper()
        landing = "https://repo.test/article"
        first, second = "https://repo.test/dead.pdf", "https://repo.test/live.pdf"
        record.add_pdf_candidate(landing, "unpaywall_landing", kind="landing")
        client = Client([HttpError("Not found", 404, first), Response(good_pdf(), second)])
        with patch.object(downloader, "resolve_landing_pdfs", return_value=[first, second]):
            self.run_record(record, client)
        self.assertEqual(record.download_status, "downloaded")
        self.assertEqual([call[0] for call in client.calls], [first, second])
        self.assertEqual(client.calls[1][1]["headers"]["Referer"], landing)

    def test_deferred_host_does_not_block_another_host(self):
        record = paper()
        urls = ["https://limited.test/a.pdf", "https://limited.test/b.pdf", "https://other.test/c.pdf"]
        for url in urls:
            record.add_pdf_candidate(url, "openalex")
        error = HttpError("Wait", 429, urls[0])
        error.retry_after = 120
        error.retry_at = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
        error.deferred = True
        client = Client([error, Response(good_pdf(), urls[2])])
        self.run_record(record, client)
        self.assertEqual(record.download_status, "downloaded")
        self.assertEqual([call[0] for call in client.calls], [urls[0], urls[2]])
        self.assertTrue(any(item.get("retry_at") == error.retry_at for item in record.attempts))

    def test_all_deferred_keeps_resume_time(self):
        record = paper()
        url = "https://limited.test/a.pdf"
        record.add_pdf_candidate(url, "openalex")
        error = HttpError("Wait", 429, url)
        error.retry_at = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
        error.deferred = True
        client = Client([error])
        self.run_record(record, client)
        self.assertEqual(record.download_status, "deferred")
        self.assertEqual(record.retry_at, error.retry_at)
        self.run_record(record, Client([]))
        self.assertEqual(record.download_status, "deferred")

    def test_bare_pmcid_does_not_bypass_qualified_fulltext_candidates(self):
        record = paper(pmcid="PMC123")
        client = Client([])
        with patch.object(downloader, "fetch_jats_url") as fetch:
            self.run_record(record, client)
        self.assertEqual(record.download_status, "no_oa_version")
        fetch.assert_not_called()
        self.assertEqual(client.calls, [])

    def test_jats_reader_is_separate_and_respects_phase_flag(self):
        record = paper(pmcid="PMC123")
        record.add_pdf_candidate("https://pmc.test/PMC123.xml", "pmc_oa_cloud", "TDM", "acceptedVersion",
                                 kind="jats", pmcid="PMC123", oa_route="author_manuscript_tdm")
        with patch.object(downloader, "fetch_jats_url", return_value=f"# {TITLE}\nFull text") as fetch:
            self.run_record(record, Client([]), allow_fulltext_fallback=False)
            fetch.assert_not_called()
            self.run_record(record, Client([]), allow_fulltext_fallback=True)
        self.assertEqual(record.download_status, "fulltext_only")
        self.assertTrue(Path(record.local_fulltext).is_file())
        self.assertEqual(record.local_pdf, "")
        self.assertEqual(record.sha256, "")

    def test_unique_verified_target_preserves_unattempted_records(self):
        records = [paper(doi=f"10.1234/{index}") for index in range(4)]
        hashes = iter(["same", "same", "other"])
        def downloaded(record, *args, **kwargs):
            record.download_status, record.identity_status = "downloaded", "verified"
            record.local_pdf, record.sha256 = f"synthetic-{record.identity_digest}.pdf", next(hashes)
            return record
        with patch.object(downloader, "download_record", side_effect=downloaded):
            successes, failures = downloader.download_until_target(records, self.pdf_dir, 2, 100, 30)
        self.assertEqual(len(successes), 2)
        self.assertEqual({record.sha256 for record in successes}, {"same", "other"})
        self.assertEqual(failures[0].download_status, "duplicate_pdf")
        self.assertEqual(failures[0].duplicate_of, records[0].local_pdf)
        self.assertEqual(records[3].download_status, "pending")


if __name__ == "__main__":
    unittest.main()
