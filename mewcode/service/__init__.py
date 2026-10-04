"""Headless service layer: alert-driven job intake, execution and PR delivery.

The service is a shell around the existing agent core: it never re-implements
agent behaviour, it persists jobs, schedules them on a worker pool and wires
the result back to the code host. See docs/evolution/02-architecture.md.
"""

from .jobs import (
    InvalidTransition,
    Job,
    JobNotFound,
    JobStore,
    JobStoreError,
)
from .worker import JobHandler, WorkerPool

__all__ = [
    "InvalidTransition",
    "Job",
    "JobHandler",
    "JobNotFound",
    "JobStore",
    "JobStoreError",
    "WorkerPool",
]
