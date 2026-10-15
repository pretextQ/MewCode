"""Search patterns and symlinks cannot expand the permitted search root."""
from pathlib import Path

import pytest

from mewcode.tools.glob import Glob
from mewcode.tools.glob import Params as GlobParams
from mewcode.tools.grep import Grep
from mewcode.tools.grep import Params as GrepParams


@pytest.mark.asyncio
@pytest.mark.parametrize("symlink", [False, True])
async def test_search_does_not_read_or_list_outside_root(tmp_path: Path, symlink: bool):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("secret-value", encoding="utf-8")
    (project / "inside.txt").write_text("public-value", encoding="utf-8")
    pattern = "../outside/*.txt"
    if symlink:
        try:
            (project / "escape.txt").symlink_to(secret)
        except OSError as exc:
            pytest.skip(f"symlink creation unavailable: {exc}")
        pattern = "*.txt"
    glob_result = await Glob().execute(GlobParams(path=str(project), pattern=pattern))
    grep_result = await Grep().execute(GrepParams(path=str(project), pattern="value", include=pattern))
    assert "secret" not in glob_result.output
    assert "escape.txt" not in glob_result.output
    assert "secret" not in grep_result.output
    assert "escape.txt" not in grep_result.output
    if symlink:
        assert "inside.txt" in glob_result.output
        assert "public-value" in grep_result.output
