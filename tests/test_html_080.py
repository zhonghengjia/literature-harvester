"""Offline minimal shapes, not copies of copyrighted complete articles."""
import hashlib
import json
import unittest

from lit_harvest.fulltext import FullTextError
from lit_harvest.html_document import html_document, table_page_document
from lit_harvest.models import PaperRecord


class HTML080(unittest.TestCase):
    def setUp(self):
        self.rec = PaperRecord(title="Synthetic chloride study", doi="10.1234/example", pmid="123")

    def page(self, body, *, wrapper='class="article-body"', doi="10.1234/example", title="Synthetic chloride study"):
        return (f'<html><head><meta name="citation_title" content="{title}">'
                f'<meta name="citation_doi" content="{doi}"></head><body>'
                f'<div {wrapper}>{body}</div></body></html>')

    def parse(self, body, **kwargs):
        page = self.page(body, **kwargs)
        return html_document(page.encode(), page, self.rec)

    @staticmethod
    def table(id="Tab1"):
        return (f'<div class="c-article-table" id="{id}"><figure><figcaption>Table 1 Chloride levels</figcaption></figure>'
                '<table><tr><th colspan="2">Group</th></tr><tr><td rowspan="2">A</td><td>117</td></tr></table>'
                '<div class="tw-foot"><div><p>HR, hazard ratio.</p></div><div><p>Model 2: adjusted.</p></div></div></div>')

    def test_roles_keep_abstract_and_back_out_of_body_counts(self):
        doc = self.parse('<section class="abstract"><h2>Abstract</h2><p>Summary.</p></section>'
                         '<section><h2>Methods</h2><p>Actual design.</p></section>'
                         '<section><h2>Results</h2><p>Actual result.</p></section>'
                         '<section><h2>Funding</h2><p>Grant.</p></section>'
                         '<section class="ref-list"><h2>References</h2><ul><li>Reference title.</li></ul></section>')
        by_text = {e["text"]: e for e in doc["elements"]}
        self.assertEqual(by_text["Summary."]["content_role"], "abstract")
        self.assertEqual(by_text["Grant."]["content_role"], "backmatter")
        self.assertEqual(by_text["Reference title."]["kind"], "reference")
        self.assertEqual(by_text["Actual design."]["content_role"], "body")
        self.assertEqual(doc["coverage"]["body_text_chars"], len("MethodsActual design.ResultsActual result."))

    def test_abstract_only_ref_only_and_generic_sections_rejected(self):
        for body, wrapper in [('<section class="abstract"><h2>Methods</h2><p>Summary.</p></section>', 'class="article-body"'),
                              ('<h2>References</h2><p>Reference DOI 10.1234/example.</p>', 'class="article-body"'),
                              ('<section><h2>Research</h2><p>Navigation text.</p></section>', 'class="page"')]:
            with self.subTest(body=body), self.assertRaises(FullTextError):
                self.parse(body, wrapper=wrapper)

    def test_wrong_doi_and_duplicate_conflicting_meta_rejected(self):
        with self.assertRaises(FullTextError):
            self.parse('<p>Body.</p>', doi="10.1234/wrong")
        page = self.page('<p>Body.</p>').replace('</head>', '<meta name="citation_doi" content="10.1234/wrong"></head>')
        with self.assertRaises(FullTextError):
            html_document(page.encode(), page, self.rec)

    def test_noise_nested_in_explicit_body_is_not_science(self):
        doc = self.parse('<section aria-label="Contents"><nav><p>Menu noise.</p></nav></section>'
                         '<div class="related-content"><p>Other article.</p></div>'
                         '<script>do_not_execute()</script><p>Science.</p><aside><p>Ad.</p></aside>')
        self.assertEqual([e["text"] for e in doc["elements"]], ["Science."])

    def test_flat_body_late_reference_heading_does_not_erase_body(self):
        doc = self.parse('<p>Science first.</p><h2>References</h2>'
                         '<p>Reference with <a href="/supplement.docx">Supplementary results</a>.</p>')
        self.assertEqual(doc["elements"][0]["content_role"], "body")
        self.assertEqual(doc["elements"][-1]["kind"], "reference")
        self.assertEqual(doc["declared_resources"], [])

    def test_supplement_after_body_but_inside_same_article_is_inventoried(self):
        page = self.page('<article><div class="article-body"><p>Science.</p></div>'
                         '<section class="supplementary-materials"><a href="/bin/s1.docx">File 1</a></section></article>',
                         wrapper='class="page"')
        doc = html_document(page.encode(), page, self.rec)
        self.assertEqual(doc["declared_resources"][0]["href"], "/bin/s1.docx")

    def test_pmc_table_caption_footnotes_cells_and_single_object(self):
        doc = self.parse('<h2>Results</h2><section class="tw" id="T1"><h4>Table 1.</h4>'
                         '<div class="caption"><p>Chloride mortality.</p></div>'
                         '<div class="tbl-box"><table><tr><th colspan="2">Model</th></tr><tr><td rowspan="2">A</td><td>1.55</td></tr></table></div>'
                         '<div class="tw-foot"><div><p>HR: hazard ratio.</p></div><div><p>Model 2: age.</p></div></div></section>')
        tables = [e for e in doc["elements"] if e["kind"] == "table"]
        self.assertEqual(len(tables), 1)
        table = tables[0]
        self.assertEqual(table["caption"], "Chloride mortality.")
        self.assertEqual(table["footnotes"], ["HR: hazard ratio.", "Model 2: age."])
        self.assertEqual(table["rows"][0][0]["colspan"], "2")
        self.assertEqual(table["rows"][1][0]["rowspan"], "2")
        self.assertTrue(table["caption_dom_path"])
        self.assertTrue(table["footnote_dom_paths"])
        self.assertEqual(table["source_id"], "T1")
        self.assertEqual(len({e["dom_path"] for e in doc["elements"]}), len(doc["elements"]))

    def test_title_only_table_keeps_only_real_declared_url(self):
        doc = self.parse('<p>Body.</p><div class="c-article-table" data-container-section="table" id="table-1">'
                         '<figure><figcaption><b id="Tab1">Table 1 Outcomes</b></figcaption>'
                         '<a data-test="table-link" href="/article/10.1234/example/tables/1">Full size table</a></figure></div>')
        table = next(e for e in doc["elements"] if e["kind"] == "table")
        self.assertEqual(table["rows"], [])
        self.assertEqual(table["resource_references"], ["/article/10.1234/example/tables/1"])
        self.assertEqual(table["declared_resources"][0]["kind"], "table_html")
        self.assertTrue(any(g["reason"] == "table_has_no_parsed_cells" for g in doc["gaps"]))
        self.assertFalse(any(e["kind"] == "figure" for e in doc["elements"]))

    def test_missing_table_url_is_not_invented(self):
        doc = self.parse('<p>Body.</p><div class="c-article-table"><figcaption>Table 1</figcaption></div>')
        table = next(e for e in doc["elements"] if e["kind"] == "table")
        self.assertEqual(table["declared_resources"], [])

    def test_dedicated_sibling_wrapper_does_not_consume_broad_section(self):
        doc = self.parse('<section><h2>Results</h2><p>Important narrative.</p><div>'
                         '<p class="table-caption">Small table.</p><table><tr><td>117</td></tr></table>'
                         '<p class="table-footnote">Unit mmol/L.</p></div><p>Conclusion.</p></section>')
        table = next(e for e in doc["elements"] if e["kind"] == "table")
        self.assertEqual(table["caption"], "Small table.")
        self.assertEqual(table["footnotes"], ["Unit mmol/L."])
        self.assertEqual([e["text"] for e in doc["elements"] if e["kind"] == "paragraph"], ["Important narrative.", "Conclusion."])

    def test_supplement_anchor_docx_duplicate_and_unsafe_scheme(self):
        doc = self.parse('<p>Read <a href="#SM1">Supplementary Table 1</a>.</p>'
                         '<section><h2>Supplementary material</h2><section class="sm" id="SM1">'
                         '<div class="caption"><a href="/bin/SupplementaryFile1.docx">File 1</a></div></section>'
                         '<a href="/bin/SupplementaryFile1.docx">Supplementary copy</a>'
                         '<a href="javascript:alert(1)">Supplement</a></section><p>Real conclusion.</p>')
        refs = [r for r in doc["declared_resources"] if r["kind"] == "supplement"]
        self.assertEqual({r["href"] for r in refs}, {"#SM1", "/bin/SupplementaryFile1.docx"})
        supplements = [e for e in doc["elements"] if e["kind"] == "supplement"]
        self.assertEqual(len(supplements), 2)
        anchor = next(e for e in supplements if e["resource_references"] == ["#SM1"])
        self.assertEqual(anchor["resource_status"], "source_anchor")
        self.assertTrue(anchor["target_dom_path"])

    def test_figure_refs_legacy_and_hash_preserved(self):
        page = self.page('<p>Body.</p><figure id="f1"><img src="figure.png"><figcaption>Flow.</figcaption></figure>')
        doc = html_document(page.encode(), page, self.rec)
        figure = next(e for e in doc["elements"] if e["kind"] == "figure")
        self.assertEqual(figure["resource_references"], ["figure.png"])
        self.assertEqual(figure["declared_resources"][0]["kind"], "figure")
        self.assertEqual(doc["source_sha256"], hashlib.sha256(page.encode()).hexdigest())

    def test_table_page_id_label_and_unique_none(self):
        page = self.page(self.table())
        for target in ("Tab1", "Table 1", None):
            doc = table_page_document(page.encode(), page, self.rec, target)
            table = doc["elements"][0]
            self.assertEqual(table["footnotes"], ["HR, hazard ratio.", "Model 2: adjusted."])
            self.assertEqual(table["rows"][1][1]["text"], "117")
        with self.assertRaises(FullTextError):
            table_page_document(page.encode(), page, self.rec, "Table 2")

    def test_linked_page_identity_and_target_ambiguity_rejected(self):
        for page in (self.page(self.table(), doi="10.1234/wrong"), '<table id="Tab1"><tr><td>117</td></tr></table>',
                     self.page(self.table() + self.table("Tab2"))):
            with self.subTest(page=page[:60]), self.assertRaises(FullTextError):
                table_page_document(page.encode(), page, self.rec, None)
        page = self.page(self.table() + '<div class="c-article-table"><figcaption>Table 2</figcaption></div>')
        with self.assertRaises(FullTextError):
            table_page_document(page.encode(), page, self.rec, None)

    def test_labelled_parent_source_and_reference_doi_not_equivalent(self):
        source = ('<div class="c-article-table__source" data-test="table-source">From: '
                  '<a href="/article/10.1234/example">Synthetic chloride study</a></div>')
        page = source + self.table()
        doc = table_page_document(page.encode(), page, self.rec, "Table 1")
        self.assertEqual(doc["html_identity_basis"], "labelled_parent_title_and_doi_link")
        wrong = '<section class="references">' + source + '</section>' + self.table()
        with self.assertRaises(FullTextError):
            table_page_document(wrong.encode(), wrong, self.rec, "Table 1")

    @staticmethod
    def satellite(number=1):
        # Minimal Springer/Nature captured-page shape; no real article content.
        return (f'<main><header><h1 class="c-article-satellite-title u-h1" id="table-{number}-title">Table {number} Synthetic outcome</h1>'
                '<p class="c-article-satellite-subtitle">From: <a data-test="subtitle" href="/articles/probe">Synthetic chloride study</a></p></header>'
                '<div class="c-article-table-container"><div class="c-article-table-border"><div class="c-table-scroll-wrapper__content">'
                '<table class="data last-table"><thead><tr><th>Exposure</th><th>Outcome</th></tr></thead>'
                '<tbody><tr><td>Chloride</td><td>117</td></tr></tbody></table></div></div>'
                '<div class="c-article-table-footer"><ol><li>First abbreviation.</li><li>Second model note.</li></ol></div></div></main>')

    def test_satellite_table_without_article_body_keeps_caption_and_footer(self):
        page = self.page(self.satellite(), wrapper='class="page"')
        for target in ("table-1", "Tab1", "Table 1", None):
            doc = table_page_document(page.encode(), page, self.rec, target)
            table = doc["elements"][0]
            self.assertEqual(table["caption"], "Table 1 Synthetic outcome")
            self.assertEqual(table["footnotes"], ["First abbreviation.", "Second model note."])
            self.assertEqual(table["rows"][1][1]["text"], "117")
            self.assertTrue(table["caption_dom_path"].endswith("/header[1]/h1[1]"))
            self.assertEqual(table["content_role"], "linked_table")
            self.assertFalse(doc["coverage"]["body_present"])
        with self.assertRaises(FullTextError):
            html_document(page.encode(), page, self.rec)
        with self.assertRaises(FullTextError):
            table_page_document(page.encode(), page, self.rec, "Table 2")

    def nature_satellite(self, *, doi="10.1234/probe", title="Synthetic chloride study", page_type="table", metadata=True):
        data = [{"content": {"article": {"doi": doi}, "contentInfo": {"title": title}},
                 "page": {"category": {"pageType": page_type}}}]
        script = '<script>window.dataLayer = ' + json.dumps(data) + '; window.dataLayer.push({notJson: true});</script>' if metadata else ""
        return script + self.satellite()

    def test_nature_table_requires_structured_parent_and_visible_source(self):
        rec = PaperRecord(title=self.rec.title, doi="10.1234/probe")
        page = self.nature_satellite()
        doc = table_page_document(page.encode(), page, rec, "table-1")
        self.assertEqual(doc["html_identity_basis"], "structured_table_parent_doi_title_and_source_link")
        self.assertEqual(doc["elements"][0]["rows"][1][1]["text"], "117")
        for wrong in (self.nature_satellite(doi="10.1234/wrong"), self.nature_satellite(title="Other article"),
                      self.nature_satellite(page_type="article"), self.nature_satellite(metadata=False),
                      page.replace('href="/articles/probe"', 'href="/articles/another"'),
                      page.replace('From:', 'Unlabelled:'), page.replace('<p class="c-article-satellite-subtitle">', '<p>')):
            with self.subTest(page=wrong[:100]), self.assertRaises(FullTextError):
                table_page_document(wrong.encode(), wrong, rec, "Table 1")

    def test_satellite_wrong_identity_and_multiple_ambiguous_tables_rejected(self):
        for page in (self.page(self.satellite(), wrapper='class="page"', doi="10.1234/wrong"),
                     self.page(self.satellite().replace('</table>', '</table><table><tr><td>Second</td></tr></table>'), wrapper='class="page"')):
            with self.assertRaises(FullTextError):
                table_page_document(page.encode(), page, self.rec, "table-1")

    def test_structured_parent_data_is_json_not_javascript_execution(self):
        rec = PaperRecord(title=self.rec.title, doi="10.1234/probe")
        page = self.nature_satellite().replace('window.dataLayer = [', 'window.dataLayer = buildDynamicData([')
        with self.assertRaises(FullTextError):
            table_page_document(page.encode(), page, rec, "Table 1")


if __name__ == "__main__":
    unittest.main()
