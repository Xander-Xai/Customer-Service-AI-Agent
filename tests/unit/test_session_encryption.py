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
        """场景 5：历史明文（JSON）必须可判定地"不是密文"。

        注意本测试**只**断言启发式本身。启发式返回 False 并不等于"这东西是可信明文"
        —— 见 :class:`TestFormatDiscrimination`，破坏密文同样会得到 False。
        明文兼容在记录层（``_open_record``）显式判定，见
        :meth:`TestFileBackendFailClosed.test_load_legacy_plaintext_with_key`。
        """
        _set_key(monkeypatch, "unit-test-key-material")
        legacy = json.dumps([{"role": "user", "content": "old plaintext"}], ensure_ascii=False)
        assert sm._is_fernet_token(legacy) is False

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
        assert on_disk.startswith(sm._SESSION_ENVELOPE_PREFIX)
        assert sm._is_fernet_token(on_disk[len(sm._SESSION_ENVELOPE_PREFIX) :])
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
            assert value.startswith(sm._SESSION_ENVELOPE_PREFIX)
            assert sm._is_fernet_token(value[len(sm._SESSION_ENVELOPE_PREFIX) :])

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


# ---------------------------------------------------------------------------
# 格式判别（format discrimination）：本轮复审的核心
#
# 旧实现的判据是**否定式**的："看起来不像 Fernet token => 当作可信历史明文"。
# 复审复现了这条判据被绕过的方式：任何**同时**破坏 Fernet 形态的损坏（首字符
# g->h、非法 base64、整条替换成 JSON）都会"降级"成明文 —— 损坏得越多越被信任，
# 反单调，并直接抵消了 PR #23 的 fail-closed 修复。
#
# 修复：版本标记 envelope（``csae1:``）+ 显式三分类（``_open_record``）。
# ---------------------------------------------------------------------------


def _set_compat(monkeypatch, mode: str) -> None:
    monkeypatch.setattr("core.config.SESSION_ENCRYPTION_PLAINTEXT_COMPAT", mode)


def _flip_first_char(token: str) -> str:
    return ("h" if token[0] != "h" else "i") + token[1:]


def _token_of(record: str) -> str:
    assert record.startswith(sm._SESSION_ENVELOPE_PREFIX), record[:16]
    return record[len(sm._SESSION_ENVELOPE_PREFIX) :]


class TestFormatDiscrimination:
    """带标记 envelope：token 本体被破坏一律 fail closed（不降级为明文）。"""

    def _sealed(self, monkeypatch) -> tuple[str, bytes]:
        key = _set_key(monkeypatch, "unit-test-key-material")
        assert key is not None
        record = sm._seal(json.dumps([{"role": "user", "content": "x"}]), key)
        assert record.startswith(sm._SESSION_ENVELOPE_PREFIX)
        return record, key

    def test_envelope_first_char_corruption_is_rejected(self, monkeypatch):
        """回归：token 首字符 g->h。旧实现把它当明文原样返回（绕过）。"""
        record, key = self._sealed(monkeypatch)
        tampered = sm._SESSION_ENVELOPE_PREFIX + _flip_first_char(_token_of(record))

        # 关键断言：这个损坏**同时**骗过了启发式 —— 正是绕过成立的原因。
        assert sm._is_fernet_token(_token_of(tampered)) is False

        with pytest.raises(SessionEncryptionError):
            sm._open_record(tampered, key)

    def test_envelope_truncation_is_rejected(self, monkeypatch):
        """回归：token 截断。"""
        record, key = self._sealed(monkeypatch)
        with pytest.raises(SessionEncryptionError):
            sm._open_record(record[:-8], key)

    def test_envelope_invalid_base64_is_rejected(self, monkeypatch):
        """回归：破坏 base64（非法字符）。旧实现会当成明文返回。"""
        record, key = self._sealed(monkeypatch)
        broken = record[:-8] + "!!!!!!!!"
        assert sm._is_fernet_token(_token_of(broken)) is False
        with pytest.raises(SessionEncryptionError):
            sm._open_record(broken, key)

    def test_envelope_wrong_key_is_rejected(self, monkeypatch):
        """不同密钥 -> 解密失败，不是明文。"""
        record, _ = self._sealed(monkeypatch)
        _set_key(monkeypatch, "a-completely-different-key")
        with pytest.raises(SessionEncryptionError):
            sm._open_record(record, sm._get_encryption_key())

    def test_envelope_roundtrip_still_works(self, monkeypatch):
        """加固不能误伤：本版本写入的记录必须能正常读回。"""
        _set_key(monkeypatch, "unit-test-key-material")
        messages = [{"role": "user", "content": "roundtrip"}]
        record = sm._seal(json.dumps(messages, ensure_ascii=False), sm._get_encryption_key())
        value, kind = sm._open_record(record, sm._get_encryption_key())
        assert value == messages
        assert kind == sm._RECORD_KIND_ENVELOPE

    def test_legacy_unmarked_ciphertext_still_loads(self, monkeypatch):
        """对旧版**无标记** Fernet token 的兼容：必须仍可读。"""
        key = _set_key(monkeypatch, "unit-test-key-material")
        assert key is not None
        messages = [{"role": "user", "content": "written by an older version"}]
        unmarked = sm._encrypt_data(json.dumps(messages, ensure_ascii=False), key)
        assert not unmarked.startswith(sm._SESSION_ENVELOPE_PREFIX)

        value, kind = sm._open_record(unmarked, key)
        assert value == messages
        assert kind == sm._RECORD_KIND_LEGACY_TOKEN

    def test_decrypt_data_no_longer_passes_through_plaintext(self, monkeypatch):
        """``_decrypt_data`` 已收敛为纯原语：输入不是 token 就抛错，不再"原样返回"。"""
        key = _set_key(monkeypatch, "unit-test-key-material")
        assert key is not None
        with pytest.raises(SessionEncryptionError):
            sm._decrypt_data(json.dumps([{"role": "user"}]), key)


class TestPlaintextSubstitutionBoundary:
    """整条记录被替换成明文 JSON —— **不可约的边界**，必须被如实记录而非假装解决。"""

    @staticmethod
    def _planted() -> str:
        return json.dumps([{"role": "user", "content": "attacker-planted"}])

    def test_legacy_mode_cannot_distinguish_planted_plaintext(self, monkeypatch):
        """没有可信的历史格式标记或来源元数据时，合法历史明文与被替换明文**不可区分**。

        这里断言的是一个**已知且不可修复**的限制：legacy 模式下两者都会被接受。
        任何声称"已解决明文替换"的测试都是错的 —— 它需要外部信任锚点才能成立。
        """
        key = _set_key(monkeypatch, "unit-test-key-material")
        _set_compat(monkeypatch, "legacy")
        assert key is not None

        planted_value, planted_kind = sm._open_record(self._planted(), key)
        legacy_value, legacy_kind = sm._open_record(
            json.dumps([{"role": "user", "content": "genuine old"}]), key
        )

        assert planted_kind == legacy_kind == sm._RECORD_KIND_LEGACY_PLAINTEXT
        assert planted_value != legacy_value  # 内容不同，但两者都被接受且不可区分

    def test_strict_mode_rejects_planted_plaintext(self, monkeypatch):
        """严格模式（reject）：非密文记录一律拒绝 —— 这是**来源白名单**而非 JSON 语法检查。"""
        key = _set_key(monkeypatch, "unit-test-key-material")
        _set_compat(monkeypatch, "reject")
        assert key is not None
        with pytest.raises(SessionEncryptionError):
            sm._open_record(self._planted(), key)

    def test_strict_mode_still_reads_real_ciphertext(self, monkeypatch):
        """严格模式不得误伤密文记录（否则等于停服）。"""
        key = _set_key(monkeypatch, "unit-test-key-material")
        assert key is not None
        messages = [{"role": "user", "content": "real"}]
        record = sm._seal(json.dumps(messages, ensure_ascii=False), key)
        _set_compat(monkeypatch, "reject")
        value, kind = sm._open_record(record, sm._get_encryption_key())
        assert value == messages
        assert kind == sm._RECORD_KIND_ENVELOPE

    def test_invalid_compat_value_fails_closed(self, monkeypatch):
        """非法取值必须 fail closed —— 静默兜底成 legacy 就等于无声降级。"""
        key = _set_key(monkeypatch, "unit-test-key-material")
        assert key is not None
        _set_compat(monkeypatch, "rejct")
        with pytest.raises(SessionEncryptionError):
            sm._open_record(self._planted(), sm._get_encryption_key())

    def test_startup_validation_rejects_invalid_value(self):
        from core.config import validate_session_encryption_settings as validate

        assert validate(encryption_key="k", plaintext_compat="legacy") == []
        assert validate(encryption_key="k", plaintext_compat="reject") == []
        assert validate(encryption_key="k", plaintext_compat="rejct")
        assert validate(encryption_key="k", plaintext_compat="")

    def test_startup_validation_rejects_inert_reject_mode(self):
        """reject 但没有密钥 = 声称了不会生效的严格模式（同 validate_hitl_settings 原则）。"""
        from core.config import validate_session_encryption_settings as validate

        assert validate(encryption_key="", plaintext_compat="reject")
        assert validate(encryption_key="", plaintext_compat="legacy") == []


class TestLegacyCompatAndUpgrade:
    """历史兼容：不得让旧会话不可读，也不得做破坏性迁移。"""

    def test_legacy_plaintext_file_still_readable_and_upgraded(self, tmp_path, monkeypatch):
        """历史明文可读；下一次正常写入自动升级为 envelope（无需停机迁移）。"""
        _set_key(monkeypatch, "unit-test-key-material")
        _set_compat(monkeypatch, "legacy")
        instance = _make_file_sm(tmp_path)
        legacy = [{"role": "user", "content": "legacy plaintext"}]
        fp = os.path.join(str(tmp_path), "sess-up.json")
        with open(fp, "w", encoding="utf-8") as f:
            f.write(json.dumps(legacy, ensure_ascii=False))

        loaded = instance._create_memory_backend("sess-up")
        assert loaded == legacy, "历史明文不可读 = 破坏性兼容回退"

        instance._save_to_file("sess-up", loaded)
        with open(fp, encoding="utf-8") as f:
            upgraded = f.read()
        assert upgraded.startswith(sm._SESSION_ENVELOPE_PREFIX)
        assert "legacy plaintext" not in upgraded
        assert instance._create_memory_backend("sess-up") == legacy

    def test_legacy_plaintext_redis_record_still_readable(self, monkeypatch):
        _set_key(monkeypatch, "unit-test-key-material")
        _set_compat(monkeypatch, "legacy")
        instance, fake = TestRedisBackendFailClosed._sm_with_fake_redis()
        legacy = [{"role": "user", "content": "legacy redis"}]
        fake.store["csai:session:sess-lp:messages"] = json.dumps(legacy, ensure_ascii=False)
        fake.store["csai:session:sess-lp:meta"] = json.dumps(
            {"user_id": "", "summary": ""}, ensure_ascii=False
        )

        instance2, fake2 = TestRedisBackendFailClosed._sm_with_fake_redis()
        fake2.store = fake.store
        await_id = instance2.create_session("sess-lp")
        assert await_id == "sess-lp"
        assert instance2.sessions["sess-lp"]["messages"] == legacy

    def test_strict_mode_would_break_legacy_reads(self, tmp_path, monkeypatch):
        """记录 strict 模式的**已知代价**：历史明文将不再可读。

        因此默认必须是 legacy，切换到 reject 前需要先完成迁移。
        """
        _set_key(monkeypatch, "unit-test-key-material")
        _set_compat(monkeypatch, "reject")
        instance = _make_file_sm(tmp_path)
        fp = os.path.join(str(tmp_path), "sess-legacy.json")
        with open(fp, "w", encoding="utf-8") as f:
            f.write(json.dumps([{"role": "user", "content": "legacy"}]))
        with pytest.raises(SessionEncryptionError):
            instance._create_memory_backend("sess-legacy")


class TestLoaderDoesNotSilentlyRebuildEmptySession:
    """必须能区分「拒绝损坏数据」与「普通读取失败退回空会话」。"""

    def test_corrupt_envelope_file_raises_not_empty(self, tmp_path, monkeypatch):
        _set_key(monkeypatch, "unit-test-key-material")
        instance = _make_file_sm(tmp_path)
        instance._save_to_file("sess-x", [{"role": "user", "content": "secret"}])
        fp = os.path.join(str(tmp_path), "sess-x.json")
        with open(fp, encoding="utf-8") as f:
            raw = f.read()
        with open(fp, "w", encoding="utf-8") as f:
            f.write(sm._SESSION_ENVELOPE_PREFIX + _flip_first_char(_token_of(raw)))

        # 旧实现在这里返回 []（静默空会话）。必须改成显式拒绝。
        with pytest.raises(SessionEncryptionError):
            instance._create_memory_backend("sess-x")

    def test_missing_file_is_a_normal_new_session(self, tmp_path, monkeypatch):
        """对照：记录**不存在**是正常路径，仍返回空会话。"""
        _set_key(monkeypatch, "unit-test-key-material")
        instance = _make_file_sm(tmp_path)
        assert instance._create_memory_backend("never-written") == []

    def test_io_error_is_a_normal_read_failure(self, tmp_path, monkeypatch):
        """对照：I/O 失败（权限/磁盘）属普通读取失败，保持既有降级语义，不 fail closed。"""
        _set_key(monkeypatch, "unit-test-key-material")
        instance = _make_file_sm(tmp_path)
        instance._save_to_file("sess-io", [{"role": "user", "content": "x"}])

        def _boom(*_a, **_k):
            raise OSError("permission denied")

        monkeypatch.setattr("builtins.open", _boom)
        assert instance._create_memory_backend("sess-io") == []

    def test_unparseable_non_ciphertext_file_raises(self, tmp_path, monkeypatch):
        """既非密文、也不是合法 JSON 的记录 = 损坏，拒绝静默重建。"""
        _set_key(monkeypatch, "unit-test-key-material")
        _set_compat(monkeypatch, "legacy")
        instance = _make_file_sm(tmp_path)
        with open(os.path.join(str(tmp_path), "sess-junk.json"), "w", encoding="utf-8") as f:
            f.write("<<<not json and not a token>>>")

        with pytest.raises(SessionEncryptionError):
            instance._create_memory_backend("sess-junk")

    @pytest.mark.asyncio
    async def test_redis_corrupt_envelope_raises_not_empty(self, monkeypatch):
        _set_key(monkeypatch, "unit-test-key-material")
        instance, fake = TestRedisBackendFailClosed._sm_with_fake_redis()
        await instance.add_message("sess-rx", "confidential")

        msg_key = next(k for k in fake.store if k.endswith(":messages"))
        fake.store[msg_key] = sm._SESSION_ENVELOPE_PREFIX + _flip_first_char(
            _token_of(fake.store[msg_key])
        )

        instance2, fake2 = TestRedisBackendFailClosed._sm_with_fake_redis()
        fake2.store = fake.store
        with pytest.raises(SessionEncryptionError):
            await instance2.create_session("sess-rx")
        # 关键：不得留下一个"成功重建的空会话"。
        assert "sess-rx" not in instance2.sessions

    @pytest.mark.asyncio
    async def test_redis_missing_key_is_a_normal_new_session(self, monkeypatch):
        """对照：Redis 里没有这条会话 = 正常新建。"""
        _set_key(monkeypatch, "unit-test-key-material")
        instance, _ = TestRedisBackendFailClosed._sm_with_fake_redis()
        assert await instance.create_session("never-written") == "never-written"
        assert instance.sessions["never-written"]["messages"] == []
