"""Source-linked article objects. Extraction never certifies AI reading."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET
from .fulltext import _parse_jats, FullTextError
from .identity import _pdf_backend, verify_pdf_identity
from .models import normalize_doi
from .settings import DEFAULT_CONFIG, positive

PARSER_VERSION = "article-document/4"


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
            "identity_record": {k: getattr(record, k) for k in
                ("title", "authors", "year", "doi", "pmid", "pmcid", "arxiv_id", "sha256")},
            "format": format_name, "elements": [], "gaps": [],
            "ai_reading_status": "not_submitted"}


def jats_document(raw, record, *, max_bytes=DEFAULT_CONFIG["resources"]["xml_bytes"]):
    root = _parse_jats(raw, record.pmcid, preserve_namespaces=True, max_bytes=max_bytes)
    meta = first(root, "article-meta")
    if meta is None or not record.pmcid:
        raise FullTextError("Structured JATS requires verified PMCID metadata", code="identity_mismatch")
    dois = {normalize_doi(text(n)) for n in meta if tag(n) == "article-id" and n.get("pub-id-type") == "doi"}
    dois.discard("")
    if record.doi and dois and dois != {record.doi}:
        raise FullTextError("JATS DOI conflicts with the requested article", code="identity_mismatch")
    body = next(iter(children(root, "body")), None)
    if body is None or not text(body):
        raise FullTextError("JATS body is missing or empty", code="no_usable_body")
    doc = _document(hashlib.sha256(raw).hexdigest(), record, "jats")
    if len(text(body)) < 400:
        doc["gaps"].append({"reason": "short_body_verify_completeness", "body_chars": len(text(body))})
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
                   "source_id": node.get("id", ""),
                   "content_role": "abstract" if section[:1] == ["Abstract"] else "body" if section[:1] == ["Body"] else "backmatter", **fields}
        doc["elements"].append(element)
        return element
    known_containers = {"body", "back", "sec", "abstract", "list", "boxed-text", "disp-quote", "ref-list", "fn-group", "app-group", "app", "floats-group", "speech", "statement", "table-wrap-group", "fig-group", "disp-formula-group"}
    visited = set()
    def walk(node, section):
        if id(node) in visited:
            return
        visited.add(id(node))
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
                if child is not node and tag(child) in {"inline-formula", "disp-formula", "table-wrap", "fig", "supplementary-material", "inline-supplementary-material", "media", "graphic", "inline-graphic"}:
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
        elif kind in {"fig", "supplementary-material", "inline-supplementary-material", "media", "graphic", "inline-graphic"}:
            links = [value for n in node.iter() for key, value in n.attrib.items() if key.split("}")[-1] == "href"]
            supplementary = "supplementary-material" in kind
            declared = [{"href": value, "kind": "supplement" if supplementary else "figure",
                         "source_dom_path": paths[id(n)], "label": text(node)}
                        for n in node.iter() for key, value in n.attrib.items() if key.split("}")[-1] == "href"]
            element = add(node, "figure" if kind == "fig" else "supplement" if supplementary else "media", section,
                          resource_references=links, declared_resources=declared, resource_status="not_acquired")
            # The container already inventories its descendant hrefs. A parent
            # paragraph's descendant traversal must not re-emit each media node.
            visited.update(id(n) for n in node.iter())
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


def pdf_document(path: Path, record, max_pages=0, *, page_batch_size=500,
                 ocr="auto", ocr_language="eng", ocr_dpi=150, page_numbers=None):
    positive(max_pages, "max_pages", zero=True)
    positive(page_batch_size, "page_batch_size")
    positive(ocr_dpi, "ocr_dpi")
    if ocr not in {"auto", "off"}:
        raise ValueError("ocr must be auto or off")
    if page_numbers is not None and (not page_numbers or
            any(type(p) is not int or p < 1 for p in page_numbers) or len(set(page_numbers)) != len(page_numbers)):
        raise ValueError("page_numbers must contain unique positive page numbers")
    def indices(count):
        chosen = list(range(1, count+1)) if page_numbers is None else sorted(page_numbers)
        if any(p > count for p in chosen):
            raise ValueError("Requested page exceeds PDF length")
        return [p-1 for p in (chosen[:max_pages] if max_pages else chosen)]
    raw_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    if not record.sha256 or record.sha256 != raw_hash:
        raise FullTextError("PDF checksum is absent or changed", code="checksum_conflict")
    identity = verify_pdf_identity(path, record, allow_ocr=ocr == "auto")
    if identity.status != "verified":
        raise FullTextError(identity.reason, code="identity_mismatch")
    backend_name, backend = _pdf_backend()
    doc = _document(raw_hash, record, "pdf")
    doc["extraction_options"] = {"max_pages": max_pages, "page_batch_size": page_batch_size,
        "ocr": ocr, "ocr_language": ocr_language, "ocr_dpi": ocr_dpi, "page_numbers": page_numbers}
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
            for index in indices(count):
                try:
                    page = pdf[index]
                    blocks = [(b[4], list(b[:4])) for b in page.get_text("blocks", sort=True) if len(b) > 6 and b[6] == 0]
                    if ocr == "auto" and not any(value.strip() for value, _ in blocks):
                        try:
                            textpage = page.get_textpage_ocr(language=ocr_language, dpi=ocr_dpi, full=True)
                            blocks = [(page.get_text("text", textpage=textpage), None)]
                            doc.setdefault("ocr_pages", []).append(index+1)
                        except Exception as exc:
                            doc["gaps"].append({"page": index+1, "reason": "local_OCR_unavailable_or_failed",
                                                "error_type": type(exc).__name__})
                    add_page(index+1, blocks)
                    if page.get_images():
                        doc["gaps"].append({"page": index+1, "reason": "images_not_interpreted"})
                except Exception as exc:
                    pages.append({"page": index+1, "status": "failed", "error_type": type(exc).__name__})
                    doc["gaps"].append({"page": index+1, "reason": "page_extraction_failed"})
    else:
        with path.open("rb") as stream:
            pdf = backend(stream)
            count = len(pdf.pages)
            for index in indices(count):
                try:
                    add_page(index+1, [(pdf.pages[index].extract_text() or "", None)])
                except Exception as exc:
                    pages.append({"page": index+1, "status": "failed", "error_type": type(exc).__name__})
                    doc["gaps"].append({"page": index+1, "reason": "page_extraction_failed"})
    unprocessed = sorted(set(range(1, count+1)) - {p["page"] for p in pages})
    if unprocessed:
        doc["gaps"].append({"reason": "page_limit", "unprocessed_pages": unprocessed})
    doc["gaps"].append({"reason": "PDF_table_formula_figure_structure_not_verified"})
    processed = len(pages)
    doc["coverage"] = {"page_count": count, "pages": pages, "extraction_status": "partial",
                       "page_batch_size": page_batch_size, "batches": (processed+page_batch_size-1)//page_batch_size,
                       "semantic_accuracy_verified": False, "supplements_processed": False}
    if not doc["elements"] and page_numbers is None:
        raise FullTextError("PDF has no extractable text", code="no_usable_body")
    return doc


def element_reading_text(element):
    """The same complete representation is used by packs and quote verification."""
    value = element["text"]
    if not isinstance(value, str):
        raise ValueError("Document element text must be a string")
    if element["kind"] == "table":
        value += "\n\nStructured table:\n" + json.dumps(
            {k: element[k] for k in ("caption", "rows", "footnotes", "merged_ranges", "office_part", "worksheet_state") if k in element}, ensure_ascii=False)
    if element["kind"] == "formula" and element.get("source_xml"):
        value += "\n\nSource formula XML:\n" + element["source_xml"]
    if element.get("resource_references"):
        value += "\n\nLinked objects (availability is not proof of interpretation):\n" + json.dumps(
            {k: element[k] for k in ("resource_references", "resource_status", "assets") if k in element}, ensure_ascii=False)
    return value


def element_locator(doc, element, start=0, end=None):
    """Offsets address element_reading_text, not original PDF/HTML byte offsets."""
    value = element_reading_text(element)
    end = len(value) if end is None else end
    if type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(value):
        raise ValueError("Invalid element character range")
    contract = {"parser": doc.get("parser"), "backend": doc.get("backend", {}),
                "options": doc.get("extraction_options", {}), "continuation": doc.get("continuation", {})}
    return {"source_sha256": doc["source_sha256"], "parser": doc.get("parser"),
            "extraction_sha256": hashlib.sha256(json.dumps(contract, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            "element_id": element["id"], "text_sha256": hashlib.sha256(value.encode()).hexdigest(),
            "char_start": start, "char_end": end, "offset_basis": "element_reading_text_unicode_codepoints",
            **({"object_source": element["object_source"]} if element.get("object_source") else {}),
            **{k: element[k] for k in ("page", "bbox", "xml_path", "dom_path", "source_id", "section") if k in element}}


def render_document(doc):
    lines = ["# " + doc["article"]["title"], "", "Extraction artifact, not an AI-reading certificate.", ""]
    for element in doc["elements"]:
        locator = f"page {element['page']}" if "page" in element else element.get("xml_path", element.get("dom_path", ""))
        lines.extend([f"## [{element['id']}] {element['kind']} — {locator}", element_reading_text(element), ""])
    lines.extend(["## Known gaps", *["- " + str(gap) for gap in doc["gaps"]]])
    return "\n".join(lines) + "\n"
