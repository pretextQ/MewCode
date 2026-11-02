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
