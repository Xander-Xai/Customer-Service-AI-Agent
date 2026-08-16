"""P1-03 / ID-6: legacy Point ID discovery & rebuild (migration safety).

These tests pin the read-only discovery (dry run) and the safe in-place
rebuild contract. No real Qdrant is required — the qdrant client is mocked.
"""

from unittest.mock import MagicMock

import pytest

from rag.point_id import document_id_to_point_id
from rag.point_id_migration import (
    RebuildReport,
    discover_legacy_points,
    rebuild_collection_point_ids,
)


def _rec(point_id, doc_id, *, content="x", vector=None):
    """A scroll Record-like object."""
    r = MagicMock()
    r.id = point_id
    r.payload = {"doc_id": doc_id, "content": content} if doc_id is not None else {}
    r.vector = vector
    return r


def _client_returning(records):
    """A mock client whose scroll returns one page of records then stops."""
    client = MagicMock()
    client.scroll.return_value = (list(records), None)
    return client


# ====================================================================
# discover_legacy_points — read-only classification (dry run)
# ====================================================================


class TestDiscoverLegacyPoints:
    def test_classifies_stable_legacy_and_unmappable(self):
        stable_pid = document_id_to_point_id("c", "stable_doc")
        client = _client_returning(
            [
                _rec(stable_pid, "stable_doc"),  # stable
                _rec(12345, "legacy_doc"),  # legacy (wrong id)
                _rec(99999, None),  # unmappable (no doc_id)
            ]
        )
        report = discover_legacy_points(client, "c")
        assert report.collection == "c"
        assert report.total_points == 3
        assert report.stable_points == 1
        assert report.legacy_points == 1
        assert report.unmappable_points == 1
        assert 12345 in report.legacy_point_ids
        assert 99999 in report.legacy_point_ids  # unmappable counted as legacy-by-id

    def test_detects_duplicate_logical_doc_ids(self):
        client = _client_returning(
            [
                _rec(111, "dup_doc"),
                _rec(222, "dup_doc"),  # same logical id at two points
                _rec(document_id_to_point_id("c", "ok"), "ok"),
            ]
        )
        report = discover_legacy_points(client, "c")
        assert report.duplicate_logical_ids == ["dup_doc"]

    def test_detects_potential_stable_id_conflicts(self, monkeypatch):
        # A real SHA-256 truncation collision (two distinct doc_ids mapping
        # to the same stable id) is astronomically rare and cannot be
        # synthesized from real inputs. Monkeypatch the mapping to force one
        # so the detection logic is exercised honestly.
        import rag.point_id_migration as mig

        def fake_map(coll, doc_id):
            if doc_id in ("docA", "docB"):
                return 0x1234
            return document_id_to_point_id(coll, doc_id)

        monkeypatch.setattr(mig, "document_id_to_point_id", fake_map)
        client = _client_returning(
            [
                _rec(11, "docA"),  # stable id 0x1234, stored 11 -> legacy
                _rec(22, "docB"),  # stable id 0x1234, stored 22 -> legacy
            ]
        )
        report = discover_legacy_points(client, "c")
        assert len(report.potential_conflicts) == 1
        sid, dids = report.potential_conflicts[0]
        assert sid == 0x1234
        assert set(dids) == {"docA", "docB"}

    def test_discovery_is_read_only(self):
        client = _client_returning([_rec(12345, "legacy_doc")])
        discover_legacy_points(client, "c")
        assert not client.upsert.called
        assert not client.delete.called
        assert not client.delete_collection.called


# ====================================================================
# rebuild_collection_point_ids — dry-run default + safe execute
# ====================================================================


class TestRebuildCollectionPointIds:
    def test_dry_run_default_makes_no_writes(self):
        legacy = _rec(12345, "doc1", vector=[0.1, 0.2])
        client = _client_returning([legacy])
        report = rebuild_collection_point_ids(client, "c")  # dry_run defaults True
        assert isinstance(report, RebuildReport)
        assert report.dry_run is True
        assert report.scrolled == 1
        assert report.upserted == 1  # would upsert 1 stable point
        assert report.deleted_legacy == 1  # would delete 1 legacy id
        assert not client.upsert.called
        assert not client.delete.called

    def test_execute_upserts_at_stable_id_and_deletes_legacy(self):
        stable_pid = document_id_to_point_id("c", "doc1")
        legacy = _rec(12345, "doc1", vector=[0.1, 0.2])
        already_stable = _rec(stable_pid, "doc2", vector=[0.3, 0.4])
        client = _client_returning([legacy, already_stable])
        report = rebuild_collection_point_ids(client, "c", dry_run=False)
        assert report.dry_run is False
        assert report.upserted == 2
        assert report.deleted_legacy == 1  # only the legacy id, not the stable one
        # upserted points carry stable ids
        pts = client.upsert.call_args.kwargs["points"]
        upserted_ids = {p.id for p in pts}
        assert upserted_ids == {
            document_id_to_point_id("c", "doc1"),
            document_id_to_point_id("c", "doc2"),
        }
        # legacy 12345 deleted; stable_pid was NOT deleted
        assert client.delete.called
        selector = client.delete.call_args.kwargs["points_selector"]
        assert 12345 in selector.points
        assert stable_pid not in selector.points

    def test_execute_skips_unmappable_without_doc_id(self):
        unmappable = _rec(77777, None, vector=[0.5])
        client = _client_returning([unmappable])
        report = rebuild_collection_point_ids(client, "c", dry_run=False)
        assert report.skipped_unmappable == 1
        assert report.upserted == 0
        assert not client.upsert.called

    def test_execute_never_deletes_a_stable_id_even_if_legacy_collides(self):
        """Safety: if a legacy point's old id happens to equal another doc's
        stable id (rare collision), the rebuild must NOT delete it (that
        would remove a stable point). The legacy point is left for manual
        resolution via discover_legacy_points.potential_conflicts."""
        stable_pid_b = document_id_to_point_id("c", "docB")
        # legacy point for docA happens to sit at docB's stable id
        legacy_colliding = _rec(stable_pid_b, "docA", vector=[0.1])
        # docB's own point is present at its correct stable id, so
        # stable_pid_b is in stable_ids_set and must be protected.
        docb_stable = _rec(stable_pid_b, "docB", vector=[0.2])
        client = _client_returning([legacy_colliding, docb_stable])
        rebuild_collection_point_ids(client, "c", dry_run=False)
        if client.delete.called:
            selector = client.delete.call_args.kwargs["points_selector"]
            assert stable_pid_b not in selector.points


# ====================================================================
# BF-P1-03-01 (migration) / BF-P1-03-02 : adversarial migration contracts
# (reproduced by independent acceptance review; must FAIL before the fix)
# ====================================================================


class TestMigrationExecuteAdversarial:
    def test_execute_aborts_when_stable_id_already_occupied_by_unmappable_point(self):
        """BF-P1-03-01 (migration): a mappable document being rebuilt whose
        stable ID is already occupied by an UNMAPPABLE legacy point (no
        doc_id) must not be overwritten. Execute aborts (structured report,
        no raise) before any upsert/delete, surfacing the blocking id for
        manual resolution."""
        target = document_id_to_point_id("c", "docX")
        # unmappable legacy point sitting AT docX's stable id
        orphan = _rec(target, None, vector=[0.9])
        # docX itself, present as a legacy point at a different (wrong) id,
        # being rebuilt onto its stable id == `target` (collides with orphan)
        docx_legacy = _rec(22222, "docX", vector=[0.1])
        client = _client_returning([orphan, docx_legacy])
        report = rebuild_collection_point_ids(client, "c", dry_run=False)
        assert report.aborted is True
        assert target in report.blocking_unmappable_occupied
        assert not client.upsert.called
        assert not client.delete.called

    def test_execute_aborts_on_duplicate_logical_ids_collapsing_to_one_stable_id(self):
        """BF-P1-03-02: two legacy points carrying the SAME logical doc_id
        collapse to the same stable ID. Execute must not silently last-wins
        upsert them. It must either deterministically pick a winner and
        verify, or fail closed. Either way: exactly one upserted point at
        that stable id, no ambiguity, and no legacy delete before resolution."""
        dup_a = _rec(222, "dup_doc", vector=[0.1])
        dup_b = _rec(333, "dup_doc", vector=[0.2])
        client = _client_returning([dup_a, dup_b])
        report = rebuild_collection_point_ids(client, "c", dry_run=True)
        # dry-run must SURFACE the unresolved duplicate as a blocking signal
        assert "dup_doc" in report.blocking_duplicates

    def test_execute_aborts_on_distinct_doc_ids_mapping_to_same_stable_id(self):
        """BF-P1-03-02 (forced collision): two DISTINCT doc_ids mapping to the
        same stable ID (astronomically rare via SHA-256; forced here) must
        abort execute before any upsert. The collision must be surfaced, not
        silently last-wins-clobbered. Aborts via structured report (no raise)
        so the CLI can surface it and return non-zero."""
        legacy_a = _rec(444, "docA", vector=[0.1])
        legacy_b = _rec(555, "docB", vector=[0.2])
        client = _client_returning([legacy_a, legacy_b])

        import rag.point_id_migration as mig

        def fake_map(coll, doc_id):
            # force docA and docB to the same stable id
            return 0x999 if doc_id in ("docA", "docB") else document_id_to_point_id(coll, doc_id)

        with _patched(mig, "document_id_to_point_id", fake_map):
            report = rebuild_collection_point_ids(client, "c", dry_run=False)
        assert report.aborted is True
        assert len(report.blocking_conflicts) == 1
        _sid, dids = report.blocking_conflicts[0]
        assert set(dids) == {"docA", "docB"}
        assert not client.upsert.called
        assert not client.delete.called


class _MonkeyPatch:
    """Minimal contextmanager-free monkeypatch helper (test self-contained)."""

    def __init__(self, target, name, value):
        self.target, self.name, self.value = target, name, value
        self._orig = None

    def __enter__(self):
        self._orig = getattr(self.target, self.name)
        setattr(self.target, self.name, self.value)
        return self

    def __exit__(self, *exc):
        setattr(self.target, self.name, self._orig)


def _patched(target, name, value):
    return _MonkeyPatch(target, name, value)


# ====================================================================
# CLI safety contract (Step 15) — dry-run default, refuse unack'd execute
# ====================================================================


class TestMigrateCliSafety:
    def test_default_mode_is_dry_run_and_needs_no_confirmation(self):
        from scripts.migrate_point_ids import build_parser, validate_args

        args = build_parser().parse_args(["--collection", "c"])
        assert args.execute is False
        validate_args(args)  # does not raise — safe default

    def test_execute_without_acknowledgement_is_refused(self):
        from scripts.migrate_point_ids import build_parser, validate_args

        args = build_parser().parse_args(["--collection", "c", "--execute"])
        with pytest.raises(SystemExit) as exc:
            validate_args(args)
        assert exc.value.code == 2

    def test_execute_with_acknowledgement_is_accepted(self):
        from scripts.migrate_point_ids import build_parser, validate_args

        args = build_parser().parse_args(
            ["--collection", "c", "--execute", "--i-understand-this-is-destructive"]
        )
        validate_args(args)  # does not raise
        assert args.execute is True

    def test_execute_and_dry_run_are_mutually_exclusive(self):
        from scripts.migrate_point_ids import build_parser

        with pytest.raises(SystemExit):
            build_parser().parse_args(["--collection", "c", "--execute", "--dry-run"])

    def test_requires_at_least_one_target(self):
        from scripts.migrate_point_ids import build_parser

        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_per_collection_failure_returns_nonzero_exit(self, monkeypatch, capsys):
        """BF-P1-03-03: when a selected collection raises during processing,
        the CLI must return a NON-zero exit code (no migration false-green),
        while still emitting the structured error report."""
        import scripts.migrate_point_ids as m

        # mock a connected client; the rebuild call raises per-collection
        client = MagicMock()
        client.get_collections.return_value = MagicMock(collections=[])
        client.scroll.side_effect = RuntimeError("scroll blew up")
        monkeypatch.setattr(m, "_connect_client", lambda: client)

        rc = m.main(["--collection", "c", "--execute", "--i-understand-this-is-destructive"])
        assert rc != 0, "per-collection failure must not return exit 0"
        out = capsys.readouterr()
        assert "error" in (out.out + out.err).lower()

    def test_all_collections_succeed_returns_zero_exit(self, monkeypatch):
        """BF-P1-03-03 inverse: a fully-successful run returns 0 (no
        over-broad failure)."""
        import scripts.migrate_point_ids as m

        client = MagicMock()
        client.get_collections.return_value = MagicMock(collections=[])
        client.scroll.return_value = ([], None)  # empty collection -> clean
        monkeypatch.setattr(m, "_connect_client", lambda: client)

        rc = m.main(["--collection", "c", "--dry-run"])
        assert rc == 0
