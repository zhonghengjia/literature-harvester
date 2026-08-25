from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lit_harvest.downloader import (  # noqa: E402
    DownloadError,
    _parse_pmc_cloud_listing,
    _resolve_pmc_oa_cloud,
    _stream_pdf_response,
)


LISTING = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <CommonPrefixes><Prefix>PMC5053142.1/</Prefix></CommonPrefixes>
  <CommonPrefixes><Prefix>PMC5053142.2/</Prefix></CommonPrefixes>
</ListBucketResult>"""


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, limit=-1):
        return self.body if limit < 0 else self.body[:limit]


class _CloudClient:
    def request(self, _url, headers=None):
        return _Response(LISTING)

    def get_json(self, url):
        assert url.endswith("/metadata/PMC5053142.2.json")
        return {
            "pmcid": "PMC5053142",
            "version": 2,
            "is_pmc_openaccess": True,
            "is_retracted": False,
            "license_code": "CC BY",
            "pdf_url": "s3://pmc-oa-opendata/PMC5053142.2/PMC5053142.2.pdf?md5=abc",
        }


class DownloaderTests(unittest.TestCase):
    def test_pmc_cloud_listing_selects_latest_version(self) -> None:
        pmcid, version = _parse_pmc_cloud_listing(LISTING)
        self.assertEqual(pmcid, "PMC5053142")
        self.assertEqual(version, 2)
        mixed = LISTING.replace(b"PMC5053142.2", b"PMC9999999.2")
        with self.assertRaises(DownloadError):
            _parse_pmc_cloud_listing(mixed)

    @patch("lit_harvest.downloader.is_public_https_url", return_value=True)
    def test_pmc_cloud_resolver_requires_metadata_license(self, _public) -> None:
        url, license_value = _resolve_pmc_oa_cloud(_CloudClient(), "https://example.org/list")
        self.assertEqual(url, "https://pmc-oa-opendata.s3.amazonaws.com/PMC5053142.2/PMC5053142.2.pdf")
        self.assertEqual(license_value, "CC BY")

    @patch("lit_harvest.downloader.time.monotonic", side_effect=[0.0, 0.0, 100.0])
    def test_stream_has_whole_body_deadline(self, _clock) -> None:
        response = type("SlowResponse", (), {"read1": lambda self, _size: b"%PDF-1.7"})()
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(DownloadError, "exceeded 90 seconds") as raised:
                _stream_pdf_response(response, Path(temp_dir) / "paper.pdf.part", 1024, 1, 90)
        self.assertEqual(raised.exception.code, "download_timeout")


if __name__ == "__main__":
    unittest.main()
