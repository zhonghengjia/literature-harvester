"""Read-only diagnosis for an existing discovery, retrieval or content run."""
from collections import Counter
from pathlib import Path

from .content_store import load_document
from .downloader import sha256_file
from .outcomes import PDF_STATUSES
from .pipeline import load_manifest
from .reading import _candidate_map, read_json


ROUTE_ACTIONS = {
    "error": "Inspect provider errors or its retry deadline before rerunning the saved query in a fresh discovery.",
    "partial": "Preserve the existing hits. Check the failed page or missing identifiers before repeating the saved route.",
    "disabled": "Check whether this source should be enabled in the next discovery.",
    "missing_status": "Check the saved plan and capture the missing route outcome in a new run.",
    "quota_reached": "Review the route budget, provider ordering and exhaustion evidence before extending discovery.",
    "zero_results": "Inspect the executed query and its provider translation before changing scope.",
}
ATTEMPT_SUCCESS = {"downloaded", "already_downloaded", "response_received", "resolved", "fulltext_only", "extracted"}


def _search_checks(manifest):
    source_status = manifest.get("source_status", {})
    plan_ref = manifest.get("research_context", {}).get("query-plan.json")
    routes = read_json(plan_ref["path"])["routes"] if plan_ref else [
        {"id": key, "source": key} for key in source_status if key not in {"selection", "openalex_content"}]
    output = []
    for route in routes:
        outcome = source_status.get(route["id"], {})
        status = outcome.get("status", "missing_status")
        limit = outcome.get("effective_limit") or route.get("limit")
        count = outcome.get("count")
        exhausted = outcome.get("exhausted")
        if status == "ok":
            if count == 0:
                status = "zero_results"
            elif type(limit) is int and type(count) is int and count >= limit and exhausted is not True:
                status = "quota_reached"
        output.append({"route_id": route["id"], "source": route["source"], "status": status,
                       "count": count, "limit": limit, "exhausted": exhausted,
                       "skipped_rows": outcome.get("skipped_rows"),
                       "missing_requested_ids": outcome.get("missing_requested_ids", []),
                       "unexpected_response_ids": outcome.get("unexpected_response_ids", []),
                       "duplicate_response_ids": outcome.get("duplicate_response_ids", []),
                       "retry_at": outcome.get("retry_at"), "next_action": ROUTE_ACTIONS.get(status)})
    return output


def _pdf_check(record):
    status = record.download_status
    if status in PDF_STATUSES:
        if not record.local_pdf or not record.sha256 or record.identity_status != "verified":
            return "unverified_pdf", "Use resume to reverify identity and integrity in a fresh run."
        if not Path(record.local_pdf).is_file():
            return "missing_pdf", "Use resume to recover the missing local PDF."
        try:
            if sha256_file(Path(record.local_pdf)) != record.sha256:
                return "changed_pdf", "Preserve these bytes for review and use resume to seek a verified alternative."
        except OSError:
            return "unreadable_pdf", "Check local file access before resuming."
        return "verified_pdf", None
    if status == "duplicate_pdf":
        return status, None
    if status == "fulltext_only":
        if record.local_fulltext and Path(record.local_fulltext).is_file():
            return status, "Use content for a source-linked article reading package."
        return "missing_fulltext_reader", "Use content or resume to recover from an eligible source."
    if status in {"deferred", "rate_limited"}:
        return status, "Use resume with the recorded host deadlines, or inspect eligible alternatives."
    if status in {"checksum_conflict", "manual_review"}:
        return status, "Review the identity or digest discrepancy before counting or reusing the file."
    if status == "pending":
        return status, None
    return status, "Use content to check eligible alternate formats and inspect the saved attempt evidence."


def _content_checks(path, manifest_sha, candidate_ids):
    if not path:
        return {}, None
    index = read_json(path)
    if index.get("schema_version") != 1 or index.get("source_manifest_sha256") != manifest_sha:
        raise ValueError("Content index is unsupported or belongs to a different frozen manifest")
    results = {}
    for item in index["records"]:
        ident = item["candidate_id"]
        if ident not in candidate_ids or ident in results:
            raise ValueError("Content index contains unknown or duplicate candidate IDs")
        results[ident] = item
    if results.keys() != candidate_ids:
        raise ValueError("Content index does not cover the frozen selection")
    return results, sha256_file(Path(path))


def _content_check(item):
    if item is None:
        return {"status": "not_checked"}
    status = item["status"]
    if status != "extracted":
        return {"status": status, "next_action": (
            "Continue content with the saved index in a fresh output directory."
            if status == "pending_budget" else
            "Inspect content attempts before trying another eligible source or verified local input.")}
    try:
        path = Path(item["document"])
        if sha256_file(path) != item["document_sha256"]:
            raise ValueError("Document digest differs")
        document, _ = load_document(path)
    except (KeyError, TypeError, ValueError, OSError):
        return {"status": "content_integrity_error", "next_action":
                "Restore the original artifact or run content into a fresh directory without this index."}
    gaps = sorted({gap["reason"] for gap in document["gaps"]})
    actions = []
    if "page_limit" in gaps:
        actions.append("content-next")
    if document.get("asset_summary", {}).get("pending"):
        actions.append("assets-resume")
    if gaps and not actions:
        actions.append("review_extraction_gaps")
    return {"status": "extracted_with_gaps" if gaps else "extracted", "document": str(path),
            "format": document.get("format"), "body_text_chars": document["coverage"].get("body_text_chars"),
            "gap_codes": gaps, "next_actions": actions}


def check_run(manifest_path, content_index=None):
    manifest_path = Path(manifest_path).resolve()
    manifest, records = load_manifest(manifest_path)
    manifest_sha = sha256_file(manifest_path)
    by_id = _candidate_map(records)
    indexed, index_sha = _content_checks(content_index, manifest_sha, by_id.keys())
    routes = _search_checks(manifest)
    findings, issue_stats = [], Counter()
    for ident, record in by_id.items():
        pdf_status, action = _pdf_check(record)
        content = _content_check(indexed.get(ident))
        attempt_counts = Counter((str(a.get("stage", "")), str(a.get("outcome", "")), str(a.get("source", "")))
                                 for a in record.attempts if a.get("outcome") not in ATTEMPT_SUCCESS)
        if pdf_status not in {"verified_pdf", "duplicate_pdf", "fulltext_only", "pending"}:
            issue_stats.update(attempt_counts)
        findings.append({"candidate_id": ident, "title": record.title,
                         "doi": record.doi, "pmid": record.pmid,
                         "pdf_status": pdf_status, "pdf_next_action": action, "retry_at": record.retry_at or None,
                         "download_attempt_diagnostics": [
                             {"stage": key[0], "outcome": key[1], "source": key[2], "attempts": count}
                             for key, count in sorted(attempt_counts.items())],
                         "content": content})
    selection = manifest.get("source_status", {}).get("selection", {})
    return {
        "schema_version": 1, "source_manifest": str(manifest_path), "source_manifest_sha256": manifest_sha,
        "content_index_sha256": index_sha, "selected_records": len(records),
        "discovered_records": selection.get("deduplicated_records"),
        "outside_selected_queue": selection.get("outside_selected_cap"),
        "search_routes": routes, "search_status_counts": dict(Counter(row["status"] for row in routes)),
        "pdf_status_counts": dict(Counter(row["pdf_status"] for row in findings)),
        "content_status_counts": dict(Counter(row["content"]["status"] for row in findings)),
        "failed_pdf_attempt_counts": [{"stage": key[0], "outcome": key[1], "source": key[2], "attempts": count}
                                      for key, count in sorted(issue_stats.items())],
        "host_retry_at": manifest.get("host_retry_at", {}),
        "records": findings,
    }
