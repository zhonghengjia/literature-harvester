"""Authoritative population and retrieval denominators."""
from __future__ import annotations
from collections import Counter
from pathlib import Path
from typing import Any
from .models import PaperRecord

PDF_STATUSES = {"downloaded", "already_downloaded"}
ARTICLE_TYPES = {"research", "review", "case_report", "letter", "editorial", "preprint", "unknown"}

def record_key(record: PaperRecord) -> str:
    strong = [key for key in record.identity_keys if not key.startswith("title:")]
    return strong[0] if strong else f"title:{record.normalized_title}|{record.year}|{'|'.join(record.authors)}"

def collect_population(manifest: dict[str, Any], records: list[PaperRecord]) -> list[PaperRecord]:
    population = list(records)
    for item in manifest.get("download_attempts", []):
        record = PaperRecord.from_dict(item)
        matches = [existing for existing in population if record_key(existing) == record_key(record)]
        compatible = next((existing for existing in matches if not any(
            getattr(existing, name) and getattr(record, name) and getattr(existing, name) != getattr(record, name)
            for name in ("doi", "pmid", "pmcid", "arxiv_id"))), None)
        if compatible is None:
            if matches:
                record.extra["identity_conflict"] = True
                for existing in matches:
                    existing.extra["identity_conflict"] = True
            population.append(record)
        else:
            compatible.extra.setdefault("legacy_download_attempts", [])
            snapshot = {"download_status": record.download_status, "failure_reason": record.failure_reason,
                        "download_source": record.download_source, "download_url": record.download_url}
            if snapshot not in compatible.extra["legacy_download_attempts"]:
                compatible.extra["legacy_download_attempts"].append(snapshot)
            for attempt in record.attempts:
                if attempt not in compatible.attempts:
                    compatible.attempts.append(attempt)
    return population

def article_type(record: PaperRecord) -> str:
    values = record.extra.get("publication_types") or []
    if isinstance(values, str):
        values = [values]
    kinds = " ".join(str(v).casefold().replace("-", " ") for v in values)
    if record.item_type == "preprint" or "preprint" in kinds:
        return "preprint"
    if "case report" in kinds:
        return "case_report"
    if "letter" in kinds or "correspondence" in kinds:
        return "letter"
    if "editorial" in kinds or "comment" in kinds:
        return "editorial"
    if "review" in kinds or "meta analysis" in kinds:
        return "review"
    if any(v in kinds for v in ("clinical trial", "randomized controlled trial",
           "observational study", "comparative study", "evaluation study",
           "multicenter study", "research article")):
        return "research"
    # A generic journal-article type does not establish original research or SCIE.
    return "unknown"

def is_verified_pdf(record: PaperRecord) -> bool:
    return bool(record.download_status in PDF_STATUSES
                and record.identity_status == "verified" and record.sha256
                and record.local_pdf and Path(record.local_pdf).is_file())

def unique_pdf_records(records: list[PaperRecord]) -> list[PaperRecord]:
    seen, result = set(), []
    for record in records:
        if is_verified_pdf(record) and record.sha256 not in seen:
            seen.add(record.sha256)
            result.append(record)
    return result

def summarize_outcomes(records: list[PaperRecord], target: int | None = None) -> dict[str, Any]:
    pdfs = unique_pdf_records(records)
    readers = [r for r in records if r.local_fulltext and Path(r.local_fulltext).is_file() and not is_verified_pdf(r)]
    attempted = [r for r in records if r.download_status != "pending" or r.attempts]
    total, tried = len(records), len(attempted)
    statuses = Counter(r.download_status for r in records)
    return {
        "record_count": total, "attempted_records": tried, "pending_records": total-tried,
        "pdf_count": len(pdfs), "pdf_downloaded": len(pdfs), "downloaded": len(pdfs),
        "fulltext_only_count": len(readers), "fulltext_only": len(readers),
        "readable_fulltext_count": len(pdfs)+len(readers), "readable_fulltext": len(pdfs)+len(readers),
        "download_rate": round(len(pdfs)/total, 4) if total else 0.0,
        "attempted_download_rate": round(len(pdfs)/tried, 4) if tried else 0.0,
        "readable_fulltext_rate": round((len(pdfs)+len(readers))/total, 4) if total else 0.0,
        "unverified_legacy_pdf_records": sum(r.download_status in PDF_STATUSES and r.identity_status != "verified" for r in records),
        "duplicate_pdf_records": statuses.get("duplicate_pdf", 0),
        "download_status_counts": dict(statuses),
        "article_type_counts": dict(Counter(article_type(r) for r in records)),
        "pdf_article_type_counts": dict(Counter(article_type(r) for r in pdfs)),
        "retrieval_type_counts": dict(Counter(r.retrieval_type or "unclassified" for r in pdfs+readers)),
        "target_pdfs": target, "target_met": len(pdfs) >= target if target else None,
        "task_completion_rate": min(len(pdfs)/target, 1.0) if target else None,
        "metric_note": "PDF counts require article identity verification and unique SHA-256. Article type is not SCIE indexing.",
    }
