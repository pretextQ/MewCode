# 2026-11-01 dead-end 服务反馈验收

起点 `e4ebb2b5e947cef9a089e93180dfefa9e60a90af`，master，开工工作区干净且 origin/master SHA 一致。检查工作区与父目录未发现适用 AGENTS.md。用户明确授权本阶段实现、验证、更新文档、提交并推送；不调用付费模型、不创建远端修复 PR、不部署生产。

## 交付行为

新增纯 Python `service/failure_feedback.py`，在现有 `execution.py` 修复后验证与重试反馈之间接线；不更改状态机重试、Outbox、策略重验证或人工合并边界。服务监督默认关闭，关闭时不增加反馈事件、提示或模型请求。

开启时识别完整普通 pytest 输出（`pytest-long-v1`），保留全部失败块和节点语义计算 SHA-256，仅归一化已知动态根与输出格式。失败真实变化记 changed，未知/不完整格式不认定重复；可能 TDD、环境/基础设施问题、超时和 passed/not_run 均中断连续计数。具体命令、输出契约与未覆盖格式见[监督指南](../../guides/supervision.md)。

同 execution 连续两次相同完整失败证据后，只向现有预算允许的下一次 attempt 追加重新诊断建议。默认三次完整 attempt 时最早第三次携带，不因相同指纹提前停止、升级或增加请求。与上游三次文件编辑循环不同，无成本或修复质量收益结论。

结构化事件关联现有 execution/attempt 和原始 verify_tests 的 event_seq；原始输出保持既有截断约定，新增 excerpt 与截断元数据，未新增全量原始输出归档。提醒 prepared 仅表示服务已组装反馈，不冒称 provider 送达或模型采纳。新 execution（取消后重派、重启恢复、人工返工）重置连续计数，旧证据保留。

## 验证

所有模型及外部服务使用替身；真实 pytest 子进程只执行临时合成仓库，Docker 专项使用假模型。未触碰真实配置、凭据或业务数据库。
本地系统为 Windows，Python 3.14.4；mypy 仍按项目 Python 3.11 类型目标分别检查 win32/linux。远端实际运行平台与 Python 版本另由 CI 核实。

| 检查 | 结果 |
| --- | --- |
| 新增 dead-end 专项 | 最终 48 passed，20.00 秒 |
| 相关服务/沙箱/监督回归 | 252 passed，84.73 秒（新增专项初版 36 项时运行） |
| 最终全量 pytest | 1579 passed / 19 skipped，246.11 秒；最终代码与 48 项新增专项全部纳入 |
| 独立真实 Docker 专项 | 16 passed，79.19 秒；Engine 29.8.2，无本次 mewcode 容器残留；不与全量计数相加 |
| Ruff / 两平台目标 mypy / 工程守卫 | 通过；Windows/Linux 目标各 172 个文件，未提高源码 ceiling |
| 四项旧缺陷回植 | GREEN 各 0 失败；RED 命中 2 / 5 / 5 / 1 个预期断言失败 |
| 锁文件 / 构建 / 子代理组件脚本 | uv lock --check、0.3.0 sdist/wheel 构建及 86/86 均通过，无新增依赖或真实模型调用 |

覆盖相同指纹、仅动态路径/耗时变化、全部失败块（含第四项变化）、源码/断言/行号/参数变化、ANSI/CRLF、未知命令/输出、不完整 summary、TDD、环境/超时/基础设施故障、关闭兼容、基线不计数、上限无下一次提醒、真实 pytest 服务链、Worker 取消重派、SQLite 关闭重开恢复及人工返工。测试同时断言实际修复提示、attempt/token 数、状态及 execution/原始证据关联。

初次 Ruff 发现导入排序与复用 fixture 的未使用导入告警，已用显式 fixture 重导出修正；初版专项 36 passed 带一项 TestOutcome 收集警告，改用别名后后续专项无该警告。随后专项 42 passed / 14.66 秒，首轮全量 1573 passed / 19 skipped / 249.16 秒。代码复核补充保留源码路径字面量、忽略标题填充宽度、未知节点截断、按实际落库判断原始输出截断、保留捕获输出中应用自定义标题之后的语义及异常计数回退。追加测试曾把原始输出与 normalized excerpt 的截断标记误认为总相同，出现 1 failed / 45 passed；修正断言后 46 passed / 18.78 秒，最终补充后 48 passed / 20.00 秒。

补充最后两项解析边界前的一轮全量为 1577 passed / 19 skipped / 246.79 秒；最终交付版结果以上表为准。本地原始验证日志位于忽略的 `docs/evidence/dead-end-2026-11-01/`，仅精确放行本验收报告。最终全量使用 `uv run pytest -q --maxfail=10 --timeout=120`；Docker 单独以 `MEWCODE_DOCKER_TESTS=1` 运行 `tests/test_sandbox_live.py`，默认全量未开启 Docker。工程检查命令为 `uv run ruff check .`、`uv run mypy mewcode --platform win32` / `--platform linux`、`uv run python scripts/engineering_guards.py`、`uv run --locked python scripts/verify_restored_defects.py`、`uv lock --check`、`uv build` 和 `uv run python scripts/verify_subagent.py`。

## 交付与边界

本报告随功能提交发布，所属提交 SHA 以 Git 历史为准。本表记录本地验收；推送后另行核对 origin/master SHA 和该提交 Windows/Linux × Python 3.11/3.13 及 security 五项 CI，并在交付答复报告最终状态。本地通过不等于远端通过。

此次只实现服务验证反馈，不监听模型内部编辑/测试循环，不实现 self-verify、token 预算预警、intent anchor、GitHub 评论/评审接入、observe、PR 发布持久化队列或自动合并。容器配置协议和 Agent 总线未修改，真实 Docker 回归用于核对既有接线保持。只有行为验证，没有付费模型效果评测或生产收益证据。
