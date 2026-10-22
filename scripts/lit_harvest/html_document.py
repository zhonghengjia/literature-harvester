"""Identity-checked explicit article regions with source-linked HTML structure."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
from html import escape
from html.parser import HTMLParser
import re

from .article_document import _document
from .fulltext import FullTextError
from .models import normalize_doi, normalize_pmid, normalize_title

VOID = {"meta", "link", "img", "br", "hr", "input", "source", "wbr", "area", "base", "embed", "param", "track", "col"}
BLOCKS = {"p", "li", "table", "figure", "math", "h1", "h2", "h3", "h4", "h5", "h6"}


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    path: str = ""
    position: tuple = (1, 0)
    body: bool = False
    excluded: bool = False
    children: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)


def descendants(node, *, stop=()):
    for child in node.children:
        if isinstance(child, Node) and not child.excluded:
            yield child
            if child.tag not in stop:
                yield from descendants(child, stop=stop)


def node_text(node):
    if isinstance(node, str):
        return node
    if node.excluded:
        return ""
    return " ".join(node_text(c) for c in node.children)


def clean_text(node):
    return " ".join(node_text(node).split())


def source_html(node):
    if isinstance(node, str):
        return escape(node)
    attrs = "".join(f' {k}="{escape(str(v), quote=True)}"' for k, v in node.attrs.items())
    start = f"<{node.tag}{attrs}>"
    return start if node.tag in VOID else start + "".join(source_html(c) for c in node.children) + f"</{node.tag}>"


class ArticleHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.metadata = {}
        self.root = Node("document")
        self.stack = [self.root]

    def handle_starttag(self, name, attrs):
        attrs = {k: v or "" for k, v in attrs}
        if name == "meta":
            key = (attrs.get("name") or attrs.get("property") or "").lower()
            self.metadata.setdefault(key, []).append(attrs.get("content", ""))
        # Honor common optional end tags, not a full browser DOM repair.
        if name in {"p", "li", "tr", "td", "th"}:
            peer = {"td", "th"} if name in {"td", "th"} else {name}
            if self.stack[-1].tag in peer:
                self.stack.pop()
        parent = self.stack[-1]
        marker = " ".join(attrs.get(k, "") for k in ("class", "id", "itemprop"))
        body = bool(re.search(r"(?:^|[\s_-])(?:article-body|articleBody|fulltext|full-text)(?:$|[\s_-])", marker, re.I))
        excluded = name in {"script", "style", "nav", "aside", "footer", "form"} or bool(
            re.search(r"(?:^|[\s_-])(?:abstract|references|navigation|login)(?:$|[\s_-])", marker, re.I))
        parent.counts[name] = parent.counts.get(name, 0) + 1
        node = Node(name, attrs, f"{parent.path}/{name}[{parent.counts[name]}]", self.getpos(),
                    parent.body or body, parent.excluded or excluded)
        parent.children.append(node)
        if name not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, name, attrs):
        self.handle_starttag(name, attrs)
        if name not in VOID:
            self.handle_endtag(name)

    def handle_data(self, data):
        self.stack[-1].children.append(data)

    def handle_endtag(self, name):
        for pos in range(len(self.stack)-1, 0, -1):
            if self.stack[pos].tag == name:
                del self.stack[pos:]
                break


def html_document(raw, html, record):
    parser = ArticleHTML()
    parser.feed(html)
    parser.close()
    meta = parser.metadata
    titles = {normalize_title(v) for v in meta.get("citation_title", [])}
    dois = {normalize_doi(v) for v in meta.get("citation_doi", [])} - {""}
    pmids = {normalize_pmid(v) for v in meta.get("citation_pmid", [])} - {""}
    if ((record.doi and dois and dois != {record.doi})
            or (record.pmid and pmids and pmids != {record.pmid})):
        raise FullTextError("HTML identifiers conflict with requested article", code="identity_mismatch")
    id_match = bool((record.doi and dois == {record.doi}) or (record.pmid and pmids == {record.pmid}))
    if record.normalized_title not in titles or not id_match:
        raise FullTextError("HTML requires matching citation title and DOI/PMID", code="identity_mismatch")
    doc = _document(hashlib.sha256(raw).hexdigest(), record, "html")
    headings, visited = [], set()

    def add(node, kind, **fields):
        element = {"id": f"e{len(doc['elements'])+1:05d}", "kind": kind,
                   "text": clean_text(node), "section": [v for _, v in headings],
                   "html_tag": node.tag, "dom_path": node.path,
                   "source_id": node.attrs.get("id", ""), "source_position": list(node.position), **fields}
        element["xrefs"] = [{"rid": n.attrs["href"][1:], "type": "html_fragment", "text": clean_text(n)}
                            for n in descendants(node) if n.tag == "a" and n.attrs.get("href", "").startswith("#")]
        doc["elements"].append(element)
        return element

    def walk(node):
        if node.excluded or id(node) in visited:
            return
        visited.add(id(node))
        if not node.body or node.tag not in BLOCKS:
            for child in node.children:
                if isinstance(child, Node):
                    walk(child)
            return
        if re.fullmatch(r"h[1-6]", node.tag):
            level = int(node.tag[1])
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, clean_text(node)))
            add(node, "heading")
        elif node.tag == "table":
            nested = list(descendants(node, stop={"table"}))
            rows = [[{"text": clean_text(cell), "header": cell.tag == "th",
                      "rowspan": cell.attrs.get("rowspan", "1"), "colspan": cell.attrs.get("colspan", "1"),
                      "dom_path": cell.path}
                     for cell in row.children if isinstance(cell, Node) and cell.tag in {"td", "th"}]
                    for row in nested if row.tag == "tr"]
            element = add(node, "table", rows=rows,
                caption=next((clean_text(n) for n in nested if n.tag == "caption"), ""),
                footnotes=[clean_text(n) for n in nested if n.tag == "tfoot" or n.attrs.get("role") == "doc-footnote"],
                source_html=source_html(node))
            if not rows:
                doc["gaps"].append({"element_id": element["id"], "reason": "table_has_no_parsed_cells"})
            for child in nested:
                if child.tag in {"table", "math"}:
                    walk(child)
        elif node.tag == "figure":
            nested = list(descendants(node))
            links = list(dict.fromkeys(n.attrs.get("src") or n.attrs.get("data-src")
                                      for n in nested if n.tag == "img" and (n.attrs.get("src") or n.attrs.get("data-src"))))
            element = add(node, "figure", caption=next((clean_text(n) for n in nested if n.tag == "figcaption"), ""),
                          resource_references=links, resource_status="not_acquired")
            doc["gaps"].append({"element_id": element["id"], "reason": "resource_content_not_processed"})
            for child in nested:
                if child.tag in {"table", "math"}:
                    walk(child)
        elif node.tag == "math":
            add(node, "formula", source_xml=source_html(node), interpretation="not_verified")
        else:
            if clean_text(node):
                add(node, "paragraph")
            for child in descendants(node):
                if child.tag in {"table", "figure", "math"}:
                    walk(child)

    walk(parser.root)
    if not any(e["kind"] in {"paragraph", "table"} and e["text"] for e in doc["elements"]):
        raise FullTextError("No explicitly marked article body; abstract is not full text", code="no_usable_body")
    source_ids = [n.attrs["id"] for n in descendants(parser.root) if n.attrs.get("id")]
    duplicates = sorted(k for k, n in Counter(source_ids).items() if n > 1)
    if duplicates:
        doc["gaps"].append({"reason": "duplicate_source_ids", "ids": duplicates})
    unresolved = sorted({x["rid"] for e in doc["elements"] for x in e["xrefs"] if x["rid"] not in source_ids})
    if unresolved:
        doc["gaps"].append({"reason": "unresolved_cross_references", "ids": unresolved})
    doc["gaps"].append({"reason": "HTML_figures_formulas_supplements_not_verified"})
    doc["coverage"] = {"scope": "explicit article-body regions, not whole web page", "body_present": True,
        "element_count": len(doc["elements"]), "extraction_status": "partial", "semantic_accuracy_verified": False,
        "structure_counts": dict(Counter(e["kind"] for e in doc["elements"])),
        "dom_parser": "stdlib HTMLParser; source paths are not a browser-repaired DOM"}
    return doc
