from __future__ import annotations

from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if not SCRIPTS.is_dir():
    SCRIPTS = Path(__file__).resolve().parents[2] / "literature-harvester" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lit_harvest.downloader import (
    DownloadError, _stream_pdf_response, upgrade_http_candidate,
)
from lit_harvest.pmc import PmcError, resolve_pmc_candidates

LISTING_URL = (
    "https://pmc-oa-opendata.s3.amazonaws.com/"
    "?list-type=2&prefix=PMC5053142.&delimiter=%2F"
)
LISTING = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Prefix>PMC5053142.</Prefix>
  <IsTruncated>false</IsTruncated>
  <CommonPrefixes><Prefix>PMC5053142.1/</Prefix></CommonPrefixes>
  <CommonPrefixes><Prefix>PMC5053142.2/</Prefix></CommonPrefixes>
</ListBucketResult>"""
CHECKSUM = "a" * 32


class _Response:
    def __init__(self, body: bytes, url=LISTING_URL) -> None:
        self.body = body
        self.url = url
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, limit=-1):
        if limit < 0:
            limit = len(self.body)
        chunk, self.body = self.body[:limit], self.body[limit:]
        return chunk


class _CloudClient:
    def __init__(self, listing=LISTING, license_value="CC BY"):
        self.listing = listing
        self.license_value = license_value
        self.metadata_calls = []

    def request(self, url, headers=None):
        if url != LISTING_URL:
            raise AssertionError(f"unexpected listing URL: {url}")
        return _Response(self.listing, url)

    def get_json(self, url, params=None, headers=None):
        self.metadata_calls.append(url)
        expected = {
            f"https://pmc-oa-opendata.s3.amazonaws.com/metadata/PMC5053142.{v}.json": v
            for v in (1, 2)
        }
        if url not in expected:
            raise AssertionError(f"unexpected metadata URL: {url}")
        version = expected[url]
        return {
            "pmcid": "PMC5053142", "version": version,
            "is_pmc_openaccess": True, "is_retracted": False,
            "is_manuscript": False, "license_code": self.license_value,
            "pdf_url": (
                f"s3://pmc-oa-opendata/PMC5053142.{version}/"
                f"PMC5053142.{version}.pdf?md5={CHECKSUM}"
            ),
        }


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        dns = patch("socket.getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ])
        connect = patch("socket.create_connection", side_effect=AssertionError("network forbidden"))
        opener = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network forbidden"))
        for guard in (dns, connect, opener):
            guard.start()
            self.addCleanup(guard.stop)

    def test_http_candidate_upgrade_changes_only_transport(self) -> None:
        self.assertEqual(
            upgrade_http_candidate("http://repo.example/paper.pdf?download=1"),
            "https://repo.example/paper.pdf?download=1",
        )
        self.assertEqual(
            upgrade_http_candidate("https://repo.example/paper.pdf"),
            "https://repo.example/paper.pdf",
        )
        self.assertEqual(
            upgrade_http_candidate("http://user:secret@repo.example/paper.pdf"),
            "http://user:secret@repo.example/paper.pdf",
        )

    def test_pmc_cloud_listing_preserves_all_versions_and_rejects_mixed_ids(self) -> None:
        # Approved behavior now retains every eligible version, including the
        # latest; the former max-version-only assertion would lose legal routes.
        client = _CloudClient()
        candidates = resolve_pmc_candidates(client, LISTING_URL)
        self.assertEqual([item["article_version"] for item in candidates], [1, 2])
        self.assertEqual({item["pmcid"] for item in candidates}, {"PMC5053142"})
        self.assertEqual([item["kind"] for item in candidates], ["pdf", "pdf"])
        self.assertEqual(len(client.metadata_calls), 2)
        mixed = LISTING.replace(b"PMC5053142.2", b"PMC9999999.2")
        bad_client = _CloudClient(listing=mixed)
        with self.assertRaises(PmcError) as raised:
            resolve_pmc_candidates(bad_client, LISTING_URL)
        self.assertEqual(raised.exception.code, "identity_mismatch")
        self.assertEqual(bad_client.metadata_calls, [])

    def test_pmc_cloud_resolver_requires_metadata_license(self) -> None:
        candidates = resolve_pmc_candidates(_CloudClient(), LISTING_URL)
        latest = next(item for item in candidates if item["article_version"] == 2)
        self.assertEqual(
            latest["url"],
            "https://pmc-oa-opendata.s3.amazonaws.com/PMC5053142.2/PMC5053142.2.pdf",
        )
        self.assertEqual(latest["license"], "CC BY")
        self.assertEqual(latest["oa_route"], "open_access")
        self.assertEqual(latest["expected_md5"], CHECKSUM)
        for missing in ("", None):
            with self.subTest(license=missing), self.assertRaises(PmcError) as raised:
                resolve_pmc_candidates(_CloudClient(license_value=missing), LISTING_URL)
            self.assertEqual(raised.exception.code, "license_unknown")

    @patch("lit_harvest.downloader.time.monotonic", side_effect=[0.0, 0.0, 100.0])
    def test_stream_has_whole_body_deadline(self, _clock) -> None:
        response = type("SlowResponse", (), {"read1": lambda self, _size: b"%PDF-1.7"})()
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(DownloadError, "exceeded 90 seconds") as raised:
                _stream_pdf_response(response, Path(temp_dir) / "paper.pdf.part", 1024, 1, 90)
        self.assertEqual(raised.exception.code, "download_timeout")


if __name__ == "__main__":
    unittest.main()
