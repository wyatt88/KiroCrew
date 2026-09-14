"""Member thread projection without an inbox: peer rows on the plain transcript.

Three seams, one PR:

* ``authorize_target`` gains two narrow ``send``-only allows in front of the
  ``not_creator`` fence -- member -> member, and child -> the session that
  created it. Every other operation, and every other caller/target pair, is
  refused exactly as before.
* ``send_to_target`` stamps ``meta.sent_by`` on the delivered row (the text
  prefix the model reads is unchanged) and steers a BUSY member instead of
  queueing behind it.
* ``send_message(session="origin")`` from a non-cron caller reaches the session
  that created the caller, through the same delivery, instead of degrading to
  the bell.
"""

from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_delivery as cd
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.members import DM_SLOT_KEY_PREFIX, DM_SLOT_MODE

if "kiro_crew.slack.handler" not in sys.modules:
    _stub = types.ModuleType("kiro_crew.slack.handler")
    _stub.is_allowed_user = lambda uid: False  # type: ignore[attr-defined]
    _stub.is_tracked_channel = lambda cid: False  # type: ignore[attr-defined]
    sys.modules["kiro_crew.slack.handler"] = _stub

from kiro_crew.dashboard.handlers import api_send_message  # noqa: E402

MEMBER_A = DM_SLOT_KEY_PREFIX + "conductor"
MEMBER_B = DM_SLOT_KEY_PREFIX + "autofix"


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


# ── authorization matrix (fake slots, real gate order) ───────────────────────


def _fake(key: str, *, created_by: str = "", mode: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        workspace="default",
        memory_mode="persistent",
        _app="",
        linked_session_key="",
        _created_by=created_by,
        mode=mode,
        running=False,
        messages=[],
        executor="local",
    )


class _State:
    def __init__(self, slots: dict[str, SimpleNamespace]):
        self._slots = slots

    def get_slot(self, key: str):
        return self._slots.get(key)


def _authorize(state: _State, caller_key: str, target_key: str, operation: str):
    with (
        patch.object(sc, "caller_slot_key", return_value=caller_key),
        patch.object(sc, "member_dispatch_enabled", return_value=True),
        patch.object(sc, "_resolve_slot", return_value=state._slots.get(target_key)),
    ):
        return sc.authorize_target(
            state,
            caller_session_key="dashboard:whatever",
            target=target_key,
            operation=operation,
        )


class TestPeerMemberAllow:
    def _two_members(self) -> _State:
        return _State(
            {
                MEMBER_A: _fake(MEMBER_A, mode=DM_SLOT_MODE),
                MEMBER_B: _fake(MEMBER_B, mode=DM_SLOT_MODE),
            }
        )

    def test_member_may_send_to_another_member(self):
        state = self._two_members()
        slot = _authorize(state, MEMBER_A, MEMBER_B, "send")
        assert slot.key == MEMBER_B

    @pytest.mark.parametrize("operation", ["stop", "close", "read"])
    def test_member_may_not_stop_close_or_read_a_peer(self, operation):
        state = self._two_members()
        with pytest.raises(sc.SessionControlError) as exc_info:
            _authorize(state, MEMBER_A, MEMBER_B, operation)
        assert exc_info.value.code == "not_creator"

    def test_non_member_caller_still_cannot_send_to_a_member(self):
        """An agent-created ordinary session is fenced to what it created; a
        member thread it did not create stays out of reach."""
        state = _State(
            {
                "chat-1-agent": _fake("chat-1-agent", created_by="chat-1-owner"),
                MEMBER_B: _fake(MEMBER_B, mode=DM_SLOT_MODE),
            }
        )
        with pytest.raises(sc.SessionControlError) as exc_info:
            _authorize(state, "chat-1-agent", MEMBER_B, "send")
        assert exc_info.value.code == "not_creator"

    def test_a_squatter_on_a_member_key_is_not_a_member_target(self):
        """The allow needs the member MODE, not only the key prefix."""
        state = _State(
            {
                MEMBER_A: _fake(MEMBER_A, mode=DM_SLOT_MODE),
                MEMBER_B: _fake(MEMBER_B, mode=""),
            }
        )
        with pytest.raises(sc.SessionControlError) as exc_info:
            _authorize(state, MEMBER_A, MEMBER_B, "send")
        assert exc_info.value.code == "not_creator"

    def test_member_still_cannot_send_to_the_users_own_session(self):
        state = _State(
            {
                MEMBER_A: _fake(MEMBER_A, mode=DM_SLOT_MODE),
                "chat-1-user": _fake("chat-1-user"),
            }
        )
        with pytest.raises(sc.SessionControlError) as exc_info:
            _authorize(state, MEMBER_A, "chat-1-user", "send")
        assert exc_info.value.code == "not_creator"


class TestReportToCreatorAllow:
    def _worker_and_creator(self) -> _State:
        return _State(
            {
                MEMBER_A: _fake(MEMBER_A, mode=DM_SLOT_MODE),
                "chat-1-w1": _fake("chat-1-w1", created_by=MEMBER_A),
            }
        )

    def test_child_may_send_to_its_creator(self):
        slot = _authorize(self._worker_and_creator(), "chat-1-w1", MEMBER_A, "send")
        assert slot.key == MEMBER_A

    @pytest.mark.parametrize("operation", ["stop", "close", "read"])
    def test_child_may_not_stop_close_or_read_its_creator(self, operation):
        with pytest.raises(sc.SessionControlError) as exc_info:
            _authorize(self._worker_and_creator(), "chat-1-w1", MEMBER_A, operation)
        assert exc_info.value.code == "not_creator"

    def test_child_may_not_send_to_a_stranger(self):
        state = _State(
            {
                MEMBER_A: _fake(MEMBER_A, mode=DM_SLOT_MODE),
                "chat-1-w1": _fake("chat-1-w1", created_by="chat-9-other"),
            }
        )
        with pytest.raises(sc.SessionControlError) as exc_info:
            _authorize(state, "chat-1-w1", MEMBER_A, "send")
        assert exc_info.value.code == "not_creator"


# ── delivery: steer vs turn vs queue, and the meta shape ─────────────────────


def _key(slot) -> str:
    return slot_history_key(slot)


def _busy(slot):
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


def _steerable(accepted: bool = True) -> MagicMock:
    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(return_value=accepted)
    return client


def _members(state):
    a = state.get_or_create_slot(MEMBER_A, agent="kirocrew-conductor", mode=DM_SLOT_MODE)
    b = state.get_or_create_slot(MEMBER_B, agent="kirocrew-autofix", mode=DM_SLOT_MODE)
    return a, b


class TestSendToMember:
    def test_idle_member_starts_a_turn_with_sent_by_and_the_prefix(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        a, b = _members(state)
        a.title = "Conductor"
        ran: dict[str, str] = {}

        async def _fake_run_chat(_state, slot, prompt):
            ran["prompt"] = prompt

        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _fake_run_chat)

        async def _drive():
            out = await sc.send_to_target(
                state, caller_session_key=_key(a), target=MEMBER_B, message="take the first task"
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return out

        out = asyncio.run(_drive())
        assert out == {"ok": True, "target": MEMBER_B, "started": True, "steered": False}
        # The model still reads the exact provenance prefix.
        assert ran["prompt"].startswith(f"[sent by session {MEMBER_A} via session_send]\n\n")
        (row,) = [m for m in b.messages if m.get("role") == "user"]
        assert row["content"].startswith("[sent by session ")
        sent_by = row["meta"]["sent_by"]
        assert sent_by == {
            "session_key": MEMBER_A,
            "via": "session_send",
            "title": "Conductor",
            "agent": "kirocrew-conductor",
            "member_slug": "conductor",
        }

    def test_busy_member_is_steered_not_queued(self, tmp_path):
        state = _make_state(tmp_path)
        a, b = _members(state)
        _busy(b)
        b._acp_client = _steerable()

        out = asyncio.run(
            sc.send_to_target(
                state, caller_session_key=_key(a), target=MEMBER_B, message="one more thing"
            )
        )
        assert out["steered"] is True and out["started"] is False
        b._acp_client.steer.assert_awaited_once()
        assert b._acp_client.steer.await_args.args[0].startswith("[sent by session ")
        assert b._queue == []
        (row,) = [m for m in b.messages if m.get("role") == "user"]
        assert row["meta"]["steer"] is True
        assert row["meta"]["sent_by"]["via"] == "session_send"
        assert row["meta"]["sent_by"]["member_slug"] == "conductor"

    def test_busy_member_without_a_steerable_client_queues_with_meta(self, tmp_path):
        state = _make_state(tmp_path)
        a, b = _members(state)
        _busy(b)
        b._acp_client = None

        out = asyncio.run(
            sc.send_to_target(state, caller_session_key=_key(a), target=MEMBER_B, message="later")
        )
        assert out["steered"] is False and out["started"] is False
        (entry,) = b._queue
        assert "later" in entry["content"]
        assert entry["meta"]["sent_by"]["session_key"] == MEMBER_A

    def test_busy_non_member_target_still_queues(self, tmp_path):
        """The steer path is the member thread's; an ordinary peer keeps the
        queue behaviour and the steer client is never touched."""
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")
        target = state.get_or_create_slot("chat-2")
        _busy(target)
        target._acp_client = _steerable()

        out = asyncio.run(
            sc.send_to_target(state, caller_session_key=_key(caller), target="chat-2", message="hi")
        )
        assert out["steered"] is False and out["started"] is False
        target._acp_client.steer.assert_not_awaited()
        (entry,) = target._queue
        assert entry["meta"]["sent_by"]["via"] == "session_send"
        assert "member_slug" not in entry["meta"]["sent_by"]

    def test_steer_push_frame_carries_the_provenance(self, tmp_path):
        state = _make_state(tmp_path)
        a, b = _members(state)
        _busy(b)
        b._acp_client = _steerable()
        frames: list[tuple[str, dict]] = []
        state.broadcast_ws = lambda kind, payload: frames.append((kind, payload))

        asyncio.run(
            sc.send_to_target(state, caller_session_key=_key(a), target=MEMBER_B, message="x")
        )
        (payload,) = [p for k, p in frames if k == "steer_push"]
        assert payload["sentBy"]["session_key"] == MEMBER_A
        assert payload["sentBy"]["via"] == "session_send"


class TestSentByMeta:
    def test_member_caller_shape(self, tmp_path):
        state = _make_state(tmp_path)
        a, _ = _members(state)
        a.title = "Conductor"
        rec = sc.sent_by_meta(state, MEMBER_A, via=sc.SENT_BY_VIA_SESSION_SEND)
        assert rec == {
            "session_key": MEMBER_A,
            "via": "session_send",
            "title": "Conductor",
            "agent": "kirocrew-conductor",
            "member_slug": "conductor",
        }

    def test_ordinary_caller_has_no_member_slug(self, tmp_path):
        state = _make_state(tmp_path)
        w = state.get_or_create_slot("chat-1-w1", agent="kirocrew-worker")
        rec = sc.sent_by_meta(state, "chat-1-w1", via=sc.SENT_BY_VIA_SEND_MESSAGE_ORIGIN)
        assert rec["via"] == "send_message_origin"
        assert rec["agent"] == "kirocrew-worker"
        assert "member_slug" not in rec
        assert rec["session_key"] == w.key

    def test_unknown_caller_still_yields_a_record(self, tmp_path):
        state = _make_state(tmp_path)
        rec = sc.sent_by_meta(state, "chat-gone", via=sc.SENT_BY_VIA_SESSION_SEND)
        assert rec == {"session_key": "chat-gone", "via": "session_send", "title": "", "agent": ""}


# ── send_message(session="origin") for a non-cron caller ─────────────────────


class TestDeliverToCreator:
    def test_worker_reports_into_its_creators_thread(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        a, _ = _members(state)
        worker = state.get_or_create_slot("chat-1-w1", agent="kirocrew-worker")
        worker._created_by = a.key
        ran: dict[str, str] = {}

        async def _fake_run_chat(_state, slot, prompt):
            ran["slot"] = slot.key
            ran["prompt"] = prompt

        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _fake_run_chat)

        async def _drive():
            out = await sc.deliver_to_creator(
                state, caller_session_key=_key(worker), text="Triage #42 done: fixed the flake"
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return out

        out = asyncio.run(_drive())
        assert out == {"target": a.key, "started": True, "steered": False}
        assert ran["slot"] == a.key
        assert ran["prompt"].startswith(f"[sent by session {worker.key} via send_message]\n\n")
        (row,) = [m for m in a.messages if m.get("role") == "user"]
        assert row["meta"]["sent_by"]["via"] == "send_message_origin"
        assert row["meta"]["sent_by"]["session_key"] == worker.key
        assert "member_slug" not in row["meta"]["sent_by"]

    def test_busy_member_creator_is_steered(self, tmp_path):
        state = _make_state(tmp_path)
        a, _ = _members(state)
        worker = state.get_or_create_slot("chat-1-w1")
        worker._created_by = a.key
        _busy(a)
        a._acp_client = _steerable()

        out = asyncio.run(
            sc.deliver_to_creator(state, caller_session_key=_key(worker), text="done")
        )
        assert out == {"target": a.key, "started": False, "steered": True}

    def test_orphan_caller_has_no_origin(self, tmp_path):
        state = _make_state(tmp_path)
        orphan = state.get_or_create_slot("chat-1-solo")
        assert (
            asyncio.run(sc.deliver_to_creator(state, caller_session_key=_key(orphan), text="x"))
            is None
        )

    def test_unknown_caller_has_no_origin(self, tmp_path):
        state = _make_state(tmp_path)
        assert (
            asyncio.run(sc.deliver_to_creator(state, caller_session_key="cron:abc", text="x"))
            is None
        )

    def test_creator_gone_falls_back(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        worker = state.get_or_create_slot("chat-1-w1")
        worker._created_by = "chat-0-vanished"

        async def _no_rehydrate(_state, _key):
            return None

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_persistence.rehydrate_slot_from_history_async",
            _no_rehydrate,
        )
        assert (
            asyncio.run(sc.deliver_to_creator(state, caller_session_key=_key(worker), text="x"))
            is None
        )

    def test_creator_in_another_workspace_is_refused(self, tmp_path):
        """The child->creator allow is an exemption from the ownership fence
        only; every other containment refusal still applies."""
        state = _make_state(tmp_path)
        creator = state.get_or_create_slot("chat-0-creator", workspace="alpha")
        worker = state.get_or_create_slot("chat-1-w1", workspace="beta")
        worker._created_by = creator.key
        assert (
            asyncio.run(sc.deliver_to_creator(state, caller_session_key=_key(worker), text="x"))
            is None
        )
        assert not [m for m in creator.messages if m.get("role") == "user"]


def _make_app(state) -> web.Application:
    app = web.Application()
    app.router.add_post("/api/send-message", api_send_message)
    app["state"] = state
    return app


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.sel.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


class TestSendMessageOriginRoute:
    def _state(self):
        state = MagicMock()
        state.slack_client = None
        state.owner_id = ""
        state.crons.list_jobs.return_value = []
        return state

    @pytest.mark.asyncio
    async def test_non_cron_caller_with_a_creator_is_delivered_as_session(self, mock_sel):
        state = self._state()
        landed = AsyncMock(return_value={"target": MEMBER_A, "started": True, "steered": False})
        with patch("kiro_crew.dashboard.session_control.deliver_to_creator", landed):
            async with TestClient(TestServer(_make_app(state))) as c:
                resp = await c.post(
                    "/api/send-message",
                    json={"text": "report", "session": "origin"},
                    headers={"X-Session-Key": "dashboard:chat-1-w1"},
                )
                data = await resp.json()
        assert data["delivered_to"] == "session"
        landed.assert_awaited_once()
        assert landed.await_args.kwargs["caller_session_key"] == "dashboard:chat-1-w1"
        state.notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_cron_caller_without_a_creator_falls_back_to_the_bell(self, mock_sel):
        state = self._state()
        landed = AsyncMock(return_value=None)
        with patch("kiro_crew.dashboard.session_control.deliver_to_creator", landed):
            async with TestClient(TestServer(_make_app(state))) as c:
                resp = await c.post(
                    "/api/send-message",
                    json={"text": "report", "session": "origin"},
                    headers={"X-Session-Key": "dashboard:chat-1-solo"},
                )
                data = await resp.json()
        assert data["delivered_to"] == "notification"
        state.notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_cron_caller_keeps_the_job_origin_path(self, mock_sel):
        """A cron's origin is its job's session, never a creator lookup."""
        state = self._state()
        landed = AsyncMock(return_value=None)
        with patch("kiro_crew.dashboard.session_control.deliver_to_creator", landed):
            async with TestClient(TestServer(_make_app(state))) as c:
                await c.post(
                    "/api/send-message",
                    json={"text": "report", "session": "origin", "caller_session": "cron:job1"},
                    headers={"X-Session-Key": "cron:job1"},
                )
        landed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_header_means_no_creator_lookup(self, mock_sel):
        state = self._state()
        landed = AsyncMock(return_value=None)
        with patch("kiro_crew.dashboard.session_control.deliver_to_creator", landed):
            async with TestClient(TestServer(_make_app(state))) as c:
                await c.post("/api/send-message", json={"text": "report", "session": "origin"})
        landed.assert_not_awaited()


class TestSteerCarriesSentBy:
    def test_steer_row_and_frame_without_sent_by_are_unchanged(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        _busy(slot)
        slot._acp_client = _steerable()
        frames: list[tuple[str, dict]] = []
        state.broadcast_ws = lambda kind, payload: frames.append((kind, payload))
        asyncio.run(cd.steer_into_running_turn(state, slot, "plain steer"))
        (row,) = [m for m in slot.messages if m.get("role") == "user"]
        assert "sent_by" not in row["meta"]
        (payload,) = [p for k, p in frames if k == "steer_push"]
        assert "sentBy" not in payload
