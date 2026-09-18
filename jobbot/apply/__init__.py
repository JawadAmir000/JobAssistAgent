"""Apply layer: one adapter per ATS, plus the answers resolver.

Public API:
    from jobbot.apply import get_adapter_for, run_application
    run_application(app_id) -> None        # blocking; runs in a worker thread; updates applications row as it goes
    resume_application(app_id, answer) -> None   # after user answers a needs_you question
"""
from jobbot.apply.base import Adapter, ApplyContext, NeedsHuman, get_adapter_for  # noqa: F401
from jobbot.apply.runner import run_application, resume_application  # noqa: F401
