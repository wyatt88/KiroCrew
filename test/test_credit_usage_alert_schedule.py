"""Tests for the credit-usage alert-schedule gateway route (Option B).

Drives ``api_credit_usage_alert_schedule`` with a real ``CronService`` in an
isolated temp store, asserting: enable installs the packaged checker script
into ``<config_dir>/crons/`` and registers exactly one hourly job named
``credit-usage-alert``; disable removes it; a repeated enable does not
accumulate duplicates.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.cron import CronService
from kiro_crew.dashboard.handlers import cron as h

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Isolate both the cron store and config_dir into tmp_path."""
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    # config_dir() is imported into the handler module namespace; patch there.
    monkeypatch.setattr(h, "config_dir", lambda: tmp_path)
    return tmp_path


def _state(svc: CronService) -> MagicMock:
    st = MagicMock()
    st.crons = svc
    st.push_refresh = MagicMock()
    # get_history().delete_job_history is awaited in teardown.
    hist = MagicMock()
    hist.delete_job_history = AsyncMock(return_value=None)
    svc.get_history = MagicMock(return_value=hist)  # type: ignore[method-assign]
    return st


def _req(state: MagicMock, body: dict) -> web.Request:
    app = web.Application()
    app["state"] = state
    req = make_mocked_request("POST", "/api/apps/credit-usage/alert-schedule", app=app)
    req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _jobs(svc: CronService):
    return [j for j in svc.list_jobs(include_disabled=True) if j.name == h._CREDIT_ALERT_JOB_NAME]


async def test_enable_creates_hourly_job_and_installs_script(isolated):
    svc = CronService()
    state = _state(svc)

    resp = await h.api_credit_usage_alert_schedule(_req(state, {"enabled": True}))
    payload = json.loads(resp.body.decode())

    assert payload["ok"] is True and payload["enabled"] is True
    jobs = _jobs(svc)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.schedule.every_secs == 120
    assert job.script.endswith("credit_usage_alert.py:run")
    assert job.hide_in_chat is True
    assert job.minimal_context is True
    assert job.persistent_session is False
    # Script physically installed under <config_dir>/crons/.
    installed = isolated / "crons" / "credit_usage_alert.py"
    assert installed.exists()
    assert "def run(ctx)" in installed.read_text(encoding="utf-8")


async def test_disable_removes_job(isolated):
    svc = CronService()
    state = _state(svc)
    await h.api_credit_usage_alert_schedule(_req(state, {"enabled": True}))
    assert len(_jobs(svc)) == 1

    resp = await h.api_credit_usage_alert_schedule(_req(state, {"enabled": False}))
    payload = json.loads(resp.body.decode())
    assert payload["ok"] is True and payload["enabled"] is False
    assert payload["removed"] == 1
    assert _jobs(svc) == []


async def test_repeated_enable_is_idempotent(isolated):
    svc = CronService()
    state = _state(svc)
    await h.api_credit_usage_alert_schedule(_req(state, {"enabled": True}))
    await h.api_credit_usage_alert_schedule(_req(state, {"enabled": True}))
    await h.api_credit_usage_alert_schedule(_req(state, {"enabled": True}))
    # Never accumulates duplicates.
    assert len(_jobs(svc)) == 1


async def test_invalid_body_is_400(isolated):
    svc = CronService()
    state = _state(svc)
    app = web.Application()
    app["state"] = state
    req = make_mocked_request("POST", "/api/apps/credit-usage/alert-schedule", app=app)
    req.json = AsyncMock(side_effect=ValueError("bad json"))  # type: ignore[method-assign]
    resp = await h.api_credit_usage_alert_schedule(req)
    assert resp.status == 400
