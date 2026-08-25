from __future__ import annotations

import ipaddress
import json
import socket
import ssl
import time
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


class HttpError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


def redact_url(url: str) -> str:
    parsed = urlparse(url)
    sensitive = {"api_key", "apikey", "key", "email", "mailto", "token", "access_token"}
    query = urlencode(
        [(name, "REDACTED" if name.casefold() in sensitive else value) for name, value in parse_qsl(parsed.query)]
    )
    return urlunparse(parsed._replace(query=query))


def is_public_https_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.casefold()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return False
    try:
        addresses = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            return False
    return True


class ValidatingRedirectHandler(HTTPRedirectHandler):
    def __init__(self, validator: Callable[[str], bool]) -> None:
        super().__init__()
        self.validator = validator

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Request:
        absolute = urljoin(req.full_url, newurl)
        if not self.validator(absolute):
            raise HttpError(
                f"Unsafe redirect rejected: {redact_url(absolute)}", status=code, url=redact_url(absolute)
            )
        return super().redirect_request(req, fp, code, msg, headers, absolute)


def _retry_delay(headers: Any, attempt: int) -> float:
    value = headers.get("Retry-After") if headers else None
    if value:
        try:
            return min(float(value), 30.0)
        except ValueError:
            try:
                delay = parsedate_to_datetime(value).timestamp() - time.time()
                return min(max(delay, 0.0), 30.0)
            except (TypeError, ValueError, OverflowError):
                pass
    return min(2**attempt, 8)


class HttpClient:
    def __init__(
        self,
        timeout: float = 30,
        user_agent: str = "LiteratureHarvester/0.1 (+local-personal-research)",
        max_retries: int = 2,
        validate_redirects: Callable[[str], bool] | None = None,
    ) -> None:
        self.timeout = timeout
        self.user_agent = user_agent
        self.max_retries = max_retries
        try:
            import certifi  # type: ignore

            ca_file = certifi.where()
        except ImportError:
            try:
                from pip._vendor import certifi  # type: ignore

                ca_file = certifi.where()
            except ImportError:
                ca_file = None
        context = ssl.create_default_context(cafile=ca_file)
        if hasattr(ssl, "enum_certificates"):
            try:
                for certificate, encoding, trust in ssl.enum_certificates("ROOT"):
                    if encoding == "x509_asn" and (trust is True or "1.3.6.1.5.5.7.3.1" in trust):
                        context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(certificate))
            except (OSError, ssl.SSLError):
                pass
        handlers: list[Any] = [HTTPSHandler(context=context)]
        if validate_redirects:
            handlers.append(ValidatingRedirectHandler(validate_redirects))
        self.opener = build_opener(*handlers)

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
    ) -> Any:
        request_headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        request_headers.update(headers or {})
        safe_url = redact_url(url)
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            request = Request(url, data=data, headers=request_headers, method=method)
            try:
                return self.opener.open(request, timeout=self.timeout)
            except HTTPError as exc:
                last_error = exc
                if exc.code not in {429, 500, 502, 503, 504} or attempt >= self.max_retries:
                    body = exc.read(4096).decode("utf-8", "replace")
                    raise HttpError(
                        f"HTTP {exc.code} for {safe_url}: {body[:500]}", exc.code, safe_url
                    ) from exc
                time.sleep(_retry_delay(exc.headers, attempt))
            except (URLError, TimeoutError, ConnectionError, OSError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    raise HttpError(f"Network error for {safe_url}: {exc}", url=safe_url) from exc
                time.sleep(_retry_delay(None, attempt))
        raise HttpError(f"Request failed for {safe_url}: {last_error}", url=safe_url)

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        if params:
            query = urlencode({key: value for key, value in params.items() if value not in (None, "")})
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        with self.request(url, headers=headers) as response:
            raw = response.read()
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpError(f"Invalid JSON from {redact_url(url)}", url=redact_url(url)) from exc

    def post_json(
        self,
        url: str,
        payload: Any,
        headers: dict[str, str] | None = None,
    ) -> tuple[Any, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        merged = {"Content-Type": "application/json", **(headers or {})}
        with self.request(url, method="POST", headers=merged, data=body) as response:
            raw = response.read()
            response_headers = response.headers
        return (json.loads(raw.decode("utf-8")) if raw else None, response_headers)
