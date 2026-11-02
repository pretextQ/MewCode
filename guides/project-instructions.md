# 项目指令

启动时所有入口共用 `load_instructions(work_dir)`，按下列顺序加载，后面的文本约定为更具体的规则：

1. 全局 `~/.mewcode/MEWCODE.md`、`~/.mewcode/AGENTS.md`。
2. 从最近 Git 根到运行目录逐级加载 `MEWCODE.md`、`AGENTS.md`、`.mewcode/MEWCODE.md`。
3. 运行目录 `MEWCODE.local.md`。

`.git` 目录和 worktree 的 `.git` 文件均可识别。没有 Git 根时只读取运行目录，
不收集外部父目录指令。顺序是给模型的约定，不保证模型一定遵循。相较旧版，
全局指令移到项目前，并新增 AGENTS.md、目录链和本地覆盖文件。

每段标明真实来源，同一真实文件只读取一次。独立子代理按自己的运行目录加载，
再附加角色提示；fork 继承已有上下文。独立 pane worker 通过经认证的 bootstrap
接收已准备好的指令，旧 payload 则在 worker 自身运行目录加载。正常轮次不重复注入；
不根据此后任意编辑目标动态加载其子目录规则。

支持独立一行的相对 include，例如：

```text
@include docs/coding-rules.md
```

include 相对所在文件解析。项目 include 限于仓库根（无 Git 时为运行目录），
全局 include 限于全局 `.mewcode`。绝对路径、`~`、跨范围链接均拒绝；
Markdown 反引号/波浪线围栏中的示例不会展开。环和重复文件被跳过，缺失、解码失败、
读取失败及预算耗尽输出简短诊断，不使启动失败。

预算：包含深度最多 5 层，单文件最多 64 KiB，总读取最多 256 KiB（UTF-8 字节），
最多 128 个真实文件，Git 根查找最多 64 级。过大的文件整份跳过，耗尽后不再读取；
找不到预算内 Git 根时只加载运行目录并提示。预算包括主指令和 include，
来源标签/简短诊断可能增加少量输出。示例不包含任何凭据。
