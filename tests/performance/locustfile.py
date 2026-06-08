"""
性能基线测试（E3 — Locust 压测脚本）
用法:
  locust -f tests/performance/locustfile.py --host=http://localhost:8000

压测场景:
  1. 轻量级：健康检查 + 缓存命中查询
  2. 中等负载：随机产品咨询
  3. 高负载：并发多轮对话

运行前：
  pip install locust
  make dev  # 启动应用
"""

import random

from locust import HttpUser, between, events, task

# 测试用例：模拟真实用户查询
TEST_QUERIES = [
    "你们有什么精华液推荐？",
    "敏感肌适合用什么面霜？",
    "玻尿酸和神经酰胺哪个保湿效果好？",
    "我想查询订单物流状态",
    "产品开封后保质期多久？",
    "如何申请退货？",
    "视黄醇和果酸可以一起用吗？",
    "油性肌肤夏天用什么护肤品？",
    "会员有什么权益？",
    "这个产品有防伪标识吗？",
    "痘痘肌怎么护理？",
    "物理防晒和化学防晒哪个好？",
    "产品过敏了怎么办？",
    "可以叠加使用优惠券吗？",
    "冬季护肤有什么建议？",
]


class HealthCheckUser(HttpUser):
    """健康检查用户（只检查系统状态）"""

    weight = 1
    wait_time = between(5, 15)

    @task(3)
    def health_check(self):
        self.client.get("/api/health")


class ChatUser(HttpUser):
    """聊天用户（模拟真实对话场景）"""

    weight = 5
    wait_time = between(2, 8)

    def on_start(self):
        """用户启动时获取 token"""
        self.token = ""
        self.session_id = ""
        self.session_token = ""
        try:
            resp = self.client.post(
                "/api/auth/login",
                json={
                    "username": "admin",
                    "password": "admin123",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                self.token = data.get("token", "")
        except Exception:
            pass

    def _get_headers(self):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @task(5)
    def chat_query(self):
        """发送聊天查询"""
        query = random.choice(TEST_QUERIES)
        payload = {
            "query": query,
            "session_id": self.session_id,
            "session_token": self.session_token,
        }
        with self.client.post(
            "/api/chat",
            json=payload,
            headers=self._get_headers(),
            catch_response=True,
            name="/api/chat [POST]",
        ) as response:
            if response.status_code == 200:
                data = response.json()
                if "response" in data and data["response"]:
                    # 更新 session 信息
                    if data.get("session_id"):
                        self.session_id = data["session_id"]
                    if data.get("session_token"):
                        self.session_token = data["session_token"]
                    response.success()
                else:
                    response.failure("Empty response")
            elif response.status_code == 429:
                response.failure("Rate limited")
            else:
                response.failure(f"HTTP {response.status_code}")

    @task(1)
    def get_metrics(self):
        """获取系统指标（模拟管理员查看）"""
        if self.token:
            self.client.get(
                "/api/metrics",
                headers={"X-Admin-Token": "dev-admin-token"},
                name="/api/metrics [GET]",
            )

    @task(1)
    def get_cache_stats(self):
        """获取缓存统计"""
        if self.token:
            self.client.get(
                "/api/cache/stats",
                headers=self._get_headers(),
                name="/api/cache/stats [GET]",
            )


class AdminUser(HttpUser):
    """管理用户（管理后台操作）"""

    weight = 1
    wait_time = between(5, 20)

    def on_start(self):
        self.token = ""
        try:
            resp = self.client.post(
                "/api/auth/login",
                json={
                    "username": "admin",
                    "password": "admin123",
                },
            )
            if resp.status_code == 200:
                self.token = resp.json().get("token", "")
        except Exception:
            pass

    @task(2)
    def list_users(self):
        if self.token:
            self.client.get(
                "/api/auth/users",
                headers={"Authorization": f"Bearer {self.token}"},
                name="/api/auth/users [GET]",
            )

    @task(1)
    def health_check(self):
        self.client.get("/api/health", name="/api/health [GET]")


# ===== 自定义统计 =====


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    print("=" * 60)
    print("  药妆智多星 性能基线测试")
    print("  Target: " + environment.host)
    print("=" * 60)


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    print("\n" + "=" * 60)
    print("  测试完成 — 请查看统计报告")
    print("=" * 60)
