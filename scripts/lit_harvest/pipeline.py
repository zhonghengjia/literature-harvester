from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import tempfile
import tomllib
from typing import Any
from uuid import uuid4

from .downloader import SOURCE_PRIORITY, download_all, download_until_target
from .http import HttpClient
from .models import PaperRecord, normalize_title
from .sources import SOURCE_FUNCTIONS, enrich_preprint_servers, enrich_unpaywall


DEFAULT_CONFIG: dict[str, Any] = {
    "general": {
        "output_root": "~/Documents/Literature",
        "max_results": 25,
        "max_pdf_mb": 100,
        "request_timeout_seconds": 30,
        "contact_email": "",
    },
    "sources": {
        "pubmed": True,
        "crossref": True,
        "europe_pmc": True,
        "openalex": True,
        "semantic_scholar": True,
        "arxiv": True,
        "unpaywall": True,
        "biorxiv_medrxiv": True,
    },
    "zotero": {
        "base_url": "http://127.0.0.1:23119/api",
        "collection": "",
        "create_collection": False,
        "attachment_mode": "imported_file",
    },
}

METADATA_SOURCE_PRIORITY = {
    "pubmed": 0,
    "europe_pmc": 1,
    "crossref": 2,
    "openalex": 3,
    "semantic_scholar": 4,
    "arxiv": 5,
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = {key: (value.copy() if isinstance(value, dict) else value) for key, value in base.items()}
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    if not path:
        return _deep_merge(DEFAULT_CONFIG, {})
    with Path(path).expanduser().open("rb") as handle:
        return _deep_merge(DEFAULT_CONFIG, tomllib.load(handle))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def effective_contact_email(config: dict[str, Any], env: dict[str, str]) -> str:
    return (
        env.get("LITERATURE_HARVESTER_CONTACT_EMAIL", "").strip()
        or env.get("UNPAYWALL_EMAIL", "").strip()
        or str(config["general"].get("contact_email", "")).strip()
    )


def slugify(value: str, max_length: int = 48) -> str:
    value = "-".join(normalize_title(value).split())
    return (value[:max_length].strip("-") or "literature")


def create_run_dir(query: str, config: dict[str, Any], output_dir: str | None = None) -> Path:
    if output_dir:
        run_dir = Path(output_dir).expanduser()
    else:
        root = Path(config["general"]["output_root"]).expanduser()
        run_dir = root / f"{datetime.now():%Y%m%d_%H%M%S}_{slugify(query)}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir.resolve()


def _merge_candidate(existing: PaperRecord, incoming: PaperRecord) -> bool:
    if existing.doi and incoming.doi and existing.doi != incoming.doi:
        return False
    existing.merge(incoming)
    return True


def merge_records(records: list[PaperRecord], limit: int | None = None) -> list[PaperRecord]:
    merged: list[PaperRecord] = []
    exact_index: dict[str, int] = {}
    title_index: dict[str, int] = {}
    for record in records:
        match_index: int | None = None
        for key in record.identity_keys:
            if key.startswith("title:"):
                continue
            if key in exact_index:
                match_index = exact_index[key]
                break
        title_key = record.normalized_title
        if match_index is None and len(title_key) >= 20 and title_key in title_index:
            candidate_index = title_index[title_key]
            if not (merged[candidate_index].doi and record.doi and merged[candidate_index].doi != record.doi):
                match_index = candidate_index
        if match_index is None:
            match_index = len(merged)
            merged.append(record)
        else:
            _merge_candidate(merged[match_index], record)
        current = merged[match_index]
        for key in current.identity_keys:
            if not key.startswith("title:"):
                exact_index[key] = match_index
        if len(current.normalized_title) >= 20:
            title_index[current.normalized_title] = match_index

    for record in merged:
        record.pdf_candidates.sort(
            key=lambda item: (SOURCE_PRIORITY.get(item.get("source", ""), 99), item.get("url", ""))
        )
        citation_bonus = math.log10((record.citation_count or 0) + 1) / 20
        source_bonus = min(len(record.sources), 5) * 0.01
        record.relevance_score = round(record.relevance_score + citation_bonus + source_bonus, 5)
    merged.sort(key=lambda record: (record.relevance_score, record.citation_count or 0), reverse=True)
    return merged[:limit] if limit else merged


def filter_required_terms(records: list[PaperRecord], terms: list[str] | None) -> list[PaperRecord]:
    normalized_terms = [term.casefold().strip() for term in (terms or []) if term.strip()]
    if not normalized_terms:
        return records
    return [
        record
        for record in records
        if any(
            term in f"{record.title} {record.abstract} {record.doi}".casefold()
            for term in normalized_terms
        )
    ]


def _search_one(
    source: str,
    query: str,
    limit: int,
    config: dict[str, Any],
    env: dict[str, str],
) -> list[PaperRecord]:
    timeout = int(config["general"]["request_timeout_seconds"])
    contact = effective_contact_email(config, env)
    client = HttpClient(timeout=timeout)
    if source == "pubmed":
        return SOURCE_FUNCTIONS[source](
            client, query, limit, contact, env.get("NCBI_API_KEY", "")
        )
    if source == "crossref":
        return SOURCE_FUNCTIONS[source](client, query, limit, contact)
    if source == "openalex":
        return SOURCE_FUNCTIONS[source](client, query, limit, env.get("OPENALEX_API_KEY", ""), contact)
    if source == "semantic_scholar":
        return SOURCE_FUNCTIONS[source](client, query, limit, env.get("S2_API_KEY", ""))
    return SOURCE_FUNCTIONS[source](client, query, limit)


def search_all(
    query: str,
    config: dict[str, Any],
    max_results: int,
    env: dict[str, str] | None = None,
) -> tuple[list[PaperRecord], dict[str, Any]]:
    env = env or dict(os.environ)
    secrets_to_redact = {
        value
        for value in (
            env.get("OPENALEX_API_KEY", ""),
            env.get("S2_API_KEY", ""),
            env.get("NCBI_API_KEY", ""),
            env.get("UNPAYWALL_EMAIL", ""),
            env.get("LITERATURE_HARVESTER_CONTACT_EMAIL", ""),
            str(config["general"].get("contact_email", "")),
        )
        if value
    }
    enabled = [source for source in SOURCE_FUNCTIONS if config["sources"].get(source, False)]
    all_records: list[PaperRecord] = []
    source_status: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=min(len(enabled), 5) or 1) as executor:
        futures = {
            executor.submit(_search_one, source, query, max_results, config, env): source
            for source in enabled
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                found = future.result()
                all_records.extend(found)
                source_status[source] = {"status": "ok", "count": len(found)}
            except Exception as exc:
                message = str(exc)
                for secret in secrets_to_redact:
                    message = message.replace(secret, "REDACTED")
                source_status[source] = {"status": "error", "count": 0, "error": message[:1000]}
    all_records.sort(
        key=lambda record: min(
            (METADATA_SOURCE_PRIORITY.get(source, 99) for source in record.sources), default=99
        )
    )
    records = merge_records(all_records, max_results)

    if config["sources"].get("biorxiv_medrxiv", False):
        client = HttpClient(timeout=int(config["general"]["request_timeout_seconds"]))
        count, attempted, errors = enrich_preprint_servers(client, records)
        for index, message in enumerate(errors):
            for secret in secrets_to_redact:
                message = message.replace(secret, "REDACTED")
            errors[index] = message
        source_status["biorxiv_medrxiv"] = {
            "status": "partial" if errors else "ok",
            "count": count,
            "attempted": attempted,
            **({"errors": errors[:5]} if errors else {}),
        }

    if config["sources"].get("unpaywall", False):
        email = env.get("UNPAYWALL_EMAIL") or effective_contact_email(config, env)
        if email:
            client = HttpClient(timeout=int(config["general"]["request_timeout_seconds"]))
            count, attempted, errors = enrich_unpaywall(client, records, email)
            for index, message in enumerate(errors):
                for secret in secrets_to_redact:
                    message = message.replace(secret, "REDACTED")
                errors[index] = message
            if errors and len(errors) == attempted:
                status = "error"
            elif errors:
                status = "partial"
            else:
                status = "ok"
            source_status["unpaywall"] = {
                "status": status,
                "count": count,
                "attempted": attempted,
                **({"errors": errors[:5]} if errors else {}),
            }
        else:
            source_status["unpaywall"] = {
                "status": "disabled_missing_email",
                "count": 0,
                "error": "Set LITERATURE_HARVESTER_CONTACT_EMAIL or UNPAYWALL_EMAIL",
            }
    return records, source_status


def new_manifest(query: str, records: list[PaperRecord], source_status: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": 1,
        "run_id": str(uuid4()),
        "query": query,
        "created_at": now,
        "updated_at": now,
        "settings": settings,
        "source_status": source_status,
        "records": [record.to_dict() for record in records],
    }


def save_manifest(path: Path, manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = utc_now()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def load_manifest(path: str | Path) -> tuple[dict[str, Any], list[PaperRecord]]:
    manifest_path = Path(path).expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("records"), list):
        raise ValueError("Unsupported or invalid manifest schema")
    records = [PaperRecord.from_dict(item) for item in manifest["records"]]
    return manifest, records


def run_pipeline(
    query: str,
    config: dict[str, Any],
    max_results: int,
    output_dir: str | None,
    download: bool,
    target_pdfs: int | None = None,
    required_terms: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> tuple[Path, dict[str, Any], list[PaperRecord]]:
    run_dir = create_run_dir(query, config, output_dir)
    manifest_path = run_dir / "manifest.json"
    records, source_status = search_all(query, config, max_results, env)
    normalized_terms = [term.casefold().strip() for term in (required_terms or []) if term.strip()]
    records = filter_required_terms(records, normalized_terms)
    settings = {
        "max_results": max_results,
        "download": download,
        "target_pdfs": target_pdfs,
        "required_terms": normalized_terms,
        "enabled_sources": [key for key, value in config["sources"].items() if value],
        "output_dir": str(run_dir),
    }
    manifest = new_manifest(query, records, source_status, settings)
    save_manifest(manifest_path, manifest)

    if download:
        def checkpoint(current: list[PaperRecord]) -> None:
            manifest["records"] = [record.to_dict() for record in current]
            save_manifest(manifest_path, manifest)

        if target_pdfs:
            successes, failures = download_until_target(
                records,
                run_dir / "pdfs",
                target_pdfs,
                int(config["general"]["max_pdf_mb"]),
                int(config["general"]["request_timeout_seconds"]),
                checkpoint,
            )
            manifest["download_attempts"] = [record.to_dict() for record in failures]
            manifest["settings"]["target_met"] = len(successes) >= target_pdfs
            records = successes[:target_pdfs]
            manifest["records"] = [record.to_dict() for record in records]
            save_manifest(manifest_path, manifest)
        else:
            download_all(
                records,
                run_dir / "pdfs",
                int(config["general"]["max_pdf_mb"]),
                int(config["general"]["request_timeout_seconds"]),
                checkpoint,
            )
            checkpoint(records)
    return run_dir, manifest, records
