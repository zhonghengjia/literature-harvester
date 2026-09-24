from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reportlab.pdfgen import canvas

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lit_harvest import browser
from lit_harvest.downloader import filename_for, sha256_file
from lit_harvest.models import PaperRecord


TITLE = "Synthetic clinical outcomes after a randomized intervention"
DOI = "10.1234/synthetic"
SOURCE = "https://publisher.example/article/synthetic.pdf"


def paper(**kwargs):
    return PaperRecord(**{"title": TITLE, "doi": DOI, "authors": ["Jane Doe"], **kwargs})


def pdf_bytes(lines=None):
    buffer = BytesIO()
    writer = canvas.Canvas(buffer)
    for index, line in enumerate(lines if lines is not None else [TITLE, "Jane Doe", f"DOI: {DOI}"]):
        writer.drawString(35, 790 - index * 20, line)
    writer.showPage()
    writer.save()
    return buffer.getvalue()


def basis(**kwargs):
    return {"kind": "oa", "source_kind": "publisher", "evidence_url": "https://publisher.example/article",
            "evidence_note": "The publisher article page exposes this PDF and an explicit CC BY license.",
            "confirmed_by": "user", "original_provider_confirmed": True,
            "license_or_oa_statement": "CC BY 4.0", **kwargs}


class BrowserHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "user-download.pdf"
        self.source.write_bytes(pdf_bytes())
        self.pdf_dir = self.root / "run" / "pdfs"
        self.record = paper()

    def ingest(self, record=None, evidence=None, url=SOURCE, records=None):
        record = record if record is not None else self.record
        return browser.ingest_local_pdf(record, self.source, self.pdf_dir, url,
                                        basis() if evidence is None else evidence,
                                        [record] if records is None else records)

    def test_status_honestly_reports_no_automatic_plugin_control(self):
        status = browser.browser_status()
        self.assertFalse(status["automatic_extension_control"])
        self.assertFalse(status["extension_invoked"])
        self.assertEqual(status["mode"], "manual_handoff")

    def test_queue_is_read_only_and_excludes_accepted_and_duplicate_records(self):
        ready = paper(download_status="downloaded", identity_status="verified")
        duplicate = paper(download_status="duplicate_pdf")
        failed = paper(download_status="anti_bot", pmid="12345", pmcid="PMC12345")
        before = failed.to_dict()
        queue = browser.queue_records([ready, duplicate, failed])
        self.assertEqual(queue["count"], 1)
        self.assertEqual(queue["records"][0]["record_id"], "doi:" + DOI)
        self.assertEqual({row["kind"] for row in queue["records"][0]["routes"]},
                         {"doi_landing", "pubmed", "pmc_article"})
        self.assertEqual(failed.to_dict(), before)

    def test_queue_never_adds_shadow_or_unqualified_candidates(self):
        self.record.add_pdf_candidate(url="https://sci-hub.se/file.pdf", source="unpaywall", license="cc-by")
        self.record.add_pdf_candidate(url="https://unknown.example/a.pdf", source="crossref")
        self.record.add_pdf_candidate(url=SOURCE, source="unpaywall", license="cc-by")
        routes = browser.queue_records([self.record])["records"][0]["routes"]
        self.assertEqual(len(routes), 2)
        self.assertIn(SOURCE, [row["url"] for row in routes])

    def test_selector_requires_unique_strong_identifier(self):
        self.assertIs(browser.select_record([self.record], "doi:" + DOI), self.record)
        for rows, query in [([self.record], "title:" + self.record.normalized_title),
                            ([self.record, paper()], "doi:" + DOI), ([self.record], "pmid:9999")]:
            with self.assertRaises(ValueError):
                browser.select_record(rows, query)

    def test_good_pdf_is_copied_with_provenance_source_unchanged(self):
        original = self.source.read_bytes()
        result = self.ingest()
        self.assertEqual(result["outcome"], "downloaded")
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(Path(self.record.local_pdf).read_bytes(), original)
        self.assertEqual(self.record.sha256, sha256_file(self.source))
        self.assertEqual(self.record.identity_status, "verified")
        self.assertEqual(self.record.retrieval_type, "browser_oa_pdf")
        self.assertIn("user_attested", result["access_evidence"]["verification_level"])
        self.assertFalse(list(self.pdf_dir.glob("*.part")))

    def test_title_only_record_is_held_before_copy(self):
        result = self.ingest(record=paper(doi=""))
        self.assertEqual(result["outcome"], "manual_review")
        self.assertFalse(self.pdf_dir.exists())

    def test_bare_oa_label_and_record_is_oa_do_not_establish_access(self):
        self.record.is_oa = True
        for evidence in ["oa", "institutional", {}, basis(original_provider_confirmed=False),
                         basis(confirmed_by="plugin_verified"), basis(license_or_oa_statement="")]:
            self.assertEqual(self.ingest(evidence=evidence)["outcome"], "manual_review")
        self.assertFalse(self.pdf_dir.exists())

    def test_institutional_requires_explicit_entitlement(self):
        evidence = basis(kind="institutional", source_kind="institutional_library")
        self.assertEqual(self.ingest(evidence=evidence)["outcome"], "manual_review")
        evidence["entitlement_confirmed"] = True
        self.assertEqual(self.ingest(evidence=evidence)["outcome"], "downloaded")
        self.assertEqual(self.record.retrieval_type, "browser_authorized_pdf")
        self.assertIsNone(self.record.is_oa)

    def test_known_shadow_domains_rejected_even_with_user_claim(self):
        for url in ["https://sci-hub.se/paper", "https://libgen.is/file", "https://annas-archive.org/item"]:
            self.assertEqual(self.ingest(url=url)["outcome"], "rejected_source")
        self.assertEqual(self.ingest(evidence=basis(evidence_url="https://libgen.li/record"))["outcome"], "rejected_source")
        self.assertFalse(self.pdf_dir.exists())

    def test_changed_unknown_host_is_not_access_evidence(self):
        self.assertEqual(self.ingest(url="https://new-mirror.example/file.pdf", evidence="oa")["outcome"], "manual_review")
        self.assertEqual(self.ingest(url="https://new-mirror.example/file.pdf",
                                    evidence=basis(confirmed_by="plugin_verified"))["outcome"], "manual_review")

    def test_credential_local_and_non_https_urls_are_held(self):
        for url in ["https://user:secret@publisher.example/file.pdf", "http://publisher.example/file.pdf",
                    "https://127.0.0.1/file", "https://localhost/file", "file:///tmp/a.pdf",
                    "https://publisher.example:7777/file"]:
            self.assertEqual(self.ingest(url=url)["outcome"], "manual_review")
        self.assertNotIn("secret", json.dumps(self.record.attempts))

    def test_query_and_fragment_are_not_persisted(self):
        result = self.ingest(url=SOURCE + "?token=verysecret#password",
                             evidence=basis(evidence_url="https://publisher.example/article?auth=sensitive"))
        self.assertEqual(result["outcome"], "downloaded")
        serialized = json.dumps(self.record.to_dict())
        self.assertNotIn("verysecret", serialized)
        self.assertNotIn("sensitive", serialized)

    def test_invalid_or_mismatched_pdf_never_becomes_accepted(self):
        for payload in [b"<html>Login required</html>", pdf_bytes(["Supplementary information", TITLE, DOI]),
                        pdf_bytes(["A completely different synthetic article", "John Smith", "10.1234/other"])]:
            self.source.write_bytes(payload)
            record = paper()
            result = self.ingest(record=record)
            self.assertNotEqual(result["outcome"], "downloaded")
            self.assertFalse(record.local_pdf)
            self.assertFalse(list(self.pdf_dir.glob("*.pdf")))
            self.assertFalse(list(self.pdf_dir.glob("*.part")))

    def test_missing_pdf_and_oversize_pdf_fail_before_copy(self):
        self.source.unlink()
        self.assertEqual(self.ingest()["outcome"], "download_failed")
        self.source.write_bytes(pdf_bytes())
        with patch.object(browser, "MAX_LOCAL_BYTES", 10):
            self.assertEqual(self.ingest()["outcome"], "too_large")
        self.assertFalse(self.pdf_dir.exists())

    def test_existing_destination_not_overwritten(self):
        self.pdf_dir.mkdir(parents=True)
        collision = self.pdf_dir / filename_for(self.record)
        collision.write_bytes(b"protected existing artifact")
        result = self.ingest()
        self.assertEqual(result["outcome"], "downloaded")
        self.assertEqual(collision.read_bytes(), b"protected existing artifact")
        self.assertNotEqual(Path(self.record.local_pdf), collision)

    def test_same_record_repeat_does_not_copy_twice(self):
        self.assertEqual(self.ingest()["outcome"], "downloaded")
        self.assertEqual(self.ingest()["outcome"], "already_downloaded")
        self.assertEqual(len(list(self.pdf_dir.glob("*.pdf"))), 1)

    def test_batch_sha_deduplication_does_not_copy_twice(self):
        self.ingest()
        second = paper(pmid="12345")
        result = self.ingest(record=second, records=[self.record, second])
        self.assertEqual(result["outcome"], "duplicate_pdf")
        self.assertEqual(second.download_status, "duplicate_pdf")
        self.assertEqual(second.duplicate_of, self.record.local_pdf)
        self.assertFalse(second.local_pdf)
        self.assertEqual(len(list(self.pdf_dir.glob("*.pdf"))), 1)

    def test_failed_later_attempt_preserves_accepted_pdf(self):
        self.ingest()
        saved = self.record.local_pdf
        self.source.write_bytes(b"invalid new download")
        result = self.ingest()
        self.assertEqual(result["outcome"], "not_pdf")
        self.assertEqual(self.record.download_status, "downloaded")
        self.assertEqual(self.record.identity_status, "verified")
        self.assertEqual(self.record.local_pdf, saved)
        self.assertEqual(len(self.record.attempts), 2)

    def test_ingest_performs_no_network_or_zotero_requests(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden")), \
             patch("socket.create_connection", side_effect=AssertionError("Network forbidden")):
            self.assertEqual(self.ingest()["outcome"], "downloaded")


if __name__ == "__main__":
    unittest.main()
