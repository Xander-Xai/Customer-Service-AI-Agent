"""会话数据加密 fail-closed 行为测试（H-2）。

背景：``core/session/session_manager.py`` 的 ``_encrypt_data`` / ``_decrypt_data``
曾在 ``ImportError`` / 任意异常时**返回原文**，因此当 ``SESSION_ENCRYPTION_KEY``
已配置但 Fernet 不可用（或密文损坏）时，会把明文当作"加密成功"写入、或把损坏的
密文当明文返回。本文件锁定修复后的语义：

* 未配置 key           -> 明文读写（既有、明确设计的兼容行为）；
* 已配置 key + 加密失败 -> 抛 ``SessionEncryptionError``，**绝不写明文**；
* 已配置 key + 密文解密失败（坏数据/错误密钥）-> 抛 ``SessionEncryptionError``；
* 已配置 key + 历史明文（非 Fernet token）-> 原样返回，兼容启用加密前的数据。

其中"Fernet 导入失败"用 ``sys.modules['cryptography.fernet']=None`` 做真实故障
注入（Python 对 ``None`` 条目会抛 ImportError），而不是打桩业务函数。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from core.exceptions import SessionEncryptionError
from core.session import session_manager as sm


def _set_key(monkeypatch, value: str) -> bytes | None:
    """把 SESSION_ENCRYPTION_KEY 设为 ``value``（空串 = 未配置）。"""
    monkeypatch.delenv("SESSION_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr("core.config.SESSION_ENCRYPTION_KEY", value)
    return sm._get_encryption_key()


def _break_fernet(monkeypatch) -> None:
    """故障注入：让 ``from cryptography.fernet import Fernet`` 抛 ImportError。"""
    monkeypatch.setitem(sys.modules, "cryptography.fernet", None)


def _make_file_sm(tmp_path) -> sm.EnhancedSessionManager:
    return sm.EnhancedSessionManager(storage_backend="file", storage_dir=str(tmp_path))


class _FakeRedis:
    """最小 Redis 替身（仅为 file/redis 后端加解密路径提供 get/set/delete）。"""

    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):  # noqa: A002 - 对齐 redis API
        self.store[key] = value
        return True

    def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)

    def ping(self):
        return True


# ---------------------------------------------------------------------------
# 基础原语
# ---------------------------------------------------------------------------


class TestEncryptionPrimitives:
    def test_no_key_is_plaintext_passthrough(self):
        """场景 1：未配置 key 时保持既有明文兼容行为。"""
        assert sm._encrypt_data("hello 世界", None) == "hello 世界"
        assert sm._decrypt_data("hello 世界", None) == "hello 世界"

    def test_roundtrip_and_ciphertext_is_fernet_token(self, monkeypatch):
        """场景 2：有效 key 下加解密往返，且密文不泄露明文。"""
        key = _set_key(monkeypatch, "unit-test-key-material")
        assert key is not None
        plaintext = json.dumps([{"role": "user", "content": "敏感会话内容"}], ensure_ascii=False)

        ciphertext = sm._encrypt_data(plaintext, key)

        assert ciphertext != plaintext
        assert "敏感会话内容" not in ciphertext
        assert sm._is_fernet_token(ciphertext)
        assert sm._decrypt_data(ciphertext, key) == plaintext

    def test_legacy_plaintext_is_not_a_fernet_token(self, monkeypatch):
        """场景 5：历史明文（JSON）必须可判定地"不是密文"。"""
        _set_key(monkeypatch, "unit-test-key-material")
        legacy = json.dumps([{"role": "user", "content": "old plaintext"}], ensure_ascii=False)
        assert sm._is_fernet_token(legacy) is False
        key = sm._get_encryption_key()
        # 已配置 key 仍能原样读取历史明文（兼容），不会被当成损坏密文。
        assert sm._decrypt_data(legacy, key) == legacy

    def test_encrypt_fails_closed_when_fernet_unavailable(self, monkeypatch):
        """场景 2：key 已配置但 Fernet 不可用 -> 明确抛错，不返回明文。"""
        key = _set_key(monkeypatch, "unit-test-key-material")
        _break_fernet(monkeypatch)
        with pytest.raises(SessionEncryptionError):
            sm._encrypt_data("must-not-be-returned", key)

    def test_decrypt_ciphertext_fails_closed_when_fernet_unavailable(self, monkeypatch):
        """场景 2：key 已配置、密文存在但 Fernet 不可用 -> 明确抛错。"""
        key = _set_key(monkeypatch, "unit-test-key-material")
        ciphertext = sm._encrypt_data("payload", key)
        _break_fernet(monkeypatch)
        with pytest.raises(SessionEncryptionError):
            sm._decrypt_data(ciphertext, key)

    def test_decrypt_corrupt_token_raises(self, monkeypatch):
        """场景 4：密文损坏/认证失败 -> 抛错，绝不返回损坏数据当明文。"""
        key = _set_key(monkeypatch, "unit-test-key-material")
        ciphertext = sm._encrypt_data("payload", key)
        # 翻转中间一个字符，破坏 HMAC（保留首字节 0x80，仍是可识别的 Fernet token）。
        mid = len(ciphertext) // 2
        tampered = (
            ciphertext[:mid] + ("A" if ciphertext[mid] != "A" else "B") + ciphertext[mid + 1 :]
        )
        assert sm._is_fernet_token(tampered)
        with pytest.raises(SessionEncryptionError):
            sm._decrypt_data(tampered, key)

    def test_decrypt_wrong_key_raises(self, monkeypatch):
        """场景 4：错误密钥 -> 抛错，不返回密文/明文。"""
        key_a = _set_key(monkeypatch, "key-A")
        key_b = _set_key(monkeypatch, "key-B")
        assert key_a != key_b
        ciphertext = sm._encrypt_data("payload", key_a)
        with pytest.raises(SessionEncryptionError):
            sm._decrypt_data(ciphertext, key_b)


# ---------------------------------------------------------------------------
# 文件后端
# ---------------------------------------------------------------------------


class TestFileBackendFailClosed:
    def test_save_writes_ciphertext_then_loads_back(self, tmp_path, monkeypatch):
        """场景 2/5：file 后端落盘是密文；重新加载能还原。"""
        key = _set_key(monkeypatch, "unit-test-key-material")
        assert key is not None
        instance = _make_file_sm(tmp_path)
        messages = [{"role": "user", "content": "信用卡 4111-1111-1111-1111"}]

        instance._save_to_file("sess-a", messages)

        fp = os.path.join(str(tmp_path), "sess-a.json")
        with open(fp, encoding="utf-8") as f:
            on_disk = f.read()
        assert "4111" not in on_disk
        assert sm._is_fernet_token(on_disk)
        assert instance._create_memory_backend("sess-a") == messages

    def test_save_fails_closed_when_fernet_unavailable(self, tmp_path, monkeypatch):
        """场景 2/3：加密不可用时拒绝写盘，且**不产生明文文件**。"""
        _set_key(monkeypatch, "unit-test-key-material")
        instance = _make_file_sm(tmp_path)
        _break_fernet(monkeypatch)

        with pytest.raises(SessionEncryptionError):
            instance._save_to_file("sess-b", [{"role": "user", "content": "secret"}])

        fp = os.path.join(str(tmp_path), "sess-b.json")
        assert not os.path.exists(fp), "fail-closed 被破坏：加密失败却写出了文件"

    def test_load_legacy_plaintext_with_key(self, tmp_path, monkeypatch):
        """场景 5：启用加密前写入的明文文件仍可读取（兼容）。"""
        _set_key(monkeypatch, "unit-test-key-material")
        instance = _make_file_sm(tmp_path)
        legacy_messages = [{"role": "user", "content": "legacy plaintext"}]
        with open(os.path.join(str(tmp_path), "sess-c.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps(legacy_messages, ensure_ascii=False))

        assert instance._create_memory_backend("sess-c") == legacy_messages

    def test_load_ciphertext_with_wrong_key_raises(self, tmp_path, monkeypatch):
        """场景 4：用错误密钥读取密文 -> 明确抛错，不静默当空会话。"""
        _set_key(monkeypatch, "key-A")
        instance = _make_file_sm(tmp_path)
        instance._save_to_file("sess-d", [{"role": "user", "content": "x"}])

        # 换一个不同密钥再加载。
        _set_key(monkeypatch, "key-B")
        with pytest.raises(SessionEncryptionError):
            instance._create_memory_backend("sess-d")

    @pytest.mark.asyncio
    async def test_add_message_propagates_failure_fail_closed(self, tmp_path, monkeypatch):
        """场景 6：上层存储 API 不得把加密失败吞成"保存成功"。"""
        _set_key(monkeypatch, "unit-test-key-material")
        instance = _make_file_sm(tmp_path)
        _break_fernet(monkeypatch)

        with pytest.raises(SessionEncryptionError):
            await instance.add_message("sess-e", "hello")
        assert not os.path.exists(os.path.join(str(tmp_path), "sess-e.json"))


# ---------------------------------------------------------------------------
# Redis 后端
# ---------------------------------------------------------------------------


class TestRedisBackendFailClosed:
    @staticmethod
    def _sm_with_fake_redis():
        instance = sm.EnhancedSessionManager(storage_backend="redis")
        fake = _FakeRedis()
        instance._get_redis = lambda: fake  # type: ignore[method-assign]
        return instance, fake

    @pytest.mark.asyncio
    async def test_redis_save_writes_ciphertext_for_values_and_meta(self, monkeypatch):
        _set_key(monkeypatch, "unit-test-key-material")
        instance, fake = self._sm_with_fake_redis()

        await instance.add_message("sess-r", "敏感内容 1234")

        values = [v for k, v in fake.store.items() if k.endswith((":messages", ":meta"))]
        assert values, "Redis 未写入任何会话数据"
        for value in values:
            assert "敏感内容" not in value
            assert sm._is_fernet_token(value)

    @pytest.mark.asyncio
    async def test_redis_load_roundtrip(self, monkeypatch):
        _set_key(monkeypatch, "unit-test-key-material")
        instance, fake = self._sm_with_fake_redis()
        await instance.add_message("sess-rl", "hello redis")

        # 新建实例（空内存），从同一 Redis 数据恢复会话。
        instance2, fake2 = self._sm_with_fake_redis()
        fake2.store = fake.store
        sid = await instance2.create_session("sess-rl")
        assert sid == "sess-rl"
        assert instance2.sessions["sess-rl"]["messages"] == [
            {"role": "user", "content": "hello redis"}
        ]

    @pytest.mark.asyncio
    async def test_redis_load_corrupt_ciphertext_raises(self, monkeypatch):
        _set_key(monkeypatch, "unit-test-key-material")
        instance, fake = self._sm_with_fake_redis()
        await instance.add_message("sess-rc", "payload")

        # 破坏 messages 密文（中间字符，保持 Fernet token 可识别）。
        msg_key = next(k for k in fake.store if k.endswith(":messages"))
        ct = fake.store[msg_key]
        mid = len(ct) // 2
        fake.store[msg_key] = ct[:mid] + ("A" if ct[mid] != "A" else "B") + ct[mid + 1 :]

        instance2, fake2 = self._sm_with_fake_redis()
        fake2.store = fake.store
        with pytest.raises(SessionEncryptionError):
            await instance2.create_session("sess-rc")

    @pytest.mark.asyncio
    async def test_redis_save_fails_closed_when_fernet_unavailable(self, monkeypatch):
        _set_key(monkeypatch, "unit-test-key-material")
        instance, fake = self._sm_with_fake_redis()
        _break_fernet(monkeypatch)

        with pytest.raises(SessionEncryptionError):
            await instance.add_message("sess-rf", "secret")
        assert not [v for k, v in fake.store.items() if k.endswith((":messages", ":meta"))]
