# Windows 平台已知边界

Windows 是 MewCode 的一等公民运行平台（CI 在 windows-latest 与
ubuntu-latest 双平台跑全量测试）。本文记录平台相关的行为约定与已知边界。

## 命令 shell

`Bash` 是兼容保留的工具名，不代表所有平台都运行 Bash。Windows 命令工具和
command hook 使用系统命令 shell（`COMSPEC`，通常为 `cmd.exe`）；POSIX 使用
`/bin/sh`，不保证支持 Bash 扩展。模型的工具描述和环境提示会给出实际平台契约。
Windows 使用 `cd`、`type` 等 cmd 语法，不能假设 `pwd`、`cat`、`sleep` 存在；
PowerShell 命令必须显式调用 PowerShell。带空格的解释器和路径需要引号。
不自动安装或根据 PATH 上的 Bash 改变执行 shell；权限规则与危险检测继续生效。

## 编码

- 文件读写不走系统默认编码：`tools/base.py` 的 `detect_encoding` 按
  BOM → utf-8 → locale（win32 额外尝试 `mbcs`/ANSI 代码页，中文系统
  为 GBK）→ utf-8 宽松解码兜底。
- GBK 文件读取与编辑保持原编码写回；不可解码字节以 U+FFFD 兜底，
  不让工具在混合编码文件上崩掉。
- 子进程环境按白名单继承（`PATH`、`SystemRoot`、`COMSPEC`、`TEMP`、
  `TMP`、`HOME`、`USERPROFILE`、`APPDATA`、`LOCALAPPDATA`、
  `PROGRAMFILES`、`LANG`、`LC_ALL`）——缺 `SystemRoot`/`COMSPEC` 会让
  `npx` 等 stdio MCP server 起不来；白名单外的主机变量（含各类 API
  key）不会泄漏给 MCP 子进程。

## 行尾

- 文件工具以 `newline=""` 读写（`read_text_preserve` /
  `write_text_preserve`）：LF-only 文件编辑后仍是 LF，git diff 不会
  出现全文件行尾变更；CRLF 文件保持 CRLF。
- 仓库内的 Python 源码与测试以 LF 提交；Windows checkout 时 git 的
  `core.autocrlf` 转换不影响运行时行为。

## 进程与阻塞 I/O

- 命令工具、command hook、宿主验证与运行时命令共用 `processes.py` 的生命周期管理。
  POSIX 使用独立进程组；Windows 在启动真实命令之前加入独立 Job Object，
  超时或取消时回收整个进程树，即使原命令父进程已经退出也能定位后代。
  Windows 命令会等待其 Job 内后代退出，后代同样受本次命令的超时约束；
  这不是 OS 沙箱，不能代替容器隔离。
- 清理有明确等待上限，重复取消不会打断回收；正常命令的输出和退出码保持不变。
  Windows 实现依据 [Job Object 进程归属](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject)
  和 [整组终止](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-terminatejobobject) 的原生接口契约。
- 危险命令黑名单区分 POSIX 与 Windows（`Remove-Item -Recurse -Force`、
  `reg add ...\CurrentVersion\Run`、`certutil -urlcache`、
  `powershell -enc` 等各有模式）。
- 大目录 grep/glob、文件读取、worktree 清理等阻塞 I/O 一律走
  `asyncio.to_thread`，不冻结 TUI 事件循环。
- 后台 stdio 子进程（MCP server）通过 `AsyncExitStack` 管理生命周期，
  `connect`/`close` 串行化，避免并发连接泄漏进程句柄。

## 已知边界

- tmux/iTerm2 的 teammate 后端在 Windows 上不可用（`teammate_mode`
  用 `in-process`）。
- `Path.resolve()` 在大小写不敏感的文件系统上可能与字面路径大小写
  不一致；沙箱与 FileStateCache 均以 resolve 后的绝对路径为键。
- 事件循环里禁止同步文件 I/O（测试有事件循环饥饿的回归防护）。
