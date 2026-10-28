"""Render service-owned status separately from untrusted model opinions."""
from __future__ import annotations

import html
import json
import re

from .jobs import JobEvent
from .review_models import ReviewResult


def plain(value: str) -> str:
    value = html.escape(" ".join(value.split()), quote=True)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>-])", r"\\\1", value)


def render_review(result: ReviewResult | None) -> list[str]:
    if result is None:
        return []
    labels = {"findings": "发现问题", "no_findings": "所提供范围内未发现问题", "incomplete": "审查未完成"}
    lines = [
        "", "## 独立修复审查（仅供人工参考）", "",
        f"- 状态：{labels[result.status]}（{plain(result.reason)}）",
        f"- 受审版本：base `{result.base_sha or 'unknown'}` / head `{result.head_sha}`",
        f"- 完整输入覆盖：{'是' if result.coverage_complete else '否'}；"
        f"问题 {len(result.findings)} 条，变更文件 {len(result.coverage)} 个",
        f"- 模型：{plain(result.provider)} / {plain(result.model)}；耗时 {result.elapsed_seconds:.3f}s",
        f"- 用量：input {result.input_tokens}, output {result.output_tokens}, "
        f"cache read/create {result.cache_read}/{result.cache_creation}；"
        f"{'provider 已报告' if result.usage_known else '用量未知，预算保留预留值'}",
        "",
        "> 下方意见来自独立上下文模型，不覆盖服务记录的测试事实，不代表验证通过或合并批准。",
    ]
    if result.summary:
        lines += ["", "模型摘要：" + plain(result.summary)]
    for item in result.findings:
        lines.append(
            f"- **{item['severity']}** {plain(item['path'])}:{item['line']} ({item['side']}) "
            f"— {plain(item['title'])}：{plain(item['detail'])}"
        )
    lines += [
        "", "### 模型输入覆盖", "",
        "full/partial/none 仅表示 diff 输入包含程度；请求失败不代表这些代码已完成审查。",
        "", "| 文件 | 输入覆盖 | 原因 |", "|---|---|---|",
    ]
    for file in result.coverage[:30]:
        name = (file.old_path + " → " if file.old_path else "") + file.path
        lines.append(f"| {plain(name)} | {file.status} | {plain(file.reason)} |")
    if len(result.coverage) > 30:
        lines.append(f"\n其余 {len(result.coverage) - 30} 个文件见 job 报告中的完整覆盖记录。")
    lines.append(f"\n完整输入覆盖：{'是' if result.coverage_complete else '否'}。")
    for kind, path in result.artifacts.items():
        lines.append(f"- 本地证据 {kind}：{plain(path)}（SHA-256 `{result.hashes.get(kind, 'unknown')}`）")
    limited: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > 12_000:
            limited += ["", "PR 展示预算已用完；完整问题、覆盖与证据见 job 报告。"]
            break
        limited.append(line)
        used += len(line) + 1
    return limited


def review_reports(events: list[JobEvent]) -> list[dict]:
    reports = []
    for event in events:
        if event.kind == "review_finished":
            try:
                reports.append({"ts": event.ts, **json.loads(event.detail)})
            except (ValueError, TypeError):
                reports.append({"ts": event.ts, "status": "incomplete", "reason": "unreadable_review_record"})
    return reports
