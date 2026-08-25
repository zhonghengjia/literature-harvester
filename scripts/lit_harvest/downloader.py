from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import time
import unicodedata
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse
from xml.etree import ElementTree

from .http import HttpClient, HttpError, is_public_https_url
from .models import PaperRecord


SOURCE_PRIORITY = {
    "europe_pmc": 0,
    "pmc": 0,
    "pmc_oa_cloud": 0,
    "arxiv": 1,
    "biorxiv": 1,
    "medrxiv": 1,
    "openalex": 2,
    "unpaywall": 3,
    "semantic_scholar": 4,
    "crossref_verified_oa": 5,
}


class DownloadError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def safe_filename(value: str, max_length: int = 150) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value)
    value = " ".join(value.split()).strip(" .")
    if not value:
        value = "paper"
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if value.upper() in reserved:
        value = f"paper-{value}"
    return value[:max_length].rstrip(" .")


def filename_for(record: PaperRecord) -> str:
    if record.authors:
        parts = record.authors[0].split()
        first_author = parts[0] if len(parts) > 1 and parts[-1].isupper() and len(parts[-1]) <= 5 else parts[-1]
    else:
        first_author = "Unknown"
    year = str(record.year or "n.d.")
    return safe_filename(f"{year} - {first_author} - {record.title}") + ".pdf"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_pdf(path: Path) -> None:
    with path.open("rb") as handle:
        first = handle.read(4096)
    if b"%PDF-" not in first:
        raise DownloadError("not_pdf", "Downloaded content does not contain a PDF signature")
    try:
        try:
            import pymupdf as fitz  # type: ignore
        except ImportError:
            import fitz  # type: ignore

        with fitz.open(path) as document:
            if document.page_count < 1:
                raise DownloadError("not_pdf", "PDF parser found zero pages")
    except ImportError:
        return
    except DownloadError:
        raise
    except Exception as exc:
        raise DownloadError("not_pdf", f"PDF parser rejected the file: {exc}") from exc


def _classify_http_error(exc: HttpError) -> str:
    if exc.status == 429:
        return "rate_limited"
    if exc.status in {401, 403}:
        return "anti_bot"
    if exc.status in {404, 410}:
        return "dead_link"
    return "download_failed"


def sorted_candidates(record: PaperRecord) -> list[dict[str, str]]:
    return sorted(
        record.pdf_candidates,
        key=lambda candidate: (SOURCE_PRIORITY.get(candidate.get("source", ""), 99), candidate.get("url", "")),
    )


def _parse_pmc_cloud_listing(raw: bytes) -> tuple[str, int]:
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise DownloadError("download_failed", "PMC OA Cloud listing returned invalid XML") from exc
    namespace = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    versions: list[tuple[str, int]] = []
    for node in root.findall(".//s3:CommonPrefixes/s3:Prefix", namespace):
        match = re.fullmatch(r"(PMC\d+)\.(\d+)/", (node.text or "").strip(), re.I)
        if match:
            versions.append((match.group(1).upper(), int(match.group(2))))
    if not versions:
        raise DownloadError("no_oa_version", "No current PMC OA Cloud article version is available")
    pmcids = {pmcid for pmcid, _ in versions}
    if len(pmcids) != 1:
        raise DownloadError("download_failed", "PMC OA Cloud listing mixed multiple PMC identifiers")
    pmcid = versions[0][0]
    return pmcid, max(version for _, version in versions)


def _resolve_pmc_oa_cloud(client: HttpClient, listing_url: str) -> tuple[str, str]:
    listing_limit = 1024 * 1024
    with client.request(listing_url, headers={"Accept": "application/xml"}) as response:
        raw = response.read(listing_limit + 1)
    if len(raw) > listing_limit:
        raise DownloadError("download_failed", "PMC OA Cloud listing exceeded 1 MB")
    pmcid, version = _parse_pmc_cloud_listing(raw)
    version_id = f"{pmcid}.{version}"
    metadata_url = f"https://pmc-oa-opendata.s3.amazonaws.com/metadata/{version_id}.json"
    metadata = client.get_json(metadata_url)
    if str(metadata.get("pmcid", "")).upper() != pmcid or int(metadata.get("version", 0)) != version:
        raise DownloadError("download_failed", "PMC OA Cloud metadata does not match the listing")
    if not metadata.get("is_pmc_openaccess") or metadata.get("is_retracted"):
        raise DownloadError("no_oa_version", "PMC OA Cloud record is not an active OA article")
    license_value = str(metadata.get("license_code", "")).strip()
    if not license_value:
        raise DownloadError("license_unknown", "PMC OA Cloud metadata lacks an article license")
    parsed = urlparse(str(metadata.get("pdf_url", "")))
    if parsed.scheme != "s3" or parsed.netloc != "pmc-oa-opendata":
        raise DownloadError("no_oa_version", "PMC OA Cloud record does not provide an official PDF")
    expected_prefix = f"/{version_id}/"
    if not parsed.path.startswith(expected_prefix) or not parsed.path.casefold().endswith(".pdf"):
        raise DownloadError("download_failed", "PMC OA Cloud PDF path does not match the article version")
    pdf_url = urlunparse(("https", "pmc-oa-opendata.s3.amazonaws.com", parsed.path, "", "", ""))
    if not is_public_https_url(pdf_url):
        raise DownloadError("download_failed", "PMC OA Cloud PDF failed public HTTPS validation")
    return pdf_url, license_value


def _stream_pdf_response(
    response: Any,
    part_path: Path,
    max_bytes: int,
    max_pdf_mb: int,
    total_timeout: int,
) -> bytes:
    """Stream a response with both inactivity and whole-body time limits.

    ``HTTPResponse.read(size)`` may wait until the full requested size arrives.
    ``read1`` returns currently available buffered data, which lets us enforce a
    wall-clock deadline even when a server keeps a connection alive by sending
    only tiny fragments.
    """
    started = time.monotonic()
    total = 0
    first_bytes = b""
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
                first_bytes += chunk[: 4096 - len(first_bytes)]
            handle.write(chunk)
    return first_bytes


def download_record(
    record: PaperRecord,
    pdf_dir: Path,
    max_pdf_mb: int = 100,
    timeout: int = 30,
) -> PaperRecord:
    pdf_dir.mkdir(parents=True, exist_ok=True)
    final_path = pdf_dir / filename_for(record)
    if final_path.exists():
        try:
            validate_pdf(final_path)
            record.download_status = "already_downloaded"
            record.local_pdf = str(final_path.resolve())
            record.sha256 = sha256_file(final_path)
            return record
        except DownloadError:
            final_path = pdf_dir / (final_path.stem + " - redownload.pdf")

    candidates = sorted_candidates(record)
    if not candidates:
        record.download_status = "no_oa_version"
        record.failure_reason = "No verified legal open-access PDF candidate was found"
        return record

    errors: list[tuple[str, str]] = []
    max_bytes = max_pdf_mb * 1024 * 1024
    client = HttpClient(timeout=timeout, max_retries=2, validate_redirects=is_public_https_url)
    for candidate in candidates:
        url = candidate.get("url", "")
        if not is_public_https_url(url):
            errors.append(("download_failed", f"Unsafe or non-public HTTPS URL rejected: {url}"))
            continue
        part_path = final_path.with_suffix(final_path.suffix + ".part")
        try:
            resolved_url = url
            if candidate.get("source") == "pmc_oa_cloud":
                resolved_url, license_value = _resolve_pmc_oa_cloud(client, url)
                candidate["license"] = candidate.get("license") or license_value
                record.license = record.license or license_value
            with client.request(
                resolved_url,
                headers={"Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.1"},
            ) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > max_bytes:
                    raise DownloadError("too_large", f"Content-Length exceeds {max_pdf_mb} MB")
                first_bytes = _stream_pdf_response(
                    response,
                    part_path,
                    max_bytes=max_bytes,
                    max_pdf_mb=max_pdf_mb,
                    total_timeout=max(timeout * 3, 90),
                )
            if b"%PDF-" not in first_bytes:
                raise DownloadError("not_pdf", "Response was HTML or another non-PDF format")
            validate_pdf(part_path)
            os.replace(part_path, final_path)
            record.download_status = "downloaded"
            record.local_pdf = str(final_path.resolve())
            record.sha256 = sha256_file(final_path)
            record.download_source = candidate.get("source", "")
            record.download_url = resolved_url
            record.failure_reason = ""
            return record
        except DownloadError as exc:
            errors.append((exc.code, str(exc)))
        except HttpError as exc:
            errors.append((_classify_http_error(exc), str(exc)))
        except (OSError, ValueError) as exc:
            errors.append(("download_failed", str(exc)))
        finally:
            try:
                if part_path.exists():
                    part_path.unlink()
            except OSError:
                pass

    codes = [code for code, _ in errors]
    for preferred in (
        "license_unknown",
        "rate_limited",
        "download_timeout",
        "anti_bot",
        "too_large",
        "not_pdf",
        "dead_link",
        "no_oa_version",
    ):
        if preferred in codes:
            record.download_status = preferred
            break
    else:
        record.download_status = "download_failed"
    record.failure_reason = " | ".join(message for _, message in errors)[:3000]
    return record


def download_all(
    records: list[PaperRecord],
    pdf_dir: Path,
    max_pdf_mb: int,
    timeout: int,
    checkpoint: Callable[[list[PaperRecord]], None] | None = None,
) -> list[PaperRecord]:
    for record in records:
        if record.download_status in {"downloaded", "already_downloaded"} and record.local_pdf:
            continue
        download_record(record, pdf_dir, max_pdf_mb=max_pdf_mb, timeout=timeout)
        if checkpoint:
            checkpoint(records)
    return records


def download_until_target(
    records: list[PaperRecord],
    pdf_dir: Path,
    target_pdfs: int,
    max_pdf_mb: int,
    timeout: int,
    checkpoint: Callable[[list[PaperRecord]], None] | None = None,
) -> tuple[list[PaperRecord], list[PaperRecord]]:
    successes: list[PaperRecord] = []
    failures: list[PaperRecord] = []
    for record in records:
        download_record(record, pdf_dir, max_pdf_mb=max_pdf_mb, timeout=timeout)
        if record.download_status in {"downloaded", "already_downloaded"}:
            successes.append(record)
        else:
            failures.append(record)
        if checkpoint:
            checkpoint(records)
        if len(successes) >= target_pdfs:
            break
    return successes, failures
