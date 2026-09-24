"""Independent content packages, screening decisions and source-grounded reading receipts."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .article_document import jats_document, pdf_document, render_document
from .downloader import user_agent, sha256_file, _safe_message
from .fulltext import _fetch_bounded, MAX_XML_BYTES, FullTextError
from .http import HttpClient, is_public_https_url, redact_url, host_deferrals, restore_host_deferrals
from .models import PaperRecord
from .pmc import resolve_pmc_candidates, BUCKET_HOST, is_pmc_listing_candidate
from .pipeline import create_run_dir, load_manifest, new_manifest, save_manifest, utc_now, effective_contact_email, _redact
from .query_plan import candidate_id
from .sources import _add_pmc_cloud


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _qualified_jats(candidate, record):
    version = candidate.get("article_version")
    version_id = f"{record.pmcid}.{version}"
    url = candidate.get("url", "")
    return (candidate.get("source") == "pmc_oa_cloud" and candidate.get("kind") == "jats"
            and candidate.get("pmcid") == record.pmcid and type(version) is int and version > 0
            and bool(candidate.get("license")) and candidate.get("oa_route") in {"open_access", "author_manuscript_tdm"}
            and url == f"https://{BUCKET_HOST}/{version_id}/{version_id}.xml")


def acquire_document(record, directory, config, env, *, local_only=False):
    directory.mkdir(parents=True, exist_ok=False)
    contact = effective_contact_email(config, env)
    attempts = []
    doc = None
    source = {}
    clone = PaperRecord.from_dict(record.to_dict())
    if not local_only and clone.pmcid:
        # Re-resolve current official eligibility; a manifest label alone is not permission.
        _add_pmc_cloud(clone)
        client = HttpClient(timeout=int(config["general"]["request_timeout_seconds"]), max_retries=2,
                            user_agent=user_agent(contact), validate_redirects=is_public_https_url)
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
                raw, actual, _ = _fetch_bounded(client, candidate["url"], MAX_XML_BYTES, "application/xml")
                if candidate.get("expected_md5") and hashlib.md5(raw).hexdigest() != candidate["expected_md5"]:
                    raise FullTextError("PMC object checksum mismatch", code="checksum_conflict")
                doc = jats_document(raw, clone)
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
    if doc is None and record.local_pdf:
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
            doc = pdf_document(copied, record)
            source = {"artifact": str(copied.resolve()), "original_local_path": str(path.resolve()),
                      "download_url": redact_url(record.download_url), "download_version": record.download_version,
                      "license": record.license, "acquired_at": utc_now()}
            attempts.append({"stage": "local_pdf", "outcome": "extracted"})
        except Exception as exc:
            doc = None
            attempts.append({"stage": "local_pdf", "outcome": getattr(exc, "code", "error"), "error": _safe_message(exc, contact)})
    result = {"candidate_id": record.extra.get("candidate_id") or candidate_id(record), "title": record.title,
              "status": "content_unavailable", "attempts": attempts, "ai_reading_status": "not_submitted"}
    if doc is not None:
        doc["source"] = source
        document_path = directory / "document.json"
        write_json(document_path, _redact(doc, config, env))
        with (directory / "reader.md").open("x", encoding="utf-8") as stream:
            stream.write(render_document(doc))
        result.update({"status": "extracted", "document": str(document_path.resolve()),
                       "document_sha256": sha256_file(document_path), "coverage": doc["coverage"], "gaps": doc["gaps"]})
    elif not attempts:
        result["reason"] = "No permitted native fulltext route or verified local PDF; abstract is not full text"
    return result


def prepare_content(manifest_path, output_dir, config, *, local_only=False, max_records=25, env=None):
    if type(max_records) is not int or not 1 <= max_records <= 10000:
        raise ValueError("max_records must be 1..10000")
    manifest_path = Path(manifest_path).resolve()
    manifest, records = load_manifest(manifest_path)
    env = dict(os.environ) if env is None else env
    directory = create_run_dir("article-content", config, output_dir)
    restore_host_deferrals(manifest.get("host_retry_at", {}))
    index = {"schema_version": 1, "created_at": utc_now(), "source_manifest": str(manifest_path),
             "source_manifest_sha256": sha256_file(manifest_path), "local_only": local_only,
             "selected_population": len(records), "max_records": max_records, "records": []}
    for position, record in enumerate(records):
        if position < max_records:
            item = acquire_document(record, directory / f"article-{position+1:04d}", config, env, local_only=local_only)
        else:
            item = {"candidate_id": record.extra.get("candidate_id") or candidate_id(record),
                    "title": record.title, "status": "pending_budget", "ai_reading_status": "not_submitted"}
        index["records"].append(item)
    index["host_retry_at"] = host_deferrals()
    index["metrics"] = {"selected_population": len(records), "processed_records": min(max_records, len(records)),
                        "extracted_records": sum(r["status"] == "extracted" for r in index["records"]),
                        "ai_read_records": 0}
    write_json(directory / "content-index.json", _redact(index, config, env))
    return directory, index


def screen_candidates(manifest_path, decisions_path, output_dir, config):
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
    by_id = {r.extra.get("candidate_id") or candidate_id(r): r for r in records}
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
              "note": "Project-specific screening report; source candidates and frozen PDF population unchanged"}
    write_json(directory / "screening.json", ledger)
    reading_records = [by_id[ident] for ident in ledger["included_ids"]]
    for record in reading_records:
        decision = next(d for d in decisions if d["candidate_id"] == (record.extra.get("candidate_id") or candidate_id(record)))
        record.extra["screening"] = {**decision, "at": ledger["at"]}
    scoped = new_manifest(str(manifest.get("query", "")), reading_records, manifest.get("source_status", {}),
                          {**manifest.get("settings", {}), "output_dir": str(directory),
                           "download": False, "target_pdfs": None, "selection": "explicit_include_decisions"})
    scoped["parent_manifest_sha256"] = ledger["source_manifest_sha256"]
    scoped["parent_run_id"] = manifest.get("run_id")
    scoped["host_retry_at"] = manifest.get("host_retry_at", {})
    if context:
        scoped["research_context"] = {**context, "selected_candidate_ids": ledger["included_ids"]}
    scoped["screening_ledger"] = {"path": str((directory / "screening.json").resolve()),
                                  "sha256": sha256_file(directory / "screening.json")}
    save_manifest(directory / "manifest.json", scoped)
    return directory, ledger


def _chunks(doc, size=4000):
    chunks = []
    for element in doc["elements"]:
        value = element["text"]
        if not isinstance(value, str):
            raise ValueError("Document element text must be a string")
        for start in range(0, len(value), size):
            chunks.append({"id": f"{element['id']}:{start}", "element_id": element["id"],
                           "start": start, "end": min(start+size, len(value)), "text": value[start:start+size],
                           "kind": element["kind"], "page": element.get("page"), "xml_path": element.get("xml_path"),
                           "section": element.get("section", [])})
    return chunks


def prepare_reading(document_path, output_dir, config, *, question, max_chars=24000, start_chunk=0):
    if not isinstance(question, str) or not question.strip() or len(question) > 4000:
        raise ValueError("A bounded reading question is required")
    if type(max_chars) is not int or not 4000 <= max_chars <= 200000:
        raise ValueError("max_chars must be 4000..200000")
    document_path = Path(document_path).resolve()
    doc = read_json(document_path)
    if doc.get("schema_version") != 1 or "elements" not in doc:
        raise ValueError("Unsupported article document")
    if sha256_file(Path(doc["source"]["artifact"])) != doc["source_sha256"]:
        raise ValueError("Source artifact checksum changed")
    chunks = _chunks(doc)
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
    pack = {"schema_version": 1, "at": utc_now(), "question": question.strip(),
            "document": str(document_path), "document_sha256": sha256_file(document_path),
            "source_sha256": doc["source_sha256"], "chunks": picked, "all_chunk_count": len(chunks),
            "start_chunk": start_chunk, "next_start_chunk": end if end < len(chunks) else None,
            "prior_chunks_not_in_this_pack": start_chunk, "remaining_chunks": len(chunks)-end,
            "text_chars": used, "state": "prepared_not_read", "gaps": doc["gaps"]}
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
    if not isinstance(receipt, dict) or set(receipt) != {"pack_sha256", "actor", "observations"}:
        raise ValueError("Receipt requires pack_sha256, actor and observations")
    if receipt["pack_sha256"] != sha256_file(pack_path) or not isinstance(receipt["actor"], str) or not receipt["actor"].strip():
        raise ValueError("Receipt pack hash or actor is invalid")
    doc_path = Path(pack["document"])
    if sha256_file(doc_path) != pack["document_sha256"]:
        raise ValueError("Article document checksum changed")
    doc = read_json(doc_path)
    if sha256_file(Path(doc["source"]["artifact"])) != pack["source_sha256"]:
        raise ValueError("Source artifact checksum changed")
    originals = {c["id"]: c for c in _chunks(doc)}
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
    directory = create_run_dir("reading-receipt", config, output_dir)
    result = {"schema_version": 1, "at": utc_now(), "pack": str(pack_path), **receipt,
              "state": "reported_reading_with_verified_quotes", "quote_integrity_verified": True,
              "semantic_accuracy_verified": False, "model_context_delivery_independently_verified": False,
              "observed_chunks": len(seen), "offered_chunks": len(offered), "article_chunks": len(originals),
              "unobserved_offered_ids": sorted(set(offered) - seen),
              "article_text_coverage": len(seen) / len(originals) if originals else 0,
              "coverage_basis": "One anchored observation per reported chunk, not proof every sentence was understood",
              "extraction_gaps": doc["gaps"], "whole_article_read_certified": False}
    write_json(directory / "reading-receipt.json", result)
    return directory, result
