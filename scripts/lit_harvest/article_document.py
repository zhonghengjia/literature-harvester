"""Source-linked article objects. Extraction never certifies AI reading."""
from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET
from .fulltext import _parse_jats, FullTextError
from .identity import _pdf_backend, verify_pdf_identity
from .models import normalize_doi

PARSER_VERSION = "article-document/1"


def tag(node):
    return node.tag.split("}")[-1]


def text(node):
    return " ".join("".join(node.itertext()).split()) if node is not None else ""


def children(node, name):
    return [n for n in node if tag(n) == name]


def first(node, name):
    return next((n for n in node.iter() if tag(n) == name), None)


def _document(raw_hash, record, format_name):
    return {"schema_version": 1, "parser": PARSER_VERSION, "source_sha256": raw_hash,
            "article": {"title": record.title, "ids": record.identity_keys},
            "format": format_name, "elements": [], "gaps": [],
            "ai_reading_status": "not_submitted"}


def jats_document(raw, record):
    root = _parse_jats(raw, record.pmcid, preserve_namespaces=True)
    meta = first(root, "article-meta")
    if meta is None or not record.pmcid:
        raise FullTextError("Structured JATS requires verified PMCID metadata", code="identity_mismatch")
    dois = {normalize_doi(text(n)) for n in meta if tag(n) == "article-id" and n.get("pub-id-type") == "doi"}
    dois.discard("")
    if record.doi and dois and dois != {record.doi}:
        raise FullTextError("JATS DOI conflicts with the requested article", code="identity_mismatch")
    body = next(iter(children(root, "body")), None)
    if body is None or len(text(body)) < 400:
        raise FullTextError("JATS body is missing or too short", code="no_usable_body")
    doc = _document(hashlib.sha256(raw).hexdigest(), record, "jats")
    paths = {}
    def locate(node, path):
        paths[id(node)] = path
        counts = {}
        for child in node:
            key = tag(child)
            counts[key] = counts.get(key, 0) + 1
            locate(child, f"{path}/{key}[{counts[key]}]")
    locate(root, "/article[1]")
    def add(node, kind, section, **fields):
        element = {"id": f"e{len(doc['elements'])+1:05d}", "kind": kind,
                   "text": text(node), "section": list(section), "xml_path": paths[id(node)],
                   "source_id": node.get("id", ""), **fields}
        doc["elements"].append(element)
        return element
    known_containers = {"body", "back", "sec", "abstract", "list", "boxed-text", "disp-quote", "ref-list", "fn-group", "app-group", "app", "floats-group", "speech", "statement"}
    def walk(node, section):
        kind = tag(node)
        heading = next(iter(children(node, "title")), None)
        if kind in known_containers:
            if heading is not None:
                section = section + [text(heading)]
                add(heading, "heading", section)
            for child in node:
                if tag(child) != "title":
                    walk(child, section)
            return
        if kind in {"p", "list-item", "fn", "ref", "ack"}:
            element = add(node, {"ref": "reference", "fn": "footnote"}.get(kind, "paragraph"), section)
            element["xrefs"] = [{"rid": n.get("rid", ""), "type": n.get("ref-type", ""), "text": text(n)} for n in node.iter() if tag(n) == "xref"]
            # Inline formulas and nested figures/tables need their own non-lossy representation.
            for child in node.iter():
                if child is not node and tag(child) in {"inline-formula", "disp-formula", "table-wrap", "fig", "supplementary-material"}:
                    walk(child, section)
        elif kind == "table-wrap":
            rows = []
            for row in node.iter():
                if tag(row) == "tr":
                    rows.append([{"text": text(cell), "header": tag(cell) == "th",
                                  "rowspan": cell.get("rowspan", "1"), "colspan": cell.get("colspan", "1")}
                                 for cell in row if tag(cell) in {"td", "th"}])
            element = add(node, "table", section, rows=rows, caption=text(first(node, "caption")),
                          footnotes=[text(n) for n in node.iter() if tag(n) == "fn"], source_xml=ET.tostring(node, encoding="unicode"))
            if not rows:
                doc["gaps"].append({"element_id": element["id"], "reason": "table_has_no_parsed_cells"})
        elif kind in {"inline-formula", "disp-formula"}:
            add(node, "formula", section, source_xml=ET.tostring(node, encoding="unicode"), interpretation="not_verified")
        elif kind in {"fig", "supplementary-material", "media", "graphic"}:
            links = [value for n in node.iter() for key, value in n.attrib.items() if key.split("}")[-1] == "href"]
            element = add(node, "figure" if kind == "fig" else "supplement" if kind == "supplementary-material" else "media", section,
                          resource_references=links, resource_status="not_acquired")
            doc["gaps"].append({"element_id": element["id"], "reason": "resource_content_not_processed"})
        else:
            if text(node):
                element = add(node, "other", section)
                doc["gaps"].append({"element_id": element["id"], "reason": "unclassified_xml_element", "tag": kind})
    title = first(meta, "article-title")
    if title is not None:
        add(title, "title", [])
    for abstract in children(meta, "abstract"):
        walk(abstract, ["Abstract"])
    walk(body, ["Body"])
    for name in ("back", "floats-group"):
        for node in children(root, name):
            walk(node, [name])
    source_ids = [n.get("id") for n in root.iter() if n.get("id")]
    duplicates = sorted(i for i, count in Counter(source_ids).items() if count > 1)
    if duplicates:
        doc["gaps"].append({"reason": "duplicate_source_ids", "ids": duplicates})
    unresolved = sorted({rid for n in root.iter() if tag(n) == "xref" for rid in n.get("rid", "").split() if rid not in source_ids})
    if unresolved:
        doc["gaps"].append({"reason": "unresolved_cross_references", "ids": unresolved})
    doc["coverage"] = {"scope": "article XML body/back plus abstract; linked assets inventoried only",
                       "body_present": True, "element_count": len(doc["elements"]),
                       "extraction_status": "partial" if doc["gaps"] else "structured_text_extracted",
                       "supplement_inventory_exhaustive": False, "semantic_accuracy_verified": False}
    return doc


def pdf_document(path: Path, record, max_pages=500):
    raw_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    if not record.sha256 or record.sha256 != raw_hash:
        raise FullTextError("PDF checksum is absent or changed", code="checksum_conflict")
    identity = verify_pdf_identity(path, record)
    if identity.status != "verified":
        raise FullTextError(identity.reason, code="identity_mismatch")
    backend_name, backend = _pdf_backend()
    doc = _document(raw_hash, record, "pdf")
    doc["backend"] = {"name": backend_name, "version": str(getattr(backend, "VersionBind", "unknown"))}
    pages = []
    def add_page(number, blocks):
        added = 0
        for value, bbox in blocks:
            if value.strip():
                doc["elements"].append({"id": f"e{len(doc['elements'])+1:05d}", "kind": "pdf_text",
                    "text": value.strip(), "page": number, "bbox": bbox, "section": [], "reading_order": "backend_heuristic"})
                added += 1
        pages.append({"page": number, "status": "text_extracted" if added else "no_text", "blocks": added})
        if not added:
            doc["gaps"].append({"page": number, "reason": "no_text_may_require_OCR"})
    if backend_name == "pymupdf":
        with backend.open(path) as pdf:
            count = pdf.page_count
            for index in range(min(count, max_pages)):
                try:
                    page = pdf[index]
                    add_page(index+1, [(b[4], list(b[:4])) for b in page.get_text("blocks", sort=True) if len(b) > 6 and b[6] == 0])
                    if page.get_images():
                        doc["gaps"].append({"page": index+1, "reason": "images_not_interpreted"})
                except Exception as exc:
                    pages.append({"page": index+1, "status": "failed", "error_type": type(exc).__name__})
                    doc["gaps"].append({"page": index+1, "reason": "page_extraction_failed"})
    else:
        with path.open("rb") as stream:
            pdf = backend(stream)
            count = len(pdf.pages)
            for index in range(min(count, max_pages)):
                try:
                    add_page(index+1, [(pdf.pages[index].extract_text() or "", None)])
                except Exception as exc:
                    pages.append({"page": index+1, "status": "failed", "error_type": type(exc).__name__})
                    doc["gaps"].append({"page": index+1, "reason": "page_extraction_failed"})
    if count > max_pages:
        doc["gaps"].append({"reason": "page_limit", "unprocessed_pages": list(range(max_pages+1, count+1))})
    doc["gaps"].append({"reason": "PDF_table_formula_figure_structure_not_verified"})
    doc["coverage"] = {"page_count": count, "pages": pages, "extraction_status": "partial",
                       "semantic_accuracy_verified": False, "supplements_processed": False}
    if not doc["elements"]:
        raise FullTextError("PDF has no extractable text", code="no_usable_body")
    return doc


def render_document(doc):
    lines = ["# " + doc["article"]["title"], "", "Extraction artifact, not an AI-reading certificate.", ""]
    for element in doc["elements"]:
        locator = f"page {element['page']}" if "page" in element else element.get("xml_path", "")
        lines.extend([f"## [{element['id']}] {element['kind']} — {locator}", element["text"], ""])
        if element.get("rows"):
            lines.extend([" | ".join(cell["text"] for cell in row) for row in element["rows"]])
        if element.get("source_xml") and element["kind"] == "formula":
            lines.extend(["```xml", element["source_xml"], "```", ""])
    lines.extend(["## Known gaps", *["- " + str(gap) for gap in doc["gaps"]]])
    return "\n".join(lines) + "\n"
