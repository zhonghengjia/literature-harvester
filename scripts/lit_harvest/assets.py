"""Acquire declared article objects; bytes available never means AI interpreted."""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urljoin
from .fulltext import _fetch_bounded, FullTextError
from .http import is_web_url, redact_url
from .settings import section, positive


def acquire_assets(doc, directory, client, config, *, local_only=False, reuse=False):
    content, resources = section(config, "content"), section(config, "resources")
    cap = positive(content["asset_limit"], "asset_limit", zero=True)
    byte_limit = positive(resources["asset_bytes"], "asset_bytes")
    source = doc["source"]
    source_candidate = source.get("candidate", {})
    permitted = source_candidate.get("oa_route") == "open_access" or source_candidate.get("oa_verified") is True
    cache, attempted, reused = {}, 0, 0
    for element in doc["elements"]:
        refs = element.get("resource_references") or []
        if not refs:
            continue
        previous = {a.get("declared_reference"): a for a in element.get("assets", [])} if reuse else {}
        assets = element["assets"] = []
        for ref in refs:
            url = urljoin(source.get("url", ""), ref)
            item = {"declared_reference": ref, "url": redact_url(url), "status": "pending",
                    "content_interpreted": False}
            assets.append(item)
            old = previous.get(ref, {})
            if old.get("status") == "acquired_not_interpreted":
                path = Path(old.get("path", ""))
                if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == old.get("sha256"):
                    item.update(old, content_interpreted=False, reused_verified=True)
                    cache[url] = {k: v for k, v in item.items() if k != "declared_reference"}
                    reused += 1
                    continue
                item["previous_asset_integrity"] = "missing_or_changed"
            if url in cache:
                item.update(cache[url])
                continue
            if local_only or not content["fetch_assets"] or not permitted:
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
                raw, actual, _ = _fetch_bounded(client, url, byte_limit, "image/*,application/pdf,application/xml,text/plain;q=0.8")
                # Types are established from bytes, not a potentially misleading extension.
                if raw.startswith(b"%PDF-"):
                    extension = ".pdf"
                elif raw.startswith(b"\x89PNG\r\n\x1a\n"):
                    extension = ".png"
                elif raw.startswith(b"\xff\xd8\xff"):
                    extension = ".jpg"
                elif raw.startswith((b"II*\x00", b"MM\x00*")):
                    extension = ".tiff"
                elif raw.startswith((b"GIF87a", b"GIF89a")):
                    extension = ".gif"
                else:
                    raise FullTextError("Object is not a supported PDF/image; it remains a declared link", code="unsupported_asset")
                digest = hashlib.sha256(raw).hexdigest()
                folder = Path(directory) / "assets"
                folder.mkdir(exist_ok=True)
                path = folder / (digest + extension)
                if not path.exists():
                    with path.open("xb") as stream:
                        stream.write(raw)
                item.update(status="acquired_not_interpreted", path=str(path.resolve()), sha256=digest,
                            size_bytes=len(raw), url=redact_url(actual), relationship="source_declared_article_object")
            except Exception as exc:
                item.update(status="unavailable", reason=getattr(exc, "code", type(exc).__name__))
            cache[url] = {k: v for k, v in item.items() if k != "declared_reference"}
        element["resource_status"] = "acquired_not_interpreted" if all(a["status"] == "acquired_not_interpreted" for a in assets) else "partial_or_unavailable"
    pending = [{"element_id": e["id"], "declared_reference": a["declared_reference"], "status": a["status"]}
        for e in doc["elements"] for a in e.get("assets", []) if a["status"] != "acquired_not_interpreted"]
    doc["asset_summary"] = {"attempted": attempted, "budget": cap, "reused_references": reused, "pending": pending,
        "acquired": sum(v["status"] == "acquired_not_interpreted" for v in cache.values()),
        "interpretation_certified": False}


def resume_assets(document_path, output_dir, config, *, local_only=False, client=None):
    from .content_store import load_document, write_document
    from .pipeline import create_run_dir
    from .settings import make_client
    from .http import is_web_url
    doc, digest = load_document(document_path)
    directory = create_run_dir("assets-resume", config, output_dir)
    acquire_assets(doc, directory, client or make_client(config, validate_redirects=is_web_url), config,
                   local_only=local_only, reuse=True)
    doc["asset_continuation"] = {"parent_document": str(Path(document_path).resolve()), "parent_document_sha256": digest}
    doc["ai_reading_status"] = "not_submitted"
    write_document(directory, doc)
    return directory, doc
