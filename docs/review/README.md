# 参考库对照审查

当前状态：**A1–A5 与 U1–U7 均已交付**。本目录中的完整对照和复现记录描述修复前基线；不能将其“待处理”“复现成功”当作当前缺陷清单或修复通过。当前候选与未验证协议统一见[维护待办](../backlog.md)。

## 受版本控制的入口

- [2026-11-02 能力比较](review-2026-11-02-current-borrowing.md)：实施前静态差异与离线探针；对应[11 号方案](../evolution/11-upstream-borrowing-plan.md)和[U1–U7 最终台账](../acceptance/stage-2026-11-02-borrowing.md)。
- [A1–A5 方案](../evolution/10-review-a1-a5-fix-plan.md)和[最终修复验收](../acceptance/stage-2026-11-02-a1-a5.md)：请求配对、通知、pane、TaskStop、自动记忆，均已完成。

## 原审查 ID 与交付映射

| 原 ID | 当前结论 |
| --- | --- |
| A1 / A2 / A3 / A4 / A5 | 已修复，各自方案、提交和 CI 见 A1–A5 验收；A4 原问题是自动写入未进入类型化召回，并非召回永远为空 |
| B1 MCP | U6 已交付 legacy/eager/dispatch/显式 auto；legacy 默认。native/tool_reference 仍未独立验证，不将整个 B1 判为全部完成 |
| B2 写保护 | U1 已交付 |
| B3 Shell 并发与退出码 | U7 已交付，严格参数白名单，不扩大权限放行 |
| B4 项目指令 | U2 已交付目录链、AGENTS.md 与受控 `@include path`；没有新增 `@path` 语法或编辑时动态加载 |
| B5 提醒重发守卫 | N23 与 keyed transient reminder 已避免历史累积；但“工具池未变且提醒仍在时跳过重发”的一次性守卫未实现，仍为独立候选，不宣称已节省 token |
| B6 崩溃诊断 | U3 已交付 |
| B7 / B8 / B9 / B10 | 自定义命令、技能安装器、后台记忆整理、浏览器 UI：候选，未授权实施 |
| V1 / V2 | native/beta 端点与 reasoning_content 回传兼容性仍待独立运行时验证 |
| 编辑 diff（原附注） | U5 已交付成功写入后的有界 diff，不是编辑前确认功能 |
| C1–C7 | 保留本仓进程树、权限、平台、MCP 只读、泛型工具等现有契约，不为对齐上游回退 |

## 本地原始资料

以下资料保留在本地，不随普通克隆分发。它们包含原始静态发现、修订和复现依据，正文状态冻结于各自基线：

- [2026-11-01 完整对照](review-2026-11-01-upstream-parity-full.md)与[findings JSON](review-2026-11-01-upstream-parity-findings.json)：A 五条、B 十条、C 七项及 V 两条；JSON 是当时快照，不是当前看板。
- [2026-11-02 独立复核](verification-2026-11-02.md)：A1–A5 在 `0d2e213` 的离线复现；A3 含真实约 60 秒等待，A5 只验证三 SDK 的请求配对缺口，没有实测远端 HTTP 400。
- `evidence-2026-11-02/`：脚本、初始及最终 observations、日志、修复台账；旧脚本断言缺陷存在，不在当前源码上自动复跑。
- `evidence-2026-11-02-borrowing/compare_probes.py` 与 `observations.json`：已受版本控制的能力比较探针与观测，保留原始记录，不作为当前产品验收脚本。

参考对象为当时本地 `D:/Downloads/mewcode-python` 的上游 0.2.0 快照。两库已明显分叉，比较用于借鉴能力与设计，不能整文件替换或用静态端点推断取代真实兼容验证。
