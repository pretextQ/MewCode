from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mewcode.hooks.models import HookContext


@dataclass
class Condition:
    field: str
    operator: str
    value: str


    def evaluate(self, ctx: HookContext) -> bool:
        field_value = ctx.get_field(self.field)
        if self.operator == "==":
            return field_value == self.value
        if self.operator == "!=":
            return field_value != self.value
        if self.operator == "=~":
            pattern = self.value
            if pattern.startswith("/") and pattern.endswith("/"):
                pattern = pattern[1:-1]
            try:
                return bool(re.search(pattern, field_value))
            except re.error:
                return False
        if self.operator == "~=":
            return fnmatch.fnmatch(field_value, self.value)
        return False


@dataclass
class ConditionGroup:
    conditions: list[Condition] = field(default_factory=list)
    logic: str = "and"


    def evaluate(self, ctx: HookContext) -> bool:
        if not self.conditions:
            return True
        if self.logic == "and":
            return all(c.evaluate(ctx) for c in self.conditions)
        return any(c.evaluate(ctx) for c in self.conditions)


class ConditionParseError(Exception):
    pass


_OPERATORS = ("==", "!=", "=~", "~=")


def _parse_single(expr: str) -> Condition:
    expr = expr.strip()
    # 取最早出现的运算符：值里可能含其他运算符（如 `args.text ~= a==b`），
    # 按枚举顺序找会把 == 误当分隔符错拆。
    best_op: str | None = None
    best_idx = len(expr)
    for op in _OPERATORS:
        idx = expr.find(op)
        if idx != -1 and idx < best_idx:
            best_idx = idx
            best_op = op
    if best_op is None:
        raise ConditionParseError(f"No valid operator found in condition: '{expr}'")
    field_part = expr[:best_idx].strip()
    value_part = expr[best_idx + len(best_op):].strip()
    if value_part.startswith('"') and value_part.endswith('"'):
        value_part = value_part[1:-1]
    return Condition(field=field_part, operator=best_op, value=value_part)


def parse_condition(expr: str) -> ConditionGroup | None:
    if not expr or not expr.strip():
        return None

    expr = expr.strip()
    has_and = "&&" in expr
    has_or = "||" in expr

    if has_and and has_or:
        raise ConditionParseError(
            "Cannot mix '&&' and '||' in a single condition expression. "
            "Split into separate hooks instead."
        )

    if has_and:
        parts = expr.split("&&")
        logic = "and"
    elif has_or:
        parts = expr.split("||")
        logic = "or"
    else:
        parts = [expr]
        logic = "and"

    conditions = [_parse_single(p) for p in parts]
    return ConditionGroup(conditions=conditions, logic=logic)
