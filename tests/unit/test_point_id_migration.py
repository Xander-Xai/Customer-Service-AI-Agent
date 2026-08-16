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
