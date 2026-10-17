"""A4：超时/租约交叉不变量（可执行配置表）。

这些约束区分两类：

- **正确性约束**（不满足 -> 拒绝启动）：违反会在运行时产生**不确定的重复执行**或
  锁丢失；
- **建议**（不满足 -> 只警告）：只影响恢复延迟或运维偏好。

刻意**不**要求 ``lease > task_time_limit``：周期续租的架构里那不是正确性问题，
一刀切要求会把正常的长任务配置判为非法。证明条件是「两次续租机会都在过期之前」
（``lease >= 2 * heartbeat``）。

本文件是 ``docs/reference/runtime-state-convergence.md`` 第 4.1 节那张表的**可执行
镜像**：改了表就必须改这里，反之亦然。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.config import (
    timeout_invariant_recommendations,
    validate_timeout_invariants,
)

#: 仓库根目录。用它解析文档/源码路径：测试可能被从任意 cwd 启动，
#: 相对路径会在那种情况下静默指向不存在的文件（表现为「文档缺内容」的假失败）。
PROJECT_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.unit

#: 仓库出厂默认（``core/config.py``），必须全部满足不变量
DEFAULTS = dict(
    lease_seconds=180.0,
    heartbeat_seconds=30.0,
    task_time_limit_seconds=180.0,
    visibility_timeout_seconds=3600.0,
    tool_timeout_seconds=30.0,
    thread_lock_ttl_seconds=300.0,
    lock_enabled=True,
)


class TestShippedDefaults:
    def test_defaults_satisfy_every_correctness_invariant(self):
        assert validate_timeout_invariants(**DEFAULTS) == []

    def test_defaults_produce_no_recommendation_noise(self):
        """出厂默认不该刷警告，否则告警会被忽略。"""
        assert (
            timeout_invariant_recommendations(
                lease_seconds=DEFAULTS["lease_seconds"],
                task_time_limit_seconds=DEFAULTS["task_time_limit_seconds"],
            )
            == []
        )

    def test_docs_and_validation_agree_on_the_invariant_table(self):
        """不变量表必须覆盖校验函数实际强制的每一条约束。

        做法不是匹配散文片段（太脆），而是**用校验函数自己的报错标识**去比对文档：
        校验里出现的每个约束标识都必须在文档表里出现，否则文档就会落后于代码——
        这正是本仓库反复出现的那类漂移。
        """
        text = (PROJECT_ROOT / "docs/reference/runtime-state-convergence.md").read_text(
            encoding="utf-8"
        )

        # 每条致命约束都配一个「必定触发失败」的探针配置
        probes = {
            "lease": dict(lease_seconds=20.0, heartbeat_seconds=30.0),
            "visibility": dict(visibility_timeout_seconds=1.0),
            "claim ttl": dict(tool_timeout_seconds=1000.0, claim_ttl_seconds=1.0),
            "lock": dict(thread_lock_ttl_seconds=1.0),
        }
        for label, kw in probes.items():
            problems = validate_timeout_invariants(**{**DEFAULTS, **kw})
            assert problems, f"{label}: 探针配置应当触发行使不变量的失败"
            assert any(
                label.upper() in p.upper() for p in problems
            ), f"{label}: 校验未给出可识别的报错，文档无法与之对齐: {problems}"

        for needle in (
            "AGENT_RUN_LEASE_SECONDS",
            "AGENT_RUN_HEARTBEAT_SECONDS",
            "AGENT_RUN_TASK_TIME_LIMIT",
            "AGENT_RUN_VISIBILITY_TIMEOUT",
            "LOCK_TTL",
            "claim TTL",
            "validate_timeout_invariants",
        ):
            assert needle in text, f"不变量表缺少 {needle}"


class TestLeaseHeartbeat:
    def test_heartbeat_must_be_smaller_than_lease(self):
        problems = validate_timeout_invariants(**{**DEFAULTS, "heartbeat_seconds": 200.0})
        assert any("HEARTBEAT" in p for p in problems)

    def test_single_renewal_opportunity_is_rejected(self):
        """只有一次续租机会 = 无法证明长执行不会因抖动丢租约。"""
        problems = validate_timeout_invariants(**{**DEFAULTS, "lease_seconds": 45.0})
        assert any("两倍" in p or "HEARTBEAT" in p for p in problems)

    def test_two_renewal_opportunities_are_enough(self):
        """lease == 2 * heartbeat 可接受：不需要 lease 大于任务时长。"""
        assert (
            validate_timeout_invariants(
                **{
                    **DEFAULTS,
                    "lease_seconds": 60.0,
                    "heartbeat_seconds": 30.0,
                    "task_time_limit_seconds": 180.0,
                }
            )
            == []
        )

    def test_lease_need_not_exceed_task_duration_when_renewal_is_on(self):
        """有续租时 lease < task_duration 是合法配置，不应被一刀切拒绝。"""
        assert (
            validate_timeout_invariants(
                **{
                    **DEFAULTS,
                    "lease_seconds": 60.0,
                    "heartbeat_seconds": 20.0,
                    "task_time_limit_seconds": 180.0,
                }
            )
            == []
        )


class TestVisibility:
    def test_visibility_must_cover_the_longest_execution(self):
        problems = validate_timeout_invariants(**{**DEFAULTS, "visibility_timeout_seconds": 120.0})
        assert any("VISIBILITY" in p for p in problems)

    def test_task_limit_can_be_disabled_which_skips_the_check(self):
        """显式关闭 task time limit -> 不再用它推导可见性下界。"""
        assert (
            validate_timeout_invariants(
                **{
                    **DEFAULTS,
                    "task_time_limit_enabled": False,
                    "task_time_limit_seconds": 0.0,
                    "visibility_timeout_seconds": 1.0,
                }
            )
            == []
        )


class TestSideEffectClaimTtl:
    def test_claim_ttl_must_cover_the_tool_timeout_plus_margin(self):
        problems = validate_timeout_invariants(
            **{**DEFAULTS, "tool_timeout_seconds": 100.0, "claim_ttl_seconds": 60.0}
        )
        assert any("claim TTL" in p for p in problems)

    def test_claim_ttl_uses_the_ledgers_own_margin(self):
        """余量必须来自 ledger 模块，不能在 config 里另写一个数字。"""
        from runtime.side_effects import CLAIM_TTL_MIN_MARGIN_SECONDS, min_claim_ttl_seconds

        floor = min_claim_ttl_seconds(100.0)
        assert floor == 100.0 + CLAIM_TTL_MIN_MARGIN_SECONDS

        ok = validate_timeout_invariants(
            **{**DEFAULTS, "tool_timeout_seconds": 100.0, "claim_ttl_seconds": floor}
        )
        assert ok == []

        bad = validate_timeout_invariants(
            **{**DEFAULTS, "tool_timeout_seconds": 100.0, "claim_ttl_seconds": floor - 0.1}
        )
        assert any("claim TTL" in p for p in bad)

    def test_raising_the_tool_timeout_makes_the_old_claim_ttl_illegal(self):
        """这正是修复前的形态：工具超时可调大到超过硬编码的 60s claim TTL。"""
        from runtime.side_effects import DEFAULT_CLAIM_TTL_SECONDS

        assert (
            validate_timeout_invariants(
                **{
                    **DEFAULTS,
                    "tool_timeout_seconds": DEFAULT_CLAIM_TTL_SECONDS,
                    "claim_ttl_seconds": DEFAULT_CLAIM_TTL_SECONDS,
                }
            )
            != []
        )


class TestThreadLockTtl:
    def test_lock_ttl_must_cover_the_task(self):
        problems = validate_timeout_invariants(**{**DEFAULTS, "thread_lock_ttl_seconds": 200.0})
        assert any("LOCK_TTL" in p for p in problems)

    def test_check_is_skipped_when_the_lock_is_disabled(self):
        assert (
            validate_timeout_invariants(
                **{
                    **DEFAULTS,
                    "thread_lock_ttl_seconds": 1.0,
                    "lock_enabled": False,
                }
            )
            == []
        )


class TestRecommendationsAreNotFatal:
    def test_disabled_heartbeat_with_short_lease_is_only_a_warning(self):
        """心跳停用 + lease < 任务时长是**建议**，不是正确性违规。"""
        assert (
            validate_timeout_invariants(
                **{
                    **DEFAULTS,
                    "lease_seconds": 60.0,
                    "heartbeat_seconds": 0.0,
                }
            )
            == []
        )
        recs = timeout_invariant_recommendations(
            lease_seconds=60.0,
            task_time_limit_seconds=180.0,
            heartbeat_enabled=False,
        )
        assert recs and "建议" in recs[0]

    def test_zero_valued_dials_are_treated_as_explicitly_disabled(self):
        """<= 0 视为关闭：不能因为「关了某个旋钮」就报一堆无关错误。"""
        assert (
            validate_timeout_invariants(
                **{
                    **DEFAULTS,
                    "heartbeat_seconds": 0.0,
                    "visibility_timeout_seconds": 0.0,
                    "tool_timeout_seconds": 0.0,
                    "thread_lock_ttl_seconds": 0.0,
                }
            )
            == []
        )


class TestValidationRunsInEveryEnvironment:
    def test_startup_validation_is_not_gated_on_dev_mode(self):
        """不变量必须在 DEV_MODE 下也强制。

        违反会产生不确定的重复执行，与「是不是生产」无关；开发机放过它等于让集成
        测试拿到虚假的安全性。
        """
        src = (PROJECT_ROOT / "core/config.py").read_text(encoding="utf-8")
        start = src.index("def validate_required_config():")
        body = src[start : start + 6000]
        inv_at = body.index("validate_timeout_invariants(")
        dev_return = body.index("return  # 非生产环境的开发模式跳过后续校验")
        assert inv_at < dev_return, "超时校验必须早于 DEV_MODE 早退"
