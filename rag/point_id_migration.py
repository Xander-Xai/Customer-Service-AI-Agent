"""Legacy Qdrant Point ID discovery & rebuild (P1-03, ID-6).

Legacy points were written by the old ``hash(id_) & 0x7FFFFFFFFFFFFFFF`` path,
which is process-seed-dependent: their stored Point IDs cannot be recomputed
and are effectively random per-process. This module provides a **read-only**
discovery (dry run) and a safe **in-place rebuild** strategy. It is the only
place outside :mod:`rag.point_id` that reasons about Point ID equality, and
it never calls ``hash()``.

Rebuild strategy (in-place, same collection, same int ID type)
-------------------------------------------------------------
The stable id and the legacy id share the same ``int`` type and 63-bit mask,
so legacy and new ids live in one ID space — enabling detection+cleanup in
place rather than a full collection cutover.

1. PRECHECK  — :func:`discover_legacy_points` classifies every point as
   stable / legacy / unmappable and reports duplicate logical doc_ids and
   potential stable-ID conflicts. **Read-only; modifies nothing.**
2. BACKUP   — the operator snapshots the collection's payloads (and vectors)
   to a local file before any destructive step. The CLI refuses
   ``--execute`` without ``--i-understand-this-is-destructive``.
3. DRY RUN  — ``--dry-run`` (default) prints the discovery report; no writes.
4. REBUILD  — ``--execute``: scroll all points (with vectors); re-upsert each
   at its stable Point ID (reusing the stored vector — **no re-embedding**);
   delete the orphaned legacy points.
5. VALIDATE — re-run :func:`discover_legacy_points`; expect
   ``legacy_points == 0`` and ``stable_points == total_points``.
6. ROLLBACK — keep the pre-rebuild backup; if validation fails, re-ingest
   from the backup payloads (or from source seed data) into a fresh
   collection. The rebuild never deletes a whole collection — it deletes
   individual orphaned legacy points only after the stable points are in
   place, so a partial failure leaves both stable and legacy points
   present (no data loss; re-running rebuild is idempotent).

Destructive migration is **never** the default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rag.point_id import document_id_to_point_id

if TYPE_CHECKING:
    from qdrant_client import QdrantClient

__all__ = [
    "LegacyReport",
    "RebuildReport",
    "discover_legacy_points",
    "rebuild_collection_point_ids",
]

_SCROLL_LIMIT = 500


def _scroll_all(client: QdrantClient, collection_name: str, *, with_vectors: bool):
    """Yield every Record in the collection, paginating via scroll.

    qdrant-client ``scroll`` returns ``(list[Record], next_offset)``; when
    ``next_offset`` is falsy the scan is complete. A defensive ``.points``
    fallback covers wrapper objects.
    """
    offset = None
    while True:
        result = client.scroll(
            collection_name=collection_name,
            limit=_SCROLL_LIMIT,
            offset=offset,
            with_payload=True,
            with_vectors=with_vectors,
        )
        if isinstance(result, tuple):
            points, next_offset = result[0], result[1]
        else:
            points = getattr(result, "points", None) or []
            next_offset = getattr(result, "next_page_offset", None)
        yield from points or []
        if not next_offset:
            break
        offset = next_offset


@dataclass
class LegacyReport:
    """Read-only classification of a collection's points vs the stable map."""

    collection: str
    total_points: int = 0
    stable_points: int = 0
    legacy_points: int = 0  # has doc_id, stored id != stable id
    unmappable_points: int = 0  # no doc_id payload -> cannot compute stable id
    duplicate_logical_ids: list[str] = field(default_factory=list)
    potential_conflicts: list[tuple[int, list[str]]] = field(default_factory=list)
    legacy_point_ids: list[int] = field(default_factory=list)


def discover_legacy_points(client: QdrantClient, collection_name: str) -> LegacyReport:
    """Read-only dry run: classify points as stable / legacy / unmappable.

    A point is:

    * **stable**     — ``point.id == document_id_to_point_id(coll, payload.doc_id)``
    * **legacy**      — carries a ``doc_id`` but its stored id != stable id
    * **unmappable**  — no ``doc_id`` payload (cannot compute a stable id)

    Also reports:

    * ``duplicate_logical_ids`` — doc_ids carried by more than one point
      (ghost duplicates from the old hash instability)
    * ``potential_conflicts`` — stable Point IDs claimed by 2+ distinct
      doc_ids (a SHA-256 truncation collision — astronomically rare)

    Performs no writes.
    """
    report = LegacyReport(collection=collection_name)
    doc_id_to_pids: dict[Any, list[int]] = {}
    stable_id_to_doc_ids: dict[int, list[Any]] = {}
    for rec in _scroll_all(client, collection_name, with_vectors=False):
        report.total_points += 1
        payload = getattr(rec, "payload", None) or {}
        doc_id = payload.get("doc_id")
        stored_id = rec.id
        if doc_id is None:
            report.unmappable_points += 1
            report.legacy_point_ids.append(stored_id)
            continue
        stable_id = document_id_to_point_id(collection_name, doc_id)
        if stored_id == stable_id:
            report.stable_points += 1
        else:
            report.legacy_points += 1
            report.legacy_point_ids.append(stored_id)
        doc_id_to_pids.setdefault(doc_id, []).append(stored_id)
        stable_id_to_doc_ids.setdefault(stable_id, []).append(doc_id)
    report.duplicate_logical_ids = sorted(
        str(d) for d, pids in doc_id_to_pids.items() if len(pids) > 1
    )
    report.potential_conflicts = [
        (sid, sorted(str(x) for x in dids))
        for sid, dids in stable_id_to_doc_ids.items()
        if len(set(dids)) > 1
    ]
    return report


@dataclass
class RebuildReport:
    """Result of a (dry-run or executed) rebuild."""

    collection: str
    dry_run: bool
    scrolled: int = 0
    upserted: int = 0
    deleted_legacy: int = 0
    skipped_unmappable: int = 0
    legacy_point_ids: list[int] = field(default_factory=list)


def rebuild_collection_point_ids(
    client: QdrantClient,
    collection_name: str,
    *,
    dry_run: bool = True,
) -> RebuildReport:
    """Rebuild Point IDs in place.

    Scrolls all points (with vectors), re-upserts each at its stable Point ID
    — **reusing the stored vector, no re-embedding** — then deletes the
    orphaned legacy points.

    * ``dry_run=True`` (default): read-only; reports what *would* happen.
    * ``dry_run=False``: performs the rebuild.

    Safety: a legacy point's old id is only deleted when it is NOT also a
    stable id for some other document in the same batch — so a rare
    truncation collision (legacy id == another doc's stable id) is never
    silently turned into data loss. Collisions are surfaced by
    :func:`discover_legacy_points` for manual resolution.

    The rebuild is idempotent: re-running it after a partial failure leaves
    the collection with both stable and legacy points present (no loss);
    a subsequent run completes the cleanup.
    """
    from qdrant_client.http import models

    report = RebuildReport(collection=collection_name, dry_run=dry_run)
    records = list(_scroll_all(client, collection_name, with_vectors=True))
    report.scrolled = len(records)

    to_upsert: list[models.PointStruct] = []
    stable_ids_set: set[int] = set()
    legacy_to_delete: list[int] = []
    for rec in records:
        payload = getattr(rec, "payload", None) or {}
        doc_id = payload.get("doc_id")
        if not doc_id:
            report.skipped_unmappable += 1
            continue
        stable_id = document_id_to_point_id(collection_name, doc_id)
        stable_ids_set.add(stable_id)
        vector = getattr(rec, "vector", None)
        to_upsert.append(models.PointStruct(id=stable_id, vector=vector, payload=payload))
        if rec.id != stable_id:
            legacy_to_delete.append(rec.id)

    if dry_run:
        report.upserted = len(to_upsert)
        report.deleted_legacy = len(legacy_to_delete)
        report.legacy_point_ids = legacy_to_delete
        return report

    if to_upsert:
        client.upsert(collection_name=collection_name, points=to_upsert)
        report.upserted = len(to_upsert)
    # Never delete an id that is also a stable id for another document.
    safe_to_delete = [pid for pid in legacy_to_delete if pid not in stable_ids_set]
    if safe_to_delete:
        client.delete(
            collection_name=collection_name,
            points_selector=models.PointIdsList(points=safe_to_delete),
        )
        report.deleted_legacy = len(safe_to_delete)
    report.legacy_point_ids = safe_to_delete
    return report
