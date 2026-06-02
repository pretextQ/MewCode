from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from mewcode.config import ConfigError, load_config
from mewcode.hooks import HookConfigError, HookEngine, load_hooks
from mewcode.permissions import PermissionMode


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
        filename=".mewcode/debug.log",
        filemode="w",
    )

    parser = argparse.ArgumentParser(prog="mewcode", description="MewCode AI coding assistant")
    parser.add_argument(
        "--mode",
        choices=[m.value for m in PermissionMode],
        default=None,
        help="Permission mode (overrides config.yaml)",
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    mode_str = args.mode if args.mode else config.permission_mode
    permission_mode = PermissionMode(mode_str)

    try:
        hooks = load_hooks(config.raw_hooks)
    except HookConfigError as e:
        print(f"Hook config error: {e}", file=sys.stderr)
        sys.exit(1)


    hook_engine = HookEngine(hooks) if hooks else None

    from mewcode.app import MewCodeApp


    app = MewCodeApp(
        providers=config.providers,
        permission_mode=permission_mode,
        mcp_servers=config.mcp_servers,
        hook_engine=hook_engine,
        enable_fork=config.enable_fork,
        enable_verification_agent=config.enable_verification_agent,
        worktree_config=config.worktree,
        teammate_mode=config.teammate_mode,
        enable_coordinator_mode=config.enable_coordinator_mode,
    )
    app.run(inline=True, inline_no_clear=True)


if __name__ == "__main__":
    main()

