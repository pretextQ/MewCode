# 维护待办与候选方向

更新：2026-11-03。先核对[当前交付](README.md)及源码；本页记录当前未解决边界，不自动授权实施产品功能、真实模型调用或推送。

## 未解决问题与人工验收

| 事项 | 当前事实 | 后续处理与证据 |
| --- | --- | --- |
| U1 真实配置覆盖事故 | 测试未绑定临时注册表，覆盖真实 `.mewcode/config.yaml`；没有可靠备份，未原样恢复 | 不读取、猜补或用示例配置冒充恢复。只有获得可核实原件才讨论恢复；见[U1 事故记录](acceptance/stage-2026-11-02-borrowing.md#u1-实施记录) |
| Linux tree-service 偶发回收失败 | timeout/cancel 曾检测存活后代；独立复查未稳定复现，根因未确认 | 保留现有断言和诊断，按实际 Linux/Python 环境复核；后续 CI 成功不能视为修复根因。见[U6](acceptance/stage-2026-11-02-borrowing.md#u6-实施记录2026-11-03)及[U7](acceptance/stage-2026-11-02-borrowing.md#u7-实施记录2026-11-03) |
| Windows/Linux GUI 终端人工验收 | 真实 ConPTY/POSIX PTY 自动检查已做；字体/全角字形、鼠标拖选、GUI resize/zoom 重绘与 scrollback 仍待人工 | 按[终端验收清单](acceptance/stage-2026-11-02-terminal.md#仍需实窗人工确认)在两平台记录；自动 PTY、Textual headless 和既有黑色外观认可不替代完整矩阵 |
| 临时测试目录和缓存卷 | U4 清理曾被自动审批拒绝，U6/U7 也有保留的临时快照/卷 | 具体名称和拒绝事实见[台账](acceptance/stage-2026-11-02-borrowing.md)。本轮未核验资源归属，不清理、不声称无残留 |
| 独立审查效果 | 局部覆盖说明、语义评判及候选对比已实施；小样本未证明候选质量提升，大 diff 未完成 | 保留固定源码、原始响应、覆盖与逐条语义复核；正式效果基准及当前版本真实修复→远端 PR 演示需要另定范围与预算，见[比较验收](acceptance/next-stage-2026-10-30.md) |
| 外部协议兼容性 | native/tool_reference、beta header 与 reasoning_content 的端点行为没有完成独立运行时验证 | 保持 MCP legacy 默认及现有 thinking 契约；不凭参考库静态实现判断外部端点已支持，见[原审查边界](review/README.md) |

## 候选能力：尚未授权实施

- token 预算预警：已有的是执行循环预警。容器用量当前在结果回读后门禁，实施前需设计实时用量与跨 attempt 剩余额度传递。
- GitHub 评论/review → rework 闭环、持续问题跟踪：现有人工 decision API 不等于远端评论事件接入。
- PR 发布队列持久化与中断恢复：通知 Outbox 和 execution 记录已完成，不能据此宣称 PR 队列已经交付。
- observe 模式、动态上下文检索、多轮分块审查：仍为提案，默认关闭和仅提供意见的边界保持。
- 可选自定义命令、skill installer、后台记忆整理、浏览器 UI：需单独选择范围；类型化记忆接通不等于后台整理已经实现。
- 更广的注入面治理、更多缺陷回植及正式效果评测：现有最小工程守卫和四项回植已完成；扩展见[测试体系提案](evolution/07-tianshu-test-notes.md)。
- 一次性延迟工具提醒守卫（原 B5）：现有 keyed transient reminder 避免历史累积，但尚未实现工具池不变且提醒仍在时跳过重发；需先量化各加载模式的实际请求，不能照抄上游 token 估算。

其他部署方向继续保留但未承诺排期：更多触发器、跨任务记忆、模型路由、注册管理、多服务实例/全局任务锁、Compose 端口分配、镜像预热、多层配置覆盖。Docker bridge/none 无细粒度出口白名单；镜像/工具可重复性、第三方 readOnlyHint、规模指标、外部服务器退出与取消、Windows 文件锁须按具体环境独立验证。事务去重不等于多服务执行器整体安全。

## 已完成，不重复立项

早期 42 项、R1–R5、R01–R09、N01–N41、最小架构/规模守卫和四项回植、独立审查首期与局部输入修正、监督总线/循环预算预警/dead-end/self-verify/intent anchor、服务可靠性、TUI A/B、A1–A5、U1–U7 均已有实现与交付证据。提交映射见[维护记录](../guides/maintenance.md)，各轮验证见[验收索引](acceptance/README.md)。原始审查中的“待处理”和旧交接中的“下一项”只属于原基线。
