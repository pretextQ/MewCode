# 权限语义说明

MewCode 的权限系统是分层的（Layer 0–3）。本文档说明各层的语义与优先级，
对应实现：`mewcode/permissions/checker.py`。

## 判定顺序

对每次工具调用，`PermissionChecker.check` 按以下顺序判定，命中即返回：

1. **Layer 0 — PLAN 模式工具白名单**：PLAN 模式下只允许只读工具；
   plan 文件写入例外先过沙箱再按 resolve 后的父目录前缀判定。
2. **Layer 1b — 危险命令黑名单**（先于白名单）：`Bash` 类工具先过
   `DangerousCommandDetector`，命中即 deny，与白名单无关。
3. **Layer 1 — 安全命令白名单**：`echo`、`ls`、`git status` 等只读命令
   自动放行。白名单不含任何具备写入/执行能力的命令；含禁用字符
   （`|`、`;`、`&&`、`>`、`<`、`&`、`$(`、反引号、换行、回车）或绝对
   路径目标的命令不进白名单。
4. **Layer 2 — 路径沙箱**：read/write 类工具的所有路径参数
   （`file_path`、`path` 等）逐一过 `PathSandbox`；沙箱外路径在
   DEFAULT 模式下触发 ask 或 deny。
5. **Layer 3 — 用户规则引擎**：用户在配置里声明的 `allow/deny/ask`
   规则。**deny 规则优先于安全白名单**——即使命令在白名单内，命中
   deny 也拒绝。
6. **模式兜底**：DEFAULT 下写类/命令类触发 ask；ACCEPT_EDITS 自动
   放行写文件；BYPASS 跳过 ask（危险检测与规则仍生效）。

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
