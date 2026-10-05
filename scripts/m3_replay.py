#!/usr/bin/env python
"""M3 W3 评估集回放：把 demo 仓的三类 bug 告警批量回放，输出成功率/MTTR/token 报告。

对服务的唯一依赖是 HTTP 端点（先 ``uv run mewcode serve --port 9300``）：

    # 报告 A（基线）
    uv run python scripts/m3_replay.py --label baseline-a --fingerprint-prefix replay-a \
        --repo-path "C:/Users/32519/AppData/Local/Temp/m1demo"
    # 报告 B（改动后，带 A/B 对比小节）
    uv run python scripts/m3_replay.py --label after-rule-b --fingerprint-prefix replay-b \
        --repo-path "C:/Users/32519/AppData/Local/Temp/m1demo" \
        --compare docs/evolution/replay/baseline-a.json

每轮必须换 ``--fingerprint-prefix``：服务端按 fingerprint 做了 1800s 去重窗口，
同前缀重放会被合并进旧 job 而不是新建 job（响应里出现 deduped 时会如实记录）。

流程（用例串行执行）：每个用例先把 demo 仓重置成该 bug 的现场（调
``scripts/m1_demo.py init``，m1_demo 与测试用同一套素材）→ 推 main 到 GitHub
（告警语义是"bug 就在 main 上"）→ POST /webhook/manual → 轮询 /jobs/{id}/report
到 human_review（PR 已开出，等服务不动它）或终态 → 拉取 PR diff 做规范标记统计
（``--diff-marker-regex``，默认找 ``# fix(...):`` 追溯注释）→ 汇总。

评估集（内置常量 ``CASES``）：三类 bug 的告警输入来自真机跑过的三单——
PR #1（config_error）/ PR #2（null_deref）/ PR #5（unhandled_timeout），
告警文本与 m1_demo.py alert 的构造一致；incident_id 进 extra（Evidence 段），
供"修复必须引用告警标识"这类规范规则使用。

报告输出 JSON + Markdown 到 ``--out``（默认 docs/evolution/replay/<label>.json；
docs/ 不进 git，报告是本地证据）。/metrics、/costs 的服务级快照原样附在报告里
（注意：那是含历史 job 的累计口径，报告正文只统计本轮的 job）。

退出码：0 全部成功（PR 开出）；1 有用例失败/escalate；2 有用例基础设施错误
（intake 失败、轮询超时等）。报告在任何情况下都会落盘。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent

#: 重试成功 = 到达 human_review（或 merged）；失败 = 其余终态
SUCCESS_STATES = frozenset({"human_review", "merged"})
FAILED_STATES = frozenset({"escalate", "cant_repro", "invalid"})

#: 评估集：三类 bug 的告警输入（与真机 PR #1/#2/#5 的告警同源，见模块 docstring）
CASES: list[dict[str, str]] = [
    {
        "case_id": "config_error",
        "bug": "config_error",
        "title": "DemoBug: config_error in production",
        "summary": "config_error in production",
        "logs": "traceback points at app.py (config_error)",
        "source_pr": "https://github.com/pretextQ/mewcode-alert-demo/pull/1",
    },
    {
        "case_id": "null_deref",
        "bug": "null_deref",
        "title": "DemoBug: null_deref in production",
        "summary": "null_deref in production",
        "logs": "traceback points at app.py (null_deref)",
        "source_pr": "https://github.com/pretextQ/mewcode-alert-demo/pull/2",
    },
    {
        "case_id": "unhandled_timeout",
        "bug": "unhandled_timeout",
        "title": "DemoBug: unhandled_timeout in production",
        "summary": "unhandled_timeout in production",
        "logs": "traceback points at app.py (unhandled_timeout)",
        "source_pr": "https://github.com/pretextQ/mewcode-alert-demo/pull/5",
    },
]


def log(msg: str) -> None:
    print(msg, flush=True)


def http_request(
    url: str,
    *,
    payload: dict | None = None,
    token: str = "",
    method: str = "GET",
    timeout: float = 30,
    headers: dict[str, str] | None = None,
) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={
            "Content-Type": "application/json",
            **({"X-MewCode-Token": token} if token else {}),
            **(headers or {}),
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def percentile(values: list[float], p: float) -> float | None:
    """最近秩百分位（n 小时不做插值，样本数会写进报告）。"""
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round(p / 100 * len(ordered) + 0.5)) - 1))
    return ordered[idx]


# ---------------------------------------------------------------------------
# demo 仓现场重置：init（m1_demo.py，与测试同素材）+ push main
# ---------------------------------------------------------------------------


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    # GCM 非交互（执行记录 15 / 6eb827a）：宁可失败也不弹窗挂死
    env = {
        **os.environ,
        "GCM_INTERACTIVE": "never",
        "GCM_PROVIDER": "generic",
        "GIT_TERMINAL_PROMPT": "0",
    }
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False, env=env
    )


def reset_demo_repo(repo_path: str, bug: str, *, push: bool) -> None:
    """把 demo 仓 main 重置成指定 bug 的现场，并同步到 GitHub。

    m1_demo.py init 的语义：写 bug 文件 → 有差异就追加一个 "demo: planted X"
    提交（相邻用例 bug 不同，因此总是前进一步或已就位；不会出现"文件已改、
    提交没成"的中间态——commit 只在树与 HEAD 完全一致时才无事可做）。
    工作区不干净一律中止：worktree 从 main 的提交创建，脏 index 会让 job
    看到错误的现场，这种回放结果没有意义。
    """
    init = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "m1_demo.py"), "init",
         "--path", repo_path, "--bug", bug, "--with-compose"],
        capture_output=True, text=True, check=False,
    )
    if init.returncode != 0:
        raise RuntimeError(f"m1_demo.py init failed: {init.stderr.strip() or init.stdout.strip()}")

    status = _git(repo_path, "status", "--porcelain")
    # .mewcode/ 是服务自己的簿记（worktree 标记文件，历史上被误跟踪进 demo 仓），
    # 服务运行时会持续改写——提交侧执行链本来就排除它（执行记录 17），这里同口径。
    # porcelain 行格式是 "XY PATH"：路径从第 4 列开始，不能对整行 startswith。
    dirty = [
        line
        for line in (status.stdout.splitlines() if status.returncode == 0 else [])
        if not line[3:].strip('"').startswith(".mewcode/")
    ]
    if status.returncode != 0 or dirty:
        raise RuntimeError(f"demo repo dirty after init, refusing to replay: {dirty!r}")

    if push:
        proc = _git(repo_path, "push", "origin", "main")
        if proc.returncode != 0:
            raise RuntimeError(f"git push origin main failed: {proc.stderr.strip()}")
        head = _git(repo_path, "rev-parse", "HEAD")
        remote = _git(repo_path, "rev-parse", "origin/main")
        if head.stdout.strip() != remote.stdout.strip():
            raise RuntimeError("origin/main != HEAD after push; refusing to replay on a stale base")
        log(f"    repo reset to {bug}, origin/main synced ({head.stdout.strip()[:8]})")
    else:
        log(f"    repo reset to {bug} (push skipped)")


# ---------------------------------------------------------------------------
# 单用例回放
# ---------------------------------------------------------------------------


def manual_payload(case: dict[str, str], *, repo: str, fingerprint: str, incident_id: str) -> dict:
    # extra 会进 Evidence 段（sop.extract_logs 以 JSON 渲染）：告警元数据 +
    # incident_id 都让 agent 可见——规范若要求追溯注释，依据就在眼前
    return {
        "repo": repo,
        "title": case["title"],
        "summary": case["summary"],
        "logs": case["logs"],
        "severity": "critical",
        "fingerprint": fingerprint,
        "payload": {
            "alertname": "DemoBug",
            "service": "demo-api",
            "runbook_url": "https://example.com/runbook/demo",
            "incident_id": incident_id,
        },
    }


def post_case(
    base_url: str, payload: dict, *, token: str
) -> tuple[str | None, str]:
    """POST /webhook/manual。返回 (job_id, 说明)；job_id 为 None 表示没建出 job。"""
    status, body = http_request(
        f"{base_url}/webhook/manual", payload=payload, token=token, method="POST"
    )
    try:
        parsed = json.loads(body)
    except ValueError:
        return None, f"HTTP {status}, non-JSON body: {body[:200]}"
    if status == 202:
        accepted = parsed.get("accepted") or []
        if accepted:
            return accepted[0]["id"], "accepted"
        return None, f"HTTP {status} without accepted job: {parsed}"
    if status == 422:
        return None, f"rejected/deduped: {json.dumps(parsed, ensure_ascii=False)[:300]}"
    return None, f"HTTP {status}: {json.dumps(parsed, ensure_ascii=False)[:300]}"


def poll_job(
    base_url: str, job_id: str, *, token: str, timeout_s: float, interval_s: float
) -> tuple[dict | None, str]:
    """轮询 /jobs/{id}/report 到 human_review/终态。返回 (报告, 说明)。"""
    deadline = time.monotonic() + timeout_s
    last_status = "?"
    while time.monotonic() < deadline:
        status, body = http_request(f"{base_url}/jobs/{job_id}/report", token=token)
        if status != 200:
            return None, f"report endpoint returned HTTP {status}: {body[:200]}"
        try:
            report = json.loads(body)
        except ValueError:
            return None, f"non-JSON report body: {body[:200]}"
        last_status = report.get("job", {}).get("status", "?")
        if last_status in SUCCESS_STATES or last_status in FAILED_STATES:
            return report, last_status
        time.sleep(interval_s)
    return None, f"timeout after {timeout_s:.0f}s (last status: {last_status})"


def fetch_pr_diff(pr_url: str) -> tuple[str | None, str]:
    """拉 PR 的统一 diff（公开仓，GitHub 的 .diff 端点匿名可读）。"""
    try:
        status, body = http_request(
            pr_url.rstrip("/") + ".diff",
            headers={"Accept": "text/plain"},
            timeout=60,
        )
    except (urllib.error.URLError, OSError) as e:
        return None, f"fetch failed: {e}"
    if status != 200:
        return None, f"HTTP {status}"
    return body, "ok"


def diff_markers(diff_text: str, marker_regex: str, incident_id: str) -> dict:
    files = adds = dels = 0
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            files += 1
        elif line.startswith("+") and not line.startswith("+++"):
            adds += 1
        elif line.startswith("-") and not line.startswith("---"):
            dels += 1
    return {
        "regex": marker_regex,
        "matches": len(re.findall(marker_regex, diff_text)),
        "incident_id_in_diff": incident_id in diff_text,
        "files": files,
        "added": adds,
        "removed": dels,
    }


# ---------------------------------------------------------------------------
# 汇总与报告
# ---------------------------------------------------------------------------


def build_summary(rows: list[dict]) -> dict:
    ok = [r for r in rows if r.get("success")]
    durations = [r["alert_to_pr_seconds"] for r in ok if r.get("alert_to_pr_seconds") is not None]
    tokens = [
        (r["tokens"]["input"], r["tokens"]["output"])
        for r in rows
        if isinstance(r.get("tokens"), dict)
    ]
    return {
        "cases_total": len(rows),
        "cases_success": len(ok),
        "success_rate": round(len(ok) / len(rows), 4) if rows else None,
        "attempts_mean": round(statistics.mean([r["attempts"] for r in rows]), 2)
        if all(isinstance(r.get("attempts"), int) for r in rows) and rows
        else None,
        "alert_to_pr_seconds": {
            "n": len(durations),
            "median": round(statistics.median(durations), 1) if durations else None,
            "p90": round(v, 1) if (v := percentile(durations, 90)) is not None else None,
        },
        "tokens": {
            "input": sum(t[0] for t in tokens),
            "output": sum(t[1] for t in tokens),
            "total": sum(t[0] + t[1] for t in tokens),
        },
        "diff_marker_matches_total": sum(
            r["diff_markers"]["matches"] for r in rows if isinstance(r.get("diff_markers"), dict)
        ),
    }


def markdown_report(data: dict, compare: dict | None) -> str:
    label = data["label"]
    summary = data["summary"]
    atp = summary["alert_to_pr_seconds"]
    lines = [
        f"# M3 W3 回放报告：{label}",
        "",
        f"- 时间：{data['started_at']} → {data['finished_at']}（服务 {data['base_url']}，"
        f"模型 {data['model']}）",
        f"- fingerprint 前缀：`{data['fingerprint_prefix']}` · repo：`{data['repo']}` · "
        f"diff 标记正则：`{data['diff_marker_regex']}`",
        f"- 评估集：{summary['cases_total']} 个用例（config_error / null_deref / "
        "unhandled_timeout，同真机 PR #1/#2/#5 的告警输入）",
        "",
        "## 汇总",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| PR 开出（成功率） | {summary['cases_success']}/{summary['cases_total']}"
        f"（{summary['success_rate']:.0%}） |" if summary["success_rate"] is not None else "",
        f"| alert→PR 中位 / p90 | {atp['median']}s / {atp['p90']}s（n={atp['n']}） |",
        f"| attempts 均值 | {summary['attempts_mean']} |",
        f"| token（in / out / 合计） | {summary['tokens']['input']} / "
        f"{summary['tokens']['output']} / {summary['tokens']['total']} |",
        f"| diff 标记命中总数 | {summary['diff_marker_matches_total']} |",
        "",
        "## 逐单",
        "",
        "| case | fingerprint | job | 状态 | attempts | alert→PR | token 合计 | PR | CI | diff |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in data["cases"]:
        tokens = row.get("tokens") or {}
        markers = row.get("diff_markers")
        diff_cell = (
            f"{markers['matches']} hit; {markers['files']}f +{markers['added']}/-{markers['removed']}"
            if markers
            else (row.get("diff_error") or "-")
        )
        lines.append(
            f"| {row['case_id']} | `{row['fingerprint']}` | `{row.get('job_id') or '-'}` "
            f"| {row.get('status') or row.get('error') or '-'} | {row.get('attempts', '-')} "
            f"| {row.get('alert_to_pr_seconds', '-') if row.get('alert_to_pr_seconds') is not None else '-'}s "
            f"| {tokens.get('total', '-')} "
            f"| {('[PR](' + row['pr_url'] + ')') if row.get('pr_url') else '-'} "
            f"| {row.get('ci_status') or '-'} | {diff_cell} |"
        )
    lines += ["", "### 逐单口径说明", "",
              "- 成功 = job 到达 `human_review`（PR 已开出、等人审）；`escalate` 等为失败路径，"
              "成本照实计入。",
              "- alert→PR 只对开出 PR 的 job 有定义（W1 的诚实口径）；本轮 n 写在汇总里。",
              "- token 为 provider 报告的原始数量，未换算金额（无定价数据，不编造汇率）。"]

    if compare is not None:
        lines += [
            "",
            "## 与基线对比：" + compare["label"],
            "",
            "| 指标 | " + compare["label"] + " | " + label + " |",
            "|---|---|---|",
        ]
        a_sum, b_sum = compare["summary"], summary
        rows = [
            ("成功率", f"{a_sum['cases_success']}/{a_sum['cases_total']}",
             f"{b_sum['cases_success']}/{b_sum['cases_total']}"),
            ("alert→PR 中位", f"{a_sum['alert_to_pr_seconds']['median']}s",
             f"{b_sum['alert_to_pr_seconds']['median']}s"),
            ("alert→PR p90", f"{a_sum['alert_to_pr_seconds']['p90']}s",
             f"{b_sum['alert_to_pr_seconds']['p90']}s"),
            ("token 合计", str(a_sum["tokens"]["total"]), str(b_sum["tokens"]["total"])),
            ("diff 标记命中", str(a_sum["diff_marker_matches_total"]),
             str(b_sum["diff_marker_matches_total"])),
        ]
        lines += [f"| {name} | {a} | {b} |" for name, a, b in rows]
        lines += [
            "",
            "| case | diff 标记（基线 → 本轮） | attempts（基线 → 本轮） |",
            "|---|---|---|",
        ]
        prev_by_case = {c["case_id"]: c for c in compare["cases"]}
        for row in data["cases"]:
            prev = prev_by_case.get(row["case_id"], {})
            a_mark = prev.get("diff_markers", {}).get("matches", "-") if prev.get("diff_markers") else "-"
            b_mark = row["diff_markers"]["matches"] if row.get("diff_markers") else "-"
            lines.append(
                f"| {row['case_id']} | {a_mark} → {b_mark} "
                f"| {prev.get('attempts', '-')} → {row.get('attempts', '-')} |"
            )

    lines += [
        "",
        "## 附：服务级快照（累计口径，含历史 job，不只本轮）",
        "",
        "```",
        data["metrics_snapshot"].strip(),
        "```",
        "",
        "```json",
        json.dumps(data["costs_snapshot"], ensure_ascii=False, indent=2),
        "```",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="M3 W3 evaluation replay")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9300)
    parser.add_argument("--repo", default="demo", help="repository name in service.repos")
    parser.add_argument("--token", default="", help="service.webhook_token, if configured")
    parser.add_argument("--label", required=True, help="report label, e.g. baseline-a")
    parser.add_argument("--fingerprint-prefix", required=True,
                        help="unique per round; server dedups fingerprints for 1800s")
    parser.add_argument("--incident-prefix", default="",
                        help="incident id prefix for extra.incident_id (default: fingerprint prefix upper)")
    parser.add_argument("--repo-path", default="",
                        help="local demo repo to reset per case (m1_demo.py init + push main)")
    parser.add_argument("--no-push", action="store_true",
                        help="reset the repo locally but skip pushing main")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--case-timeout", type=float, default=1500.0)
    parser.add_argument("--diff-marker-regex", default=r"#\s*fix\(",
                        help="regex counted in the PR diff (default: traceability comment '# fix(')")
    parser.add_argument("--out", default="",
                        help="JSON report path (default: docs/evolution/replay/<label>.json)")
    parser.add_argument("--compare", default="",
                        help="path of the baseline JSON report to compare against")
    parser.add_argument("--skip-cases", default="",
                        help="comma-separated case_ids to skip (partial reruns)")
    args = parser.parse_args(argv)
    started_at_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    base_url = f"http://{args.host}:{args.port}"
    out_path = Path(args.out) if args.out else (
        PROJECT_ROOT / "docs" / "evolution" / "replay" / f"{args.label}.json"
    )

    status, body = http_request(f"{base_url}/healthz", timeout=10)
    if status != 200:
        log(f"service not healthy at {base_url} (HTTP {status}): {body[:200]}")
        log("hint: start it with `uv run mewcode serve --port <port>`")
        return 2
    model = ""
    status, body = http_request(f"{base_url}/costs", timeout=10)
    if status == 200:
        try:
            model = json.loads(body).get("model", "")
        except ValueError:
            pass
    log(f"service healthy at {base_url} (model: {model or 'unknown'})")

    compare_report = None
    if args.compare:
        compare_report = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        log(f"will compare against: {compare_report.get('label')} ({args.compare})")

    skip = {c.strip() for c in args.skip_cases.split(",") if c.strip()}
    incident_prefix = args.incident_prefix or args.fingerprint_prefix.upper()
    rows: list[dict] = []
    infra_errors = 0

    for idx, case in enumerate(CASES, start=1):
        case_id = case["case_id"]
        if case_id in skip:
            log(f"[{idx}/{len(CASES)}] {case_id}: skipped (--skip-cases)")
            continue
        fingerprint = f"{args.fingerprint_prefix}-{case_id}"
        incident_id = f"{incident_prefix}-{idx:02d}"
        log(f"[{idx}/{len(CASES)}] {case_id}: fingerprint={fingerprint} incident={incident_id}")

        row: dict = {
            "case_id": case_id,
            "bug": case["bug"],
            "fingerprint": fingerprint,
            "incident_id": incident_id,
            "source_pr": case["source_pr"],
        }
        try:
            if args.repo_path:
                reset_demo_repo(args.repo_path, case["bug"], push=not args.no_push)

            job_id, note = post_case(
                base_url,
                manual_payload(case, repo=args.repo, fingerprint=fingerprint,
                               incident_id=incident_id),
                token=args.token,
            )
            row["job_id"] = job_id
            if job_id is None:
                row["error"] = note
                infra_errors += 1
                log(f"    intake failed: {note}")
                continue
            log(f"    job accepted: {job_id}; polling (timeout {args.case_timeout:.0f}s)")

            report, note = poll_job(
                base_url, job_id, token=args.token,
                timeout_s=args.case_timeout, interval_s=args.poll_interval,
            )
            if report is None:
                row["error"] = note
                infra_errors += 1
                log(f"    {note}")
                continue

            job = report["job"]
            row.update({
                "status": job["status"],
                "success": job["status"] in SUCCESS_STATES,
                "attempts": job["attempts"],
                "alert_to_pr_seconds": report["timing"]["alert_to_pr_seconds"],
                "total_duration_seconds": report["timing"]["total_duration_seconds"],
                "tokens": job["tokens"],
                "pr_url": job["pr_url"],
                "ci_status": job["ci_status"],
                "branch": job["branch"],
                "last_error": job["last_error"],
                "job_report": report,
            })
            log(
                f"    -> {job['status']} (attempts={job['attempts']}, "
                f"alert→pr={row['alert_to_pr_seconds']}s, tokens={job['tokens']['total']})"
            )
            if row["pr_url"]:
                diff_text, note = fetch_pr_diff(row["pr_url"])
                if diff_text is None:
                    row["diff_error"] = note
                    log(f"    diff fetch failed: {note}")
                else:
                    row["diff_markers"] = diff_markers(
                        diff_text, args.diff_marker_regex, incident_id
                    )
                    log(
                        f"    diff: {row['diff_markers']['files']} files "
                        f"+{row['diff_markers']['added']}/-{row['diff_markers']['removed']}, "
                        f"marker hits {row['diff_markers']['matches']}"
                    )
        except RuntimeError as e:
            row["error"] = str(e)
            infra_errors += 1
            log(f"    case infrastructure error: {e}")
        finally:
            rows.append(row)

    status, metrics_text = http_request(f"{base_url}/metrics", timeout=30)
    metrics_snapshot = metrics_text if status == 200 else f"metrics unavailable: HTTP {status}"
    status, costs_body = http_request(f"{base_url}/costs", timeout=30)
    try:
        costs_snapshot = json.loads(costs_body) if status == 200 else {"error": costs_body[:300]}
    except ValueError:
        costs_snapshot = {"error": costs_body[:300]}

    data = {
        "label": args.label,
        "started_at": started_at_iso,
        "finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base_url": base_url,
        "model": model,
        "repo": args.repo,
        "repo_path": args.repo_path or None,
        "fingerprint_prefix": args.fingerprint_prefix,
        "incident_prefix": incident_prefix,
        "diff_marker_regex": args.diff_marker_regex,
        "cases": rows,
        "summary": build_summary(rows),
        "metrics_snapshot": metrics_snapshot,
        "costs_snapshot": costs_snapshot,
    }
    if compare_report is not None:
        data["compared_with"] = {"label": compare_report.get("label"), "path": args.compare}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = out_path.with_suffix(".md")
    md_path.write_text(markdown_report(data, compare_report), encoding="utf-8")

    summary = data["summary"]
    log("")
    log(f"report: {out_path}")
    log(f"        {md_path}")
    log(
        f"summary: {summary['cases_success']}/{summary['cases_total']} success, "
        f"alert→pr median {summary['alert_to_pr_seconds']['median']}s / p90 "
        f"{summary['alert_to_pr_seconds']['p90']}s (n={summary['alert_to_pr_seconds']['n']}), "
        f"tokens total {summary['tokens']['total']}"
    )
    if infra_errors:
        return 2
    return 0 if summary["cases_success"] == summary["cases_total"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
