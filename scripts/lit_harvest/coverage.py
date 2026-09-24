"""Retrieval-coverage diagnostics.

The pipeline already records why a single download failed. This module answers
the aggregate question instead: across a whole run, where is full text being
lost, and which of the fixable causes accounts for the most loss?

Every bucket in ``recoverable`` names a specific, legal remedy. A record that is
flagged ``landing_page_only`` is not paywalled -- a source asserted open access
but only handed over an HTML page, so the PDF link was never extracted.
"""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .models import PaperRecord
from .outcomes import collect_population, summarize_outcomes, unique_pdf_records, is_verified_pdf


PDF_STATUSES = {"downloaded", "already_downloaded"}
FULLTEXT_STATUSES = {"fulltext_only"}
SUCCESS_STATUSES = PDF_STATUSES | FULLTEXT_STATUSES

# Each bucket maps to a concrete remedy rather than a generic "try harder".
RECOVERABLE_GUIDANCE = {
    "landing_page_only": (
        "A source reported open access but exposed no direct PDF URL. "
        "Resolve the landing page and read its citation_pdf_url metadata."
    ),
    "single_candidate": (
        "Only one candidate URL existed, so a single failure lost the record. "
        "Check the recorded fallback-source status and retry unavailable providers later."
    ),
    "pmc_without_pdf": (
        "A PMC identifier exists but no PDF was retrieved. "
        "Fall back to the Europe PMC JATS full-text XML."
    ),
    "oa_but_failed": (
        "A source asserted open access and supplied candidates, yet all attempts failed. "
        "Inspect the host breakdown; often a request-header or redirect problem."
    ),
    "doi_without_candidate": (
        "A DOI exists but no OA location was found by any enabled source. "
        "Add aggregator sources (CORE, OpenAIRE, DOAJ) and preprint title matching."
    ),
    "closed_no_route": (
        "No verified access route was found in the enabled sources; this is not proof of a paywall. "
        "route them to author request or interlibrary loan."
    ),
}


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").casefold()
    except ValueError:
        return ""


def _candidate_sources(record: PaperRecord) -> set[str]:
    return {str(candidate.get("source", "")) for candidate in record.pdf_candidates if candidate.get("source")}


def _classify_recoverable(record: PaperRecord) -> str:
    """Bucket one failed record by the cheapest remedy that could rescue it."""
    if record.pmcid:
        return "pmc_without_pdf"
    if record.is_oa and not record.pdf_candidates:
        return "landing_page_only"
    if record.is_oa and record.pdf_candidates:
        return "single_candidate" if len(record.pdf_candidates) == 1 else "oa_but_failed"
    if record.doi:
        return "doi_without_candidate"
    return "closed_no_route"


def build_coverage(manifest: dict[str, Any], records: list[PaperRecord]) -> dict[str, Any]:
    """Use the same frozen population and verified denominators as the CLI."""
    population = collect_population(manifest, records)
    metrics = summarize_outcomes(population, manifest.get("settings", {}).get("target_pdfs"))
    pdfs = unique_pdf_records(population)
    failed = [r for r in population if not is_verified_pdf(r)
              and r.download_status not in {"pending", "duplicate_pdf"}]
    offered, reached, won = Counter(), Counter(), Counter()
    for record in population:
        for candidate in record.pdf_candidates:
            offered[str(candidate.get("source") or "unknown")] += 1
        reached.update(_candidate_sources(record))
    won.update(r.download_source for r in pdfs if r.download_source)
    effectiveness = {
        source: {"candidate_urls_offered": offered[source], "records_reached": reached[source],
                 "downloads_won": won[source],
                 "win_rate_of_reached": round(won[source]/reached[source], 4) if reached[source] else 0.0}
        for source in sorted(set(offered) | set(reached) | set(won))
    }
    failing_hosts, buckets, examples = Counter(), Counter(), {}
    for record in failed:
        urls = [item.get("url", "") for item in record.attempts
                if item.get("outcome") not in {"response_received", "resolved", "downloaded", "already_downloaded"}]
        if not record.attempts:
            urls = [item.get("url", "") for item in record.pdf_candidates]
        failing_hosts.update({host for url in urls if (host := _host(url))})
        bucket = _classify_recoverable(record)
        buckets[bucket] += 1
        examples.setdefault(bucket, [])
        if len(examples[bucket]) < 3:
            examples[bucket].append(record.doi or record.pmid or record.title[:80])
    total = len(population)
    return {
        **metrics, "query": manifest.get("query", ""), "run_id": manifest.get("run_id", ""),
        "records_with_candidates": sum(bool(r.pdf_candidates) for r in population),
        "records_without_candidates": sum(not r.pdf_candidates for r in population),
        "mean_candidates_per_record": round(sum(len(r.pdf_candidates) for r in population)/total, 3) if total else 0.0,
        "oa_status_counts": dict(Counter(r.oa_status or "unknown" for r in population)),
        "source_effectiveness": effectiveness,
        "source_effectiveness_note": "Ordered winning source, not marginal yield from an ablation experiment.",
        "failing_hosts": dict(failing_hosts.most_common(15)), "recoverable": dict(buckets),
        "recoverable_examples": examples, "source_status": manifest.get("source_status", {}),
    }


def merge_coverage(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine several single-run reports into one benchmark summary."""
    if not reports:
        return {"record_count": 0, "downloaded": 0, "download_rate": 0.0, "runs": 0}
    total = sum(report["record_count"] for report in reports)
    downloaded = sum(report["downloaded"] for report in reports)
    fulltext_only = sum(report.get("fulltext_only", 0) for report in reports)
    readable_fulltext = downloaded + fulltext_only

    def _sum_counter(key: str) -> dict[str, int]:
        merged: Counter[str] = Counter()
        for report in reports:
            merged.update(report.get(key, {}))
        return dict(merged.most_common())

    source_effectiveness: dict[str, dict[str, Any]] = {}
    for report in reports:
        for source, stats in report.get("source_effectiveness", {}).items():
            target = source_effectiveness.setdefault(
                source,
                {"candidate_urls_offered": 0, "records_reached": 0, "downloads_won": 0},
            )
            for key in ("candidate_urls_offered", "records_reached", "downloads_won"):
                target[key] += stats.get(key, 0)
    for stats in source_effectiveness.values():
        reached = stats["records_reached"]
        stats["win_rate_of_reached"] = round(stats["downloads_won"] / reached, 4) if reached else 0.0

    return {
        "runs": len(reports),
        "queries": [report.get("query", "") for report in reports],
        "record_count": total,
        "downloaded": downloaded,
        "pdf_downloaded": downloaded,
        "fulltext_only": fulltext_only,
        "readable_fulltext": readable_fulltext,
        "download_rate": round(downloaded / total, 4) if total else 0.0,
        "readable_fulltext_rate": round(readable_fulltext / total, 4) if total else 0.0,
        "retrieval_type_counts": _sum_counter("retrieval_type_counts"),
        "download_status_counts": _sum_counter("download_status_counts"),
        "oa_status_counts": _sum_counter("oa_status_counts"),
        "failing_hosts": dict(Counter(_sum_counter("failing_hosts")).most_common(15)),
        "recoverable": _sum_counter("recoverable"),
        "source_effectiveness": dict(
            sorted(source_effectiveness.items(), key=lambda item: -item[1]["downloads_won"])
        ),
        "per_run": [
            {
                "query": report.get("query", ""),
                "record_count": report["record_count"],
                "downloaded": report["downloaded"],
                "download_rate": report["download_rate"],
            }
            for report in reports
        ],
    }


def render_coverage_markdown(report: dict[str, Any]) -> str:
    total = report.get("record_count", 0)
    lines = [
        "# Retrieval coverage diagnostic",
        "",
        f"- Frozen candidate records: {total}",
        f"- Attempted / pending: {report.get('attempted_records', 'not aggregated')} / {report.get('pending_records', 'not aggregated')}",
        f"- PDFs retrieved: {report.get('pdf_downloaded', report.get('downloaded', 0))} "
        f"({report.get('download_rate', 0.0) * 100:.1f}%)",
        f"- Readable full text: {report.get('readable_fulltext', report.get('downloaded', 0))} "
        f"({report.get('readable_fulltext_rate', report.get('download_rate', 0.0)) * 100:.1f}%)",
    ]
    if report.get("fulltext_only"):
        lines.append(
            f"  - JATS-derived Markdown readers (not PDFs): {report['fulltext_only']}"
        )
    if "runs" in report:
        lines.append(f"- Runs aggregated: {report['runs']} (run-record totals; not deduplicated across queries)")
    else:
        lines.extend(
            [
                f"- Records with at least one candidate URL: {report.get('records_with_candidates', 0)}",
                f"- Mean candidate URLs per record: {report.get('mean_candidates_per_record', 0.0)}",
            ]
        )
    lines.append("")

    if report.get("per_run"):
        lines.extend(["## Per query", "", "| Query | Records | PDFs | Rate |", "|---|---:|---:|---:|"])
        for entry in report["per_run"]:
            lines.append(
                f"| {entry['query']} | {entry['record_count']} | {entry['downloaded']} | "
                f"{entry['download_rate'] * 100:.1f}% |"
            )
        lines.append("")

    lines.extend(["## Where full text is lost", "", "| Bucket | Records | Remedy |", "|---|---:|---|"])
    for bucket, count in sorted(report.get("recoverable", {}).items(), key=lambda item: -item[1]):
        lines.append(f"| `{bucket}` | {count} | {RECOVERABLE_GUIDANCE.get(bucket, '')} |")
    lines.append("")

    lines.extend(
        [
            "## Source effectiveness",
            "",
            "`records_reached` counts records this source supplied at least one candidate for. "
            "`downloads_won` counts unique verified PDFs won by the ordered route, not marginal source yield.",
            "",
            "| Source | Candidate URLs | Records reached | Downloads won | Win rate |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for source, stats in report.get("source_effectiveness", {}).items():
        lines.append(
            f"| {source} | {stats['candidate_urls_offered']} | {stats['records_reached']} | "
            f"{stats['downloads_won']} | {stats['win_rate_of_reached'] * 100:.1f}% |"
        )
    lines.append("")

    if report.get("download_status_counts"):
        lines.extend(["## Download status", "", "| Status | Records |", "|---|---:|"])
        for status, count in sorted(report["download_status_counts"].items(), key=lambda item: -item[1]):
            lines.append(f"| `{status}` | {count} |")
        lines.append("")

    if report.get("failing_hosts"):
        lines.extend(
            [
                "## Hosts on failed records",
                "",
                "A host here served a candidate for a record that ultimately failed. "
                "A high count is a signal to inspect that host's response, not to force it.",
                "",
                "| Host | Failed records |",
                "|---|---:|",
            ]
        )
        for host, count in report["failing_hosts"].items():
            lines.append(f"| {host} | {count} |")
        lines.append("")

    examples = report.get("recoverable_examples", {})
    if examples:
        lines.extend(["## Examples per bucket", ""])
        for bucket, items in examples.items():
            lines.append(f"- `{bucket}`: " + "; ".join(items))
        lines.append("")

    return "\n".join(lines)


def write_coverage(run_dir: Path, manifest: dict[str, Any], records: list[PaperRecord]) -> dict[str, Any]:
    report = build_coverage(manifest, records)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "coverage.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "coverage.md").write_text(render_coverage_markdown(report), encoding="utf-8")
    return report
