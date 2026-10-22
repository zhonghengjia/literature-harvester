"""Opt-in local extraction cache and source-bound continuation/lookup artifacts."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path
from uuid import uuid4
from .article_document import pdf_document, PARSER_VERSION, render_document, element_reading_text, element_locator
from .downloader import sha256_file
from .identity import _pdf_backend
from .models import PaperRecord
from .pipeline import create_run_dir
from .query_plan import digest_json
from .settings import section, positive


def cached_pdf_document(path, record, *, cache_dir="", **options):
    path = Path(path)
    if not cache_dir:
        return pdf_document(path, record, **options)
    source_hash = sha256_file(path)
    if not record.sha256 or source_hash != record.sha256:
        raise ValueError("PDF checksum changed before cache lookup")
    engine, backend = _pdf_backend()
    try:
        engine_version = version(engine)
    except PackageNotFoundError:
        engine_version = str(getattr(backend, "VersionBind", "unknown"))
    contract = {"source_sha256": source_hash, "parser": PARSER_VERSION, "engine": engine,
        "engine_version": engine_version, "options": options,
        "identity": {k: getattr(record, k) for k in ("title", "authors", "year", "doi", "pmid", "pmcid", "arxiv_id")}}
    key = digest_json(contract)
    folder = Path(cache_dir).expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    entry = folder / (key + ".json")
    if entry.exists():
        envelope = json.loads(entry.read_text(encoding="utf-8"))
        doc = envelope.get("document")
        if (envelope.get("contract") != contract or not isinstance(doc, dict) or
                envelope.get("document_sha256") != digest_json(doc) or doc.get("source_sha256") != source_hash):
            raise ValueError("Extraction cache integrity check failed; remove only the identified damaged entry")
        doc["cache"] = {"key": key, "hit": True}
        return doc
    doc = pdf_document(path, record, **options)
    temporary = folder / (key + "." + uuid4().hex + ".tmp")
    temporary.write_text(json.dumps({"contract": contract, "document": doc,
        "document_sha256": digest_json(doc)}, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, entry)
    doc["cache"] = {"key": key, "hit": False}
    return doc


def verify_document_sources(doc, *, allow_unread_asset_recovery=False):
    """Check both the article and every derived object before offering text."""
    source = Path(doc["source"]["artifact"])
    if not source.is_file() or sha256_file(source) != doc.get("source_sha256"):
        raise ValueError("Document source is missing or changed")
    checked = {}
    by_id = {e["id"]: e for e in doc["elements"]}
    derived_paths = {e["object_source"]["artifact"] for e in doc["elements"] if e.get("object_source")}
    for element in doc["elements"]:
        for asset in element.get("assets", []):
            if asset.get("status") != "acquired_not_interpreted":
                continue
            if allow_unread_asset_recovery and asset.get("path") not in derived_paths:
                continue
            path = Path(asset.get("path", ""))
            key = str(path.resolve())
            if key not in checked:
                checked[key] = sha256_file(path) if path.is_file() else None
            if not asset.get("sha256") or checked[key] != asset["sha256"]:
                raise ValueError("Linked object source is missing or changed")
    for element in doc["elements"]:
        binding = element.get("object_source")
        if not binding:
            continue
        parent = by_id.get(binding.get("declared_by_element_id"), {})
        if binding.get("parent_source_sha256") != doc["source_sha256"] or not any(
                a.get("sha256") == binding.get("sha256") and a.get("path") == binding.get("artifact")
                and a.get("declared_reference") == binding.get("declaration", {}).get("href")
                for a in parent.get("assets", [])):
            raise ValueError("Linked object declaration is missing or changed")
        path = Path(binding["artifact"])
        key = str(path.resolve())
        if key not in checked:
            checked[key] = sha256_file(path) if path.is_file() else None
        if checked[key] != binding.get("sha256"):
            raise ValueError("Linked object source is missing or changed")


def load_document(path, *, allow_unread_asset_recovery=False):
    path = Path(path).resolve()
    doc = json.loads(path.read_text(encoding="utf-8"))
    verify_document_sources(doc, allow_unread_asset_recovery=allow_unread_asset_recovery)
    return doc, sha256_file(path)


def write_document(directory, doc):
    path = directory / "document.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump(doc, stream, ensure_ascii=False, indent=2)
    with (directory / "reader.md").open("x", encoding="utf-8") as stream:
        stream.write(render_document(doc))
    return path


def next_pages(document_path, output_dir, config, *, max_pages=0, cache_dir=""):
    positive(max_pages, "max_pages", zero=True)
    previous, parent_hash = load_document(document_path)
    if previous.get("format") != "pdf" or not previous.get("identity_record"):
        raise ValueError("Page continuation requires a PDF document prepared with identity_record metadata")
    count = previous["coverage"]["page_count"]
    processed = {p["page"] for p in previous["coverage"]["pages"]}
    pending = sorted(set(range(1, count+1)) - processed)
    if not pending:
        raise ValueError("No unprocessed pages remain; failed/OCR pages are reported separately")
    settings = section(config, "content")
    options = {k: settings[k] for k in ("page_batch_size", "ocr", "ocr_language", "ocr_dpi")}
    record = PaperRecord.from_dict(previous["identity_record"])
    current = cached_pdf_document(previous["source"]["artifact"], record, max_pages=max_pages,
        page_numbers=pending, cache_dir=cache_dir, **options)
    result = copy.deepcopy(previous)
    for element in current["elements"]:
        element["id"] = f"e{len(result['elements'])+1:05d}"
        result["elements"].append(element)
    result["coverage"]["pages"] += current["coverage"]["pages"]
    result["coverage"]["pages"].sort(key=lambda p: p["page"])
    result["coverage"].update(page_batch_size=options["page_batch_size"],
        batches=(len(result["coverage"]["pages"])+options["page_batch_size"]-1)//options["page_batch_size"])
    result["gaps"] = [g for g in previous["gaps"] if g.get("reason") != "page_limit"]
    result["gaps"] += [g for g in current["gaps"] if g.get("reason") != "page_limit" and g not in result["gaps"]]
    pending = sorted(set(range(1, count+1)) - {p["page"] for p in result["coverage"]["pages"]})
    if pending:
        result["gaps"].append({"reason": "page_limit", "unprocessed_pages": pending})
    result["ocr_pages"] = sorted(set(previous.get("ocr_pages", []) + current.get("ocr_pages", [])))
    result["continuation"] = {"parent_document": str(Path(document_path).resolve()),
        "parent_document_sha256": parent_hash, "new_pages": [p["page"] for p in current["coverage"]["pages"]],
        "cache": current.get("cache", {}), "options": options, "backend": current["backend"]}
    result["ai_reading_status"] = "not_submitted"
    result.pop("cache", None)
    directory = create_run_dir("content-next", config, output_dir)
    write_document(directory, result)
    return directory, result


def find_content(document_path, query, *, limit=10, section_filter=""):
    positive(limit, "limit")
    terms = list(dict.fromkeys(re.findall(r"\w+", query.casefold())))
    if not terms:
        raise ValueError("Lookup requires nonempty query terms")
    doc, digest = load_document(document_path)
    hits = []
    for element in doc["elements"]:
        value = element_reading_text(element)
        headings = " ".join(str(s) for s in element.get("section", []))
        if section_filter and section_filter.casefold() not in headings.casefold():
            continue
        score = sum(value.casefold().count(term) for term in terms)
        if score:
            matches = sorted({(m.start(), m.end()) for term in terms
                              for m in re.finditer(re.escape(term), value, re.I)})
            hits.append({"element_id": element["id"], "page": element.get("page"),
                "xml_path": element.get("xml_path"), "section": element.get("section", []),
                "dom_path": element.get("dom_path"), "locator": element_locator(doc, element),
                "match_spans": [list(span) for span in matches[:1000]], "match_span_count": len(matches),
                "score": score, "text": value})
    hits.sort(key=lambda hit: -hit["score"])
    return {"schema_version": 1, "document": str(Path(document_path).resolve()), "document_sha256": digest,
        "query": query, "method": "local_lexical_occurrence_count", "matched_elements": len(hits),
        "hits": hits[:limit], "ai_reading_status": "not_submitted", "full_paper_read": False}
