# 借鉴 Tianshu Harness 的测试体系与实施边界

> 2026-10-30 状态：最小架构/规模守卫和四项缺陷回植已实施并再次验收通过；其余注入面治理、更多回植等仍属提案。当前结果见[验收报告](../acceptance-2026-10-30.md)。下面的“待立项”和规模数字仅对应原始调研范围与日期。

记录日期：2026-10-27。
状态：2026-10-27 已对照 GitHub 源码完成静态审阅并修订，待立项。本文件是调研记录，不代表已实施；
与 [06 号监督闭环笔记](06-tianshu-supervision-notes.md) 同批调研，
独立成篇（一篇一主题）。是否立项由维护者审阅后决定。

2026-10-28 实施更新：第一阶段最小工程守卫已实现（静态依赖边界、重点文件行数预算、
四项隔离缺陷回植），实现范围和可复现命令见 [维护记录](../../guides/maintenance.md)。
下文仍保留调研时的拟议范围；上下文注入面申报表、更多回植及监督闭环不属于本阶段。

来源与方法：初稿对 `D:\projects\Tianshu-harness`（v3.37.0，Apache-2.0）做静态阅读。
本次审阅从 GitHub 下载并比对两份笔记引用的 16 个文件，均与本地快照一致；
固定基线为 [`23afee4733fdce04abf82e1e3fcc46d6246b9f45`](https://github.com/huiliyi37/Tianshu-harness/tree/23afee4733fdce04abf82e1e3fcc46d6246b9f45)。
未运行上游代码或安装依赖。下文区分上游已有检查与 MewCode 拟议规则，
静态阅读不能代替门禁运行和变异用例验证。

## 1. 现状对照

| | MewCode | Tianshu |
| --- | --- | --- |
| 测试文件 | 58 个（tests/，约 87 万字节） | 约 1,875 文件 / 31.9 万行 |
| 风格 | 功能单测、回归用例、异步门控及局部源码守卫 | 功能单测 + **四类守卫/验证机制**（见 §2，部分为独立脚本） |
| 自检 | mypy / ruff，已有主入口约束检查 | 另有系统性架构扫描、行数棘轮、注入面门禁、缺陷回植 |

结论：MewCode 已有局部源码守卫，例如 `tests/test_main_gate.py` 使用
`inspect.getsource` 检查主入口不再依赖 team 私有状态；未使用 ast 不等于没有源码扫描。
可补充的是系统性的层间依赖、上下文写入口与文件规模守卫。
仅凭静态测试数量和种类不能断言现有测试质量充分。

数量口径：MewCode 的 58 个文件为 tests/ 顶层 test_*.py，本次已重新计数；
其余体量数字为初稿近似统计，未保存可复现脚本，不用于质量、覆盖率或成本比较。

## 2. 借鉴清单（按建议实施顺序）

### 2.1 架构守卫测试（优先级最高，成本最低）

Tianshu 实现：`src/__tests__/architecture-guards.test.ts`。

设计语义：CI 级源码扫描，把设计约束变成红绿测试——扫描全部源文件找
违禁模式（层间禁 import、文件行数超红线 MAX_LINES_BASELINE/REDLINE、
白名单外调用点），违规时失败信息直接指名文件。

MewCode 适配（拟议规则，实施时可增删）：

- `mewcode/service/` 不得 import TUI 层；
- `mewcode/tools/impl/` 不得 import `agent.py` 内部状态；
- `mewcode/permissions/` 不得依赖 `service/`；
- 超过约定行数的源文件报警（与 §2.4 棘轮联动）。

实现：pytest 用标准库 `ast` 扫描自身源码，脱网单测，零新增依赖。
mypy 管类型，本守卫管**结构腐化**——对从 toy 长大的项目是关键闸门。

⚠ 上游注释中的现成教训（对本项目直接生效）：
**路径比较必须归一化为 POSIX 形式**。上游 Windows 上白名单用 `/` 字面量
`includes` 判定反斜杠路径静默全落空，守卫形同虚设（Linux CI 绿、
本机假红）。本项目 Windows 是一等公民，归一化须作为实现第一原则。

验收要求：人为制造一条违禁 import → 测试必须红且指名该文件；
Windows 与 Linux 上结果一致。

### 2.2 缺陷回植验证（"不会失败的门禁等于装饰"）

Tianshu 实现：`scripts/verify-obstacle-restored-defects.mjs` 及 20+ 同族脚本。

设计语义：对每个已修复缺陷，**在内存中把原始 bug 行为塞回真实源码**
（字符串替换，不改共享文件），断言对应测试此刻**确实变红**。
修复的回归保护从"声称有覆盖"升级为"被证明能咬住旧行为"。

MewCode 适配：素材完备——`REVIEW_FIXES.md` 与 `fixes/` 中 R1–R5、
N01–N41 共 41+ 已修复项。挑选其中高危的 10–15 条
（权限绕过、沙箱逃逸、误带文件提交、凭据处理类）各写一条回植用例：
monkeypatch/字符串替换恢复旧行为 → 断言现有安全测试失败。

验收要求：每条回植用例必须在“旧行为”下出现预期断言失败、“当前代码”下绿；
替换锚点缺失、语法/导入错误或超时不得冒充有效 RED。验证变异确实作用于被测路径，
使用独立进程或可靠隔离恢复，不能污染后续用例或修改共享源码文件。
回植锚点与预期断言保持明确映射。

### 2.3 上下文注入面申报表（覆盖边界需明确）

Tianshu 实现：`src/prompt/injection-surfaces.ts`（申报表）+
`scripts/verify-injection-surfaces.ts`（校验器）+
`scripts/__tests__/injection-surfaces.test.ts`（门禁测试）。

上游行为：申报表描述各类上下文通道，校验器主要对指定文件的已知结构做对账，
包括锚点存在、CvmInjectionSource 计量源覆盖、frozen 字段分区、appendix 推送点
数量与顺序、尾部挂载形状及部分间接字段消费；顺序倒挂检查仅报告警告。
它不是全仓任意上下文写入的数据流分析器，无法据此承诺任意新增注入路径必被发现。
该机制主要服务上下文布局、计量和缓存纪律，不能直接等同于提示词注入防御。
门禁测试用变异构造证明既定检查会失败；读入源码时归一化 CRLF，避免跨平台解析差异。

MewCode 拟议适配：服务模式会把**告警内容**
（Loki 日志、Alertmanager annotations——攻击者可影响的外部文本）
拼入上下文，值得建立可审计清单。先盘点 SOP、hook prompt 队列、工具结果与记忆等
实际写入口，再定义 `PROMPT_SURFACES` 和集中构造接口，记录来源、信任级别、通道与处理规则。
ast 守卫检查集中接口调用及已知旁路；动态调用、别名与字符串间接拼接等覆盖限制须明确记录。
若要求所有写入都受门禁管理，须先收敛写入口，并用绕过入口的变异用例验证约束。
登记仅提供审计与结构约束，仍需独立的权限、沙箱和外部文本信任边界；
登记完成不代表不可信文本已经安全。

验收要求：申报表与声明支持的写入口一致；删除申报、在支持入口新增未申报注入、
通过已知旁路直接写上下文均应失败，失败须定位到具体入口。
覆盖外限制、未盘点路径与仅告警项单独列出，不以表校验通过宣称全仓防注入完成。

### 2.4 行数棘轮账本（防第二个巨型文件）

Tianshu 实现：`scripts/source-budgets.manifest.json`（每文件 ceiling
+ 全局 redline）+
`scripts/verify-source-budgets.ts`（校验 + `--list` 用量表）+
`architecture-guards.test.ts` 执法。

上游行为：architecture-guards 对具名文件执行 ceiling，对其他文件使用 redline；
独立脚本主要检查账本条目，不要求所有源文件都登记。超过 ceiling 会失败，
增长可通过显式修改账本并说明理由接受评审；该机制不等于绝对禁止文件增长，
也不自动保证删行后 ceiling 随之收紧。

MewCode 适配：项目尚小，现在建账本成本最低。对 `mewcode/` 源文件
登记 ceiling（按现状 + 余量），pytest 或独立脚本校验；
`--list` 式用量报告可放 `scripts/`。

验收要求：超 ceiling 的文件使守卫红；删行后不再红；
拟议的全文件登记须额外检查未登记源文件及失效条目，与上游具名大文件账本区分；
若要求删行同步收紧 ceiling，需另设变更检查规则，不能仅依赖当前上限比较。

## 3. 建议顺序与工作量

| 序 | 项目 | 预估 | 依赖 |
| --- | --- | --- | --- |
| 1 | 架构守卫（含 POSIX 归一化） | 约半天 | 无 |
| 2 | 行数棘轮 | 约半天 | 可与 1 同一测试文件 |
| 3 | 缺陷回植（10–15 条高危） | 约两天 | 从 REVIEW_FIXES.md 选材 |
| 4 | 注入面申报表 | 约 2–3 天 | 需先盘点现有 prompt 构造点 |

以上是初稿粗估，尚未包含全部接线、跨平台运行及变异验证成本，不作为交付承诺；
上下文写入口收敛的范围确定后再重估。

与 05/06 号路线图的关系：06 号监督层上线后，§2.2 回植验证可直接用于
证明监督规则（self-verify、dead-end 判据）确实咬合；
§2.3 申报表与 05 号 P1-B 的"模型输入与证据分离"审计方向互补。

## 4. 不建议抄的部分（边界）

- **TS 基建特供技巧**：esbuild transform、`module.registerHooks` 内存回植
  是 Node 生态能力；Python 用 monkeypatch / `ast` / 字符串替换等价实现即可，
  不为抄手段引入依赖。
- **1:1 测试体量目标**：本项目 58 文件约 2 万行覆盖的是该覆盖的功能面；
  不为比例注水。借鉴的是测试**类型**，不是体量。

## 5. 参考资料

- [Tianshu Harness](https://github.com/huiliyi37/Tianshu-harness)
  （2026-10-27 本地快照 v3.37.0，Apache-2.0；上游持续演进，实施前重新核对）
- 固定基线源码：[架构守卫](https://github.com/huiliyi37/Tianshu-harness/blob/23afee4733fdce04abf82e1e3fcc46d6246b9f45/src/__tests__/architecture-guards.test.ts)、[缺陷回植](https://github.com/huiliyi37/Tianshu-harness/blob/23afee4733fdce04abf82e1e3fcc46d6246b9f45/scripts/verify-obstacle-restored-defects.mjs)、[注入面申报表](https://github.com/huiliyi37/Tianshu-harness/blob/23afee4733fdce04abf82e1e3fcc46d6246b9f45/src/prompt/injection-surfaces.ts)、[校验器](https://github.com/huiliyi37/Tianshu-harness/blob/23afee4733fdce04abf82e1e3fcc46d6246b9f45/scripts/verify-injection-surfaces.ts)、[变异测试](https://github.com/huiliyi37/Tianshu-harness/blob/23afee4733fdce04abf82e1e3fcc46d6246b9f45/scripts/__tests__/injection-surfaces.test.ts)、[行数校验](https://github.com/huiliyi37/Tianshu-harness/blob/23afee4733fdce04abf82e1e3fcc46d6246b9f45/scripts/verify-source-budgets.ts)。
- 本仓：[06 号监督闭环笔记](06-tianshu-supervision-notes.md)、
  [05 号 PR 审查改进计划](05-pr-review-improvements.md)、
  `REVIEW_FIXES.md`、`fixes/`（回植选材来源）
- 本文所有阈值（文件行数红线、棘轮 ceiling 等）为拟议值，
  实施时按本项目文件现状标定，不照抄上游数字。
