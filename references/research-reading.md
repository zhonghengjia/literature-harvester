# Research discovery and source-linked reading

Use this reference for query-plan discovery, screening, independent article content, and host-AI reading. Existing run/resume/PDF metrics remain defined in manifest-schema.md; access rules remain in source-policy.md.

## Query plan v1

The host AI constructs the plan from the user's question. Verify medically important terms and any proposed scope restriction; the validator checks structure and a conservative native-query syntax subset, not completeness or clinical validity. Broad and focused routes can coexist. Avoid automatically AND-ing every PICO field or excluding non-OA articles.

~~~json
{
  "schema_version": 1,
  "question": "Clinical evidence concerning hyperchloremia",
  "scope_note": "No unrequested date or population restriction",
  "concepts": [
    {"id": "chloride", "terms": ["hyperchloremia", "hyperchloraemia", "elevated serum chloride"]}
  ],
  "routes": [
    {"id": "pubmed-broad", "source": "pubmed", "query": "hyperchloremia", "syntax": "text", "limit": 100},
    {"id": "pubmed-variants", "source": "pubmed", "concept_ids": ["chloride"], "limit": 100},
    {"id": "epmc-variants", "source": "europe_pmc", "concept_ids": ["chloride"], "limit": 100}
  ]
}
~~~

Supported sources: pubmed, europe_pmc, crossref, openalex, semantic_scholar, arxiv. Disabled sources are reported as disabled. Configurable `plan_limits` bound routes, terms, concepts, text and candidate counts; `scripts/lit_harvest/settings.py` is the authority for defaults. A plan may state explicit `limits`. Resource budgets are not provider entitlements or guaranteed yield. Reserved route IDs selection and openalex_content are not available.

Each route supplies query or concept_ids, not both. Concept compilation ORs terms within a concept and ANDs selected concepts, using PubMed Title/Abstract, Europe PMC TITLE_ABS or arXiv all fields. A concept may include `mesh_terms: [{"term": "a verified heading", "evidence_url": "https://meshb.nlm.nih.gov/..."}]`; PubMed ORs those headings with free text. The host must verify the actual heading; URL-host validation alone does not establish MeSH validity. Native PubMed/Europe PMC/arXiv routes may carry independently checked syntax. arXiv plain queries combine tokens with AND and preserve quoted phrases. Other APIs accept explicit text, not a borrowed Boolean grammar. Never include secrets or arbitrary fetch instructions in a plan.

`query-check --plan <plan.json> --output-dir <new-check>` runs the same validation as discovery and saves query-validation.json. Native queries are checked for balanced quotes/delimiters and trailing Boolean operators, with zero-based character offsets. Unknown field tags are warnings, not rewritten or silently rejected; this is not a full database grammar. Text-mode queries are explicitly not Boolean-validated; database field syntax on a text route is warned about, not rewritten or rejected. Unparenthesized mixed AND/OR is a review warning. The plan's query_validation is always recomputed; supplied claims cannot override it. MeSH evidence URLs remain host-provided evidence, never checker-certified headings.

discover uses the existing source adapters, pagination and pacing. It saves:
- query-plan.json: normalized question, concepts and actual compiled routes.
- candidates.json: complete deduplicated observed pool, including outside-queue and explicitly filtered records.
- work-families.json: non-destructive complete-inventory version groups, with confirmed-provider and provisional metadata layers, original candidate IDs and evidence hashes. This never substitutes representative DOIs or PDF identities.
- manifest.json: selected queue only, retaining normal v2 PDF semantics.
- research_context: file/hash references and selected candidate IDs.

Every candidate in `run` and `discover` has extra.candidate_id, discovery_hits (route/source/rank/rank_semantics/source IDs), concept_evidence, queue_eligible and screening. The default screening state is uncertain. Conflicting identifiers remain separate; candidate IDs distinguish conflicts.

Ranking is reciprocal rank fusion, sum of 1/(60+one-based rank), with the best rank per route and candidate-ID tie-break. Bulk Semantic Scholar and partitioned PubMed routes contribute equal discovery evidence, not a fictitious global relevance rank. Citation counts are display metadata only. Concept evidence is normalized literal phrase matching, not semantic inference. Legacy literal filters remain explicit; an article-type queue prioritizes matching known types and retains unknown types for assessment. Provider metadata does not certify SCIE indexing.

--queue-size controls only the selected queue. --article-type is an explicit queue filter and does not remove records from candidates.json. discover does not download by default; --download enables the existing permitted PDF workflow for its selected queue. It exits 2 when a planned route is disabled, partial or failed, while preserving usable results. An ok source can still be capped; inspect exhausted/continuation rather than assuming complete discovery.

Frozen sidecars must stay present and match their hashes. Resume reuses them and never reruns plan routes. Start a new discovery run to change a plan.

`coverage-plan --plan <original.json> --per-source-budget 100 --output-dir <new-strategy>` creates an envelope plus executable query-plan.json. Only explicit PubMed/Europe PMC concepts (or their exact standard compilation) are accepted; arbitrary handwritten filters are not reverse-guessed. Quotas are split across original precision and native broad routes within each source, without adding population/date/OA restrictions. PubMed broad routes permit Automatic Term Mapping; Europe PMC unfielded phrases have different semantics. Quotas count provider result slots, not unique records or requests. This is an opt-in strategy experiment, not guaranteed recall improvement. PubMed book/chapter rows retain item_type, book title, collective authors, publisher, ISBN and declared accession links; availability is not inferred from document type. Source diagnostics retain returned/parsed/skipped rows and missing requested IDs.

`work-families --manifest <manifest> --output-dir <new-families>` creates a sidecar for that manifest population without modifying it; discovery's automatic sidecar instead covers the complete candidates inventory. `confirmed_layer` uses source-bound explicit preprint/version/manuscript relationships. `families` additionally labels exact-title, compatible author/year/role and distinctive abstract evidence as high_confidence_metadata; weak matches and translations/comments/corrections/retractions stay separate or in review. Bridge conflicts cannot silently collapse multiple works. Counts are record_count, confirmed_layer_family_count and work_family_count separately; provisional groups do not establish independent studies/cohorts or formal version identity. Input candidate IDs and files remain untouched.

## Citation expansion v1

Use `expand --manifest <parent> --seed <candidate-or-namespaced-ID> --direction both --max-pages 5 --max-results 500 --output-dir <new-child>`. Seed IDs must identify unique records in the frozen parent population; there is no title-guessing seed resolution. Directions are referenced (outgoing) and citing (incoming). OpenAlex conversion and identifier merge use the existing adapters; no new ranking or fuzzy identity policy is introduced. No PDF or paid content delivery is invoked.

Each seed/direction uses at most max-pages API pages and max-results returned rows per invocation, with provider page size at most 100. Only seed records are expanded: returned records never recursively become seeds. Child manifest.json contains the deduplicated expansion results, separate from the parent's population; screening/content commands can consume it. Overlap with the parent is possible and must not be added to the parent denominator. expansion-state.json retains source-linked edges, discovery-time IDs, page cursors, received counts and complete/partial_budget/error/disabled states. Edges retain source identifiers because later metadata merging can change a candidate ID. Complete means the API cursor exhausted, not an exhaustive printed bibliography.

`expand` with the same parent/seeds/direction plus `--resume-state <old-expansion-state.json>` writes a new child, retaining acquired metadata and continuing incomplete routes. Changed parent hashes are rejected; completed routes are not repeated. Source failures are not zero-hit successful searches. Exit 2 preserves partial output for disabled, failed or budget-limited routes. Secrets are not copied into state. API authentication/rate limits still apply.

## Screening v1

Supply a JSON list of decisions:

~~~json
[
  {
    "candidate_id": "ID copied from the inventory",
    "decision": "include",
    "reason": "Population and exposure address the question",
    "basis": "Title and abstract; full-text eligibility still needs checking",
    "actor": "host-ai"
  }
]
~~~

Decisions are include, exclude or uncertain. The candidate ID must exist; reason, basis and actor are required. Absence of an abstract is a reason for uncertainty, not automatic exclusion. Record metadata type and actual scope separately from SCIE indexing.

screen writes a fresh screening.json and scoped manifest.json. By default only included records enter that manifest; `--include-uncertain` also queues uncertain records for full-text assessment, keeping their original decisions and separate assessment_ids. Unreviewed IDs stay visible. This can select candidates outside the original queue. Original inventory, manifest and PDF denominator remain unchanged. Screening itself does not download or write to Zotero.

## Independent content v1

content accepts either a legacy frozen manifest, a discovery queue or an explicitly screened manifest; writes a fresh content-index.json plus per-article folders; and never writes back to that source manifest.

For an explicitly supplied saved HTML file, use `content --manifest <single-record-manifest> --local-html <saved.html> --output-dir <new-content>`. This implies local-only, disables all network/asset fetching and enrichment, and requires exactly one selected record with a matching citation title and DOI/PMID. A fresh run is required (no simultaneous --resume-index); ambiguous multi-record matching is not guessed. Bytes are bounded, decoded using the declared meta charset or UTF-8, then copied unchanged into source.html after identity/body validation. Original HTML and manifest remain read-only. Saved abstract/paywall pages or absent/mismatched metadata remain failures, not full text. This is a manual file input, not browser/extension automation.

The default task processes the entire selected population; `content.batch_size` is a batch unit, not a total limit. Explicit `--max-records` and `--max-seconds` stop between records and preserve pending_budget rows; zero means no task-level bound. Individual requests/files remain bounded. Each outcome is checkpointed. `--resume-index` verifies the frozen manifest and successful source/document hashes, reuses successful documents, and retries remaining records into a new directory. A record failure does not terminate the population. Resume merges host_retry_at from both source manifest and prior content index, keeping the later deadline.

`--local-only` performs no network requests. Otherwise current PMC eligibility is re-resolved before declared JATS; verified local PDF, eligible article HTML and online PDF are independent alternatives. If existing routes fail, content invokes the configured free-copy enrichment once and attempts newly declared eligible HTML/PDF routes; it does not activate metered APIs or alter the parent. HTML must have matching citation title and DOI/PMID plus an explicitly marked article-body region; abstract-only pages fail. Native source XML/HTML and exact PDF bytes remain traceable. Short JATS bodies receive a completeness warning rather than automatic rejection; empty bodies, conflicting IDs and entity declarations still fail. BioC and an external model service are not implemented.

Local PDF copying requires its recorded SHA, followed by article identity verification. Missing checksum, uncertainty and changed bytes remain failures. Without usable local/native content, content may acquire a candidate PDF independently; its result belongs to the content index and never changes the frozen PDF metrics. An old prose-only Markdown reader is not promoted to a structured article.

Article folders retain the original source.xml, source.html or source.pdf, document.json and reader.md when extraction succeeds. Failed PDF validation may leave the copied source for inspection; only a successful document is listed as extracted. Native and PDF versions may differ: use the recorded source/version of the actual document, not the old PDF version.

document.json fields include:
- schema_version 1, parser version, source_sha256, article identity and source provenance.
- elements with stable document-local IDs, kind, text, section and XML path/source ID, HTML DOM path/source position, or PDF page/bbox. HTML paths identify the source parser tree, not a browser-repaired DOM.
- JATS and HTML tables: rows/cells, header status, row/column spans, units in original cell text, captions, footnotes and original XML/HTML. Grouped objects recurse without repeated structures. HTML headings preserve section hierarchy; content_role separates body, abstract and backmatter, navigation is excluded. A title-only table has empty rows and a gap, not a completed table. Source-declared external table pages require matching parent identity and target table, not an invented URL.
- Formulas: source XML with namespaces plus readable text; mathematical interpretation is not verified.
- Figures/media/supplements: captions, declared_resources (href/kind/source_dom_path/label) plus legacy references and per-object status/hash/path. Authorized declared PDF/images, DOCX/XLSX and UTF-8 TXT/CSV/TSV objects use the shared per-article asset budget. Same-page anchors are inventory, not fetches. Unsupported objects remain links. Safe Office parsing never extracts arbitrary archive paths or executes macros/scripts/formulas; cached spreadsheet formula values and unformatted styles/dates remain explicitly unverified. Images require host visual reading; PDF/Word layout and spreadsheet charts may remain gaps.
- Derived linked_table/supplement elements retain object_source with raw artifact/hash/parser, declaration and parent element/source hash. Identical file bytes within one article are derived once across URLs and figure/supplement declarations; all original declarations remain inventoried, with additional bindings in also_declared_by. Declared HTML table targets remain distinct even when they share a page. Acquired and text-extracted object counts use unique raw hashes within each article, not declaration counts. These elements enter the same ordered reading inputs but never raise the article or verified-PDF count. Extraction is extracted_not_read, not AI understanding; a failed PDF parser can leave acquired bytes with no readable elements.
- References and inline xrefs, with unresolved/duplicate source-ID diagnostics.
- coverage and gaps, plus ai_reading_status=not_submitted.

PDF extraction uses PyMuPDF or pypdf, processes all pages by default and records page batches. An explicit content.max_pages budget reports unprocessed pages. `content-next --document <document.json> --max-pages N --output-dir <new-document>` extracts only unprocessed pages, preserving previous elements/IDs and recording the parent document hash and continuation options. Zero processes all remaining pages. Page continuation requires identity_record metadata from this version; prepare older PDF content again to obtain it. Previously failed/OCR-incomplete pages remain explicit gaps, not silently counted as recovered. PyMuPDF local OCR is attempted for textless pages when content.ocr is auto; missing Tesseract/languages or OCR failure stays an explicit gap, never a success. No new OCR/model dependency is installed automatically. Neither backend certifies table/figure/formula interpretation or perfect reading order. Native supplement inventory is not claimed exhaustive.

Optional `content --cache-dir <trusted-local-folder>` or content.cache_dir enables a shared PDF extraction cache. Keys include PDF SHA-256, identity metadata, parser/backend versions and extraction options; cache payload integrity is checked before reuse. Empty disables it. A damaged entry is an explicit failure, not silently trusted. This cache contains article text and is local, not a public upload or an AI reading cache. XML/HTML and media are not placed in this PDF parser cache.

`content-find --document <document.json> --query <terms> --section <optional-heading> --limit 10 --output-dir <new-hits>` verifies source bytes and writes lexical occurrence-ranked elements with page/XML/DOM/element locators and document hash. Locators bind source SHA-256, parser/backend/options, rendered element text hash and zero-based Unicode character range; match_spans are navigation offsets, not evidence of reading. At most 1,000 match spans per returned element are emitted, with match_span_count preserving the full count. It is deterministic local lookup, not semantic reranking, evidence synthesis or whole-paper reading. Existing ordered reading packs remain the full-text reading route.

`assets-resume --document <document.json> --output-dir <new-document>` verifies the original and any sources already used for derived text, reuses hash-verified bytes and retries unresolved declared objects within the per-invocation budget. Use --local-only to disable fetching. Derived elements are rebuilt into a new artifact; old receipts remain pinned to the parent. Pending/unsupported/failed objects remain explicit, reuse counts separate from attempts. It never changes the old document or PDF metrics and never certifies interpretation. Object format is established from bytes/Office package structure, not a PDF filename; delimited/plain-text objects require a declared matching suffix and valid text. Supplemental PDF identity derives from the declaring parent relationship, not an independent paper-header match.

content-index metrics separate selected population, processed and extracted records. ai_read_records remains zero: extraction cannot establish reading. PDF counters and local_fulltext from the old fallback are untouched. A pending/unavailable selected record causes exit 2 with a retained partial index; an extracted but structurally partial document still requires its gap report to be read.

## Host-AI reading packs v3 and compatible receipts

reading-pack takes document.json, a question, a fresh directory, a configurable positive character budget and optional zero-based --start-chunk. Defaults come from reading settings. It verifies source hashes, renders table rows/spans/footnotes and formula XML into the reading text, and splits at paragraph/sentence boundaries where possible. IDs encode element and rendered-text offset; source/parser-bound locators retain page/XML/DOM positions. Legacy v1 text-only and v2 rendered-structure packs remain verifiable using their original contracts; new packs are schema_version 3. No frozen pack is rewritten.

The pack is prepared_not_read. Its Markdown is the actual host reading input. JSON retains exact spans, parent and derived-object hashes, omitted/remaining chunks and next_start_chunk. All source bindings are revalidated for preparation, receipt, continuation and content reuse. The budget counts rendered content characters including tables/formulas/supplement text, not prompt tokens. A linked path alone is only availability evidence; parsed object elements expose text for reading, still not proof of host examination or image interpretation.

For whole-article text reading, read the actual pack, submit a grounded receipt, then use `reading-next --receipt <verified-receipt> --output-dir <fresh-folder>`. It revalidates the prior source/pack/raw-receipt chain and advances only after all current offered chunks have been reported covered. Continue to the requested scope; report any earlier omitted chunks or object gaps. A question-focused reading must state its narrower scope. Never generate receipts without actually reading the offered material.

After actually reading the offered text, the host writes:

~~~json
{
  "pack_sha256": "SHA-256 of the exact reading-pack.json",
  "actor": "host-ai",
  "covered_chunk_ids": ["e00001:0"],
  "observations": [
    {"chunk_id": "e00001:0", "quote": "Exact text from this chunk", "note": "What this source passage establishes, or leaves uncertain"}
  ]
}
~~~

reading-receipt verifies source hashes, offered spans, unique reported IDs and verbatim quotations. covered_chunk_ids is a host coverage claim; observations provide separately verified quotation anchors. reported_text_coverage and article_text_coverage have distinct meanings. Chained continuation records cumulative covered/observed IDs and uncovered article IDs without altering old packs or receipts. A receipt may be partial, but reading-next will not advance past an incompletely reported current pack. A terminal sequence can still omit earlier chunks or have extraction/object gaps.

The output explicitly leaves semantic_accuracy_verified, model_context_delivery_independently_verified and whole_article_read_certified false. Quotation integrity is not entailment; one observation does not prove every sentence in that chunk was understood. Do not convert these receipts into an unconditional “AI has fully read and understood the paper” assertion.

## Fixed judged-set evaluation v1

`evaluate --benchmark <judgments.json> --manifest <frozen-run> --k 25 --output-dir <new-evaluation>` evaluates the manifest's deduplicated record order, not unseen literature or an automatically reordered candidate inventory. Use the same independently fixed benchmark and k for comparisons. Benchmark hashes are recorded; a user-supplied frozen_at is provenance, not independently verified timing or scientific adjudication.

~~~json
{"schema_version":1,"benchmark_id":"example-synthetic","kind":"synthetic","frozen_at":"2026-10-03",
 "judgments":[{"ids":["doi:10.1234/example","pmid:123"],"relevant":true,"basis":"Independent work-level judgment"}]}
~~~

Each work has unique aliases, boolean relevant and nonempty basis; duplicate/conflicting aliases are rejected. Precision@K uses K as denominator, including missing ranks. Unknown retrieved judgments produce null precision plus a lower bound, not assumed negatives. Recall is explicitly recall of known relevant benchmark works, not recall of all real literature. Both metrics count a judged work once across aliases; no relevant labels gives null recall. Source failures/partial states are carried unchanged. Downloads and AI reading are not evaluated by these retrieval metrics. Synthetic tests establish calculation behavior only; no live-quality gain may be asserted from them.

All these operations are local orchestration plus permitted retrieval; no hidden LLM, embedding or reranking service is called. Decisions, questions and receipts are project-specific. Cross-project reuse is limited to the opt-in hash-bound PDF cache; citation expansion requires explicit frozen seeds and fresh child runs.
