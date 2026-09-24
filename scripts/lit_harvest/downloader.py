from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
import os
from pathlib import Path
import re
import time
import unicodedata
from typing import Any, Callable
from urllib.parse import urlencode, urlparse, urlunparse
from uuid import uuid4

from .fulltext import FullTextError, fetch_jats_url, resolve_landing_pdfs
from .http import HttpClient, HttpError, is_public_https_url, redact_url
from .identity import PdfValidationError as DownloadError, validate_pdf_structure, verify_pdf_identity
from .models import PaperRecord, normalize_pmcid, candidate_request_key
from .pmc import resolve_pmc_candidates, is_pmc_listing_candidate
from . import __version__


SOURCE_PRIORITY = {
    "europe_pmc": 0, "pmc": 0, "pmc_oa_cloud": 0,
    "arxiv": 1, "biorxiv": 1, "medrxiv": 1, "europe_pmc_preprint": 1,
    "openalex_content": 2, "openalex": 2, "unpaywall": 3, "core": 4,
    "openaire": 5, "doaj": 5, "semantic_scholar": 6, "crossref_verified_oa": 7,
    "europe_pmc_preprint_landing": 20, "openalex_landing": 20,
    "unpaywall_landing": 20, "core_landing": 21, "openaire_landing": 21, "doaj_landing": 21,
}
KIND_PRIORITY = {"pdf": 0, "pmc_listing": 0, "landing": 1, "jats": 2, "fulltext_xml": 2}
PDF_STATUSES = {"downloaded", "already_downloaded"}


def safe_filename(value: str, max_length: int = 150) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value)
    value = " ".join(value.split()).strip(" .") or "paper"
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if value.upper() in reserved:
        value = f"paper-{value}"
    return value[:max_length].rstrip(" .")


def filename_for(record: PaperRecord) -> str:
    parts = record.authors[0].split() if record.authors else []
    author = (parts[0] if len(parts) > 1 and parts[-1].isupper() and len(parts[-1]) <= 5 else parts[-1]) if parts else "Unknown"
    readable = safe_filename(f"{record.year or 'n.d.'} - {author} - {record.title}", 125)
    return f"{readable} - {record.identity_digest}.pdf"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_pdf(path: Path) -> None:
    """Structural compatibility entry point; it does not assert article identity."""
    validate_pdf_structure(path)


def sorted_candidates(record: PaperRecord) -> list[dict[str, Any]]:
    return sorted(record.pdf_candidates, key=lambda candidate: (
        KIND_PRIORITY.get(candidate.get("kind", "pdf"), 3),
        SOURCE_PRIORITY.get(candidate.get("source", ""), 99), candidate.get("url", ""),
    ))


def user_agent(contact_email: str = "") -> str:
    base = f"LiteratureHarvester/{__version__} (+local-personal-research)"
    contact = (contact_email or "").strip()
    return f"{base[:-1]}; mailto:{contact})" if contact else base


def retrieval_type(candidate: dict[str, Any]) -> str:
    version = str(candidate.get("version", "")).casefold()
    source = str(candidate.get("source", "")).casefold()
    if "submitted" in version or source in {"arxiv", "biorxiv", "medrxiv", "europe_pmc_preprint", "europe_pmc_preprint_landing"}:
        return "preprint_pdf"
    if "accepted" in version or "manuscript" in version:
        return "accepted_manuscript_pdf"
    if "published" in version:
        return "version_of_record_pdf"
    if source in {"core", "core_landing", "openaire", "openaire_landing"}:
        return "repository_pdf"
    return "legal_oa_pdf"


def upgrade_http_candidate(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme.casefold() != "http" or not parsed.netloc or parsed.username or parsed.password:
        return url
    return urlunparse(("https", parsed.netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _future(value: str) -> bool:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp > datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return False


def _safe_message(value: Any, contact: str = "") -> str:
    message = str(value)
    if contact:
        message = message.replace(contact, "REDACTED")
    message = re.sub(r"https?://[^\s<>\"']+", lambda match: redact_url(match.group(0)), message)
    return message[:700]


def _attempt(record: PaperRecord, candidate: dict[str, Any], stage: str, outcome: str,
             url: str = "", error: Any = "", contact: str = "", **details: Any) -> dict[str, Any]:
    item = {
        "at": _now(), "stage": stage, "source": str(candidate.get("source", "")),
        "candidate_url": redact_url(str(candidate.get("url", ""))),
        "url": redact_url(url or str(candidate.get("url", ""))),
        "version": str(candidate.get("version", "")), "outcome": outcome,
        **details,
    }
    if error:
        item["error"] = _safe_message(error, contact)
    record.attempts.append(item)
    return item


def _classify_http_error(exc: HttpError) -> str:
    if getattr(exc, "deferred", False) or _future(getattr(exc, "retry_at", "")):
        return "deferred"
    if exc.status == 429:
        return "rate_limited"
    if exc.status in {401, 403}:
        return "anti_bot"
    if exc.status in {404, 410}:
        return "dead_link"
    return "download_failed"


class _TracedClient:
    """Record resolver requests as well as file URLs, with host deferrals."""
    def __init__(self, client: HttpClient, record: PaperRecord, contact: str) -> None:
        self.client, self.record, self.contact = client, record, contact
        self.candidate: dict[str, Any] = {}
        self.deferred_hosts: dict[str, str] = {}
        for item in record.attempts:
            if _future(item.get("retry_at", "")):
                host = urlparse(item.get("url", "")).hostname or ""
                self.deferred_hosts[host] = item["retry_at"]

    def _check_deferred(self, url: str) -> None:
        host = urlparse(url).hostname or ""
        retry_at = self.deferred_hosts.get(host, "")
        if retry_at and _future(retry_at):
            exc = HttpError("Host request deferred until the recorded Retry-After time", status=429, url=redact_url(url))
            exc.retry_at, exc.deferred = retry_at, True
            _attempt(self.record, self.candidate, "request", "deferred", url, exc, self.contact, retry_at=retry_at)
            raise exc

    def _record_http_failure(self, url: str, exc: HttpError) -> None:
        url = exc.url or url
        retry_at = getattr(exc, "retry_at", "") or ""
        if retry_at:
            self.deferred_hosts[urlparse(url).hostname or ""] = retry_at
            if not self.record.retry_at or retry_at < self.record.retry_at:
                self.record.retry_at = retry_at
        _attempt(self.record, self.candidate, "request", _classify_http_error(exc), url, exc,
                 self.contact, http_status=exc.status, retry_at=retry_at,
                 retry_after=getattr(exc, "retry_after", None))

    def request(self, url: str, **kwargs: Any) -> Any:
        self._check_deferred(url)
        try:
            response = self.client.request(url, **kwargs)
        except HttpError as exc:
            self._record_http_failure(url, exc)
            raise
        actual = getattr(response, "url", "")
        actual = actual if isinstance(actual, str) and actual else url
        _attempt(self.record, self.candidate, "request", "response_received", actual,
                 requested_url=redact_url(url))
        return response

    def get_json(self, url: str, params: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None) -> Any:
        if params:
            query = urlencode({key: value for key, value in params.items() if value not in (None, "")})
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        self._check_deferred(url)
        try:
            payload = self.client.get_json(url, headers=headers)
        except HttpError as exc:
            self._record_http_failure(url, exc)
            raise
        _attempt(self.record, self.candidate, "metadata", "response_received", url)
        return payload


def _stream_pdf_response(response: Any, part_path: Path, max_bytes: int,
                         max_pdf_mb: int, total_timeout: int) -> bytes:
    started, total, first_bytes = time.monotonic(), 0, b""
    read_chunk = getattr(response, "read1", None) or response.read
    with part_path.open("wb") as handle:
        while True:
            if time.monotonic() - started > total_timeout:
                raise DownloadError("download_timeout", f"PDF body exceeded {total_timeout} seconds")
            chunk = read_chunk(64 * 1024)
            if time.monotonic() - started > total_timeout:
                raise DownloadError("download_timeout", f"PDF body exceeded {total_timeout} seconds")
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise DownloadError("too_large", f"Download exceeded {max_pdf_mb} MB")
            if len(first_bytes) < 4096:
                first_bytes += chunk[:4096 - len(first_bytes)]
            handle.write(chunk)
    length = response.headers.get("Content-Length")
    if length and int(length) != total:
        raise HTTPException("PDF body length does not match Content-Length")
    return first_bytes


def _unused_path(path: Path) -> Path:
    if not path.exists():
        return path
    return path.with_name(f"{path.stem} - {uuid4().hex[:10]}{path.suffix}")


def _commit_file(part_path: Path, path: Path) -> Path:
    # Hard linking within the output directory's filesystem atomically refuses
    # overwrite, unlike replace(). The partial name is removed only afterward.
    path = _unused_path(path)
    os.link(part_path, path)
    part_path.unlink()
    return path


def _apply_identity(record: PaperRecord, path: Path) -> Any:
    result = verify_pdf_identity(path, record)
    record.identity_status = result.status
    record.extra["pdf_identity"] = result.to_dict()
    return result


def _success(record: PaperRecord, path: Path, candidate: dict[str, Any], url: str, existing: bool) -> None:
    record.download_status = "already_downloaded" if existing else "downloaded"
    record.local_pdf = str(path.resolve())
    record.sha256 = sha256_file(path)
    record.download_source = str(candidate.get("source", "")) or record.download_source
    record.download_url = redact_url(url) if url else record.download_url
    record.download_version = str(candidate.get("version", "")) or record.download_version
    record.retrieval_type = retrieval_type(candidate) if candidate else (record.retrieval_type or "local_pdf_unclassified")
    record.license = str(candidate.get("license", "")) or record.license
    record.failure_reason, record.duplicate_of, record.retry_at = "", "", ""


def download_record(record: PaperRecord, pdf_dir: Path, max_pdf_mb: int = 100,
                    timeout: int = 30, contact_email: str = "",
                    allow_fulltext_fallback: bool = True) -> PaperRecord:
    pdf_dir = Path(pdf_dir)
    pdf_dir.mkdir(parents=True, exist_ok=True)
    final_path = pdf_dir / filename_for(record)
    errors: list[tuple[str, str]] = []
    previous_path, previous_hash = record.local_pdf, record.sha256
    record.local_pdf, record.sha256, record.duplicate_of = "", "", ""
    record.identity_status = "unverified"
    if not _future(record.retry_at):
        record.retry_at = ""
    existing_paths = list(dict.fromkeys([Path(previous_path)] if previous_path else []))
    if final_path not in existing_paths:
        existing_paths.append(final_path)
    for path in existing_paths:
        if not path.is_file():
            continue
        try:
            result = _apply_identity(record, path)
            resolved_path = str(path.resolve())
            expected_hash = (
                previous_hash if previous_path and path.resolve() == Path(previous_path).resolve() else ""
            ) or record.extra.get("pdf_checksum_conflicts", {}).get(resolved_path, "")
            changed = bool(expected_hash and sha256_file(path) != expected_hash)
            if changed:
                # Keep the original checksum after local_pdf/sha256 are cleared;
                # the enrichment phase must not silently approve changed bytes.
                record.extra.setdefault("pdf_checksum_conflicts", {})[resolved_path] = expected_hash
            if result.status == "verified" and not changed:
                _success(record, path, {}, "", True)
                _attempt(record, {}, "existing_file", "already_downloaded", path=str(path.resolve()))
                return record
            reason = "Previously recorded PDF checksum changed" if changed else result.reason
            record.identity_status = "manual_review" if changed else result.status
            errors.append(("manual_review", reason))
            _attempt(record, {}, "existing_file", "manual_review", error=reason, path=str(path.resolve()))
        except (DownloadError, OSError) as exc:
            errors.append((getattr(exc, "code", "download_failed"), str(exc)))
            _attempt(record, {}, "existing_file", "validation_failed", error=exc, path=str(path.resolve()))

    client = _TracedClient(HttpClient(timeout=timeout, max_retries=2,
        user_agent=user_agent(contact_email), validate_redirects=is_public_https_url), record, contact_email)
    pending = sorted_candidates(record)
    attempted_requests: set[tuple[str, str, str]] = set()
    jats_candidates: list[dict[str, Any]] = []
    while pending:
        candidate = pending.pop(0)
        client.candidate = candidate
        url = upgrade_http_candidate(str(candidate.get("url", "")))
        kind = candidate.get("kind", "pdf")
        if kind in {"jats", "fulltext_xml"}:
            jats_candidates.append(candidate)
            continue
        request_key = candidate_request_key({**candidate, "url": url})
        if request_key in attempted_requests:
            continue
        attempted_requests.add(request_key)
        if not is_public_https_url(url):
            message = "Unsafe or non-public HTTPS URL rejected"
            errors.append(("download_failed", message))
            _attempt(record, candidate, "candidate", "unsafe_url", url, message)
            continue
        try:
            is_listing = is_pmc_listing_candidate(candidate, url)
            if is_listing:
                resolved = resolve_pmc_candidates(client, url)
                for item in resolved:
                    record.add_pdf_candidate(**item)
                pending = sorted(resolved, key=lambda item: KIND_PRIORITY.get(item.get("kind", "pdf"), 3)) + pending
                _attempt(record, candidate, "resolve", "resolved" if resolved else "no_oa_version", url,
                         resolved_count=len(resolved))
                continue
            if kind == "landing":
                urls = resolve_landing_pdfs(client, url)
                resolved = [{**candidate, "url": value, "kind": "pdf", "referer": url} for value in urls]
                pending = resolved + pending
                for item in resolved:
                    record.add_pdf_candidate(**item)
                _attempt(record, candidate, "resolve", "resolved", url, resolved_count=len(resolved))
                continue
        except (HttpError, FullTextError, HTTPException, OSError, ValueError) as exc:
            code = _classify_http_error(exc) if isinstance(exc, HttpError) else getattr(exc, "code", "landing_unresolved")
            errors.append((code, str(exc)))
            _attempt(record, candidate, "resolve", code, url, exc, contact_email)
            continue

        headers = {"Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.1"}
        if candidate.get("referer"):
            headers["Referer"] = candidate["referer"]
        for body_attempt in range(2):
            part_path = pdf_dir / f".{record.identity_digest}-{uuid4().hex}.pdf.part"
            actual_url = url
            try:
                with client.request(url, headers=headers) as response:
                    actual = getattr(response, "url", "")
                    actual_url = actual if isinstance(actual, str) and actual else url
                    length = response.headers.get("Content-Length")
                    if length and int(length) > max_pdf_mb * 1024 * 1024:
                        raise DownloadError("too_large", f"Content-Length exceeds {max_pdf_mb} MB")
                    first_bytes = _stream_pdf_response(response, part_path, max_pdf_mb * 1024 * 1024,
                                                       max_pdf_mb, max(timeout * 3, 90))
                if b"%PDF-" not in first_bytes:
                    raise DownloadError("not_pdf", "Response is not a PDF")
                result = _apply_identity(record, part_path)
                if result.status != "verified":
                    review_dir = pdf_dir.parent / "review"
                    review_dir.mkdir(parents=True, exist_ok=True)
                    saved = _commit_file(part_path, review_dir / filename_for(record))
                    review = {"path": str(saved.resolve()), "sha256": sha256_file(saved),
                              "url": redact_url(actual_url), "identity": result.to_dict()}
                    record.extra.setdefault("identity_review_files", []).append(review)
                    errors.append(("manual_review", result.reason))
                    _attempt(record, candidate, "file", "manual_review", actual_url, result.reason,
                             contact_email, identity_status=result.status, path=review["path"])
                    break
                saved = _commit_file(part_path, final_path)
                _success(record, saved, candidate, actual_url, False)
                _attempt(record, candidate, "file", "downloaded", actual_url,
                         identity_status="verified", sha256=record.sha256)
                return record
            except HttpError as exc:
                if exc.code == "body_read_failed" and body_attempt == 0:
                    _attempt(record, candidate, "file", "retry_body", actual_url, exc,
                             contact_email, body_attempt=body_attempt + 1)
                    time.sleep(3 if urlparse(url).hostname in {"arxiv.org", "export.arxiv.org"} else 1)
                    continue
                code = _classify_http_error(exc)
                errors.append((code, str(exc)))
                _attempt(record, candidate, "file", code, actual_url, exc, contact_email,
                         retry_at=getattr(exc, "retry_at", "") or "")
                break
            except (HTTPException, OSError, DownloadError, ValueError) as exc:
                code = getattr(exc, "code", "download_failed")
                transient = isinstance(exc, (HTTPException, TimeoutError, ConnectionError)) or (
                    isinstance(exc, OSError) and not isinstance(exc, (PermissionError, FileNotFoundError))
                ) or code == "download_timeout"
                retry = transient and body_attempt == 0
                _attempt(record, candidate, "file", "retry_body" if retry else code, actual_url,
                         exc, contact_email, body_attempt=body_attempt + 1)
                if not retry:
                    errors.append((code, str(exc)))
                    break
                time.sleep(3 if urlparse(url).hostname in {"arxiv.org", "export.arxiv.org"} else 1)
            finally:
                if part_path.exists():
                    try:
                        part_path.unlink()
                    except OSError as exc:
                        _attempt(record, candidate, "cleanup", "cleanup_failed", actual_url, exc, contact_email,
                                 path=str(part_path))

    # Only explicit, qualified JATS candidates reach this phase. A bare PMCID
    # is insufficient evidence of eligibility for automated full-text access.
    if allow_fulltext_fallback:
        for candidate in jats_candidates:
            client.candidate = candidate
            url = str(candidate.get("url", ""))
            try:
                candidate_pmcid = normalize_pmcid(candidate.get("pmcid"))
                record_pmcid = normalize_pmcid(record.pmcid)
                if candidate_pmcid and record_pmcid and candidate_pmcid != record_pmcid:
                    raise FullTextError("JATS candidate PMCID differs from the requested article",
                                        code="identity_mismatch")
                markdown = fetch_jats_url(client, url, record_pmcid or candidate_pmcid)
                fulltext_dir = pdf_dir.parent / "fulltext"
                fulltext_dir.mkdir(parents=True, exist_ok=True)
                path = _unused_path(fulltext_dir / (Path(filename_for(record)).stem + ".fulltext.md"))
                with path.open("x", encoding="utf-8") as handle:
                    handle.write(markdown)
                record.local_fulltext, record.fulltext_format = str(path.resolve()), "markdown_from_jats"
                record.download_status, record.retrieval_type = "fulltext_only", "generated_jats_markdown"
                record.download_source = str(candidate.get("source", ""))
                record.download_url, record.download_version = redact_url(url), str(candidate.get("version", ""))
                record.license = str(candidate.get("license", "")) or record.license
                record.failure_reason = "No identity-verified PDF was retrieved; a qualified JATS reader is available"
                _attempt(record, candidate, "fulltext", "fulltext_only", url)
                return record
            except (HttpError, FullTextError, HTTPException, OSError, ValueError) as exc:
                code = _classify_http_error(exc) if isinstance(exc, HttpError) else "fulltext_unavailable"
                errors.append((code, str(exc)))
                _attempt(record, candidate, "fulltext", code, url, exc, contact_email)

    codes = {code for code, _ in errors}
    priority = ("manual_review", "deferred", "license_unknown", "rate_limited", "download_timeout",
                "anti_bot", "too_large", "not_pdf", "landing_unresolved", "dead_link", "fulltext_unavailable")
    record.download_status = next((code for code in priority if code in codes), "download_failed" if errors else "no_oa_version")
    if _future(record.retry_at) and record.download_status != "manual_review":
        record.download_status = "deferred"
    record.failure_reason = " | ".join(_safe_message(message, contact_email) for _, message in errors)[:3000] or "No verified legal full-text candidate was found"
    return record


def mark_duplicate(record: PaperRecord, seen_hashes: dict[str, str]) -> bool:
    if record.download_status not in PDF_STATUSES or record.identity_status != "verified" or not record.sha256 or not record.local_pdf:
        return False
    if record.sha256 in seen_hashes:
        record.download_status, record.duplicate_of = "duplicate_pdf", seen_hashes[record.sha256]
        record.failure_reason = "This PDF's SHA-256 already belongs to another accepted record in the batch"
        _attempt(record, {}, "batch", "duplicate_pdf", duplicate_of=record.duplicate_of, sha256=record.sha256)
        return True
    seen_hashes[record.sha256] = record.local_pdf
    return False


def download_all(records: list[PaperRecord], pdf_dir: Path, max_pdf_mb: int, timeout: int,
                 checkpoint: Callable[[list[PaperRecord]], None] | None = None,
                 contact_email: str = "") -> list[PaperRecord]:
    seen: dict[str, str] = {}
    for record in records:
        download_record(record, pdf_dir, max_pdf_mb, timeout, contact_email)
        mark_duplicate(record, seen)
        if checkpoint:
            checkpoint(records)
    return records


def download_until_target(records: list[PaperRecord], pdf_dir: Path, target_pdfs: int,
                          max_pdf_mb: int, timeout: int,
                          checkpoint: Callable[[list[PaperRecord]], None] | None = None,
                          contact_email: str = "") -> tuple[list[PaperRecord], list[PaperRecord]]:
    if target_pdfs < 1:
        raise ValueError("target_pdfs must be positive")
    successes, failures, seen = [], [], {}
    for record in records:
        download_record(record, pdf_dir, max_pdf_mb, timeout, contact_email)
        duplicate = mark_duplicate(record, seen)
        if not duplicate and record.download_status in PDF_STATUSES and record.identity_status == "verified" and record.sha256 and record.local_pdf:
            successes.append(record)
        else:
            failures.append(record)
        if checkpoint:
            checkpoint(records)
        if len(successes) >= target_pdfs:
            break
    return successes, failures
