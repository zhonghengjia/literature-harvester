"""Bounded HTTPS transport with explicit deferred retries and safe diagnostics."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import ipaddress
import json
import math
import socket
import ssl
import time
import threading
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


NETWORK_ERRORS = (URLError, HTTPException, TimeoutError, ConnectionError, OSError)
_HOST_COOLDOWNS: dict[str, float] = {}
_ARXIV_LOCK = threading.Lock()
_ARXIV_COMPLETED = 0.0


def _arxiv_slot(host: str):
    if host not in {"arxiv.org", "export.arxiv.org"}:
        return None
    _ARXIV_LOCK.acquire()
    delay = 3 - (time.monotonic() - _ARXIV_COMPLETED)
    if delay > 0:
        time.sleep(delay)
    def release():
        global _ARXIV_COMPLETED
        _ARXIV_COMPLETED = time.monotonic()
        _ARXIV_LOCK.release()
    return release


def host_deferrals() -> dict[str, str]:
    return {host: datetime.fromtimestamp(deadline, timezone.utc).isoformat()
            for host, deadline in list(_HOST_COOLDOWNS.items()) if deadline > time.time()}


def restore_host_deferrals(values: dict[str, str]) -> None:
    for host, value in values.items():
        try:
            deadline = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            if deadline > time.time():
                _HOST_COOLDOWNS[host] = max(_HOST_COOLDOWNS.get(host, 0), deadline)
        except (ValueError, TypeError, OverflowError):
            continue
_SECRET_NAMES = {"apikey", "key", "email", "mailto", "token", "accesstoken",
                 "authorization", "password", "secret", "clientsecret", "signature",
                 "credential", "xamzsignature", "xamzcredential", "xamzsecuritytoken"}


def _secret_name(value: str) -> bool:
    return value.casefold().replace("-", "").replace("_", "") in _SECRET_NAMES


def redact_url(url: str) -> str:
    """Remove credentials and fragments; preserve useful non-secret query fields."""
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        query = urlencode([(name, "REDACTED" if _secret_name(name) else value)
                           for name, value in parse_qsl(parsed.query, keep_blank_values=True)])
        return urlunparse(parsed._replace(netloc=netloc, query=query, fragment=""))
    except (TypeError, ValueError):
        return "[invalid URL]"


class HttpError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, url: str = "", *,
                 retry_after: float | None = None, retry_at: str | None = None,
                 deferred: bool = False, code: str = "http_error") -> None:
        super().__init__(message)
        self.status = status
        self.url = redact_url(url)
        self.retry_after = retry_after
        self.retry_at = retry_at
        self.deferred = deferred
        self.code = code


def is_public_https_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if (parsed.scheme.casefold() != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or any(ord(c) < 32 for c in url) or "\\" in url):
            return False
        host = parsed.hostname.casefold().rstrip(".")
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".localhost")):
            return False
        addresses = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(a[4][0]).is_global for a in addresses)
    except (OSError, TypeError, ValueError):
        return False


class ValidatingRedirectHandler(HTTPRedirectHandler):
    def __init__(self, validator: Callable[[str], bool]) -> None:
        super().__init__()
        self.validator = validator

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Request | None:
        absolute = urljoin(req.full_url, newurl)
        if not self.validator(absolute):
            raise HttpError(f"Unsafe redirect rejected: {redact_url(absolute)}",
                            status=code, url=absolute, code="unsafe_url")
        redirected = super().redirect_request(req, fp, code, msg, headers, absolute)
        host = urlparse(absolute).hostname or ""
        remaining = _HOST_COOLDOWNS.get(host, 0) - time.time()
        if remaining > 0:
            raise HttpError(f"Redirect host retry is deferred: {host}", 429, absolute,
                            retry_after=remaining, retry_at=_retry_at(remaining),
                            deferred=True, code="deferred")
        if redirected is not None:
            old, new = urlparse(req.full_url), urlparse(absolute)
            if (old.scheme, old.hostname, old.port or 443) != (new.scheme, new.hostname, new.port or 443):
                for name in list(dict(redirected.header_items())):
                    if _secret_name(name) or name.casefold() in {"cookie", "proxy-authorization", "x-api-key"}:
                        redirected.remove_header(name)
        return redirected


def _retry_after(headers: Any) -> float | None:
    value = headers.get("Retry-After") if headers else None
    if value is None:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except (TypeError, ValueError):
        try:
            moment = parsedate_to_datetime(str(value))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return max(0.0, moment.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None


def _retry_delay(headers: Any, attempt: int) -> float:
    """Never truncate a server's Retry-After deadline."""
    delay = _retry_after(headers)
    return delay if delay is not None else min(2 ** attempt, 8)


def _retry_at(delay: float | None) -> str | None:
    if delay is None:
        return None
    try:
        return datetime.fromtimestamp(time.time() + delay, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


class _SafeResponse:
    """Convert streaming read errors without retrying an already-consumed stream."""
    def __init__(self, response: Any, url: str, on_close=None) -> None:
        self._response, self._url = response, url
        self._on_close = on_close

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)

    def __enter__(self) -> "_SafeResponse":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._response.close()
        except NETWORK_ERRORS:
            pass
        finally:
            if self._on_close is not None:
                callback, self._on_close = self._on_close, None
                callback()

    def read(self, *args: Any, **kwargs: Any) -> bytes:
        return self._read("read", *args, **kwargs)

    def read1(self, *args: Any, **kwargs: Any) -> bytes:
        return self._read("read1", *args, **kwargs)

    def _read(self, name: str, *args: Any, **kwargs: Any) -> bytes:
        try:
            reader = getattr(self._response, name, None) or self._response.read
            return reader(*args, **kwargs)
        except NETWORK_ERRORS as exc:
            raise HttpError(f"Response body read failed for {redact_url(self._url)} ({type(exc).__name__})",
                            url=self._url, code="body_read_failed") from None


class HttpClient:
    def __init__(self, timeout: float = 30,
                 user_agent: str = "LiteratureHarvester/0.3 (+local-personal-research)",
                 max_retries: int = 2,
                 validate_redirects: Callable[[str], bool] | None = None, *,
                 max_inline_retry_delay: float = 10, max_retry_wait: float = 30) -> None:
        self.timeout = timeout
        self.user_agent = user_agent
        self.max_retries = max(0, int(max_retries))
        self.max_inline_retry_delay = max(0.0, float(max_inline_retry_delay))
        self.max_retry_wait = max(0.0, float(max_retry_wait))
        self.validator = validate_redirects or is_public_https_url
        try:
            import certifi
            ca_file = certifi.where()
        except ImportError:
            try:
                from pip._vendor import certifi
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
        self.opener = build_opener(HTTPSHandler(context=context), ValidatingRedirectHandler(self.validator))

    def _execute(self, url: str, method: str, headers: dict[str, str] | None,
                 data: bytes | None, consume: Callable[[Any], Any] | None = None) -> Any:
        safe_url = redact_url(url)
        if not self.validator(url):
            raise HttpError(f"Unsafe request rejected: {safe_url}", url=url, code="unsafe_url")
        host = urlparse(url).hostname or ""
        remaining = _HOST_COOLDOWNS.get(host, 0) - time.time()
        if remaining > 0:
            raise HttpError(f"Host retry is deferred: {host}", 429, url,
                            retry_after=remaining, retry_at=_retry_at(remaining),
                            deferred=True, code="deferred")
        minimum_delay = 3 if host in {"arxiv.org", "export.arxiv.org"} else 0
        request_headers = {"User-Agent": self.user_agent, "Accept": "application/json", **(headers or {})}
        method = method.upper()
        retryable_method = method in {"GET", "HEAD"}
        attempts = self.max_retries + 1 if retryable_method else 1
        waited = 0.0
        for attempt in range(attempts):
            response = None
            release_slot = _arxiv_slot(host)
            try:
                response = self.opener.open(Request(url, data=data, headers=request_headers, method=method),
                                            timeout=self.timeout)
                if consume is None:
                    wrapped = _SafeResponse(response, url, release_slot)
                    release_slot = None
                    return wrapped
                return consume(response)
            except HTTPError as exc:
                status = exc.code
                actual_url = exc.url or url
                error_host = urlparse(actual_url).hostname or host
                delay_header = _retry_after(exc.headers)
                delay = max(minimum_delay, delay_header if delay_header is not None else min(2 ** attempt, 8))
                retryable = status == 429 or 500 <= status < 600
                if retryable and delay_header is not None and delay_header > 0:
                    _HOST_COOLDOWNS[error_host] = max(_HOST_COOLDOWNS.get(error_host, 0), time.time() + delay_header)
                # Never embed server bodies or exception strings: either may echo credentials.
                try:
                    exc.close()
                except NETWORK_ERRORS:
                    pass
                terminal = (not retryable or attempt + 1 >= attempts
                            or delay > self.max_inline_retry_delay or waited + delay > self.max_retry_wait)
                if terminal:
                    deferred = retryable and delay_header is not None and delay_header > 0
                    raise HttpError(f"HTTP {status} for {redact_url(actual_url)}" + ("; retry deferred" if deferred else ""),
                                    status, actual_url, retry_after=delay_header,
                                    retry_at=_retry_at(delay_header), deferred=deferred,
                                    code="deferred" if deferred else "http_error") from None
            except NETWORK_ERRORS as exc:
                if attempt + 1 >= attempts:
                    raise HttpError(f"Network or response body error for {safe_url} ({type(exc).__name__})",
                                    url=url, code="network_error") from None
                delay = max(minimum_delay, min(2 ** attempt, 8))
                if waited + delay > self.max_retry_wait or delay > self.max_inline_retry_delay:
                    raise HttpError(f"Network retry budget exhausted for {safe_url}", url=url,
                                    code="network_error") from None
            finally:
                if consume is not None and response is not None:
                    try:
                        response.close()
                    except NETWORK_ERRORS:
                        pass
                if release_slot is not None:
                    release_slot()
            # sleep receives the full server delay, never a truncated value.
            time.sleep(delay)
            waited += delay
        raise AssertionError("unreachable request state")

    def request(self, url: str, *, method: str = "GET", headers: dict[str, str] | None = None,
                data: bytes | None = None) -> Any:
        return self._execute(url, method, headers, data)

    @staticmethod
    def _json_body(response: Any, url: str, allow_empty: bool = False) -> tuple[Any, Any]:
        limit = 16 * 1024 * 1024
        raw = response.read(limit + 1)
        if len(raw) > limit:
            raise HttpError(f"JSON exceeded {limit} bytes for {redact_url(url)}", url=url, code="body_too_large")
        try:
            return (None if allow_empty and not raw else json.loads(raw.decode("utf-8-sig")), response.headers)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HttpError(f"Invalid JSON from {redact_url(url)}", url=url, code="invalid_json") from None

    def get_json(self, url: str, params: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None) -> Any:
        if params:
            query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        result, _ = self._execute(url, "GET", headers, None, lambda response: self._json_body(response, url))
        return result

    def post_json(self, url: str, payload: Any,
                  headers: dict[str, str] | None = None) -> tuple[Any, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._execute(url, "POST", {"Content-Type": "application/json", **(headers or {})}, data,
                             lambda response: self._json_body(response, url, allow_empty=True))
