from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from mewcode.conversation import ConversationManager, Message, ToolResultBlock

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SINGLE_RESULT_CHAR_LIMIT = 5_000
AGGREGATE_CHAR_LIMIT = 20_000
PREVIEW_CHARS = 2_000

KEEP_RECENT_TURNS = 10
OLD_RESULT_SNIP_CHARS = 2_000
SNIPPED_TAG = "<snipped>"

SUMMARY_OUTPUT_RESERVE = 20_000
AUTO_COMPACT_SAFETY_MARGIN = 13_000
MANUAL_COMPACT_SAFETY_MARGIN = 3_000

PERSISTED_TAG = "<persisted-output>"

SESSION_SUBDIR = ".mewcode/session/tool-results"


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass
class CompactEvent:
    before_tokens: int


# ---------------------------------------------------------------------------
# Content replacement state — Design B (decision freezing, no mutation)
# ---------------------------------------------------------------------------

@dataclass
class ContentReplacementState:
    seen_ids: set[str] = field(default_factory=set)
    replacements: dict[str, str] = field(default_factory=dict)


@dataclass
class ContentReplacementRecord:
    tool_use_id: str
    replacement: str
    kind: str = "tool-result"


def create_replacement_state() -> ContentReplacementState:
    return ContentReplacementState()


def clone_replacement_state(src: ContentReplacementState) -> ContentReplacementState:
    return ContentReplacementState(
        seen_ids=set(src.seen_ids),
        replacements=dict(src.replacements),
    )


REPLACEMENT_RECORDS_FILENAME = "replacement_records.jsonl"


def append_replacement_records(
    session_dir: Path, records: list[ContentReplacementRecord]
) -> None:
    if not records:
        return
    path = session_dir / REPLACEMENT_RECORDS_FILENAME
    with path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps({
                "kind": r.kind,
                "tool_use_id": r.tool_use_id,
                "replacement": r.replacement,
            }, ensure_ascii=False) + "\n")


def load_replacement_records(session_dir: Path) -> list[ContentReplacementRecord]:
    path = session_dir / REPLACEMENT_RECORDS_FILENAME
    if not path.exists():
        return []
    out: list[ContentReplacementRecord] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            out.append(ContentReplacementRecord(
                kind=obj.get("kind", "tool-result"),
                tool_use_id=obj["tool_use_id"],
                replacement=obj["replacement"],
            ))
    return out


def reconstruct_replacement_state(
    messages: list[Message],
    records: list[ContentReplacementRecord],
    inherited_replacements: Mapping[str, str] | None = None,
) -> ContentReplacementState:
    state = create_replacement_state()
    candidate_ids: set[str] = set()
    for msg in messages:
        for tr in msg.tool_results:
            candidate_ids.add(tr.tool_use_id)
    state.seen_ids.update(candidate_ids)
    for r in records:
        if r.kind == "tool-result" and r.tool_use_id in candidate_ids:
            state.replacements[r.tool_use_id] = r.replacement
    if inherited_replacements:
        for tool_use_id, replacement in inherited_replacements.items():
            if tool_use_id in candidate_ids and tool_use_id not in state.replacements:
                state.replacements[tool_use_id] = replacement
    return state


# ---------------------------------------------------------------------------
# Session directory management
# ---------------------------------------------------------------------------

def ensure_session_dir(work_dir: str) -> Path:
    session_dir = Path(work_dir) / SESSION_SUBDIR
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def cleanup_tool_results(session_dir: Path) -> None:
    if session_dir.exists():
        shutil.rmtree(session_dir)
        session_dir.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Layer 1: Persist large tool results to disk
# ---------------------------------------------------------------------------

def persist_tool_result(tool_use_id: str, content: str, session_dir: Path) -> Path:
    file_path = session_dir / f"{tool_use_id}.txt"
    try:
        fd = os.open(str(file_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
    except FileExistsError:
        pass
    return file_path


def make_persisted_preview(content: str, file_path: Path) -> str:
    size_kb = len(content.encode("utf-8")) // 1024
    preview = content[:PREVIEW_CHARS]
    return (
        f"{PERSISTED_TAG}\n"
        f"输出太大（{size_kb}KB），完整内容已保存到：\n"
        f"{file_path}\n"
        f"\n"
        f"预览（前 2KB）：\n"
        f"{preview}\n"
        f"</persisted-output>"
    )


def _count_turns(messages: list[Message]) -> int:
    count = 0
    for m in messages:
        if m.role == "assistant" and not m.tool_uses:
            count += 1
    return count


def _copy_message_with_results(
    msg: Message, new_tool_results: list[ToolResultBlock]
) -> Message:
    return Message(
        role=msg.role,
        content=msg.content,
        tool_uses=list(msg.tool_uses),
        tool_results=new_tool_results,
        thinking_blocks=list(msg.thinking_blocks),
    )


def _snip_stale_messages(
    history: list[Message],
) -> list[Message]:
    total_turns = _count_turns(history)
    if total_turns <= KEEP_RECENT_TURNS:
        return history

    out: list[Message] = []
    turns_seen = 0
    old_boundary = total_turns - KEEP_RECENT_TURNS

    for msg in history:
        if msg.role == "assistant" and not msg.tool_uses:
            turns_seen += 1
        if turns_seen > old_boundary or not msg.tool_results:
            out.append(msg)
            continue

        new_results: list[ToolResultBlock] = []
        changed = False
        for tr in msg.tool_results:
            if (
                tr.content.startswith(SNIPPED_TAG)
                or tr.content.startswith(PERSISTED_TAG)
                or len(tr.content) <= OLD_RESULT_SNIP_CHARS
            ):
                new_results.append(tr)
                continue
            preview = tr.content[:200]
            orig_len = len(tr.content)
            new_content = (
                f"{SNIPPED_TAG}\n"
                f"(旧结果已裁剪，原始长度 {orig_len} 字符)\n"
                f"{preview}\n"
                f"… (snipped)"
            )
            new_results.append(ToolResultBlock(
                tool_use_id=tr.tool_use_id,
                content=new_content,
                is_error=tr.is_error,
            ))
            changed = True

        out.append(_copy_message_with_results(msg, new_results) if changed else msg)

    return out


def apply_tool_result_budget(
    conversation: ConversationManager,
    session_dir: Path,
    state: ContentReplacementState,
) -> tuple[ConversationManager, list[ContentReplacementRecord]]:
    """
    Design B: 不 mutate 原 conversation。

    返回一个新的 ConversationManager，其中 tool_result.content 已根据 state.replacements
    应用了决策，并对本轮 fresh 候选执行了 Pass 1（单条超限）+ Pass 2（聚合超限）。
    Pass 3（陈旧裁剪）在新 history 上跑，仍然 stateless（边界 drift 是已知 trade-off）。

    state 会被 mutate：本轮新决定的 id 进入 seen_ids，新决定替换的 id 进入 replacements。
    """
    new_records: list[ContentReplacementRecord] = []
    new_history: list[Message] = []

    for msg in conversation.history:
        if not msg.tool_results:
            new_history.append(msg)
            continue

        decisions: dict[str, str] = {}
        fresh: list[ToolResultBlock] = []

        for tr in msg.tool_results:
            if tr.tool_use_id in state.replacements:
                decisions[tr.tool_use_id] = state.replacements[tr.tool_use_id]
            elif tr.tool_use_id in state.seen_ids:
                decisions[tr.tool_use_id] = tr.content
            elif tr.content.startswith(PERSISTED_TAG):
                # 已被外部（如某些工具本身）打了 persisted-output 标签 — 视为已知决策
                state.seen_ids.add(tr.tool_use_id)
                state.replacements[tr.tool_use_id] = tr.content
                decisions[tr.tool_use_id] = tr.content
                new_records.append(ContentReplacementRecord(
                    tool_use_id=tr.tool_use_id, replacement=tr.content,
                ))
            else:
                fresh.append(tr)

        # Pass 1: single oversized
        persisted_p1: set[str] = set()
        for tr in fresh:
            if len(tr.content) > SINGLE_RESULT_CHAR_LIMIT:
                fp = persist_tool_result(tr.tool_use_id, tr.content, session_dir)
                preview = make_persisted_preview(tr.content, fp)
                decisions[tr.tool_use_id] = preview
                state.replacements[tr.tool_use_id] = preview
                state.seen_ids.add(tr.tool_use_id)
                new_records.append(ContentReplacementRecord(
                    tool_use_id=tr.tool_use_id, replacement=preview,
                ))
                persisted_p1.add(tr.tool_use_id)

        # Pass 2: aggregate
        remaining = [tr for tr in fresh if tr.tool_use_id not in persisted_p1]
        total = sum(len(c) for c in decisions.values()) + sum(
            len(tr.content) for tr in remaining
        )
        if total > AGGREGATE_CHAR_LIMIT:
            ranked = sorted(remaining, key=lambda tr: len(tr.content), reverse=True)
            for tr in ranked:
                if total <= AGGREGATE_CHAR_LIMIT:
                    break
                fp = persist_tool_result(tr.tool_use_id, tr.content, session_dir)
                preview = make_persisted_preview(tr.content, fp)
                old_len = len(tr.content)
                decisions[tr.tool_use_id] = preview
                state.replacements[tr.tool_use_id] = preview
                state.seen_ids.add(tr.tool_use_id)
                new_records.append(ContentReplacementRecord(
                    tool_use_id=tr.tool_use_id, replacement=preview,
                ))
                total -= old_len - len(preview)

        # Freeze remaining fresh as "seen but not replaced"
        for tr in fresh:
            if tr.tool_use_id not in state.replacements:
                state.seen_ids.add(tr.tool_use_id)
                decisions[tr.tool_use_id] = tr.content

        # Materialize new tool_results, preserving original order
        new_tool_results = [
            ToolResultBlock(
                tool_use_id=tr.tool_use_id,
                content=decisions[tr.tool_use_id],
                is_error=tr.is_error,
            )
            for tr in msg.tool_results
        ]
        new_history.append(_copy_message_with_results(msg, new_tool_results))

    # Pass 3: stale snip on the new history (stateless; out-of-scope drift accepted)
    new_history = _snip_stale_messages(new_history)

    new_conv = ConversationManager()
    new_conv.history = new_history
    new_conv.env_injected = conversation.env_injected
    new_conv.ltm_injected = conversation.ltm_injected
    new_conv.last_input_tokens = conversation.last_input_tokens

    return new_conv, new_records


# ---------------------------------------------------------------------------
# Layer 2: Full-conversation summary (Auto-Compact)
# ---------------------------------------------------------------------------

def compute_compact_threshold(context_window: int, manual: bool = False) -> int:
    effective = context_window - SUMMARY_OUTPUT_RESERVE
    margin = MANUAL_COMPACT_SAFETY_MARGIN if manual else AUTO_COMPACT_SAFETY_MARGIN
    return effective - margin


def should_auto_compact(last_input_tokens: int, context_window: int) -> bool:
    return last_input_tokens >= compute_compact_threshold(context_window)


SUMMARY_PROMPT = """\
你是一个对话摘要助手。你只能输出纯文本，不能调用任何工具。

请对下面的对话生成一份结构化摘要。

先在 <analysis> 标签中梳理对话中发生了什么（这部分会被丢弃），然后在 <summary> 标签中输出正式摘要。

<summary> 必须包含以下 9 个部分：

1. **主要请求和意图**：用户到底想做什么
2. **关键技术概念**：讨论过的重要技术点
3. **文件和代码段**：涉及哪些文件，关键代码片段要保留
4. **错误和修复**：遇到了什么错，怎么解决的
5. **问题解决过程**：解决问题的思路和方法
6. **所有用户消息**：用户说过的所有非工具结果的话（原文保留，不可改写！）
7. **待办任务**：还没完成的事
8. **当前工作**：最近在做什么（要最详细）
9. **可能的下一步**：接下来打算做什么

提醒：不要调用任何工具。工具调用会被拒绝，你会失败。只输出纯文本。"""


def extract_summary(llm_output: str) -> str:
    start = llm_output.find("<summary>")
    end = llm_output.find("</summary>")
    if start == -1 or end == -1:
        return llm_output
    return llm_output[start + len("<summary>"):end].strip()


COMPACT_BOUNDARY_MESSAGE = (
    "上面是之前对话的摘要。如果你需要文件的具体内容，"
    "请用 ReadFile 重新读取，不要根据摘要猜测代码细节。"
)


def build_compact_messages(summary: str, attachment: str = "") -> list[Message]:
    user_content = f"[摘要]\n{summary}"
    if attachment:
        user_content += "\n\n---\n\n" + attachment
    return [
        Message(role="user", content=user_content),
        Message(role="assistant", content=COMPACT_BOUNDARY_MESSAGE),
    ]


# ---------------------------------------------------------------------------
# Post-compact recovery state
# ---------------------------------------------------------------------------

# Recovery limits for the attachment block appended to the summary user
# message. Compact wipes the working conversation; without these snapshots
# the model would forget which files it just read and which skill SOPs it
# was operating under.
RECOVERY_FILE_LIMIT = 5
RECOVERY_TOKENS_PER_FILE = 5_000
RECOVERY_SKILLS_BUDGET = 25_000
RECOVERY_TOKENS_PER_SKILL = 5_000
_RECOVERY_CHARS_PER_TOKEN = 3.5


@dataclass
class FileReadRecord:
    path: str
    content: str
    timestamp: float


@dataclass
class SkillInvocationRecord:
    name: str
    body: str
    timestamp: float


class RecoveryState:
    """Per-agent snapshots that survive Layer 2 compaction.

    Tracks the bytes ReadFile returned and the SOP bodies skills were
    invoked with. The recorded data is re-attached to the summary user
    message so the model still has working context after the transcript
    collapses.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._files: dict[str, FileReadRecord] = {}
        self._skills: dict[str, SkillInvocationRecord] = {}

    def record_file_read(self, path: str, content: str) -> None:
        if not path:
            return
        with self._lock:
            self._files[path] = FileReadRecord(
                path=path, content=content, timestamp=time.time()
            )

    def record_skill_invocation(self, name: str, body: str) -> None:
        if not name:
            return
        with self._lock:
            self._skills[name] = SkillInvocationRecord(
                name=name, body=body, timestamp=time.time()
            )

    def snapshot_files(self, limit: int) -> list[FileReadRecord]:
        with self._lock:
            records = list(self._files.values())
        records.sort(key=lambda r: r.timestamp, reverse=True)
        if limit > 0:
            records = records[:limit]
        return records

    def snapshot_skills(self) -> list[SkillInvocationRecord]:
        with self._lock:
            records = list(self._skills.values())
        records.sort(key=lambda r: r.timestamp, reverse=True)
        return records


def _approx_tokens(s: str) -> int:
    if not s:
        return 0
    return int(len(s) / _RECOVERY_CHARS_PER_TOKEN)


def _truncate_by_tokens(s: str, token_budget: int) -> str:
    if token_budget <= 0 or not s:
        return s
    if _approx_tokens(s) <= token_budget:
        return s
    max_chars = int(token_budget * _RECOVERY_CHARS_PER_TOKEN)
    if max_chars <= 0 or max_chars >= len(s):
        return s
    return s[:max_chars] + "\n… (内容已截断)"


def _first_line(s: str) -> str:
    for line in s.split("\n"):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def build_recovery_attachment(
    state: RecoveryState | None,
    tool_schemas: list[Mapping[str, Any]] | None,
) -> str:
    """Render the four-section post-compact attachment.

    Returns "" when nothing worth attaching so the caller can keep the
    summary message clean. `tool_schemas` is expected to be the schemas
    the agent will send on the next request — names + descriptions are
    used to remind the model what's wired up.
    """
    sections: list[str] = []

    if state is not None:
        files = state.snapshot_files(RECOVERY_FILE_LIMIT)
        if files:
            buf = ["## 最近读过的文件\n",
                   "以下快照是文件读取工具上次返回的内容。如需当前字节请重新读取。\n"]
            for rec in files:
                content = _truncate_by_tokens(rec.content, RECOVERY_TOKENS_PER_FILE)
                ts = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(rec.timestamp)
                )
                buf.append(f"### {rec.path}  (read {ts})\n")
                buf.append("```\n")
                buf.append(content)
                if not content.endswith("\n"):
                    buf.append("\n")
                buf.append("```\n")
            sections.append("".join(buf))

        skills = state.snapshot_skills()
        if skills:
            buf = ["## 已激活的技能\n",
                   "下列技能在本会话中被调用过，其触发条件仍然适用。\n"]
            used = 0
            emitted = False
            for sk in skills:
                body = _truncate_by_tokens(sk.body, RECOVERY_TOKENS_PER_SKILL)
                tokens = _approx_tokens(body) + _approx_tokens(sk.name) + 8
                if used + tokens > RECOVERY_SKILLS_BUDGET:
                    break
                used += tokens
                buf.append(f"### {sk.name}\n\n{body}\n")
                emitted = True
            if emitted:
                sections.append("".join(buf))

    if tool_schemas:
        buf = ["## 可用工具\n",
               "你仍然可以调用以下工具，需要时直接发起调用即可：\n"]
        for t in tool_schemas:
            name = t.get("name") if isinstance(t, Mapping) else None
            if not name:
                continue
            desc = t.get("description", "") if isinstance(t, Mapping) else ""
            desc = _first_line(desc or "")
            if desc:
                buf.append(f"- {name} — {desc}\n")
            else:
                buf.append(f"- {name}\n")
        sections.append("".join(buf))

    if not sections:
        return ""

    sections.append(
        "## 提示\n\n以上恢复的上下文是重建的。若需要原文代码、错误信息或用户原话，"
        "请用文件读取工具重新读取，不要根据摘要猜测细节。\n"
    )
    return "\n".join(sections)


def _group_messages_by_turn(messages: list[Message]) -> list[list[Message]]:
    groups: list[list[Message]] = []
    current: list[Message] = []
    for msg in messages:
        current.append(msg)
        if msg.role == "assistant" and not msg.tool_uses:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


@dataclass
class CompactCircuitBreaker:
    max_failures: int = 3
    consecutive_failures: int = field(default=0, init=False)

    def record_failure(self) -> None:
        self.consecutive_failures += 1

    def record_success(self) -> None:
        self.consecutive_failures = 0


    def is_open(self) -> bool:
        return self.consecutive_failures >= self.max_failures


# ---------------------------------------------------------------------------
# Auto-compact orchestrator
# ---------------------------------------------------------------------------

async def auto_compact(
    conversation: ConversationManager,
    client: Any,
    context_window: int,
    session_dir: Path,
    protocol: str = "anthropic",
    manual: bool = False,
    breaker: CompactCircuitBreaker | None = None,
    recovery: RecoveryState | None = None,
    tool_schemas: list[Mapping[str, Any]] | None = None,
) -> CompactEvent | str | None:
    threshold = compute_compact_threshold(context_window, manual=manual)

    if not manual and conversation.last_input_tokens < threshold:
        return None

    if not manual and breaker is not None and breaker.is_open():
        return "自动压缩已熔断（连续失败 3 次），请手动处理或使用 /compact"

    before_tokens = conversation.last_input_tokens
    messages_for_summary = conversation.serialize(protocol)

    summary_messages: list[dict[str, Any]] = [
        {"role": "user", "content": SUMMARY_PROMPT},
    ]
    summary_messages.extend(messages_for_summary)
    summary_messages.append(
        {"role": "user", "content": "请根据以上对话生成结构化摘要。记住：不要调用任何工具。"}
    )

    summary_conv = ConversationManager()
    summary_conv.history = [
        Message(role="user", content=SUMMARY_PROMPT),
    ]
    for msg in conversation.history:
        summary_conv.history.append(msg)
    summary_conv.history.append(
        Message(role="user", content="请根据以上对话生成结构化摘要。记住：不要调用任何工具。")
    )

    max_retries = 3
    llm_output: str | None = None

    for attempt in range(max_retries):
        try:
            from mewcode.tools.base import StreamEnd, StreamEvent, TextDelta

            collected_text = ""
            async for event in client.stream(summary_conv, system=SUMMARY_PROMPT):
                if isinstance(event, TextDelta):
                    collected_text += event.text
                elif isinstance(event, StreamEnd):
                    pass
            llm_output = collected_text
            break

        except Exception as e:
            err_msg = str(e).lower()
            if "prompt" in err_msg and "long" in err_msg or "too many" in err_msg:
                groups = _group_messages_by_turn(summary_conv.history[1:-1])
                drop_count = max(1, len(groups) // 5)
                remaining = groups[drop_count:]
                summary_conv.history = (
                    [summary_conv.history[0]]
                    + [m for g in remaining for m in g]
                    + [summary_conv.history[-1]]
                )
                continue
            if breaker is not None:
                breaker.record_failure()
            return f"摘要生成失败: {e}"

    if llm_output is None:
        if breaker is not None:
            breaker.record_failure()
        return "摘要生成失败：多次重试后仍超出上下文限制"

    summary = extract_summary(llm_output)
    attachment = build_recovery_attachment(recovery, tool_schemas)
    new_messages = build_compact_messages(summary, attachment=attachment)

    conversation.replace_history(new_messages)
    cleanup_tool_results(session_dir)

    if breaker is not None:
        breaker.record_success()

    return CompactEvent(before_tokens=before_tokens)
