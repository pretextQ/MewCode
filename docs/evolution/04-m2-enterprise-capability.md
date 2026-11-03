# M2：隔离执行与上下文能力

更新：2026-10-30。M2 已完成；“企业能力”表示设计方向，不表示真实企业部署。

## Docker / Compose

默认启用沙箱并禁止隐式宿主回退。Agent 与验证在容器内，依赖环境由服务编排；Compose 测试按服务名寻址，清理包含失败与取消路径。integration_skipped 不算通过。

非 root、只读根、资源限额和挂载约束构成主要隔离；进程树清理与 worktree 不能替代权限沙箱。网络只有 bridge/none，无细粒度出口白名单；基础镜像可重复性、各语言依赖支持和宿主端口冲突仍需具体评估。

## Skills / MCP

未配置 skills 使用默认包，明确 [] 表示不注入。普通 skill 可以仓库覆盖，服务 org-code-style 自查清单固定取内置版本并注明来源；Agent 自查不等于人工结论。

MCP 会话隔离管理生命周期。服务筛选只读工具，但第三方 readOnlyHint 不能作为独立可信保证；内置日志与 CI server 走 GET 查询。

## 当前证据

2026-10-30 Docker 真机专项 15 passed，覆盖容器内 Agent/MCP、Compose 正例与失败清理等；此专项使用假 LLM 和测试后端。详情见[验收](../acceptance/acceptance-2026-10-30.md)。

历史真实模型、演示 PR、规范对照及集成负控制见[历史记录](../history/evolution-2026-10-05.md)。旧的自动宿主降级描述已被 R1 修复替代。最新代码版本未重跑真实模型完整远端修复演示。
