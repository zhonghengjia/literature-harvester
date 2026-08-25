# Source and access policy

Read this reference before live discovery or download work.

## Allowed automated sources

- Crossref for DOI and bibliographic metadata. A `link` field alone is not proof of open access.
- PubMed E-utilities for biomedical discovery and identifiers. Use batch requests and respect NCBI request-rate guidance.
- Europe PMC and the PMC Open Access subset for biomedical metadata and licensed full text.
- OpenAlex for discovery and OA locations. Normal use should provide a free API key.
- Semantic Scholar for discovery and `openAccessPdf` locations. Anonymous access may be throttled.
- arXiv and supported preprint repositories for their official public PDFs.
- Unpaywall for DOI-level OA resolution; it requires a contact email.
- Publisher or institutional-repository files when the source explicitly exposes a legal OA copy.

CORE and DOAJ may be added as metadata or OA-location adapters, but they are not required for the MVP.

## Disallowed sources and behavior

- Shadow libraries, leaked credentials, session theft, CAPTCHA bypass, automated circumvention of access controls, or redistribution of acquired PDFs.
- Guessing publisher PDF URLs and treating an HTTP 200 response as legal access.
- Bulk scraping PMC article pages. Use supported APIs or the current PMC OA cloud data routes and respect the article license.
- Using PMC's retired FTP/OA Web Service paths after the August 2026 migration. Resolve only the current `pmc-oa-opendata` Cloud version, require active-OA and license metadata, and accept only the declared PDF object in the official bucket.
- Automated browser SSO unless the user explicitly enables an institutional-access mode and confirms they are entitled to each source.

## Candidate ordering

Prefer candidates in this order when they are legal and verifiable:

1. PMC/Europe PMC OA copy.
2. arXiv, bioRxiv, medRxiv, or another official preprint.
3. OpenAlex OA location.
4. Unpaywall OA location.
5. Semantic Scholar `openAccessPdf`.
6. Verified institutional repository or publisher OA file.

Deduplicate URLs and retain provenance, license, and version type. If no legal PDF is found, return an actionable route: DOI landing page, PubMed/Europe PMC, institutional library, interlibrary loan, repository search, or an author-request template.

## Network behavior

- Identify the client with a stable user agent and contact email where an API requests one.
- Respect `Retry-After`; retry only transient network errors, HTTP 429, and HTTP 5xx.
- Do not retry access denials, CAPTCHA pages, invalid PDFs, or license uncertainty in a tight loop.
- Record per-source failures in the run summary.
