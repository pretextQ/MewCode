# M2：隔离执行与上下文能力

M2 已交付。以下记录隔离执行与上下文能力，部署证据来自所列的测试和演示环境。[架构与生命周期](../../guides/architecture.md)

## Docker / Compose

默认启用沙箱并禁止隐式宿主回退。Compose 依赖由宿主容器运行时按已校验、冻结的配置拉起。集成测试命令在沙箱容器内运行，加入该任务的依赖网络，按服务名寻址。清理覆盖失败与取消。沙箱不可用或关闭时，如实记录 `integration_skipped`。

非 root、只读根、资源限额和挂载约束构成主要隔离。常规沙箱网络配置支持 `bridge` 和 `none`，服务没有细粒度域名出口白名单。基础镜像可重复性、各语言依赖支持和宿主端口冲突仍需按部署环境评估。[维护待办](../backlog.md)

## Skills / MCP

未配置 skills 使用默认包，明确 [] 表示不注入。普通 skill 可以仓库覆盖，服务 org-code-style 自查清单固定取内置版本并注明来源；Agent 自查不等于人工结论。

MCP 会话独立管理连接和清理。服务按工具提供方的 `readOnlyHint` 声明筛选只读工具。内置日志与 CI server 使用 GET 查询。服务默认 `legacy`，可显式设置其他加载模式。[MCP 加载指南](../../guides/mcp-loading.md)

## 验收证据

2026-10-30 Docker 真机专项 15 passed，覆盖容器内 Agent/MCP、Compose 正例与失败清理等；此专项使用假 LLM 和测试后端。详情见[验收](../acceptance/acceptance-2026-10-30.md)。

2026-11-18 的验收覆盖服务验证源码树和 pytest 范围，Docker 传输和 GitHub API 均使用替身。早期真实模型、演示 PR、规范对照与集成负控制见历史索引中的本地档案。[11 月 18 日验收](../acceptance/stage-2026-11-18-service-verification.md) · [历史索引](../history/README.md)
