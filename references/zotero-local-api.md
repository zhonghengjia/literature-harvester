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
4. Persistent keys are cached per server ID in the user's local application-data directory, never in the skill or run folder. On Windows the cache value is protected with the current user's DPAPI credentials. A `401` invalidates the cached key and requires a new authorization.

## Safe import sequence

1. Fetch non-attachment library items and collections.
2. Plan exact DOI and exact normalized-title matches. Hold fuzzy title matches for manual review.
3. Present counts and per-record actions. Do not write during planning.
4. After explicit approval, create missing collection only when `--create-collection` was supplied.
5. Fetch a Zotero item template, populate supported fields, and create one parent item.
6. If a verified local PDF exists, create an `imported_file` child attachment and use the official local three-phase upload:
   - request upload authorization with MD5, filename, byte size, and millisecond mtime;
   - upload the bytes to the returned localhost upload URL;
   - register the upload key.
7. Read back the parent and attachment. Record partial failures without deleting the parent.

## Attachment choice

The MVP uses `imported_file`: Zotero receives its own managed copy, while the downloaded source file remains in the literature run folder. Linked-file mode can be added later for users who deliberately manage a shared attachment directory.
