"""Offline transport/resolver regression tests; no external requests or real papers."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import format_datetime
from http.client import IncompleteRead
from io import BytesIO
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lit_harvest.http import HttpClient, HttpError, ValidatingRedirectHandler, is_public_https_url, redact_url
from lit_harvest import http
from lit_harvest.fulltext import (FullTextError, extract_pdf_urls, resolve_landing_pdfs,
                                  fetch_jats_url, jats_to_markdown)
from lit_harvest.pmc import PmcError, resolve_pmc_candidates, BUCKET_HOST


class Response:
    def __init__(self, body=b"{}", url=None, fail=None):
        self.body, self.url, self.fail = body, url, fail
        self.headers = {}
        self.closed = False
    def read(self, count=-1):
        if self.fail:
            raise self.fail
        return self.body if count < 0 else self.body[:count]
    read1 = read
    def close(self):
        self.closed = True
    def __enter__(self):
        return self
    def __exit__(self, *_):
        self.close()


def client_with(*items, **kwargs):
    client = HttpClient(validate_redirects=lambda _url: True, **kwargs)
    client.opener = Mock()
    client.opener.open.side_effect = items
    return client


class HttpRegressionTests(unittest.TestCase):
    def setUp(self):
        http._HOST_COOLDOWNS.clear()

    def tearDown(self):
        http._HOST_COOLDOWNS.clear()

    @patch("lit_harvest.http.time.sleep")
    def test_get_body_failure_retries_and_closes(self, sleep):
        first = Response(fail=IncompleteRead(b"partial"))
        client = client_with(first, Response(b'{"ok":true}'))
        self.assertEqual(client.get_json("https://example.org"), {"ok": True})
        self.assertTrue(first.closed)
        self.assertEqual(client.opener.open.call_count, 2)
        sleep.assert_called_once_with(1)

    @patch("lit_harvest.http.time.sleep")
    def test_body_failure_retry_budget_is_not_nested(self, sleep):
        client = client_with(*(Response(fail=TimeoutError("secret")) for _ in range(4)), max_retries=2)
        with self.assertRaises(HttpError) as caught:
            client.get_json("https://example.org?api_key=secret")
        self.assertEqual(client.opener.open.call_count, 3)
        self.assertNotIn("secret", str(caught.exception))

    @patch("lit_harvest.http.time.sleep")
    def test_non_idempotent_post_never_retried(self, sleep):
        for error in (TimeoutError("uncertain"), HTTPError("https://example.org", 503, "busy", {}, BytesIO())):
            with self.subTest(error=type(error).__name__):
                client = client_with(error, Response())
                with self.assertRaises(HttpError):
                    client.post_json("https://example.org", {"write": True})
                self.assertEqual(client.opener.open.call_count, 1)
        client = client_with(Response(fail=IncompleteRead(b"partial")), Response())
        with self.assertRaises(HttpError):
            client.post_json("https://example.org", {})
        self.assertEqual(client.opener.open.call_count, 1)
        sleep.assert_not_called()

    @patch("lit_harvest.http.time.time", return_value=1800000000)
    @patch("lit_harvest.http.time.sleep")
    def test_long_retry_after_is_deferred_without_early_retry(self, sleep, _clock):
        client = client_with(HTTPError("https://example.org", 429, "busy", {"Retry-After": "3600"}, BytesIO()))
        with self.assertRaises(HttpError) as caught:
            client.get_json("https://example.org?token=private")
        error = caught.exception
        self.assertTrue(error.deferred)
        self.assertEqual(error.retry_after, 3600)
        self.assertEqual(datetime.fromisoformat(error.retry_at).timestamp(), 1800003600)
        self.assertEqual(client.opener.open.call_count, 1)
        self.assertNotIn("private", str(error))
        sleep.assert_not_called()

    @patch("lit_harvest.http.time.time", return_value=1800000000)
    @patch("lit_harvest.http.time.sleep")
    def test_http_date_retry_after_waits_full_short_delay(self, sleep, _clock):
        date = format_datetime(datetime.fromtimestamp(1800000007, timezone.utc), usegmt=True)
        client = client_with(HTTPError("https://example.org", 503, "busy", {"Retry-After": date}, BytesIO()), Response())
        self.assertEqual(client.get_json("https://example.org"), {})
        sleep.assert_called_once_with(7)

    @patch("lit_harvest.http.time.sleep")
    def test_exhausted_retry_retains_server_deadline(self, sleep):
        client = client_with(HTTPError("https://example.org", 429, "busy", {"Retry-After": "3"}, BytesIO()), max_retries=0)
        with self.assertRaises(HttpError) as caught:
            client.request("https://example.org")
        self.assertTrue(caught.exception.deferred)
        self.assertEqual(caught.exception.retry_after, 3)
        sleep.assert_not_called()

    def test_stream_read_is_structured_but_not_replayed(self):
        client = client_with(Response(fail=IncompleteRead(b"x")))
        with client.request("https://example.org") as response:
            with self.assertRaises(HttpError) as caught:
                response.read1(42)
        self.assertEqual(caught.exception.code, "body_read_failed")
        self.assertEqual(client.opener.open.call_count, 1)

    def test_http_error_body_is_not_read_or_leaked(self):
        body = Mock()
        body.read.side_effect = IncompleteRead(b"API_KEY=secret")
        client = client_with(HTTPError("https://example.org", 403, "secret", {}, body))
        with self.assertRaises(HttpError) as caught:
            client.request("https://example.org?api_key=secret")
        body.read.assert_not_called()
        self.assertNotIn("secret", str(caught.exception))

    def test_invalid_json_not_retried(self):
        client = client_with(Response(b"not json"), Response())
        with self.assertRaises(HttpError) as caught:
            client.get_json("https://example.org")
        self.assertEqual(caught.exception.code, "invalid_json")
        self.assertEqual(client.opener.open.call_count, 1)

    def test_empty_get_json_rejected_but_empty_post_allowed(self):
        with self.assertRaises(HttpError):
            client_with(Response(b"")).get_json("https://example.org")
        self.assertEqual(client_with(Response(b"")).post_json("https://example.org", {}), (None, {}))

    @patch("lit_harvest.http.socket.getaddrinfo")
    def test_url_validation_fails_closed(self, dns):
        dns.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
        self.assertTrue(is_public_https_url("https://example.org/a"))
        for url in ("http://example.org", "https://user:pass@example.org", "https://example.org:bad", "https://localhost."):
            self.assertFalse(is_public_https_url(url))
        dns.return_value = []
        self.assertFalse(is_public_https_url("https://example.org"))
        dns.return_value = [(2, 1, 6, "", ("127.0.0.1", 443))]
        self.assertFalse(is_public_https_url("https://example.org"))

    def test_redaction_and_cross_origin_header_filter(self):
        url = redact_url("https://alice:pass@example.org/a?api-key=secret&x=ok#token")
        for private in ("alice", "pass", "secret", "#token"):
            self.assertNotIn(private, url)
        handler = ValidatingRedirectHandler(lambda _url: True)
        request = Request("https://a.example/a", headers={"Authorization": "secret", "Cookie": "secret", "Accept": "text/html"})
        redirected = handler.redirect_request(request, None, 302, "found", {}, "https://b.example/b")
        self.assertNotIn("secret", str(redirected.header_items()))
        with self.assertRaises(HttpError):
            ValidatingRedirectHandler(lambda _url: False).redirect_request(request, None, 302, "found", {}, "http://localhost/a")


def jats(pmcid="PMC123", body=None):
    text = body if body is not None else "A synthetic body paragraph for transport testing. " * 20
    return (f'<article><front><article-meta><article-id pub-id-type="pmc">{pmcid}</article-id>'
            '<title-group><article-title>Synthetic</article-title></title-group></article-meta></front>'
            f'<body><sec><title>Methods</title><p>{text}</p></sec></body></article>').encode()


@patch("lit_harvest.fulltext.is_public_https_url", side_effect=lambda url: url.startswith("https://example.org"))
class FulltextRegressionTests(unittest.TestCase):
    def test_all_declared_candidates_dedup_no_path_guessing(self, _public):
        html = ('<meta name="citation_pdf_url" content="/first.pdf#p1">'
                '<meta name="citation_pdf_url" content="/first.pdf#p2">'
                '<link rel="alternate" type="application/pdf" href="/second.pdf">'
                '<meta name="citation_pdf_url" content="http://127.0.0.1/private.pdf">'
                '<a href="/guessed.pdf">PDF</a>')
        self.assertEqual(extract_pdf_urls(html, "https://example.org/article"),
                         ["https://example.org/first.pdf", "https://example.org/second.pdf"])

    def test_landing_body_error_and_deferred_survive(self, _public):
        client = Mock()
        client.request.return_value = Response(fail=TimeoutError("secret"))
        with self.assertRaises(HttpError):
            resolve_landing_pdfs(client, "https://example.org/a")
        error = HttpError("deferred", 429, retry_after=500, retry_at="future", deferred=True)
        client.request.side_effect = error
        with self.assertRaises(HttpError) as caught:
            resolve_landing_pdfs(client, "https://example.org/a")
        self.assertIs(caught.exception, error)

    def test_jats_identity_body_and_namespace(self, _public):
        self.assertIn("Methods", jats_to_markdown(jats(), "PMC123"))
        self.assertIn("Methods", jats_to_markdown(jats("123"), "PMC123"))
        for raw in (jats("PMC999"), jats(""), jats(body="short")):
            with self.assertRaises(FullTextError):
                jats_to_markdown(raw, "PMC123")
        namespaced = jats().replace(b"<article>", b'<article xmlns="urn:jats">')
        self.assertIn("Methods", jats_to_markdown(namespaced, "PMC123"))
        fake_html = b"<html><body>" + b"x" * 1000 + b"</body></html>"
        with self.assertRaises(FullTextError):
            jats_to_markdown(fake_html)

    def test_fetch_verified_url(self, _public):
        client = Mock()
        client.request.return_value = Response(jats(), "https://example.org/a.xml")
        self.assertIn("Synthetic", fetch_jats_url(client, "https://example.org/a.xml", "PMC123"))

    def test_jats_body_timeout_is_structured(self, _public):
        client = Mock()
        client.request.return_value = Response(fail=IncompleteRead(b"xml"))
        with self.assertRaises(HttpError):
            fetch_jats_url(client, "https://example.org/a.xml", "PMC123")


LISTING = f"https://{BUCKET_HOST}/?list-type=2&prefix=PMC123.&delimiter=%2F"


def listing(*versions, next_token=""):
    prefixes = "".join(f"<CommonPrefixes><Prefix>PMC123.{version}/</Prefix></CommonPrefixes>" for version in versions)
    return (f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f'<Prefix>PMC123.</Prefix>{prefixes}<IsTruncated>{str(bool(next_token)).lower()}</IsTruncated>'
            f'<NextContinuationToken>{next_token}</NextContinuationToken></ListBucketResult>').encode()


def metadata(version=1, **overrides):
    result = {"pmcid": "PMC123", "version": version, "is_retracted": False, "is_pmc_openaccess": True,
              "is_manuscript": False, "license_code": "CC BY",
              "pdf_url": f"s3://pmc-oa-opendata/PMC123.{version}/PMC123.{version}.pdf",
              "xml_url": f"s3://pmc-oa-opendata/PMC123.{version}/PMC123.{version}.xml"}
    return {**result, **overrides}


def pmc_client(pages, versions):
    client = Mock()
    client.request.side_effect = [Response(page) for page in pages]
    client.get_json.side_effect = versions
    return client


@patch("lit_harvest.pmc.is_public_https_url", return_value=True)
class PmcRegressionTests(unittest.TestCase):
    def test_all_versions_and_formats_not_only_max(self, _public):
        client = pmc_client([listing(1, 2)], [metadata(1), metadata(2, is_manuscript=True)])
        candidates = resolve_pmc_candidates(client, LISTING)
        self.assertEqual(len(candidates), 4)
        self.assertEqual({c["article_version"] for c in candidates}, {1, 2})
        self.assertEqual([c["version"] for c in candidates if c["article_version"] == 2], ["acceptedVersion"] * 2)

    def test_tdm_is_only_manuscript_jats_even_if_pdf_declared(self, _public):
        client = pmc_client([listing(1)], [metadata(is_pmc_openaccess=False, is_manuscript=True, license_code="TDM")])
        candidates = resolve_pmc_candidates(client, LISTING)
        self.assertEqual([c["kind"] for c in candidates], ["jats"])
        self.assertEqual(candidates[0]["oa_route"], "author_manuscript_tdm")

    def test_missing_or_false_eligibility_rejected(self, _public):
        for updates in ({"is_retracted": True}, {"is_retracted": None}, {"license_code": None},
                        {"pmcid": "PMC999"}, {"version": None}, {"is_pmc_openaccess": "false"},
                        {"is_pmc_openaccess": False, "is_manuscript": False, "license_code": "TDM"}):
            with self.subTest(updates=updates):
                with self.assertRaises(PmcError):
                    resolve_pmc_candidates(pmc_client([listing(1)], [metadata(**updates)]), LISTING)

    def test_declared_object_identity_and_host_required(self, _public):
        for path in ("s3://another/PMC123.1/PMC123.1.pdf", "s3://pmc-oa-opendata/PMC999.1/PMC999.1.pdf",
                     "https://example.org/a.pdf", "s3://pmc-oa-opendata/PMC123.1/../secret.pdf"):
            with self.subTest(path=path):
                with self.assertRaises(PmcError):
                    resolve_pmc_candidates(pmc_client([listing(1)], [metadata(pdf_url=path, xml_url=None)]), LISTING)

    def test_oa_without_pdf_keeps_jats(self, _public):
        values = resolve_pmc_candidates(pmc_client([listing(1)], [metadata(pdf_url=None)]), LISTING)
        self.assertEqual([c["kind"] for c in values], ["jats"])

    def test_one_bad_version_preserves_others_and_warning(self, _public):
        values = resolve_pmc_candidates(pmc_client([listing(1, 2)], [metadata(1, is_retracted=True), metadata(2)]), LISTING)
        self.assertEqual(len(values), 2)
        self.assertIn("resolver_warnings", values[0])

    def test_pagination_and_repeated_token(self, _public):
        values = resolve_pmc_candidates(pmc_client([listing(1, next_token="a"), listing(2)], [metadata(1), metadata(2)]), LISTING)
        self.assertEqual(len(values), 4)
        with self.assertRaises(PmcError):
            resolve_pmc_candidates(pmc_client([listing(1, next_token="a"), listing(2, next_token="a")], []), LISTING)

    def test_wrong_listing_identifier_and_body_failure(self, _public):
        with self.assertRaises(PmcError):
            resolve_pmc_candidates(pmc_client([listing(1).replace(b"PMC123.1", b"PMC999.1")], []), LISTING)
        client = Mock()
        client.request.return_value = Response(fail=IncompleteRead(b"partial"))
        with self.assertRaises(HttpError):
            resolve_pmc_candidates(client, LISTING)

    def test_unknown_metadata_is_not_silent_empty(self, _public):
        for data in (None, [], {"pmcid": "PMC123"}):
            with self.subTest(data=data):
                with self.assertRaises(PmcError):
                    resolve_pmc_candidates(pmc_client([listing(1)], [data]), LISTING)

    def test_no_candidate_deferred_error_preserved(self, _public):
        error = HttpError("deferred", 429, retry_after=50, retry_at="future", deferred=True)
        with self.assertRaises(HttpError) as caught:
            resolve_pmc_candidates(pmc_client([listing(1)], [error]), LISTING)
        self.assertIs(error, caught.exception)


if __name__ == "__main__":
    unittest.main()
