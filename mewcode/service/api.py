"""无头服务的 HTTP 入口（aiohttp）。

对外只暴露四类端点（架构文档第三节）：
- ``POST /webhook/alert``  告警源 webhook（Alertmanager 等，经适配器归一化）
- ``POST /webhook/manual`` 手动触发（本地 JSON，demo 与测试用）
- ``GET  /healthz``        健康检查（队列深度、在途数、状态分布）
- ``GET  /jobs``           调试用 job 查询

鉴权：配置了 ``service.webhook_token`` 时，写端点要求
``X-MewCode-Token`` 头（或 ``Authorization: Bearer``），常数时间比较。
未配置 token 时放行——只允许监听回环地址，这个约束在 CLI 入口处强制
（见 ``mewcode/__main__.py``：非回环监听 + 无 token 直接拒绝启动）。
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any

from aiohttp import web

from .jobs import Job
from .runtime import ServiceRuntime
from .triggers.base import TriggerAdapter, TriggerError

log = logging.getLogger(__name__)

DEFAULT_MAX_BODY_BYTES = 1_000_000
MAX_JOBS_LIMIT = 200


def _job_summary(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "repo": job.repo,
        "title": job.title,
        "status": job.status,
        "severity": job.severity,
        "attempts": job.attempts,
        "branch": job.branch,
        "pr_url": job.pr_url,
        "ci_status": job.ci_status,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _token_ok(request: web.Request, expected: str) -> bool:
    provided = request.headers.get("X-MewCode-Token", "")
    if not provided:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            provided = auth[len("Bearer "):]
    return bool(provided) and hmac.compare_digest(provided, expected)


def create_app(
    runtime: ServiceRuntime,
    adapters: dict[str, TriggerAdapter],
    *,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> web.Application:
    """组装 aiohttp 应用。``adapters`` 至少包含 ``alert`` 与 ``manual``。"""
    token = runtime.config.webhook_token
    if not token:
        log.warning("service.webhook_token is empty: endpoints accept unauthenticated requests")

    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        if request.path.startswith("/webhook/") and token and not _token_ok(request, token):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def read_json(request: web.Request) -> dict[str, Any]:
        # request.read() 走应用级 client_max_size 限制：超限自动 413，
        # 不会把整个大 body 读进内存（手动 read(n) 截断会掩盖超限）。
        body = await request.read()
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": f"invalid JSON: {e}"}),
                content_type="application/json",
            ) from e
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "payload must be a JSON object"}),
                content_type="application/json",
            )
        return payload

    async def handle_webhook(request: web.Request, source: str) -> web.Response:
        payload = await read_json(request)
        adapter = adapters.get(source)
        if adapter is None:
            return web.json_response({"error": f"no adapter configured for '{source}'"}, status=503)
        try:
            parsed = adapter.parse(payload)
        except TriggerError as e:
            return web.json_response({"error": str(e)}, status=400)
        result = await runtime.intake(parsed.drafts)
        body = result.as_dict()
        # 批量告警里被跳过的条目必须可见：这是"告警为什么没被修"的唯一答复处
        body["skipped"] = parsed.skipped
        body["warnings"] = parsed.warnings + body["warnings"]
        if not result.accepted and not result.deduped and parsed.skipped:
            # 全部条目都不可受理：用 4xx 让上游/告警系统看到异常，而不是假装 202
            return web.json_response(body, status=422)
        return web.json_response(body, status=202)

    async def post_alert(request: web.Request) -> web.Response:
        return await handle_webhook(request, "alert")

    async def post_manual(request: web.Request) -> web.Response:
        return await handle_webhook(request, "manual")

    async def healthz(request: web.Request) -> web.Response:
        counts = await runtime.store.count_by_status()
        payload = {
            "status": "ok",
            "queue_depth": runtime.pool.queue_depth,
            "in_flight": len(runtime.pool.in_flight),
            "jobs_by_status": counts,
        }
        status = 200 if runtime.pool.running else 503
        if status != 200:
            payload["status"] = "stopping"
        return web.json_response(payload, status=status)

    async def list_jobs(request: web.Request) -> web.Response:
        raw_limit = request.query.get("limit", "20")
        try:
            limit = max(1, min(int(raw_limit), MAX_JOBS_LIMIT))
        except ValueError:
            return web.json_response({"error": "limit must be an integer"}, status=400)
        status_filter = request.query.get("status") or None
        jobs = await runtime.store.list_jobs(status=status_filter, limit=limit)
        return web.json_response({"jobs": [_job_summary(j) for j in jobs]})

    app = web.Application(middlewares=[auth_middleware], client_max_size=max_body_bytes)
    app.router.add_post("/webhook/alert", post_alert)
    app.router.add_post("/webhook/manual", post_manual)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/jobs", list_jobs)
    return app
