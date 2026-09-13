"""Hire a crew member from a local Custom Agent file — POST /api/members.

Rollout step 2 of *Crew Member = Custom Agent + Wrapper Layer*: the "adopt"
path. A hire (1) creates the wrapper row through the same validated create
path ``POST /api/agents`` uses, minting the id from the display name, then
(2) copies the source definition into a member-owned agent file whose declared
``name`` is the copy's stem (derived from the member id) and rebinds the row to
it — the private-copy fork the crew editor already uses on first edit.

The step-2 GATE: two members hired from ONE file coexist, each with its own
row and its own copy.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from member_memory_helpers import patch_private_memory_supported

from kiro_crew import agent_state
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard.handlers import agents as _agents

SOURCE = "reviewer"


@pytest.fixture(autouse=True)
def _owner_caller(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )
    patch_private_memory_supported(monkeypatch)


@pytest.fixture
def agents_dir(tmp_path: Path):
    d = tmp_path / "agents"
    d.mkdir()
    spec = {
        "name": SOURCE,
        "description": "Reviews pull requests.",
        "prompt": "You review code.",
        "model": "claude-x",
        "tools": ["ReadFile"],
    }
    (d / f"{SOURCE}.json").write_text(json.dumps(spec), encoding="utf-8")
    cfg = KiroCrewConfig()
    cfg.agents = {"default": KiroCrewAgentConfig(kiro_agent="kirocrew")}
    cfg.default_agent = "default"
    cfg.save()
    # `list_agents()` (the create path's existence probe) and the fork's spec
    # reads both resolve the agents directory through kiro_crew.agent.
    with (
        patch("kiro_crew.agent.KIRO_AGENTS_DIR", d),
        patch(
            "kiro_crew.dashboard.handlers.agents.list_agents",
            lambda *a, **k: [SimpleNamespace(name=SOURCE), SimpleNamespace(name="kirocrew")],
        ),
    ):
        yield d


def _app() -> web.Application:
    from kiro_crew.dashboard.handlers import (
        api_kirocrew_agent_update,
        api_kirocrew_agents,
        api_member_hire,
        api_members,
    )

    @web.middleware
    async def _auth(request: web.Request, handler):
        request.setdefault("app", "")
        request.setdefault("user", "local-app")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = MagicMock(sessions=None)
    app.router.add_post("/api/members", api_member_hire)
    app.router.add_get("/api/members", api_members)
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    app.router.add_get("/api/agents", api_kirocrew_agents)
    return app


def _hire(display_name: str, **extra) -> dict:
    body = {"display_name": display_name, "source": {"kind": "local", "agent": SOURCE}}
    body.update(extra)
    return body


class TestHire:
    @pytest.mark.asyncio
    async def test_hire_creates_row_and_member_owned_copy(self, agents_dir: Path):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_hire("Checkout triage", role="Oncall"))
            assert resp.status == 200, await resp.text()
            data = await resp.json()
        # The answer is what a caller reads: the minted id and the copy. The
        # label, store and source are read back from the roster row.
        assert data == {"ok": True, "id": "Checkout-triage"}
        # The copy: a real file whose declared name equals its stem, carrying
        # the source definition; the source itself is untouched.
        copy = json.loads((agents_dir / "Checkout-triage.json").read_text(encoding="utf-8"))
        assert copy["name"] == "Checkout-triage"
        assert copy["prompt"] == "You review code."
        assert json.loads((agents_dir / f"{SOURCE}.json").read_text())["name"] == SOURCE
        # Lineage for the fork refresh, and the row rebound to the copy.
        assert agent_state.get_fork_info("Checkout-triage") == {
            "forked_from": SOURCE,
            "private_to": "Checkout-triage",
        }
        row = KiroCrewConfig.load().agents["Checkout-triage"]
        assert row.kiro_agent == "Checkout-triage"
        assert row.display_name == "Checkout triage"
        assert row.role == "Oncall"

    @pytest.mark.asyncio
    async def test_gate_two_members_from_one_file_coexist(self, agents_dir: Path):
        async with TestClient(TestServer(_app())) as client:
            a = await (await client.post("/api/members", json=_hire("Checkout triage"))).json()
            b = await (await client.post("/api/members", json=_hire("Payments triage"))).json()
            roster = {
                r["name"]: r for r in (await (await client.get("/api/members")).json())["members"]
            }
        assert a["id"] == "Checkout-triage" and b["id"] == "Payments-triage"
        assert a["ok"] and b["ok"]
        # Two rows, two copies, one untouched source.
        cfg = KiroCrewConfig.load()
        assert cfg.agents["Checkout-triage"].kiro_agent == "Checkout-triage"
        assert cfg.agents["Payments-triage"].kiro_agent == "Payments-triage"
        assert (agents_dir / "Checkout-triage.json").exists()
        assert (agents_dir / "Payments-triage.json").exists()
        assert json.loads((agents_dir / f"{SOURCE}.json").read_text())["name"] == SOURCE
        # Both listed, each under its own label; both created here.
        assert roster["Checkout-triage"]["display_name"] == "Checkout triage"
        assert roster["Payments-triage"]["display_name"] == "Payments triage"
        assert roster["Checkout-triage"]["source"] == "kirocrew"
        # Lineage on the roster: each is bound to its OWN copy of the source,
        # and the row says which template that copy came from. The built-in
        # default, bound to a shared template directly, reports none.
        assert roster["Checkout-triage"]["kiro_agent"] == "Checkout-triage"
        assert roster["Checkout-triage"]["template_origin"] == SOURCE
        assert roster["Payments-triage"]["template_origin"] == SOURCE
        assert roster["default"]["template_origin"] == ""

    @pytest.mark.asyncio
    async def test_same_display_name_twice_is_409_not_a_second_copy(self, agents_dir: Path):
        async with TestClient(TestServer(_app())) as client:
            assert (await client.post("/api/members", json=_hire("Triage"))).status == 200
            resp = await client.post("/api/members", json=_hire("Triage"))
            assert resp.status == 409
            assert (await resp.json())["code"] == "agent_exists"
            # "triage" and "Triage" are distinct ids (the grammar is case-sensitive)
            # but share one SLUG -- the key of the member's space, rules and thread
            # binding -- so the hire refuses the second before anything is written.
            resp = await client.post("/api/members", json=_hire("triage"))
            assert resp.status == 409
            assert (await resp.json())["code"] == "slug_collision"
        assert sorted(p.stem for p in agents_dir.glob("*.json")) == ["Triage", SOURCE]

    @pytest.mark.asyncio
    async def test_copy_stem_is_suffixed_when_a_file_already_owns_the_id(self, agents_dir: Path):
        """An unrelated template already named like the member: the copy takes
        the next free stem and the row binds to THAT, never to the stranger."""
        (agents_dir / "Triage.json").write_text(json.dumps({"name": "Triage", "prompt": "other"}))
        with patch(
            "kiro_crew.dashboard.handlers.agents.list_agents",
            lambda *a, **k: [SimpleNamespace(name=SOURCE), SimpleNamespace(name="Triage")],
        ):
            async with TestClient(TestServer(_app())) as client:
                data = await (await client.post("/api/members", json=_hire("Triage"))).json()
        assert data["id"] == "Triage"
        assert KiroCrewConfig.load().agents["Triage"].kiro_agent == "Triage-2"
        assert (
            json.loads((agents_dir / "Triage-2.json").read_text())["prompt"] == "You review code."
        )
        assert json.loads((agents_dir / "Triage.json").read_text())["prompt"] == "other"
        assert KiroCrewConfig.load().agents["Triage"].kiro_agent == "Triage-2"

    @pytest.mark.asyncio
    async def test_no_moment_exists_where_a_row_is_bound_to_the_shared_source(
        self, agents_dir: Path
    ):
        """The copy is made BEFORE the row exists, inside the create's lock hold,
        and the row is published already bound to it. A concurrent thread open
        can therefore never resolve a member bound to the shared template."""
        real_copy = _agents.__dict__["_write_private_copy"]
        real_persist = _agents.__dict__["persist_member_config"]
        seen: dict[str, object] = {}

        def copy_then_look(*args, **kwargs):
            result = real_copy(*args, **kwargs)
            # The copy exists; the row does not, yet.
            seen["row_at_copy"] = "Triage" in KiroCrewConfig.load().agents
            seen["file_at_copy"] = (agents_dir / "Triage.json").exists()
            return result

        def persist_then_look(cfg, name, **kwargs):
            # What is about to be published is bound to the COPY, never the source.
            seen["binding_at_persist"] = cfg.agents[name].kiro_agent
            return real_persist(cfg, name, **kwargs)

        with (
            patch("kiro_crew.dashboard.handlers.agents._write_private_copy", copy_then_look),
            patch("kiro_crew.dashboard.handlers.agents.persist_member_config", persist_then_look),
        ):
            async with TestClient(TestServer(_app())) as client:
                resp = await client.post("/api/members", json=_hire("Triage"))
                assert resp.status == 200, await resp.text()
        assert seen == {"row_at_copy": False, "file_at_copy": True, "binding_at_persist": "Triage"}
        assert KiroCrewConfig.load().agents["Triage"].kiro_agent == "Triage"

    @pytest.mark.asyncio
    async def test_a_failed_copy_leaves_no_member(self, agents_dir: Path):
        """Atomic: the copy fails, nothing is published -- no row, no private
        memory, no file -- and the copy's error is the answer. A retry is a
        clean create, not a 409 on a phantom."""
        with patch(
            "kiro_crew.dashboard.handlers.agents._write_private_copy",
            side_effect=RuntimeError("disk"),
        ):
            async with TestClient(TestServer(_app())) as client:
                resp = await client.post("/api/members", json=_hire("Triage"))
                assert resp.status == 500
                data = await resp.json()
                assert data["code"] == "fork_failed"
                assert "rolled_back" not in data
            assert set(KiroCrewConfig.load().agents) == {"default"}
            assert not (agents_dir / "Triage.json").exists()
            assert agent_state.get_fork_info("Triage") is None
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_hire("Triage"))
            assert resp.status == 200, await resp.text()
        cfg = KiroCrewConfig.load()
        assert set(cfg.agents) == {"default", "Triage"}
        assert cfg.agents["Triage"].kiro_agent == "Triage"
        assert (agents_dir / "Triage.json").exists()

    @pytest.mark.asyncio
    async def test_a_source_that_vanishes_between_resolve_and_copy_is_404_with_nothing_written(
        self, agents_dir: Path
    ):
        """Step 1 saw the file; it is gone by the copy (a concurrent uninstall).
        The copy's own 404 is the answer and nothing is left behind."""
        real_copy = _agents.__dict__["_write_private_copy"]

        def vanish_then_copy(*args, **kwargs):
            (agents_dir / f"{SOURCE}.json").unlink()
            return real_copy(*args, **kwargs)

        with patch("kiro_crew.dashboard.handlers.agents._write_private_copy", vanish_then_copy):
            async with TestClient(TestServer(_app())) as client:
                resp = await client.post("/api/members", json=_hire("Triage"))
                assert resp.status == 404, await resp.text()
                data = await resp.json()
        assert data["code"] == "template_not_found"
        assert set(KiroCrewConfig.load().agents) == {"default"}
        assert not (agents_dir / "Triage.json").exists()

    @pytest.mark.asyncio
    async def test_a_row_that_fails_to_persist_unwinds_the_copy(self, agents_dir: Path):
        """The copy exists when the row's publish fails (disk, store): the copy
        and its lineage are taken back, so a retry does not find a stranded
        file already claiming the id."""
        with patch(
            "kiro_crew.dashboard.handlers.agents.persist_member_config",
            side_effect=OSError("disk full"),
        ):
            async with TestClient(TestServer(_app())) as client:
                resp = await client.post("/api/members", json=_hire("Triage"))
                assert resp.status == 409, await resp.text()
                assert (await resp.json())["code"] == "member_memory_unavailable"
        assert set(KiroCrewConfig.load().agents) == {"default"}
        assert not (agents_dir / "Triage.json").exists()
        assert agent_state.get_fork_info("Triage") is None
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_hire("Triage"))
            assert resp.status == 200, await resp.text()
        assert (agents_dir / "Triage.json").exists()

    @pytest.mark.asyncio
    async def test_an_unwind_that_cannot_remove_the_file_keeps_its_lineage(self, agents_dir: Path):
        """The copy's lineage is what makes every ownership check read it as
        PRIVATE. Pruned while the file stays -- a row took it, or the unlink
        failed -- the copy would read as a shared template."""

        from kiro_crew.dashboard.handlers.agents import _unwind_private_copy

        dest = agents_dir / "Triage.json"
        dest.write_text(json.dumps({"name": "Triage"}), encoding="utf-8")
        agent_state.set_fork_info("Triage", forked_from=SOURCE, private_to="Triage")
        # A row bound to the copy: the file and its lineage both stay.
        cfg = KiroCrewConfig.load()
        cfg.agents["Triage"] = KiroCrewAgentConfig(kiro_agent="Triage")
        cfg.save()
        _unwind_private_copy(dest, "Triage")
        assert dest.exists()
        assert agent_state.get_fork_info("Triage")["private_to"] == "Triage"
        # No row, but the unlink fails: lineage still stays with the file.
        del cfg.agents["Triage"]
        cfg.save()
        with patch.object(Path, "unlink", side_effect=OSError("busy")):
            _unwind_private_copy(dest, "Triage")
        assert dest.exists()
        assert agent_state.get_fork_info("Triage")["private_to"] == "Triage"
        # The ordinary case: file gone, then lineage.
        _unwind_private_copy(dest, "Triage")
        assert not dest.exists()
        assert agent_state.get_fork_info("Triage") is None

    @pytest.mark.asyncio
    async def test_a_name_that_shares_another_members_slug_is_refused_before_anything_is_written(
        self, agents_dir: Path
    ):
        """The slug keys the member's space, rules and thread binding and is
        lossy: two members on one slug would inherit each other's briefing and
        be refused their thread. The hire says so before it writes."""
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_hire("Triage"))
            assert resp.status == 200, await resp.text()
            before = sorted(p.name for p in agents_dir.iterdir())
            resp = await client.post("/api/members", json=_hire("triage"))
            assert resp.status == 409, await resp.text()
            body = await resp.json()
            assert body["code"] == "slug_collision"
            assert "Triage" in body["error"] and "triage" in body["error"]
        assert sorted(p.name for p in agents_dir.iterdir()) == before
        assert set(KiroCrewConfig.load().agents) == {"default", "Triage"}

    @pytest.mark.asyncio
    async def test_a_cancelled_hire_finishes_its_transaction(self, agents_dir: Path):
        """The handler is cancelled (gateway shutdown, client gone) while the
        create -- copy plus row -- is in flight. The transaction still reaches
        its own end: the member is whole, never a copy without its row."""
        import asyncio

        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.dashboard.handlers import api_member_hire

        real_create = _agents.__dict__["_create_crew"]
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_create(request, body, **kwargs):
            entered.set()
            await release.wait()
            return await real_create(request, body, **kwargs)

        app = _app()
        request = make_mocked_request("POST", "/api/members", app=app, payload=None)
        request["user"] = "local-app"
        body = _hire("Triage")

        async def _json():
            return body

        request.json = _json  # type: ignore[method-assign]
        with patch(
            "kiro_crew.dashboard.handlers.members._agents_handlers._create_crew", slow_create
        ):
            handler = asyncio.ensure_future(api_member_hire(request))
            await entered.wait()
            handler.cancel()  # the outer request is gone mid-hire
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await handler
        cfg = KiroCrewConfig.load()
        assert cfg.agents["Triage"].kiro_agent == "Triage"
        assert (agents_dir / "Triage.json").exists()

    @pytest.mark.asyncio
    async def test_the_source_must_not_be_another_members_private_copy(self, agents_dir: Path):
        """The create's foreign-copy check runs on the SOURCE: hiring from a file
        the sidecar names as another crew's private copy is refused."""
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_hire("Triage"))
            assert resp.status == 200, await resp.text()
            resp = await client.post(
                "/api/members",
                json={"display_name": "Copycat", "source": {"kind": "local", "agent": "Triage"}},
            )
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == "foreign_private_copy"
        assert "Copycat" not in KiroCrewConfig.load().agents

    @pytest.mark.asyncio
    async def test_the_slug_check_runs_inside_the_config_lock(self, agents_dir: Path):
        """The check is the create's ``admit`` hook: it runs with the lock held,
        against the snapshot the row is published from and with the id the
        create minted -- a pre-lock check would let ``Triage`` and ``triage``
        both pass and then serialize into two rows on one slug."""
        from kiro_crew.dashboard.handlers import members as members_handlers

        real = members_handlers.__dict__["_slug_collision_refusal"]
        observed: list[tuple[bool, str, set[str]]] = []

        def observe(cfg, candidate):
            observed.append((_agents._get_config_lock().locked(), candidate, set(cfg.agents)))
            return real(cfg, candidate)

        with patch("kiro_crew.dashboard.handlers.members._slug_collision_refusal", observe):
            async with TestClient(TestServer(_app())) as client:
                resp = await client.post("/api/members", json=_hire("Triage"))
                assert resp.status == 200, await resp.text()
                resp = await client.post("/api/members", json=_hire("triage"))
                assert resp.status == 409
                assert (await resp.json())["code"] == "slug_collision"
        assert observed == [
            (True, "Triage", {"default"}),
            (True, "triage", {"default", "Triage"}),
        ]
        assert not _agents._get_config_lock().locked()

    @pytest.mark.asyncio
    async def test_concurrent_hires_on_one_slug_publish_exactly_one_member(self, agents_dir: Path):
        """``Triage`` and ``triage`` hired at once: whichever serializes second
        sees the first's row under the lock and is refused."""
        import asyncio

        async with TestClient(TestServer(_app())) as client:
            first, second = await asyncio.gather(
                client.post("/api/members", json=_hire("Triage")),
                client.post("/api/members", json=_hire("triage")),
            )
            statuses = sorted([first.status, second.status])
            assert statuses == [200, 409], [await first.text(), await second.text()]
            refused = first if first.status == 409 else second
            assert (await refused.json())["code"] == "slug_collision"
        names = set(KiroCrewConfig.load().agents) - {"default"}
        assert len(names) == 1 and names <= {"Triage", "triage"}
        assert sorted(p.stem for p in agents_dir.glob("*.json")) == sorted([*names, SOURCE])

    @pytest.mark.asyncio
    async def test_the_copy_is_reserved_and_written_under_the_config_file_lock(
        self, agents_dir: Path
    ):
        """The bindings are read and the copy created while the config FILE
        lock (the cross-process one, ``<config>.lock``) is held, the way the
        fork and publish paths do: a writer in another process cannot bind the
        destination between the read and the file's first byte."""
        from kiro_crew.config.loader import config_path
        from kiro_crew.platform_compat import try_acquire_lock

        real = _agents.__dict__["_write_private_copy"]
        observed: list[bool] = []

        def observe(*args, **kwargs):
            lock_path = config_path().with_name(config_path().name + ".lock")
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                # A second descriptor cannot take the lock while the update holds it.
                observed.append(not try_acquire_lock(fd, exclusive=True))
            finally:
                os.close(fd)
            return real(*args, **kwargs)

        with patch("kiro_crew.dashboard.handlers.agents._write_private_copy", observe):
            async with TestClient(TestServer(_app())) as client:
                resp = await client.post("/api/members", json=_hire("Triage"))
                assert resp.status == 200, await resp.text()
        assert observed == [True]

    @pytest.mark.asyncio
    async def test_a_dotted_template_the_listing_offers_can_be_hired(self, agents_dir: Path):
        """The source is a template FILE (the template-name grammar allows
        ``reviewer.v2``); the row is bound to the copy, whose stem is the
        member id, so the binding stays inside the agent-name grammar."""
        (agents_dir / "reviewer.v2.json").write_text(
            json.dumps({"name": "reviewer.v2", "prompt": "v2"}), encoding="utf-8"
        )
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post(
                "/api/members",
                json={
                    "display_name": "Triage",
                    "source": {"kind": "local", "agent": "reviewer.v2"},
                },
            )
            assert resp.status == 200, await resp.text()
        row = KiroCrewConfig.load().agents["Triage"]
        assert row.kiro_agent == "Triage"
        assert json.loads((agents_dir / "Triage.json").read_text())["prompt"] == "v2"
        assert agent_state.get_fork_info("Triage")["forked_from"] == "reviewer.v2"

    @pytest.mark.asyncio
    async def test_gate_zero_config_hire_names_the_member_after_its_role(self, agents_dir: Path):
        """No naming step: a hire with only a source lands a member named after
        its role, marked ``named_by_user: false``; a second hire of the same
        template gets a distinct id and label (``-2`` / ``#2``) instead of a
        409; a typed name is the user's and marked as such."""
        async with TestClient(TestServer(_app())) as client:
            body = {"source": {"kind": "local", "agent": SOURCE}, "role": "Code Reviewer"}
            first = await client.post("/api/members", json=body)
            assert first.status == 200, await first.text()
            assert (await first.json())["id"] == "Code-Reviewer"
            second = await client.post("/api/members", json=body)
            assert second.status == 200, await second.text()
            assert (await second.json())["id"] == "Code-Reviewer-2"
            typed = await client.post(
                "/api/members", json=dict(body, display_name="Nia", role="Code Reviewer")
            )
            assert typed.status == 200, await typed.text()
            roster = {
                r["name"]: r for r in (await (await client.get("/api/members")).json())["members"]
            }
        cfg = KiroCrewConfig.load()
        assert cfg.agents["Code-Reviewer"].display_name == "Code Reviewer"
        assert cfg.agents["Code-Reviewer"].named_by_user is False
        assert cfg.agents["Code-Reviewer-2"].display_name == "Code Reviewer #2"
        assert cfg.agents["Code-Reviewer-2"].named_by_user is False
        assert cfg.agents["Nia"].named_by_user is True
        assert roster["Code-Reviewer"]["display_name"] == "Code Reviewer"
        assert roster["Code-Reviewer"]["named_by_user"] is False
        assert roster["Code-Reviewer-2"]["display_name"] == "Code Reviewer #2"
        assert roster["Nia"]["named_by_user"] is True
        # Each has its own copy, bound and lineaged.
        for member in ("Code-Reviewer", "Code-Reviewer-2", "Nia"):
            assert cfg.agents[member].kiro_agent == member
            assert agent_state.get_fork_info(member)["private_to"] == member

    @pytest.mark.asyncio
    async def test_a_hire_without_a_role_is_named_after_the_source_file(self, agents_dir: Path):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post(
                "/api/members", json={"source": {"kind": "local", "agent": SOURCE}}
            )
            assert resp.status == 200, await resp.text()
            assert (await resp.json())["id"] == "reviewer"
        row = KiroCrewConfig.load().agents["reviewer"]
        assert row.display_name == "" and row.named_by_user is False  # label = id

    @pytest.mark.asyncio
    async def test_the_first_rename_marks_the_member_as_named_by_its_user(self, agents_dir: Path):
        """``named_by_user`` is a member field, not a URL param: the thread
        header's just-hired hint reads it, and the rename PUT retires it."""
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post(
                "/api/members", json={"source": {"kind": "local", "agent": SOURCE}, "role": "Judge"}
            )
            assert resp.status == 200, await resp.text()
            assert KiroCrewConfig.load().agents["Judge"].named_by_user is False
            # A PUT that does not touch the name leaves the flag alone.
            resp = await client.put("/api/agents/Judge", json={"role": "Head Judge"})
            assert resp.status == 200, await resp.text()
            assert KiroCrewConfig.load().agents["Judge"].named_by_user is False
            resp = await client.put("/api/agents/Judge", json={"display_name": "Ada"})
            assert resp.status == 200, await resp.text()
            roster = {
                r["name"]: r for r in (await (await client.get("/api/members")).json())["members"]
            }
        row = KiroCrewConfig.load().agents["Judge"]
        assert row.display_name == "Ada" and row.named_by_user is True
        assert roster["Judge"]["named_by_user"] is True
        # Stored as written; a hand-edited junk value reads as named.
        from kiro_crew.config.loader import config_path

        raw = json.loads(config_path().read_text())
        assert raw["agents"]["Judge"]["named_by_user"] is True
        raw["agents"]["Judge"]["named_by_user"] = "false"
        config_path().write_text(json.dumps(raw))
        assert KiroCrewConfig.load().agents["Judge"].named_by_user is True

    @pytest.mark.asyncio
    async def test_a_typed_name_that_collides_is_still_a_409(self, agents_dir: Path):
        """Suffixing is for names nobody typed. A user who typed a taken name
        is told, the same contract the create has always had."""
        async with TestClient(TestServer(_app())) as client:
            assert (await client.post("/api/members", json=_hire("Judge"))).status == 200
            resp = await client.post("/api/members", json=_hire("Judge"))
            assert resp.status == 409
            assert (await resp.json())["code"] == "agent_exists"
            resp = await client.post("/api/members", json=_hire("Judge", named_by_user="no"))
            # The hire body does not carry the flag; a stray value is ignored.
            assert resp.status == 409

    @pytest.mark.asyncio
    async def test_the_hire_re_corroborates_its_copy_at_the_spawn_gate(
        self, agents_dir: Path, monkeypatch
    ):
        """A refresh pass that interleaved between the copy's lineage record and
        the row's persist saw a fork with no binding and recorded it as failed;
        the hire re-runs the pass once the row is on disk (as the fork endpoint
        does after its rebind), so the member is not blocked at the spawn gate."""
        import kiro_crew.agent as agent_mod

        # The interleave's outcome, seeded directly: the copy's name is already
        # in the failure set before the create answers.
        monkeypatch.setattr(agent_mod, "_fork_refresh_failed", frozenset({"Triage"}))
        assert "Triage" in agent_mod._fork_refresh_failed
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_hire("Triage"))
            assert resp.status == 200, await resp.text()
        assert "Triage" not in agent_mod._fork_refresh_failed
        agent_mod.require_fork_governance("Triage")


class TestHireValidation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body, code",
        [
            ({"display_name": "x"}, "invalid_source"),
            ({"display_name": "x", "source": "reviewer"}, "invalid_source"),
            (
                {"display_name": "x", "source": {"kind": "cloud", "agent": SOURCE}},
                "unsupported_source_kind",
            ),
            (
                {"display_name": "x", "source": {"kind": "store", "app": 3, "agent": SOURCE}},
                "invalid_source",
            ),
            # Unhashable kinds: a 400, not a TypeError out of the frozenset test.
            (
                {"display_name": "x", "source": {"kind": ["local"], "agent": SOURCE}},
                "unsupported_source_kind",
            ),
            (
                {"display_name": "x", "source": {"kind": {"k": 1}, "agent": SOURCE}},
                "unsupported_source_kind",
            ),
            (
                {"display_name": "x", "source": {"kind": "local", "agent": "../etc"}},
                "invalid_source_agent",
            ),
            ({"display_name": "x", "source": {"kind": "local"}}, "invalid_source_agent"),
            (
                {"display_name": 3, "source": {"kind": "local", "agent": SOURCE}},
                "invalid_display_name",
            ),
        ],
    )
    async def test_refused_bodies_write_nothing(self, agents_dir: Path, body, code):
        before = sorted(p.name for p in agents_dir.iterdir())
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=body)
            assert resp.status == 400, await resp.text()
            assert (await resp.json())["code"] == code
        assert sorted(p.name for p in agents_dir.iterdir()) == before
        assert set(KiroCrewConfig.load().agents) == {"default"}

    @pytest.mark.asyncio
    async def test_a_blank_display_name_is_an_absence_not_a_name(self, agents_dir: Path):
        """``display_name: "  "`` is nobody's name: the hire defaults it like a
        missing one (zero-config) rather than answering the create's 400."""
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post(
                "/api/members",
                json={"display_name": "  ", "source": {"kind": "local", "agent": SOURCE}},
            )
            assert resp.status == 200, await resp.text()
            assert (await resp.json())["id"] == "reviewer"
        assert KiroCrewConfig.load().agents["reviewer"].named_by_user is False

    @pytest.mark.asyncio
    async def test_unknown_source_file_is_404_before_anything_is_written(self, agents_dir: Path):
        """The create step accepts an unlisted template with a warning (an
        edition may resolve it); a hire promises a COPY of the file, so a name
        with no file behind it is refused up front and no row is created."""
        before = sorted(p.name for p in agents_dir.iterdir())
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post(
                "/api/members",
                json={"display_name": "Ghost", "source": {"kind": "local", "agent": "no-such"}},
            )
            assert resp.status == 404
            data = await resp.json()
        assert data["code"] == "template_not_found"
        assert "rolled_back" not in data
        assert set(KiroCrewConfig.load().agents) == {"default"}
        assert sorted(p.name for p in agents_dir.iterdir()) == before

    @pytest.mark.asyncio
    async def test_non_owner_is_refused(self, agents_dir: Path, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: False,
        )
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_hire("Triage"))
            assert resp.status in (401, 403)
        assert set(KiroCrewConfig.load().agents) == {"default"}


# ---------------------------------------------------------------------------
# Store hire: a template an installed app offers in its manifest's ``crew``.
# ---------------------------------------------------------------------------

APP = "oncall-pack"
TEMPLATE_AGENT = "agents/triage.json"


@pytest.fixture
def store_app(agents_dir: Path):
    """An installed, enabled app offering one template, with its agent materialized.

    Mirrors what ``apps.bridges._register_agents`` leaves behind on enable: the
    app directory (manifest + shipped agent + briefing), ``installed.json``, and
    the ``<app>--<agent>.json`` copy in the agents directory.
    """
    from kiro_crew.apps.manager import (
        APP_MANIFEST_FILENAME,
        InstalledApp,
        _write_installed,
        app_dir,
    )

    root = app_dir(APP)
    (root / "agents").mkdir(parents=True)
    (root / "briefings").mkdir()
    spec = {
        "name": "triage",
        "description": "Triages pages.",
        "prompt": "You triage incidents.",
        "tools": ["ReadFile"],
    }
    (root / TEMPLATE_AGENT).write_text(json.dumps(spec), encoding="utf-8")
    (root / "briefings" / "triage.md").write_text(
        "# Day one\nRead the runbook.\n", encoding="utf-8"
    )
    manifest = {
        "name": APP,
        "version": "1.2.0",
        "displayName": "Oncall pack",
        "description": "Oncall roles",
        "author": "tester",
        "agents": [TEMPLATE_AGENT],
        "crew": {
            "templates": [
                {
                    "agent": TEMPLATE_AGENT,
                    "role": "Oncall Triage Engineer",
                    "triggers": "incident, prod outage",
                    "initial_briefing": "briefings/triage.md",
                }
            ]
        },
    }
    (root / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    _write_installed(APP, InstalledApp(name=APP, version="1.2.0", enabled=True))
    materialized = agents_dir / f"{APP}--triage.json"
    materialized.write_text(json.dumps(spec), encoding="utf-8")
    return root


def _store_hire(display_name: str, **extra) -> dict:
    body = {
        "display_name": display_name,
        "source": {"kind": "store", "app": APP, "agent": TEMPLATE_AGENT},
    }
    body.update(extra)
    return body


class TestStoreHire:
    @pytest.mark.asyncio
    async def test_gate_one_template_hired_twice_with_different_names(
        self, agents_dir: Path, store_app: Path
    ):
        """The step-3 gate: two members from one store template coexist, each
        with its own copy, the card's defaults, provenance and pristine copy."""
        from kiro_crew import member_templates, members

        async with TestClient(TestServer(_app())) as client:
            a = await client.post("/api/members", json=_store_hire("Checkout triage"))
            assert a.status == 200, await a.text()
            b = await client.post(
                "/api/members", json=_store_hire("Payments triage", role="Payments Oncall")
            )
            assert b.status == 200, await b.text()
            roster = {
                r["name"]: r for r in (await (await client.get("/api/members")).json())["members"]
            }
        cfg = KiroCrewConfig.load()
        for member_id, role in (
            ("Checkout-triage", "Oncall Triage Engineer"),
            ("Payments-triage", "Payments Oncall"),
        ):
            row = cfg.agents[member_id]
            # Bound to its OWN copy of the materialized template file.
            assert row.kiro_agent == member_id
            assert (agents_dir / f"{member_id}.json").exists()
            assert json.loads((agents_dir / f"{member_id}.json").read_text())["prompt"] == (
                "You triage incidents."
            )
            # The card's defaults, the caller's word winning.
            assert row.role == role
            assert row.triggers == "incident, prod outage"
            # Provenance on the row and the roster.
            assert row.template == f"{APP}/triage"
            assert row.template_version == "1.2.0"
            assert roster[member_id]["template"] == f"{APP}/triage"
            assert roster[member_id]["template_version"] == "1.2.0"
            # Lineage names the copy's source by its DECLARED name, as the fork records it.
            assert roster[member_id]["template_origin"] == "triage"
            # The pristine copy (BASE of a later merge) and the seeded briefing.
            slug = members.slug_for_name(member_id)
            pristine = json.loads(member_templates.pristine_copy_path(slug).read_text())
            assert pristine["template"] == f"{APP}/triage"
            assert pristine["version"] == "1.2.0"
            assert pristine["agent"]["prompt"] == "You triage incidents."
            assert pristine["card"] == {
                "role": "Oncall Triage Engineer",
                "triggers": "incident, prod outage",
            }
            if members.member_briefing_supported():
                assert (
                    members.member_briefing_path(slug).read_text()
                    == "# Day one\nRead the runbook.\n"
                )
        # The shipped and materialized files are untouched.
        assert json.loads((agents_dir / f"{APP}--triage.json").read_text())["name"] == "triage"
        assert roster["default"]["template"] == ""

    @pytest.mark.asyncio
    async def test_the_briefing_is_seeded_once_and_never_overwrites_lived_state(
        self, agents_dir: Path, store_app: Path
    ):
        from kiro_crew import members

        if not members.member_briefing_supported():
            pytest.skip("briefings are not read on this platform")
        slug = members.slug_for_name("Checkout-triage")
        path = members.member_briefing_path(slug)
        path.parent.mkdir(parents=True)
        path.write_text("my own notes", encoding="utf-8")
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_store_hire("Checkout triage"))
            assert resp.status == 200, await resp.text()
        assert path.read_text() == "my own notes"

    @pytest.mark.asyncio
    async def test_a_planted_link_in_the_app_tree_is_refused_not_followed(
        self, agents_dir: Path, store_app: Path, tmp_path: Path
    ):
        """The app's tree is the app's to change after install. A symlink where
        the briefing (or the shipped spec) should be must not be followed into
        a credential file and copied into a prompt-visible briefing. The
        manifest is re-validated against the tree as it is NOW, so a link that
        leaves the app root makes the listing unhireable -- the same verdict
        install would give."""
        from kiro_crew import members

        secret = tmp_path / "secret.txt"
        secret.write_text("AKIAIOSFODNN7EXAMPLE\n", encoding="utf-8")
        briefing = store_app / "briefings" / "triage.md"
        briefing.unlink()
        briefing.symlink_to(secret)
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_store_hire("Checkout triage"))
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == "template_invalid"
        slug = members.slug_for_name("Checkout-triage")
        assert not members.member_briefing_path(slug).exists()
        assert "Checkout-triage" not in KiroCrewConfig.load().agents
        # A link INSIDE the root (validation cannot tell it from a file) is
        # still not read: pinned reads refuse it.
        briefing.unlink()
        briefing.symlink_to(store_app / "manifest-copy.json")
        (store_app / "manifest-copy.json").write_text("{}", encoding="utf-8")
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_store_hire("Checkout triage"))
            assert resp.status == 200, await resp.text()
        assert not members.member_briefing_path(slug).exists()
        # The shipped spec through a link: the listing is unhireable.
        spec = store_app / TEMPLATE_AGENT
        spec.unlink()
        spec.symlink_to(store_app / "manifest-copy.json")
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_store_hire("Payments triage"))
            assert resp.status == 409
            assert (await resp.json())["code"] == "template_spec_unreadable"
        assert "Payments-triage" not in KiroCrewConfig.load().agents

    def test_seeding_is_decided_by_the_create_not_by_a_check_before_it(self, agents_dir: Path):
        """A member that starts its notes between an existence check and an
        atomic replace would lose them to template text. The create is exclusive."""
        from kiro_crew import member_templates, members

        if not members.member_briefing_supported():
            pytest.skip("briefings are not read on this platform")
        slug = "race-member"
        path = members.member_briefing_path(slug)
        path.parent.mkdir(parents=True)
        # Two seeds racing for one file: exactly one wins, the other reports False
        # and the winner's bytes are what remain.
        assert member_templates.seed_briefing(slug, "first") is True
        assert member_templates.seed_briefing(slug, "second") is False
        assert path.read_text() == "first"
        # A link planted at the name is neither followed nor replaced.
        path.unlink()
        target = path.parent / "elsewhere.md"
        target.write_text("theirs")
        path.symlink_to(target)
        assert member_templates.seed_briefing(slug, "template") is False
        assert target.read_text() == "theirs"

    @pytest.mark.asyncio
    async def test_a_store_hire_holds_the_apps_lifecycle_lock(
        self, agents_dir: Path, store_app: Path
    ):
        """An app update landing between resolving the listing and copying its
        materialized agent would record a pristine BASE that never existed. The
        hire holds the same lock install/update/uninstall take."""
        import asyncio

        from kiro_crew.apps.manager import app_lifecycle_lock

        observed: list[bool] = []
        real_copy = _agents.__dict__["_write_private_copy"]

        def observe_then_copy(*args, **kwargs):
            observed.append(app_lifecycle_lock(APP).locked())
            return real_copy(*args, **kwargs)

        with patch("kiro_crew.dashboard.handlers.agents._write_private_copy", observe_then_copy):
            async with TestClient(TestServer(_app())) as client:
                resp = await client.post("/api/members", json=_store_hire("Checkout triage"))
                assert resp.status == 200, await resp.text()
        assert observed == [True]
        assert not app_lifecycle_lock(APP).locked()
        del asyncio

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "mutate, status, code",
        [
            ("uninstall", 404, "app_not_installed"),
            ("disable", 409, "app_disabled"),
            ("no-card", 404, "template_not_offered"),
            ("unmaterialized", 409, "template_not_materialized"),
            ("bad-spec", 409, "template_spec_unreadable"),
            # The manifest install validated is not the manifest on disk now.
            ("agent-traversal", 409, "template_invalid"),
            ("briefing-traversal", 409, "template_invalid"),
        ],
    )
    async def test_an_unhireable_listing_is_refused_before_anything_is_written(
        self, agents_dir: Path, store_app: Path, mutate, status, code
    ):
        from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, InstalledApp, _write_installed

        if mutate == "uninstall":
            import shutil

            shutil.rmtree(store_app)
        elif mutate == "disable":
            _write_installed(APP, InstalledApp(name=APP, version="1.2.0", enabled=False))
        elif mutate == "no-card":
            m = json.loads((store_app / APP_MANIFEST_FILENAME).read_text())
            m["crew"]["templates"][0]["agent"] = "agents/other.json"
            m["agents"].append("agents/other.json")
            (store_app / APP_MANIFEST_FILENAME).write_text(json.dumps(m))
        elif mutate == "unmaterialized":
            (agents_dir / f"{APP}--triage.json").unlink()
        elif mutate == "bad-spec":
            (store_app / TEMPLATE_AGENT).write_text("not json")
        elif mutate in ("agent-traversal", "briefing-traversal"):
            outside = store_app.parent.parent / "outside"
            outside.mkdir()
            (outside / "planted.json").write_text(json.dumps({"name": "triage"}))
            (outside / "planted.md").write_text("AKIAIOSFODNN7EXAMPLE\n")
            m = json.loads((store_app / APP_MANIFEST_FILENAME).read_text())
            card = m["crew"]["templates"][0]
            if mutate == "agent-traversal":
                card["agent"] = "../../outside/planted.json"
                m["agents"] = [card["agent"]]
            else:
                card["initial_briefing"] = "../../outside/planted.md"
            (store_app / APP_MANIFEST_FILENAME).write_text(json.dumps(m))
        before = sorted(p.name for p in agents_dir.iterdir())
        body = _store_hire("Checkout triage")
        if mutate == "agent-traversal":
            body["source"]["agent"] = "../../outside/planted.json"
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=body)
            assert resp.status == status, await resp.text()
            assert (await resp.json())["code"] == code
        assert sorted(p.name for p in agents_dir.iterdir()) == before
        assert set(KiroCrewConfig.load().agents) == {"default"}

    @pytest.mark.asyncio
    async def test_an_explicit_empty_role_or_triggers_is_a_word_not_an_absence(
        self, agents_dir: Path, store_app: Path
    ):
        """The card is the default only where the caller sent NO such key. A
        member hired with ``triggers: ""`` asked for no triggers and must not
        be re-armed with the card's."""
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post(
                "/api/members", json=_store_hire("Quiet triage", role="", triggers="")
            )
            assert resp.status == 200, await resp.text()
            resp = await client.post("/api/members", json=_store_hire("Loud triage"))
            assert resp.status == 200, await resp.text()
        cfg = KiroCrewConfig.load()
        assert cfg.agents["Quiet-triage"].role == ""
        assert cfg.agents["Quiet-triage"].triggers == ""
        assert cfg.agents["Loud-triage"].role == "Oncall Triage Engineer"
        assert cfg.agents["Loud-triage"].triggers == "incident, prod outage"

    @pytest.mark.asyncio
    async def test_the_cards_ghost_face_is_the_members_face_and_a_caller_cannot_pass_one(
        self, agents_dir: Path, store_app: Path
    ):
        """A card's ``avatar`` (ghost only; the manifest refuses a picture or a
        pack) lands on the row normalized by the crew record's own validator,
        so the member wears the template's face from its first frame. A card
        without one leaves the row's face empty (the name-seeded ghost). The
        hire body carries no avatar of its own."""
        from kiro_crew.apps.manager import APP_MANIFEST_FILENAME

        m = json.loads((store_app / APP_MANIFEST_FILENAME).read_text())
        m["crew"]["templates"][0]["avatar"] = {
            "kind": "ghost",
            "traits": {"eyes": "visor", "accessory": "phones", "tile": "#de2121"},
        }
        (store_app / APP_MANIFEST_FILENAME).write_text(json.dumps(m))
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post(
                "/api/members",
                json=_store_hire("Pager triage", avatar={"kind": "image", "file": "x.png"}),
            )
            assert resp.status == 200, await resp.text()
            roster = {
                r["name"]: r for r in (await (await client.get("/api/members")).json())["members"]
            }
        row = KiroCrewConfig.load().agents["Pager-triage"]
        assert row.avatar["kind"] == "ghost"
        assert row.avatar["traits"]["eyes"] == "visor"
        assert row.avatar["traits"]["accessory"] == "phones"
        assert row.avatar["traits"]["tile"] == "#de2121"
        assert roster["Pager-triage"]["avatar"]["traits"]["eyes"] == "visor"
        # No card face: no face on the row.
        del m["crew"]["templates"][0]["avatar"]
        (store_app / APP_MANIFEST_FILENAME).write_text(json.dumps(m))
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_store_hire("Plain triage"))
            assert resp.status == 200, await resp.text()
        assert KiroCrewConfig.load().agents["Plain-triage"].avatar == {}

    @pytest.mark.asyncio
    async def test_a_link_planted_at_the_member_directory_is_not_written_through(
        self, agents_dir: Path, store_app: Path, tmp_path: Path
    ):
        """``members/<slug>`` is agent-writable. A symlink planted there must not
        turn the pristine-copy publish into a write under the link's target."""
        from kiro_crew import member_templates, members

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        slug = members.slug_for_name("Checkout-triage")
        member_dir = members.member_dir(slug)
        member_dir.parent.mkdir(parents=True, exist_ok=True)
        member_dir.symlink_to(elsewhere, target_is_directory=True)
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_store_hire("Checkout triage"))
            assert resp.status == 500, await resp.text()
            data = await resp.json()
        assert data["code"] == "template_link_failed"
        assert data["rolled_back"] is True
        assert list(elsewhere.iterdir()) == []
        assert member_dir.is_symlink()
        assert set(KiroCrewConfig.load().agents) == {"default"}
        del member_templates

    def test_a_briefing_seed_that_fails_midway_leaves_no_file_behind(
        self, agents_dir: Path, monkeypatch
    ):
        """A short write or ENOSPC must not leave a truncated briefing that the
        exclusive create then reports as the member's own on every retry."""
        from kiro_crew import member_templates, members

        if not members.member_briefing_supported():
            pytest.skip("briefings are not read on this platform")
        slug = "unlucky-member"
        path = members.member_briefing_path(slug)
        real_write = os.write
        calls: list[int] = []

        def short_then_fail(fd, data):
            calls.append(len(data))
            if len(calls) == 1:
                return real_write(fd, bytes(data[:3]))
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(member_templates.os, "write", short_then_fail)
        with pytest.raises(OSError):
            member_templates.seed_briefing(slug, "# Day one\nRead the runbook.\n")
        # The partial write was retried for the remainder, then the file removed.
        assert calls[0] > calls[1]
        assert not path.exists()
        monkeypatch.undo()
        assert member_templates.seed_briefing(slug, "# Day one\n") is True
        assert path.read_text() == "# Day one\n"

    @pytest.mark.asyncio
    async def test_admission_is_re_run_against_the_manifest_on_disk_at_hire(
        self, agents_dir: Path, store_app: Path, monkeypatch
    ):
        """Install, update and enable each run admission; the manifest is the
        app's to rewrite afterwards, and a fleet that bans the app or requires a
        signature the edited manifest does not carry must not see the card's
        text hired into a member. Builtins are exempt exactly as enable exempts
        them."""
        from kiro_crew.apps import admission
        from kiro_crew.apps.manager import InstalledApp, _write_installed

        monkeypatch.setattr(
            admission,
            "load_app_admission_policy",
            lambda: admission.AppAdmissionPolicy(mode=admission.MODE_OPEN, banned=[APP]),
        )
        before = sorted(p.name for p in agents_dir.iterdir())
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_store_hire("Checkout triage"))
            assert resp.status == 409, await resp.text()
            body = await resp.json()
            assert body["code"] == "app_admission_denied"
            assert "banned" in body["error"]
        assert sorted(p.name for p in agents_dir.iterdir()) == before
        assert set(KiroCrewConfig.load().agents) == {"default"}
        # A builtin is first-party code shipped unsigned: exempt, as on enable.
        _write_installed(
            APP, InstalledApp(name=APP, version="1.2.0", enabled=True, origin="builtin")
        )
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_store_hire("Checkout triage"))
            assert resp.status == 200, await resp.text()

    @pytest.mark.asyncio
    async def test_the_template_link_runs_under_the_config_lock(
        self, agents_dir: Path, store_app: Path
    ):
        """Step 4 checks the row's generation and copy, then publishes the
        pristine copy and the briefing. Held under the config lock, every other
        locked writer (delete, rebind, same-id recreate) waits, so the files are
        published for the row the guard checked."""
        from kiro_crew.dashboard.handlers import members as members_handlers

        real_link = members_handlers.__dict__["_link_member_to_template"]
        observed: list[bool] = []

        def observe_then_link(*args, **kwargs):
            observed.append(_agents._get_config_lock().locked())
            return real_link(*args, **kwargs)

        with patch(
            "kiro_crew.dashboard.handlers.members._link_member_to_template", observe_then_link
        ):
            async with TestClient(TestServer(_app())) as client:
                resp = await client.post("/api/members", json=_store_hire("Checkout triage"))
                assert resp.status == 200, await resp.text()
        assert observed == [True]
        assert not _agents._get_config_lock().locked()

    @pytest.mark.asyncio
    async def test_a_failed_template_link_rolls_the_hire_back(
        self, agents_dir: Path, store_app: Path
    ):
        with patch(
            "kiro_crew.dashboard.handlers.members._link_member_to_template",
            side_effect=OSError("disk full"),
        ):
            async with TestClient(TestServer(_app())) as client:
                resp = await client.post("/api/members", json=_store_hire("Checkout triage"))
                assert resp.status == 500
                data = await resp.json()
        assert data["code"] == "template_link_failed"
        assert data["rolled_back"] is True
        assert set(KiroCrewConfig.load().agents) == {"default"}
        # The copy the fork had already made goes with the row, lineage included.
        assert not (agents_dir / "Checkout-triage.json").exists()
        assert agent_state.get_fork_info("Checkout-triage") is None
        # And the retry is clean.
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_store_hire("Checkout triage"))
            assert resp.status == 200, await resp.text()
