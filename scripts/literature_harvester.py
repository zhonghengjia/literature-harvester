#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from lit_harvest.pipeline import (
    load_config,
    load_manifest,
    run_pipeline,
    resume_pipeline,
    save_manifest,
)
from lit_harvest.coverage import (
    build_coverage,
    merge_coverage,
    render_coverage_markdown,
)
from lit_harvest.downloader import DownloadError, validate_pdf
from lit_harvest.downloader import sha256_file
from lit_harvest.identity import verify_pdf_identity
from lit_harvest.outcomes import ARTICLE_TYPES, summarize_outcomes
from lit_harvest.reports import write_reports
from lit_harvest.zotero import (
    ZoteroError,
    ZoteroLocalClient,
    find_collection_key,
    import_records,
    plan_import,
)


def _common_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="TOML configuration file")
    parser.add_argument("--zotero-base-url", help="Override the loopback Zotero API URL")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="literature-harvester",
        description="Legal OA literature discovery, download, reporting, and Zotero import",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser("discover", help="Execute a bounded query plan; preserve all candidates and select a reading queue")
    discover.add_argument("--plan", required=True, help="Validated query-plan JSON")
    discover.add_argument("--queue-size", type=int, default=25)
    discover.add_argument("--article-type", action="append", choices=sorted(ARTICLE_TYPES), default=[])
    discover.add_argument("--output-dir", required=True)
    discover.add_argument("--download", action="store_true", help="Also run the existing permitted PDF acquisition for the selected queue")
    _common_config(discover)
    content = subparsers.add_parser("content", help="Prepare source-linked article content without modifying the frozen manifest or PDF metrics")
    content.add_argument("--manifest", required=True)
    content.add_argument("--output-dir", required=True)
    content.add_argument("--max-records", type=int, default=25)
    content.add_argument("--local-only", action="store_true", help="No network; parse only identity/hash-verified local PDFs")
    _common_config(content)
    screen = subparsers.add_parser("screen", help="Record project-specific screening decisions without deleting candidates")
    screen.add_argument("--manifest", required=True)
    screen.add_argument("--decisions", required=True)
    screen.add_argument("--output-dir", required=True)
    _common_config(screen)
    reading = subparsers.add_parser("reading-pack", help="Prepare ordered source chunks; preparation is not AI reading")
    reading.add_argument("--document", required=True)
    reading.add_argument("--question", required=True)
    reading.add_argument("--max-chars", type=int, default=24000)
    reading.add_argument("--start-chunk", type=int, default=0)
    reading.add_argument("--output-dir", required=True)
    _common_config(reading)
    receipt = subparsers.add_parser("reading-receipt", help="Verify reported reading evidence against exact source chunks")
    receipt.add_argument("--pack", required=True)
    receipt.add_argument("--receipt", required=True)
    receipt.add_argument("--output-dir", required=True)
    _common_config(receipt)

    run = subparsers.add_parser("run", help="Search, deduplicate, download OA PDFs, and write reports")
    run.add_argument("--query", required=True, help="Research topic, title, DOI, PMID, PMCID, or arXiv ID")
    run.add_argument("--max-results", type=int, help="Maximum merged records")
    run.add_argument("--max-candidates", type=int, help="Maximum records discovered per source before filtering")
    run.add_argument("--article-type", action="append", choices=sorted(ARTICLE_TYPES), default=[],
                     help="Explicit metadata type; repeat for OR. This does not certify SCIE indexing")
    run.add_argument("--output-dir", help="Exact output directory for this run")
    run.add_argument("--no-download", action="store_true", help="Create a metadata-only run")
    run.add_argument(
        "--target-pdfs",
        type=int,
        help="Keep trying ranked candidates until this many legal OA PDFs are downloaded",
    )
    run.add_argument(
        "--required-term",
        action="append",
        default=[],
        help="Keep only records whose title, abstract, or DOI contains this term; repeat for OR synonyms",
    )
    _common_config(run)
    resume = subparsers.add_parser("resume", help="Recheck and retry a frozen manifest into a fresh output directory")
    resume.add_argument("--manifest", required=True)
    resume.add_argument("--output-dir")
    _common_config(resume)

    subparsers.add_parser("browser-status", help="Show supported manual browser handoff capabilities")
    queue = subparsers.add_parser("browser-queue", help="Return permitted manual routes without opening a browser")
    queue.add_argument("--manifest", required=True)
    local = subparsers.add_parser("browser-ingest", help="Verify one user-downloaded file and original-source evidence")
    local.add_argument("--manifest", required=True)
    local.add_argument("--record-id", required=True, help="Exact namespaced DOI, PMID, PMCID or arXiv ID")
    local.add_argument("--pdf", required=True)
    local.add_argument("--source-url", required=True)
    local.add_argument("--access-basis-json", required=True, help="Path to user-confirmed original-source evidence JSON")

    diagnose = subparsers.add_parser(
        "diagnose",
        help="Benchmark retrieval coverage across several queries and report where full text is lost",
    )
    diagnose.add_argument(
        "--query",
        action="append",
        required=True,
        dest="queries",
        help="Benchmark query; repeat to average over several topics",
    )
    diagnose.add_argument("--max-results", type=int, default=25, help="Records per query")
    diagnose.add_argument(
        "--output-dir",
        required=True,
        help="Directory for the benchmark report and its per-query runs",
    )
    diagnose.add_argument(
        "--label",
        default="baseline",
        help="Name for this benchmark, e.g. baseline or after-fixes",
    )
    _common_config(diagnose)

    status = subparsers.add_parser("zotero-status", help="Check the Zotero 10+ local API")
    _common_config(status)

    plan = subparsers.add_parser("zotero-plan", help="Preview deduplicated Zotero actions without writing")
    plan.add_argument("--manifest", required=True)
    plan.add_argument("--collection", default="")
    plan.add_argument(
        "--pdf-only",
        action="store_true",
        help="Plan only records with an existing, validated downloaded PDF",
    )
    _common_config(plan)

    ingest = subparsers.add_parser("zotero-import", help="Execute a previously reviewed Zotero import plan")
    ingest.add_argument("--manifest", required=True)
    ingest.add_argument("--collection", default="")
    ingest.add_argument("--create-collection", action="store_true")
    ingest.add_argument(
        "--pdf-only",
        action="store_true",
        help="Import only records with an existing, validated downloaded PDF",
    )
    ingest.add_argument(
        "--confirm-write",
        action="store_true",
        help="Required acknowledgement that this command mutates the local Zotero library",
    )
    _common_config(ingest)
    return parser


def _client(args: argparse.Namespace, config: dict[str, Any]) -> ZoteroLocalClient:
    base_url = args.zotero_base_url or config["zotero"]["base_url"]
    return ZoteroLocalClient(base_url=base_url)


def command_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    max_results = args.max_results if args.max_results is not None else int(config["general"]["max_results"])
    if args.target_pdfs is not None:
        if args.no_download:
            raise ValueError("--target-pdfs cannot be combined with --no-download")
        if args.target_pdfs < 1 or args.target_pdfs > 100:
            raise ValueError("--target-pdfs must be between 1 and 100")
        if args.max_results is None:
            max_results = max(args.target_pdfs, int(config["general"]["max_candidates"]))
    if not 1 <= max_results <= 10000:
        raise ValueError("--max-results must be between 1 and 10000")
    run_dir, manifest, records = run_pipeline(
        args.query,
        config,
        max_results,
        args.output_dir,
        not args.no_download,
        args.target_pdfs,
        args.required_term,
        article_types=args.article_type,
        max_candidates=args.max_candidates,
    )
    write_reports(run_dir, manifest, records)
    result = {
        "run_dir": str(run_dir),
        "manifest": str(run_dir / "manifest.json"),
        "records": len(records),
        **summarize_outcomes(records, args.target_pdfs),
        "source_status": manifest.get("source_status", {}),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["target_met"] is False else 0


def command_research(args: argparse.Namespace) -> int:
    from lit_harvest.reading import read_json, prepare_content, screen_candidates, prepare_reading, verify_receipt
    from lit_harvest.query_plan import validate_plan
    config = load_config(args.config)
    if args.command == "discover":
        plan = validate_plan(read_json(args.plan))
        directory, manifest, records = run_pipeline(plan["question"], config, args.queue_size,
            args.output_dir, args.download, article_types=args.article_type, query_plan=plan)
        write_reports(directory, manifest, records)
        states = manifest["source_status"]
        result = {"run_dir": str(directory), "manifest": str(directory / "manifest.json"),
                  "selected_records": len(records), "candidate_records": len(records.candidates),
                  "source_status": states, "research_context": manifest["research_context"]}
        failed = any(states[r["id"]]["status"] in {"error", "partial", "disabled"} for r in plan["routes"])
    elif args.command == "content":
        directory, index = prepare_content(args.manifest, args.output_dir, config,
            local_only=args.local_only, max_records=args.max_records)
        result = {"run_dir": str(directory), "content_index": str(directory / "content-index.json"), **index["metrics"]}
        failed = any(r["status"] != "extracted" for r in index["records"])
    elif args.command == "screen":
        directory, ledger = screen_candidates(args.manifest, args.decisions, args.output_dir, config)
        result = {"ledger": str(directory / "screening.json"), "manifest": str(directory / "manifest.json"),
                  "selected_for_reading": len(ledger["included_ids"]), "decisions": len(ledger["decisions"]), "unreviewed": len(ledger["unreviewed_ids"])}
        failed = False
    elif args.command == "reading-pack":
        directory, pack = prepare_reading(args.document, args.output_dir, config, question=args.question,
            max_chars=args.max_chars, start_chunk=args.start_chunk)
        result = {"pack": str(directory / "reading-pack.json"), "reader": str(directory / "reading-pack.md"),
                  "state": pack["state"], "chunks": len(pack["chunks"]), "next_start_chunk": pack["next_start_chunk"],
                  "remaining_chunks": pack["remaining_chunks"]}
        failed = False
    else:
        directory, receipt = verify_receipt(args.pack, args.receipt, args.output_dir, config)
        result = {"receipt": str(directory / "reading-receipt.json"), "state": receipt["state"],
                  "article_text_coverage": receipt["article_text_coverage"], "semantic_accuracy_verified": False}
        failed = False
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if failed else 0


def command_resume(args: argparse.Namespace) -> int:
    run_dir, manifest, records = resume_pipeline(args.manifest, load_config(args.config), args.output_dir)
    write_reports(run_dir, manifest, records)
    result = {"run_dir": str(run_dir), "manifest": str(run_dir / "manifest.json"),
              **summarize_outcomes(records, manifest["settings"].get("target_pdfs"))}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["target_met"] is False else 0


def command_browser(args: argparse.Namespace) -> int:
    from lit_harvest.browser import browser_status, queue_records, select_record, ingest_local_pdf
    if args.command == "browser-status":
        print(json.dumps(browser_status(), ensure_ascii=False, indent=2))
        return 0
    manifest, records = load_manifest(args.manifest)
    if args.command == "browser-queue":
        print(json.dumps(queue_records(records), ensure_ascii=False, indent=2))
        return 0
    record = select_record(records, args.record_id)
    evidence = json.loads(Path(args.access_basis_json).expanduser().read_text(encoding="utf-8"))
    manifest_path = Path(args.manifest).expanduser().resolve()
    result = ingest_local_pdf(record, Path(args.pdf), manifest_path.parent / "pdfs",
                              args.source_url, evidence, records)
    manifest["schema_version"] = 2
    manifest.pop("download_attempts", None)
    manifest["records"] = [item.to_dict() for item in records]
    manifest["metrics"] = summarize_outcomes(records, manifest.get("settings", {}).get("target_pdfs"))
    manifest.setdefault("settings", {})["target_met"] = manifest["metrics"]["target_met"]
    save_manifest(manifest_path, manifest)
    write_reports(manifest_path.parent, manifest, records)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["outcome"] in {"downloaded", "already_downloaded"} else 2


def command_diagnose(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if not 1 <= args.max_results <= 500:
        raise ValueError("--max-results must be between 1 and 500")
    root = Path(args.output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    reports: list[dict[str, Any]] = []
    for index, query in enumerate(args.queries, 1):
        run_dir, manifest, records = run_pipeline(
            query,
            config,
            args.max_results,
            str(root / f"{args.label}_{index:02d}"),
            download=True,
        )
        write_reports(run_dir, manifest, records)
        reports.append(build_coverage(manifest, records))

    benchmark = merge_coverage(reports)
    benchmark["label"] = args.label
    (root / f"benchmark_{args.label}.json").write_text(
        json.dumps(benchmark, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (root / f"benchmark_{args.label}.md").write_text(
        render_coverage_markdown(benchmark), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "label": args.label,
                "records": benchmark["record_count"],
                "downloaded": benchmark["downloaded"],
                "download_rate": benchmark["download_rate"],
                "recoverable": benchmark["recoverable"],
                "report": str(root / f"benchmark_{args.label}.md"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def command_zotero_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    status = _client(args, config).status()
    print(json.dumps(status.to_dict(), ensure_ascii=False, indent=2))
    return 0 if status.reachable else 2


def _select_zotero_records(records: list[Any], pdf_only: bool) -> list[Any]:
    if not pdf_only:
        return records
    selected: list[Any] = []
    for record in records:
        if record.download_status not in {"downloaded", "already_downloaded"} or not record.local_pdf:
            continue
        path = Path(record.local_pdf)
        if not path.is_file():
            continue
        try:
            validate_pdf(path)
            identity = verify_pdf_identity(path, record)
            checksum = sha256_file(path)
            if identity.status != "verified" or (record.sha256 and record.sha256 != checksum):
                continue
        except (DownloadError, OSError):
            continue
        record.identity_status = "verified"
        record.sha256 = checksum
        record.extra["pdf_identity"] = identity.to_dict()
        selected.append(record)
    return selected


def command_zotero_plan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    client = _client(args, config)
    status = client.status()
    if not status.reachable:
        raise ZoteroError(status.message)
    manifest, all_records = load_manifest(args.manifest)
    records = _select_zotero_records(all_records, args.pdf_only)
    if not records:
        raise ZoteroError("No records matched the requested Zotero import filter")
    if args.pdf_only:
        selected_ids = {id(record) for record in records}
        for record in all_records:
            if id(record) not in selected_ids:
                record.zotero = {}
    counts = plan_import(records, client.get_items())
    collection_name = args.collection or config["zotero"].get("collection", "")
    collection_key = find_collection_key(client.get_collections(), collection_name)
    manifest["records"] = [record.to_dict() for record in all_records]
    manifest_path = Path(args.manifest).expanduser().resolve()
    save_manifest(manifest_path, manifest)
    write_reports(manifest_path.parent, manifest, all_records)
    option = " --pdf-only" if args.pdf_only else ""
    preview = {
        "write_performed": False,
        "actions": counts,
        "selected_records": len(records),
        "excluded_records": len(all_records) - len(records),
        "pdf_only": bool(args.pdf_only),
        "collection": collection_name,
        "collection_exists": bool(collection_key) if collection_name else None,
        "manifest": str(manifest_path),
        "next_command": (
            f"zotero-import{option} --confirm-write"
            if not collection_name or collection_key
            else f"zotero-import{option} --create-collection --confirm-write"
        ),
    }
    print(json.dumps(preview, ensure_ascii=False, indent=2))
    return 0


def command_zotero_import(args: argparse.Namespace) -> int:
    if not args.confirm_write:
        raise ZoteroError(
            "Refusing to mutate Zotero without --confirm-write. Run zotero-plan and review it first."
        )
    config = load_config(args.config)
    client = _client(args, config)
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest, all_records = load_manifest(manifest_path)
    records = _select_zotero_records(all_records, args.pdf_only)
    if not records:
        raise ZoteroError("No records matched the requested Zotero import filter")
    collection_name = args.collection or config["zotero"].get("collection", "")
    create_collection = bool(args.create_collection)

    def checkpoint(_current: list[Any]) -> None:
        manifest["records"] = [record.to_dict() for record in all_records]
        save_manifest(manifest_path, manifest)

    counts = import_records(
        client,
        records,
        query=str(manifest.get("query", "")),
        collection_name=collection_name,
        create_collection=create_collection,
        checkpoint=checkpoint,
    )
    checkpoint(records)
    write_reports(manifest_path.parent, manifest, all_records)
    print(
        json.dumps(
            {
                "write_performed": True,
                "results": counts,
                "selected_records": len(records),
                "excluded_records": len(all_records) - len(records),
                "pdf_only": bool(args.pdf_only),
                "collection": collection_name,
                "manifest": str(manifest_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command in {"discover", "content", "screen", "reading-pack", "reading-receipt"}:
            return command_research(args)
        if args.command == "run":
            return command_run(args)
        if args.command == "resume":
            return command_resume(args)
        if args.command.startswith("browser-"):
            return command_browser(args)
        if args.command == "diagnose":
            return command_diagnose(args)
        if args.command == "zotero-status":
            return command_zotero_status(args)
        if args.command == "zotero-plan":
            return command_zotero_plan(args)
        if args.command == "zotero-import":
            return command_zotero_import(args)
        raise ValueError(f"Unknown command: {args.command}")
    except (ValueError, ZoteroError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc), "command": args.command}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
