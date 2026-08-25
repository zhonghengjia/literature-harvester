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
    save_manifest,
)
from lit_harvest.downloader import DownloadError, validate_pdf
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

    run = subparsers.add_parser("run", help="Search, deduplicate, download OA PDFs, and write reports")
    run.add_argument("--query", required=True, help="Research topic, title, DOI, PMID, PMCID, or arXiv ID")
    run.add_argument("--max-results", type=int, help="Maximum merged records")
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
    max_results = args.max_results or int(config["general"]["max_results"])
    if args.target_pdfs:
        if args.no_download:
            raise ValueError("--target-pdfs cannot be combined with --no-download")
        if args.target_pdfs < 1 or args.target_pdfs > 100:
            raise ValueError("--target-pdfs must be between 1 and 100")
        max_results = max(max_results, args.target_pdfs * 4)
    if not 1 <= max_results <= 500:
        raise ValueError("--max-results must be between 1 and 500")
    run_dir, manifest, records = run_pipeline(
        args.query,
        config,
        max_results,
        args.output_dir,
        not args.no_download,
        args.target_pdfs,
        args.required_term,
    )
    write_reports(run_dir, manifest, records)
    result = {
        "run_dir": str(run_dir),
        "manifest": str(run_dir / "manifest.json"),
        "records": len(records),
        "downloaded": sum(
            record.download_status in {"downloaded", "already_downloaded"} for record in records
        ),
        "target_pdfs": args.target_pdfs,
        "target_met": manifest.get("settings", {}).get("target_met"),
        "source_status": manifest.get("source_status", {}),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
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
        except (DownloadError, OSError):
            continue
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
    create_collection = bool(args.create_collection or config["zotero"].get("create_collection", False))

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
        if args.command == "run":
            return command_run(args)
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
