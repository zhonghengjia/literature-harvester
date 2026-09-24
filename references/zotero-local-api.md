# Zotero local API workflow

Read this reference before interacting with Zotero.

## Preconditions

- Zotero 10 or later is running.
- Settings > Advanced > **Allow other applications on this computer to communicate with Zotero** is enabled.
- The API is reachable only at `http://127.0.0.1:23119/api/`. Never forward or expose this port.

## Authorization

Read requests need no key. Writes require a runtime local key:

1. `GET /api/` and cache `Zotero-Server-ID`.
2. `POST /api/local/authorize` with the server ID and an application name.
3. The user should choose **Always Allow** for multi-item imports. A one-time key is consumed by the first successful write and is unsuitable for a multi-step attachment upload.
4. Persistent keys are cached per server ID in the user's local application-data directory, never in the skill or run folder. On Windows the cache value is protected with the current user's DPAPI credentials. A `401` invalidates the cached key and requires a new authorization. Reject a response that supplies a different server ID during an operation. A pending import bound to another server cannot reuse its saved item keys or create a new collection.

## Safe import sequence

1. Fetch non-attachment library items and collections.
2. Plan exact DOI and exact normalized-title matches. Hold fuzzy title matches and explicit DOI conflicts for manual review. A fresh plan replaces stale attachment state; a partial import retains its recorded keys and resumes the parent or attachment. Planning only inventories metadata; an existing attachment entry is not newly verified file content.
3. Present counts and per-record actions. Do not write during planning.
4. After explicit approval, create a missing collection only when this command supplies `--create-collection`. A legacy global configuration field cannot authorize collection creation.
5. For new items, fetch a Zotero item template, populate supported fields, and create one parent item. Preserve preprint item type; a template request failure is reported without silently substituting a journal article. Checkpoint the parent key and server ID. For every new, matched or resumed parent, read back that exact key, bibliographic item type and matching DOI (or exact normalized title when a DOI is missing) before an upload or metadata-only success. Hold a failed readback as `parent_readback_failed` and retain the key.
6. Before every attachment upload, recheck PDF article identity and its recorded SHA-256, even when `--pdf-only` was not used. An uncertain or changed file is held as `parent_created_attachment_unverified`; retain the metadata parent and report the partial outcome. For an accepted local PDF, create an `imported_file` child only if no saved child key exists, and checkpoint its key before file transfer. Read the exact child and verify its parent relationship and link mode. If the actual stored-file MD5 already matches the local bytes, no new upload is needed. A different nonempty MD5 is a conflict, not authorization to overwrite. Otherwise use the official local three-phase upload:
   - request upload authorization with MD5, filename, byte size, and millisecond mtime;
   - upload the bytes to the returned localhost upload URL;
   - register the upload key.
7. After either an `exists` reply or upload registration, read back the exact child, parent relationship, link mode and actual stored-file MD5. Only a complete MD5 match permits PDF import success. If registration completed but its response was lost, the next explicitly confirmed import reads the saved child first and can finish without another POST. Failed or missing readback remains partial; never delete the parent/child or blindly replace a stored file to retry.

## Attachment choice

The implemented mode is `imported_file`: Zotero receives its own managed copy, while the downloaded source file remains in the literature run folder. Linked-file mode is not implemented.
