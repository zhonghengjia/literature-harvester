"""Offline, explicit browser handoff. No extension RPC, cookies, or Zotero writes."""
from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
import os
from pathlib import Path
import re
from typing import Any, Iterable
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

from .downloader import filename_for, sha256_file, validate_pdf
from .identity import PdfValidationError, verify_pdf_identity
from .models import PaperRecord


ACCEPTED = {"downloaded", "already_downloaded"}
# This is a rejection aid, never an allowlist. Unknown domains need independent
# provenance evidence; a renamed mirror does not gain eligibility here.
SHADOW_LABELS = re.compile(r"(?:sci[-_]?hub|libgen|library[-_]?genesis|annas[-_]?archive|z[-_]?lib|zlibrary)", re.I)
OA_PROVENANCE_SOURCES = {
    "pmc", "europe_pmc", "pmc_oa_cloud", "arxiv", "biorxiv", "medrxiv",
    "openalex", "unpaywall", "core", "semantic_scholar", "crossref_verified_oa",
}
MAX_LOCAL_BYTES = 100 * 1024 * 1024


def browser_status() -> dict[str, Any]:
    return {
        "mode": "manual_handoff", "automatic_extension_control": False,
        "extension_rpc_available": False, "extension_invoked": False,
        "supported": ["queue_manual_routes", "ingest_explicit_local_pdf"],
        "blocked": "No verified external messaging API is available for EasyPubMedicine. "
                   "Automatic extension download is not implemented. The extension may "
                   "enable disallowed sources; this module neither invokes it nor changes its settings.",
        "requirements": "Use an official publisher/repository or an entitled institutional route. "
                        "Supply original-source evidence and a strong record identifier before ingestion.",
    }


def _safe_url(value: Any) -> str:
    """Validate offline without DNS, retaining no query/fragment credentials."""
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        return ""
    try:
        parsed = urlsplit(value.strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            return ""
        if parsed.port not in {None, 443} or "\\" in value or "%" in host:
            return ""
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host:
            return ""
        try:
            if not ipaddress.ip_address(host).is_global:
                return ""
        except ValueError:
            pass
        return urlunsplit(("https", host, parsed.path or "/", "", ""))
    except (TypeError, ValueError):
        return ""


def _shadow(value: str) -> bool:
    try:
        return bool(SHADOW_LABELS.search(urlsplit(value).hostname or ""))
    except ValueError:
        return True


def _strong_id(record: PaperRecord) -> str:
    return next((key for key in record.identity_keys if not key.startswith("title:")), "")


def select_record(records: Iterable[PaperRecord], record_id: str) -> PaperRecord:
    """Exact DOI/PMID/PMCID/arXiv namespace selection, never a fuzzy title."""
    selected = [record for record in records if record_id.casefold() in
                [key.casefold() for key in record.identity_keys if not key.startswith("title:")]]
    if len(selected) != 1:
        raise ValueError("record_id must match exactly one DOI/PMID/PMCID/arXiv identifier")
    return selected[0]


def queue_records(records: Iterable[PaperRecord]) -> dict[str, Any]:
    """Build a read-only handoff queue from failed or unresolved manifest rows."""
    queue: list[dict[str, Any]] = []
    for record in records:
        if record.download_status in ACCEPTED and record.identity_status == "verified":
            continue
        if record.download_status == "duplicate_pdf":
            continue
        routes: list[dict[str, str]] = []
        if record.doi:
            routes.append({"kind": "doi_landing", "url": "https://doi.org/" + quote(record.doi, safe="/()"),
                           "access": "landing_only_not_oa_evidence"})
        if record.pmid:
            routes.append({"kind": "pubmed", "url": f"https://pubmed.ncbi.nlm.nih.gov/{record.pmid}/",
                           "access": "metadata_only_not_oa_evidence"})
        if record.pmcid:
            routes.append({"kind": "pmc_article", "url": f"https://pmc.ncbi.nlm.nih.gov/articles/{record.pmcid}/",
                           "access": "article_page_check_license"})
        if record.arxiv_id:
            routes.append({"kind": "arxiv", "url": "https://arxiv.org/abs/" + quote(record.arxiv_id, safe="/"),
                           "access": "official_preprint"})
        for candidate in record.pdf_candidates:
            url = _safe_url(candidate.get("url"))
            if not url or _shadow(url) or candidate.get("kind", "pdf") not in {"pdf", "landing"}:
                continue
            if candidate.get("source") in OA_PROVENANCE_SOURCES and (
                candidate.get("license") or candidate.get("oa_verified") is True
            ):
                routes.append({"kind": "oa_candidate", "url": url,
                               "access": "manifest_oa_evidence_check_original_provider"})
        unique = {item["url"]: item for item in routes}
        queue.append({"record_id": _strong_id(record), "identity_digest": record.identity_digest,
                      "title": record.title, "status": record.download_status,
                      "failure_reason": _safe_text(record.failure_reason),
                      "requires_strong_id": not bool(_strong_id(record)),
                      "routes": list(unique.values()),
                      "next_step": "Open a permitted route manually; provide the downloaded PDF and original-source evidence."})
    return {**browser_status(), "count": len(queue), "records": queue}


def _safe_text(value: Any) -> str:
    text = str(value or "")[:1000]
    text = re.sub(r"https?://[^\s<>\"']+", lambda match: _safe_url(match.group(0)) or "[invalid URL]", text)
    return re.sub(r"(?i)(?:api[_-]?key|token|password|authorization|cookie)\s*[:=]\s*\S+", "[redacted credential]", text)


def _access_evidence(source_url: str, basis: Any) -> tuple[str, str, dict[str, Any]]:
    if _shadow(source_url):
        return "rejected_source", "Known shadow-library source is disallowed", {}
    source = _safe_url(source_url)
    if not source:
        return "manual_review", "A public HTTPS original source URL without credentials is required", {}
    if not isinstance(basis, dict):
        return "manual_review", "An access label alone is insufficient; original-provider evidence is required", {}
    kind = basis.get("kind")
    provider = basis.get("source_kind")
    if kind not in {"oa", "institutional"} or provider not in {
        "publisher", "institutional_repository", "institutional_library", "official_preprint",
    }:
        return "manual_review", "Declare OA or entitled institutional access and the original provider type", {}
    evidence_url = _safe_url(basis.get("evidence_url"))
    note = _safe_text(basis.get("evidence_note"))
    if _shadow(str(basis.get("evidence_url", ""))):
        return "rejected_source", "Shadow-library evidence cannot establish permitted access", {}
    # The user must confirm observation of original-provider evidence. A
    # plugin's success state, file presence, and host spelling are insufficient.
    if (basis.get("confirmed_by") != "user" or basis.get("original_provider_confirmed") is not True
            or not evidence_url or len(note.strip()) < 20):
        return "manual_review", "User confirmation of observed original-provider evidence is missing; plugin success is not proof", {}
    if kind == "institutional" and basis.get("entitlement_confirmed") is not True:
        return "manual_review", "Institutional access requires explicit user entitlement confirmation", {}
    if kind == "oa" and not str(basis.get("license_or_oa_statement", "")).strip():
        return "manual_review", "OA access requires the provider's license or OA statement", {}
    evidence = {"kind": kind, "source_kind": provider, "source_url": source,
                "evidence_url": evidence_url, "evidence_note": note, "confirmed_by": "user",
                "original_provider_confirmed": True,
                "verification_level": "user_attested_original_provider_not_independently_verified"}
    if kind == "institutional":
        evidence["entitlement_confirmed"] = True
    else:
        evidence["license_or_oa_statement"] = _safe_text(basis["license_or_oa_statement"])
    return "accepted", "User supplied explicit original-provider evidence", evidence


def ingest_local_pdf(record: PaperRecord, path: Path, pdf_dir: Path,
                     source_url: str, access_basis: Any,
                     records: Iterable[PaperRecord]) -> dict[str, Any]:
    """Verify one explicitly selected local PDF and update its record in memory.

    The CLI owns manifest/report persistence. Access is never inferred from the
    plugin, hostname, PDF bytes, or the record's is_oa flag. A failure preserves
    an already accepted PDF and records the failed new attempt separately.
    """
    previous_good = record.download_status in ACCEPTED and record.identity_status == "verified"
    provenance: dict[str, Any] = {}

    def finish(outcome: str, reason: str, **details: Any) -> dict[str, Any]:
        result = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "stage": "browser_ingest", "source": "user_local_handoff",
                  "record_id": _strong_id(record), "outcome": outcome,
                  "url": _safe_url(source_url), "reason": reason,
                  "access_evidence": provenance, **details}
        record.attempts.append(result)
        if outcome not in ACCEPTED and not previous_good:
            record.download_status = outcome
            record.failure_reason = reason
        return result

    if not _strong_id(record):
        return finish("manual_review", "A strong record identifier is required; title-only ingestion is disabled")
    outcome, reason, provenance = _access_evidence(source_url, access_basis)
    if outcome != "accepted":
        return finish(outcome, reason)
    source = Path(path).expanduser()
    if not source.is_file():
        return finish("download_failed", "The explicitly supplied local PDF does not exist or is not a file")
    target_dir = Path(pdf_dir).expanduser().resolve()
    part: Path | None = None
    try:
        if source.stat().st_size > MAX_LOCAL_BYTES:
            return finish("too_large", "Local PDF exceeds the 100 MiB ingestion limit")
        target_dir.mkdir(parents=True, exist_ok=True)
        part = target_dir / f".browser-ingest-{uuid4().hex}.part"
        # Verify this stable snapshot, so changes to Downloads during validation
        # cannot change the accepted bytes after the identity check.
        with source.open("rb") as input_file, part.open("xb") as output_file:
            copied = 0
            for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                copied += len(chunk)
                if copied > MAX_LOCAL_BYTES:
                    return finish("too_large", "Local PDF grew beyond the ingestion size limit")
                output_file.write(chunk)
        validate_pdf(part)
        identity = verify_pdf_identity(part, record)
        if identity.status != "verified":
            if not previous_good:
                record.identity_status = identity.status
                record.extra["identity_verification"] = identity.to_dict()
            return finish("manual_review", identity.reason, identity_status=identity.status,
                          identity_evidence=identity.evidence)
        checksum = sha256_file(part)
        for existing in records:
            if existing.sha256 != checksum or existing.identity_status != "verified" or not existing.local_pdf:
                continue
            existing_path = Path(existing.local_pdf)
            if existing.download_status not in ACCEPTED or not existing_path.is_file():
                continue
            if sha256_file(existing_path) != checksum:
                continue
            if existing is record:
                record.download_status = "already_downloaded"
                return finish("already_downloaded", "The same verified PDF is already attached to this record",
                              sha256=checksum, identity_status="verified", local_pdf=str(existing_path.resolve()))
            if not previous_good:
                record.identity_status = "verified"
                record.extra["identity_verification"] = identity.to_dict()
                record.duplicate_of = str(existing_path.resolve())
                record.sha256 = checksum
                record.local_pdf = ""
            return finish("duplicate_pdf", "An accepted record already owns these exact PDF bytes",
                          duplicate_of=str(existing_path.resolve()), sha256=checksum, identity_status="verified")
        destination = target_dir / filename_for(record)
        if destination.exists():
            # Never overwrite an existing file, even if a record is stale.
            destination = target_dir / f"{destination.stem}-{uuid4().hex[:12]}.pdf"
        if destination.parent.resolve() != target_dir:
            return finish("download_failed", "Generated destination escaped the run PDF directory")
        # Hard-link is atomic and fails if another process claims the name. The
        # temporary file lives on the same filesystem; cleanup unlinks only it.
        os.link(part, destination)
        record.local_pdf = str(destination.resolve())
        record.sha256, record.identity_status = checksum, "verified"
        record.download_status, record.download_source = "downloaded", "user_local_handoff"
        record.download_url = provenance["source_url"]
        record.download_version = ""
        record.retrieval_type = "browser_oa_pdf" if provenance["kind"] == "oa" else "browser_authorized_pdf"
        record.duplicate_of, record.failure_reason, record.retry_at = "", "", ""
        record.extra["identity_verification"] = identity.to_dict()
        record.extra["browser_access_evidence"] = provenance
        return finish("downloaded", "Copied and verified the user-supplied PDF",
                      sha256=checksum, identity_status="verified", local_pdf=record.local_pdf)
    except (OSError, PdfValidationError) as exc:
        return finish(getattr(exc, "code", "download_failed"), _safe_text(exc))
    finally:
        if part is not None and part.exists():
            try:
                part.unlink()
            except OSError:
                record.attempts.append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                        "stage": "browser_ingest_cleanup", "outcome": "cleanup_failed",
                                        "reason": "Could not remove this invocation's temporary snapshot",
                                        "path": str(part)})
