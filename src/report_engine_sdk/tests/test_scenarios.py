"""End-to-end scenario tests for the report engine SDK example packs.

These tests validate the scenarios described in spec section "四、场景兼容性
设计方案" (4.1 multi-role, 4.2 single-role, 4.3 agent briefing) plus a
learning-report scenario that exercises shared variables and shared-rule
references. They use the package's real ``config/packs/`` examples. No
temporary fixtures are created; the actual shipping examples are exercised so
that any configuration drift in the examples is caught here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from report_engine_sdk import ReportEngine, MemoryStorage

# Path to the package's own config dir (contains packs/<pack_id>/pack.json).
CONFIG_DIR = str(Path(__file__).parent.parent / "config")


def test_scenario_4_1_multi_role() -> None:
    """Scenario 4.1: multi-role report rendered to manager and teacher views."""
    engine = ReportEngine(CONFIG_DIR, MemoryStorage())
    facts = {
        "teacher_name": "张老师",
        "base_score": 95,
        "bonus": 5,
        "class_size": 45,
    }

    eval_res = engine.evaluate("teacher_eval:performance", facts)

    assert eval_res.success is True
    assert eval_res.data is not None
    # total_score = 95 * 0.8 + 5 = 76 + 5 = 81.0
    assert eval_res.data["total_score"] == pytest.approx(81.0)
    # 81 >= 75 and < 90 -> "良好" (shared rule referenced via {"ref": ...})
    assert eval_res.data["performance_level"] == "良好"

    render_res_mgr = engine.render(
        "teacher_eval:performance", eval_res.data, view="manager"
    )
    assert render_res_mgr.success is True
    assert render_res_mgr.preview is not None
    assert "管理层视图" in render_res_mgr.preview
    assert "张老师" in render_res_mgr.preview

    render_res_tch = engine.render(
        "teacher_eval:performance", eval_res.data, view="teacher"
    )
    assert render_res_tch.success is True
    assert render_res_tch.preview is not None
    assert "教师视图" in render_res_tch.preview


def test_scenario_4_2_single_role() -> None:
    """Scenario 4.2: single standard report rendered via the default view."""
    engine = ReportEngine(CONFIG_DIR, MemoryStorage())
    facts = {
        "service_name": "api-gateway",
        "uptime_pct": 99.9,
        "error_count": 3,
        "latency_ms": 45.2,
        "date_str": "2026-07-17",
    }

    eval_res = engine.evaluate("ops_report:health_check", facts)

    assert eval_res.success is True
    assert eval_res.data is not None
    # 99.9 >= 99.5 and 3 < 10 -> "健康"
    assert eval_res.data["status"] == "健康"

    render_res = engine.render("ops_report:health_check", eval_res.data)
    assert render_res.success is True
    assert render_res.preview is not None
    assert "系统健康检查报告" in render_res.preview
    assert "api-gateway" in render_res.preview
    assert "健康" in render_res.preview


def test_scenario_4_3_agent_briefing() -> None:
    """Scenario 4.3: agent briefing with empty rules passes data straight through."""
    engine = ReportEngine(CONFIG_DIR, MemoryStorage())
    agent_data = {
        "user_name": "Alice",
        "summary_text": "今日完成3个任务，明日继续推进项目X。",
        "action_items": ["Review PR #123", "部署到测试环境"],
        "date_str": "2026-07-17",
    }

    # Empty rules -> evaluate is a passthrough; data is unchanged.
    eval_res = engine.evaluate("work_report:daily_briefing", agent_data)

    assert eval_res.success is True
    assert eval_res.data is not None
    assert eval_res.data == agent_data

    # Direct render (single-step workflow) also works against the raw data.
    render_res = engine.render(
        "work_report:daily_briefing", agent_data, view="summary"
    )
    assert render_res.success is True
    assert render_res.preview is not None
    assert "每日简报" in render_res.preview
    assert "Alice" in render_res.preview
    assert "Review PR #123" in render_res.preview


def test_scenario_learning_report_shared_defs() -> None:
    """Learning report exercises shared variables + a shared-rule reference.

    ``report_date`` is supplied by the pack's shared_variables (merged into the
    report's input_schema, required), and ``learning_level`` is produced by a
    shared rule referenced via ``{"ref": "learning_level"}``.
    """
    engine = ReportEngine(CONFIG_DIR, MemoryStorage())
    facts = {
        "student_name": "李同学",
        "grade": "高二",
        "report_date": "2026-07-17",  # required via shared_variables
        "gpa": 3.9,
        "class_rank": 3,
        "class_size": 40,
        "learning_points": 88,
        "online_hours": 12.5,
        "homework_completion_rate": 95.0,
        "participation_score": 90.0,
        "strengths": ["数学", "物理"],
        "weaknesses": ["英语听力"],
        "focus_area": "英语听力",
        "maintain_area": "数学",
    }

    eval_res = engine.evaluate("learning_report:learning_profile", facts)

    assert eval_res.success is True
    assert eval_res.data is not None
    # gpa 3.9 >= 3.8 -> "优秀" (shared rule)
    assert eval_res.data["learning_level"] == "优秀"

    render_res = engine.render(
        "learning_report:learning_profile", eval_res.data, view="default"
    )
    assert render_res.success is True
    assert render_res.preview is not None
    assert "学习画像报告" in render_res.preview
    assert "李同学" in render_res.preview
    assert "2026-07-17" in render_res.preview  # shared variable rendered
    assert "优秀" in render_res.preview


def test_scenario_learning_report_missing_shared_required() -> None:
    """Omitting a shared-required field (report_date) fails validation."""
    engine = ReportEngine(CONFIG_DIR, MemoryStorage())
    facts = {
        "student_name": "李同学",
        "grade": "高二",
        "gpa": 3.9,
        "class_rank": 3,
        "class_size": 40,
        # report_date omitted -- required via shared_variables
    }

    eval_res = engine.evaluate("learning_report:learning_profile", facts)

    assert eval_res.success is False
    assert eval_res.errors is not None
    assert "missing_fields" in eval_res.errors
    assert "report_date" in eval_res.errors["missing_fields"]


def test_engine_loads_all_examples() -> None:
    """The package packs declare all four example reports."""
    engine = ReportEngine(CONFIG_DIR, MemoryStorage())

    assert engine.list_reports() == [
        "learning_report:learning_profile",
        "ops_report:health_check",
        "teacher_eval:performance",
        "work_report:daily_briefing",
    ]
