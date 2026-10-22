"""Independent content packages, screening decisions and source-grounded reading receipts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from .article_document import jats_document, render_document, element_reading_text, element_locator
from .content_store import cached_pdf_document as pdf_document
from .html_document import html_document
from .downloader import user_agent, sha256_file, _safe_message, download_record
from .fulltext import _fetch_bounded, MAX_XML_BYTES, FullTextError
from .http import HttpClient, is_web_url, redact_url, host_deferrals, restore_host_deferrals
from .models import PaperRecord, candidate_request_key
from .pmc import resolve_pmc_candidates, BUCKET_HOST, is_pmc_listing_candidate
from .pipeline import create_run_dir, load_manifest, new_manifest, save_manifest, utc_now, effective_contact_email, _redact, enrich_records
from .query_plan import candidate_id
from .sources import _add_pmc_cloud
from .settings import client_options, section, positive
from .assets import acquire_assets
from .metered import bind_budget


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _candidate_map(records):
    result = {}
    for record in records:
        ident = record.extra.get("candidate_id") or candidate_id(record)
        if ident in result:
            raise ValueError("Ambiguous candidate IDs: regenerate a new candidate inventory; frozen IDs cannot be silently reassigned")
        result[ident] = record
    return result


def _qualified_jats(candidate, record):
    version = candidate.get("article_version")
    version_id = f"{record.pmcid}.{version}"
    url = candidate.get("url", "")
    return (candidate.get("source") == "pmc_oa_cloud" and candidate.get("kind") == "jats"
            and candidate.get("pmcid") == record.pmcid and type(version) is int and version > 0
            and bool(candidate.get("license")) and candidate.get("oa_route") in {"open_access", "author_manuscript_tdm"}
            and url == f"https://{BUCKET_HOST}/{version_id}/{version_id}.xml")


def _remote_document(clone, directory, config, client, pdf_options, contact, attempts):
    doc, source = None, {}
    resources = section(config, "resources")
    attempt_start = len(clone.attempts)
    if clone.pdf_candidates:
        for candidate in clone.pdf_candidates:
            # Sources must establish eligibility; an arbitrary publisher URL is not OA proof.
            if candidate.get("kind") != "landing" or not (candidate.get("oa_verified") is True or candidate.get("license")):
                continue
            try:
                raw, actual, html = _fetch_bounded(client, candidate["url"], resources["html_bytes"], "text/html")
                doc = html_document(raw, html, clone)
                raw_path = directory / "source.html"
                with raw_path.open("xb") as stream:
                    stream.write(raw)
                source = {"artifact": str(raw_path.resolve()), "url": redact_url(actual),
                          "candidate": candidate, "acquired_at": utc_now()}
                attempts.append({"stage": "html", "url": redact_url(actual), "outcome": "extracted"})
                break
            except Exception as exc:
                doc = None
                attempts.append({"stage": "html", "url": redact_url(candidate["url"]),
                    "outcome": getattr(exc, "code", "error"), "error": _safe_message(exc, contact)})
    if doc is None and clone.pdf_candidates:
        try:
            download_record(clone, directory / "pdfs", section(config, "general")["max_pdf_mb"],
                section(config, "general")["request_timeout_seconds"], contact_email=contact,
                allow_fulltext_fallback=False, config=config)
            attempts.extend(clone.attempts[attempt_start:])
            if clone.identity_status == "verified" and clone.local_pdf and clone.sha256:
                doc = pdf_document(Path(clone.local_pdf), clone, **pdf_options)
                source = {"artifact": str(Path(clone.local_pdf).resolve()), "url": redact_url(clone.download_url),
                          "download_version": clone.download_version, "license": clone.license, "acquired_at": utc_now()}
                attempts.append({"stage": "online_pdf", "outcome": "extracted"})
        except Exception as exc:
            doc = None
            attempts.append({"stage": "online_pdf", "outcome": getattr(exc, "code", "error"), "error": _safe_message(exc, contact)})
    return doc, source


def acquire_document(record, directory, config, env, *, local_only=False, local_html=None):
    directory.mkdir(parents=True, exist_ok=False)
    contact = effective_contact_email(config, env)
    attempts = []
    doc = None
    source = {}
    clone = PaperRecord.from_dict(record.to_dict())
    content_settings = section(config, "content")
    resources = section(config, "resources")
    pdf_options = {k: content_settings[k] for k in ("max_pages", "page_batch_size", "ocr", "ocr_language", "ocr_dpi")}
    pdf_options["cache_dir"] = content_settings["cache_dir"]
    client = HttpClient(**client_options(config), user_agent=user_agent(contact), validate_redirects=is_web_url)
    if local_html is not None:
        local_only = True
        try:
            path = Path(local_html).expanduser().resolve()
            with path.open("rb") as stream:
                raw = stream.read(resources["html_bytes"] + 1)
            if len(raw) > resources["html_bytes"]:
                raise FullTextError("Local HTML exceeds configured resource budget", code="too_large")
            declared = re.search(br'<meta\b[^>]*\bcharset\s*=\s*[\"\s\x27]*([a-zA-Z0-9_-]+)', raw[:4096], re.I)
            encoding = declared.group(1).decode("ascii") if declared else "utf-8-sig"
            doc = html_document(raw, raw.decode(encoding), clone)
            copied = directory / "source.html"
            with copied.open("xb") as stream:
                stream.write(raw)
            source = {"artifact": str(copied.resolve()), "original_local_path": str(path),
                      "acquisition": "explicit_user_local_html", "encoding": encoding, "acquired_at": utc_now()}
            attempts.append({"stage": "local_html", "outcome": "extracted"})
        except Exception as exc:
            doc = None
            attempts.append({"stage": "local_html", "outcome": getattr(exc, "code", "error"),
                             "error": _safe_message(exc, contact)})
    if doc is None and not local_only and clone.pmcid:
        # Re-resolve current official eligibility; a manifest label alone is not permission.
        _add_pmc_cloud(clone)
        listings = [c for c in clone.pdf_candidates if is_pmc_listing_candidate(c)]
        candidates = []
        for listing in listings:
            try:
                resolved = resolve_pmc_candidates(client, listing["url"])
                candidates.extend(c for c in resolved if _qualified_jats(c, clone))
                attempts.append({"stage": "resolve", "url": redact_url(listing["url"]), "outcome": "resolved", "jats_count": len(candidates)})
            except Exception as exc:
                attempts.append({"stage": "resolve", "url": redact_url(listing["url"]), "outcome": getattr(exc, "code", "error"),
                                 "error": _safe_message(exc, contact), "retry_at": getattr(exc, "retry_at", "")})
        seen = set()
        for candidate in candidates:
            if candidate["url"] in seen:
                continue
            seen.add(candidate["url"])
            try:
                raw, actual, _ = _fetch_bounded(client, candidate["url"], resources["xml_bytes"], "application/xml")
                if candidate.get("expected_md5") and hashlib.md5(raw).hexdigest() != candidate["expected_md5"]:
                    raise FullTextError("PMC object checksum mismatch", code="checksum_conflict")
                doc = jats_document(raw, clone, max_bytes=resources["xml_bytes"])
                raw_path = directory / "source.xml"
                with raw_path.open("xb") as stream:
                    stream.write(raw)
                source = {"artifact": str(raw_path.resolve()), "url": redact_url(actual),
                          "candidate": candidate, "acquired_at": utc_now()}
                attempts.append({"stage": "jats", "url": redact_url(candidate["url"]), "outcome": "extracted"})
                break
            except Exception as exc:
                doc = None
                attempts.append({"stage": "jats", "url": redact_url(candidate["url"]), "outcome": getattr(exc, "code", "error"),
                                 "error": _safe_message(exc, contact), "retry_at": getattr(exc, "retry_at", "")})
    if doc is None and record.local_pdf and local_html is None:
        try:
            path = Path(record.local_pdf)
            if not path.is_file() or path.stat().st_size > int(config["general"]["max_pdf_mb"]) * 1024 * 1024:
                raise FullTextError("Local PDF is missing or exceeds the configured bound", code="local_pdf_unavailable")
            raw = path.read_bytes()
            if not record.sha256 or hashlib.sha256(raw).hexdigest() != record.sha256:
                raise FullTextError("Local PDF checksum is missing or changed", code="checksum_conflict")
            copied = directory / "source.pdf"
            with copied.open("xb") as stream:
                stream.write(raw)
            doc = pdf_document(copied, record, **pdf_options)
            source = {"artifact": str(copied.resolve()), "original_local_path": str(path.resolve()),
                      "download_url": redact_url(record.download_url), "download_version": record.download_version,
                      "license": record.license, "acquired_at": utc_now()}
            attempts.append({"stage": "local_pdf", "outcome": "extracted"})
        except Exception as exc:
            doc = None
            attempts.append({"stage": "local_pdf", "outcome": getattr(exc, "code", "error"), "error": _safe_message(exc, contact)})
    if doc is None and not local_only:
        doc, source = _remote_document(clone, directory, config, client, pdf_options, contact, attempts)
        if doc is None:
            previous_requests = {candidate_request_key(c) for c in clone.pdf_candidates}
            # Reuse existing configured free sources, never the metered content API.
            fallback = enrich_records([clone], config, env, force=True)
            attempts.append({"stage": "free_enrichment", "sources": fallback})
            clone.pdf_candidates = [c for c in clone.pdf_candidates
                                    if candidate_request_key(c) not in previous_requests]
            doc, source = _remote_document(clone, directory, config, client, pdf_options, contact, attempts)
    result = {"candidate_id": record.extra.get("candidate_id") or candidate_id(record), "title": record.title,
              "status": "content_unavailable", "attempts": attempts, "ai_reading_status": "not_submitted"}
    if doc is not None:
        doc["source"] = source
        acquire_assets(doc, directory, client, config, local_only=local_only)
        document_path = directory / "document.json"
        write_json(document_path, _redact(doc, config, env))
        with (directory / "reader.md").open("x", encoding="utf-8") as stream:
            stream.write(render_document(doc))
        result.update({"status": "extracted", "document": str(document_path.resolve()),
                       "document_sha256": sha256_file(document_path), "coverage": doc["coverage"], "gaps": doc["gaps"]})
    elif not attempts:
        result["reason"] = "No permitted native fulltext route or verified local PDF; abstract is not full text"
    return result


def prepare_content(manifest_path, output_dir, config, *, local_only=False, max_records=None,
                    batch_size=None, max_seconds=None, resume_index=None, env=None, local_html=None):
    settings = section(config, "content")
    max_records = positive(settings["max_records"] if max_records is None else max_records, "max_records", zero=True)
    batch_size = positive(settings["batch_size"] if batch_size is None else batch_size, "batch_size")
    max_seconds = positive(settings["max_seconds"] if max_seconds is None else max_seconds, "max_seconds", zero=True)
    manifest_path = Path(manifest_path).resolve()
    manifest, records = load_manifest(manifest_path)
    if local_html is not None:
        if len(records) != 1 or resume_index:
            raise ValueError("--local-html requires exactly one selected record and a new content run without --resume-index")
        local_only = True
    env = dict(os.environ) if env is None else env
    prior, previous = {}, {}
    valid_ids = set(_candidate_map(records))
    if resume_index:
        previous = read_json(resume_index)
        if previous.get("source_manifest_sha256") != sha256_file(manifest_path):
            raise ValueError("Resume content index refers to a different frozen manifest")
        for item in previous["records"]:
            ident = item["candidate_id"]
            if ident not in valid_ids or ident in prior:
                raise ValueError("Resume index contains unknown or duplicate candidate IDs")
            prior[ident] = item
            if item.get("status") == "extracted":
                path = Path(item["document"])
                if sha256_file(path) != item["document_sha256"]:
                    raise ValueError("Resume document checksum changed")
                doc = read_json(path)
                if sha256_file(Path(doc["source"]["artifact"])) != doc["source_sha256"]:
                    raise ValueError("Resume source artifact checksum changed")
    directory = create_run_dir("article-content", config, output_dir)
    config = bind_budget(config, env, directory)
    restore_host_deferrals(manifest.get("host_retry_at", {}))
    restore_host_deferrals(previous.get("host_retry_at", {}))
    index = {"schema_version": 1, "created_at": utc_now(), "source_manifest": str(manifest_path),
             "source_manifest_sha256": sha256_file(manifest_path), "local_only": local_only,
             "selected_population": len(records), "max_records": max_records, "batch_size": batch_size,
             "max_seconds": max_seconds, "resume_index": str(Path(resume_index).resolve()) if resume_index else None,
             "local_html": str(Path(local_html).expanduser().resolve()) if local_html is not None else None,
             "records": [{"candidate_id": r.extra.get("candidate_id") or candidate_id(r),
                          "title": r.title, "status": "pending_budget", "ai_reading_status": "not_submitted"} for r in records]}
    started, processed, reused = time.monotonic(), 0, 0
    def checkpoint():
        index["host_retry_at"] = host_deferrals()
        extracted = sum(r["status"] == "extracted" for r in index["records"])
        index["metrics"] = {"selected_population": len(records), "processed_records": processed,
            "reused_records": reused, "extracted_records": extracted, "ai_read_records": 0,
            "pending_records": sum(r["status"] == "pending_budget" for r in index["records"]),
            "content_acquisition_rate": extracted / len(records) if records else 0,
            "pdf_metrics_unchanged": True, "completed_batches": processed // batch_size}
        save_manifest(directory / "content-index.json", _redact(index, config, env))
    checkpoint()
    for position, record in enumerate(records):
        ident = record.extra.get("candidate_id") or candidate_id(record)
        if prior.get(ident, {}).get("status") == "extracted":
            item = prior[ident]
            reused += 1
        elif (not max_records or processed < max_records) and (not max_seconds or time.monotonic()-started < max_seconds):
            try:
                options = {"local_html": local_html} if local_html is not None else {}
                item = acquire_document(record, directory / f"article-{position+1:04d}", config, env, local_only=local_only, **options)
            except Exception as exc:
                item = {"candidate_id": ident, "title": record.title, "status": "content_unavailable",
                        "error": _safe_message(exc, effective_contact_email(config, env)), "ai_reading_status": "not_submitted"}
            processed += 1
        else:
            continue
        index["records"][position] = item
        checkpoint()
    checkpoint()
    return directory, index


def screen_candidates(manifest_path, decisions_path, output_dir, config, *, include_uncertain=False):
    manifest_path = Path(manifest_path).resolve()
    manifest, selected = load_manifest(manifest_path)
    context = manifest.get("research_context", {})
    ref = context.get("candidates.json")
    if ref:
        if sha256_file(Path(ref["path"])) != ref["sha256"]:
            raise ValueError("Candidate inventory checksum changed")
        records = [PaperRecord.from_dict(r) for r in read_json(ref["path"])["records"]]
    else:
        records = selected
    decisions = read_json(decisions_path)
    if not isinstance(decisions, list):
        raise ValueError("Decisions must be a list")
    by_id = _candidate_map(records)
    seen = set()
    for entry in decisions:
        if not isinstance(entry, dict) or set(entry) != {"candidate_id", "decision", "reason", "basis", "actor"}:
            raise ValueError("Each decision requires candidate_id, decision, reason, basis and actor")
        ident = entry["candidate_id"]
        if not isinstance(ident, str) or ident not in by_id or ident in seen or entry["decision"] not in {"include", "exclude", "uncertain"}:
            raise ValueError("Unknown/duplicate candidate or invalid decision")
        if any(not isinstance(entry[k], str) or not entry[k].strip() for k in ("reason", "basis", "actor")):
            raise ValueError("Decision reasons, evidence basis and actor cannot be empty")
        seen.add(ident)
    directory = create_run_dir("screening", config, output_dir)
    ledger = {"schema_version": 1, "at": utc_now(), "source_manifest": str(manifest_path),
              "source_manifest_sha256": sha256_file(manifest_path), "candidate_inventory": ref,
              "decisions": decisions, "unreviewed_ids": sorted(set(by_id) - seen),
              "included_ids": [d["candidate_id"] for d in decisions if d["decision"] == "include"],
              "assessment_ids": [d["candidate_id"] for d in decisions if include_uncertain and d["decision"] == "uncertain"],
              "note": "Project-specific screening report; source candidates and frozen PDF population unchanged"}
    write_json(directory / "screening.json", ledger)
    reading_ids = ledger["included_ids"] + ledger["assessment_ids"]
    reading_records = [by_id[ident] for ident in reading_ids]
    for record in reading_records:
        decision = next(d for d in decisions if d["candidate_id"] == (record.extra.get("candidate_id") or candidate_id(record)))
        record.extra["screening"] = {**decision, "at": ledger["at"]}
    scoped = new_manifest(str(manifest.get("query", "")), reading_records, manifest.get("source_status", {}),
                          {**manifest.get("settings", {}), "output_dir": str(directory),
                           "download": False, "target_pdfs": None, "selection": "include_and_uncertain_assessment" if include_uncertain else "explicit_include_decisions"})
    scoped["parent_manifest_sha256"] = ledger["source_manifest_sha256"]
    scoped["parent_run_id"] = manifest.get("run_id")
    scoped["host_retry_at"] = manifest.get("host_retry_at", {})
    if context:
        scoped["research_context"] = {**context, "selected_candidate_ids": reading_ids}
    scoped["screening_ledger"] = {"path": str((directory / "screening.json").resolve()),
                                  "sha256": sha256_file(directory / "screening.json")}
    save_manifest(directory / "manifest.json", scoped)
    return directory, ledger


def _chunks(doc, size=4000, *, version=3):
    positive(size, "chunk_chars")
    chunks = []
    for element in doc["elements"]:
        value = element["text"] if version == 1 else element_reading_text(element)
        if not isinstance(value, str):
            raise ValueError("Document element text must be a string")
        start = 0
        while start < len(value):
            end = min(start+size, len(value))
            if version > 1 and end < len(value):
                # Prefer paragraph/sentence boundaries, without omitting a byte.
                boundary = max(value.rfind("\n\n", start+size//2, end),
                               value.rfind(". ", start+size//2, end))
                if boundary >= start+size//2:
                    end = boundary + 2
            chunks.append({"id": f"{element['id']}:{start}", "element_id": element["id"],
                           "start": start, "end": end, "text": value[start:end],
                           "kind": element["kind"], "page": element.get("page"), "xml_path": element.get("xml_path"),
                           "section": element.get("section", [])})
            if version >= 3:
                chunks[-1]["locator"] = element_locator(doc, element, start, end)
            start = end
    return chunks


def prepare_reading(document_path, output_dir, config, *, question, max_chars=None, start_chunk=0,
                    previous_receipt=None):
    settings = section(config, "reading")
    max_chars = positive(settings["max_chars"] if max_chars is None else max_chars, "max_chars")
    chunk_chars = min(positive(settings["chunk_chars"], "chunk_chars"), max_chars)
    if not isinstance(question, str) or not question.strip() or len(question) > settings["question_chars"]:
        raise ValueError("A bounded reading question is required")
    document_path = Path(document_path).resolve()
    doc = read_json(document_path)
    if doc.get("schema_version") != 1 or "elements" not in doc:
        raise ValueError("Unsupported article document")
    if sha256_file(Path(doc["source"]["artifact"])) != doc["source_sha256"]:
        raise ValueError("Source artifact checksum changed")
    chunks = _chunks(doc, chunk_chars)
    if type(start_chunk) is not int or start_chunk < 0 or start_chunk >= len(chunks):
        raise ValueError("start_chunk must identify an available chunk")
    picked, used = [], 0
    for chunk in chunks[start_chunk:]:
        if used + len(chunk["text"]) > max_chars:
            break
        picked.append(chunk)
        used += len(chunk["text"])
    end = start_chunk + len(picked)
    directory = create_run_dir("reading-pack", config, output_dir)
    pack = {"schema_version": 3, "chunk_chars": chunk_chars, "at": utc_now(), "question": question.strip(),
            "document": str(document_path), "document_sha256": sha256_file(document_path),
            "source_sha256": doc["source_sha256"], "chunks": picked, "all_chunk_count": len(chunks),
            "start_chunk": start_chunk, "next_start_chunk": end if end < len(chunks) else None,
            "prior_chunks_not_in_this_pack": start_chunk, "remaining_chunks": len(chunks)-end,
            "text_chars": used, "state": "prepared_not_read", "gaps": doc["gaps"],
            "previous_receipt": previous_receipt,
            "receipt_instruction": "Report covered_chunk_ids separately from observations with exact quotes. Continue via reading-next after verification; never manufacture a receipt."}
    write_json(directory / "reading-pack.json", pack)
    with (directory / "reading-pack.md").open("x", encoding="utf-8") as stream:
        stream.write("# Reading input — not a reading receipt\n\nQuestion: " + question.strip() + "\n\n")
        for chunk in picked:
            stream.write(f"## [{chunk['id']}] {chunk['kind']}\n\n{chunk['text']}\n\n")
        stream.write(f"Remaining later chunks: {pack['remaining_chunks']}; prior chunks omitted: {start_chunk}.\n")
        stream.write("Known extraction gaps: " + json.dumps(doc["gaps"], ensure_ascii=False))
    return directory, pack


def verify_receipt(pack_path, receipt_path, output_dir, config):
    pack_path = Path(pack_path).resolve()
    pack, receipt = read_json(pack_path), read_json(receipt_path)
    required = {"pack_sha256", "actor", "observations"}
    if not isinstance(receipt, dict) or not required <= set(receipt) or set(receipt) - required - {"covered_chunk_ids"}:
        raise ValueError("Receipt requires pack_sha256, actor and observations")
    if receipt["pack_sha256"] != sha256_file(pack_path) or not isinstance(receipt["actor"], str) or not receipt["actor"].strip():
        raise ValueError("Receipt pack hash or actor is invalid")
    doc_path = Path(pack["document"])
    if sha256_file(doc_path) != pack["document_sha256"]:
        raise ValueError("Article document checksum changed")
    doc = read_json(doc_path)
    if sha256_file(Path(doc["source"]["artifact"])) != pack["source_sha256"]:
        raise ValueError("Source artifact checksum changed")
    originals = {c["id"]: c for c in _chunks(doc, pack.get("chunk_chars", 4000), version=pack.get("schema_version", 1))}
    offered = {c["id"]: c for c in pack["chunks"]}
    if len(offered) != len(pack["chunks"]) or any(c != originals.get(i) for i, c in offered.items()):
        raise ValueError("Pack chunks differ from the source document")
    observations = receipt["observations"]
    if not isinstance(observations, list) or not observations:
        raise ValueError("Receipt requires at least one source-grounded observation")
    seen = set()
    for entry in observations:
        if not isinstance(entry, dict) or set(entry) != {"chunk_id", "quote", "note"}:
            raise ValueError("Observation requires chunk_id, quote and note")
        ident = entry["chunk_id"]
        if not isinstance(ident, str) or ident not in offered or ident in seen:
            raise ValueError("Observation chunk is absent or repeated")
        if not isinstance(entry["quote"], str) or not entry["quote"].strip() or entry["quote"] not in offered[ident]["text"]:
            raise ValueError("Observation quotation is not present verbatim in its chunk")
        if not isinstance(entry["note"], str) or not entry["note"].strip():
            raise ValueError("Observation note is required")
        seen.add(ident)
    covered = receipt.get("covered_chunk_ids", sorted(seen))
    if (not isinstance(covered, list) or any(not isinstance(i, str) for i in covered)
            or len(set(covered)) != len(covered) or not set(covered) <= set(offered) or not seen <= set(covered)):
        raise ValueError("covered_chunk_ids must be unique offered IDs and include quoted observations")
    previous_covered, previous_observed = set(), set()
    previous = pack.get("previous_receipt")
    if previous:
        validated = _validate_previous_receipt(previous)
        if validated["document_sha256"] != pack["document_sha256"] or validated.get("chunk_chars", 4000) != pack.get("chunk_chars", 4000):
            raise ValueError("Previous receipt uses a different document or chunking")
        previous_covered.update(validated["cumulative_covered_ids"])
        previous_observed.update(validated["cumulative_observed_ids"])
    all_covered, all_observed = previous_covered | set(covered), previous_observed | seen
    directory = create_run_dir("reading-receipt", config, output_dir)
    result = {"schema_version": 1, "at": utc_now(), "pack": str(pack_path), **receipt,
              "state": "reported_reading_with_verified_quotes", "quote_integrity_verified": True,
              "raw_receipt": str(Path(receipt_path).resolve()), "raw_receipt_sha256": sha256_file(Path(receipt_path)),
              "document_sha256": pack["document_sha256"], "chunk_chars": pack.get("chunk_chars", 4000),
              "covered_chunk_ids": covered, "cumulative_covered_ids": sorted(all_covered),
              "cumulative_observed_ids": sorted(all_observed), "previous_receipt": previous,
              "reported_text_coverage": len(all_covered) / len(originals) if originals else 0,
              "uncovered_article_ids": sorted(set(originals) - all_covered),
              "semantic_accuracy_verified": False, "model_context_delivery_independently_verified": False,
              "observed_chunks": len(seen), "offered_chunks": len(offered), "article_chunks": len(originals),
              "unobserved_offered_ids": sorted(set(offered) - seen),
              "article_text_coverage": len(all_observed) / len(originals) if originals else 0,
              "coverage_basis": "One anchored observation per reported chunk, not proof every sentence was understood",
              "extraction_gaps": doc["gaps"], "whole_article_read_certified": False}
    write_json(directory / "reading-receipt.json", result)
    return directory, result


def _validate_previous_receipt(reference):
    """Recheck the evidence chain; a generated status label is not a certificate."""
    visited, chain = set(), []
    current = reference
    while current:
        path = Path(current["path"]).resolve()
        if path in visited:
            raise ValueError("Cyclic reading receipt chain")
        visited.add(path)
        if sha256_file(path) != current["sha256"]:
            raise ValueError("Previous receipt checksum changed")
        value = read_json(path)
        pack_path, raw_path = Path(value["pack"]), Path(value["raw_receipt"])
        if sha256_file(pack_path) != value["pack_sha256"] or sha256_file(raw_path) != value["raw_receipt_sha256"]:
            raise ValueError("Previous pack or receipt checksum changed")
        pack, raw = read_json(pack_path), read_json(raw_path)
        if value["document_sha256"] != pack["document_sha256"] or value["chunk_chars"] != pack.get("chunk_chars", 4000):
            raise ValueError("Previous receipt differs from pack source or chunking")
        doc_path = Path(pack["document"])
        if sha256_file(doc_path) != pack["document_sha256"]:
            raise ValueError("Previous document checksum changed")
        doc = read_json(doc_path)
        if sha256_file(Path(doc["source"]["artifact"])) != pack["source_sha256"]:
            raise ValueError("Previous source artifact checksum changed")
        originals = {c["id"]: c for c in _chunks(doc, pack.get("chunk_chars", 4000), version=pack.get("schema_version", 1))}
        offered = {c["id"]: c for c in pack["chunks"]}
        if any(c != originals.get(i) for i, c in offered.items()):
            raise ValueError("Previous pack differs from its document")
        observations = raw["observations"]
        seen = [o["chunk_id"] for o in observations]
        covered = raw.get("covered_chunk_ids", sorted(seen))
        if (raw["pack_sha256"] != value["pack_sha256"] or raw["actor"] != value["actor"]
                or not observations or len(set(seen)) != len(seen) or not set(seen) <= set(covered)
                or len(set(covered)) != len(covered) or not set(covered) <= set(offered)
                or any(not o["quote"].strip() or not o["note"].strip()
                    or o["quote"] not in offered[o["chunk_id"]]["text"] for o in observations)):
            raise ValueError("Previous reading evidence is invalid")
        if value.get("previous_receipt") != pack.get("previous_receipt"):
            raise ValueError("Previous receipt chain differs from pack")
        chain.append((value, set(covered), set(seen)))
        current = pack.get("previous_receipt")
    covered, observed = set(), set()
    signature = None
    for value, local_covered, local_observed in reversed(chain):
        current_signature = (value["document_sha256"], value["chunk_chars"])
        if signature and current_signature != signature:
            raise ValueError("Receipt chain changes source or chunking")
        signature = current_signature
        covered |= local_covered
        observed |= local_observed
        if covered != set(value["cumulative_covered_ids"]) or observed != set(value["cumulative_observed_ids"]):
            raise ValueError("Previous cumulative coverage differs from evidence")
    return chain[0][0]


def next_reading(receipt_path, output_dir, config, *, max_chars=None):
    path = Path(receipt_path).resolve()
    reference = {"path": str(path), "sha256": sha256_file(path)}
    result = _validate_previous_receipt(reference)
    pack = read_json(result["pack"])
    if set(c["id"] for c in pack["chunks"]) - set(result["covered_chunk_ids"]):
        raise ValueError("Current pack still has unreported chunks; finish its receipt before advancing")
    next_start = pack["next_start_chunk"]
    if next_start is None:
        return None, {"state": "offered_text_sequence_complete", "reported_text_coverage": result["reported_text_coverage"],
            "uncovered_article_ids": result["uncovered_article_ids"], "extraction_gaps": result["extraction_gaps"],
            "whole_article_read_certified": False}
    continuation_config = {**config, "reading": {**section(config, "reading"), "chunk_chars": result["chunk_chars"]}}
    effective_chars = section(config, "reading")["max_chars"] if max_chars is None else max_chars
    if effective_chars < result["chunk_chars"]:
        raise ValueError("Continuation pack must fit the original chunk size")
    return prepare_reading(pack["document"], output_dir, continuation_config, question=pack["question"],
        max_chars=max_chars, start_chunk=next_start, previous_receipt=reference)
