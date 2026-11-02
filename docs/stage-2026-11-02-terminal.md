# 2026-11-02 真实终端驱动集成与人工验收边界

日期：2026-11-02（Asia/Shanghai）。起点 `e467524`，master 与实时远端一致；开工已有 `docs/README.md` 的参考库审查入口，原样保留。用户随后授权提交推送本轮终端验收；原有审查入口不夹带，提交 SHA、远端和 CI 以实际 Git/Actions 核验为准。

用户要求先完成“真实 Windows/Linux 终端的完整滚动、缩放、重绘和退出恢复验收”。**本轮完成真实 OS 驱动的 PTY 集成，实窗人工项仍未完成，待办 1 不能全部关闭。**没有修改产品逻辑、调用付费模型、启动真实 Agent/MCP/告警服务或创建远端 PR。

## 本轮已验收

| 检查 | 结果 | 证据含义 |
| --- | --- | --- |
| Windows ConPTY / 正式 NoAltScreenDriver | 32 项通过 | Python 3.14.4、Textual 8.2.8；真实 WindowsDriver 与控制台输入事件 |
| Linux POSIX PTY / 正式 NoAltScreenDriver | 32 项通过 | 本地既有 Linux 镜像、Python 3.12.15、Textual 8.2.8；真实 LinuxDriver、SIGWINCH 与 termios |
| Windows 子进程异常退出 | 6 项通过 | 离线替身以退出码 7 立即结束，正式父启动器仍复位协议与控制台模式 |
| 既有 TUI/详情/加固/视觉/启动器回归 | 47 passed / 20.96 秒 | RuntimeWarning 和 PytestUnraisableExceptionWarning 按错误处理，无警告 |
| Ruff / 工程守卫 / 文档本地链接 / git diff --check | 通过 | 最终工作区检查，未修改锁文件 |

PTY 两平台分别覆盖：长历史、SGR 鼠标滚轮、离底稳定、Ctrl+End、F2/Esc、80×24 / 60×18 / 160×48 / 120×36 四尺寸与输入可见边界、中英全角长路径、权限拒绝与焦点恢复、失败显示、流式期间不强拉到底部、第一次 Ctrl+C 停止运行、空闲 Ctrl+C 退出、父终端恢复后的中文输入与回显。检查启动前哨兵输出和无备用屏进入序列；这不是 GUI scrollback 拖动回看的证明。

Windows 正常与异常退出均测得输入模式 **503 → 503**、输出模式 **7 → 7**；Linux 正常退出前后完整 termios 相等。两平台退出输出均含关闭 SGR 鼠标和恢复光标序列，父终端收到完整 `recovery-中文`。这些证明协议与输入模式恢复，不证明真实鼠标在 GUI 窗口里移动时的表现。

Linux 使用既有 `mewcode-sandbox-live-agent:9096280f3277`，关闭网络、只读根文件系统及源码挂载，丢弃 capabilities，使用临时 `/tmp` 与单独可写证据目录。它是 Linux 内核的真实 PTY，不是宿主目标 mypy，也不是 Linux 桌面终端模拟器。仅有 docker-desktop WSL；没有假称安装或验证普通 Linux 桌面。

## 可复跑入口

`tests/helpers/tui_terminal_smoke.py` 使用真实控件、样式和正式 NoAltScreenDriver；初始化、Agent、模型、记忆、会话写盘、MCP 与工具执行均以离线替身隔离。输入 `history`、`stream`、`permission`、`failure` 或普通文本进行检查；`crash` 仅用于有父启动器保护的异常退出实验。Bash 工具行是明确标注的替身事件，不能视为真实命令或测试通过。

Windows（在项目根目录）：

```powershell
uv run --locked --with pyte==0.8.2 --with pywinpty==3.1.2 python scripts/verify_terminal_smoke.py --platform windows --root docs/evidence/terminal-local
uv run --locked --with pyte==0.8.2 --with pywinpty==3.1.2 python scripts/verify_terminal_smoke.py --platform windows --crash --root docs/evidence/terminal-abrupt
```

Linux（已安装项目依赖的 Linux checkout）：

```bash
uv run --locked --with pyte==0.8.2 python scripts/verify_terminal_smoke.py --platform linux --root docs/evidence/terminal-local
```

pyte 仅解析和保留 VT 文字屏幕，不参与产品绘制，不是终端字体或截图证据。Windows runner 自动生成本地 uv 替身，使**原样的** `scripts/run_tui.ps1` 启动离线 fixture；没有修改正式入口。重复运行保留旧尝试的完整文件，不把旧恢复标记当作新结果。

## 仍需实窗人工确认

在真实 Windows Terminal 或独立 Linux 桌面终端中执行下面的离线入口即可检查，不需要 API key 或模型预算：

```bash
uv run --locked python -m tests.helpers.tui_terminal_smoke --evidence docs/evidence/terminal-manual
```

Windows 若需同时检查正式父启动器，先运行上述 Windows runner，再在真实终端执行其生成的 `docs/evidence/terminal-local/windows/launch.ps1`。本轮本机已生成的入口为 `docs/evidence/tui-terminal-2026-11-02/windows/launch.ps1`，它只启动离线替身。退出后按提示输入任意恢复标记并回车，输入不会作为 shell 命令执行。

| 人工项目 | Windows Terminal | 独立 Linux 终端 |
| --- | --- | --- |
| 中文、全角与英文真实字体、长路径/命令换行 | 待验 | 待验 |
| 鼠标滚轮、Shift 拖选/复制与离底阅读 | 待验 | 待验 |
| 反复拖动窗口尺寸、GUI 字号缩放、切回后的重绘 | 待验 | 待验 |
| 终端自身 scrollback 回看启动前哨兵 | 待验 | 待验 |
| stream 中 Ctrl+C 停止、空闲 Ctrl+C 退出；随后移动鼠标、输入中文，确认无协议碎片 | 待验 | 待验 |

实窗限制来自已读取的 [computer-use SKILL.md](C:/Users/32519/.codex/plugins/cache/openai-bundled/computer-use/26.1027.30824/skills/computer-use/SKILL.md) 及其要求阅读的 [guidance.md](C:/Users/32519/.codex/plugins/cache/openai-bundled/computer-use/26.1027.30824/docs/guidance.md)：后者明确规定 “Do not automate terminal applications such as Windows Terminal, Command Prompt, or Windows PowerShell.” 窗口工具也未暴露目标终端控制面。没有通过其他 GUI 自动化手段绕过，也没有用协议结果代替人工结果。

## 原始证据与失败记录

本轮证据在忽略的 `docs/evidence/tui-terminal-2026-11-02/`：`windows/`、`linux/`、`abrupt/windows/` 各自含 `checks.json`、`events.jsonl`、`terminal.raw`、VT 文字快照、布局状态与恢复标记；Windows 另有 `console-modes.json`，Linux 有 `termios.json`。历史尝试保存在 attempts 或带 previous 时间戳的文件；不将失败覆盖成成功。核心来源以本轮最终 helper/runner 的 SHA-256 清单追溯。原始证据不随普通克隆取得。

验收入口开发期间修正了三处测试设施问题：Windows PowerShell 5 将无 BOM 的中文脚本误读；Textual 挂载分发重复初始化覆盖假 Agent；Linux Textual 使用 stderr 输出，最初重定向令 VT 捕获不完整。VT 解析器也改为按真实光标位置回答终端查询，不固定回报 1,1。这些是 fixture/runner 修正，没有认定为产品缺陷。最终两个完整平台运行和 Windows 异常退出运行全部通过。

可选验收依赖装在忽略的证据目录（pyte 0.8.2、pywinpty 3.1.2、wcwidth 0.9.4），产品依赖与锁文件未改动。没有为本轮测试扩展历史真实模型授权，也没有重复完整服务/Docker 安全套件。

会话收尾确认无验收 Python/PowerShell/Windows Terminal 子进程和验收容器残留。Docker Desktop 起初未运行，本轮为 Linux PTY 检查启动；确认无其他运行容器后停止，恢复原状态。
