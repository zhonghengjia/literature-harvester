"""Identity-checked article HTML and declared objects with source-tree locators.

Not a browser DOM or a generic readability guesser: only explicit article-body
regions establish main text. Extraction never certifies complete reading.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
from html import escape
from html.parser import HTMLParser
import re
from urllib.parse import urlparse

from .article_document import _document
from .fulltext import FullTextError
from .models import normalize_doi, normalize_pmid, normalize_title

HTML_PARSER_VERSION = "html-document/4.1"
VOID = {"meta", "link", "img", "br", "hr", "input", "source", "wbr", "area", "base", "embed", "param", "track", "col"}
NOISE = {"script", "style", "nav", "aside", "footer", "form", "button", "template", "noscript"}
BACK_HEADINGS = re.compile(r"^(?:references|bibliography|acknowledg\w*|funding|disclosures?|footnotes?|data availability.*|ethics.*|author(?:s)?[’']? contributions?.*|conflict.*|competing interests?.*|publisher[’']?s note|supplementary materials?|supporting information|associated data|declarations?|additional information|author information|rights and permissions|about this article|generative ai statement)$", re.I)


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
    parent: Node | None = field(default=None, repr=False)
    role: str = ""
    section_kind: str = ""


def descendants(node, *, stop=()):
    for child in node.children:
        if isinstance(child, Node) and not child.excluded:
            yield child
            if child.tag not in stop:
                yield from descendants(child, stop=stop)


def node_text(node):
    if isinstance(node, str):
        return node
    return "" if node.excluded else " ".join(node_text(c) for c in node.children)


def clean_text(node):
    return " ".join(node_text(node).split())


def source_html(node):
    if isinstance(node, str):
        return escape(node)
    attrs = "".join(f' {k}="{escape(str(v), quote=True)}"' for k, v in node.attrs.items())
    start = f"<{node.tag}{attrs}>"
    return start if node.tag in VOID else start + "".join(source_html(c) for c in node.children) + f"</{node.tag}>"


def tokens(node):
    return set(node.attrs.get("class", "").lower().split())


def own_role(node):
    marker = " ".join(node.attrs.get(k, "") for k in ("class", "id", "itemprop", "data-container-section"))
    if re.search(r"(?:^|[\s_-])abstract(?:$|[\s_-])", marker, re.I) or node.attrs.get("role") == "doc-abstract":
        return "abstract", "abstract"
    if re.search(r"(?:^|[\s_-])(?:ref-list|references|bibliography|front-matter|back-matter|supplementary-materials|author-information|article-info|copyright|license|affiliations|correspondence|kwd-group)(?:$|[\s_-])", marker, re.I):
        return "backmatter", "references" if re.search(r"ref-list|references|bibliography", marker, re.I) else "backmatter"
    if node.tag in {"section", "div"}:
        # A late References heading must not relabel earlier prose in a flat body.
        heading = next((c for c in node.children if isinstance(c, Node) and c.tag not in {"a", "span"}), None)
        if heading is not None and re.fullmatch(r"h[1-6]", heading.tag) and BACK_HEADINGS.fullmatch(clean_text(heading).strip().rstrip(":")):
            return "backmatter", normalize_title(clean_text(heading)).replace(" ", "_")
    return "", ""


class ArticleHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.metadata = {}
        self.script_texts = []
        self.table_parent_metadata = []
        self.root = Node("document")
        self.stack = [self.root]

    def handle_starttag(self, name, attrs):
        attrs = {k: v or "" for k, v in attrs}
        if name == "meta":
            key = (attrs.get("name") or attrs.get("property") or "").lower()
            self.metadata.setdefault(key, []).append(attrs.get("content", ""))
        # Common optional end tags only; paths refer to this parser's source tree.
        if name in {"p", "li", "tr", "td", "th"}:
            peer = {"td", "th"} if name in {"td", "th"} else {name}
            if self.stack[-1].tag in peer:
                self.stack.pop()
        parent = self.stack[-1]
        marker = " ".join(attrs.get(k, "") for k in ("class", "id", "itemprop"))
        explicit = bool(re.search(r"(?:^|[\s_-])(?:article-body|articleBody|fulltext|full-text)(?:$|[\s_-])", marker, re.I))
        explicit |= attrs.get("data-article-body", "").lower() == "true"
        excluded = name in NOISE or attrs.get("role") in {"navigation", "banner", "contentinfo"} or bool(re.search(
            r"(?:^|[\s_-])(?:navigation|table-of-contents|toc|login|cookie|advertisement|related-content|recommended-articles|share-tools)(?:$|[\s_-])", marker, re.I))
        excluded |= attrs.get("aria-label", "").strip().lower() in {"contents", "table of contents", "article navigation"}
        parent.counts[name] = parent.counts.get(name, 0) + 1
        node = Node(name, attrs, f"{parent.path}/{name}[{parent.counts[name]}]", self.getpos(),
                    parent.body or explicit, parent.excluded or excluded, parent=parent)
        parent.children.append(node)
        if name not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, name, attrs):
        self.handle_starttag(name, attrs)
        if name not in VOID:
            self.handle_endtag(name)

    def handle_data(self, data):
        self.stack[-1].children.append(data)
        if self.stack[-1].tag == "script":
            self.script_texts.append(data)

    def handle_endtag(self, name):
        for pos in range(len(self.stack)-1, 0, -1):
            if self.stack[pos].tag == name:
                del self.stack[pos:]
                break


def _parse(html):
    parser = ArticleHTML()
    parser.feed(html)
    parser.close()
    def annotate(node, role="", kind=""):
        local_role, local_kind = own_role(node)
        node.role = local_role or role or ("body" if node.body else "")
        node.section_kind = local_kind or kind or node.role
        for child in node.children:
            if isinstance(child, Node):
                annotate(child, node.role, node.section_kind)
    annotate(parser.root)
    # Read only the JSON literal used by dedicated publisher table pages. No
    # JavaScript evaluation, regex DOI harvesting, or arbitrary nested ID search.
    decoder = json.JSONDecoder()
    for script in parser.script_texts:
        for match in re.finditer(r"\bwindow\.dataLayer\s*=\s*(?=\[)", script):
            try:
                literal, end = decoder.raw_decode(script[match.end():])
            except (ValueError, RecursionError):
                continue
            tail = script[match.end()+end:].lstrip()
            if not isinstance(literal, list) or (tail and not tail.startswith(";")):
                continue
            for entry in literal:
                if not isinstance(entry, dict):
                    continue
                page, content = entry.get("page", {}), entry.get("content", {})
                if not isinstance(page, dict) or not isinstance(content, dict):
                    continue
                category, article, info = page.get("category", {}), content.get("article", {}), content.get("contentInfo", {})
                if not all(isinstance(v, dict) for v in (category, article, info)) or category.get("pageType") != "table":
                    continue
                doi, title = article.get("doi"), info.get("title")
                if isinstance(doi, str) and isinstance(title, str) and normalize_doi(doi) and normalize_title(title):
                    parser.table_parent_metadata.append({"doi": normalize_doi(doi), "title": normalize_title(title)})
    return parser


def _identity(parser, record, *, table_page=False):
    meta = parser.metadata
    titles = {normalize_title(v) for v in meta.get("citation_title", [])}
    dois = {normalize_doi(v) for v in meta.get("citation_doi", [])} - {""}
    pmids = {normalize_pmid(v) for v in meta.get("citation_pmid", [])} - {""}
    if ((record.doi and dois and dois != {record.doi}) or (record.pmid and pmids and pmids != {record.pmid})):
        raise FullTextError("HTML identifiers conflict with requested article", code="identity_mismatch")
    if table_page and record.doi and any(m["doi"] != record.doi for m in parser.table_parent_metadata):
        raise FullTextError("Structured table parent DOI conflicts with requested article", code="identity_mismatch")
    id_match = bool((record.doi and dois == {record.doi}) or (record.pmid and pmids == {record.pmid}))
    if id_match and record.normalized_title in titles:
        return "citation_title_and_identifier"
    # Dedicated table pages can declare their parent in a labelled source block,
    # never in arbitrary body prose or a reference-list DOI.
    if table_page and record.doi and not titles:
        for n in descendants(parser.root):
            labelled = n.attrs.get("data-test", "") in {"table-source", "parent-article"} or bool(tokens(n) & {"c-article-table__source", "c-article-satellite-subtitle"})
            if not labelled or n.role in {"abstract", "backmatter"}:
                continue
            for a in descendants(n):
                context = clean_text(n).lower()
                if a.tag != "a" or not (a.attrs.get("rel") == "up" or "from:" in context or "source:" in context):
                    continue
                if normalize_doi(a.attrs.get("href", "")) == record.doi and record.normalized_title == normalize_title(clean_text(a)):
                    return "labelled_parent_title_and_doi_link"
                # Nature table links omit the DOI prefix. Require an independent
                # exact DOI/title declaration in the structured table-page data,
                # plus the visible exact-title parent link; never infer the DOI.
                structured = {"doi": record.doi, "title": record.normalized_title} in parser.table_parent_metadata
                suffix = record.doi.split("/", 1)[1]
                if (structured and record.normalized_title == normalize_title(clean_text(a))
                        and urlparse(a.attrs.get("href", "")).path.rstrip("/") == "/articles/" + suffix):
                    return "structured_table_parent_doi_title_and_source_link"
    raise FullTextError("HTML requires matching citation title and DOI/PMID or explicit table-parent declaration", code="identity_mismatch")


def _table_container(node):
    if node.attrs.get("data-container-section") == "table" or tokens(node) & {"tw", "table-wrap", "table-wrapper", "table-container", "c-article-table", "c-article-table-container"}:
        return True
    # A minimal, dedicated sibling wrapper is also safe. Never promote a broad
    # scientific section (with narrative paragraphs) merely because it has a table.
    children = [c for c in node.children if isinstance(c, Node) and not c.excluded]
    caption_foot = {"caption", "table-caption", "tw-foot", "table-footnotes", "table-footnote"}
    return (node.tag in {"div", "section"} and any(tokens(c) & caption_foot for c in children)
            and sum(c.tag == "table" for c in children) == 1
            and all(c.tag in {"table", "h4", "h5", "figcaption"} or tokens(c) & caption_foot for c in children))


def _reference(node, kind):
    href = node.attrs.get("href") if node.tag == "a" else node.attrs.get("src") or node.attrs.get("data-src")
    if not href or (urlparse(href).scheme and urlparse(href).scheme.lower() not in {"http", "https"}):
        return None
    return {"href": href, "kind": kind, "source_dom_path": node.path,
            "label": clean_text(node) or node.attrs.get("alt", "") or node.attrs.get("aria-label", "")}


def _table_links(container):
    out = []
    for n in descendants(container):
        if n.tag == "a":
            marker = " ".join(n.attrs.get(k, "") for k in ("data-test", "data-track-action", "aria-label"))
            if re.search(r"table-link|view table|full size table", marker + " " + clean_text(n), re.I):
                ref = _reference(n, "table_html")
                if ref:
                    out.append(ref)
    return out


def _table_fields(table, container, *, table_page=False):
    nested = list(descendants(table, stop={"table"})) if table else []
    rows = [[{"text": clean_text(cell), "header": cell.tag == "th",
              "rowspan": cell.attrs.get("rowspan", "1"), "colspan": cell.attrs.get("colspan", "1"),
              "dom_path": cell.path, "source_position": list(cell.position)}
             for cell in row.children if isinstance(cell, Node) and cell.tag in {"td", "th"}]
            for row in nested if row.tag == "tr"]
    captions = [n for n in descendants(container) if n.tag in {"caption", "figcaption"} or tokens(n) & {"caption", "table-caption"}]
    # Dedicated table pages put the caption in an immediately preceding header.
    # Bind it only to the adjacent table container, not globally to every table.
    if table_page and not captions and "c-article-table-container" in tokens(container) and container.parent:
        siblings = [n for n in container.parent.children if isinstance(n, Node) and not n.excluded]
        index = next((i for i, n in enumerate(siblings) if n is container), 0)
        header = siblings[index-1] if index else None
        if header is not None and header.tag == "header":
            candidates = [n for n in descendants(header) if n.tag == "h1" and "c-article-satellite-title" in tokens(n)]
            if len(candidates) == 1 and re.match(r"table\s+\d+(?:\D|$)", clean_text(candidates[0]), re.I):
                captions = candidates
    caption = next((clean_text(n) for n in captions if clean_text(n)), "")
    foot_roots = [n for n in descendants(container) if n.tag == "tfoot" or n.attrs.get("role") == "doc-footnote" or tokens(n) & {"tw-foot", "table-footnotes", "table-footnote", "c-article-table-footer"}]
    footnotes, paths = [], []
    for root in foot_roots:
        parts = ([n for n in descendants(root) if n.tag == "p"]
                 or [n for n in descendants(root) if n.tag == "li" and not any(c.tag == "li" for c in descendants(n))] or [root])
        for part in parts:
            value = clean_text(part)
            if value and part.path not in paths:
                footnotes.append(value)
                paths.append(part.path)
    refs = _table_links(container)
    return {"rows": rows, "caption": caption, "footnotes": footnotes,
            "caption_dom_path": captions[0].path if captions else "", "footnote_dom_paths": paths,
            "container_dom_path": container.path, "source_html": source_html(table or container),
            "source_container_html": source_html(container), "resource_references": list(dict.fromkeys(r["href"] for r in refs)),
            "declared_resources": refs, "resource_status": "inline_extracted" if any(rows) else "not_acquired"}


def _extract(parser, raw, record, *, table_target="", table_page=False):
    doc = _document(hashlib.sha256(raw).hexdigest(), record, "html")
    doc["html_parser"] = HTML_PARSER_VERSION
    headings, visited, inventory = [], set(), {}
    def add(node, kind, **fields):
        role, section_kind = node.role, node.section_kind
        if role == "body" and headings:
            role, section_kind = headings[-1][2:]
        element = {"id": f"e{len(doc['elements'])+1:05d}", "kind": kind, "text": clean_text(node),
                   "section": [v for _, v, _, _ in headings], "content_role": role or "body",
                   "section_kind": section_kind or role or "body", "html_tag": node.tag, "dom_path": node.path,
                   "source_id": node.attrs.get("id", ""), "source_position": list(node.position), **fields}
        element["xrefs"] = [{"rid": n.attrs["href"][1:], "type": "html_fragment", "text": clean_text(n)}
                            for n in descendants(node) if n.tag == "a" and n.attrs.get("href", "").startswith("#")]
        for ref in element.get("declared_resources", []):
            inventory.setdefault((ref["kind"], ref["href"]), ref)
        doc["elements"].append(element)
        return element

    def emit_table(table, container):
        loc = table or container
        fields = _table_fields(table, container, table_page=table_page)
        if table_target:
            candidates = {n.attrs.get("id", "") for n in [container, *descendants(container)]}
            label_match = normalize_title(table_target) in {normalize_title(v) for v in candidates if v}
            wanted = re.fullmatch(r"(?:table|tab|t)[\s_-]*(\d+)", table_target, re.I)
            caption_match = bool(wanted and re.match(rf"table\s*{wanted[1]}(?:\D|$)", fields["caption"], re.I))
            if not (label_match or caption_match):
                return
        source_id = container.attrs.get("id") or loc.attrs.get("id", "")
        e = add(loc, "table", source_id=source_id, **({"content_role": "linked_table", "section_kind": "linked_table"} if table_page else {}), **fields)
        if table_target:
            e["parent_table_id"] = table_target
        if not any(fields["rows"]):
            doc["gaps"].append({"element_id": e["id"], "reason": "table_has_no_parsed_cells",
                                "declared_link_count": len(fields["declared_resources"])})

    def walk(node):
        if node.excluded or id(node) in visited:
            return
        visited.add(id(node))
        if not node.body and not table_page:
            for child in node.children:
                if isinstance(child, Node):
                    walk(child)
            return
        if _table_container(node):
            tables = [n for n in descendants(node, stop={"table"}) if n.tag == "table"]
            for table in tables or [None]:
                emit_table(table, node)
            return
        if node.tag == "table":
            emit_table(node, node)
            return
        if table_page:
            for child in node.children:
                if isinstance(child, Node):
                    walk(child)
            return
        if re.fullmatch(r"h[1-6]", node.tag):
            level, value = int(node.tag[1]), clean_text(node)
            while headings and headings[-1][0] >= level:
                headings.pop()
            role, kind = node.role, node.section_kind
            if BACK_HEADINGS.fullmatch(value.strip().rstrip(":")):
                role, kind = "backmatter", normalize_title(value).replace(" ", "_")
            elif value.lower() == "abstract":
                role, kind = "abstract", "abstract"
            elif role == "body":
                kind = normalize_title(value).replace(" ", "_")
            headings.append((level, value, role, kind))
            add(node, "heading")
        elif node.tag == "figure":
            nested = list(descendants(node))
            refs = [r for n in nested if n.tag == "img" for r in [_reference(n, "figure")] if r]
            e = add(node, "figure", caption=next((clean_text(n) for n in nested if n.tag == "figcaption"), ""),
                    resource_references=list(dict.fromkeys(r["href"] for r in refs)), declared_resources=refs, resource_status="not_acquired")
            doc["gaps"].append({"element_id": e["id"], "reason": "resource_content_not_processed"})
            for child in nested:
                if child.tag in {"table", "math"}:
                    walk(child)
        elif node.tag == "math":
            add(node, "formula", source_xml=source_html(node), interpretation="not_verified")
        elif node.tag in {"p", "li"}:
            if clean_text(node):
                reference = node.section_kind == "references" or (headings and headings[-1][3] in {"references", "bibliography"})
                add(node, "reference" if reference else "paragraph")
            for child in descendants(node, stop={"table", "figure", "math"}):
                if child.tag in {"table", "figure", "math"}:
                    walk(child)
        else:
            for child in node.children:
                if isinstance(child, Node):
                    walk(child)
    walk(parser.root)
    if not table_page:
        ids = {n.attrs.get("id"): n for n in descendants(parser.root) if n.attrs.get("id")}
        seen_supplements = set()
        reference_paths = [e["dom_path"] for e in doc["elements"] if e["kind"] == "reference"]
        for n in descendants(parser.root):
            if n.tag != "a" or not n.attrs.get("href") or n.section_kind == "references":
                continue
            if any(n.path.startswith(path + "/") for path in reference_paths):
                continue
            article = n.parent
            while article is not None and article.tag != "article":
                article = article.parent
            if not n.body and not (article and any(c.body for c in descendants(article))):
                continue
            href = n.attrs["href"]
            target = ids.get(href[1:]) if href.startswith("#") else None
            context, supplementary = n, False
            while context and context.tag not in {"article", "body", "document"}:
                supplementary |= bool(tokens(context) & {"sm", "supplementary-materials", "supplementary-material", "supplement", "supporting-information"})
                context = context.parent
            supplementary |= bool(re.search(r"supplement|supporting[ _-]?(?:information|material)", href + " " + clean_text(n), re.I))
            supplementary |= bool(target and tokens(target) & {"sm", "supplementary-materials", "supplement"})
            if supplementary:
                ref = _reference(n, "supplement")
                if ref and href not in seen_supplements:
                    seen_supplements.add(href)
                    e = add(n, "supplement", text=clean_text(n) or href, section=[], content_role="backmatter", section_kind="supplement",
                            resource_references=[href], declared_resources=[ref], resource_status="source_anchor" if href.startswith("#") else "not_acquired",
                            target_dom_path=target.path if target else "")
                    doc["gaps"].append({"element_id": e["id"], "reason": "resource_content_not_processed"})
    doc["declared_resources"] = list(inventory.values())
    source_ids = [n.attrs["id"] for n in descendants(parser.root) if n.attrs.get("id")]
    duplicates = sorted(k for k, n in Counter(source_ids).items() if n > 1)
    if duplicates:
        doc["gaps"].append({"reason": "duplicate_source_ids", "ids": duplicates})
    unresolved = sorted({x["rid"] for e in doc["elements"] for x in e["xrefs"] if x["rid"] not in source_ids})
    if unresolved:
        doc["gaps"].append({"reason": "unresolved_cross_references", "ids": unresolved})
    body = [e for e in doc["elements"] if e["content_role"] == "body"]
    usable = (any(e["kind"] == "table" and any(e["rows"]) for e in doc["elements"]) if table_page else
              any((e["kind"] == "paragraph" and e["text"]) or (e["kind"] == "table" and any(e["rows"])) for e in body))
    if not usable:
        raise FullTextError("No explicitly marked scientific article body; abstract/backmatter is not full text", code="no_usable_body")
    doc["gaps"].append({"reason": "HTML_figures_formulas_supplements_not_verified"})
    doc["coverage"] = {"scope": "explicit article-body regions with abstract/backmatter roles; declared objects only",
        "body_present": not table_page, "body_element_count": len(body), "body_text_chars": sum(len(e["text"]) for e in body),
        "element_count": len(doc["elements"]), "extraction_status": "partial", "semantic_accuracy_verified": False,
        "supplement_inventory_exhaustive": False, "structure_counts": dict(Counter(e["kind"] for e in doc["elements"])),
        "content_role_counts": dict(Counter(e["content_role"] for e in doc["elements"])),
        "dom_parser": "stdlib HTMLParser; source paths are not a browser-repaired DOM"}
    return doc


def html_document(raw, html, record):
    parser = _parse(html)
    doc_identity = _identity(parser, record)
    doc = _extract(parser, raw, record)
    doc["html_identity_basis"] = doc_identity
    return doc


def table_page_document(raw, html, parent_record, declared_table_id=None):
    """Parse one source-declared table page, with no I/O or URL guessing.

    declared_table_id is a source ID (Tab1/table-1) or label (Table 1), not
    an e00001 ID. None only accepts a single unambiguous nonempty table.
    Caller retains the source-declared URL/parent relationship; #anchors are
    same-document inventory entries, never independent network requests.
    """
    if declared_table_id is not None and (not isinstance(declared_table_id, str) or not declared_table_id.strip()):
        raise FullTextError("A source-declared table ID/label is required", code="identity_mismatch")
    parser = _parse(html)
    doc_identity = _identity(parser, parent_record, table_page=True)
    doc = _extract(parser, raw, parent_record, table_target=(declared_table_id or "").strip(), table_page=True)
    tables = [e for e in doc["elements"] if e["kind"] == "table"]
    if len(tables) != 1 or not any(tables[0]["rows"]):
        raise FullTextError("Declared table must identify exactly one nonempty table", code="identity_mismatch")
    doc["html_identity_basis"] = doc_identity
    doc["parent_table_id"] = declared_table_id
    doc["coverage"]["scope"] = "identity-checked source-declared linked table, not article full text"
    return doc
