# Linux 与 Windows 图形终端验收

2026-11-27，从 `5ec41a3` 的独立源码快照运行 [离线验收 fixture](../../tests/helpers/tui_terminal_smoke.py)。界面使用实际 `NoAltScreenDriver`。输入内容和流式响应由 fixture 生成。

## Linux XTerm

Debian 容器在 Linux WSL2 6.18.40.1 内核运行 XTerm 379、Openbox 3.6.1 与 Xvfb 图形显示。验收通过窗口键鼠事件、实际窗口截图、夹具状态和退出后回执核对。

| 检查 | 实窗证据 |
| --- | --- |
| 字形和换行 | 中英混排、全角字符、含空格的路径和长命令可读，长命令在窗口缩窄后继续换行。 |
| 滚轮和选择 | 滚轮上移显示回到最新入口。Shift 拖选可从 X11 PRIMARY 读出 `历史 40 · 中文 English`。流式输出期间滚动位置保持 133，历史总长继续增长。 |
| 尺寸、缩放和切回 | Openbox 鼠标拖动使界面从 120×36 变为 86×27。XTerm 字号放大和缩回改变窗口像素尺寸，输入区仍可见。最小化并切回后画面重绘。 |
| 终端历史滚动区 | 退出后使用 XTerm 的 Shift+PageUp 回看到启动前的 `MEWCODE_SMOKE_SCROLLBACK_BEFORE`。 |
| 停止、退出和恢复 | 流式运行中 Ctrl+C 记录已停止，空闲时再次 Ctrl+C 正常退出。退出前后 termios 相同，移动鼠标未在输入行留下协议字符，中文恢复标记 `recovery-中文` 完整回显。 |

## Windows Terminal

Windows Terminal 1.25.3131.0 的离线夹具回执记录了 120×30 与 156×41 两个尺寸、离底阅读、`stream`、已停止和正常退出。输入模式 **503 → 503**，输出模式 **7 → 7**。本次回执没有采集到预期中文恢复标记。字形和换行、Shift 拖选、字号缩放与重绘、历史滚动区及退出后鼠标行为待实窗操作者报告。

Linux 截图、X11 选择文本、夹具状态和 termios 证据保存在本地忽略目录 `docs/evidence/service-and-terminal-2026-11-27/linux/`。[原终端验收矩阵](stage-2026-11-02-terminal.md#仍需实窗人工确认)
