# 进程回收测试存活判定验收

2026-11-27 在 `master` 基线 `307ced1` 与本地测试改动上验证。生产进程回收实现沿用该基线。

## 复现与修正

- 在 Linux 上创建真实子进程，让它在旧探针成功执行 `kill(pid, 0)` 之后、检查 `/proc/<pid>/stat` 是否存在之前退出并被等待回收。旧探针返回存活，随后诊断记录 PID 已消失。替换探针后，在读取 `stat` 时安排同样的退出，结果为已退出。
- Linux 探针直接读取一次 `stat`，把条目消失及 `Z`、`X` 判作已退出。回收断言和错误详情复用同一轮采样，继续检查三代进程及延迟写入，保留原有 2 秒等待上限。另保留一个真实子进程运行到断言期限，确认断言仍失败并记录其 `S` 状态。[Linux 进程状态定义](https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html)
- CI #195 的 `test_cancellation_reaps_descendants[tree-host]` 在断言中报告存活，随后三个 PID 的诊断均为 `gone`，与复现现象一致。U6 的 `tree-service` timeout 日志只记录了布尔断言失败，U7 记录的是 `tree-service` cancel 失败。两次早期失败均缺少该断言对应的进程状态，无法逐次归因。[CI #195 作业](https://github.com/pretextQ/MewCode/actions/runs/44293175312/job/134763298489) · [U6 首次失败作业](https://github.com/pretextQ/MewCode/actions/runs/41565098297/job/125684609788) · [U7 台账](stage-2026-11-02-borrowing.md#u7-实施记录2026-11-03)

## 验证结果

专项命令为 `python -B -m pytest -q tests/test_process_cleanup.py -p no:cacheprovider --timeout=40`。验证使用源码快照、临时工作目录及隔离的 HOME/USERPROFILE，依赖按 `uv.lock` 安装。

| 环境 | 结果 |
| --- | --- |
| Linux 6.18.40.1 WSL2 内核，Debian 容器，Python 3.11.18 | 基线 21 通过，修正后三轮各 26 通过 |
| 同一 Linux 内核，Debian 容器，Python 3.13.18 | 修正后三轮各 26 通过 |
| Windows，Python 3.11.9 | 基线 21 通过。旧探针对新增用例的结果为 2 失败、5 通过。修正后专项 26 通过 |

专项覆盖 Bash、command hook、仓库测试、宿主命令、父进程先退出、重复取消与正常退出码。`ruff check .`、工程守卫、`uv lock --check` 和 `git diff --check` 均通过。未运行全量测试和远端 CI。
