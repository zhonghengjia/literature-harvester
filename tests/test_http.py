from __future__ import annotations

from pathlib import Path
import sys
import unittest
from urllib.request import Request


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lit_harvest.http import HttpError, ValidatingRedirectHandler, redact_url  # noqa: E402


class HttpTests(unittest.TestCase):
    def test_every_redirect_is_revalidated(self) -> None:
        handler = ValidatingRedirectHandler(lambda _url: False)
        with self.assertRaises(HttpError):
            handler.redirect_request(
                Request("https://example.org/start"),
                None,
                302,
                "Found",
                {},
                "http://127.0.0.1/private.pdf",
            )

    def test_sensitive_query_values_are_redacted(self) -> None:
        redacted = redact_url("https://example.org/?email=person@example.org&api_key=secret&x=ok")
        self.assertNotIn("person@example.org", redacted)
        self.assertNotIn("secret", redacted)
        self.assertIn("x=ok", redacted)


if __name__ == "__main__":
    unittest.main()
