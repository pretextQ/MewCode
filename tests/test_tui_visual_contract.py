"""Rendered hierarchy regression for the user's approved TUI hierarchy and current dark palette."""
from __future__ import annotations

from importlib.metadata import version

import pytest
from textual.widgets import Static

from mewcode.app import ChatInput
from mewcode.conversation import Message
from mewcode.tui.details import DetailsPanel
from tests.helpers.tui_offline import OfflineApp


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 36), (160, 48)])
async def test_reference_header_composer_and_always_visible_task(tmp_path, size):
    app = OfflineApp(str(tmp_path))
    async with app.run_test(size=size) as pilot:
        await app._render_restored_messages([
            Message("user", "请检查界面。"), Message("assistant", "已经进入对话区域。"),
        ])
        await pilot.pause()
        title = app.query_one("#title-bar", Static)
        workspace = app.query_one("#workspace-label", Static)
        model = app.query_one("#model-label", Static)
        assert "MewCode" in str(title.content) and "v" + version("mewcode") in str(title.content)
        assert "offline-model" not in str(title.content)
        assert str(tmp_path) in workspace.tooltip.plain
        assert str(tmp_path)[:8] in str(workspace.content) and "⎇" in str(workspace.content)
        assert "offline-model" in str(model.content)
        assert title.region.bottom <= workspace.region.y == model.region.y
        assert workspace.region.right <= model.region.x
        assert app.current_theme.dark
        assert app.screen.styles.background.hex == "#0E1015"
        assert app.query_one(".user-message").styles.background != app.screen.styles.background
        assert app.query_one(".user-message").region.y <= app.query_one("#chat-area").content_region.y + 1
        inp = app.query_one(ChatInput)
        composer = app.query_one("#composer")
        assert composer.region.height >= 7 and inp.region in composer.region
        assert app.query_one("#composer-label").region.bottom <= inp.region.y
        assert app.query_one("#key-hints").region.y >= inp.region.bottom
        assert app.query_one("#status-bar").region.y >= composer.region.bottom
        await pilot.press("f2")
        await pilot.pause()
        panel = app.query_one(DetailsPanel)
        # The task is above the tabs, visible on the default file-changes page too.
        task = panel.query_one("#task-facts")
        assert task.display and task.region in panel.content_region
        assert task.region.bottom <= panel.query_one("TabbedContent").region.y
        assert "无结构化任务" in str(task.content)


@pytest.mark.asyncio
async def test_provider_selection_fills_available_body(tmp_path):
    app = OfflineApp(str(tmp_path))
    app.providers.append(app.providers[0])
    # Use unique Option IDs while leaving both entries offline.
    from dataclasses import replace
    app.providers[1] = replace(app.providers[0], name="offline-two")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        assert not app.query_one("#conversation-layout").display
        assert app.query_one("#provider-select").region.height >= 28
