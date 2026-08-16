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
    key = f"{collection_name}:{doc_id}".encode()
    digest = hashlib.sha256(key).digest()
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
    Point IDs. If an existing point's ``payload.doc_id`` differs from the
    doc_id being written for the same Point ID, raise
    :class:`PointIdCollisionError`. This catches the astronomically-rare
    SHA-256 truncation collision and any legacy contamination.

    Idempotent re-upsert (same Point ID, same stored doc_id) is allowed:
    Qdrant overwrites the existing point in place. A retrieval failure
    fails closed — the exception propagates and no upsert is performed,
    consistent with the P0-05 fail-closed philosophy: never upsert when we
    cannot verify there is no collision.

    Designed to degrade cleanly under a bare ``MagicMock`` client: a mock
    ``retrieve()`` returns a ``MagicMock`` whose ``list()`` is ``[]`` (no
    existing points), so the check is a no-op and the upsert proceeds —
    existing unit tests that mock the Qdrant client are unaffected.

    Args:
        client: QdrantClient (real or mock).
        collection_name: target collection.
        point_ids: stable Point IDs that will be upserted.
        doc_ids: logical doc_ids being written, aligned with ``point_ids``.
    """
    if not point_ids:
        return
    # Fail closed on retrieval error: propagate rather than upsert blindly.
    existing = client.retrieve(
        collection_name=collection_name,
        ids=list(point_ids),
        with_payload=True,
        with_vectors=False,
    )
    existing_records = list(existing) if existing else []
    stored_doc_by_pid: dict[int, object] = {}
    for rec in existing_records:
        try:
            pid = rec.id
            payload = getattr(rec, "payload", None) or {}
            stored_doc_by_pid[pid] = payload.get("doc_id")
        except Exception:  # noqa: BLE001 - skip unparseable record, do not block
            continue
    for pid, new_doc_id in zip(point_ids, doc_ids, strict=True):
        stored = stored_doc_by_pid.get(pid)
        if stored is not None and stored != new_doc_id:
            raise PointIdCollisionError(
                collection=collection_name,
                point_id=pid,
                existing_doc_id=stored,
                new_doc_id=new_doc_id,
            )
