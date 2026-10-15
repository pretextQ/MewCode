# 项目审查问题修复记录

日期：2026-10-15。对应 `PROJECT_REVIEW.md` 中的 R01～R09。

## 提交与推送

各问题独立提交，验证通过后依次推送至 `origin/master`，没有合并为一个提交。

| 问题 | 修复 | 提交 |
| --- | --- | --- |
| R01 | 校验 Compose 配置，仅允许受限的依赖服务选项；拒绝宿主挂载、特权模式、构建和外部资源；在 worktree 外保存执行快照并阻止宿主环境变量插值 | `f0bdafd` |
| R02 | Grep、Glob 对每个匹配结果解析真实路径，阻止目录穿越与符号链接越界 | `55503fa` |
| R03 | Git branch、tag、remote 仅对明确的只读参数自动放行，修改命令进入正常权限确认 | `97428b3` |
| R04 | 测试容器使用实际 job ID，镜像和审计使用对应仓库名；无 job ID 的独立调用使用唯一标识 | `326d2e4` |
| R05 | agent 和命令运行在取消时强制删除 daemon 容器；重复取消不会提前打断清理；清理后继续传播取消信号 | `1a127e2` |
| R06 | 完成轮次的检查点保存当时文件内容；记录文件存在状态和后续首次修改文件的初始状态；备份丢失不再触发误删 | `89ae2a5` |
| R07 | 服务启动复用入口加载的完整 AppConfig，统一 provider、hooks、worktree 和 service 配置 | `e644e65` |
| R08 | 容器使用 provider.resolve_api_key() 的结果，保持字面量、自定义变量和标准变量的认证优先级；密钥不写入容器配置文件 | `8e1c655` |
| R09 | 每次运行刷新 job 累计用量；预算包含此前 attempt；耗尽时禁止新运行；超限 attempt 的消耗仍写入数据库与审计 | 本文件所在提交 |

## 回归验证

- R01：34 项 Compose/集成验证测试通过；2 项真实 Docker Compose 测试通过。
- R02：72 项搜索与权限安全测试通过；1 项符号链接测试因 Windows 环境不允许创建链接跳过；工具类型检查通过。
- R03：142 项 Git 与权限测试通过；1 项不适用于当前系统文件布局的符号链接测试跳过。
- R04：117 项测试容器、沙箱、执行链和 CLI 测试通过。
- R05：45 项取消与沙箱测试通过；新增真实 Docker 取消测试通过，确认容器已被删除。
- R06：88 项文件历史、回滚缓存与权限测试通过。
- R07：23 项服务 CLI 与主入口测试通过。
- R08：75 项认证、MCP 与沙箱测试通过。
- R09：121 项执行链、策略与指标测试通过，覆盖直跑和沙箱重试累计用量及超预算审计。

最终全量验证结果见本文件末尾。

## 行为变更

自动 Compose 集成验证现在只支持安全子集：不支持宿主 bind mount、特权选项、build、env_file、include、extends、外部网络和外部卷。环境变量必须显式给值，不会再从宿主插值获取值。普通镜像、命令、健康检查、服务依赖及无额外选项的 project 内命名卷和网络仍可使用。

搜索结果必须位于所请求目录解析后的真实路径内；指向目录外的符号链接不会出现在结果中。Git 只读命令无法明确识别时，仍可经正常权限流程使用。

沙箱预算继续采用运行后的累计用量门禁，不能中途停止已经发生的模型消耗；预算耗尽后不会继续启动新 attempt。

## 最终全量验证

**1312 passed，3 skipped，0 failed**，耗时 **228.21 秒**。

- 已开启 `MEWCODE_DOCKER_TESTS=1`，15 项真实 Docker 测试全部通过，包括新增的取消后删除容器测试。
- Ruff 全项目检查通过，CI 范围内 mypy 检查通过（32 个源文件），`git diff --check` 通过。
- 3 项跳过分别为：当前系统没有适合符号链接测试的系统文件、Windows 未授予创建符号链接的权限、测试父 agent 未配置 checker。
- 针对 Windows 跳过的链接场景，另在真实 Linux 沙箱容器内创建外部文件符号链接并运行 Grep、Glob，验证外部内容和文件名不返回，内部文件仍可搜索，输出 `GLOB_GREP_SYMLINK_BOUNDARY_OK`。
- 运行后检查测试容器、Compose 网络及测试 project 卷，没有发现残留。
- 测试使用本地假 LLM 和外部系统替身，没有发布真实 PR，也没有调用付费模型。

```powershell
$env:MEWCODE_DOCKER_TESTS='1'
.\.venv\Scripts\python.exe -m pytest -q -ra --tb=short --timeout=1200 --junitxml=.mewcode/test-runs/fixes-20261015/results.xml
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\mypy.exe mewcode/tools mewcode/permissions mewcode/serialization.py
```

完整日志：`.mewcode/test-runs/fixes-20261015/pytest.log`；JUnit XML：`.mewcode/test-runs/fixes-20261015/results.xml`。这些本地测试产物由 `.gitignore` 排除。
