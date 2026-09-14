"""The hire gallery's catalog (design step 6): ``GET /api/members/templates``.

Every template a member can be hired from, from all three sources -- an
installed app's job cards, the files this package ships, the user's own agent
files -- in one shape, with the exact ``source`` a hire sends, what the gallery
renders, and who was already hired from each.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import test_member_hire as _hire
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import member_gallery
from kiro_crew.agent_files import CONDUCTOR_AGENT_FILENAME
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME

APP = _hire.APP
TEMPLATE_AGENT = _hire.TEMPLATE_AGENT
SOURCE = _hire.SOURCE
_store_hire = _hire._store_hire
_hire_body = _hire._hire
_owner_caller = _hire._owner_caller
agents_dir = _hire.agents_dir
store_app = _hire.store_app


def _app() -> web.Application:
    from kiro_crew.dashboard.handlers import (
        api_member_briefing_get,
        api_member_hire,
        api_member_templates,
        api_members,
    )

    @web.middleware
    async def _auth(request: web.Request, handler):
        request.setdefault("app", "")
        request.setdefault("user", "local-app")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = MagicMock(sessions=None)
    app.router.add_get("/api/members/templates", api_member_templates)
    app.router.add_get("/api/members/{slug}/briefing", api_member_briefing_get)
    app.router.add_post("/api/members", api_member_hire)
    app.router.add_get("/api/members", api_members)
    return app


def _card_full(store_app: Path) -> None:
    """Give the store app's card everything the gallery renders."""
    m = json.loads((store_app / APP_MANIFEST_FILENAME).read_text())
    m["crew"]["templates"][0].update(
        {
            "duty": "Triages every page, correlates it with deploys.",
            "description": "Owns a paging queue end to end. Never rolls back without an ack.",
            "category": "ops",
            "tags": ["Incident triage", "Deploy correlation", "Rollback plans"],
            "starter_prompts": [
                "What paged overnight?",
                {"text": "Draft a rollback plan.", "attachment": "incident.md"},
            ],
            "avatar": {"kind": "ghost", "traits": {"eyes": "visor", "tile": "#de2121"}},
        }
    )
    (store_app / APP_MANIFEST_FILENAME).write_text(json.dumps(m))


def _shipped_conductor(agents_dir: Path) -> None:
    (agents_dir / CONDUCTOR_AGENT_FILENAME).write_text(
        json.dumps(
            {
                "name": CONDUCTOR_AGENT_FILENAME[:-5],
                "description": "Runs one issue-to-PR pipeline as a supervised fleet.",
                "mcpServers": {"github": {"command": "gh-mcp"}},
                "resources": ["skill://~/.kiro/skills/prepare-pr/SKILL.md"],
            }
        )
    )


@pytest.fixture(autouse=True)
def _installed_apps_visible(store_app: Path, monkeypatch):
    """The discovery's app-name probe reads the apps root under the data home
    the fixtures already point at; nothing else to wire."""
    yield


class TestCatalog:
    @pytest.mark.asyncio
    async def test_gate_the_gallery_lists_cards_from_all_three_sources(
        self, agents_dir: Path, store_app: Path
    ):
        """One listing: the app's card (with everything the manifest gave it and
        the exact store source), the shipped conductor as a built-in, the
        user's ``reviewer`` file as local -- and NOT the assistant, NOT the
        app's materialized file on its own, NOT a member's private copy."""
        _card_full(store_app)
        _shipped_conductor(agents_dir)
        listed = [
            *[
                dict(name=n)
                for n in (SOURCE, "kirocrew", CONDUCTOR_AGENT_FILENAME[:-5], f"{APP}--triage")
            ],
        ]
        with patch(
            "kiro_crew.dashboard.handlers.agents.list_agents",
            lambda *a, **k: [MagicMock(name=x["name"]) for x in listed],
        ):
            async with TestClient(TestServer(_app())) as client:
                # A member hired from the local file: its private copy must not list.
                resp = await client.post("/api/members", json=_hire_body("Nia"))
                assert resp.status == 200, await resp.text()
                resp = await client.get("/api/members/templates")
                assert resp.status == 200, await resp.text()
                cards = {c["id"]: c for c in (await resp.json())["templates"]}
        assert set(cards) == {
            f"app:{APP}/{TEMPLATE_AGENT}",
            f"builtin:{CONDUCTOR_AGENT_FILENAME[:-5]}",
            f"local:{SOURCE}",
        }
        app_card = cards[f"app:{APP}/{TEMPLATE_AGENT}"]
        assert app_card["source"] == {"kind": "store", "app": APP, "agent": TEMPLATE_AGENT}
        assert app_card["role"] == "Oncall Triage Engineer"
        assert app_card["duty"] == "Triages every page, correlates it with deploys."
        assert app_card["category"] == "ops"
        assert app_card["tags"] == ["Incident triage", "Deploy correlation", "Rollback plans"]
        assert app_card["starter_prompts"] == [
            {"text": "What paged overnight?"},
            {"text": "Draft a rollback plan.", "attachment": "incident.md"},
        ]
        assert (
            app_card["avatar"]["kind"] == "ghost"
            and app_card["avatar"]["traits"]["eyes"] == "visor"
        )
        assert app_card["publisher"] == "Oncall pack" and app_card["version"] == "1.2.0"
        assert app_card["hireable"] is True and app_card["hired_as"] == []
        assert app_card["agent"] == "triage"
        builtin = cards[f"builtin:{CONDUCTOR_AGENT_FILENAME[:-5]}"]
        assert builtin["origin"] == "builtin" and builtin["publisher"] == "Kiro Crew"
        assert builtin["source"] == {"kind": "local", "agent": CONDUCTOR_AGENT_FILENAME[:-5]}
        assert builtin["role"] == member_gallery.humanize_agent_name(CONDUCTOR_AGENT_FILENAME[:-5])
        assert builtin["duty"] == "Runs one issue-to-PR pipeline as a supervised fleet."
        assert builtin["category"] == "other" and builtin["avatar"] is None
        assert {(c["kind"], c["name"]) for c in builtin["capabilities"]} == {
            ("mcp", "github"),
            ("skill", "prepare-pr"),
        }
        local = cards[f"local:{SOURCE}"]
        assert local["origin"] == "local" and local["publisher"] == ""
        assert local["role"] == "Reviewer" and local["duty"] == "Reviews pull requests."
        # The member hired from it is attributed to it; its copy is not a card.
        assert local["hired_as"] == ["Nia"]

    @pytest.mark.asyncio
    async def test_hired_as_follows_store_hires_and_direct_bindings(
        self, agents_dir: Path, store_app: Path
    ):
        async with TestClient(TestServer(_app())) as client:
            for name in ("Checkout triage", "Payments triage"):
                resp = await client.post("/api/members", json=_store_hire(name))
                assert resp.status == 200, await resp.text()
            resp = await client.get("/api/members/templates")
            cards = {c["id"]: c for c in (await resp.json())["templates"]}
        assert sorted(cards[f"app:{APP}/{TEMPLATE_AGENT}"]["hired_as"]) == [
            "Checkout-triage",
            "Payments-triage",
        ]
        # Nobody hired from the local file yet; the default member is the
        # assistant's, which is never a card.
        assert cards[f"local:{SOURCE}"]["hired_as"] == []
        assert not any(c["id"].endswith(":kirocrew") for c in cards.values())

    @pytest.mark.asyncio
    async def test_an_unhireable_card_is_listed_with_the_hires_own_refusal(
        self, agents_dir: Path, store_app: Path
    ):
        """A card whose agent is not materialized (the app was never enabled
        cleanly) still lists -- the gallery says why Hire is off, in the code
        the hire itself would answer -- while a disabled app's cards do not."""
        from kiro_crew.apps.manager import InstalledApp, _write_installed

        (agents_dir / f"{APP}--triage.json").unlink()
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/members/templates")
            cards = {c["id"]: c for c in (await resp.json())["templates"]}
            card = cards[f"app:{APP}/{TEMPLATE_AGENT}"]
            assert card["hireable"] is False
            assert card["unavailable_code"] == "template_not_materialized"
            assert card["role"] == "Oncall Triage Engineer"  # the card face still reads
            _write_installed(APP, InstalledApp(name=APP, version="1.2.0", enabled=False))
            resp = await client.get("/api/members/templates")
            cards = {c["id"]: c for c in (await resp.json())["templates"]}
        assert not any(c["origin"] == "app" for c in cards.values())

    @pytest.mark.asyncio
    async def test_credential_shaped_card_text_is_redacted(self, agents_dir: Path, store_app: Path):
        m = json.loads((store_app / APP_MANIFEST_FILENAME).read_text())
        m["crew"]["templates"][0]["duty"] = "Rotate AKIAIOSFODNN7EXAMPLE daily"
        m["crew"]["templates"][0]["tags"] = ["AKIAIOSFODNN7EXAMPLE"]
        (store_app / APP_MANIFEST_FILENAME).write_text(json.dumps(m))
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/members/templates")
            body = await resp.text()
        assert "AKIAIOSFODNN7EXAMPLE" not in body

    @pytest.mark.asyncio
    async def test_non_owner_is_refused(self, agents_dir: Path, store_app: Path, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: False,
        )
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/members/templates")
            assert resp.status in (401, 403, 404)

    def test_humanize_agent_name(self):
        assert member_gallery.humanize_agent_name("pipeline-conductor") == "Pipeline Conductor"
        assert member_gallery.humanize_agent_name("code_reviewer.v2") == "Code Reviewer V2"
        assert member_gallery.humanize_agent_name("myAgent") == "myAgent"
        assert member_gallery._first_sentence("One. Two.") == "One."
        assert member_gallery._first_sentence("") == ""


class TestBriefingRead:
    @pytest.mark.asyncio
    async def test_the_drawer_reads_the_members_own_briefing_read_only(
        self, agents_dir: Path, store_app: Path
    ):
        """What the prompt builder would inject is what the drawer shows: the
        same pinned read, an absent file as empty text, a bad slug or a member
        that does not derive the slug refused."""
        from kiro_crew import members

        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/members", json=_store_hire("Pager triage"))
            assert resp.status == 200, await resp.text()
            slug = members.slug_for_name("Pager-triage")
            resp = await client.get(f"/api/members/{slug}/briefing?member=Pager-triage")
            assert resp.status == 200, await resp.text()
            body = await resp.json()
            if members.member_briefing_supported():
                # Seeded once from the card's initial_briefing at hire.
                assert body == {"text": members.read_member_briefing(slug), "supported": True}
                assert "Read the runbook." in body["text"]
            else:
                assert body == {"text": "", "supported": False}
            resp = await client.get(f"/api/members/{slug}/briefing?member=Nobody")
            assert resp.status == 400
            assert (await resp.json())["code"] == "member_slug_mismatch"
            resp = await client.get(f"/api/members/{slug}/briefing")
            assert resp.status == 400
            resp = await client.get("/api/members/..%2Fx/briefing?member=x")
            assert resp.status in (400, 404)
            # A member with no briefing yet reads as empty text, never 404.
            resp = await client.post("/api/members", json=_hire_body("Nia"))
            assert resp.status == 200, await resp.text()
            resp = await client.get(
                f"/api/members/{members.slug_for_name('Nia')}/briefing?member=Nia"
            )
            assert resp.status == 200
            assert (await resp.json())["text"] == ""
