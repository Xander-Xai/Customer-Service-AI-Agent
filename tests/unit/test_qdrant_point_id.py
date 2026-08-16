"""P1-03: Stable Qdrant Point ID contract tests.

Pins the identity contract for Qdrant storage:

    logical document_id  ──document_id_to_point_id()──▶  stable int Point ID

Invariants (must hold across processes, restarts, and PYTHONHASHSEED):
  ID-1 deterministic        ID-2 idempotent upsert     ID-3 stable delete
  ID-4 valid Qdrant id      ID-5 collision policy      ID-6 migration safety
  ID-7 collection namespace ID-8 single mapping boundary

The previous implementation used ``hash(id_) & 0x7FFFFFFFFFFFFFFF`` inside
``add_documents``. Python's built-in ``hash()`` for strings is randomized per
process via ``PYTHONHASHSEED``, so the same logical doc_id mapped to a
*different* storage Point ID in every process restart — breaking idempotent
upsert (ghost duplicates) and any cross-process Point-ID reference. These
tests prove the stable replacement and lock the identity contract.
"""

import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from rag.qdrant_knowledge_base import _EMBEDDING_DIM, QdrantKnowledgeBase

# Repo root, used as PYTHONPATH for cross-process subprocesses so they can
# import the project's ``rag`` package in a fresh interpreter.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _point_id_in_subprocess(collection: str, doc_id: str, seed) -> str:
    """Compute document_id_to_point_id in a fresh Python process.

    ``seed`` is a PYTHONHASHSEED value (str) or ``None`` for default
    randomization. Returns the printed int as a string.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT
    if seed is None:
        env.pop("PYTHONHASHSEED", None)
    else:
        env["PYTHONHASHSEED"] = str(seed)
    script = (
        "from rag.point_id import document_id_to_point_id;"
        f"print(document_id_to_point_id({collection!r}, {doc_id!r}))"
    )
    out = subprocess.check_output([sys.executable, "-c", script], env=env)
    return out.decode().strip()


# ====================================================================
# ID-1 / ID-4 / ID-5(algo) / ID-7 / ID-8 : pure mapping contract
# ====================================================================


class TestPointIdMapping:
    def test_same_logical_id_produces_same_qdrant_point_id(self):
        """ID-1 (within process): same (collection, doc_id) -> same Point ID."""
        from rag.point_id import document_id_to_point_id

        a = document_id_to_point_id("product_knowledge", "pr_000")
        b = document_id_to_point_id("product_knowledge", "pr_000")
        assert a == b

    def test_point_id_is_valid_for_qdrant(self):
        """ID-4: result is a positive int63 — valid Qdrant uint64 point id,
        matching the existing collection's integer-id convention."""
        from rag.point_id import document_id_to_point_id

        for coll, doc in [
            ("product_knowledge", "pr_000"),
            ("faq", "faq_000"),
            ("tech_support", "tech_000"),
            ("complaint_knowledge", "co_000"),
        ]:
            pid = document_id_to_point_id(coll, doc)
            assert isinstance(pid, int)
            assert 0 <= pid <= 0x7FFFFFFFFFFFFFFF

    def test_distinct_doc_ids_produce_distinct_point_ids(self):
        """ID-5 (algorithm level): collision-resistance of the pure mapping —
        distinct (collection, doc_id) inputs must not collide within a
        realistic sample."""
        from rag.point_id import document_id_to_point_id

        seen: set[int] = set()
        for coll in ("product_knowledge", "faq", "tech_support", "complaint_knowledge"):
            for i in range(2000):
                pid = document_id_to_point_id(coll, f"{coll}_{i:06d}")
                assert pid not in seen, f"unexpected collision for {coll}_{i:06d}"
                seen.add(pid)

    def test_collection_namespace_semantics(self):
        """ID-7: collection participates in the namespace -> the same logical
        doc_id in two collections yields two different Point IDs (Qdrant
        collections are themselves isolated, so this is defense-in-depth)."""
        from rag.point_id import document_id_to_point_id

        a = document_id_to_point_id("product_knowledge", "shared_doc")
        b = document_id_to_point_id("faq", "shared_doc")
        assert a != b


# ====================================================================
# ID-1 (cross process) : the decisive reproduction-as-test
# ====================================================================


class TestCrossProcessStability:
    def test_point_id_stable_across_processes(self):
        """ID-1 across processes: two fresh Python processes with *different*
        PYTHONHASHSEED values must compute the same Point ID for the same
        logical doc_id. This is the direct replacement for the old
        ``hash(id_)`` instability that created ghost duplicates on restart."""
        a = _point_id_in_subprocess("product_knowledge", "pr_000", "0")
        b = _point_id_in_subprocess("product_knowledge", "pr_000", "1")
        c = _point_id_in_subprocess("product_knowledge", "pr_000", None)  # randomized
        assert a == b == c, f"unstable across processes: {a} {b} {c}"
        assert a.isdigit()
        assert 0 <= int(a) <= 0x7FFFFFFFFFFFFFFF

    def test_mapping_does_not_depend_on_python_hash_seed(self):
        """ID-1 explicit seed-independence: many seeds (incl. default) agree."""
        ids = {_point_id_in_subprocess("faq", "faq_000", s) for s in ("0", "1", "2", "42", None)}
        assert len(ids) == 1, f"seed-dependent mapping: {ids}"


# ====================================================================
# fixtures for QdrantKnowledgeBase-level tests
# ====================================================================


@pytest.fixture
def mock_qdrant_client():
    with (
        patch("rag.qdrant_knowledge_base.QdrantClient") as mock_client_cls,
        patch.object(QdrantKnowledgeBase, "_create_embedding_function", return_value=MagicMock()),
    ):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.get_collections.return_value = MagicMock(collections=[])
        mock_count = MagicMock()
        mock_count.count = 0
        mock_client.count.return_value = mock_count
        # retrieve() defaults to an empty iterable under a bare MagicMock
        # (verified: list(MagicMock()) == []), so no existing points -> no
        # collision -> add_documents proceeds to upsert.
        yield mock_client


@pytest.fixture
def kb(mock_qdrant_client):
    return QdrantKnowledgeBase(host="localhost", port=6333)


def _upserted_points(client, call_index: int):
    """Return the `points` list passed to the Nth upsert call."""
    call = client.upsert.call_args_list[call_index]
    return call.kwargs.get("points") or call.args[-1]


# ====================================================================
# ID-8 / ID-2 : upsert routes through the stable boundary; idempotent
# ====================================================================


class TestUpsertUsesStableId:
    def test_add_documents_upserts_with_stable_point_id(self, kb, mock_qdrant_client):
        """ID-8: add_documents routes Point ID generation through
        document_id_to_point_id (no business hash)."""
        from rag.point_id import document_id_to_point_id

        kb._embed_texts = MagicMock(return_value=[[0.1] * _EMBEDDING_DIM])
        kb.add_documents("product_knowledge", ["doc1"], ids=["pr_000"])
        assert mock_qdrant_client.upsert.called
        points = _upserted_points(mock_qdrant_client, 0)
        assert len(points) >= 1
        assert points[0].id == document_id_to_point_id("product_knowledge", "pr_000")

    def test_duplicate_upsert_is_idempotent(self, kb, mock_qdrant_client):
        """ID-2: re-importing the same logical doc_id targets the SAME Point
        ID so Qdrant overwrites the existing point instead of creating a
        ghost duplicate (the exact failure mode of the old hash() approach
        across processes)."""
        from rag.point_id import document_id_to_point_id

        kb._embed_texts = MagicMock(return_value=[[0.1] * _EMBEDDING_DIM])
        kb.add_documents("product_knowledge", ["doc1"], ids=["pr_000"])
        first_id = _upserted_points(mock_qdrant_client, 0)[0].id
        kb.add_documents("product_knowledge", ["doc1"], ids=["pr_000"])
        second_id = _upserted_points(mock_qdrant_client, 1)[0].id
        expected = document_id_to_point_id("product_knowledge", "pr_000")
        assert first_id == second_id == expected


# ====================================================================
# ID-5 (storage) : collision policy — refuse silent overwrite
# ====================================================================


class TestCollisionPolicy:
    def test_refuses_overwrite_when_existing_point_has_different_doc_id(
        self, kb, mock_qdrant_client
    ):
        """ID-5 (storage): if the computed stable Point ID already exists with
        a *different* logical doc_id, the system MUST refuse (raise) rather
        than silently clobber the other document."""
        from rag.point_id import PointIdCollisionError, document_id_to_point_id

        pid = document_id_to_point_id("product_knowledge", "pr_000")
        existing = MagicMock()
        existing.id = pid
        existing.payload = {"doc_id": "someone_else", "content": "x"}
        mock_qdrant_client.retrieve.return_value = [existing]

        kb._embed_texts = MagicMock(return_value=[[0.1] * _EMBEDDING_DIM])
        with pytest.raises(PointIdCollisionError):
            kb.add_documents("product_knowledge", ["doc1"], ids=["pr_000"])
        # refused BEFORE upsert — no silent overwrite
        assert not mock_qdrant_client.upsert.called

    def test_allows_idempotent_reinsert_when_same_doc_id(self, kb, mock_qdrant_client):
        """ID-5 + ID-2: same Point ID AND same stored logical doc_id is a
        legitimate idempotent re-upsert — proceeds (Qdrant overwrites)."""
        from rag.point_id import document_id_to_point_id

        pid = document_id_to_point_id("product_knowledge", "pr_000")
        existing = MagicMock()
        existing.id = pid
        existing.payload = {"doc_id": "pr_000", "content": "old"}
        mock_qdrant_client.retrieve.return_value = [existing]

        kb._embed_texts = MagicMock(return_value=[[0.1] * _EMBEDDING_DIM])
        kb.add_documents("product_knowledge", ["doc1"], ids=["pr_000"])
        assert mock_qdrant_client.upsert.called


# ====================================================================
# BF-P1-03-01 / intra-batch / delimiter : adversarial negative contracts
# (reproduced by independent acceptance review; must FAIL before the fix)
# ====================================================================


class TestCollisionGuardAdversarial:
    def test_refuses_overwrite_when_existing_point_has_no_doc_id(self, kb, mock_qdrant_client):
        """BF-P1-03-01: an existing point at the target ID whose owner CANNOT
        be verified (empty {} payload, or unreadable payload) must NOT be
        treated as "unoccupied". Fail closed — refuse the upsert rather than
        silently overwriting an owner we cannot identify."""
        from rag.point_id import PointIdCollisionError, document_id_to_point_id

        pid = document_id_to_point_id("product_knowledge", "pr_000")
        existing = MagicMock()
        existing.id = pid
        existing.payload = {}  # occupied, owner unverifiable
        mock_qdrant_client.retrieve.return_value = [existing]

        kb._embed_texts = MagicMock(return_value=[[0.1] * _EMBEDDING_DIM])
        with pytest.raises(PointIdCollisionError):
            kb.add_documents("product_knowledge", ["doc1"], ids=["pr_000"])
        assert not mock_qdrant_client.upsert.called

    def test_refuses_overwrite_when_payload_doc_id_is_missing_key(self, kb, mock_qdrant_client):
        """BF-P1-03-01 variant: payload present but has no `doc_id` key at all
        (e.g. legacy point from a different schema). Must fail closed."""
        from rag.point_id import PointIdCollisionError, document_id_to_point_id

        pid = document_id_to_point_id("product_knowledge", "pr_000")
        existing = MagicMock()
        existing.id = pid
        existing.payload = {"content": "orphan", "source": "legacy"}  # no doc_id key
        mock_qdrant_client.retrieve.return_value = [existing]

        kb._embed_texts = MagicMock(return_value=[[0.1] * _EMBEDDING_DIM])
        with pytest.raises(PointIdCollisionError):
            kb.add_documents("product_knowledge", ["doc1"], ids=["pr_000"])
        assert not mock_qdrant_client.upsert.called

    def test_refuses_intra_batch_collision_between_two_distinct_doc_ids(
        self, kb, mock_qdrant_client
    ):
        """BF-P1-03 (intra-batch): two distinct doc_ids in ONE add_documents
        call that map to the same stable Point ID must not both be sent to
        Qdrant (last-wins silent clobber). The guard must reject the batch."""
        from rag.point_id import PointIdCollisionError

        # Force a same-stable-id collision within the batch by monkeypatching
        # the mapping (SHA-256 makes this astronomically rare naturally).
        kb._embed_texts = MagicMock(return_value=[[0.1] * _EMBEDDING_DIM, [0.2] * _EMBEDDING_DIM])

        with (
            patch("rag.qdrant_knowledge_base.document_id_to_point_id", return_value=777),
            pytest.raises(PointIdCollisionError),
        ):
            kb.add_documents("product_knowledge", ["docA", "docB"], ids=["idA", "idB"])
        assert not mock_qdrant_client.upsert.called

    def test_mapping_rejects_colon_bearing_inputs(self):
        """ID-7 boundary hardening: a ':' in collection or doc_id would make
        the `"{coll}:{doc}"` framing ambiguous ((`"a:b","c")` vs
        `("a","b:c")`). Rather than silently encoding them differently
        (which would change every ordinary ID — see BF-P1-03-04), the
        mapping rejects ':' outright. No configured collection/doc_id uses
        ':'. This keeps the accepted df328b5 mapping byte-stable while
        removing the ambiguity."""
        from rag.point_id import document_id_to_point_id

        for coll, doc in [("a:b", "c"), ("a", "b:c"), ("pre:post", "x"), ("x", "y:z")]:
            with pytest.raises(ValueError):
                document_id_to_point_id(coll, doc)

    def test_mapping_is_byte_identical_to_accepted_df328b5_mapping(self):
        """BF-P1-03-04 regression guard: the stable mapping MUST stay
        byte-identical to the accepted `df328b5` mapping
        (`SHA256(f"{coll}:{doc}")[:8] & 0x7FFFFFFFFFFFFFFF`) for ordinary
        inputs. A re-framing (e.g. length-prefixed encoding) changes every
        Point ID and recreates ghost duplicates on rolling re-import. This
        locks the accepted mapping's exact bytes against drift."""
        import hashlib

        from rag.point_id import document_id_to_point_id

        def accepted_df328b5(coll, doc):
            return (
                int.from_bytes(hashlib.sha256(f"{coll}:{doc}".encode()).digest()[:8], "big")
                & 0x7FFFFFFFFFFFFFFF
            )

        for coll, doc in [
            ("product_knowledge", "pr_000"),
            ("faq", "faq_000"),
            ("tech_support", "tech_000"),
            ("complaint_knowledge", "co_000"),
            ("image_knowledge", "img_000"),
            ("product_knowledge", "derm_001310"),
        ]:
            assert document_id_to_point_id(coll, doc) == accepted_df328b5(coll, doc), (
                f"mapping drifted from df328b5 for {coll}/{doc}"
            )

    def test_reimport_after_df328b5_collection_does_not_create_ghost_duplicate(
        self, kb, mock_qdrant_client
    ):
        """BF-P1-03-04 cross-commit regression: a collection written under
        the accepted df328b5 mapping holds a point at the df328b5 ID. A
        normal re-import under the current mapping MUST target the SAME
        Point ID (so Qdrant overwrites in place) — NOT a new ID that leaves
        the old point as a ghost duplicate. This is the exact failure mode
        a length-prefixed re-framing reintroduced during rolling deploy."""
        import hashlib

        def df328b5(coll, doc):
            return (
                int.from_bytes(hashlib.sha256(f"{coll}:{doc}".encode()).digest()[:8], "big")
                & 0x7FFFFFFFFFFFFFFF
            )

        old_id = df328b5("product_knowledge", "pr_000")
        # The collection still holds the df328b5-era point at old_id.
        existing = MagicMock()
        existing.id = old_id
        existing.payload = {"doc_id": "pr_000", "content": "old"}
        # The guard retrieves at the CURRENT mapping's id — which must equal
        # old_id, so it sees the existing point (idempotent re-upsert, no ghost).
        mock_qdrant_client.retrieve.return_value = [existing]

        kb._embed_texts = MagicMock(return_value=[[0.1] * _EMBEDDING_DIM])
        kb.add_documents("product_knowledge", ["doc1"], ids=["pr_000"])

        upserted_id = _upserted_points(mock_qdrant_client, 0)[0].id
        assert upserted_id == old_id, (
            f"current mapping ({upserted_id}) != df328b5 id ({old_id}): "
            "re-import would leave a ghost duplicate"
        )
        # guard retrieved at the same id as the persisted point
        retrieved_ids = mock_qdrant_client.retrieve.call_args.kwargs["ids"]
        assert retrieved_ids == [old_id]


# ====================================================================
# ID-3 : delete contract (regression guard — preserved behavior)
# ====================================================================


class TestDeleteContract:
    def test_delete_uses_payload_filter_on_logical_doc_id(self, kb, mock_qdrant_client):
        """ID-3: delete by logical doc_id via a payload Filter — stable,
        independent of the Point ID algorithm, and uniquely capable of
        cleaning up legacy ghost duplicates (delete-by-point-id would leave
        them). This locks the canonical delete contract."""
        from qdrant_client.http import models

        kb.delete_documents("product_knowledge", ["pr_000"])
        assert mock_qdrant_client.delete.called
        selector = mock_qdrant_client.delete.call_args.kwargs.get("points_selector")
        assert isinstance(selector, models.Filter)
        # filter is on the doc_id payload key, not a point-id list
        assert any(getattr(c, "key", None) == "doc_id" for c in (selector.must or []))


# ====================================================================
# ID-8 : single mapping boundary (source-level guard)
# ====================================================================


class TestSingleMappingBoundary:
    def test_knowledge_base_source_uses_stable_mapping_not_hash(self):
        """ID-8: the old ``hash(id_) & 0x7FFFFFFFFFFFFFFF`` *executable*
        pattern is gone from the knowledge-base code, and the stable boundary
        ``document_id_to_point_id`` is wired into add_documents.

        Scans executable statements (assignment/argument positions) via AST
        so an explanatory comment mentioning the old pattern does not mask a
        real regression — and does not false-positive on prose either.
        """
        import ast
        import inspect

        import rag.qdrant_knowledge_base as mod

        tree = ast.parse(inspect.getsource(mod))

        def _is_builtin_hash(node: ast.AST) -> bool:
            # A bare `hash(...)` call where the function name resolves to the
            # builtin. ``id=hash(...)`` (old point-id site) and
            # ``foo = hash(...)`` both match; ``hashlib.sha256`` does not.
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "hash"
            )

        builtin_hash_calls = [n for n in ast.walk(tree) if _is_builtin_hash(n)]
        assert not builtin_hash_calls, (
            f"rag.qdrant_knowledge_base still calls builtin hash(): "
            f"{[ast.dump(n) for n in builtin_hash_calls]}"
        )

        src = inspect.getsource(mod)
        assert "document_id_to_point_id" in src


# ====================================================================
# Benchmark / golden identity contract (regression guards — spec #10)
# ====================================================================


class TestBenchmarkIdentityContract:
    def test_benchmark_expected_ids_are_logical_doc_ids_not_point_ids(self):
        """spec #10: golden expected_doc_ids are logical string doc_ids and
        never int Point IDs — so benchmark identity is independent of the
        storage Point ID algorithm and survives the P1-03 change."""
        import json

        golden = os.path.join(REPO_ROOT, "tests", "eval", "golden", "expected_doc_ids.json")
        with open(golden) as f:
            data = json.load(f)
        assert data, "golden expected_doc_ids.json is empty"
        for _qid, doc_ids in data.items():
            for did in doc_ids:
                assert isinstance(did, str), f"expected logical str doc_id, got {did!r}"
                assert not isinstance(did, int)

    def test_parse_query_result_exposes_logical_doc_id_as_id(self, kb):
        """spec #10: the retrieval result ``id`` field is the logical doc_id
        from payload, NOT the storage Point ID — so benchmark comparisons
        remain stable across the ID-algorithm change."""
        point = MagicMock()
        point.id = 999999999999  # arbitrary storage Point ID
        point.score = 0.9
        point.payload = {"doc_id": "pr_000", "content": "c"}
        result = kb._parse_query_result([point])
        assert result[0]["id"] == "pr_000"  # logical, not 999999999999
