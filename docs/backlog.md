# 维护待办与候选方向

更新于 2026-11-27。先核对[当前交付](README.md)及源码；本页记录当前未解决边界，不自动授权实施产品功能、真实模型调用或推送。

## 未解决问题与人工验收

| 事项 | 当前事实 | 后续处理与证据 |
| --- | --- | --- |
| U1 真实配置覆盖事故 | 测试未绑定临时注册表，覆盖真实 `.mewcode/config.yaml`；没有可靠备份，未原样恢复 | 不读取、猜补或用示例配置冒充恢复。只有获得可核实原件才讨论恢复；见[U1 事故记录](acceptance/stage-2026-11-02-borrowing.md#u1-实施记录) |
| Windows/Linux GUI 终端人工验收 | 真实 ConPTY/POSIX PTY 自动检查已做；字体/全角字形、鼠标拖选、GUI resize/zoom 重绘与 scrollback 仍待人工 | 按[终端验收清单](acceptance/stage-2026-11-02-terminal.md#仍需实窗人工确认)在两平台记录；自动 PTY、Textual headless 和既有黑色外观认可不替代完整矩阵 |
| 服务验证覆盖范围 | pytest 范围审计适用于可识别的单测命令。分布式收集和集成验证的前后范围在检查范围之外 | 11 月 18 日验收使用 GitHub API、集成验证和 Docker 传输替身。真实 Docker 集成依赖与真实 GitHub PR 仍待专项验收。[范围契约](../guides/service-reliability.md#pytest-验证范围) · [专项验收](acceptance/stage-2026-11-18-service-verification.md) |
| 临时测试目录和缓存卷 | U4 清理曾被自动审批拒绝，U6/U7 台账也记录了保留的临时快照和卷 | 核对台账所列资源的归属和现存状态后再处理。[交付台账](acceptance/stage-2026-11-02-borrowing.md) |
| 独立审查效果 | 局部覆盖说明、语义评判及候选对比已实施；小样本未证明候选质量提升，大 diff 未完成 | 保留固定源码、原始响应、覆盖与逐条语义复核；正式效果基准及当前版本真实修复→远端 PR 演示需要另定范围与预算，见[比较验收](acceptance/next-stage-2026-10-30.md) |
| 外部协议兼容性 | 当前 MCP 加载提供 `legacy`、`eager`、`dispatch` 和 `auto`。`native/tool_reference` 以及外部端点的 beta header、`reasoning_content` 行为还缺少独立的运行时验证 | 有实际端点需求时，按其协议与现有 thinking 行为验证。[MCP 加载指南](../guides/mcp-loading.md) · [原审查边界](review/README.md) |

## 候选能力：尚未授权实施

- token 预算预警：已有的是执行循环预警。容器用量当前在结果回读后门禁，实施前需设计实时用量与跨 attempt 剩余额度传递。
- GitHub 评论或评审触发返工及持续问题跟踪。现有返工由 `POST /jobs/{job_id}/decision` 接收人工决定。[决定接口](../guides/architecture.md) · [审查路线](evolution/05-pr-review-improvements.md)
- PR 发布队列持久化与中断恢复。现有 SQLite Outbox 保存通知意图和投递回执，execution 记录追踪执行过程。[通知与执行记录](../guides/service-reliability.md)
- 全只读 observe 模式、动态上下文检索和多轮分块审查仍为提案。现有独立审查默认关闭，启用后提供意见供参考。[服务范围](../guides/service-reliability.md#迁移与范围) · [独立审查](../guides/independent-review.md)
- 可选自定义命令、skill installer、后台记忆整理、浏览器 UI：需单独选择范围；类型化记忆接通不等于后台整理已经实现。
- 更广的注入面治理、更多缺陷回植及正式效果评测：现有最小工程守卫和四项回植已完成；扩展见[测试体系提案](evolution/07-tianshu-test-notes.md)。
- 一次性延迟工具提醒（原 B5）。有延迟工具时，请求携带按 `deferred-tools` 键更新的临时提醒。工具池未变时跳过重发属于候选，需先测量各加载模式的实际请求。[MCP 加载指南](../guides/mcp-loading.md) · [对照审查](review/README.md)

其他部署方向继续保留但未承诺排期：更多触发器、跨任务记忆、模型路由、注册管理、多服务实例/全局任务锁、Compose 端口分配、镜像预热、多层配置覆盖。Docker bridge/none 无细粒度出口白名单；镜像/工具可重复性、第三方 readOnlyHint、规模指标、外部服务器退出与取消、Windows 文件锁须按具体环境独立验证。事务去重不等于多服务执行器整体安全。

## 已完成，不重复立项

早期 42 项、R1–R5、R01–R09、N01–N41、最小架构/规模守卫和四项回植、独立审查首期与局部输入修正、监督总线/循环预算预警/dead-end/self-verify/intent anchor、服务可靠性、TUI A/B、A1–A5、U1–U7 均已有实现与交付证据。提交映射见[维护记录](../guides/maintenance.md)，各轮验证见[验收索引](acceptance/README.md)。原始审查中的“待处理”和旧交接中的“下一项”只属于原基线。

11 月 18 日已交付本地验证绑定 PR 发布提交、pytest 范围审计及重复失败的结构化诊断。覆盖范围与仍待验收的场景见上表。[服务验证验收](acceptance/stage-2026-11-18-service-verification.md)

11 月 27 日复现并修复了 Linux 进程回收测试的存活误判。早期 U6/U7 的 tree-service 失败缺少当时的进程状态，仍无法逐次归因。本次专项复跑通过，后续失败会保留断言所用的同一次状态采样。[进程回收专项验收](acceptance/stage-2026-11-27-process-cleanup.md)
