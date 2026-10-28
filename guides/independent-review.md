# 独立修复审查（第二阶段）

默认关闭。启用后，服务先完成现有修复与验证，再在本地提交已经固定、尚未推送时运行一次独立审查。
发现问题、响应格式错误、超时或输入不完整均不会自动返工或批准合并；PR 中如实展示结果，供人工判断。
现有 CI 门禁、任务状态机、沙箱与权限继续执行。审查期间若 HEAD 改变，发布会拒绝携带过期审查证据推送。
推送 refspec 和后续 CI 查询均使用受审 SHA，避免后续 HEAD 变化把证据绑定到另一个提交。

## 启用与预算

在受信任的服务配置中设置（完整示例见 `.mewcode/config.example.yaml`）：

```yaml
service:
  review:
    enabled: true
    max_input_bytes: 12000
    max_output_tokens: 2048
    token_budget: 24000
    max_calls_per_job: 2
    timeout_seconds: 60
    max_evidence_bytes: 20000000
```

使用当前服务的 provider/model，但每次审查创建新客户端与新会话，关闭 thinking 和 SDK 自动重试。
不携带修复者对话历史、仓库指令、hook、MCP 或工具注册表；修复者收尾报告仅作为明确标注的待核对数据。
审查在服务进程内执行纯模型请求，不执行仓库代码，不需要在宿主上赋予额外工具权限。
关闭时不创建审查客户端、不生成审查工件，执行路径保持现有流程。

`max_input_bytes` 限制实际 user JSON 的 UTF-8 字节数，包含版本、文件名、JSON 转义及测试/告警上下文。
告警与修复者报告各最多 1000 字符，测试输出各取末尾 1000 字符；覆盖账本针对 diff，原告警仍保留在 job payload。
system 指令另计入 token 预算预留。输出上限传给 SDK；响应文本另有 64 KiB 接收保护。
每次审查只有一次模型请求，不自动重试或分块追加请求；`max_calls_per_job` 按 job 跨 attempt、CI 重试和重启累计。
达到调用或预算上限后，新提交的审查标为未完成，不复用旧提交的结论。

token 预检使用输入 UTF-8 字节数 + 输出 token 上限 + 1024 的保守预留，同时检查 provider 的本地上下文窗口配置。
这不是精确 tokenizer，也不依赖在线模型元数据。实际用量由 provider 返回后核对；超过预留会标记审查未完成。
未返回用量的调用（含中断）保留预留值，不能按零成本继续调用。
审查预算还受有效仓库策略/服务总 token 预算限制。同一服务实例内对同一 job 的审查串行，避免并发重复消费额度。
不支持多个服务进程共享一个 job 并发运行审查。

审查的 input、output、cache read/create 单独保存在结果中；job 累计用量计入这四部分，失败响应已报告的用量也会入账。
成本只报告 token，不转换金额；现有服务及审查沿用同一 provider/model，因此报告的模型归属不变。
超时覆盖证据准备与请求过程，SDK 收尾另有最多五秒的有界窗口。

## 固定版本、证据与覆盖

受审 head 是实际提交的 SHA；base 是该 head 与配置目标分支解析出的 commit 的 merge-base。
输入与结果均记录两端 SHA。所有补丁从固定 Git 对象读取，不从不断变化的工作区拼接。
不会更新目标分支或下载代码；本地缺少目标 ref 时如实记录未完成。

每次审查在 `service.data_dir/reviews/<job-id>/<review-id>/` 保留：

- `full.diff`：完整 Git binary/full-index diff，流式写文件，不被模型输入预算截断。
- `files.z`：NUL 分隔的变更清单，包含删除、重命名与非 ASCII 文件名。
- `request.json`、`system.txt`：实际请求的 user JSON 与 system 指令；未发起请求时可能仅为已准备的候选输入。
- `response.txt`：接收的模型响应（受接收上限限制）；`result.json`：服务生成的结构化结果。

job 审计记录保存完整结果与证据相对路径、SHA-256；`GET /jobs/{id}/report` 的 `reviews` 数组保留各次记录。
工件位于服务状态目录，不进入修复提交或 PR 附件；可通过本地状态目录核对，当前没有新增 HTTP 下载接口。
`max_evidence_bytes` 是模型审查准入上限：完整 diff 超限后仍保留本地证据，模型不接收该次输入。
它不是磁盘留存配额；工件保留周期与服务状态目录一起由部署者管理，本阶段未增加自动清理任务。

按 Git 变更顺序，在字节预算内选取补丁及每个 hunk 周围三行上下文，保留删除代码与 Git 的函数标题。
`full` / `partial` / `none` 表示补丁在请求输入中的包含程度；不代表模型实际理解了这些内容。
未发起模型请求时全部降为 `none`；已尝试但失败的请求保留输入范围，同时运行状态为未完成。
超预算、二进制、非 UTF-8 和无法可靠独立定位的复杂重命名，均有明确未覆盖原因。
重命名只有元数据时，full 也只表示元数据全部送入，不能解释为全文代码审查。
当前不扩展函数全文、不做跨文件检索或多轮动态上下文；未发送代码不能被声称已审查。

状态由服务决定，模型不能指定版本、覆盖或测试结果：

| 状态 | 含义 |
| --- | --- |
| `findings` | 有格式有效且锚定实际输入增删行的问题；仍须看覆盖与原因，可能只是部分范围 |
| `no_findings` | 收到有效响应，全部可审补丁已送入，所提供范围内未发现问题；不等于修复正确 |
| `incomplete` | 请求失败、格式/定位错误、未覆盖范围、预算不足或用量异常等；不可展示为审查通过 |

PR 中测试事实、修复者自述和独立模型意见分别展示。模型文本转义后进入模板，不允许插入自定义报告标题、HTML 或链接。
finding 只接受已发送补丁中的增删行，删除使用 old 侧位置；本阶段只展示汇总，不发布行内评论。
PR 最多展示 30 个覆盖行，完整列表保存在 job 报告与工件中，省略数量明确标出。
审查小节另有 12000 字符展示预算，超出时提示查看 job 报告的完整记录，不截断或丢弃存储结果。

## 新增输入面及边界

集中入口为 `review_prompt.render_input`。`REVIEW_SURFACES` 申报四类 user JSON 来源：
外部告警、修复者报告、服务测试结果与仓库输出、固定提交补丁。
前两类与补丁均按不可信数据处理，测试出口混合服务事实与仓库输出；system 指令由服务源码定义。
申报与实际 data 字段不一致会失败，离线用例覆盖删字段和新增未申报字段。
SHA、输入覆盖和预算均由服务生成，不能被这些文本改写。

这是本轮新增入口的审计清单与接线约束，不是全仓提示词注入防御。
全仓写入口收敛、任意动态旁路扫描、运行时监督、GitHub 评论驱动返工仍未实施。
JSON 分隔和指令不能保证模型忽略恶意文本；工具缺席、原权限与沙箱仍是执行边界。

## 验证与效果评估

```bash
uv run --locked pytest tests/test_independent_review.py -q --timeout=120
uv run --locked python scripts/engineering_guards.py
uv run --locked python scripts/verify_restored_defects.py
uv run --locked ruff check .
uv run --locked mypy mewcode
uv run --locked pytest -q --timeout=120
uv lock --check
```

定向测试使用真实本地 Git 提交与 worktree、模拟模型传输及模拟远端发布。
覆盖开关、固定版本、输入字节预算、删除/中文重命名/二进制、无效位置、格式错误、工具请求、超时、取消、
累计调用及预算、用量、落库重读、实际执行链和 PR 模板。三个 SDK 的重试关闭和输出限制仅离线构造验证。

`tests/fixtures/independent_review_cases.json` 提供五个初始样例：隐藏异常、无限重试、删除权限检查、正确修复、大 diff 未修根因。
样例标签是初稿，需维护者复核。脚本化模型响应仅证明接线契约，不能计算或宣称真实模型检出效果。
后续可在同一组样例、同一版本与配置上保存基线/候选预测，再分别运行：

```bash
uv run --locked python scripts/evaluate_reviews.py --predictions baseline.json
uv run --locked python scripts/evaluate_reviews.py --predictions candidate.json
```

预测 JSON 以 case ID 为键，值使用对应 `ReviewResult` 结构。
评估器报告问题存在性的 precision/recall、漏检、不完整项、token 与耗时；不完整正例未发现问题仍算遗漏，
不完整负例不能冒充正确的无问题结论。finding 正确性仍需人工逐条裁定，小样本不能推断生产准确率。
本阶段没有调用真实模型，因此真实效果对比与生产校准尚未运行；跨平台 CI 与真实 Docker 也需各自验证。
