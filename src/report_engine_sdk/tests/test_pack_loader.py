"""Tests for the pack-based configuration providers and loader.

Each test builds a temporary ``packs/<pack_id>/pack.json`` plus template files
inside a pytest ``tmp_path`` fixture directory, then exercises
:class:`FileSystemPackProvider` / :class:`InMemoryPackProvider` and
:class:`PackLoader` to verify discovery, shared-variable merging, shared-rule
reference expansion, template path resolution, and self-check behavior.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from report_engine_sdk.core.config_provider import (
    FileSystemPackProvider,
    InMemoryPackProvider,
    PackConfig,
    PackFormatError,
    PackNotFoundError,
)
from report_engine_sdk.core.pack_loader import (
    PackLoader,
    PackSelfCheckError,
    ReportConfig,
)


def _pack_dir(tmp_path: Path, pack_id: str) -> Path:
    """Create and return ``tmp_path/packs/<pack_id>``."""
    d = tmp_path / "packs" / pack_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_pack_json(pack_dir: Path, pack: dict) -> None:
    """Serialize ``pack`` as JSON to ``pack_dir/pack.json``."""
    (pack_dir / "pack.json").write_text(
        json.dumps(pack, ensure_ascii=False), encoding="utf-8"
    )


def _write_template(pack_dir: Path, name: str, content: str) -> None:
    """Write ``content`` to ``pack_dir/templates/<name>``."""
    tdir = pack_dir / "templates"
    tdir.mkdir(exist_ok=True)
    (tdir / name).write_text(content, encoding="utf-8")


def _base_schema() -> dict:
    """Return a reusable input_schema with base_score and bonus properties."""
    return {
        "type": "object",
        "properties": {
            "base_score": {"type": "number"},
            "bonus": {"type": "number"},
        },
        "required": ["base_score"],
    }


# ---------------------------------------------------------------------------
# FileSystemPackProvider + PackLoader — discovery and loading.
# ---------------------------------------------------------------------------


def test_load_success(tmp_path: Path) -> None:
    """A valid pack loads with merged fields and absolute template paths."""
    pack_dir = _pack_dir(tmp_path, "teacher_eval")
    _write_template(
        pack_dir, "manager.md", "Total: {{ base_score }}, bonus: {{ bonus }}"
    )
    _write_pack_json(
        pack_dir,
        {
            "pack_id": "teacher_eval",
            "purpose": "教师绩效评估",
            "version": "1.0.0",
            "owner": "hr-team",
            "reports": {
                "performance": {
                    "input_schema": _base_schema(),
                    "rules": [],
                    "templates": {
                        "manager": {
                            "path": "templates/manager.md",
                            "prompt": "focus on strategy",
                        },
                    },
                }
            },
        },
    )

    loader = PackLoader(FileSystemPackProvider(str(tmp_path)))
    config = loader.get("teacher_eval:performance")

    assert isinstance(config, ReportConfig)
    assert config.report_id == "teacher_eval:performance"
    assert config.pack_id == "teacher_eval"
    assert config.name == "performance"
    assert config.input_schema == _base_schema()
    assert config.rules == []
    assert "manager" in config.templates
    # Template path resolved to absolute form.
    assert Path(config.templates["manager"]["path"]).is_absolute()
    assert Path(config.templates["manager"]["path"]).exists()
    assert config.templates["manager"]["prompt"] == "focus on strategy"
    assert loader.list_reports() == ["teacher_eval:performance"]


def test_self_check_template_var_not_in_schema(tmp_path: Path) -> None:
    """A template variable absent from schema and rules fails self-check."""
    pack_dir = _pack_dir(tmp_path, "teacher_eval")
    _write_template(pack_dir, "manager.md", "Score: {{ score }}")
    _write_pack_json(
        pack_dir,
        {
            "pack_id": "teacher_eval",
            "reports": {
                "performance": {
                    "input_schema": _base_schema(),
                    "rules": [],
                    "templates": {"manager": {"path": "templates/manager.md"}},
                }
            },
        },
    )

    with pytest.raises(PackSelfCheckError) as exc_info:
        PackLoader(FileSystemPackProvider(str(tmp_path)))
    assert "score" in str(exc_info.value)


def test_self_check_missing_template_file(tmp_path: Path) -> None:
    """A pack referencing a nonexistent template file fails self-check."""
    pack_dir = _pack_dir(tmp_path, "teacher_eval")
    _write_pack_json(
        pack_dir,
        {
            "pack_id": "teacher_eval",
            "reports": {
                "performance": {
                    "input_schema": _base_schema(),
                    "rules": [],
                    "templates": {
                        "manager": {"path": "templates/nonexistent.md"}
                    },
                }
            },
        },
    )

    with pytest.raises(PackSelfCheckError) as exc_info:
        PackLoader(FileSystemPackProvider(str(tmp_path)))
    assert "nonexistent.md" in str(exc_info.value)


def test_unknown_report_id(tmp_path: Path) -> None:
    """Requesting an unknown report_id raises PackSelfCheckError."""
    pack_dir = _pack_dir(tmp_path, "teacher_eval")
    _write_template(pack_dir, "manager.md", "{{ base_score }}")
    _write_pack_json(
        pack_dir,
        {
            "pack_id": "teacher_eval",
            "reports": {
                "performance": {
                    "input_schema": _base_schema(),
                    "rules": [],
                    "templates": {"manager": {"path": "templates/manager.md"}},
                }
            },
        },
    )

    loader = PackLoader(FileSystemPackProvider(str(tmp_path)))
    with pytest.raises(PackSelfCheckError) as exc_info:
        loader.get("teacher_eval:does_not_exist")
    assert "teacher_eval:does_not_exist" in str(exc_info.value)


def test_config_path_not_found() -> None:
    """A provider over a missing config dir raises PackNotFoundError."""
    with pytest.raises(PackNotFoundError):
        FileSystemPackProvider("/nonexistent/dir").list_packs()


def test_missing_required_keys(tmp_path: Path) -> None:
    """A report missing a required key raises PackFormatError."""
    pack_dir = _pack_dir(tmp_path, "teacher_eval")
    _write_template(pack_dir, "manager.md", "{{ base_score }}")
    _write_pack_json(
        pack_dir,
        {
            "pack_id": "teacher_eval",
            "reports": {
                "performance": {
                    "input_schema": _base_schema(),
                    "templates": {"manager": {"path": "templates/manager.md"}},
                    # "rules" missing
                }
            },
        },
    )

    with pytest.raises(PackFormatError) as exc_info:
        PackLoader(FileSystemPackProvider(str(tmp_path)))
    assert "rules" in str(exc_info.value)


def test_duplicate_report_id(tmp_path: Path) -> None:
    """Two packs with the same pack_id collide on report ids.

    Two distinct directories ``demo_0`` and ``demo_1`` both declare
    ``pack_id: "demo"`` and a report ``performance``, so each produces the
    global id ``demo:performance`` and the loader must reject the collision.
    """
    for idx in range(2):
        pack_dir = _pack_dir(tmp_path, f"demo_{idx}")
        _write_template(pack_dir, "manager.md", "{{ base_score }}")
        _write_pack_json(
            pack_dir,
            {
                "pack_id": "demo",
                "reports": {
                    "performance": {
                        "input_schema": _base_schema(),
                        "rules": [],
                        "templates": {
                            "manager": {"path": "templates/manager.md"}
                        },
                    }
                },
            },
        )

    with pytest.raises(PackFormatError) as exc_info:
        PackLoader(FileSystemPackProvider(str(tmp_path)))
    assert "Duplicate report id" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Shared variables and shared rules.
# ---------------------------------------------------------------------------


def test_shared_variables_merged_from_file(tmp_path: Path) -> None:
    """Shared variables (file) merge into every report's input_schema."""
    pack_dir = _pack_dir(tmp_path, "learning_report")
    _write_template(
        pack_dir, "profile.md", "{{ student_name }} on {{ report_date }}"
    )
    (pack_dir / "variables").mkdir(exist_ok=True)
    (pack_dir / "variables" / "common.vars.json").write_text(
        json.dumps(
            {
                "properties": {"report_date": {"type": "string"}},
                "required": ["report_date"],
            }
        ),
        encoding="utf-8",
    )
    _write_pack_json(
        pack_dir,
        {
            "pack_id": "learning_report",
            "shared_variables": "variables/common.vars.json",
            "reports": {
                "profile": {
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "student_name": {"type": "string"}
                        },
                        "required": ["student_name"],
                    },
                    "rules": [],
                    "templates": {"default": {"path": "templates/profile.md"}},
                }
            },
        },
    )

    loader = PackLoader(FileSystemPackProvider(str(tmp_path)))
    config = loader.get("learning_report:profile")

    props = config.input_schema["properties"]
    assert "student_name" in props
    assert "report_date" in props  # merged from shared_variables
    # required lists are unioned.
    assert set(config.input_schema["required"]) == {"student_name", "report_date"}


def test_shared_rule_ref_expanded(tmp_path: Path) -> None:
    """A ``{"ref": NAME}`` entry is expanded from the pack's shared rules."""
    pack_dir = _pack_dir(tmp_path, "learning_report")
    _write_template(pack_dir, "profile.md", "Level: {{ learning_level }}")
    (pack_dir / "rules").mkdir(exist_ok=True)
    (pack_dir / "rules" / "learning_level.rules.json").write_text(
        json.dumps(
            {
                "learning_level": {
                    "type": "formula",
                    "expression": "'优秀' if gpa >= 3.8 else '待提高'",
                }
            }
        ),
        encoding="utf-8",
    )
    _write_pack_json(
        pack_dir,
        {
            "pack_id": "learning_report",
            "shared_rules": "rules/learning_level.rules.json",
            "reports": {
                "profile": {
                    "input_schema": {
                        "type": "object",
                        "properties": {"gpa": {"type": "number"}},
                        "required": ["gpa"],
                    },
                    "rules": [{"ref": "learning_level"}],
                    "templates": {"default": {"path": "templates/profile.md"}},
                }
            },
        },
    )

    loader = PackLoader(FileSystemPackProvider(str(tmp_path)))
    config = loader.get("learning_report:profile")

    assert len(config.rules) == 1
    assert config.rules[0]["name"] == "learning_level"
    assert config.rules[0]["type"] == "formula"
    assert "gpa" in config.rules[0]["expression"]


def test_unknown_shared_rule_ref(tmp_path: Path) -> None:
    """A ``ref`` to a name absent from shared_rules raises PackFormatError."""
    pack_dir = _pack_dir(tmp_path, "learning_report")
    _write_template(pack_dir, "profile.md", "{{ gpa }}")
    _write_pack_json(
        pack_dir,
        {
            "pack_id": "learning_report",
            "reports": {
                "profile": {
                    "input_schema": {
                        "type": "object",
                        "properties": {"gpa": {"type": "number"}},
                        "required": ["gpa"],
                    },
                    "rules": [{"ref": "nonexistent_rule"}],
                    "templates": {"default": {"path": "templates/profile.md"}},
                }
            },
        },
    )

    with pytest.raises(PackFormatError) as exc_info:
        PackLoader(FileSystemPackProvider(str(tmp_path)))
    assert "nonexistent_rule" in str(exc_info.value)


def test_inline_shared_definitions(tmp_path: Path) -> None:
    """Inline (dict) shared variables/rules work without any extra files."""
    pack_dir = _pack_dir(tmp_path, "learning_report")
    _write_template(pack_dir, "profile.md", "{{ report_date }} {{ level }}")
    _write_pack_json(
        pack_dir,
        {
            "pack_id": "learning_report",
            "shared_variables": {
                "properties": {"report_date": {"type": "string"}},
                "required": ["report_date"],
            },
            "shared_rules": {
                "level": {
                    "type": "formula",
                    "expression": "'A' if gpa >= 3.8 else 'B'",
                }
            },
            "reports": {
                "profile": {
                    "input_schema": {
                        "type": "object",
                        "properties": {"gpa": {"type": "number"}},
                        "required": ["gpa"],
                    },
                    "rules": [{"ref": "level"}],
                    "templates": {"default": {"path": "templates/profile.md"}},
                }
            },
        },
    )

    loader = PackLoader(FileSystemPackProvider(str(tmp_path)))
    config = loader.get("learning_report:profile")
    assert "report_date" in config.input_schema["properties"]
    assert config.rules[0]["name"] == "level"


# ---------------------------------------------------------------------------
# root.json enable-list and InMemoryPackProvider.
# ---------------------------------------------------------------------------


def test_root_json_enable_list(tmp_path: Path) -> None:
    """root.json selects a subset of available packs."""
    # Two packs on disk.
    for pack_id in ("enabled_pack", "disabled_pack"):
        pack_dir = _pack_dir(tmp_path, pack_id)
        _write_template(pack_dir, "t.md", "{{ x }}")
        _write_pack_json(
            pack_dir,
            {
                "pack_id": pack_id,
                "reports": {
                    "r": {
                        "input_schema": {
                            "type": "object",
                            "properties": {"x": {"type": "string"}},
                            "required": ["x"],
                        },
                        "rules": [],
                        "templates": {"default": {"path": "templates/t.md"}},
                    }
                },
            },
        )
    # root.json enables only one.
    (tmp_path / "root.json").write_text(
        json.dumps({"packs": ["packs/enabled_pack"]}), encoding="utf-8"
    )

    loader = PackLoader(FileSystemPackProvider(str(tmp_path)))
    assert loader.list_reports() == ["enabled_pack:r"]


def test_root_json_missing_pack_json(tmp_path: Path) -> None:
    """root.json pointing at a dir without pack.json raises PackNotFoundError."""
    (tmp_path / "packs" / "empty").mkdir(parents=True)
    (tmp_path / "root.json").write_text(
        json.dumps({"packs": ["packs/empty"]}), encoding="utf-8"
    )

    with pytest.raises(PackNotFoundError):
        FileSystemPackProvider(str(tmp_path)).list_packs()


def test_in_memory_provider() -> None:
    """InMemoryPackProvider returns caller-supplied PackConfig objects."""
    pack = PackConfig(
        pack_id="demo",
        purpose="",
        version="",
        owner="",
        pack_dir=Path("/nonexistent"),
        reports={},
        shared_variables={},
        shared_rules={},
    )
    provider = InMemoryPackProvider([pack])
    assert provider.list_packs() == [pack]
    # list_packs returns a copy, not the internal list.
    assert provider.list_packs() is not provider.list_packs()


def test_from_raw_missing_pack_id() -> None:
    """PackConfig.from_raw rejects a dict without pack_id."""
    with pytest.raises(PackFormatError):
        PackConfig.from_raw({"reports": {}}, Path("/tmp"))


def test_from_raw_missing_reports() -> None:
    """PackConfig.from_raw rejects a dict without reports."""
    with pytest.raises(PackFormatError):
        PackConfig.from_raw({"pack_id": "demo"}, Path("/tmp"))
