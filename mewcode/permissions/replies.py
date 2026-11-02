"""Permission replies shared by interactive and legacy callbacks."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

from mewcode.tools.base import ToolResult

MAX_FEEDBACK_CHARS = 1000


class PermissionResponse(Enum):
    ALLOW = 'allow'
    DENY = 'deny'
    ALLOW_ALWAYS = 'allow_always'


@dataclass(frozen=True)
class PermissionReply:
    response: PermissionResponse
    feedback: str = ''


def clean_feedback(text: str) -> str:
    if not isinstance(text, str):
        return ''
    text = text[:MAX_FEEDBACK_CHARS * 4]
    text = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', text)
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    text = text.replace('\r', ' ').replace('\n', ' ')
    return ''.join(c for c in text if unicodedata.category(c) not in ('Cc', 'Cf', 'Cs'))[:MAX_FEEDBACK_CHARS].strip()


def normalize_reply(reply: PermissionResponse | PermissionReply) -> PermissionReply:
    if isinstance(reply, PermissionResponse):
        return PermissionReply(reply)
    if not isinstance(reply, PermissionReply) or not isinstance(reply.response, PermissionResponse):
        return PermissionReply(PermissionResponse.DENY)
    feedback = clean_feedback(reply.feedback) if reply.response == PermissionResponse.DENY else ''
    return PermissionReply(reply.response, feedback)


def denial_result(reply: PermissionReply) -> ToolResult:
    text = 'Permission denied: 用户拒绝了此操作'
    if reply.feedback:
        text += '\nUser feedback: ' + reply.feedback
    return ToolResult(output=text, is_error=True)
