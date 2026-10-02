"""审批提案脱敏测试。

审批表是**长期留痕**的：``proposal`` 列会把业务参数（订单号、金额）持久化下来。
如果脱敏失效，它就同时是凭据泄漏通道和超长载荷放大器。测试锁定的是「不泄漏」
与「不失控」两条底线，而不是脱敏的具体格式。
"""

from __future__ import annotations

import json

from core.hitl.sanitize import (
    MAX_DEPTH,
    MAX_ITEMS,
    MAX_STRING_LENGTH,
    REDACTED,
    is_sensitive_key,
    sanitize_proposal,
    sanitize_text,
)


class TestSensitiveKeys:
    def test_credential_keys_are_sensitive(self):
        for key in (
            "password",
            "api_key",
            "apiKey",
            "token",
            "secret",
            "card_no",
            "id_card",
            "cvv",
            "authorization",
            "cookie",
            "session_token",
            "private_key",
        ):
            assert is_sensitive_key(key) is True, key

    def test_business_keys_are_not_sensitive(self):
        """业务可判定字段必须保留——否则审批退化成盲批。"""
        for key in ("order_id", "amount", "new_status", "reason", "product_id", "quantity"):
            assert is_sensitive_key(key) is False, key


class TestSanitizeProposal:
    def test_redacts_but_keeps_key_visible(self):
        """掩码后保留 key：审批人要看得出「这里原本有值、已被隐藏」。"""
        out = sanitize_proposal({"order_id": "O-1", "password": "hunter2"})
        assert out["order_id"] == "O-1"
        assert out["password"] == REDACTED
        assert "hunter2" not in json.dumps(out, ensure_ascii=False)

    def test_nested_redaction(self):
        out = sanitize_proposal({"outer": {"inner": {"api_key": "sk-live-123", "amount": 10}}})
        assert out["outer"]["inner"]["api_key"] == REDACTED
        assert out["outer"]["inner"]["amount"] == 10
        assert "sk-live-123" not in json.dumps(out)

    def test_list_nested_redaction(self):
        out = sanitize_proposal({"items": [{"token": "t1"}, {"token": "t2"}]})
        assert all(item["token"] == REDACTED for item in out["items"])

    def test_long_string_is_clipped_with_marker(self):
        out = sanitize_proposal({"reason": "x" * (MAX_STRING_LENGTH + 100)})
        assert len(out["reason"]) < MAX_STRING_LENGTH + 100
        assert "chars]" in out["reason"]

    def test_depth_limit_stops_recursion(self):
        """超深嵌套整体截断，不无限递归。"""
        payload: dict = {"leaf": "value"}
        for _ in range(MAX_DEPTH + 5):
            payload = {"deeper": payload}
        out = sanitize_proposal(payload)
        assert "TRUNCATED:max_depth" in json.dumps(out)

    def test_value_within_depth_survives(self):
        """未超深的业务值仍然可见——截断不是「全抹掉」。"""
        payload: dict = {"leaf": "value"}
        for _ in range(MAX_DEPTH - 2):
            payload = {"deeper": payload}
        out = sanitize_proposal(payload)
        assert "value" in json.dumps(out)
        assert "TRUNCATED" not in json.dumps(out)

    def test_item_limit_truncates_large_containers(self):
        out = sanitize_proposal({f"k{i}": i for i in range(MAX_ITEMS + 40)})
        assert len(out) <= MAX_ITEMS + 1  # +1 = __truncated__ 标记
        assert "__truncated__" in out

    def test_scalars_pass_through(self):
        assert sanitize_proposal(None) is None
        assert sanitize_proposal(True) is True
        assert sanitize_proposal(7) == 7
        assert sanitize_proposal(1.5) == 1.5
        assert sanitize_proposal("plain") == "plain"

    def test_result_is_json_serializable(self):
        out = sanitize_proposal({"a": {"b": [1, 2, {"c": "d"}]}, "e": None})
        json.dumps(out)  # 不抛异常即为通过（proposal 列是 JSON）

    def test_unserializable_value_degrades_to_repr(self):
        out = sanitize_proposal({"weird": object()})
        assert isinstance(out["weird"], str)
        json.dumps(out)

    def test_does_not_mutate_input(self):
        original = {"password": "hunter2", "nested": {"token": "t"}}
        snapshot = json.dumps(original, sort_keys=True)
        sanitize_proposal(original)
        assert json.dumps(original, sort_keys=True) == snapshot

    def test_non_string_keys_are_coerced(self):
        out = sanitize_proposal({1: "a", None: "b"})
        assert "1" in out and "None" in out


class TestSanitizeText:
    def test_none_passthrough(self):
        assert sanitize_text(None) is None

    def test_short_text_unchanged(self):
        assert sanitize_text("ok") == "ok"

    def test_long_text_clipped(self):
        out = sanitize_text("y" * 5000, limit=100)
        assert len(out) < 5000
        assert "chars]" in out
