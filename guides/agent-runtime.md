# Agent 请求与队友运行契约

## 中断后的请求配对（A5）

三个 provider 客户端在合并 transient reminders 后，对发送副本规范化工具调用与结果，不修改真实历史、会话文件或审计。正常历史保留正文、thinking/signature 和缓存标记策略。

结果按原 ID 绑定到最近的调用批次，同一批次中的重复 ID 按调用先后配对，并仅在请求副本中生成唯一 ID。迟到的真实结果优先，移动到调用之后；部分缺失的结果补错误说明：执行状态未知，可能已有副作用，重试前应检查实际状态。明确的权限拒绝保留原事实。孤儿或重复结果转为带来源说明的普通文本，保留证据但不冒充已配对执行。

回归入口：`tests/test_request_pairing.py` 使用真实 Agent 取消和三个客户端的 SDK 捕获边界，检查角色、相邻顺序、ID、迟到/重复结果与历史不变。此为离线请求构造验证，未调用远端模型、未验证远端 HTTP 错误码，也未替代 TUI 实窗人工验收。

## 队友监听与退出（A3）

团队 worker 每轮完成后向实际 `lead_agent_id` 发送 idle，保持监听直到 shutdown、取消、失败、团队删除或所属应用退出。普通非团队后台任务仍一次完成。TaskManager 的完成通知是每轮快照，只消费一次；消费通知不会删除仍可派工、取消的监听句柄。

`SendMessage` 只向本团队 lead 或有消费者的成员投递；已知离线成员返回错误。广播中存在离线目标时整批报错，不将持久写入冒充接收成功。shutdown_request 的回应保留 request_id，shutdown_response 不触发模型执行。

headless 在仍有执行轮次、已接受但未消费的派工或未消费通知时继续等待；只剩 idle 监听者时结束本轮，并取消、等待所属后台句柄。TUI 退出和 TeamDelete 也先 await 收尾再删除邮箱。同步直接删除仍有监听者的团队会报错，调用者应先 await `shutdown_team`。

验证入口：`tests/test_worker_lifecycle.py`；真实超过 60 秒的低成本离线用例可用 `MEWCODE_LONG_LISTENER_TEST=1` 开启。该测试使用临时 home/cwd、真实邮箱与任务管理器、脚本 worker，无模型调用。普通 CI 使用加速时钟跨过原 60 次轮询，并覆盖真实 headless 创建团队/派工/通知/退出链路。
