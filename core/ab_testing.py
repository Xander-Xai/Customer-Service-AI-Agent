"""
A/B 测试框架（v4.1）
支持不同 Prompt 策略的效果对比。
基于 user_id 哈希确定性分配变体，确保同一用户始终看到同一变体。
"""

import hashlib
import time
from typing import Any, Dict, List, Optional

from logger import get_logger

logger = get_logger("core.ab_testing")


class ABTestManager:
    """A/B 测试管理器"""

    def __init__(self):
        self.experiments: dict[str, dict] = {}

    def create_experiment(
        self,
        name: str,
        variants: list[str],
        traffic_split: list[float] | None = None,
        description: str = "",
    ):
        """
        创建实验

        Args:
            name: 实验名称（唯一标识）
            variants: 变体列表，如 ["control", "variant_a", "variant_b"]
            traffic_split: 流量分配比例，如 [0.5, 0.3, 0.2]
                          默认均匀分配
            description: 实验描述

        Raises:
            ValueError: 变体列表为空或流量比例不合法
        """
        if not variants:
            raise ValueError("variants 不能为空")
        if len(variants) < 2:
            raise ValueError("至少需要 2 个变体进行对比")

        # 默认均匀分配
        if traffic_split is None:
            traffic_split = [1.0 / len(variants)] * len(variants)

        if len(traffic_split) != len(variants):
            raise ValueError(
                f"traffic_split 长度 ({len(traffic_split)}) "
                f"必须与 variants 长度 ({len(variants)}) 一致"
            )

        # 验证比例之和为 1.0（允许浮点误差）
        total = sum(traffic_split)
        if abs(total - 1.0) > 0.01:
            raise ValueError(f"traffic_split 之和应为 1.0，当前为 {total:.3f}")

        for i, pct in enumerate(traffic_split):
            if pct < 0 or pct > 1:
                raise ValueError(f"traffic_split[{i}] = {pct} 不在 [0, 1] 范围内")

        # 构建累积分配表（用于哈希映射）
        cumulative = []
        running = 0.0
        for pct in traffic_split:
            running += pct
            cumulative.append(running)

        self.experiments[name] = {
            "name": name,
            "variants": variants,
            "traffic_split": traffic_split,
            "cumulative_split": cumulative,
            "description": description,
            "created_at": time.time(),
            "active": True,
            # 指标存储: {variant: {metric_name: [values]}}
            "metrics": {v: {} for v in variants},
            # 变体分配缓存: {user_id: variant}
            "assignments": {},
        }

        logger.info(f"实验创建: name={name} variants={variants} split={traffic_split}")

    def assign_variant(self, experiment_name: str, user_id: str) -> str:
        """
        为用户分配实验变体
        基于 user_id + experiment_name 哈希，确保：
        1. 同一用户在同一实验中始终看到同一变体
        2. 不同用户均匀分布到各变体
        3. 分配结果可复现（确定性）

        Args:
            experiment_name: 实验名称
            user_id: 用户标识

        Returns:
            分配到的变体名称

        Raises:
            KeyError: 实验不存在
        """
        exp = self.experiments.get(experiment_name)
        if not exp:
            raise KeyError(f"实验不存在: {experiment_name}")
        if not exp["active"]:
            # 实验不活跃时返回第一个变体（control）
            return exp["variants"][0]

        # 检查缓存
        cache_key = f"{experiment_name}:{user_id}"
        if cache_key in exp["assignments"]:
            return exp["assignments"][cache_key]

        # 确定性哈希分配
        hash_input = f"{experiment_name}:{user_id}".encode()
        hash_hex = hashlib.sha256(hash_input).hexdigest()
        # 取前 8 位十六进制转整数，映射到 [0, 1)
        hash_int = int(hash_hex[:8], 16)
        bucket = hash_int / 0xFFFFFFFF  # 归一化到 [0, 1)

        # 根据累积分配表选择变体
        assigned = exp["variants"][-1]  # 默认最后一个
        for i, cum in enumerate(exp["cumulative_split"]):
            if bucket < cum:
                assigned = exp["variants"][i]
                break

        # 缓存分配结果
        exp["assignments"][cache_key] = assigned
        logger.debug(
            f"变体分配: experiment={experiment_name} user={user_id[:8]}... "
            f"-> {assigned} (bucket={bucket:.4f})"
        )
        return assigned

    def record_metric(
        self,
        experiment_name: str,
        variant: str,
        metric_name: str,
        value: float,
    ):
        """
        记录实验指标

        Args:
            experiment_name: 实验名称
            variant: 变体名称
            metric_name: 指标名称（如 "satisfaction_score", "response_time"）
            value: 指标值
        """
        exp = self.experiments.get(experiment_name)
        if not exp:
            logger.warning(f"记录指标失败: 实验 {experiment_name} 不存在")
            return

        if variant not in exp["metrics"]:
            logger.warning(f"记录指标失败: 变体 {variant} 不在实验 {experiment_name} 中")
            return

        if metric_name not in exp["metrics"][variant]:
            exp["metrics"][variant][metric_name] = []

        exp["metrics"][variant][metric_name].append(
            {
                "value": value,
                "timestamp": time.time(),
            }
        )

        logger.debug(
            f"指标记录: experiment={experiment_name} variant={variant} "
            f"metric={metric_name} value={value}"
        )

    def get_results(self, experiment_name: str) -> dict:
        """
        获取实验结果

        Args:
            experiment_name: 实验名称

        Returns:
            {
                "experiment": 名称,
                "active": 是否活跃,
                "variants": {
                    variant_name: {
                        "sample_size": 样本数,
                        "metrics": {
                            metric_name: {
                                "count": 数量,
                                "mean": 平均值,
                                "min": 最小值,
                                "max": 最大值,
                                "std": 标准差,
                            }
                        }
                    }
                },
                "assignments": 总分配数,
            }
        """
        exp = self.experiments.get(experiment_name)
        if not exp:
            return {"error": f"实验不存在: {experiment_name}"}

        variant_results = {}
        for variant in exp["variants"]:
            variant_metrics = {}
            total_samples = 0

            for metric_name, values in exp["metrics"].get(variant, {}).items():
                if not values:
                    continue

                nums = [v["value"] for v in values]
                count = len(nums)
                total_samples = max(total_samples, count)
                mean_val = sum(nums) / count
                min_val = min(nums)
                max_val = max(nums)
                std_val = (
                    (sum((x - mean_val) ** 2 for x in nums) / count) ** 0.5 if count > 1 else 0.0
                )

                variant_metrics[metric_name] = {
                    "count": count,
                    "mean": round(mean_val, 4),
                    "min": round(min_val, 4),
                    "max": round(max_val, 4),
                    "std": round(std_val, 4),
                }

            variant_results[variant] = {
                "sample_size": total_samples,
                "metrics": variant_metrics,
            }

        return {
            "experiment": experiment_name,
            "active": exp["active"],
            "description": exp.get("description", ""),
            "created_at": exp["created_at"],
            "variants": variant_results,
            "total_assignments": len(exp["assignments"]),
        }

    def stop_experiment(self, experiment_name: str):
        """停止实验"""
        exp = self.experiments.get(experiment_name)
        if exp:
            exp["active"] = False
            logger.info(f"实验停止: {experiment_name}")

    def delete_experiment(self, experiment_name: str):
        """删除实验"""
        if experiment_name in self.experiments:
            del self.experiments[experiment_name]
            logger.info(f"实验删除: {experiment_name}")

    def list_experiments(self) -> list[dict]:
        """列出所有实验概要"""
        return [
            {
                "name": exp["name"],
                "active": exp["active"],
                "variants": exp["variants"],
                "total_assignments": len(exp["assignments"]),
                "created_at": exp["created_at"],
            }
            for exp in self.experiments.values()
        ]
