# 权限语义说明

MewCode 的权限系统是分层的（Layer 0–3）。本文档说明各层的语义与优先级，
对应实现：`mewcode/permissions/checker.py`。

## 判定顺序

对每次工具调用，`PermissionChecker.check` 按以下顺序判定，命中即返回：

1. PLAN 模式先处理特定协作工具白名单。
2. command 类工具先做危险命令检测，命中即拒绝。
3. read/write 类工具的实际路径经过 PathSandbox，越界直接拒绝。
4. 写类工具先检查配置/技能写保护，再处理 PLAN 文件写入例外。
5. 用户规则按优先级匹配，规则先于安全命令白名单。
6. 只读命令白名单自动放行；shell 控制符、环境变量展开、UNC 等可疑语法回到模式判定。
7. 模式兜底：DEFAULT 下写类/命令类需要确认；ACCEPT_EDITS 自动放行写文件；
   DONT_ASK 与 BYPASS 会放行命令类，危险检测和显式 deny 规则仍生效。

规则仅支持 `allow` / `deny`，按文本大小写敏感匹配，在 Windows 与 POSIX 上保持一致。
“总是允许”会为 Bash 保存完整命令的字面匹配规则；旧的通配 allow 规则不能授权链式命令、重定向或展开。
规则文件修改后按文件元数据刷新解析缓存。

## 并行批次

并行执行的多个工具与串行路径走**同一套**权限检查与 hooks——并行不会
绕过任何一层（deny、ask 升级、pre_tool_use reject 在并行批次内同样生效）。

交互与无人值守入口都按 `can_run_concurrently(arguments)` 切分相邻批次；旧工具默认仅兼容
`is_concurrency_safe is True`，不会把同名方法对象当作放行。写操作、普通/未知命令和 MCPCall/dispatch
目标是顺序屏障。结果按提交顺序和原 tool_call ID 返回；取消或异常会等待已拥有任务与子进程收尾。
pre hook 拒绝的调用不执行工具或 post hook，串行/并行一致；其他已有门禁语义保留。

Shell 并发判定独立于权限自动放行，没有增加权限白名单。当前只接受：

| 平台 | 可进入相邻并发批次的形式 |
| --- | --- |
| POSIX `/bin/sh` | `pwd`、`cat relative-file` |
| Windows COMSPEC（通常 cmd.exe） | `cd`（无参数）、`type relative-file` |
| 两者 | `git status` 及下述受控选项 |

文件参数限单个简单 ASCII 相对路径，禁止 `..`、设备名、引号/空格路径及未知选项。
Git status 仅接受 `--short`、`--porcelain`、`--porcelain=v1/v2`、`--branch`、
`--untracked-files=no/normal/all`、`--no-renames`，最多八个不重复选项；不接受路径、别名、全局
`-c/-C`、pager/外部 diff 参数。Shell 链式/管道、展开、环境赋值、重定向、转义、通配和无法可靠
解析的语法均串行；未识别不表示授权或拒绝，仍由原有权限与执行规则决定。

受控 Git status 以 argv 执行：先只枚举 filter 键名（不读取/展示命令值），最多 3 秒、16 KB、32 个
简单 driver 名，覆盖参数最多 12 KB；无法安全检查则拒绝该次查询，不降级为普通 Shell。
查询禁用 clean/process filter、fsmonitor、pager、外部 diff、optional index locks，强制
`--ignore-submodules=all`，不显示子模块内部状态。它与 TUI Git 预览复用 filter 校验；索引不写回。
这不保证任意 PATH 程序或 Shell 命令成为 OS 沙箱，未知 Git 形式仍按原普通命令串行执行。

Windows 的 `cd/type` 是否询问仍按原权限规则，不能从“可并发”推断“自动允许”。

## Shell 退出结果

正常完成的 Bash 输出第一行固定为 `Exit code: <数值>`，之后是原 stdout/stderr。
0 表示命令自身返回成功，不代表验证覆盖充分。未知命令、测试失败及复合命令的非零退出仍是错误。
仅对严格识别的单条 `rg/grep [已知查询选项] 简单pattern 相对文件` 无匹配，及
`diff [-u/-q/--unified/--brief] 相对文件 相对文件` 有差异，将退出 1 标为非错误；2/7 等仍为错误。
不根据名称前缀放宽任意参数，不将全部非零改成成功，复杂引号/模式保守按错误返回。

超时仍是错误，已启动进程显示回收后的数值退出状态并标注 timeout termination；未能启动或 Git
预检失败明确显示 unavailable，不补造 0。取消没有完成结果，副作用仍可能未知，原请求配对修复继续工作。
保留最长 600 秒、stdout/stderr 合计 2 MiB 捕获上限、编码回退及工具上下文展示预算；退出码位于输出开头，
不会被普通长输出挤到截断尾部。TUI/恢复历史仍以纯文本显示这些结果。

## 子代理

- 子代理权限取「父模式与代理定义的更严格者」；后台任务固定
  DONT_ASK 下限（不会拿到 BYPASS）。
- 父模式为 PLAN 时，子代理工具集强制过滤为只读。
- 子代理继承父代理的 RuleEngine 实例——用户 deny 规则对子代理同样生效。
- fork 型 skill 非交互运行：权限取 DONT_ASK 下限，但危险检测、规则、
  沙箱全部继承父级；ask 一律拒绝。

## 安全命令白名单示例

| 放行 | 拒绝/确认 |
|---|---|
| `ls`、`pwd`、`git status`、`cat README.md` | `find . -name "*.py" -delete` |
| `python --version` | `sed -i 's/x/y/' C:/Users/x/f.txt` |
| | `tee 任意文件`、`npx <包名>`、`awk 'BEGIN{system(...)}'` |
| | `echo hi\nrm -rf ~`（换行注入） |

MCP 工具同样受到 agent 定义的 `tools` / `disallowed_tools` 限制，支持完整名称或通配模式。
PLAN 子代理不会获得 MCP 工具。

## 配置与技能写保护

普通 Agent 文件工具不能写项目及全局 `~/.mewcode` 下的 `config.yaml`、
`permissions.yaml`、`permissions.local.yaml` 和 `skills/`（含目录本身及后代）。
所有权限模式、显式 allow 和 PLAN 写例外均不能豁免。相对路径以沙箱项目根为准，
文件工具须绑定同一运行目录；保护同时比较路径名称和解析后的真实目标，覆盖不存在目标、
父目录链接及 Windows 大小写、分隔符和常见 Win32 路径别名。

该检查只限制文件写入，不额外限制授权读取，也不是任意 Shell 或插件代码的 OS 沙箱。
计划、自动记忆、会话及普通源码不在保护清单内。“始终允许”通过 RuleEngine 的专用
管理接口保存规则，现有 `/skill list|info|reload` 只读取和注册技能，不安装或改写定义。
运行中恶意文件系统竞态、硬链接与任意命令写入不在这层路径检查的保证范围内。

## 拒绝时附带反馈

内联授权框移到 No 后可以输入或粘贴可选反馈，最多 1000 个 Unicode 字符；
超长粘贴保留可用长度，终端控制序列和隐藏控制字符会被移除。Enter 提交拒绝与反馈，
Escape 无反馈拒绝，Ctrl+C 仍取消当前运行并走原有收尾。切换到允许选项时清除反馈草稿。
输入区在授权框关闭/取消后恢复。详情将反馈作为纯文本显示，不解析 Rich markup。

反馈只附到该次真实拒绝的错误 tool_result，并随原有下一次请求送给模型；
不新增用户轮次或额外模型请求，不表示替代操作获得授权。无人值守拒绝不虚构反馈。
内部回调支持 `PermissionReply(response, feedback='')`，仍兼容旧 `PermissionResponse`；
共享规范化只保留 DENY 的反馈，其他选择丢弃文本。原来的“始终允许”持久化接口继续工作。
