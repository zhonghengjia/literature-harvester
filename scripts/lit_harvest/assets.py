"""Source-declared objects: acquisition and extraction are not AI reading."""
from __future__ import annotations
import copy
import hashlib
from pathlib import Path
from urllib.parse import urljoin, urldefrag
from .fulltext import _fetch_bounded, FullTextError
from .http import is_web_url, redact_url
from .settings import section, positive
from .object_document import parse_object


def _declarations(element):
    declared = list(element.get("declared_resources") or [])
    known = {d["href"] for d in declared}
    for ref in element.get("resource_references") or []:
        if ref not in known:
            declared.append({"href": ref, "kind": "supplement" if element.get("kind") == "supplement" else "figure",
                "source_dom_path": element.get("dom_path", element.get("xml_path", "")), "label": element.get("text", "")[:300]})
            known.add(ref)
    return declared


def _permission(source):
    candidate = source.get("candidate", {})
    if candidate.get("oa_route") == "author_manuscript_tdm":
        return False
    license_value = str(candidate.get("license", "")).strip().casefold()
    return (candidate.get("oa_route") == "open_access" or candidate.get("oa_verified") is True or
            license_value not in {"", "unknown", "none", "null", "tdm", "n/a"})


def _parse(raw, declaration, doc, element, byte_limit):
    if declaration["kind"] == "table_html":
        from .html_document import table_page_document
        from .models import PaperRecord
        parsed = table_page_document(raw, raw.decode("utf-8-sig"), PaperRecord.from_dict(doc["identity_record"]),
                                     element.get("source_id") or declaration.get("label"))
        return {"format": "html", "parser": parsed["parser"], "elements": parsed["elements"], "gaps": parsed["gaps"]}
    try:
        return parse_object(raw, name=declaration["href"], max_bytes=byte_limit)
    except Exception as exc:
        # Acquisition and extraction have separate outcomes. Retain declared PDF
        # bytes for manual inspection even when the parser cannot read them.
        if raw.startswith(b"%PDF-"):
            return {"format": "pdf", "parser": "declared-object/1", "elements": [],
                    "gaps": [{"reason": "object_text_extraction_failed", "error_code": getattr(exc, "code", type(exc).__name__)}]}
        raise


def acquire_assets(doc, directory, client, config, *, local_only=False, reuse=False):
    settings, resources = section(config, "content"), section(config, "resources")
    cap = positive(settings["asset_limit"], "asset_limit", zero=True)
    byte_limit = positive(resources["asset_bytes"], "asset_bytes")
    source, cache, attempted, reused = doc["source"], {}, 0, 0
    permitted = _permission(source)
    # Continuations rebuild derived objects from verified bytes, with new receipts.
    doc["elements"] = [e for e in doc["elements"] if not e.get("object_source")]
    doc["gaps"] = [g for g in doc.get("gaps", []) if not g.get("object_source_sha256")]
    appended, appended_keys = [], {}
    base = source.get("url", "")
    for element in list(doc["elements"]):
        declarations = _declarations(element)
        if not declarations:
            continue
        previous = {a.get("declared_reference"): a for a in element.get("assets", [])} if reuse else {}
        assets = element["assets"] = []
        for declaration in declarations:
            ref = declaration["href"]
            url = urljoin(base, ref)
            key = (urldefrag(url)[0], declaration["kind"],
                   (element.get("source_id") or declaration.get("label")) if declaration["kind"] == "table_html" else "")
            item = {"declared_reference": ref, "url": redact_url(url), "kind": declaration["kind"],
                    "declaration": declaration, "status": "pending", "content_interpreted": False}
            assets.append(item)
            old, parsed, raw = previous.get(ref, {}), None, None
            if ref.startswith("#") or (urldefrag(url)[1] and urldefrag(url)[0] == urldefrag(base)[0]):
                item.update(status="inventory_only", reason="same_document_anchor")
                continue
            if key in cache:
                saved, parsed = cache[key]
                item.update(copy.deepcopy(saved), declaration=declaration, declared_reference=ref)
            else:
                if old.get("status") == "acquired_not_interpreted":
                    path = Path(old.get("path", ""))
                    if path.is_file() and path.stat().st_size <= byte_limit:
                        candidate_raw = path.read_bytes()
                        if hashlib.sha256(candidate_raw).hexdigest() == old.get("sha256"):
                            raw = candidate_raw
                            item.update(old, content_interpreted=False, reused_verified=True)
                            reused += 1
                    if raw is None:
                        item["previous_asset_integrity"] = "missing_or_changed"
                if raw is None:
                    if local_only or not settings["fetch_assets"] or not permitted:
                        item.update(status="not_acquired", reason="local_only_or_permission_not_established")
                        continue
                    if not is_web_url(url):
                        item.update(status="not_acquired", reason="not_a_declared_web_object")
                        continue
                    if attempted >= cap:
                        item.update(status="pending_budget")
                        continue
                    attempted += 1
                    try:
                        raw, actual, _ = _fetch_bounded(client, url, byte_limit,
                            "application/pdf,application/vnd.openxmlformats-officedocument.*,text/html,text/plain,image/*;q=0.8")
                        item["url"] = redact_url(actual)
                    except Exception as exc:
                        item.update(status="unavailable", reason=getattr(exc, "code", type(exc).__name__))
                if raw is not None:
                    try:
                        parsed = _parse(raw, declaration, doc, element, byte_limit)
                        digest = hashlib.sha256(raw).hexdigest()
                        folder = Path(directory) / "assets"
                        folder.mkdir(parents=True, exist_ok=True)
                        path = folder / (digest + "." + parsed["format"])
                        if not path.exists():
                            with path.open("xb") as stream:
                                stream.write(raw)
                        elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                            raise FullTextError("Existing object hash conflict", code="checksum_conflict")
                        item.update(status="acquired_not_interpreted", path=str(path.resolve()), sha256=digest,
                            size_bytes=len(raw), relationship="source_declared_article_object",
                            extraction_status="extracted_not_read" if parsed["elements"] else "no_text_extracted",
                            parser=parsed["parser"], format=parsed["format"], extracted_elements=len(parsed["elements"]))
                    except Exception as exc:
                        parsed = None
                        item.update(status="unavailable", reason=getattr(exc, "code", type(exc).__name__))
                cache[key] = (copy.deepcopy(item), parsed)
            object_key = (item.get("sha256"), parsed.get("parser"), key[2] if declaration["kind"] == "table_html" else "file") if parsed else None
            if parsed and object_key in appended_keys:
                binding = appended_keys[object_key]
                extra = {"element_id": element["id"], "declaration": declaration, "url": item["url"]}
                if extra not in binding.setdefault("also_declared_by", []):
                    binding["also_declared_by"].append(extra)
                if declaration["kind"] == "supplement":
                    binding["kind"] = "supplement"
            elif parsed:
                binding = {"artifact": item["path"], "sha256": item["sha256"], "url": item["url"],
                    "parser": parsed["parser"], "kind": declaration["kind"], "declared_by_element_id": element["id"],
                    "declaration": declaration, "parent_source_sha256": doc.get("source_sha256", "")}
                appended_keys[object_key] = binding
                for derived in parsed["elements"]:
                    child = copy.deepcopy(derived)
                    child["object_source"] = binding
                    child["section"] = ["Linked table" if declaration["kind"] == "table_html" else "Supplementary material", *child.get("section", [])]
                    child["content_role"] = "linked_table" if declaration["kind"] == "table_html" else "supplement"
                    appended.append(child)
                for gap in parsed["gaps"]:
                    doc["gaps"].append({**gap, "object_source_sha256": item["sha256"], "declared_by_element_id": element["id"]})
        element["resource_status"] = "acquired_not_interpreted" if all(a["status"] == "acquired_not_interpreted" for a in assets) else "partial_or_unavailable"
    next_id = max((int(e["id"][1:]) for e in doc["elements"] if e["id"].startswith("e") and e["id"][1:].isdigit()), default=0)
    for number, element in enumerate(appended, next_id+1):
        element["id"] = f"e{number:05d}"
        doc["elements"].append(element)
    for gap in doc["gaps"]:
        derived = [e for e in appended if e["object_source"]["declared_by_element_id"] == gap.get("element_id")]
        if not derived:
            continue
        if gap.get("reason") == "resource_content_not_processed":
            gap.update(reason="linked_object_extracted_interpretation_unverified", derived_element_ids=[e["id"] for e in derived])
        elif gap.get("reason") == "table_has_no_parsed_cells" and any(e.get("rows") for e in derived):
            gap.update(resolution="source_declared_linked_table_extracted", derived_element_ids=[e["id"] for e in derived])
    pending = [{"element_id": e["id"], "declared_reference": a["declared_reference"], "status": a["status"]}
        for e in doc["elements"] for a in e.get("assets", []) if a["status"] != "acquired_not_interpreted"]
    entries = [v[0] for v in cache.values()]
    doc["asset_summary"] = {"attempted": attempted, "budget": cap, "reused_references": reused, "pending": pending,
        "acquired": len({v["sha256"] for v in entries if v["status"] == "acquired_not_interpreted"}),
        "text_extracted_objects": len({v["sha256"] for v in entries if v.get("extraction_status") == "extracted_not_read"}),
        "object_count_basis": "unique raw SHA-256 within this article; declarations retained separately",
        "derived_elements": len(appended), "interpretation_certified": False}
    if "coverage" in doc:
        doc["coverage"].update(element_count=len(doc["elements"]), linked_object_elements=len(appended))


def resume_assets(document_path, output_dir, config, *, local_only=False, client=None):
    from .content_store import load_document, write_document
    from .pipeline import create_run_dir
    from .settings import make_client
    # An unread image may be refetched; text already offered to the host must
    # never be silently reconstructed after its bound source has changed.
    doc, digest = load_document(document_path, allow_unread_asset_recovery=True)
    directory = create_run_dir("assets-resume", config, output_dir)
    acquire_assets(doc, directory, client or make_client(config, validate_redirects=is_web_url), config, local_only=local_only, reuse=True)
    doc["asset_continuation"] = {"parent_document": str(Path(document_path).resolve()), "parent_document_sha256": digest}
    doc["ai_reading_status"] = "not_submitted"
    write_document(directory, doc)
    return directory, doc
