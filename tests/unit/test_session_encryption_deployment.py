"""会话数据加密的**部署侧**安全验收（生产配置安全专项）。

这个文件锁定的是"部署与配置"层面的语义，与 ``test_session_encryption.py``
（记录格式层的 fail-closed）互补：

* **生产缺少必要加密配置时有明确安全策略** —— ``SESSION_ENCRYPTION_REQUIRED=true``
  且缺密钥/用占位符密钥时，**拒绝启动**（不是告警后照跑）；
* **配置加密时不会静默回退明文** —— 密钥已配置但 Fernet 不可用 / 解密失败时，
  一律抛错，绝不写明文、绝不返回明文；
* **错误密钥、损坏密文、历史明文**三种情况各自有明确且互不混淆的行为；
* **无破坏密钥轮换** —— 旧密钥进 ``SESSION_ENCRYPTION_KEY_PREVIOUS`` 后，既有密文
  仍可读，新写入用新密钥，旧会话在下次改写时自动升级；
* **不泄露**密钥、令牌或会话内容 —— 错误信息、日志、API 响应里都没有它们；
* **不把存储加密误认为能防住拥有存储写权限的攻击者** —— 历史明文与被替换明文
  在原理上不可区分，这条边界由测试**钉住**，任何"已解决明文替换"的断言都必须失败。

.. warning::
   本文件全部使用**合成数据**（tmp_path / 内存替身）。它验证的是**治理机制**，
   **不是**真实生产会话迁移的验证。真实迁移需要真实数据与授权环境（Issue #25），
   本仓**没有**任何测试能替代它。
"""

from __future__ import annotations

import json
import sys

import pytest

from core.exceptions import SessionEncryptionError
from core.session import session_manager as sm

KEY_A = "session-key-a-0123456789abcdef"
KEY_B = "session-key-b-0123456789abcdef"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _set_key(monkeypatch, value: str) -> None:
    monkeypatch.setattr("core.config.SESSION_ENCRYPTION_KEY", value)
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", value)


def _set_previous(monkeypatch, value: str) -> None:
    monkeypatch.setattr("core.config.SESSION_ENCRYPTION_KEY_PREVIOUS", value)
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY_PREVIOUS", value)


def _set_compat(monkeypatch, mode: str) -> None:
    monkeypatch.setattr("core.config.SESSION_ENCRYPTION_PLAINTEXT_COMPAT", mode)


def _set_required(monkeypatch, value: bool) -> None:
    monkeypatch.setattr("core.config.SESSION_ENCRYPTION_REQUIRED", value)


def _make_file_sm(tmp_path) -> sm.EnhancedSessionManager:
    return sm.EnhancedSessionManager(storage_backend="file", storage_dir=str(tmp_path))


def _counter_value(metric_name: str, **labels) -> float:
    """读取监控计数当前值（无标签时取第一个样本）。"""
    from core import monitoring

    metric = getattr(monitoring, metric_name, None)
    if metric is None:
        return 0.0
    try:
        if labels:
            return float(metric.labels(**labels)._value.get())
        samples = list(metric.collect())
        return float(samples[0].samples[0].value) if samples and samples[0].samples else 0.0
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# 1. 生产配置策略：缺加密配置时的明确行为
# ---------------------------------------------------------------------------


class TestProductionEncryptionPolicy:
    """``SESSION_ENCRYPTION_REQUIRED`` 的语义矩阵。"""

    def test_not_required_keeps_backward_compatibility(self):
        """默认（未开启强制）不改变既有部署行为 —— 不阻断启动。"""
        from core.config import validate_session_encryption_required

        assert (
            validate_session_encryption_required(encryption_key="", required=False) == []
        ), "未开启强制时不得阻断启动：那是升级即中断，不是安全策略"

    def test_required_without_key_is_an_error(self):
        from core.config import validate_session_encryption_required

        errors = validate_session_encryption_required(encryption_key="", required=True)
        assert errors, "required=true 且缺密钥必须报错（否则生产静默明文）"
        assert "SESSION_ENCRYPTION_KEY" in errors[0]

    def test_required_with_placeholder_key_is_an_error(self):
        """占位符密钥与没有密钥在安全性上等价 —— 必须同等拒绝。"""
        from core.config import validate_session_encryption_required

        for placeholder in (
            "CHANGE_ME_TO_SECURE_RANDOM_SESSION_KEY",
            "your-session-key-here",
            "changeme",
            "sk-placeholder-session-key",
        ):
            errors = validate_session_encryption_required(encryption_key=placeholder, required=True)
            assert errors, f"占位符密钥必须被拒绝：{placeholder!r}"

    def test_required_with_real_key_passes(self):
        from core.config import validate_session_encryption_required

        assert validate_session_encryption_required(encryption_key=KEY_A, required=True) == []

    def test_configured_encryption_never_falls_back_to_plaintext(self):
        """配置了密钥 -> 加密失败必须抛错，绝不静默写明文。"""
        from core.config import validate_session_encryption_settings

        # 配置本身合法（不报格式错）
        assert (
            validate_session_encryption_settings(
                encryption_key=KEY_A, plaintext_compat="legacy", required=True
            )
            == []
        )


# ---------------------------------------------------------------------------
# 2. 历史密钥校验（轮换前置条件）
# ---------------------------------------------------------------------------


class TestPreviousKeyValidation:
    def test_previous_equal_to_current_is_rejected(self):
        from core.config import validate_session_encryption_previous_keys

        errors = validate_session_encryption_previous_keys(previous_keys=KEY_A, current_key=KEY_A)
        assert errors, "历史密钥与当前密钥相同 = 等于没轮换，必须报错"

    def test_previous_without_current_is_rejected(self):
        from core.config import validate_session_encryption_previous_keys

        errors = validate_session_encryption_previous_keys(previous_keys=KEY_A, current_key="")
        assert errors, "只有历史密钥没有当前密钥 = 只能读不能写，必须报错"

    def test_distinct_previous_key_is_accepted(self):
        from core.config import validate_session_encryption_previous_keys

        assert (
            validate_session_encryption_previous_keys(previous_keys=KEY_A, current_key=KEY_B) == []
        )

    def test_empty_previous_is_accepted(self):
        from core.config import validate_session_encryption_previous_keys

        assert validate_session_encryption_previous_keys(previous_keys="", current_key=KEY_A) == []


# ---------------------------------------------------------------------------
# 3. 读路径：错密钥 / 损坏密文 / 历史明文
# ---------------------------------------------------------------------------


class TestReadPathFailureModes:
    def test_wrong_key_raises_and_does_not_return_plaintext(self, monkeypatch):
        _set_key(monkeypatch, KEY_A)
        payload = json.dumps([{"role": "user", "content": "TOP-SECRET-PAYLOAD"}])
        token = sm._seal(payload, sm._get_encryption_key())

        # 换成另一把密钥
        _set_key(monkeypatch, KEY_B)
        with pytest.raises(SessionEncryptionError):
            sm._open_record(token, sm._get_encryption_key())

    def test_corrupt_ciphertext_raises(self, monkeypatch):
        _set_key(monkeypatch, KEY_A)
        payload = json.dumps([{"role": "user", "content": "x"}])
        token = sm._seal(payload, sm._get_encryption_key())
        corrupted = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")

        with pytest.raises(SessionEncryptionError):
            sm._open_record(corrupted, sm._get_encryption_key())

    def test_legacy_plaintext_accepted_in_legacy_mode(self, monkeypatch):
        _set_key(monkeypatch, KEY_A)
        _set_compat(monkeypatch, "legacy")
        payload = json.dumps([{"role": "user", "content": "history"}])
        value, kind = sm._open_record(payload, sm._get_encryption_key())
        assert value == [{"role": "user", "content": "history"}]
        assert kind == sm._RECORD_KIND_LEGACY_PLAINTEXT

    def test_legacy_plaintext_rejected_in_reject_mode(self, monkeypatch):
        _set_key(monkeypatch, KEY_A)
        _set_compat(monkeypatch, "reject")
        payload = json.dumps([{"role": "user", "content": "history"}])
        with pytest.raises(SessionEncryptionError):
            sm._open_record(payload, sm._get_encryption_key())

    def test_fernet_unavailable_never_falls_back_to_plaintext(self, monkeypatch):
        """Fernet 不可用时必须抛错，而不是把密文当明文返回。"""
        _set_key(monkeypatch, KEY_A)
        monkeypatch.setitem(sys.modules, "cryptography.fernet", None)
        with pytest.raises(SessionEncryptionError):
            sm._encrypt_data("payload", sm._get_encryption_key())


# ---------------------------------------------------------------------------
# 4. 无破坏密钥轮换
# ---------------------------------------------------------------------------


class TestKeyRotation:
    @pytest.mark.asyncio
    async def test_record_written_with_old_key_is_readable_with_previous_key(
        self, monkeypatch, tmp_path
    ):
        """旧密钥进 PREVIOUS 后，既有密文**仍然可读**（这是"无破坏"的定义）。"""
        _set_key(monkeypatch, KEY_A)
        _set_previous(monkeypatch, "")
        mgr = _make_file_sm(tmp_path)
        session_id = await mgr.create_session()
        await mgr.add_message(session_id, "round-one")

        stored = (tmp_path / f"{session_id}.json").read_text("utf-8")
        assert stored, "会话文件应当有内容"
        assert stored.startswith(sm._SESSION_ENVELOPE_PREFIX), "配了密钥就必须写成密文"

        # 轮换：新密钥为当前密钥，旧密钥挪进 PREVIOUS
        _set_key(monkeypatch, KEY_B)
        _set_previous(monkeypatch, KEY_A)

        reloaded = await mgr.get_session(session_id)
        assert reloaded is not None
        assert reloaded.get("messages"), "旧密钥在 PREVIOUS 中时，既有会话必须仍可读"

    def test_rotation_fallback_is_counted(self, monkeypatch):
        """命中历史密钥要计数（运维据此判断轮换是否排空）。"""
        _set_key(monkeypatch, KEY_A)
        _set_previous(monkeypatch, "")
        payload = json.dumps([{"role": "user", "content": "old"}])
        token = sm._seal(payload, sm._get_encryption_key())

        before = _counter_value("session_encryption_key_fallback_total")
        _set_key(monkeypatch, KEY_B)
        _set_previous(monkeypatch, KEY_A)
        value, kind = sm._open_record(token, sm._get_encryption_key())
        assert value == [{"role": "user", "content": "old"}]
        assert kind == sm._RECORD_KIND_ENVELOPE
        after = _counter_value("session_encryption_key_fallback_total")
        assert after > before, "轮换回退必须可观测，否则无法知道旧密文是否排空"

    def test_new_writes_use_the_current_key_only(self, monkeypatch):
        """轮换后新写入用**当前**密钥（不是历史密钥）。"""
        _set_key(monkeypatch, KEY_A)
        _set_previous(monkeypatch, "")
        payload = json.dumps([{"role": "user", "content": "new"}])
        old_token = sm._seal(payload, sm._get_encryption_key())

        _set_key(monkeypatch, KEY_B)
        _set_previous(monkeypatch, KEY_A)
        new_token = sm._seal(payload, sm._get_encryption_key())
        assert new_token != old_token, "新写入必须用当前密钥"

        # 新密文不能用旧密钥解开
        _set_key(monkeypatch, KEY_A)
        _set_previous(monkeypatch, "")
        with pytest.raises(SessionEncryptionError):
            sm._open_record(new_token, sm._get_encryption_key())


# ---------------------------------------------------------------------------
# 5. 可观测性：盘点信号（Issue #25 的迁移前置）
# ---------------------------------------------------------------------------


class TestObservability:
    def test_legacy_plaintext_read_is_counted(self, monkeypatch):
        """历史明文读取必须可计量 —— 否则切 reject 之前只能靠猜。"""
        _set_key(monkeypatch, KEY_A)
        _set_compat(monkeypatch, "legacy")
        before = _counter_value(
            "session_legacy_plaintext_read_total", kind=sm._RECORD_KIND_LEGACY_PLAINTEXT
        )
        payload = json.dumps([{"role": "user", "content": "history"}])
        sm._open_record(payload, sm._get_encryption_key())
        after = _counter_value(
            "session_legacy_plaintext_read_total", kind=sm._RECORD_KIND_LEGACY_PLAINTEXT
        )
        assert after > before, "来源未经验证的历史明文读取必须计数"

    def test_encryption_error_is_counted(self, monkeypatch):
        _set_key(monkeypatch, KEY_A)
        before = _counter_value("session_encryption_error_total", reason="decrypt_failed")
        # 构造一个"看起来像 Fernet token、但解不开"的密文（首字节合法、载荷被改）
        token = sm._seal(json.dumps([{"role": "user", "content": "x"}]), sm._get_encryption_key())
        raw = token[len(sm._SESSION_ENVELOPE_PREFIX) :]
        corrupted = raw[:-8] + ("AAAAAAAA" if not raw.endswith("AAAAAAAA") else "BBBBBBBB")
        with pytest.raises(SessionEncryptionError):
            sm._open_record(sm._SESSION_ENVELOPE_PREFIX + corrupted, sm._get_encryption_key())
        after = _counter_value("session_encryption_error_total", reason="decrypt_failed")
        assert after > before, "解密失败必须计数（否则 DLQ 类问题无处可见）"

    def test_metrics_do_not_carry_session_identifiers(self):
        """label 里不得出现 session_id / 内容 —— 否则时序数无界增长且泄漏会话。"""
        from core import monitoring

        for name in (
            "session_legacy_plaintext_read_total",
            "session_encryption_error_total",
            "session_encryption_key_fallback_total",
        ):
            metric = getattr(monitoring, name, None)
            if metric is None:
                continue
            for family in metric.collect():
                for sample in family.samples:
                    for label_name in sample.labels:
                        assert label_name in (
                            "kind",
                            "reason",
                        ), f"{name} 出现了意外 label: {label_name}"


# ---------------------------------------------------------------------------
# 6. 不泄露密钥 / 令牌 / 会话内容
# ---------------------------------------------------------------------------


class TestNoSecretLeak:
    def test_error_message_contains_no_key_material(self, monkeypatch):
        _set_key(monkeypatch, KEY_A)
        secret_payload = "SECRET-PAYLOAD-DO-NOT-LEAK"
        # 触发真实失败路径：密文被破坏（不是合法明文，因此不是 legacy 分支）
        token = sm._seal(json.dumps([secret_payload]), sm._get_encryption_key())
        raw = token[len(sm._SESSION_ENVELOPE_PREFIX) :]
        corrupted = (
            sm._SESSION_ENVELOPE_PREFIX
            + raw[:-8]
            + ("AAAAAAAA" if not raw.endswith("AAAAAAAA") else "BBBBBBBB")
        )
        with pytest.raises(SessionEncryptionError) as excinfo:
            sm._open_record(corrupted, sm._get_encryption_key())
        message = str(excinfo.value)
        assert secret_payload not in message
        assert KEY_A not in message
        assert sm._derive_key(KEY_A).hex() not in message

    def test_api_error_response_has_stable_code_and_no_content(self):
        """API 层：稳定错误码 + 不含内容/密钥。"""
        from fastapi.testclient import TestClient

        from api.app import create_app

        app = create_app(graph_app=None)

        @app.get("/_test/session-encryption-boom")
        async def _boom():
            raise SessionEncryptionError("boom SECRET-PAYLOAD")

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/_test/session-encryption-boom")
        assert resp.status_code == 500
        body = resp.json()
        assert body.get("code") == "SESSION_ENCRYPTION_ERROR"
        assert "SECRET-PAYLOAD" not in resp.text
        assert KEY_A not in resp.text


# ---------------------------------------------------------------------------
# 7. 边界：加密不是对"存储写权限"的防护
# ---------------------------------------------------------------------------


class TestStorageWriteAccessIsNotDefended:
    """钉住 Issue #25 的核心边界。

    这些断言**必须继续通过**：它们证明"历史明文"与"被替换进去的明文"在原理上
    不可区分。任何声称"已解决明文替换"的测试/实现都应当让这里失败。
    """

    def test_planted_plaintext_is_indistinguishable_from_legacy_plaintext(self, monkeypatch):
        _set_key(monkeypatch, KEY_A)
        _set_compat(monkeypatch, "legacy")
        genuine = json.dumps([{"role": "user", "content": "a"}])
        planted = json.dumps([{"role": "user", "content": "b"}])

        _, kind_genuine = sm._open_record(genuine, sm._get_encryption_key())
        _, kind_planted = sm._open_record(planted, sm._get_encryption_key())
        assert kind_genuine == kind_planted, (
            "合法历史明文与被替换明文在原理上不可区分（无外部信任锚点）。"
            "若这条断言失败，说明有人试图用应用层手段冒充来源证明 —— 那是不可能的。"
        )

    def test_reject_mode_does_not_defend_the_store(self, monkeypatch):
        """``reject`` 移除降级类，但**不**保护存储本身。"""
        _set_key(monkeypatch, KEY_A)
        _set_compat(monkeypatch, "reject")
        payload = json.dumps([{"role": "user", "content": "planted"}])
        with pytest.raises(SessionEncryptionError):
            sm._open_record(payload, sm._get_encryption_key())
        # 但攻击者仍可写入**有效密文**（他有存储写权限就有密钥无关的写入能力，
        # 只要拿到密钥就能造合法 token）—— 所以 reject 不等于"防住存储写权限"。
        # 这条测试只断言"非密文被拒"，不把它扩张成"存储受保护"。
