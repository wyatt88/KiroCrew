"""Fire (design: Crew Member = Custom Agent + Wrapper, rollout step 5).

The reverse of hire: the wrapper row, the member's own agent file and its
pristine copy go; the private memory store is archived under the retirement
marker; what the member LIVED -- its DM thread, activity, briefing, rules -- is
archived, not destroyed, unless the request says ``purge``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import test_member_hire as _hire
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import agent_state, member_templates, members
from kiro_crew.config.loader import KiroCrewConfig

APP = _hire.APP
_store_hire = _hire._store_hire
_owner_caller = _hire._owner_caller
agents_dir = _hire.agents_dir
store_app = _hire.store_app

MEMBER = "Pager-triage"
SLUG = "pager-triage"


class _Log:
    """A conversation log that knows which transcripts exist."""

    def __init__(self, *keys: str):
        self.keys = set(keys)

    def has_log(self, key: str) -> bool:
        return key in self.keys


def _app(state: MagicMock | None = None) -> web.Application:
    from kiro_crew.dashboard.handlers import (
        api_member_fire,
        api_member_hire,
        api_member_role_update_get,
        api_members,
    )

    @web.middleware
    async def _auth(request: web.Request, handler):
        request.setdefault("app", "")
        request.setdefault("user", "local-app")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state or MagicMock(sessions=None, _slots={}, conversation_log=None)
    app.router.add_post("/api/members", api_member_hire)
    app.router.add_get("/api/members", api_members)
    app.router.add_get("/api/members/{member}/role-update", api_member_role_update_get)
    app.router.add_post("/api/members/{member}/fire", api_member_fire)
    return app


async def _hire_pager(client) -> None:
    resp = await client.post("/api/members", json=_store_hire("Pager triage"))
    assert resp.status == 200, await resp.text()


def _live_in(slug: str) -> Path:
    """Give the member lived state: activity, a rules file, a DM binding."""
    assert members.record_activity(MEMBER, "s1", "persistent", project="p")
    members.write_member_rules(slug, member=MEMBER, text="Always page the on-call first.")
    members.write_dm_binding(slug, member=MEMBER, slot_key=f"member-{slug}")
    return members.member_dir(slug)


class TestFire:
    @pytest.mark.asyncio
    async def test_gate_fire_retires_the_member_and_archives_what_it_lived(
        self, agents_dir: Path, store_app: Path
    ):
        """The step-5 gate. Row, copy, pristine copy gone; store archived (not
        erased); activity, rules and the thread pointer moved to
        members/.retired with a fired.json; the binding gone; the thread's
        history key answered so it can be found in the History tab."""
        state = MagicMock(
            sessions=None, _slots={}, conversation_log=_Log(f"dashboard:member-{SLUG}")
        )
        async with TestClient(TestServer(_app(state))) as client:
            await _hire_pager(client)
            space = _live_in(SLUG)
            assert (space / "activity.jsonl").exists()
            row = KiroCrewConfig.load().agents[MEMBER]
            store = row.memory_store
            resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
            assert resp.status == 200, await resp.text()
            body = await resp.json()
            roster = {
                r["name"]: r for r in (await (await client.get("/api/members")).json())["members"]
            }
        assert body == {
            "ok": True,
            "thread": {"history_key": f"dashboard:member-{SLUG}", "state": "archived"},
            "lived_state": "archived",
        }
        cfg = KiroCrewConfig.load()
        assert MEMBER not in cfg.agents
        assert MEMBER not in roster
        # The definition and its pristine copy are gone; the lineage too.
        assert not (agents_dir / f"{MEMBER}.json").exists()
        assert agent_state.get_fork_info(MEMBER) is None
        assert not member_templates.pristine_copy_path(MEMBER).exists()
        # The shared materialized template is untouched.
        assert (agents_dir / f"{APP}--triage.json").exists()
        # The private store is archived under the retirement marker, never erased:
        # the files stay, and the store refuses use as archived.
        from kiro_crew.memory_stores import (
            UnknownMemoryStore,
            memory_stores_root,
            require_member_memory_not_archived,
        )

        assert (memory_stores_root() / store).is_dir()
        with pytest.raises(UnknownMemoryStore):
            require_member_memory_not_archived(store, expected_owner=MEMBER)
        # Lived state moved to .retired with the record; the binding is gone.
        assert not space.exists()
        retired = [p for p in members.retired_root().iterdir() if p.name.startswith(f"{SLUG}--")]
        assert len(retired) == 1
        assert (retired[0] / "activity.jsonl").exists()
        # The rules archive under the PROTECTED rules subtree (trust/), not in the
        # agent-writable members root beside the activity.
        assert not (retired[0] / "rules.json").exists()
        rules_archive = [
            p for p in members.retired_rules_root().iterdir() if p.name.startswith(f"{SLUG}--")
        ]
        assert len(rules_archive) == 1
        assert json.loads(rules_archive[0].read_text())["member"] == MEMBER
        assert "trust" in rules_archive[0].parts and "members" not in rules_archive[0].parts
        assert members.read_member_rules(SLUG, MEMBER) == ""
        record = json.loads((retired[0] / members.FIRED_RECORD_FILE).read_text())
        assert record["member"] == MEMBER
        assert record["display_name"] == "Pager triage"
        assert record["thread_history_key"] == f"dashboard:member-{SLUG}"
        assert record["template"] == f"{APP}/triage"
        assert record["fired_at"].endswith("Z")
        assert members.read_dm_binding(SLUG) is None

    @pytest.mark.asyncio
    async def test_the_open_thread_is_closed_like_the_tab_does_before_the_row_goes(
        self, agents_dir: Path, store_app: Path
    ):
        """A live DM session must not keep running against a row that is about
        to vanish: the slot is closed first, through the tab's own close path."""
        closed: list[tuple[str, object]] = []
        fake_slot = object()
        state = MagicMock(
            sessions=None, _slots={f"member-{SLUG}": fake_slot}, conversation_log=None
        )

        async def fake_close(st, slot, name):
            # The row is still there when the thread closes.
            assert MEMBER in KiroCrewConfig.load().agents
            closed.append((name, slot))

        async with TestClient(TestServer(_app(state))) as client:
            await _hire_pager(client)
            _live_in(SLUG)
            with patch("kiro_crew.dashboard.chat_handlers.close_slot", fake_close):
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
                assert resp.status == 200, await resp.text()
        assert closed == [(f"member-{SLUG}", fake_slot)]
        assert MEMBER not in KiroCrewConfig.load().agents

    @pytest.mark.asyncio
    async def test_a_thread_that_cannot_close_leaves_the_member_whole(
        self, agents_dir: Path, store_app: Path
    ):
        from kiro_crew.dashboard.chat_handlers import SlotCloseError

        state = MagicMock(sessions=None, _slots={f"member-{SLUG}": object()}, conversation_log=None)

        async def refuse(st, slot, name):
            raise SlotCloseError("history save failed", "history_save_failed")

        async with TestClient(TestServer(_app(state))) as client:
            await _hire_pager(client)
            space = _live_in(SLUG)
            with patch("kiro_crew.dashboard.chat_handlers.close_slot", refuse):
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
                assert resp.status == 500
                assert (await resp.json())["code"] == "thread_close_failed"
        assert MEMBER in KiroCrewConfig.load().agents
        assert (agents_dir / f"{MEMBER}.json").exists()
        assert space.exists()
        assert members.read_dm_binding(SLUG) is not None

    @pytest.mark.asyncio
    async def test_purge_removes_the_lived_state_and_reports_the_thread_honestly(
        self, agents_dir: Path, store_app: Path
    ):
        """Purge is the explicit request: no .retired copy. The transcript goes
        through the history-delete path; when that path refuses (patched to,
        here -- a cron owning the transcript, an unreadable store) the thread is
        reported KEPT, never claimed gone."""
        state = MagicMock(
            sessions=None, _slots={}, conversation_log=_Log(f"dashboard:member-{SLUG}")
        )
        with patch(
            "kiro_crew.dashboard.handlers.members._purge_thread_history", return_value=False
        ):
            async with TestClient(TestServer(_app(state))) as client:
                await _hire_pager(client)
                space = _live_in(SLUG)
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={"purge": True})
                assert resp.status == 200, await resp.text()
                body = await resp.json()
        assert body["lived_state"] == "purged"
        assert body["thread"] == {"history_key": f"dashboard:member-{SLUG}", "state": "kept"}
        assert not space.exists()
        assert not members.retired_root().exists() or not any(
            p.name.startswith(f"{SLUG}--") for p in members.retired_root().iterdir()
        )
        assert members.read_dm_binding(SLUG) is None
        assert members.read_member_rules(SLUG, MEMBER) == ""
        assert MEMBER not in KiroCrewConfig.load().agents

    @pytest.mark.asyncio
    async def test_purge_deletes_the_transcript_when_the_history_path_allows(
        self, agents_dir: Path, store_app: Path
    ):
        state = MagicMock(
            sessions=None, _slots={}, conversation_log=_Log(f"dashboard:member-{SLUG}")
        )
        with patch(
            "kiro_crew.dashboard.handlers.members._purge_thread_history", return_value=True
        ) as purge:
            async with TestClient(TestServer(_app(state))) as client:
                await _hire_pager(client)
                _live_in(SLUG)
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={"purge": True})
                assert resp.status == 200, await resp.text()
                assert (await resp.json())["thread"]["state"] == "purged"
        purge.assert_called_once_with(state, f"dashboard:member-{SLUG}")

    @pytest.mark.asyncio
    async def test_a_bound_thread_with_no_transcript_reads_as_none_not_kept(
        self, agents_dir: Path, store_app: Path
    ):
        """Opened but nothing said: History holds no transcript, so a purge has
        nothing to delete and must not report a thread it kept."""
        state = MagicMock(sessions=None, _slots={}, conversation_log=_Log())
        async with TestClient(TestServer(_app(state))) as client:
            await _hire_pager(client)
            _live_in(SLUG)
            resp = await client.post(f"/api/members/{MEMBER}/fire", json={"purge": True})
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert body["thread"] == {"history_key": f"dashboard:member-{SLUG}", "state": "none"}
        assert body["lived_state"] == "purged"

    @pytest.mark.asyncio
    async def test_a_member_that_never_lived_leaves_nothing_to_archive(
        self, agents_dir: Path, store_app: Path
    ):
        """A local hire seeds no briefing; a member that never opened its thread
        or wrote a rule has no space at all, so there is nothing to archive."""
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_hire._hire("Plain"))
            assert resp.status == 200, await resp.text()
            resp = await client.post("/api/members/Plain/fire")
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert body["thread"] == {"history_key": "", "state": "none"}
        assert body["lived_state"] == "none"
        assert not members.retired_root().exists() or not any(members.retired_root().iterdir())

    @pytest.mark.asyncio
    async def test_the_default_member_cannot_be_fired_and_bad_bodies_are_refused(
        self, agents_dir: Path, store_app: Path
    ):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members/default/fire", json={})
            assert resp.status == 409
            assert (await resp.json())["code"] == "cannot_fire_default"
            resp = await client.post("/api/members/nobody/fire", json={})
            assert resp.status == 404
            await _hire_pager(client)
            resp = await client.post(f"/api/members/{MEMBER}/fire", json={"purge": "yes"})
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_purge"
            resp = await client.post(f"/api/members/{MEMBER}/fire", json=[])
            assert resp.status == 400
        assert MEMBER in KiroCrewConfig.load().agents
        assert "default" in KiroCrewConfig.load().agents

    @pytest.mark.asyncio
    async def test_a_member_that_changed_under_the_request_is_not_fired(
        self, agents_dir: Path, store_app: Path
    ):
        """The row must still be the one the request saw when the lock is
        taken: a same-id member re-hired in between is somebody else's."""
        from kiro_crew.config.loader import update_config_locked

        real_load = KiroCrewConfig.load
        calls: list[int] = []

        def load_then_swap(*args, **kwargs):
            cfg = real_load(*args, **kwargs)
            calls.append(1)
            if len(calls) == 1:
                # Between the first read and the locked re-read: a replacement.
                def mutate(doc):
                    doc["agents"][MEMBER]["memory_store"] = "member-pager-triage-replacement"
                    return doc

                update_config_locked(mutate=mutate)
            return cfg

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            with patch(
                "kiro_crew.dashboard.handlers.members.KiroCrewConfig.load",
                side_effect=load_then_swap,
            ):
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
                assert resp.status == 409, await resp.text()
                assert (await resp.json())["code"] == "member_changed"
        assert MEMBER in KiroCrewConfig.load().agents
        assert (agents_dir / f"{MEMBER}.json").exists()

    @pytest.mark.asyncio
    async def test_the_same_name_can_be_hired_again_after_a_fire(
        self, agents_dir: Path, store_app: Path
    ):
        """The id is free again and the new colleague starts from nothing: no
        activity, no rules, no binding of the fired one leaks into it."""
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            _live_in(SLUG)
            resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
            assert resp.status == 200, await resp.text()
            await _hire_pager(client)
        row = KiroCrewConfig.load().agents[MEMBER]
        assert row.kiro_agent == MEMBER
        assert (agents_dir / f"{MEMBER}.json").exists()
        assert members.read_activity(SLUG) == []
        assert members.read_dm_binding(SLUG) is None
        assert member_templates.pristine_copy_path(MEMBER).exists()

    def test_retire_refuses_a_link_planted_at_the_member_directory(
        self, agents_dir: Path, tmp_path: Path
    ):
        """The member directory is agent-writable: a symlink planted at its name
        is removed as a link, never moved or followed into the archive."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "secret.txt").write_text("x")
        root = members.members_root()
        root.mkdir(parents=True, exist_ok=True)
        link = root / SLUG
        link.symlink_to(elsewhere, target_is_directory=True)
        archived = members.retire_member_space(SLUG, member=MEMBER, record={})
        assert archived is None
        assert not link.exists() and not link.is_symlink()
        assert (elsewhere / "secret.txt").exists()
        assert not members.retired_root().exists() or not any(members.retired_root().iterdir())

    def test_retire_refuses_a_planted_archive_root(self, agents_dir: Path, tmp_path: Path):
        """``members/.retired`` is agent-writable ground: a symlink planted at
        its name is refused, never followed -- the archive (and the protected
        rules beside it) must not land wherever the link points. Nothing is
        moved when the root is refused."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        space = _live_in(SLUG)
        root = members.members_root()
        (root / members.RETIRED_DIR_NAME).symlink_to(elsewhere, target_is_directory=True)
        with pytest.raises(members.MemberSlugError):
            members.retired_root()
        with pytest.raises(members.MemberSlugError):
            members.retire_member_space(SLUG, member=MEMBER, record={})
        assert space.is_dir() and (space / "activity.jsonl").exists()
        assert not any(elsewhere.iterdir())
        assert members.read_member_rules(SLUG, MEMBER) == "Always page the on-call first."
        # A plain file at the name is refused the same way.
        (root / members.RETIRED_DIR_NAME).unlink()
        (root / members.RETIRED_DIR_NAME).write_text("x")
        with pytest.raises(members.MemberSlugError):
            members.retired_root()

    def test_retire_removes_only_a_binding_that_names_the_member(self, agents_dir: Path):
        """Slugification is lossy: a binding under this slug naming ANOTHER
        member is that member's thread, not this fire's to remove. A binding
        that does not read (tampered) attributes nothing and goes."""
        members.write_dm_binding(SLUG, member="pager-Triage", slot_key=f"member-{SLUG}")
        members.retire_member_space(SLUG, member=MEMBER, record={})
        assert members.read_dm_binding(SLUG)["member"] == "pager-Triage"
        members.write_dm_binding(SLUG, member=MEMBER, slot_key=f"member-{SLUG}")
        members.retire_member_space(SLUG, member=MEMBER, record={})
        assert members.read_dm_binding(SLUG) is None
        members.dm_binding_path(SLUG).parent.mkdir(parents=True, exist_ok=True)
        members.dm_binding_path(SLUG).write_text("not json")
        assert members.remove_dm_binding_of(SLUG, MEMBER) is True
        assert not members.dm_binding_path(SLUG).exists()

    @pytest.mark.asyncio
    async def test_a_member_sharing_its_slug_with_a_live_colleague_is_not_fired(
        self, agents_dir: Path, store_app: Path
    ):
        """``members/<slug>/``, the rules and the binding are keyed by the lossy
        slug, so retiring them would take the colleague's along. The fire is
        refused before anything is closed or removed; the colleague renames
        (or is fired) first."""
        from kiro_crew.config.loader import KiroCrewAgentConfig, update_config_locked

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            _live_in(SLUG)

            def add_twin(doc):
                twin = KiroCrewAgentConfig(kiro_agent="kirocrew", memory_store="default")
                from dataclasses import asdict

                doc["agents"]["pager-Triage"] = asdict(twin)
                return doc

            update_config_locked(mutate=add_twin)
            resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
            assert resp.status == 409, await resp.text()
            body = await resp.json()
            assert body["code"] == "slug_collision" and "pager-Triage" in body["error"]
        assert MEMBER in KiroCrewConfig.load().agents
        assert members.member_dir(SLUG).is_dir()
        assert members.read_dm_binding(SLUG)["member"] == MEMBER

    @pytest.mark.asyncio
    async def test_a_binding_naming_a_ghost_of_the_slug_is_not_this_members_thread(
        self, agents_dir: Path, store_app: Path
    ):
        """A binding left by a deleted same-slug member names nobody live: the
        fire closes no slot for it and reports no thread, and the stale binding
        goes with the space."""
        slots = {f"member-{SLUG}": MagicMock()}
        state = MagicMock(
            sessions=None, _slots=slots, conversation_log=_Log(f"dashboard:member-{SLUG}")
        )
        async with TestClient(TestServer(_app(state))) as client:
            await _hire_pager(client)
            _live_in(SLUG)
            members.write_dm_binding(SLUG, member="pager-Triage", slot_key=f"member-{SLUG}")
            with patch("kiro_crew.dashboard.chat_handlers.close_slot") as close:
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
                assert resp.status == 200, await resp.text()
                close.assert_not_called()
                body = await resp.json()
        assert body["thread"] == {"history_key": "", "state": "none"}
        # The ghost's binding is not this member's to remove; it stays.
        assert members.read_dm_binding(SLUG)["member"] == "pager-Triage"

    @pytest.mark.asyncio
    async def test_a_fire_interrupted_after_the_row_went_is_resumed_by_the_next_fire(
        self, agents_dir: Path, store_app: Path
    ):
        """The intent is recorded before the row goes. A cleanup step that
        fails afterwards answers 500 ``fire_incomplete`` (never a silent
        success) and leaves the marker; firing the same member again finishes
        the archive, and the marker is cleared only then."""
        from kiro_crew.dashboard.handlers import members as handlers

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            space = _live_in(SLUG)
            with patch(
                "kiro_crew.dashboard.handlers.members.members_mod.retire_member_space",
                side_effect=OSError("disk full"),
            ):
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
                assert resp.status == 500, await resp.text()
                body = await resp.json()
                assert body["code"] == "fire_incomplete" and body["resumable"] is True
            # The row is gone, the files are not, the intent is on disk.
            assert MEMBER not in KiroCrewConfig.load().agents
            assert space.is_dir()
            marker = handlers._read_fire_marker(MEMBER)
            assert marker["slug"] == SLUG and marker["record"]["display_name"] == "Pager triage"
            # The retry resumes from the marker instead of answering 404.
            resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
            assert resp.status == 200, await resp.text()
            assert (await resp.json())["lived_state"] == "archived"
        assert not space.exists()
        assert handlers._read_fire_marker(MEMBER) is None
        retired = [p for p in members.retired_root().iterdir() if p.name.startswith(f"{SLUG}--")]
        assert len(retired) == 1
        assert (
            json.loads((retired[0] / members.FIRED_RECORD_FILE).read_text())["display_name"]
            == "Pager triage"
        )
        # And with nothing pending, an unknown member is still a 404.
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members/nobody/fire", json={})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_the_fire_holds_the_thread_lock_the_open_takes(
        self, agents_dir: Path, store_app: Path
    ):
        """Thread open and fire serialize on one lock, so an open cannot
        re-bind the slug between the fire's binding read and its retirement."""
        from kiro_crew.dashboard.handlers import members as handlers

        observed: list[bool] = []
        real = members.retire_member_space

        def observe(*args, **kwargs):
            observed.append(handlers._dm_thread_lock.locked())
            return real(*args, **kwargs)

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            with patch(
                "kiro_crew.dashboard.handlers.members.members_mod.retire_member_space", observe
            ):
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
                assert resp.status == 200, await resp.text()
        assert observed == [True]
        assert not handlers._dm_thread_lock.locked()

    @pytest.mark.asyncio
    async def test_a_hire_is_refused_while_a_fire_of_that_id_is_pending(
        self, agents_dir: Path, store_app: Path
    ):
        """An interrupted fire's marker means lived state under this id is still
        on disk waiting to be archived; a new member would inherit it (or lose
        its own files to the resumed cleanup). The hire says so; the resumed
        fire clears the way."""
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            _live_in(SLUG)
            with patch(
                "kiro_crew.dashboard.handlers.members.members_mod.retire_member_space",
                side_effect=OSError("disk full"),
            ):
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
                assert resp.status == 500
            resp = await client.post("/api/members", json=_store_hire("Pager triage"))
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == "fire_pending"
            assert MEMBER not in KiroCrewConfig.load().agents
            resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
            assert resp.status == 200, await resp.text()
            await _hire_pager(client)
        assert members.read_activity(SLUG) == []

    @pytest.mark.asyncio
    async def test_the_cleanup_runs_in_the_same_config_lock_hold_as_the_row_removal(
        self, agents_dir: Path, store_app: Path
    ):
        """Steps 3 and 4 run with the config lock still held, so a hire of the
        same id (which takes the lock to publish) cannot interleave and have
        this fire remove the copy, pristine copy or space it just received."""
        observed: list[bool] = []
        real = members.retire_member_space

        def observe(*args, **kwargs):
            observed.append(_hire._agents._get_config_lock().locked())
            return real(*args, **kwargs)

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            with patch(
                "kiro_crew.dashboard.handlers.members.members_mod.retire_member_space", observe
            ):
                resp = await client.post(f"/api/members/{MEMBER}/fire", json={})
                assert resp.status == 200, await resp.text()
        assert observed == [True]

    @pytest.mark.asyncio
    async def test_a_resumed_fire_never_touches_a_member_that_exists_again(
        self, agents_dir: Path, store_app: Path
    ):
        """A row under the id while a marker is pending (a hand-edited config:
        the hire refuses) is somebody else's member; the resume refuses, leaves
        the marker and touches none of the new member's files."""
        from kiro_crew.dashboard.handlers import members as handlers

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            space = _live_in(SLUG)
            intent = {
                "member": MEMBER,
                "slug": SLUG,
                "copy_name": MEMBER,
                "purge": False,
                "record": {"display_name": "Pager triage"},
            }
            handlers._write_fire_marker(MEMBER, intent)
            state = client.server.app["state"]
            resp = await handlers._finish_fire(MagicMock(), state, MEMBER, intent, False)
            assert resp.status == 409
            assert json.loads(resp.text)["code"] == "member_changed"
        assert MEMBER in KiroCrewConfig.load().agents
        assert (agents_dir / f"{MEMBER}.json").exists()
        assert member_templates.pristine_copy_path(MEMBER).exists()
        assert space.is_dir() and members.read_dm_binding(SLUG)["member"] == MEMBER
        assert handlers._read_fire_marker(MEMBER) is not None

    def test_a_directory_swapped_for_a_link_mid_retire_is_not_followed(
        self, agents_dir: Path, tmp_path: Path
    ):
        """Every step of the move is relative to a pinned descriptor of the
        members root and of the archive root: an entry that reads as a link at
        rename time is unlinked, never renamed; a plain path check followed by a
        path rename would act on whatever the swapped-in link points at."""
        if not members._DIR_FD_SUPPORTED:
            pytest.skip("dir_fd-relative rename is POSIX")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "secret.txt").write_text("x")
        space = _live_in(SLUG)
        assert space.is_dir()
        real_lstat = members.os.lstat

        def swap_then_lstat(*args, **kwargs):
            # The swap lands between the caller's view of a directory and the
            # pinned lstat: the pinned flow sees the link and unlinks it.
            if args and args[0] == SLUG and not getattr(swap_then_lstat, "done", False):
                swap_then_lstat.done = True
                import shutil

                shutil.rmtree(space)
                space.symlink_to(elsewhere, target_is_directory=True)
            return real_lstat(*args, **kwargs)

        with patch.object(members.os, "lstat", swap_then_lstat):
            archived = members.retire_member_space(SLUG, member=MEMBER, record={})
        assert not space.exists() and not space.is_symlink()
        assert (elsewhere / "secret.txt").exists()
        # The rules (archived from trust/) still produced an archive dir with the record.
        assert archived is not None and (archived / members.FIRED_RECORD_FILE).is_file()
        assert not any(p.name == "secret.txt" for p in archived.iterdir())
