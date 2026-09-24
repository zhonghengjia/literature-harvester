# Browser handoff

`browser-status` reports the supported mode: **manual handoff**. There is no
verified callable external messaging API for the inspected EasyPubMedicine
extension. This integration cannot start extension downloads, select extension
providers, inspect its current settings, read cookies, automate SSO, or control
Zotero. It never invokes the extension, whose enabled providers may include
disallowed sources. Extension installation or a successful download is not
evidence of permitted access.

`browser-queue --manifest <path>` returns failed/unresolved records with stable
strong identifiers and routes the user can open manually. DOI/PubMed/PMC landing
pages do not establish OA rights. A route labeled `oa_candidate` retains the
manifest's source assertion; the user must verify the original provider before
using the PDF. No URL is opened and no browser setting changes.

`browser-ingest` requires one exact namespaced record ID such as
`doi:10.1234/example`, an explicitly selected local PDF, the original source URL,
and an access-evidence JSON object. The CLI must match the ID against the whole
manifest, including failed target candidates, without duplicate ID ambiguity.
It must save the updated manifest and regenerate reports after **every** result,
including a rejected/manual-review attempt. There is no Downloads-folder scan.

Example access evidence (replace all illustrative statements with observed facts):

```json
{
  "kind": "oa",
  "source_kind": "publisher",
  "evidence_url": "https://publisher.example/article",
  "evidence_note": "The original publisher article page exposes this PDF under its stated license.",
  "confirmed_by": "user",
  "original_provider_confirmed": true,
  "license_or_oa_statement": "CC BY 4.0"
}
```

For user-authorized institutional access, use `kind: institutional`, an actual
publisher or institutional source, and `entitlement_confirmed: true`. OA requires
the provider's license/OA statement. `source_kind` accepts `publisher`,
`institutional_repository`, `institutional_library`, or `official_preprint`.
The evidence is explicitly stored as **user-attested original-provider evidence,
not independently verified**. User confirmation must refer to an observed
publisher/repository/library source, never merely a plugin's download-success
message. Known shadow-library sources are rejected. An unknown or changed host
does not establish permitted access; a bare OA/institutional label, file presence,
plugin statement, or record-level OA flag yields `manual_review`. Do not include
keys, cookies, credentials, or restricted personal information in evidence notes.
Query strings and fragments are removed from persisted URLs.

The library verifies PDF structure and first-page article identity on a temporary
snapshot, then copies accepted bytes under the run's PDF directory without
overwriting existing files. Ambiguous identity requires manual review; a DOI
anywhere in a document does not establish identity. SHA-256 prevents counting the
same PDF twice. The input file remains unchanged. Rejected identity/access attempts
are recorded without attaching the file; a failed retry preserves an already
accepted PDF. Ingestion is capped at 100 MiB. Publication version is left unknown
unless separately established; institutional PDFs are never relabeled OA.

Accepted records can enter the separate Zotero preview workflow. Browser handoff
does not provide permission for Zotero mutation; retain the existing approved
preview and explicit write-confirmation sequence.
