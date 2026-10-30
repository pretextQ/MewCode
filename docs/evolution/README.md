# 演进与验证证据


2026-10-30 下一阶段更新：A 审查范围及语义评估已实现，真实候选对比未证明效果提升；B 最小监督内核已实施。见[新增验收](../next-stage-2026-10-30.md)。历史验收数字保留。
更新：2026-10-30。当前进度以[总入口](../README.md)为准，当前检查结果见[本次验收](../acceptance-2026-10-30.md)。

## 里程碑

| 阶段 | 已实现内容 | 状态 |
| --- | --- | --- |
| M1 | 告警接入、事务去重、独立 worktree、修复验证、PR 与人工决策入口 | 已完成 |
| M2 | Docker、Compose、Skills、只读 MCP | 已完成；本次真机专项 15 passed |
| M3 | 指标、token 成本、多仓策略、回放 | 已完成 |
| 阶段 1 | AST 架构守卫、规模预算、四项隔离回植 | 已完成，提交 2fb8b06 |
| 阶段 2 | 固定提交独立审查、diff 覆盖与证据、预算、PR/job 展示 | 首期已完成，提交 a172581；默认关闭 |
| 阶段 3 首期 | 纯 Python 监督总线、送达及 expect、多模式配置 | 已实现，默认关闭；无 detector |
| 后续 | 四类 detector、GitHub 返工与问题跟踪 | 未实施 |

早期 42 项及 R1–R5、R01–R09、N01–N41 均已完成，原始审查不是待修清单。提交映射见[维护记录](../../guides/maintenance.md)。

本期增量：全量 1475 passed / 19 skipped；Docker 单独 16 passed；Ruff、两平台目标 mypy 各 167 文件、工程守卫、四项回植、锁文件、子代理 86/86 与构建通过。

## 历史验证（本期增量见文首）

2026-10-30 基线 4cef69f：1439 passed / 18 skipped；单独启用 Docker 专项 15 passed；Ruff、Windows/Linux 目标全包 mypy（164 源文件）、锁文件、工程守卫、四项回植、子代理、依赖审计与构建通过。基线远端 CI 五作业全绿。

五例真实模型审查中，前三个正例结论有效、负例未误报，大 diff 部分覆盖产生了错误理由。不能把存在性评分的 100% 当成有效准确率，详情及费用见验收报告。

## 历史回放：2026-10-05

演示环境两轮，各含 config_error、null_deref、unhandled_timeout 三例；对应 PR #12–#17。本节是历史结果，不是新版本重新测量。

| 指标 | 基线 A | 规则调整 B |
| --- | --- | --- |
| 到达 human_review | 3/3 | 3/3 |
| alert → PR 中位 / p90 | 25 / 29 秒 | 26 / 43 秒 |
| 输入 / 输出 token | 19862 / 7297 | 18197 / 9060 |
| token 合计 | 27159 | 27257 |
| 追溯标记命中 | 0/3 | 3/3 |

本地原始证据：[基线报告](replay/baseline-a.md)、[基线 JSON](replay/baseline-a.json)、[调整后报告](replay/after-rule-b.md)、[调整后 JSON](replay/after-rule-b.json)。

这六单均为 direct 路径，集成验证记录 integration_skipped。历史 Docker/Compose 真机证据是另一组实验，不能混在这组回放结果中。

## 指标边界

- 成功表示 PR 交付人审，不表示合并、上线或生产告警消失。
- alert → PR 不是完整故障 MTTR，不含人工审查、发布和生产恢复。
- 每轮 n=3，仅支持观察该样本的链路和规则效果，不外推生产成功率。
- token 是数量，不是金额；两轮没有证明成本或延迟改善。
- 服务快照包含历史 job，回放表仅统计本轮，口径不能混用。
- 本次真实模型审查是独立实验，不能借用旧回放证明新增能力有效。

## 设计与历史

后续设计：[PR 审查路线](05-pr-review-improvements.md)、[监督设计](06-tianshu-supervision-notes.md)、[测试体系](07-tianshu-test-notes.md)。

本地说明：[定位](01-positioning.md)、[架构补充](02-architecture.md)、[M1](03-m1-alert-driven.md)、[M2](04-m2-enterprise-capability.md)、[后续事项](../fixes/backlog.md)。

本地[完整演进历史](../history/evolution-2026-10-05.md)保留逐项执行与真机事故，旧的自动宿主降级等策略已被后续修复替代，不能作为当前默认行为。
