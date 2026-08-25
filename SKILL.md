---
name: literature-harvester
description: Search scholarly literature by topic, title, DOI, PMID, PMCID, or arXiv ID; create reproducible literature manifests; download only legal open-access PDFs; and prepare or execute deduplicated imports into a local Zotero 10+ library. Use for personal literature discovery, OA retrieval, literature inventories, Zotero import previews, and attaching downloaded PDFs. Do not use it to bypass paywalls or obtain shadow-library copies.
---

# Literature Harvester

Build a source-grounded, resumable literature run rather than returning an untracked list of links.

## Workflow

1. Determine whether the input is a topic, exact title, or identifier. Preserve explicit filters such as date range, study type, language, result count, output folder, and Zotero collection.
2. Before a live search or download, read [references/source-policy.md](references/source-policy.md). Use only official scholarly APIs, repositories, publisher-hosted OA files, or copies the user is authorized to access.
3. Run `scripts/literature_harvester.py run`. Prefer a supplied config; otherwise use the safe defaults in `config.example.toml`. Keep contact information and API keys in environment variables, never in the skill or run artifacts. `LITERATURE_HARVESTER_CONTACT_EMAIL` supplies the Unpaywall/Crossref/OpenAlex/PubMed contact; `UNPAYWALL_EMAIL` may override it for Unpaywall. Optional keys are `OPENALEX_API_KEY`, `S2_API_KEY`, and `NCBI_API_KEY`.
4. Inspect `manifest.json`, `literature.md`, and `failed_downloads.md`. State source failures and rate limits; do not silently treat an unavailable source as an empty result.
5. For Zotero work, read [references/zotero-local-api.md](references/zotero-local-api.md). Run `zotero-status`, then `zotero-plan`. Show the plan and obtain explicit approval immediately before `zotero-import --confirm-write`.
6. After a write, report created items, duplicate skips, manual-review matches, attachments, and partial failures. Preserve the manifest so the run can resume.

## Commands

Use the Python interpreter available in the workspace:

```powershell
python scripts/literature_harvester.py run --query "sepsis-associated encephalopathy" --target-pdfs 10 --config config.toml
python scripts/literature_harvester.py zotero-status
python scripts/literature_harvester.py zotero-plan --manifest "<run-folder>/manifest.json" --collection "Sepsis" --pdf-only
python scripts/literature_harvester.py zotero-import --manifest "<run-folder>/manifest.json" --collection "Sepsis" --pdf-only --create-collection --confirm-write
```

`run` downloads legal OA PDFs by default. When the user asks for a number of actual PDF files, use `--target-pdfs N`; this expands the candidate pool and stops after N verified downloads rather than confusing N search records with N files. For a precise medical entity that could drift to adjacent concepts, add one or more `--required-term` values and inspect the final titles; repeated terms are OR synonyms. Use `--no-download` only when the user asks for metadata-only work. Use `--output-dir` to override the configured library root.

For Zotero, use `--pdf-only` when the user wants the successfully downloaded library rather than every search candidate. It revalidates each local PDF and excludes records without a usable local file from both the preview and the confirmed import. Omit it only when the user explicitly wants metadata-only or failed-candidate records in Zotero.

PubMed and Europe PMC are the primary biomedical discovery sources. Semantic Scholar is supplementary because anonymous requests share a rate limit; retain its error in `source_status` instead of treating HTTP 429 as zero results. For `10.1101/*` records, resolve bioRxiv/medRxiv versions through their public metadata API and treat a Cloudflare rejection as an actionable manual fallback, not something to bypass. Resolve PMC PDFs through the current `pmc-oa-opendata` Cloud layout, verify the per-version metadata says the article is active OA and has a license, then download the declared PDF object.

## Non-negotiable boundaries

- Never use Sci-Hub, LibGen, credential sharing, CAPTCHA bypasses, or paywall circumvention.
- Do not treat a Crossref full-text link as OA unless another source or an explicit license verifies access.
- Do not expose Zotero's localhost port beyond the local machine or print/store API keys in run artifacts.
- Zotero mutation requires an explicit preview followed by `--confirm-write`. The import command is idempotent by DOI and normalized title; fuzzy matches are held for review instead of silently skipped or duplicated.
- A failed attachment upload must not trigger automatic deletion of the parent Zotero item. Record the partial result for safe recovery.

For the run schema and status vocabulary, read [references/manifest-schema.md](references/manifest-schema.md) only when interpreting, extending, or repairing a manifest.
