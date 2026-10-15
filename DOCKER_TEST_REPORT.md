# Docker 完整测试报告

> 本文记录修复前的测试基线；修复后的回归结果见 [REVIEW_FIXES.md](REVIEW_FIXES.md)。

- 日期：2026-10-15
- 平台：Windows 宿主 + Docker Desktop Linux 容器
- Docker Engine：29.8.2
- Docker Desktop：4.94.0
- Docker Compose：v5.5.1

## 结果

**1256 passed，2 skipped，0 failed**，pytest 总耗时 **239.39 秒**。

已设置 `MEWCODE_DOCKER_TESTS=1`，14 项默认关闭的真实容器测试全部执行并通过。Ruff 和 CI 范围内的 mypy 检查也通过。

项目没有固定的根目录 Compose 服务栈。本次使用项目自带测试启动沙箱容器和 Compose 依赖服务，运行完成后由测试清理。

## 真实 Docker 测试覆盖

| 测试内容 | 结果 |
| --- | --- |
| 非 root 用户运行 | 通过 |
| worktree 可写，根文件系统只读 | 通过 |
| 挂载范围外宿主文件不可见 | 通过 |
| `network=none` 阻断出网 | 通过 |
| `network=bridge` 允许出网 | 通过 |
| PID 数量限制生效 | 通过 |
| agent JSON 输出解析 | 通过 |
| 命令运行后无测试容器残留 | 通过 |
| 容器内日志 MCP server 调用 | 通过 |
| 容器内 CI MCP server 调用 | 通过 |
| 容器内完整 agent、CLI 配置及 MCP 工具往返 | 通过 |
| Compose 启动依赖、健康检查、容器内集成测试及清理 | 通过 |
| Compose 启动失败的错误反馈及清理 | 通过 |
| 同名旧容器清理后重新运行 | 通过 |

完整 agent 测试使用本地假 LLM 和假后端；实际运行 Docker、MCP 和 Compose。未调用付费模型，也未发布真实 GitHub PR。服务端到端测试中的远端 GitHub REST 使用替身。

## 跳过项

| 位置 | 原因 |
| --- | --- |
| `tests/test_permissions.py:133` | 当前环境没有找到适合符号链接测试的系统文件 |
| `tests/test_skill_executor.py:162` | 父 agent 未配置 checker，测试按自身条件跳过 |

## 静态检查

- `ruff check .`：通过。
- `mypy mewcode/tools mewcode/permissions mewcode/serialization.py`：通过，检查 32 个源文件。

## 运行命令与日志

```powershell
$env:MEWCODE_DOCKER_TESTS='1'
.\.venv\Scripts\python.exe -m pytest -q -ra --tb=short --timeout=1200 --junitxml=.mewcode/test-runs/docker-20261015/results.xml
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\mypy.exe mewcode/tools mewcode/permissions mewcode/serialization.py
```

- 完整 pytest 日志：`.mewcode/test-runs/docker-20261015/pytest.log`
- JUnit XML：`.mewcode/test-runs/docker-20261015/results.xml`

## 清理与结论边界

运行结束后检查了 `mewcode-live` 测试容器、两个测试 Compose project 的容器、`mewfix-live` 网络及成功场景的 project 卷，没有发现残留。保留可复用镜像，没有执行全局 Docker 清理。

本次未修改业务代码。测试通过说明现有测试覆盖的场景运行正常；此前 `PROJECT_REVIEW.md` 中记录的 9 个问题仍未修复，不能用本次通过结果替代这些问题的修复及针对性回归验证。
