"""Stable Qdrant Point ID mapping (P1-03).

Logical document IDs are caller-supplied strings (e.g. ``"pr_000"``,
``"faq_00001"``). The storage Point ID must be:

- deterministic across processes, restarts and ``PYTHONHASHSEED`` (ID-1)
- a valid positive int63 — a Qdrant uint64 point id (ID-4)
- produced by a single mapping boundary (ID-8)
- collision-resistant, with a refuse-on-conflict policy at the storage
  layer (ID-5)
- collection-namespaced (ID-7)

Python's built-in ``hash()`` for strings is randomized per process via
``PYTHONHASHSEED`` and MUST NOT be used for persistent identity. This module
is the only place that turns a logical document id into a Qdrant Point ID;
business code must call :func:`document_id_to_point_id` and never ``hash()``.

Algorithm
---------
``SHA-256("{collection_name}:{doc_id}")`` -> first 8 bytes, big-endian ->
mask ``& 0x7FFFFFFFFFFFFFFF`` (positive int63).

* SHA-256 is seed-independent (unlike ``hash()``) — verified across
  ``PYTHONHASHSEED`` values 0/1/random.
* 63-bit truncation keeps the existing positive-int convention and stays
  within Qdrant's uint64 range; the birthday collision bound is < 1e-8 for
  corpora up to ~1e6 documents.
* ``collection_name`` is part of the hash input, so the same logical doc_id
  in two collections yields two different Point IDs (defense-in-depth;
  Qdrant collections are themselves isolated namespaces).

Migration
---------
Legacy points created by the old ``hash(id_) & 0x7FFFFFFFFFFFFFFF`` path are
process-seed-dependent and therefore cannot be recomputed. Use
:mod:`rag.point_id_migration` (``discover_legacy_points``) to detect them and
:mod:`scripts.migrate_point_ids` (``--dry-run`` by default) to plan a rebuild.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qdrant_client import QdrantClient

__all__ = [
    "POINT_ID_MASK",
    "PointIdCollisionError",
    "assert_no_point_id_collision",
    "document_id_to_point_id",
]

# Positive int63 — the existing convention. Stays within Qdrant's uint64
# point-id range and avoids Python signed-int edge cases.
POINT_ID_MASK = 0x7FFFFFFFFFFFFFFF


def _encode_mapping_key(collection_name: str, doc_id: object) -> bytes:
    """Length-prefixed encoding of (collection, doc_id) for the hash.

    A naive ``f"{coll}:{doc}"`` is ambiguous: ``("a:b","c")`` and
    ``("a","b:c")`` hash to the same byte string. Prefixing each field with
    its length makes the encoding unambiguous without restricting the input
    domain, so two distinct (collection, doc_id) pairs never collide via the
    delimiter (ID-7 boundary hardening).
    """
    coll_b = str(collection_name).encode()
    doc_b = str(doc_id).encode()
    return b"%d:%s|%d:%s" % (len(coll_b), coll_b, len(doc_b), doc_b)


def document_id_to_point_id(collection_name: str, doc_id: object) -> int:
    """Map a logical document id to a stable Qdrant Point ID.

    Deterministic across processes, restarts and ``PYTHONHASHSEED`` — the
    inverse of the old ``hash(id_) & 0x7FFFFFFFFFFFFFFF``: same output type
    and mask, but seed-independent.

    Args:
        collection_name: Qdrant collection (participates in the namespace so
            the same doc_id in two collections maps to two Point IDs).
        doc_id: logical document id (caller-supplied string; ``uuid.UUID``
            and other hashable values are coerced via ``str()``).

    Returns:
        int in ``[0, 0x7FFFFFFFFFFFFFFF]`` — a valid Qdrant uint64 point id.

    Raises:
        ValueError: if either input is empty/None.
    """
    if not collection_name:
        raise ValueError("collection_name must be a non-empty string")
    if not doc_id:
        raise ValueError("doc_id must be a non-empty value")
    digest = hashlib.sha256(_encode_mapping_key(collection_name, doc_id)).digest()
    return int.from_bytes(digest[:8], "big") & POINT_ID_MASK


class PointIdCollisionError(Exception):
    """Raised when a stable Point ID already stores a *different* logical
    doc_id — i.e. a SHA-256 truncation collision or legacy contamination.

    The system refuses to silently overwrite the other document (ID-5).
    """

    def __init__(
        self,
        *,
        collection: str,
        point_id: int,
        existing_doc_id: object,
        new_doc_id: object,
    ):
        self.collection = collection
        self.point_id = point_id
        self.existing_doc_id = existing_doc_id
        self.new_doc_id = new_doc_id
        super().__init__(
            f"Point ID collision in collection {collection!r}: point_id={point_id} "
            f"already stores doc_id={existing_doc_id!r}; refusing to overwrite "
            f"with doc_id={new_doc_id!r}"
        )


def assert_no_point_id_collision(
    client: QdrantClient,
    collection_name: str,
    point_ids: list[int],
    doc_ids: list[object],
) -> None:
    """ID-5 (storage layer): refuse to silently overwrite a different document.

    Before upserting, retrieve any existing points at the computed stable
    Point IDs. Fail closed whenever a target Point ID is **occupied** but the
    write cannot be proven safe:

    * the stored ``payload.doc_id`` differs from the doc_id being written
      (a genuine collision / legacy contamination), OR
    * the point exists but has no readable ``doc_id`` (owner unverifiable —
      an orphaned legacy point from a different schema). BF-P1-03-01: this
      is treated as occupied, NOT as "free to overwrite".

    Only two states allow the upsert to proceed: the Point ID is absent
    (truly empty), or it is present with the *same* ``doc_id`` (idempotent
    re-upsert — Qdrant overwrites in place).

    A retrieval failure fails closed — the exception propagates and no
    upsert is performed, consistent with the P0-05 fail-closed philosophy:
    never upsert when we cannot verify there is no collision.

    Also checks the batch itself (BF-P1-03 intra-batch): two distinct
    doc_ids in this call mapping to the same Point ID are rejected rather
    than sent to Qdrant as a last-wins clobber.

    Designed to degrade cleanly under a bare ``MagicMock`` client: a mock
    ``retrieve()`` returns a ``MagicMock`` whose ``list()`` is ``[]`` (no
    existing points), so the storage check is a no-op and the upsert
    proceeds — existing unit tests that mock the Qdrant client are
    unaffected.

    Args:
        client: QdrantClient (real or mock).
        collection_name: target collection.
        point_ids: stable Point IDs that will be upserted.
        doc_ids: logical doc_ids being written, aligned with ``point_ids``.
    """
    if not point_ids:
        return

    # Intra-batch collision: two distinct doc_ids mapping to one Point ID.
    # (A same-doc_id appearing twice at one Point ID is an idempotent
    # re-upsert and is allowed.)
    seen_pairs: dict[int, object] = {}
    for pid, new_doc_id in zip(point_ids, doc_ids, strict=True):
        prior = seen_pairs.get(pid)
        if prior is None:
            seen_pairs[pid] = new_doc_id
        elif prior != new_doc_id:
            raise PointIdCollisionError(
                collection=collection_name,
                point_id=pid,
                existing_doc_id=prior,
                new_doc_id=new_doc_id,
            )

    # Fail closed on retrieval error: propagate rather than upsert blindly.
    existing = client.retrieve(
        collection_name=collection_name,
        ids=list(point_ids),
        with_payload=True,
        with_vectors=False,
    )
    existing_records = list(existing) if existing else []
    # Map each occupied Point ID to its stored doc_id, or _UNVERIFIABLE when
    # the point exists but its owner cannot be read. Absent ids are simply
    # not in the map — that is the only state we treat as "free to write".
    stored_doc_by_pid: dict[int, object] = {}
    for rec in existing_records:
        try:
            pid = rec.id
            payload = getattr(rec, "payload", None)
            if not isinstance(payload, dict):
                stored_doc_by_pid[pid] = _UNVERIFIABLE
            else:
                doc_id = payload.get("doc_id")
                stored_doc_by_pid[pid] = _UNVERIFIABLE if doc_id is None else doc_id
        except Exception:  # noqa: BLE001 - unreadable record -> occupied, fail closed
            stored_doc_by_pid[pid] = _UNVERIFIABLE
            continue
    for pid, new_doc_id in zip(point_ids, doc_ids, strict=True):
        stored = stored_doc_by_pid.get(pid)
        if stored is None:
            continue  # absent — free to write
        if stored is _UNVERIFIABLE or stored != new_doc_id:
            raise PointIdCollisionError(
                collection=collection_name,
                point_id=pid,
                existing_doc_id=stored,
                new_doc_id=new_doc_id,
            )


# Sentinel marking an occupied Point ID whose owner cannot be verified
# (missing/unreadable payload.doc_id). Distinct from None, which means the
# Point ID was absent from the retrieve response (free to write).
_UNVERIFIABLE = object()
