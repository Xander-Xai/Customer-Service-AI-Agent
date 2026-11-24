# Session record format inventory (Issue #25)

Run only on a privately held, explicitly authorized offline snapshot. Export
each persisted session value (including metadata values) as a JSON-encoded
string on one line, without keys, user IDs or filenames. Do not commit the
snapshot, keys, raw content or per-record hashes. A complete export and its
scope must be separately attested by the store owner.

```bash
python -m scripts.inventory_session_records --input /private/session-values.jsonl
```

This read-only command reuses the runtime format prefix and legacy token
heuristic. It emits aggregate counts only. An envelope may be corrupt, a
token candidate is not authenticated, and valid JSON cannot establish
historical provenance. Invalid or oversized export records fail without
printing content. A successful count (even zero plaintext candidates) is
NOT a cutover approval. No real data was examined by the fixture tests.

Before switching to reject mode, establish snapshot coverage, independently
verify all ciphertext with the authorized key set, resolve unverifiable
plaintext with the data owner, verify backup/ACL/volume controls and test
readability and rollback in staging. Do not automatically re-encrypt unknown
plaintext or delete the original. The actual production evidence and
approved migration remain tracked in #25.
