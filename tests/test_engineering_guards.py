"""Executable boundaries and isolated counterexamples for the source guards."""
import pytest

from scripts.engineering_guards import ROOT, architecture_errors, imports, source_budgets


def test_repository_guards():
    assert architecture_errors(ROOT) == []
    assert source_budgets(ROOT)[0] == []


@pytest.mark.parametrize(("name", "source"), [
    ("mewcode/service/example.py", "import mewcode.app as ui"),
    ("mewcode/service/example.py", "from .. import app"),
    ("mewcode/service/__init__.py", "from ..app import MewCodeApp"),
    ("mewcode/service/example.py", "def lazy():\n    import textual.widgets"),
    ("mewcode/permissions/example.py", "from ..service import jobs"),
    ("mewcode/permissions/example.py", "import mewcode.service.jobs"),
    ("mewcode/tools/impl/example.py", "from ...agent import Agent"),
    ("mewcode/tools/impl/example.py", "from mewcode import agent"),
])
def test_forbidden_import_is_red_and_names_file(tmp_path, name, source):
    path = tmp_path / name
    path.parent.mkdir(parents=True)
    path.write_text(source, encoding="utf-8")
    errors = architecture_errors(tmp_path)
    assert errors and all(error.startswith(name + ":") for error in errors)


def test_relative_imports_and_windows_paths():
    assert imports("from ...agent import Agent", r"mewcode\tools\impl\x.py") == [
        (1, "mewcode.agent"), (1, "mewcode.agent.Agent"),
    ]


def test_allowed_dependencies_and_similar_names(tmp_path):
    path = tmp_path / "mewcode/service/x.py"
    path.parent.mkdir(parents=True)
    path.write_text("from . import jobs\nfrom mewcode import agent\nimport mewcode.application", encoding="utf-8")
    assert architecture_errors(tmp_path) == []


def test_budgets_fail_then_recover_and_reject_stale_entries(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/source_budgets.json").write_text(
        '{"default_ceiling": 2, "ceilings": {"mewcode/big.py": 3}}', encoding="utf-8",
    )
    (tmp_path / "mewcode").mkdir()
    for name, limit in [("big.py", 3), ("other.py", 2)]:
        path = tmp_path / "mewcode" / name
        path.write_text("# line\n" * (limit + 1), encoding="utf-8")
        assert any(f"mewcode/{name}:" in error for error in source_budgets(tmp_path)[0])
        path.write_text("# line\n" * limit, encoding="utf-8")
    assert source_budgets(tmp_path)[0] == []
    assert "remaining 0" in source_budgets(tmp_path)[1][0]
    (tmp_path / "mewcode/big.py").unlink()
    assert source_budgets(tmp_path)[0] == ["mewcode/big.py: stale or non-POSIX budget entry"]
