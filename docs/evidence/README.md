# 本地证据与非 Markdown 资料

本页随仓库分发。下表所列原始资料位于本地，普通克隆仅包含索引。各项实验的日期、版本、结果和适用范围见 [验收索引](../acceptance/README.md)。

| 目录或格式 | 对应来源与保留原因 |
| --- | --- |
| `acceptance-2026-10-30/`、`next-stage-2026-10-30/` | [工程与模型验收](../acceptance/README.md)引用的请求、响应、用量、源码快照、diff 和语义评估；原始模型记录不公开归档 |
| `stage-2026-10-31/`、`service-reliability-2026-11-01/`、`dead-end-2026-11-01/`、`self-verify-2026-11-01/`、`intent-anchor-2026-11-01/` | 监督与服务阶段报告的测试、构建和 Docker 接线证据 |
| `tui-2026-11-01/`、`tui-terminal-2026-11-02/` | Textual 截图、PTY 原始终端流、协议与恢复日志。自动验证及待人工确认项见 [TUI 验收](../acceptance/stage-2026-11-01-tui.md)和[终端验收](../acceptance/stage-2026-11-02-terminal.md) |
| `documentation-2026-11-03/` | 文档整理提交的 CI 摘要与 Windows/Python 3.13 首次运行日志。迁移和验证范围见 [文档整理记录](../history/documentation-2026-11-03.md) |
| `.log` / `.txt` / `.json` / `.jsonl` / `.diff` / `.raw` | 运行与失败事实；未引用单个文件不等于无价值，部分由报告引用整个实验目录 |
| `.svg` / `.png` | 视觉纠偏与黑色主题记录；旧浅色图保留为历史过程，不是当前设计入口 |
| `.py` / `.ps1` / `.cmd`、嵌套 `.git`、数据库、压缩及二进制文件 | 实验脚本、冻结快照或临时环境。未经归属与可再现性核验不删除，也不批量执行 |

参考库对照的探针脚本和观测 JSON 随仓库分发，修复前的独立复现资料仅保存在本地。两份公开模型摘要位于 `docs/acceptance/`。回放脚本默认将新报告写到 `docs/evolution/replay/`，其中的历史报告也只保留于本地。[审查索引](../review/README.md) · [历史回放摘要](../history/replay-2026-10-05.md) · [回放脚本](../../scripts/m3_replay.py)

证据清理需要核对报告引用、唯一失败信息、源码版本与资源归属；当前未核验的临时目录和 Docker 卷继续保留，见[维护待办](../backlog.md)。不把“无 Markdown 链接”当作删除条件，不保存真实配置或密钥到文档归档。
