from __future__ import annotations

from enum import StrEnum


class LifecycleEvent(StrEnum):
    # Session level
    SESSION_START = "session_start"
    SESSION_END = "session_end"


    # Turn level
    TURN_START = "turn_start"
    TURN_END = "turn_end"


    # Tool level
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"

    # Message level
    PRE_SEND = "pre_send"
    POST_RECEIVE = "post_receive"

    # System level
    STARTUP = "startup"
    SHUTDOWN = "shutdown"
    ERROR = "error"
    COMPACT = "compact"
    PERMISSION_REQUEST = "permission_request"
    FILE_CHANGE = "file_change"
    COMMAND_EXECUTE = "command_execute"

