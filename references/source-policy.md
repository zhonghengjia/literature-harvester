# Source and access policy

Read before live discovery or download work. Free official/OA routes are the default; this policy does not authorize automated institutional sessions.

## Permitted routes

- PubMed E-utilities, Crossref, Europe PMC, OpenAlex, Semantic Scholar and arXiv for bibliographic discovery. PubMed and Europe PMC are preferred for biomedical questions. Respect provider page bounds, pacing, partial-result errors and retry deadlines.
- OpenAlex OA locations (all declared qualifying locations), Unpaywall DOI locations, Semantic Scholar openAccessPdf and CORE repository records for legal free-copy discovery. CORE requires its configured API key in this adapter. Crossref links alone are not proof of OA.
- Exact-title preprint matches require compatible author/year evidence and explicit version-relation provenance. A different preprint DOI never replaces the journal DOI. bioRxiv/medRxiv versions resolve through official metadata; a challenge page is a manual fallback, not a bypass target.
- Eligible publisher/repository landing pages can be read for every declared citation_pdf_url, eprints.document_url or PDF alternate link. Do not guess publisher paths.
- PubMed/Europe PMC/OpenAlex PMCID records register an official PMC Cloud resolver. The resolver examines bounded versions and requires matching identity, active status, license, declared object and official bucket path. An active OA version can yield its declared PDF/JATS. A non-OA author manuscript with explicit TDM eligibility is text-only: retrieve its declared JATS, not an arbitrary PDF/media object.
- Official Europe PMC JATS may be used only when article OA eligibility is established. A bare PMCID alone is not that eligibility. The generated prose reader is not a lossless PDF substitute.

Use the current pmc-oa-opendata Cloud metadata contract, not retired FTP/OA Web Service routes or bulk HTML scraping. PMC version number is deposit-processing order, not proof of the published version. Preserve per-version warnings rather than hiding alternate-version failures.

## Ordering and fallback

PDF acquisition order is centralized in downloader.sorted_candidates: direct PDFs/resolvers before landing resolution, and the legacy JATS prose fallback after PDF attempts. The independent content operation instead rechecks current official PMC eligibility and tries declared JATS before an identity/hash-verified local PDF, even when that PDF previously succeeded. It does not mutate PDF statuses or counters. Source priority favors biomedical OA repositories, then official preprints, OpenAlex, Unpaywall, CORE and other verified locations. Keep URL, license, version and resolver evidence.

A failed direct candidate does not suppress later enrichment. Query the enabled free-copy adapters for that failed record and try newly declared alternatives. Unknown/incorrect article identity requires review; it does not count as successful retrieval. A missing route is not proof of a paywall. Provide original DOI/record pages, institutional library or interlibrary-loan options, repository searches and an author-request template.

## Budget and deferred adapters

OpenAlex metadata content availability and the article license are separate facts. Its hosted content endpoint is metered; a local file cap or advertised daily allowance cannot guarantee that prepaid credit will remain untouched. The automatic content route is disabled by default and remains explicitly blocked without a verified server-side free-only spending guard. No content download or credential-bearing candidate is issued.

OpenAIRE and DOAJ remain disabled by default. New regional repositories, paid TDM, bulk institutional SSO and citation snowball expansion are deferred pending an explicit scoped decision and fixed-pool evidence.

## Network and browser boundaries

Identify the client with a stable user agent and contact address where required. Retry only bounded transient/idempotent operations. Preserve full Retry-After deadlines for the affected host, including across records and resumed runs; do not shorten them. Another independently eligible host may still be attempted; see manifest-schema.md for legacy unattributed deadlines. arXiv requests are serialized/paced within one process, including response reads; other processes/tools must coordinate usage.

Never use shadow libraries, leaked credentials, session theft, CAPTCHA bypass, access-control circumvention or redistribution. Do not guess paths or treat HTTP 200/PDF syntax as permission or article identity.

An explicit user-downloaded file may enter the manual browser handoff only with original-provider evidence and, for institutional access, confirmed entitlement. The handoff does not invoke EasyPubMedicine, read cookies, alter extension settings or attest that a plugin used a permitted source. See browser-handoff.md. Do not expose Zotero beyond loopback; library mutation requires its separately reviewed plan and explicit confirmation.
