# Docker、Redis 与 GitHub PR 真机验收

2026-11-27 在 Windows、Python 3.11.9 和 Docker Desktop 29.8.2 上，以 `master` 的 `5ec41a3` 加本轮 Git 路径修正执行。使用临时源码与 HOME。真实 `mewcode serve` 接收 Alertmanager 格式的合成告警，容器内 Agent 通过脚本模型替身执行 `ReadFile`、`EditFile`。测试依赖为真实 Redis，Git 推送、GitHub API 和 GitHub Actions 均使用演示仓。

## 结果

| 检查 | 观测结果 |
| --- | --- |
| 告警、修复与隔离 | 服务接受任务 `job-a75c67534025`，创建独立 worktree，第一次修复将 `app.py` 中的超时值从 0 改为 5 秒。宿主回退关闭。 |
| 本地 pytest | 基线两项失败，修复后两项通过。报告的 `verification.pytest_scope` 为 `preserved`，收集数 2 → 2。 |
| Docker 集成 | Compose 启动 `redis:7-alpine`，健康检查通过。加入同一网络的测试容器完成 Redis PING 和 SET/GET。服务随后移除该依赖的容器、卷和网络。 |
| 源码树与发布 | 单测、集成验证及提交对应源码树 `d12ce4fc4d2fb56b4b5d43a2e197b8e182976020`。PR head、发布分支和任务报告对应 `6d6c0ead1cd09a6ea17a7686eff773ff48c5e0e2`。基线分支 `acceptance/verification-7c6dc24b` 的提交为 `107affe6223a2e382519578f927c50849eac0521`。 |
| 远端门禁 | 推送和 PR 触发的两个 GitHub Actions `test` 作业均成功。服务按发布 SHA 查得 `success`，任务进入 `human_review`，PR 保持开放。 |

[演示 PR #21](https://github.com/pretextQ/mewcode-alert-demo/pull/21) · [推送 CI](https://github.com/pretextQ/mewcode-alert-demo/actions/runs/45970615244/job/140409853942) · [PR CI](https://github.com/pretextQ/mewcode-alert-demo/actions/runs/45970631511/job/140409909530)

## 验收中修正的问题

- 首次任务 `job-78c49c6c0c89` 的容器已经改动 `app.py`。Git 路径规则排除了忽略目录的内容，却仍匹配目录本身。`git add -N` 拒绝 `.mewcode` 和 `.pytest_cache`，差异收集返回空，服务进入 `cant_repro`。共享排除规则现覆盖目录本身和后代。回归覆盖仓库 `.gitignore`、嵌套缓存目录、可见修复差异和候选源码树。[路径实现](../../mewcode/service/verification.py) · [回归用例](../../tests/test_service_execution.py)
- Redis 默认入口会尝试切换用户，在丢弃 capabilities 后预检失败。演示 Compose 配置直接指定镜像的 Redis 用户 `999:1000`，保留原有隔离。最初的 webhook 配置使用了未展开的占位符，收到 401，改用本次运行的临时本地鉴权值后告警受理。
- Windows 服务执行、端到端、VCS、集成和 pytest 范围专项共 **165 通过**。回归用例在修正前按预期失败。Debian Git 2.39.5 也重现了旧路径规则的拒绝，使用新规则后报告差异、候选树和提交树一致。Ruff、工程守卫、Windows/Linux mypy、锁文件及 diff 检查通过。任务报告和验收摘要保存在本地忽略目录 `docs/evidence/service-and-terminal-2026-11-27/`。

本轮验收覆盖一条合成故障及其真实服务接线。分布式 pytest、集成用例范围对照和真实模型效果仍分别适用现有边界。[验证契约](../../guides/service-reliability.md#pytest-验证范围)
