# Research discovery and source-linked reading

Use this reference for query-plan discovery, screening, independent article content, and host-AI reading. Existing run/resume/PDF metrics remain defined in manifest-schema.md; access rules remain in source-policy.md.

## Query plan v1

The host AI constructs the plan from the user's question. Verify medically important terms and any proposed scope restriction; the validator checks structure, not completeness or clinical validity. Broad and focused routes can coexist. Avoid automatically AND-ing every PICO field or excluding non-OA articles.

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

Supported sources: pubmed, europe_pmc, crossref, openalex, semantic_scholar, arxiv. Disabled configured sources are reported as disabled. Each plan has 1–18 uniquely named routes, each capped at 1–1,000 candidates, total cap ≤10,000. Up to 12 concepts, each with 1–20 literal terms. Limits bound discovery, not guaranteed yield. Reserved route IDs selection and openalex_content are not available.

Each route supplies either query or concept_ids, not both. Concept compilation ORs terms within a concept and ANDs the selected concepts, using PubMed Title/Abstract, Europe PMC TITLE_ABS or arXiv all fields. This does not automatically add MeSH. The host may supply separately verified native PubMed/Europe PMC/arXiv syntax. Other sources accept explicit text queries, not a borrowed Boolean grammar. Do not put credentials, headers or URLs-to-fetch in the plan.

discover uses the existing source adapters, pagination and pacing. It saves:
- query-plan.json: normalized question, concepts and actual compiled routes.
- candidates.json: complete deduplicated observed pool, including outside-queue and explicitly filtered records.
- manifest.json: selected queue only, retaining normal v2 PDF semantics.
- research_context: file/hash references and selected candidate IDs.

Every plan-mode candidate has extra.candidate_id, discovery_hits (route/source/one-based rank/source IDs), concept_evidence, queue_eligible and screening. The default screening state is uncertain, not relevant or excluded. Conflicting article identifiers remain separate. Candidate IDs distinguish those conflicts.

Ranking is reciprocal rank fusion, sum of 1/(60+one-based rank), with the best rank per route and candidate-ID tie-break. Citation counts are displayed but not used in this mode. Concept evidence is normalized literal phrase matching in title/abstract, not semantic inference or a hard filter. The original run command retains its established ranking and literal-filter behavior.

--queue-size controls only the selected queue. --article-type is an explicit queue filter and does not remove records from candidates.json. discover does not download by default; --download enables the existing permitted PDF workflow for its selected queue. It exits 2 when a planned route is disabled, partial or failed, while preserving usable results. An ok source can still be capped; inspect exhausted/continuation rather than assuming complete discovery.

Frozen sidecars must stay present and match their hashes. Resume reuses them and never reruns plan routes. Start a new discovery run to change a plan.

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

screen writes a fresh screening.json with unreviewed IDs and a new manifest.json containing only the explicitly included records. This makes candidates outside the initial queue usable for content preparation. The complete original inventory, original manifest and original PDF denominator stay unchanged. No decision causes a download or Zotero write. Uncertain/unreviewed records remain in the parent inventory and ledger.

## Independent content v1

content accepts either a legacy frozen manifest, a discovery queue or an explicitly screened manifest; writes a fresh content-index.json plus per-article folders; and never writes back to that source manifest.

The default record budget is 25, adjustable with --max-records. Beyond-budget records remain pending_budget. --local-only prohibits network use and only parses existing, identity/hash-verified PDFs. Otherwise, records with PMCID first resolve current official PMC Cloud metadata; only declared, identity-matched, eligible JATS objects are fetched. A bare PMCID or a previously saved OA label does not authorize a body request. This phase does not fetch publisher HTML, use BioC, OCR, paid APIs, or a new model. These formats/adapters were research candidates, not implemented capabilities.

If native content is unavailable, a local PDF is copied only after its recorded SHA matches, then identity is rechecked before parsing. Missing checksum, identity uncertainty or changed bytes are failures, not silent acceptance. If a useful local PDF is not present, use the existing explicitly requested PDF acquisition workflow, then pass its new manifest to content. An old prose-only Markdown reader is not promoted to a structured full article.

Article folders retain the original source.xml or source.pdf, document.json and reader.md when extraction succeeds. Failed PDF validation may leave the copied source for inspection; only a successful document is listed as extracted. Native and PDF versions may differ: use the recorded source/version of the actual document, not the old PDF version.

document.json fields include:
- schema_version 1, parser version, source_sha256, article identity and source provenance.
- elements with stable document-local IDs, kind, text, section and XML path/source ID or PDF page/bbox.
- JATS tables: rows/cells, header status, row/column spans, units in original cell text, footnotes and original XML.
- Formulas: source XML with namespaces plus readable text; mathematical interpretation is not verified.
- Figures/media/supplements: captions and declared references, explicitly not acquired/interpreted.
- References and inline xrefs, with unresolved/duplicate source-ID diagnostics.
- coverage and gaps, plus ai_reading_status=not_submitted.

PDF extraction uses the existing PyMuPDF or pypdf backend. It processes at most 500 pages, reports unprocessed/empty/failed pages, and does not claim table/formula/image semantics or perfect reading order. Native extraction also reports unresolved objects and does not claim its supplement inventory is exhaustive. No new parser/model dependency is installed.

content-index metrics separate selected population, processed and extracted records. ai_read_records remains zero: extraction cannot establish reading. PDF counters and local_fulltext from the old fallback are untouched. A pending/unavailable selected record causes exit 2 with a retained partial index; an extracted but structurally partial document still requires its gap report to be read.

## Host-AI reading and receipts v1

reading-pack takes document.json, a question, a fresh directory, a character budget (4,000–200,000; default 24,000), and optional zero-based --start-chunk. It verifies the source artifact hash and splits element text into ordered chunks of at most 4,000 characters. IDs encode element and text offset; page/XML locator stays attached.

The pack is prepared_not_read. reading-pack.md provides the text for the host AI. reading-pack.json supplies exact spans, document/source hashes, omitted prior chunks, remaining later chunks, and next_start_chunk. The budget counts source-text characters, not total prompt tokens. Structural table/formula data remain in document.json/source XML; inspect those objects when the question requires relationships, units or mathematical detail. Flattened text alone is insufficient.

For whole-article text reading, process ordered packs and continue from next_start_chunk until the requested scope is covered. A later pack is not cumulative proof of earlier reading. Keep all receipts and distinguish article-text coverage from unprocessed images, formulas, tables or supplements. For question-focused work the host can navigate the element locations, but must state the narrower scope.

After actually reading the offered text, the host writes:

~~~json
{
  "pack_sha256": "SHA-256 of the exact reading-pack.json",
  "actor": "host-ai",
  "observations": [
    {"chunk_id": "e00001:0", "quote": "Exact text from this chunk", "note": "What this source passage establishes, or leaves uncertain"}
  ]
}
~~~

reading-receipt verifies the pack/document/source hashes and offered chunks against the document, checks unique chunk IDs, and checks that each quote is verbatim in its cited chunk. It writes reported_reading_with_verified_quotes, observed/offered/article chunk counts, unobserved offered IDs and per-receipt article_text_coverage. A receipt can be partial; it does not mutate the prepared pack or automatically aggregate other receipts.

The output explicitly leaves semantic_accuracy_verified, model_context_delivery_independently_verified and whole_article_read_certified false. Quotation integrity is not entailment; one observation does not prove every sentence in that chunk was understood. Do not convert these receipts into an unconditional “AI has fully read and understood the paper” assertion.

All these operations are local orchestration plus permitted retrieval; no hidden LLM, embedding or reranking service is called. Decisions, questions and receipts are project-specific. Cross-project persistent caching and citation-graph expansion are not part of this version.

