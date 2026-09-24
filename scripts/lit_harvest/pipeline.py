from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tomllib
from typing import Any
from uuid import uuid4
from urllib.parse import urlparse
from .downloader import SOURCE_PRIORITY, download_record, mark_duplicate
from .http import HttpClient, host_deferrals, restore_host_deferrals
from .models import PaperRecord, normalize_title, candidate_request_key
from .query_plan import validate_plan, candidate_id, concept_evidence, SelectedRecords
from .outcomes import ARTICLE_TYPES, collect_population, article_type, is_verified_pdf, summarize_outcomes
from .sources import (SOURCE_FUNCTIONS, enrich_core, enrich_doaj, enrich_openaire,
                      enrich_preprint_servers, enrich_preprints_by_title, enrich_unpaywall,
                      enrich_openalex_content, openalex_content_policy)

DEFAULT_CONFIG = {
    "general": {"output_root": "~/Documents/Literature", "max_results": 25,
                "max_candidates": 500, "max_pdf_mb": 100, "request_timeout_seconds": 30, "contact_email": ""},
    "sources": {"pubmed": True, "crossref": True, "europe_pmc": True, "openalex": True,
                "semantic_scholar": True, "arxiv": True, "unpaywall": True, "biorxiv_medrxiv": True,
                "preprint_title_match": True, "core": True, "openaire": False, "doaj": False},
    "openalex_content": {"enabled": False, "free_only": True, "max_files": 0},
    "zotero": {"base_url": "http://127.0.0.1:23119/api", "collection": "",
               "attachment_mode": "imported_file"},
}
METADATA_SOURCE_PRIORITY = {"pubmed": 0, "europe_pmc": 1, "crossref": 2, "openalex": 3, "semantic_scholar": 4, "arxiv": 5}

def _deep_merge(base, override):
    result = {k: _deep_merge(v, {}) if isinstance(v, dict) else v for k, v in base.items()}
    for k, v in override.items():
        result[k] = _deep_merge(result[k], v) if isinstance(v, dict) and isinstance(result.get(k), dict) else v
    return result

def load_config(path=None):
    if not path:
        return _deep_merge(DEFAULT_CONFIG, {})
    with Path(path).expanduser().open("rb") as handle:
        return _deep_merge(DEFAULT_CONFIG, tomllib.load(handle))

def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def effective_contact_email(config, env):
    return (env.get("LITERATURE_HARVESTER_CONTACT_EMAIL", "").strip()
            or env.get("UNPAYWALL_EMAIL", "").strip()
            or str(config["general"].get("contact_email", "")).strip())

def _redact(value, config, env):
    if isinstance(value, dict):
        return {k: _redact(v, config, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, config, env) for v in value]
    if not isinstance(value, str):
        return value
    for name in ("OPENALEX_API_KEY", "S2_API_KEY", "NCBI_API_KEY", "CORE_API_KEY", "UNPAYWALL_EMAIL", "LITERATURE_HARVESTER_CONTACT_EMAIL"):
        if env.get(name):
            value = value.replace(env[name], "REDACTED")
    contact = str(config["general"].get("contact_email", ""))
    return value.replace(contact, "REDACTED") if contact else value

def slugify(value, max_length=48):
    return "-".join(normalize_title(value).split())[:max_length].strip("-") or "literature"

def create_run_dir(query, config, output_dir=None):
    path = Path(output_dir).expanduser() if output_dir else Path(config["general"]["output_root"]).expanduser() / f"{datetime.now():%Y%m%d_%H%M%S}_{slugify(query)}_{uuid4().hex[:6]}"
    if path.exists() and any(path.iterdir()):
        raise ValueError("Output directory is not empty; use a fresh directory or resume")
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()

def _merge_candidate(existing, incoming):
    for field in ("doi", "pmid", "pmcid", "arxiv_id"):
        if getattr(existing, field) and getattr(incoming, field) and getattr(existing, field) != getattr(incoming, field):
            return False
    existing.merge(incoming)
    return True

def merge_records(records, limit=None, *, ranking="legacy"):
    merged, index = [], {}
    for record in records:
        keys = [k for k in record.identity_keys if not k.startswith("title:") or len(record.normalized_title) >= 20]
        matches = list(dict.fromkeys(i for k in keys for i in index.get(k, [])))
        match = next((i for i in matches if _merge_candidate(merged[i], record)), None)
        if match is None:
            match = len(merged)
            if matches:
                record.extra["identity_conflict"] = True
                for i in matches:
                    merged[i].extra["identity_conflict"] = True
            merged.append(record)
        for key in merged[match].identity_keys:
            if not key.startswith("title:") or len(merged[match].normalized_title) >= 20:
                bucket = index.setdefault(key, [])
                if match not in bucket:
                    bucket.append(match)
    for record in merged:
        record.pdf_candidates.sort(key=lambda item: (SOURCE_PRIORITY.get(item.get("source", ""), 99), item.get("url", "")))
        if ranking == "routes":
            ranks = {}
            for hit in record.extra.get("discovery_hits", []):
                key = hit["route_id"]
                ranks[key] = min(ranks.get(key, hit["rank"]), hit["rank"])
            record.relevance_score = sum(1.0 / (60 + rank) for rank in ranks.values())
        else:
            base = float(record.extra.setdefault("base_relevance_score", record.relevance_score))
            record.relevance_score = round(base + math.log10((record.citation_count or 0)+1)/20 + min(len(record.sources), 5)*.01, 5)
    if ranking == "routes":
        merged.sort(key=lambda r: (-r.relevance_score, candidate_id(r)))
    else:
        merged.sort(key=lambda r: (r.relevance_score, r.citation_count or 0), reverse=True)
    return merged[:limit] if limit is not None else merged

def filter_required_terms(records, terms):
    terms = [t.casefold().strip() for t in (terms or []) if t.strip()]
    return [r for r in records if not terms or any(t in f"{r.title} {r.abstract} {r.doi}".casefold() for t in terms)]

def _search_one(source, query, limit, config, env, *, native_query=False):
    contact = effective_contact_email(config, env)
    client = HttpClient(timeout=int(config["general"]["request_timeout_seconds"]))
    args = [client, query, limit]
    if source == "pubmed":
        args += [contact, env.get("NCBI_API_KEY", "")]
    elif source == "crossref":
        args += [contact]
    elif source == "openalex":
        args += [env.get("OPENALEX_API_KEY", ""), contact]
    elif source == "semantic_scholar":
        args += [env.get("S2_API_KEY", "")]
    if source == "arxiv" and native_query:
        return SOURCE_FUNCTIONS[source](*args, native_query=True)
    return SOURCE_FUNCTIONS[source](*args)

def enrich_records(records, config, env, *, force=False):
    client = HttpClient(timeout=int(config["general"]["request_timeout_seconds"]))
    email = env.get("UNPAYWALL_EMAIL") or effective_contact_email(config, env)
    jobs = {
        "biorxiv_medrxiv": (enrich_preprint_servers, (), {}),
        "unpaywall": (enrich_unpaywall, (email,), {}),
        "preprint_title_match": (enrich_preprints_by_title, (), {"force": force}),
        "core": (enrich_core, (env.get("CORE_API_KEY", ""),), {"force": force}),
        "openaire": (enrich_openaire, (), {}), "doaj": (enrich_doaj, (), {}),
    }
    statuses = {}
    for name, (function, args, kwargs) in jobs.items():
        if not config["sources"].get(name, False):
            statuses[name] = {"status": "disabled", "count": 0}
            continue
        missing = "email" if name == "unpaywall" and not email else "key" if name == "core" and not env.get("CORE_API_KEY") else ""
        if missing:
            statuses[name] = {"status": f"disabled_missing_{missing}", "count": 0}
            continue
        try:
            count, attempted, errors = function(client, records, *args, **kwargs)
            statuses[name] = {"status": "partial" if errors else "ok", "count": count, "attempted": attempted, "errors": errors[:5]}
        except Exception as exc:
            statuses[name] = {"status": "error", "count": 0, "error": f"{type(exc).__name__}: {exc}"[:1000]}
            if getattr(exc, "retry_at", None):
                statuses[name]["retry_at"] = exc.retry_at
    return _redact(statuses, config, env)

def search_all(query, config, max_results, env=None, *, required_terms=None, article_types=None, max_candidates=None, query_plan=None):
    env = dict(os.environ) if env is None else env
    enabled = [s for s in SOURCE_FUNCTIONS if config["sources"].get(s, False)]
    all_records, statuses = [], {}
    source_limit = max_candidates or max_results
    plan = validate_plan(query_plan) if query_plan is not None else None
    routes = plan["routes"] if plan else [{"id": s, "source": s, "query": query, "limit": source_limit, "syntax": "text"} for s in enabled]
    active = []
    for route in routes:
        if route["source"] not in enabled:
            statuses[route["id"]] = {"status": "disabled", "count": 0, **route}
        else:
            active.append(route)
    with ThreadPoolExecutor(max_workers=min(len(enabled), 5) or 1) as executor:
        futures = {}
        for route in active:
            args = (route["source"], route["query"], route["limit"], config, env)
            kwargs = {"native_query": True} if route["source"] == "arxiv" and route["syntax"] == "native" else {}
            futures[executor.submit(_search_one, *args, **kwargs)] = route
        for future in as_completed(futures):
            route = futures[future]
            source = route["id"]
            try:
                found = future.result()
                if plan:
                    for rank, record in enumerate(found, 1):
                        record.extra.setdefault("discovery_hits", []).append({"route_id": source,
                            "source": route["source"], "rank": rank, "source_ids": record.identity_keys})
                all_records.extend(found)
                statuses[source] = {"status": "ok", "count": len(found), **getattr(found, "source_status", {})}
            except Exception as exc:
                statuses[source] = {"status": "error", "count": 0, "error": f"{type(exc).__name__}: {exc}"[:1000]}
                if getattr(exc, "retry_at", None):
                    statuses[source]["retry_at"] = exc.retry_at
            if plan:
                statuses[source].update({"route": route, "observed_at": utc_now()})
    route_order = {r["id"]: index for index, r in enumerate(routes)}
    all_records.sort(key=lambda r: (min((METADATA_SOURCE_PRIORITY.get(s, 99) for s in r.sources), default=99),
        min((route_order[h["route_id"]] for h in r.extra.get("discovery_hits", [])), default=0) if plan else 0))
    merged = merge_records(all_records, ranking="routes" if plan else "legacy")
    eligible = filter_required_terms(merged, required_terms)
    if article_types:
        if set(article_types) - ARTICLE_TYPES:
            raise ValueError("Unsupported article type filter")
        eligible = [r for r in eligible if article_type(r) in article_types]
    records = eligible[:max_results]
    if plan:
        eligible_ids = {id(r) for r in eligible}
        for record in merged:
            record.extra["candidate_id"] = candidate_id(record)
            record.extra["concept_evidence"] = concept_evidence(record, plan["concepts"])
            record.extra["screening"] = {"decision": "uncertain", "reason": "Not yet reviewed against the research question", "actor": "system", "at": utc_now()}
            record.extra["queue_eligible"] = id(record) in eligible_ids
            if id(record) not in eligible_ids:
                record.extra["queue_exclusion_reason"] = "Explicit requested metadata/literal filter; candidate retained"
        records = SelectedRecords(records, merged)
    statuses["selection"] = {"raw_records": len(all_records), "deduplicated_records": len(merged),
        "eligible_records": len(eligible), "selected_records": len(records), "source_candidate_limit": source_limit,
        "excluded_by_filters": len(merged)-len(eligible), "outside_selected_cap": max(0, len(eligible)-max_results)}
    if plan:
        statuses["selection"]["source_candidate_limit"] = None
        statuses["selection"]["route_candidate_limits"] = {r["id"]: r["limit"] for r in routes}
    content = config.get("openalex_content", {})
    client = HttpClient(timeout=int(config["general"]["request_timeout_seconds"]))
    result = enrich_openalex_content(client, records, env.get("OPENALEX_API_KEY", ""),
        enabled=bool(content.get("enabled", False)), free_only=bool(content.get("free_only", True)),
        max_files=int(content.get("max_files", 0)))
    statuses["openalex_content"] = {**openalex_content_policy(
        enabled=bool(content.get("enabled", False)), free_only=bool(content.get("free_only", True)),
        max_files=int(content.get("max_files", 0)), api_key_present=bool(env.get("OPENALEX_API_KEY"))),
        "count": result[0], "attempted": result[1], "errors": result[2]}
    return records, _redact(statuses, config, env)

def new_manifest(query, records, source_status, settings):
    now = utc_now()
    return {"schema_version": 2, "run_id": str(uuid4()), "query": query, "created_at": now,
        "updated_at": now, "settings": settings, "source_status": source_status, "records": [r.to_dict() for r in records]}

def save_manifest(path, manifest):
    manifest["updated_at"] = utc_now()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix+".tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)

def load_manifest(path):
    manifest = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    if manifest.get("schema_version") not in {1, 2} or not isinstance(manifest.get("records"), list):
        raise ValueError("Unsupported or invalid manifest schema")
    context = manifest.get("research_context")
    if context:
        for name in ("query-plan.json", "candidates.json"):
            ref = context.get(name, {})
            source = Path(ref.get("path", ""))
            if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != ref.get("sha256"):
                raise ValueError(f"Frozen research artifact missing or changed: {name}")
    records = collect_population(manifest, [PaperRecord.from_dict(item) for item in manifest["records"]])
    return manifest, records

def _deferred(record):
    def future(value):
        try:
            retry = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if retry.tzinfo is None:
                retry = retry.replace(tzinfo=timezone.utc)
            return retry > datetime.now(timezone.utc)
        except (TypeError, ValueError):
            return False
    if not future(record.retry_at):
        return False
    # Once a deadline is attributable, transport enforces it for that host,
    # leaving other permitted copies usable. Legacy unlocated deadlines stay held.
    located = any(future(item.get("retry_at")) and urlparse(item.get("url", "")).hostname
                  for item in record.attempts)
    known_hosts = host_deferrals()
    located = located or any(urlparse(candidate.get("url", "")).hostname in known_hosts
                             for candidate in record.pdf_candidates)
    return not located

def retrieve_population(run_dir, manifest, records, config, env):
    target = manifest["settings"].get("target_pdfs")
    contact, seen_hashes = effective_contact_email(config, env), {}
    restore_host_deferrals(manifest.get("host_retry_at", {}))
    def checkpoint():
        manifest["records"] = _redact([r.to_dict() for r in records], config, env)
        manifest["metrics"] = summarize_outcomes(records, target)
        manifest["settings"]["target_met"] = manifest["metrics"]["target_met"]
        manifest["host_retry_at"] = host_deferrals()
        fallback = {}
        for item in records:
            for name, status in item.extra.get("fallback_sources", {}).items():
                group = fallback.setdefault(name, {"records_considered": 0, "count": 0, "attempted": 0, "status_counts": {}})
                group["records_considered"] += 1
                for field in ("count", "attempted"):
                    group[field] += status.get(field, 0)
                state = status.get("status", "unknown")
                group["status_counts"][state] = group["status_counts"].get(state, 0) + 1
        manifest["fallback_source_status"] = fallback
        save_manifest(run_dir / "manifest.json", manifest)
    # Verify all existing files before new downloads, including successes later
    # in the ranking. Otherwise a resume may overshoot an already satisfied target.
    for record in records:
        if record.local_pdf:
            candidates = record.pdf_candidates
            record.pdf_candidates = []
            try:
                download_record(record, run_dir/"pdfs", int(config["general"]["max_pdf_mb"]),
                    int(config["general"]["request_timeout_seconds"]), contact_email=contact,
                    allow_fulltext_fallback=False)
            finally:
                record.pdf_candidates = candidates
            mark_duplicate(record, seen_hashes)
    checkpoint()
    for record in records:
        if target and len(seen_hashes) >= target:
            break
        if _deferred(record):
            continue
        if is_verified_pdf(record) or record.download_status == "duplicate_pdf":
            continue
        try:
            download_record(record, run_dir/"pdfs", int(config["general"]["max_pdf_mb"]),
                int(config["general"]["request_timeout_seconds"]), contact_email=contact, allow_fulltext_fallback=False)
            checkpoint()
            if not is_verified_pdf(record):
                primary_status, primary_reason = record.download_status, record.failure_reason
                old_requests = {candidate_request_key(item) for item in record.pdf_candidates}
                record.extra["fallback_sources"] = enrich_records([record], config, env, force=True)
                all_candidates = list(record.pdf_candidates)
                record.pdf_candidates = [c for c in all_candidates
                                         if candidate_request_key(c) not in old_requests
                                         or c.get("kind") in {"jats", "fulltext_xml"}]
                try:
                    download_record(record, run_dir/"pdfs", int(config["general"]["max_pdf_mb"]),
                        int(config["general"]["request_timeout_seconds"]), contact_email=contact, allow_fulltext_fallback=True)
                finally:
                    new_candidates = list(record.pdf_candidates)
                    record.pdf_candidates = all_candidates
                    for candidate in new_candidates:
                        record.add_pdf_candidate(**candidate)
                if record.download_status == "no_oa_version" and primary_status != "no_oa_version":
                    record.download_status, record.failure_reason = primary_status, primary_reason
                if record.extra.get("identity_review_files") and not is_verified_pdf(record) and record.download_status != "fulltext_only":
                    record.download_status, record.identity_status = "manual_review", "manual_review"
            mark_duplicate(record, seen_hashes)
        except Exception as exc:
            record.download_status = "deferred" if getattr(exc, "retry_at", None) else "download_failed"
            record.retry_at = getattr(exc, "retry_at", "") or ""
            record.failure_reason = _redact(f"{type(exc).__name__}: {exc}", config, env)[:1000]
        checkpoint()
    checkpoint()

def run_pipeline(query, config, max_results, output_dir, download, target_pdfs=None,
                 required_terms=None, env=None, *, article_types=None, max_candidates=None, query_plan=None):
    if not 1 <= max_results <= 10000 or (max_candidates is not None and not max_results <= max_candidates <= 10000):
        raise ValueError("Candidate limits must satisfy 1 <= max_results <= max_candidates <= 10000")
    if target_pdfs is not None and (not download or not 1 <= target_pdfs <= max_results):
        raise ValueError("PDF target must be within the selected candidate cap and require downloading")
    plan = validate_plan(query_plan) if query_plan is not None else None
    if plan:
        query = plan["question"]
    env = dict(os.environ) if env is None else env
    run_dir = create_run_dir(query, config, output_dir)
    search_options = {"required_terms": required_terms, "article_types": article_types, "max_candidates": max_candidates}
    if plan:
        search_options["query_plan"] = plan
    records, statuses = search_all(query, config, max_results, env, **search_options)
    settings = {"max_results": max_results, "max_candidates": max_candidates or max_results,
        "download": download, "target_pdfs": target_pdfs, "required_terms": required_terms or [],
        "article_types": article_types or [], "enabled_sources": [k for k, v in config["sources"].items() if v],
        "output_dir": str(run_dir)}
    manifest = new_manifest(query, records, statuses, settings)
    if plan:
        context = {"schema_version": 1, "ranking": "RRF k=60, one-based source rank; citation count excluded",
                   "selected_candidate_ids": [r.extra["candidate_id"] for r in records]}
        for name, value in (("query-plan.json", plan), ("candidates.json", {"schema_version": 1, "records": [r.to_dict() for r in records.candidates]})):
            path = run_dir / name
            path.write_text(json.dumps(_redact(value, config, env), ensure_ascii=False, indent=2), encoding="utf-8")
            context[name] = {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        manifest["research_context"] = context
    save_manifest(run_dir/"manifest.json", manifest)
    if download:
        retrieve_population(run_dir, manifest, records, config, env)
    return run_dir, manifest, records

def resume_pipeline(manifest_path, config, output_dir=None, env=None):
    """Resume a frozen population in a fresh directory without repeating search."""
    source_path = Path(manifest_path).expanduser().resolve()
    previous, records = load_manifest(source_path)
    run_dir = create_run_dir(str(previous.get("query", "resume")), config, output_dir)
    manifest = new_manifest(str(previous.get("query", "")), records, previous.get("source_status", {}),
        {**previous.get("settings", {}), "output_dir": str(run_dir), "download": True,
         "enabled_sources": [key for key, enabled in config["sources"].items() if enabled]})
    manifest["parent_run_id"] = previous.get("run_id")
    if "research_context" in previous:
        manifest["research_context"] = previous["research_context"]
    manifest["parent_manifest_sha256"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
    manifest["host_retry_at"] = previous.get("host_retry_at", {})
    save_manifest(run_dir/"manifest.json", manifest)
    retrieve_population(run_dir, manifest, records, config, dict(os.environ) if env is None else env)
    return run_dir, manifest, records
