# 2026-10-30 下一阶段实施验收

代码起点：`a85444ccddc359e302b21db7f56896f810c637c5`。原验收报告及 SHA、原模型响应不改写。Windows / Python 3.14.4 / uv 0.11.7；Docker Engine 29.8.2 重新确认可用。

## A：审查范围与评估

已实现：每次请求 versions 中包含服务生成的逐文件 full/partial/none、原因、diff/sent UTF-8 字节数和 diff-only 范围声明；hunk 标题界定实际上下文。完整 diff 不截断。覆盖账本随最终补丁裁剪重新生成，和文件名、JSON 转义一起计入精确 user 输入上限。无法容纳事实与有效补丁时不请求模型。

模型指令明确：完整 diff 不等于完整文件，省略区域不证明函数不存在；无源码依据则在 summary 中保留未知。服务仍只验证格式、版本和实际输入的增删行，不能证明语义正确。功能默认关闭、仅提供意见；无工具、多轮检索、分块或自动返工。

原始五例 fixture 保持不变；新增 `independent_review_cases_v2.json` 明确 ValueError、持续失败传播、鉴权、加法及大 diff 的行为契约和复核边界。评估器保留存在性数字，另报 valid/invalid/unreviewed finding、有效正例和未复核/未覆盖案例。复核必须给源码证据；原“add 不存在”即便存在性命中也为无效。

## 真实模型比较

[完整摘要](next-stage-2026-10-30-model.json)。每组五次 deepseek-flash 请求，单例最多一次、无自动重试、user 输入 12000 字节、输出 1024 token、90 秒。基线代码从 a85444c 导出到隔离临时副本，候选使用当前改动；两组使用相同 v2 源码、契约及配置。逐例 full.diff SHA-256 相同，合成提交 SHA 分别记录。候选因覆盖事实及指令占用预算，实际补丁输入更短。

| 组别 | 有效正例 | 完整负例无误报 | 未完成大 diff | 有效/无效 finding |
| --- | --- | --- | --- | --- |
| 同配置重跑基线 | 3 | 1 | 1（输出上限） | 4 / 1 |
| 候选 | 3 | 1 | 1（输出上限） | 3 / 1 |

逐条对照完整源码：隐藏 ValueError、无限重试、删除鉴权均有成立意见；两组的额外重试意见均含过度断言，分别称循环不可杀死、泛称捕获所有异常及 cancellation，按整条意见严格拒绝。Exception 不捕获 BaseException/asyncio.CancelledError。正确修复确实从减法改成加法；大 diff 仍在 filler 后定义减法 add，两组均未完成响应，不能算正确负例或有效检出。没有用脚本化响应或模型自评分作质量证据。

**未证明效果改善。** 每组 n=5、单次调用；旧历史误判已被评分回归拒绝，但提示词不能保证模型不再误判。保持默认关闭。

2026-10-30 核查 [官方人民币价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)：Flash 最高输入 2 元/百万、输出 8 元/百万。每次按所有预留 token × 8 元/百万预检；未知用量停止。候选五次已知用量后释放未用预留，再为隔离基线五次预留 0.72192 元（加候选实际保守估计仍低于新增 0.90 元）。新增十次保守估计 **0.136408 元**，连同历史 0.06148 元累计 **0.197888 元**，不是账单金额。没有后续模型调用。

原始请求、响应、full.diff、固定 SHA、用量、耗时、哈希与执行脚本在本地 `docs/evidence/next-stage-2026-10-30/`，保持忽略；未归档实际配置或凭据。摘要版本化；历史工件不修改。

## A 工程验证

- `uv run --locked pytest tests/test_independent_review.py -q --timeout=120`：54 passed。
- Ruff、Windows/Linux 目标全包 mypy（各 164 文件）、工程守卫、锁文件通过。
- 四项缺陷回植：当前 GREEN；旧行为分别触发 2 / 5 / 5 / 1 个断言失败。
- `uv run --locked pytest -q -ra --timeout=120`：1441 passed / 18 skipped，226.62 秒。15 个 Docker opt-in、2 个本机符号链接权限、1 个 parent checker 缺省跳过，不计通过。A 未触及容器接线，Docker 专项在 B 配置接线后补跑。

## B：最小监督内核

A 交付后继续实施；尚未完成。
