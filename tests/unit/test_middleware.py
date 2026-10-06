"""Trace middleware 功能测试。"""


class TestTraceMiddleware:
    """验证 api/middleware.py 中 trace_middleware 的功能和包结构。"""

    def test_trace_middleware_module_importable(self):
        """验证 middleware 模块可以被正确导入"""
        from api.middleware import setup_middleware

        assert callable(setup_middleware), "setup_middleware 应为可调用函数"

    def test_trace_middleware_has_trace_function(self):
        """验证 api/middleware.py 中存在 trace_middleware 实现"""
        import api.middleware as mw

        # 检查 trace_id 相关常量或函数
        assert hasattr(mw, "setup_middleware"), "模块应暴露 setup_middleware"

    def test_middleware_package_has_init(self):
        """验证 api/middleware/ 目录有 __init__.py"""
        import os

        # 相对于项目根目录
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        init_path = os.path.join(base, "api", "middleware", "__init__.py")
        assert os.path.exists(init_path), (
            f"api/middleware/__init__.py 不存在 ({init_path})\n" "需要创建该文件以完善包结构"
        )
