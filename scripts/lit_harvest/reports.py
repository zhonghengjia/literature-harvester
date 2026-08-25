from __future__ import annotations

import csv
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote

from .models import PaperRecord


def _md(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def _bib_key(record: PaperRecord, index: int) -> str:
    author = re.sub(r"\W+", "", record.authors[0].split()[-1] if record.authors else "paper")
    return f"{author}{record.year or 'nd'}_{index}"


def _bib_escape(value: str) -> str:
    return value.replace("\\", "\\textbackslash ").replace("{", "\\{").replace("}", "\\}")


def write_reports(run_dir: Path, manifest: dict[str, Any], records: list[PaperRecord]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    fields = [
        "title",
        "authors",
        "year",
        "journal",
        "doi",
        "pmid",
        "pmcid",
        "arxiv_id",
        "citation_count",
        "is_oa",
        "oa_status",
        "license",
        "sources",
        "download_status",
        "local_pdf",
        "download_source",
        "url",
        "failure_reason",
    ]
    with (run_dir / "literature.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            row = record.to_dict()
            row["authors"] = "; ".join(record.authors)
            row["sources"] = "; ".join(record.sources)
            writer.writerow({field: row.get(field, "") for field in fields})

    with (run_dir / "metadata.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")

    downloaded = sum(record.download_status in {"downloaded", "already_downloaded"} for record in records)
    lines = [
        f"# Literature inventory: {manifest.get('query', '')}",
        "",
        f"- Run ID: `{manifest.get('run_id', '')}`",
        f"- Records: {len(records)}",
        f"- PDFs available locally: {downloaded}",
        "",
        "| # | Title | Year | Journal | DOI/ID | OA | Download | Zotero | Sources |",
        "|---:|---|---:|---|---|---|---|---|---|",
    ]
    for index, record in enumerate(records, 1):
        identifier = record.doi or record.pmid or record.pmcid or record.arxiv_id
        lines.append(
            f"| {index} | {_md(record.title)} | {_md(record.year)} | {_md(record.journal)} | "
            f"{_md(identifier)} | {_md(record.oa_status or record.is_oa)} | {_md(record.download_status)} | "
            f"{_md(record.zotero.get('status') or record.zotero.get('action'))} | {_md(', '.join(record.sources))} |"
        )
    lines.extend(["", "## Record details", ""])
    for index, record in enumerate(records, 1):
        lines.extend(
            [
                f"### {index}. {record.title}",
                "",
                f"- Authors: {'; '.join(record.authors) or 'Not available'}",
                f"- Journal/year: {record.journal or 'Not available'} / {record.year or 'Not available'}",
                f"- DOI: {record.doi or 'Not available'}",
                f"- PMID/PMCID/arXiv: {record.pmid or '-'} / {record.pmcid or '-'} / {record.arxiv_id or '-'}",
                f"- Local PDF: {record.local_pdf or 'Not downloaded'}",
                "",
                record.abstract or "Abstract not available from the searched sources.",
                "",
            ]
        )
    (run_dir / "literature.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    bib_entries: list[str] = []
    for index, record in enumerate(records, 1):
        entry_type = "article" if record.item_type == "journalArticle" else "misc"
        values = {
            "title": record.title,
            "author": " and ".join(record.authors),
            "year": str(record.year or ""),
            "journal": record.journal,
            "doi": record.doi,
            "url": record.url,
        }
        body = ",\n".join(
            f"  {key} = {{{_bib_escape(value)}}}" for key, value in values.items() if value
        )
        bib_entries.append(f"@{entry_type}{{{_bib_key(record, index)},\n{body}\n}}")
    (run_dir / "references.bib").write_text("\n\n".join(bib_entries) + "\n", encoding="utf-8")

    attempted_failures = [
        PaperRecord.from_dict(item) for item in manifest.get("download_attempts", [])
    ]
    write_failure_report(run_dir, records + attempted_failures)
    write_zotero_plan(run_dir, records)
    write_run_summary(run_dir, manifest, records)


def write_failure_report(run_dir: Path, records: list[PaperRecord]) -> None:
    failed = [record for record in records if record.download_status not in {"downloaded", "already_downloaded"}]
    lines = [
        "# PDF retrieval follow-up",
        "",
        "Only legal access routes are listed. A missing PDF is not evidence that no accessible version exists.",
        "",
    ]
    for index, record in enumerate(failed, 1):
        query = quote(record.doi or record.title)
        status_guidance = {
            "no_oa_version": "No verified OA location was reported by the automated sources.",
            "license_unknown": "A possible full-text link exists, but its reuse/access license could not be verified.",
            "anti_bot": "The host rejected unattended access; use its normal browser page without bypassing controls.",
            "rate_limited": "The source was temporarily rate-limited; retry later or configure its official API key.",
            "download_timeout": "The host kept the connection open too long without completing the PDF; retry later or use the record page.",
            "dead_link": "The reported OA location is no longer available at that URL.",
            "not_pdf": "The candidate returned HTML, an error page, or an invalid PDF.",
            "too_large": "The file or repository package exceeded the configured safety limit.",
        }.get(record.download_status, "The automated legal OA routes did not return a usable PDF.")
        lines.extend(
            [
                f"## {index}. {record.title}",
                "",
                f"- Status: `{record.download_status}`",
                f"- Reason: {record.failure_reason or 'No verified OA PDF candidate'}",
                f"- Interpretation: {status_guidance}",
            ]
        )
        if record.doi:
            lines.append(f"- DOI landing page: https://doi.org/{record.doi}")
        if record.pmid:
            lines.append(f"- PubMed: https://pubmed.ncbi.nlm.nih.gov/{record.pmid}/")
        if record.pmcid:
            lines.append(f"- Europe PMC: https://europepmc.org/articles/{record.pmcid}")
        if record.url:
            lines.append(f"- Known article/record page: {record.url}")
        lines.extend(
            [
                f"- Europe PMC search: https://europepmc.org/search?query={query}",
                f"- CORE search: https://core.ac.uk/search?q={query}",
                f"- Google Scholar manual search: https://scholar.google.com/scholar?q={query}",
                "- China OA fallback: https://pubscholar.cn/ and https://opensign.lib.tsinghua.edu.cn/",
                "- OA browser check: install/use Unpaywall on the DOI or publisher page: https://unpaywall.org/products/extension",
                "- Library route: institutional link resolver, CALIS/CASHL or another interlibrary-loan/document-delivery service available to you.",
                "- Author route: search the corresponding author's institutional repository or profile, then request an author-accepted manuscript.",
                "",
                "Author-request template:",
                "",
                f"> Dear Dr. [Corresponding author], I am conducting personal academic research on [topic]. "
                f"Would you be willing to share an author-accepted copy of “{record.title}” for scholarly use? Thank you.",
                "",
            ]
        )
    if not failed:
        lines.append("All records with verified candidates were downloaded successfully.\n")
    (run_dir / "failed_downloads.md").write_text("\n".join(lines), encoding="utf-8")


def write_zotero_plan(run_dir: Path, records: list[PaperRecord]) -> None:
    planned = [record for record in records if record.zotero]
    path = run_dir / "zotero_plan.md"
    if not planned:
        if path.exists():
            path.unlink()
        return
    lines = [
        "# Zotero import plan",
        "",
        "This file is a preview. Its presence does not mean a Zotero write occurred.",
        "",
        "| # | Action/status | Title | Match reason | Match/item key | PDF |",
        "|---:|---|---|---|---|---|",
    ]
    for index, record in enumerate(planned, 1):
        zotero = record.zotero
        key = zotero.get("item_key") or zotero.get("match_key") or ""
        if key and zotero.get("status", "") in {
            "imported_with_pdf",
            "imported_without_pdf",
            "attached_pdf_to_existing",
        }:
            key_text = f"[{key}](zotero://select/library/items/{key})"
        else:
            key_text = str(key)
        lines.append(
            f"| {index} | {_md(zotero.get('status') or zotero.get('action'))} | {_md(record.title)} | "
            f"{_md(zotero.get('match_reason'))} | {_md(key_text)} | {_md(record.local_pdf)} |"
        )
        if zotero.get("error"):
            lines.extend(["", f"Error for item {index}: `{_md(zotero['error'])}`", ""])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_run_summary(run_dir: Path, manifest: dict[str, Any], records: list[PaperRecord]) -> None:
    status_counts: dict[str, int] = {}
    zotero_counts: dict[str, int] = {}
    for record in records:
        status_counts[record.download_status] = status_counts.get(record.download_status, 0) + 1
        action = str(record.zotero.get("status") or record.zotero.get("action") or "not_planned")
        zotero_counts[action] = zotero_counts.get(action, 0) + 1
    summary = {
        "run_id": manifest.get("run_id"),
        "query": manifest.get("query"),
        "record_count": len(records),
        "download_status_counts": status_counts,
        "zotero_status_counts": zotero_counts,
        "source_status": manifest.get("source_status", {}),
        "target_pdfs": manifest.get("settings", {}).get("target_pdfs"),
        "target_met": manifest.get("settings", {}).get("target_met"),
        "failed_candidate_attempts": len(manifest.get("download_attempts", [])),
    }
    (run_dir / "run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
