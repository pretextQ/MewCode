from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mewcode.permissions.dangerous import DangerousCommandDetector, is_safe_command
from mewcode.permissions.modes import DecisionEffect, PermissionMode, mode_decide
from mewcode.permissions.rules import RuleEngine, extract_content, extract_paths
from mewcode.permissions.sandbox import PathSandbox
from mewcode.tools.base import Tool

_PLAN_MODE_ALLOWED_TOOLS = frozenset({"Agent", "ToolSearch", "AskUserQuestion", "ExitPlanMode"})


@dataclass
class Decision:
    effect: DecisionEffect
    reason: str


class PermissionChecker:


    def __init__(
        self,
        detector: DangerousCommandDetector,
        sandbox: PathSandbox,
        rule_engine: RuleEngine,
        mode: PermissionMode = PermissionMode.DEFAULT,
    ) -> None:
        self.detector = detector
        self.sandbox = sandbox
        self.rule_engine = rule_engine
        self.mode = mode
        self.plan_file_path: str = ""


    def check(self, tool: Tool, arguments: dict[str, Any]) -> Decision:
        content = extract_content(tool.name, arguments)

        # Layer 0: Plan 模式的工具白名单（plan 文件写例外移到 Layer 2 之后）
        if self.mode == PermissionMode.PLAN:
            if tool.name in _PLAN_MODE_ALLOWED_TOOLS:
                return Decision(effect="allow", reason="Plan mode: allowed tool")

        # Layer 1b: 危险命令黑名单（仅 Bash）——先于白名单，保证黑名单可达
        if tool.category == "command":
            hit, reason = self.detector.detect(content)
            if hit:
                return Decision(effect="deny", reason=f"危险命令拦截: {reason}")

        # Layer 1: 安全的只读命令（自动放行）
        if tool.category == "command" and is_safe_command(content or ""):
            return Decision(effect="allow", reason="Safe read-only command")

        # Layer 2: 路径沙箱——检查实际指向文件系统的参数（Glob/Grep 的 path、
        # 文件三件套的 file_path）；无路径参数的读/写工具回退到内容字段兜底
        if tool.category in ("read", "write"):
            targets = extract_paths(tool.name, arguments)
            if not targets and content:
                targets = [content]
            for target in targets:
                ok, reason = self.sandbox.check(target)
                if not ok:
                    return Decision(effect="deny", reason=f"路径沙箱拦截: {reason}")

        # Layer 2b: PLAN 模式 plan 文件写例外——沙箱已通过，再做精确路径收敛
        if (
            self.mode == PermissionMode.PLAN
            and tool.name in ("WriteFile", "EditFile")
            and content
            and self._is_plan_file(content)
        ):
            return Decision(effect="allow", reason="Plan mode: plan file write")

        # Layer 3: 规则引擎匹配
        rule_result = self.rule_engine.evaluate(tool.name, content)
        if rule_result == "allow":
            return Decision(effect="allow", reason="权限规则放行")
        if rule_result == "deny":
            return Decision(effect="deny", reason="权限规则拒绝")

        # Layer 4: 权限模式兜底判定
        effect = mode_decide(self.mode, tool.category)
        if effect == "allow":
            return Decision(effect="allow", reason=f"权限模式 {self.mode.value} 放行")
        if effect == "deny":
            return Decision(effect="deny", reason=f"权限模式 {self.mode.value} 拒绝")

        # Layer 5: 触发人工确认（HITL）
        return Decision(effect="ask", reason="需要用户确认")


    def _is_plan_file(self, target_path: str) -> bool:
        """仅当目标等于 plan 文件本身或位于 plan 文件所在目录内时为真。

        plan_file_path 为空一律 False；不做裸子串与 basename 回退。
        """
        if not self.plan_file_path or not target_path:
            return False
        try:
            abs_target = Path(target_path).expanduser().resolve()
            abs_plan = Path(self.plan_file_path).expanduser().resolve()
        except OSError:
            return False
        if abs_target == abs_plan:
            return True
        plan_dir = abs_plan.parent
        return plan_dir == abs_target or plan_dir in abs_target.parents
