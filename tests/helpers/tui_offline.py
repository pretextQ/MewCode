"""Real TUI composition with startup/model/storage/network paths disabled."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from mewcode.app import ChatInput, MewCodeApp
from mewcode.config import ProviderConfig
from mewcode.permissions import PermissionMode


class OfflineApp(MewCodeApp):
    CSS_PATH = str(Path(__file__).resolve().parents[2] / "mewcode" / "styles.tcss")

    def __init__(self, work_dir: str) -> None:
        self.offline_dir = work_dir
        super().__init__([ProviderConfig(
            name="offline", protocol="anthropic", base_url="http://127.0.0.1:1",
            model="offline-model", api_key="offline",
        )])

    def _select_provider(self, provider: ProviderConfig) -> None:
        self._selected_provider = provider
        self.agent = SimpleNamespace(  # type: ignore[assignment]
            work_dir=self.offline_dir, plan_mode=False, permission_mode=PermissionMode.DEFAULT,
            total_input_tokens=0, total_output_tokens=0,
        )
        self._refresh_header()
        self._update_mode_label()
        self.query_one("#chat-input", ChatInput).focus()

    async def _prefetch_relevant_memories(self, query: str) -> str:
        return ""

    async def _update_session_summary(self) -> None:
        pass
