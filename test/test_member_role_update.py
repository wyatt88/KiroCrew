"""Role update (design: Crew Member = Custom Agent + Wrapper, rollout step 4).

A member hired from a store template can take the template's newer version
through a per-field three-way merge -- BASE the pristine copy the hire recorded,
MINE the member's own agent file plus its card fields, THEIRS the template as
the app ships it now -- and can detach from the template for good. The step-4
gate: update a template one member customized; the customization survives.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import test_member_hire as _hire
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import agent_state, member_templates
from kiro_crew.config.loader import KiroCrewConfig

APP = _hire.APP
TEMPLATE_AGENT = _hire.TEMPLATE_AGENT
_store_hire = _hire._store_hire
# The hire suite's fixtures, registered here by assignment: an installed, enabled
# app offering one template, and an agents directory with its materialized copy.
_owner_caller = _hire._owner_caller
agents_dir = _hire.agents_dir
store_app = _hire.store_app

MEMBER = "Pager-triage"


def _app() -> web.Application:
    from kiro_crew.dashboard.handlers import (
        api_member_detach,
        api_member_hire,
        api_member_role_update_apply,
        api_member_role_update_get,
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
    app.router.add_get("/api/members/{member}/role-update", api_member_role_update_get)
    app.router.add_post("/api/members/{member}/role-update", api_member_role_update_apply)
    app.router.add_post("/api/members/{member}/detach", api_member_detach)
    return app


async def _hire_pager(client) -> None:
    resp = await client.post("/api/members", json=_store_hire("Pager triage"))
    assert resp.status == 200, await resp.text()


async def _apply(client, body: dict, *, fingerprint: str | None = None, theirs: str | None = None):
    """POST an apply the way the panel does: carrying the fingerprints of the
    member and the template as the plan just saw them (or explicit ones)."""
    if fingerprint is None or theirs is None:
        plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
        fingerprint = fingerprint or plan["member_fingerprint"]
        theirs = theirs or plan["template_fingerprint"]
    return await client.post(
        f"/api/members/{MEMBER}/role-update",
        json={"member_fingerprint": fingerprint, "template_fingerprint": theirs, **body},
    )


def _member_spec(agents_dir: Path) -> dict:
    return json.loads((agents_dir / f"{MEMBER}.json").read_text(encoding="utf-8"))


def _write_member_spec(agents_dir: Path, spec: dict) -> None:
    (agents_dir / f"{MEMBER}.json").write_text(json.dumps(spec), encoding="utf-8")


def _publish(store_app: Path, agents_dir: Path, version: str, *, spec=None, card=None) -> None:
    """Ship a new version of the app: manifest version, shipped spec, card,
    and the materialized copy the bridge would rewrite on update."""
    from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, InstalledApp, _write_installed

    manifest = json.loads((store_app / APP_MANIFEST_FILENAME).read_text())
    manifest["version"] = version
    if card:
        manifest["crew"]["templates"][0].update(card)
    (store_app / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest))
    if spec is not None:
        (store_app / TEMPLATE_AGENT).write_text(json.dumps(spec), encoding="utf-8")
        (agents_dir / f"{APP}--triage.json").write_text(json.dumps(spec), encoding="utf-8")
    _write_installed(APP, InstalledApp(name=APP, version=version, enabled=True))


def _shipped(store_app: Path) -> dict:
    return json.loads((store_app / TEMPLATE_AGENT).read_text())


class TestPlanAndMerge:
    """The pure merge, no filesystem."""

    def _theirs(self, spec: dict, role="Oncall Triage Engineer", triggers="incident"):
        from kiro_crew.apps.manifest import CrewTemplate

        return member_templates.StoreTemplate(
            app=APP,
            version="2.0.0",
            card=CrewTemplate(agent=TEMPLATE_AGENT, role=role, triggers=triggers),
            agent_name="triage",
            materialized=f"{APP}--triage",
            spec=spec,
            materialized_spec=spec,
        )

    def test_each_field_lands_in_exactly_one_state(self):
        base = {"agent": {"name": "triage", "prompt": "p0", "tools": ["A"], "model": "m0"}}
        base["card"] = {"role": "Oncall Triage Engineer", "triggers": "incident"}
        mine = {"name": "Pager-triage", "prompt": "p0", "tools": ["A", "B"], "model": "mine"}
        theirs = self._theirs(
            {"name": "triage", "prompt": "p1", "tools": ["A"], "model": "theirs", "hooks": {}},
            triggers="incident, outage",
        )
        deltas = {
            d.field: d
            for d in member_templates.plan_role_update(
                base, mine, {"role": "Oncall Triage Engineer", "triggers": "incident"}, theirs
            )
        }
        assert deltas["spec.prompt"].state == member_templates.APPLY
        assert deltas["spec.tools"].state == member_templates.KEEP
        assert deltas["spec.model"].state == member_templates.CONFLICT
        assert deltas["spec.hooks"].state == member_templates.APPLY
        assert deltas["spec.hooks"].base is member_templates.MISSING
        assert deltas["card.role"].state == member_templates.UNCHANGED
        assert deltas["card.triggers"].state == member_templates.APPLY
        # `name` is the id on both sides and never a field.
        assert "spec.name" not in deltas
        assert member_templates.needs_update(list(deltas.values()))

    def test_both_sides_agreeing_needs_no_decision(self):
        base = {"agent": {"prompt": "p0"}, "card": {"role": "R", "triggers": ""}}
        theirs = self._theirs({"prompt": "p1"}, role="R", triggers="")
        deltas = member_templates.plan_role_update(base, {"prompt": "p1"}, {"role": "R"}, theirs)
        states = {d.field: d.state for d in deltas}
        assert states["spec.prompt"] == member_templates.AGREE
        assert not member_templates.needs_update(deltas)

    def test_merge_refuses_an_unresolved_conflict_before_deciding_anything(self):
        base = {"agent": {"prompt": "p0", "model": "m0"}, "card": {"role": "R", "triggers": ""}}
        mine = {"name": "me", "prompt": "p0", "model": "mine"}
        theirs = self._theirs({"prompt": "p1", "model": "theirs"}, role="R", triggers="")
        deltas = member_templates.plan_role_update(base, mine, {"role": "R"}, theirs)
        with pytest.raises(member_templates.UnresolvedConflicts) as info:
            member_templates.merge_role_update(mine, {"role": "R"}, deltas, {})
        assert info.value.fields == ["spec.model"]
        # A resolution for a field NOT in conflict is ignored: the plan decides.
        spec, card = member_templates.merge_role_update(
            mine, {"role": "R"}, deltas, {"spec.model": "mine", "spec.prompt": "mine"}
        )
        assert spec == {"name": "me", "prompt": "p1", "model": "mine"}
        assert card == {"role": "R", "triggers": ""}
        spec, _ = member_templates.merge_role_update(
            mine, {"role": "R"}, deltas, {"spec.model": "theirs"}
        )
        assert spec["model"] == "theirs"

    def test_a_key_the_template_removed_is_removed_when_it_applies(self):
        base = {"agent": {"prompt": "p0", "legacy": 1}, "card": {"role": "R", "triggers": ""}}
        mine = {"name": "me", "prompt": "p0", "legacy": 1}
        theirs = self._theirs({"prompt": "p0"}, role="R", triggers="")
        deltas = member_templates.plan_role_update(base, mine, {"role": "R"}, theirs)
        spec, _ = member_templates.merge_role_update(mine, {"role": "R"}, deltas, {})
        assert "legacy" not in spec
        # A key set to null is a VALUE, distinct from a removed key.
        base2 = {"agent": {"x": None}, "card": {"role": "R", "triggers": ""}}
        deltas2 = member_templates.plan_role_update(
            base2, {"x": None}, {"role": "R"}, self._theirs({}, role="R", triggers="")
        )
        assert {d.field: d.state for d in deltas2}["spec.x"] == member_templates.APPLY


class TestRoleUpdateRoutes:
    @pytest.mark.asyncio
    async def test_gate_update_a_template_one_member_customized_and_the_customization_survives(
        self, agents_dir: Path, store_app: Path
    ):
        """The step-4 gate. The member customized its prompt and added a tool;
        the template's new version changes the prompt too, adds triggers and a
        hook. The member's tool survives, the hook and triggers apply, the
        prompt conflict is resolved the member's way; version and pristine
        copy advance; lived state is untouched."""
        from kiro_crew import members

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            # Up to date right after the hire.
            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            assert plan["update_available"] is False
            assert plan["installed_version"] == plan["member_version"] == "1.2.0"
            assert all(f["state"] == "unchanged" for f in plan["fields"])

            # MINE: the member customized its prompt and added a tool.
            mine = _member_spec(agents_dir)
            mine["prompt"] = "You triage incidents for the PAGER team."
            mine["tools"] = ["ReadFile", "Grep"]
            _write_member_spec(agents_dir, mine)
            slug = members.slug_for_name(MEMBER)
            briefing = members.member_briefing_path(slug)
            briefing_before = briefing.read_text() if briefing.exists() else None

            # THEIRS: v1.3.0 changes the prompt, adds a hook, widens the triggers.
            theirs = _shipped(store_app)
            theirs["prompt"] = "You triage incidents. Escalate after 15 minutes."
            theirs["hooks"] = {"agentSpawn": [{"command": "echo hi"}]}
            _publish(
                store_app,
                agents_dir,
                "1.3.0",
                spec=theirs,
                card={"triggers": "incident, prod outage, sev2"},
            )

            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            assert plan["update_available"] is True
            assert plan["installed_version"] == "1.3.0" and plan["member_version"] == "1.2.0"
            states = {f["field"]: f["state"] for f in plan["fields"]}
            assert states["spec.prompt"] == "conflict"
            assert states["spec.tools"] == "keep"
            assert states["spec.hooks"] == "apply"
            assert states["card.triggers"] == "apply"
            assert states["card.role"] == "unchanged"
            prompt = next(f for f in plan["fields"] if f["field"] == "spec.prompt")
            assert prompt["base"] == "You triage incidents."
            assert prompt["mine"] == "You triage incidents for the PAGER team."
            assert prompt["theirs"] == "You triage incidents. Escalate after 15 minutes."

            # Without a resolution for the conflict nothing is applied.
            resp = await _apply(client, {"expected_version": "1.3.0"})
            assert resp.status == 409
            assert (await resp.json()) == {
                "error": "every conflicting field needs a resolution",
                "code": "unresolved_conflicts",
                "fields": ["spec.prompt"],
            }
            assert _member_spec(agents_dir)["prompt"] == "You triage incidents for the PAGER team."

            resp = await _apply(
                client, {"resolutions": {"spec.prompt": "mine"}, "expected_version": "1.3.0"}
            )
            assert resp.status == 200, await resp.text()
            assert (await resp.json()) == {"ok": True, "version": "1.3.0"}

            roster = {
                r["name"]: r for r in (await (await client.get("/api/members")).json())["members"]
            }
        after = _member_spec(agents_dir)
        # The customization survived; the template's additions landed.
        assert after["prompt"] == "You triage incidents for the PAGER team."
        assert after["tools"] == ["ReadFile", "Grep"]
        assert after["hooks"] == {"agentSpawn": [{"command": "echo hi"}]}
        assert after["name"] == MEMBER
        row = KiroCrewConfig.load().agents[MEMBER]
        assert row.template_version == "1.3.0"
        assert row.triggers == "incident, prod outage, sev2"
        assert row.role == "Oncall Triage Engineer"
        assert roster[MEMBER]["template_version"] == "1.3.0"
        # The pristine copy advanced to the new BASE.
        pristine = member_templates.read_pristine_copy(MEMBER, generation=row.memory_store)
        assert pristine["version"] == "1.3.0"
        assert pristine["agent"]["prompt"] == "You triage incidents. Escalate after 15 minutes."
        assert pristine["card"]["triggers"] == "incident, prod outage, sev2"
        # Lived state untouched; the app's own files untouched.
        assert (briefing.read_text() if briefing.exists() else None) == briefing_before
        assert _shipped(store_app)["prompt"] == "You triage incidents. Escalate after 15 minutes."
        # And the member is now up to date; a second apply is a no-op.
        async with TestClient(TestServer(_app())) as client:
            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            assert plan["update_available"] is False
            assert {f["field"]: f["state"] for f in plan["fields"]}["spec.prompt"] == "keep"
            resp = await _apply(client, {"expected_version": "1.3.0"})
            assert resp.status == 200
        assert _member_spec(agents_dir) == after

    @pytest.mark.asyncio
    async def test_the_bridges_plumbing_is_not_read_as_the_members_customization(
        self, agents_dir: Path, store_app: Path
    ):
        """The hire copies the MATERIALIZED file -- shipped spec plus the app
        bridge's own MCP servers and managed refs. BASE and THEIRS are that
        form too, so the plumbing is `unchanged`, never `keep`."""

        shipped = _shipped(store_app)
        plumbed = dict(shipped, mcpServers={"kirocrew-core": {"command": "kirocrew", "args": []}})
        (agents_dir / f"{APP}--triage.json").write_text(json.dumps(plumbed), encoding="utf-8")
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            generation = KiroCrewConfig.load().agents[MEMBER].memory_store
            pristine = member_templates.read_pristine_copy(MEMBER, generation=generation)
            assert pristine["agent"]["mcpServers"] == plumbed["mcpServers"]
            assert _member_spec(agents_dir)["mcpServers"] == plumbed["mcpServers"]
            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            states = {f["field"]: f["state"] for f in plan["fields"]}
            assert states["spec.mcpServers"] == "unchanged"
            assert plan["update_available"] is False
            # The bridge re-plumbs on an app update: that reads as the template's change.
            replumbed = dict(
                plumbed, mcpServers={"kirocrew-core": {"command": "kirocrew", "args": ["mcp-core"]}}
            )
            _publish(store_app, agents_dir, "1.3.0", spec=shipped)
            (agents_dir / f"{APP}--triage.json").write_text(json.dumps(replumbed), encoding="utf-8")
            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            assert {f["field"]: f["state"] for f in plan["fields"]}["spec.mcpServers"] == "apply"

    @pytest.mark.asyncio
    async def test_a_template_that_moved_since_the_plan_is_refused(
        self, agents_dir: Path, store_app: Path
    ):
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            _publish(store_app, agents_dir, "1.3.0", card={"triggers": "sev1"})
            resp = await _apply(client, {"expected_version": "1.2.9"})
            assert resp.status == 409
            body = await resp.json()
            assert body["code"] == "template_changed"
            assert body["installed_version"] == "1.3.0"
        assert KiroCrewConfig.load().agents[MEMBER].triggers == "incident, prod outage"

    @pytest.mark.asyncio
    async def test_bad_bodies_and_unlinked_members_are_refused(
        self, agents_dir: Path, store_app: Path
    ):
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            for body, code in (
                ({"resolutions": ["spec.prompt"]}, "invalid_resolutions"),
                ({"resolutions": {"spec.prompt": "yours"}}, "invalid_resolutions"),
                ({"expected_version": 1}, "invalid_expected_version"),
                # Required: an apply names the version its plan was made against.
                ({"resolutions": {}}, "invalid_expected_version"),
                # And the digest of the member the plan was made about.
                ({"expected_version": "1.2.0"}, "invalid_member_fingerprint"),
                (
                    {"expected_version": "1.2.0", "member_fingerprint": 7},
                    "invalid_member_fingerprint",
                ),
                # And the digest of the template body the plan was made against.
                (
                    {"expected_version": "1.2.0", "member_fingerprint": "x"},
                    "invalid_template_fingerprint",
                ),
                (
                    {
                        "expected_version": "1.2.0",
                        "member_fingerprint": "x",
                        "template_fingerprint": "",
                    },
                    "invalid_template_fingerprint",
                ),
                ([], "body_not_object"),
            ):
                resp = await client.post(f"/api/members/{MEMBER}/role-update", json=body)
                assert resp.status == 400, (body, await resp.text())
                assert (await resp.json())["code"] == code
            resp = await client.get("/api/members/default/role-update")
            assert resp.status == 409
            assert (await resp.json())["code"] == "not_linked"
            resp = await client.get("/api/members/nobody/role-update")
            assert resp.status == 404
            resp = await client.post("/api/members/default/detach")
            assert resp.status == 409
            assert (await resp.json())["code"] == "not_linked"

    @pytest.mark.asyncio
    async def test_an_unavailable_template_is_reported_not_merged(
        self, agents_dir: Path, store_app: Path
    ):
        from kiro_crew.apps.manager import InstalledApp, _write_installed

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            _write_installed(APP, InstalledApp(name=APP, version="1.2.0", enabled=False))
            resp = await client.get(f"/api/members/{MEMBER}/role-update")
            assert resp.status == 409
            assert (await resp.json())["code"] == "app_disabled"
            import shutil

            shutil.rmtree(store_app)
            resp = await client.get(f"/api/members/{MEMBER}/role-update")
            assert resp.status == 404
            assert (await resp.json())["code"] == "app_not_installed"

    @pytest.mark.asyncio
    async def test_two_cards_whose_agents_share_a_name_are_ambiguous_not_first_wins(
        self, agents_dir: Path, store_app: Path
    ):
        """The stored ref is ``<app>/<agent name>``. An app rewritten after
        install so two cards' agents register under that name is refused
        (409 ``template_ambiguous``) rather than resolved by card order; a
        banned app surfaces its own verdict, never ``template_not_offered``."""
        from kiro_crew.apps.manager import APP_MANIFEST_FILENAME

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            m = json.loads((store_app / APP_MANIFEST_FILENAME).read_text())
            (store_app / "agents" / "scribe.json").write_text(
                json.dumps(dict(_shipped(store_app), name="triage", prompt="the other one"))
            )
            m["agents"].append("agents/scribe.json")
            m["crew"]["templates"].append(
                {"agent": "agents/scribe.json", "role": "Scribe", "triggers": ""}
            )
            (store_app / APP_MANIFEST_FILENAME).write_text(json.dumps(m))
            resp = await client.get(f"/api/members/{MEMBER}/role-update")
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == "template_ambiguous"
            resp = await _apply(client, {"expected_version": "1.2.0"}, fingerprint="x", theirs="y")
            assert resp.status == 409
            assert (await resp.json())["code"] == "template_ambiguous"
        assert _member_spec(agents_dir)["prompt"] == "You triage incidents."
        with patch(
            "kiro_crew.member_templates.resolve_store_template",
            side_effect=member_templates.TemplateUnavailable(
                "app_admission_denied", "banned", status=409
            ),
        ):
            with pytest.raises(member_templates.TemplateUnavailable) as exc:
                member_templates.resolve_template_ref(f"{APP}/triage")
            assert exc.value.code == "app_admission_denied"

    @pytest.mark.asyncio
    async def test_a_missing_or_foreign_pristine_copy_has_no_base(
        self, agents_dir: Path, store_app: Path
    ):
        """The BASE lives under the gateway's ``trust/`` subtree, keyed by the
        immutable member id (never the lossy slug), and names the member and
        its store generation: a copy for another template, another member, or
        an earlier same-id member is not this member's base."""
        from kiro_crew import members

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            path = member_templates.pristine_copy_path(MEMBER)
            assert path.parent == member_templates.pristine_copies_root().resolve()
            assert path.parent.parent.name == "trust"
            # Not in the agent-writable member directory, and not slug-keyed.
            assert not (
                members.member_dir(members.slug_for_name(MEMBER)) / "template.json"
            ).exists()
            good = json.loads(path.read_text())
            generation = KiroCrewConfig.load().agents[MEMBER].memory_store
            assert good["member"] == MEMBER and good["generation"] == generation
            path.unlink()
            resp = await client.get(f"/api/members/{MEMBER}/role-update")
            assert resp.status == 409
            assert (await resp.json())["code"] == "pristine_copy_missing"
            # A pristine copy naming ANOTHER template is not this member's base.
            path.write_text(json.dumps(dict(good, template="other/agent")))
            resp = await client.get(f"/api/members/{MEMBER}/role-update")
            assert resp.status == 409
            assert (await resp.json())["code"] == "pristine_copy_missing"
            # Nor one written for a same-id member of an EARLIER generation
            # (deleted and re-hired), nor one stamped with another member's id.
            path.write_text(json.dumps(dict(good, generation="member-pager-triage-old")))
            resp = await client.get(f"/api/members/{MEMBER}/role-update")
            assert (await resp.json())["code"] == "pristine_copy_missing"
            path.write_text(json.dumps(dict(good, member="pager-triage")))
            resp = await client.get(f"/api/members/{MEMBER}/role-update")
            assert (await resp.json())["code"] == "pristine_copy_missing"
            # The real one reads again.
            path.write_text(json.dumps(good))
            resp = await client.get(f"/api/members/{MEMBER}/role-update")
            assert resp.status == 200

    def test_the_pristine_copy_path_is_keyed_by_a_validated_member_id(self, agents_dir: Path):
        from kiro_crew import members

        for bad in ("../evil", "a/b", "", ".", "x" * 70):
            with pytest.raises(members.MemberSlugError):
                member_templates.pristine_copy_path(bad)
        # Two ids that share one slug get two files.
        assert member_templates.pristine_copy_path(
            "Pager-triage"
        ) != member_templates.pristine_copy_path("pager-triage")

    @pytest.mark.asyncio
    async def test_a_member_bound_to_a_shared_file_is_never_rewritten(
        self, agents_dir: Path, store_app: Path
    ):
        """The update rewrites the member's agent file, so it must be the copy
        the hire made for this member -- a hand-edited row pointing a member at
        the app's materialized file would otherwise have the update rewrite a
        file every other member of that template shares."""
        from kiro_crew.config.loader import update_config_locked

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)

            def rebind(doc):
                doc["agents"][MEMBER]["kiro_agent"] = f"{APP}--triage"
                return doc

            update_config_locked(mutate=rebind)
            _publish(store_app, agents_dir, "1.3.0", card={"triggers": "sev1"})
            resp = await client.get(f"/api/members/{MEMBER}/role-update")
            assert resp.status == 409
            assert (await resp.json())["code"] == "not_private_copy"
        assert json.loads((agents_dir / f"{APP}--triage.json").read_text())["name"] == "triage"

    @pytest.mark.asyncio
    async def test_a_governance_withheld_grant_in_the_template_does_not_reach_the_member(
        self, agents_dir: Path, store_app: Path
    ):
        """The merged definition goes through the same whole-config governance
        funnel every spec writer uses before it is persisted."""
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            theirs = _shipped(store_app)
            theirs["allowedTools"] = ["execute_bash"]
            _publish(store_app, agents_dir, "1.3.0", spec=theirs)
            with patch(
                "kiro_crew.dashboard.handlers.members.sanitize_agent_config_governance",
                side_effect=lambda cfg: cfg.pop("allowedTools", None),
            ) as funnel:
                resp = await _apply(client, {"expected_version": "1.3.0"})
                assert resp.status == 200, await resp.text()
            assert funnel.called
        assert "allowedTools" not in _member_spec(agents_dir)

    @pytest.mark.asyncio
    async def test_a_conflict_resolved_the_templates_way_takes_theirs(
        self, agents_dir: Path, store_app: Path
    ):
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            mine = _member_spec(agents_dir)
            mine["prompt"] = "mine"
            _write_member_spec(agents_dir, mine)
            theirs = _shipped(store_app)
            theirs["prompt"] = "theirs"
            _publish(store_app, agents_dir, "1.3.0", spec=theirs, card={"role": "Incident Lead"})
            resp = await _apply(
                client, {"resolutions": {"spec.prompt": "theirs"}, "expected_version": "1.3.0"}
            )
            assert resp.status == 200, await resp.text()
        assert _member_spec(agents_dir)["prompt"] == "theirs"
        assert KiroCrewConfig.load().agents[MEMBER].role == "Incident Lead"

    @pytest.mark.asyncio
    async def test_a_member_whose_role_was_renamed_keeps_it_when_the_card_did_not_move(
        self, agents_dir: Path, store_app: Path
    ):
        """Card fields merge like definition fields: a role the user changed is
        MINE; the template's unchanged role does not overwrite it."""
        from kiro_crew.config.loader import update_config_locked

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)

            def rename(doc):
                doc["agents"][MEMBER]["role"] = "Pager captain"
                return doc

            update_config_locked(mutate=rename)
            _publish(store_app, agents_dir, "1.3.0", card={"triggers": "sev1"})
            resp = await _apply(client, {"expected_version": "1.3.0"})
            assert resp.status == 200, await resp.text()
        row = KiroCrewConfig.load().agents[MEMBER]
        assert row.role == "Pager captain" and row.triggers == "sev1"

    @pytest.mark.asyncio
    async def test_a_member_edited_since_the_plan_is_refused(
        self, agents_dir: Path, store_app: Path
    ):
        """A plan is a decision about the member AS REVIEWED. The apply carries
        the plan's digest of MINE back; a member whose definition or card moved
        in between (the crew editor, a rename) is refused, so a stale
        ``theirs`` choice never overwrites a customization nobody saw."""
        from kiro_crew.config.loader import update_config_locked

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            theirs = _shipped(store_app)
            theirs["prompt"] = "theirs"
            _publish(store_app, agents_dir, "1.3.0", spec=theirs)
            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            stale = plan["member_fingerprint"]
            # The user rewrites the prompt after reviewing the plan.
            mine = _member_spec(agents_dir)
            mine["prompt"] = "rewritten after the plan"
            _write_member_spec(agents_dir, mine)
            resp = await _apply(
                client,
                {"resolutions": {"spec.prompt": "theirs"}, "expected_version": "1.3.0"},
                fingerprint=stale,
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "member_changed_since_plan"
            assert _member_spec(agents_dir)["prompt"] == "rewritten after the plan"
            assert KiroCrewConfig.load().agents[MEMBER].template_version == "1.2.0"
            # A card change counts too: the row is part of MINE.
            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            stale = plan["member_fingerprint"]

            def rename(doc):
                doc["agents"][MEMBER]["role"] = "Pager captain"
                return doc

            update_config_locked(mutate=rename)
            resp = await _apply(
                client,
                {"resolutions": {"spec.prompt": "theirs"}, "expected_version": "1.3.0"},
                fingerprint=stale,
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "member_changed_since_plan"
            # The digest is stable across re-reads of the same member, and a
            # fresh plan applies.
            fresh = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            again = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            assert fresh["member_fingerprint"] == again["member_fingerprint"] != stale
            resp = await _apply(
                client, {"resolutions": {"spec.prompt": "theirs"}, "expected_version": "1.3.0"}
            )
            assert resp.status == 200, await resp.text()
        assert _member_spec(agents_dir)["prompt"] == "theirs"
        assert KiroCrewConfig.load().agents[MEMBER].role == "Pager captain"

    @pytest.mark.asyncio
    async def test_a_pristine_copy_behind_the_row_keeps_the_update_offered(
        self, agents_dir: Path, store_app: Path
    ):
        """A pristine write that failed after the row advanced leaves BASE at
        the old version. The plan keeps the update available -- applying is
        what rewrites the base -- instead of reporting the member current with
        a stale base that would turn the next template change into false
        conflicts."""
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            _publish(store_app, agents_dir, "1.3.0", card={"triggers": "sev1"})
            resp = await _apply(client, {"expected_version": "1.3.0"})
            assert resp.status == 200, await resp.text()
            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            assert plan["update_available"] is False
            # Roll the base back to what a torn write would have left.
            path = member_templates.pristine_copy_path(MEMBER)
            pristine = json.loads(path.read_text())
            pristine["version"] = "1.2.0"
            path.write_text(json.dumps(pristine))
            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            assert plan["installed_version"] == plan["member_version"] == "1.3.0"
            assert plan["update_available"] is True
            assert all(f["state"] == "unchanged" for f in plan["fields"])
            resp = await _apply(client, {"expected_version": "1.3.0"})
            assert resp.status == 200, await resp.text()
            assert json.loads(path.read_text())["version"] == "1.3.0"
            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            assert plan["update_available"] is False

    @pytest.mark.asyncio
    async def test_a_template_body_rewritten_under_the_same_version_is_refused(
        self, agents_dir: Path, store_app: Path
    ):
        """The version is not the anchor: the apply carries the plan's digest of
        THEIRS (materialized definition + card + version), and an app that
        re-materialized different bytes under the same version is
        ``template_changed`` -- nothing nobody reviewed is merged."""
        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            theirs = _shipped(store_app)
            theirs["prompt"] = "reviewed"
            _publish(store_app, agents_dir, "1.3.0", spec=theirs)
            plan = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            stale = plan["template_fingerprint"]
            # The app rewrites its agent under v1.3.0 after the plan was reviewed.
            theirs["prompt"] = "rewritten, never reviewed"
            _publish(store_app, agents_dir, "1.3.0", spec=theirs)
            resp = await _apply(
                client,
                {"resolutions": {"spec.prompt": "theirs"}, "expected_version": "1.3.0"},
                theirs=stale,
            )
            assert resp.status == 409
            body = await resp.json()
            assert body["code"] == "template_changed" and body["installed_version"] == "1.3.0"
            assert _member_spec(agents_dir)["prompt"] == "You triage incidents."
            # Stable across re-reads; a fresh plan applies the reviewed bytes.
            fresh = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            again = await (await client.get(f"/api/members/{MEMBER}/role-update")).json()
            assert fresh["template_fingerprint"] == again["template_fingerprint"] != stale
            resp = await _apply(client, {"expected_version": "1.3.0"})
            assert resp.status == 200, await resp.text()
        assert _member_spec(agents_dir)["prompt"] == "rewritten, never reviewed"


class TestDetach:
    @pytest.mark.asyncio
    async def test_detach_clears_provenance_and_leaves_everything_else(
        self, agents_dir: Path, store_app: Path
    ):
        from kiro_crew import members

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
            before = _member_spec(agents_dir)
            slug = members.slug_for_name(MEMBER)
            resp = await client.post(f"/api/members/{MEMBER}/detach", json={})
            assert resp.status == 200, await resp.text()
            assert (await resp.json()) == {"ok": True}
            roster = {
                r["name"]: r for r in (await (await client.get("/api/members")).json())["members"]
            }
            # Detached: no template, no update to offer.
            resp = await client.get(f"/api/members/{MEMBER}/role-update")
            assert resp.status == 409
            assert (await resp.json())["code"] == "not_linked"
            # Twice is a refusal, not a second severing.
            resp = await client.post(f"/api/members/{MEMBER}/detach", json={})
            assert resp.status == 409
        row = KiroCrewConfig.load().agents[MEMBER]
        assert row.template == "" and row.template_version == ""
        assert row.role == "Oncall Triage Engineer"
        assert row.triggers == "incident, prod outage"
        assert row.kiro_agent == MEMBER
        assert roster[MEMBER]["template"] == ""
        assert roster[MEMBER]["template_origin"] == "triage"
        assert _member_spec(agents_dir) == before
        assert not member_templates.pristine_copy_path(MEMBER).exists()
        if members.member_briefing_supported():
            assert members.member_briefing_path(slug).exists()
        assert agent_state.get_fork_info(MEMBER)["private_to"] == MEMBER

    @pytest.mark.asyncio
    async def test_detach_refuses_a_member_replaced_under_it(
        self, agents_dir: Path, store_app: Path
    ):
        """The severing re-reads the row under the lock and only touches the
        member the request validated: a same-id member of another store
        generation (deleted and re-hired) or linked to another template is
        somebody else's, and its provenance and pristine base stay."""
        from kiro_crew.dashboard.handlers.members import _detach_member

        async with TestClient(TestServer(_app())) as client:
            await _hire_pager(client)
        row = KiroCrewConfig.load().agents[MEMBER]
        pristine = member_templates.pristine_copy_path(MEMBER)
        assert pristine.exists()
        for generation, template in (
            ("member-pager-triage-old", row.template),
            (row.memory_store, "other/agent"),
        ):
            resp = _detach_member(MEMBER, generation, template)
            assert resp.status == 409
            assert json.loads(resp.text)["code"] == "member_changed"
            after = KiroCrewConfig.load().agents[MEMBER]
            assert after.template == row.template and after.template_version == "1.2.0"
            assert pristine.exists()
        # A member removed under the lock is refused the same way.
        resp = _detach_member("nobody", row.memory_store, row.template)
        assert resp.status == 409
        # The row the request saw detaches.
        resp = _detach_member(MEMBER, row.memory_store, row.template)
        assert resp.status == 200
        assert KiroCrewConfig.load().agents[MEMBER].template == ""
        assert not pristine.exists()
