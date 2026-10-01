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
# Bounded upsert/delete request size for the rebuild (never collection-sized).
_BATCH_SIZE = 128


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
    # BF-P1-03-02: blocking conflicts surfaced by the execute preflight.
    # Unresolved duplicate logical doc_ids and forced stable-ID collisions
    # must be resolved before any upsert/delete; execute aborts when these
    # are non-empty. Populated in dry_run too, so an operator can see the
    # blockers before committing to --execute.
    blocking_duplicates: list[str] = field(default_factory=list)
    blocking_conflicts: list[tuple[int, list[str]]] = field(default_factory=list)
    # BF-P1-03-01 (migration): legacy points that occupy a stable ID but
    # cannot be mapped (no doc_id) and therefore cannot be safely rebuilt
    # in place. Execute aborts when these collide with a new stable id.
    blocking_unmappable_occupied: list[int] = field(default_factory=list)
    aborted: bool = False


def rebuild_collection_point_ids(
    client: QdrantClient,
    collection_name: str,
    *,
    dry_run: bool = True,
    batch_size: int = _BATCH_SIZE,
) -> RebuildReport:
    """Rebuild Point IDs in place.

    Scrolls all points (with vectors), re-upserts each at its stable Point ID
    — **reusing the stored vector, no re-embedding** — then deletes the
    orphaned legacy points.

    * ``dry_run=True`` (default): read-only; reports what *would* happen,
      including any blocking conflicts that would abort an execute.
    * ``dry_run=False``: performs the rebuild, **unless** the preflight finds
      blocking conflicts — in which case it aborts before any write and sets
      ``report.aborted = True``.

    Preflight (BF-P1-03-02): before any upsert, execute detects and refuses:

    * **blocking_duplicates** — two legacy points with the SAME logical
      doc_id (they collapse to one stable id; a naive last-wins upsert would
      silently drop one document's vector). Must be resolved manually
      (deterministic winner selection is a future enhancement).
    * **blocking_conflicts** — two DISTINCT doc_ids mapping to the same
      stable ID (a SHA-256 truncation collision). Must be resolved manually.
    * **blocking_unmappable_occupied** (BF-P1-03-01) — a legacy point with
      no ``doc_id`` already sitting at a stable ID we want to write. Its
      owner cannot be verified, so overwriting it is refused.

    Safety: a legacy point's old id is only deleted when it is NOT also a
    stable id for some other document in the same batch — so a rare
    truncation collision (legacy id == another doc's stable id) is never
    silently turned into data loss.

    The rebuild is idempotent: re-running it after a partial failure leaves
    the collection with both stable and legacy points present (no loss);
    a subsequent run completes the cleanup.
    """
    from qdrant_client.http import models

    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    report = RebuildReport(collection=collection_name, dry_run=dry_run)

    # ---- Pass 1 (compact, no vectors): classify + preflight ----
    # Only compact state is retained (ids, counts, doc_id sets) so a large
    # collection never materializes its full vector payload in process memory.
    stable_ids_set: set[int] = set()
    legacy_to_delete: list[int] = []
    doc_id_counts: dict[Any, int] = {}
    stable_id_doc_ids: dict[int, list[Any]] = {}
    unmappable_occupied: set[int] = set()
    mappable_count = 0

    for rec in _scroll_all(client, collection_name, with_vectors=False):
        report.scrolled += 1
        payload = getattr(rec, "payload", None) or {}
        doc_id = payload.get("doc_id") if isinstance(payload, dict) else None
        if not doc_id:
            report.skipped_unmappable += 1
            # An unmappable point may sit at a stable id we want to write
            # for a *different* document. Record its stored id so the
            # preflight can refuse to overwrite it.
            unmappable_occupied.add(rec.id)
            continue
        stable_id = document_id_to_point_id(collection_name, doc_id)
        stable_ids_set.add(stable_id)
        mappable_count += 1
        if rec.id != stable_id:
            legacy_to_delete.append(rec.id)
        doc_id_counts[doc_id] = doc_id_counts.get(doc_id, 0) + 1
        stable_id_doc_ids.setdefault(stable_id, []).append(doc_id)

    report.blocking_duplicates = sorted(
        str(d) for d, count in doc_id_counts.items() if count > 1
    )
    report.blocking_conflicts = [
        (sid, sorted(str(x) for x in set(dids)))
        for sid, dids in stable_id_doc_ids.items()
        if len(set(dids)) > 1
    ]
    # An unmappable point occupies a stable id we are about to (re)write.
    report.blocking_unmappable_occupied = sorted(
        pid for pid in unmappable_occupied if pid in stable_ids_set
    )

    # Never delete an id that is also a stable id for another document (rare
    # truncation collision). The dry-run preview uses the SAME filter as
    # execution so the operator preview cannot over-report deletions.
    safe_to_delete = [pid for pid in legacy_to_delete if pid not in stable_ids_set]

    if dry_run:
        report.upserted = mappable_count
        report.deleted_legacy = len(safe_to_delete)
        report.legacy_point_ids = safe_to_delete
        return report

    # Execute preflight: abort before any write if blocking conflicts exist.
    if (
        report.blocking_duplicates
        or report.blocking_conflicts
        or report.blocking_unmappable_occupied
    ):
        report.aborted = True
        return report

    # ---- Pass 2 (bounded batches): re-upsert at stable id, delete legacy ----
    # Vectors are held only for one batch; upsert/delete requests are bounded
    # by ``batch_size`` rather than the whole collection.
    safe_to_delete_set = set(safe_to_delete)
    batch: list[models.PointStruct] = []
    pending_deletes: list[int] = []
    upserted = 0
    deleted_ids: list[int] = []
    # A stable id may be created ahead of the live scroll offset; if scroll
    # later reaches it, process each stable id once (the upsert is idempotent).
    processed_stable: set[int] = set()

    def _flush() -> None:
        nonlocal batch, pending_deletes, upserted
        if batch:
            client.upsert(collection_name=collection_name, points=batch)
            upserted += len(batch)
            batch = []
        if pending_deletes:
            client.delete(
                collection_name=collection_name,
                points_selector=models.PointIdsList(points=pending_deletes),
            )
            deleted_ids.extend(pending_deletes)
            pending_deletes = []

    for rec in _scroll_all(client, collection_name, with_vectors=True):
        payload = getattr(rec, "payload", None) or {}
        doc_id = payload.get("doc_id") if isinstance(payload, dict) else None
        if not doc_id:
            continue
        stable_id = document_id_to_point_id(collection_name, doc_id)
        if stable_id in processed_stable:
            continue
        processed_stable.add(stable_id)
        batch.append(
            models.PointStruct(
                id=stable_id, vector=getattr(rec, "vector", None), payload=payload
            )
        )
        if rec.id in safe_to_delete_set:
            pending_deletes.append(rec.id)
        if len(batch) + len(pending_deletes) >= batch_size:
            _flush()
    _flush()

    report.upserted = upserted
    report.deleted_legacy = len(deleted_ids)
    report.legacy_point_ids = deleted_ids
    return report
