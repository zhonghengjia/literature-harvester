"""Parse source-declared objects without executing Office content or archive members.

An object belongs to its declaring article, but is not independently a paper.
Extraction is not an assertion that a model read/understood its contents.
"""
from __future__ import annotations

import csv
import io
import posixpath
import re
import zipfile
from xml.etree import ElementTree as ET
from .fulltext import FullTextError
from .identity import _pdf_backend

OBJECT_PARSER = "declared-object/1"


def _xml(raw):
    # XML syntax tokens are ASCII; stripping encoding padding also detects
    # UTF-16/32 declarations before ElementTree can expand an internal entity.
    syntax = raw.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in syntax or b"<!ENTITY" in syntax:
        raise FullTextError("Active XML declarations are unsupported", code="unsafe_object_xml")
    return ET.fromstring(raw)


def _tag(node):
    return node.tag.rsplit("}", 1)[-1]


def _text(node):
    return "".join(node.itertext()).strip()


def _cell(value, **extra):
    return {"text": value, "header": False, "rowspan": "1", "colspan": "1", **extra}


def parse_object(raw, *, name="", max_bytes=100 * 1024 * 1024):
    """Return format/elements/gaps. Unknown formats fail closed, not as text."""
    if len(raw) > max_bytes:
        raise FullTextError("Object exceeds configured resource budget", code="too_large")
    if raw.lstrip().startswith((b"<", b"<!")) and any(marker in raw.lower() for marker in
            (b"pow_challenge", b"cloudpmc-viewer-pow", b"cf-chl-", b"captcha")):
        raise FullTextError("Declared object returned a browser challenge; use the normal source page", code="anti_bot")
    elements, gaps = [], []
    def add(kind, value, **fields):
        elements.append({"kind": kind, "text": value, "section": ["Supplementary material"],
                         "content_role": "supplement", **fields})
    if raw.startswith(b"%PDF-"):
        fmt = "pdf"
        engine, backend = _pdf_backend()
        if engine == "pymupdf":
            with backend.open(stream=raw, filetype="pdf") as pdf:
                if pdf.needs_pass:
                    raise FullTextError("Encrypted supplement", code="encrypted_object")
                for i, page in enumerate(pdf):
                    value = page.get_text("text")
                    if value.strip():
                        add("paragraph", value, page=i+1)
                    else:
                        gaps.append({"reason": "object_page_without_text", "page": i+1})
        else:
            pdf = backend(io.BytesIO(raw))
            if pdf.is_encrypted:
                raise FullTextError("Encrypted supplement", code="encrypted_object")
            for i, page in enumerate(pdf.pages):
                value = page.extract_text() or ""
                if value.strip():
                    add("paragraph", value, page=i+1)
                else:
                    gaps.append({"reason": "object_page_without_text", "page": i+1})
        gaps.append({"reason": "supplement_pdf_tables_figures_not_structurally_verified"})
    elif raw.startswith(b"PK\x03\x04"):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            names = {i.filename for i in infos}
            if (len(infos) > 2048 or len(names) != len(infos) or
                    sum(i.file_size for i in infos) > max_bytes or
                    any(i.flag_bits & 1 or i.filename.startswith(("/", "\\")) or
                        ".." in i.filename.replace("\\", "/").split("/") for i in infos)):
                raise FullTextError("Unsafe or excessive Office archive", code="unsafe_object_archive")
            if any("vbaproject" in n.casefold() for n in names):
                raise FullTextError("Macro-bearing Office file not parsed", code="active_office_content")
            if "word/document.xml" in names:
                fmt = "docx"
                root = _xml(archive.read("word/document.xml"))
                body = next((n for n in root.iter() if _tag(n) == "body"), None)
                if body is None:
                    raise FullTextError("Missing Word body", code="invalid_object")
                def word_text(node):
                    return "".join(n.text or "" for n in node.iter() if _tag(n) == "t")
                def word_walk(node):
                    kind = _tag(node)
                    if kind == "tbl":
                        rows = []
                        for row in node:
                            if _tag(row) != "tr":
                                continue
                            cells = []
                            for cell in row:
                                if _tag(cell) != "tc":
                                    continue
                                span = next((n for n in cell.iter() if _tag(n) == "gridSpan"), None)
                                colspan = next(iter(span.attrib.values()), "1") if span is not None else "1"
                                cells.append(_cell(word_text(cell), colspan=colspan))
                            rows.append(cells)
                        add("table", "Word supplementary table", rows=rows, caption="", footnotes=[])
                    elif kind == "p":
                        value = word_text(node)
                        if value.strip():
                            add("paragraph", value)
                    else:
                        for child in node:
                            word_walk(child)
                word_walk(body)
                for part in ("word/footnotes.xml", "word/endnotes.xml"):
                    if part in names:
                        notes = _xml(archive.read(part))
                        for node in notes:
                            value = word_text(node)
                            if value.strip():
                                add("footnote", value, office_part=part)
                if any(n.startswith("word/media/") for n in names):
                    gaps.append({"reason": "embedded_office_images_not_interpreted"})
                gaps.append({"reason": "word_layout_and_merged_row_semantics_not_verified"})
            elif "xl/workbook.xml" in names:
                fmt = "xlsx"
                shared = []
                if "xl/sharedStrings.xml" in names:
                    shared = [_text(n) for n in _xml(archive.read("xl/sharedStrings.xml"))]
                rels = {}
                relpart = "xl/_rels/workbook.xml.rels"
                if relpart in names:
                    for rel in _xml(archive.read(relpart)):
                        if rel.get("TargetMode") != "External":
                            target = rel.get("Target", "")
                            resolved = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
                            if resolved.startswith("xl/"):
                                rels[rel.get("Id")] = resolved
                workbook = _xml(archive.read("xl/workbook.xml"))
                for sheet in workbook.iter():
                    if _tag(sheet) != "sheet":
                        continue
                    rid = next((v for k, v in sheet.attrib.items() if k.endswith("}id")), "")
                    part = rels.get(rid)
                    if part not in names:
                        gaps.append({"reason": "worksheet_relationship_unavailable", "sheet": sheet.get("name")})
                        continue
                    sheetxml = _xml(archive.read(part))
                    rows = []
                    for row in sheetxml.iter():
                        if _tag(row) != "row":
                            continue
                        cells = []
                        for c in row:
                            if _tag(c) != "c":
                                continue
                            value = next((n.text or "" for n in c if _tag(n) == "v"), "")
                            if c.get("t") == "s":
                                try:
                                    index = int(value)
                                    if not 0 <= index < len(shared):
                                        raise IndexError(index)
                                    value = shared[index]
                                except (ValueError, IndexError):
                                    raise FullTextError("Invalid shared-string index", code="invalid_object") from None
                            elif c.get("t") == "inlineStr":
                                value = "".join(n.text or "" for n in c.iter() if _tag(n) == "t")
                            formula = next((n.text or "" for n in c if _tag(n) == "f"), None)
                            extra = {"coordinate": c.get("r", ""), "value_type": c.get("t", "n")}
                            if formula is not None:
                                extra.update(formula=formula, formula_executed=False, cached_value_unverified=True)
                            cells.append(_cell(value, **extra))
                        rows.append(cells)
                    merges = [n.get("ref") for n in sheetxml.iter() if _tag(n) == "mergeCell"]
                    add("table", "Worksheet: " + sheet.get("name", ""), rows=rows,
                        caption=sheet.get("name", ""), footnotes=[], merged_ranges=merges,
                        office_part=part, worksheet_state=sheet.get("state", "visible"))
                gaps.append({"reason": "spreadsheet_styles_dates_formulas_and_charts_not_interpreted"})
            else:
                raise FullTextError("Only declared DOCX/XLSX archives are parsed", code="unsupported_asset")
    elif raw.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"II*\x00", b"MM\x00*")) or (raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"):
        fmt = "webp" if raw.startswith(b"RIFF") else "png" if raw.startswith(b"\x89PNG") else "jpg" if raw.startswith(b"\xff") else "gif" if raw.startswith(b"GIF") else "tiff"
        gaps.append({"reason": "image_requires_visual_interpretation"})
    elif re.search(r"\.(?:txt|csv|tsv)(?:$|[?#])", name, re.I):
        fmt = "csv" if re.search(r"\.csv(?:$|[?#])", name, re.I) else "tsv" if re.search(r"\.tsv(?:$|[?#])", name, re.I) else "txt"
        value = raw.decode("utf-8-sig")
        if "\x00" in value or re.search(r"<\s*(?:html|script|!doctype)\b", value, re.I):
            raise FullTextError("Response is not declared plain-text content", code="invalid_object")
        if fmt != "txt":
            rows = [[_cell(c) for c in r] for r in csv.reader(io.StringIO(value), delimiter="\t" if fmt == "tsv" else ",")]
            add("table", "Supplementary delimited table", rows=rows, caption="", footnotes=[])
        else:
            add("paragraph", value)
    else:
        raise FullTextError("Unsupported declared object", code="unsupported_asset")
    return {"parser": OBJECT_PARSER, "format": fmt, "elements": elements, "gaps": gaps,
            "ai_reading_status": "not_submitted", "content_interpreted": False}
