# Manifest schema

`manifest.json` is the resumable source of truth for a run.

## Top-level fields

- `schema_version`: currently `1`.
- `run_id`, `query`, `created_at`, `updated_at`.
- `settings`: non-secret settings used for the run, including optional target PDF count and required topic terms.
- `source_status`: success/failure/count information for each source.
- `records`: normalized paper objects.
- `download_attempts`: failed candidates tried while satisfying `--target-pdfs`; successful target records remain in `records`.

## Record identity

Records may contain `doi`, `pmid`, `pmcid`, `arxiv_id`, `openalex_id`, and `semantic_scholar_id`. Deduplication uses DOI first, then domain identifiers, then a normalized title. A fuzzy title match is never sufficient for automatic Zotero mutation.

## PDF candidates

Each candidate contains `url`, `source`, `license`, and `version`. URLs are ordered by the source policy and deduplicated before download.

## Download statuses

- `pending`
- `downloaded`
- `already_downloaded`
- `no_oa_version`
- `rate_limited`
- `anti_bot`
- `dead_link`
- `not_pdf`
- `too_large`
- `license_unclear`
- `manual_review`
- `download_failed`

Downloaded records also store `local_pdf`, `sha256`, `download_source`, and `download_url`.

## Zotero fields

`zotero` is added or updated during planning/import. It may contain `action`, `match_key`, `match_reason`, `item_key`, `attachment_key`, and `status`. Expected actions are `create`, `skip_exact`, `manual_review`, and `skip_no_metadata`.

Secrets such as API keys and the Unpaywall contact email must never be written to the manifest.
