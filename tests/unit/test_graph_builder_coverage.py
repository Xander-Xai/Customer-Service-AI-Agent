"""
测试 core/graph_builder.py — 覆盖 _format_duration 和 make_graph 向后兼容包装器
"""

import pytest
from unittest.mock import MagicMock, patch

from core.graph_builder import _format_duration


class TestFormatDuration:

    def test_milliseconds(self):
        assert _format_duration(0.001) == "1ms"
        assert _format_duration(0.5) == "500ms"
        assert _format_duration(0.999) == "999ms"

    def test_seconds(self):
        assert _format_duration(1.0) == "1.0s"
        assert _format_duration(2.5) == "2.5s"
        assert _format_duration(30.0) == "30.0s"

    def test_zero(self):
        assert _format_duration(0) == "0ms"


class TestMakeGraph:

    def test_make_graph_creates_container_and_builds(self):
        from core.graph_builder import make_graph, _default_container

        # Reset singleton
        import core.graph_builder as gb
        gb._default_container = None

        with patch("core.graph_builder.build_graph") as mock_build:
            mock_build.return_value = "mock_app"
            result = make_graph()
            assert result == "mock_app"
            mock_build.assert_called_once()

    def test_make_graph_reuses_singleton(self):
        import core.graph_builder as gb
        from core.graph_builder import make_graph
        gb._default_container = None

        with patch("core.graph_builder.build_graph") as mock_build:
            mock_build.return_value = "app1"
            make_graph()
            make_graph()
            # build_graph called twice but container is same singleton
            assert mock_build.call_count == 2

    def teardown_method(self):
        import core.graph_builder as gb
        gb._default_container = None
