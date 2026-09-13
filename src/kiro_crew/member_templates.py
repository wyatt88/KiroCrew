"""Crew templates: the store listing a crew member is hired from.

A template is an installed app's Custom Agent plus the job card its manifest's
``crew`` section carries (:class:`kiro_crew.apps.manifest.CrewTemplate`). This
module is the read side the hire route composes:

* :func:`resolve_store_template` -- from ``{app, agent}`` to the materialized
  agent file (``<app>--<agent>``, the copy ``apps.bridges`` writes into the
  agents directory when the app is enabled), the card, the version, the agent
  definition and the initial briefing text.
* :func:`template_ref` -- the ``template`` the wrapper row records
  (``<app>/<agent>``), the same namespacing the materialized file uses.
* :func:`write_pristine_copy` / :func:`read_pristine_copy` -- the unmodified
  template payload at the installed version, kept at
  ``trust/member-templates/<member id>.json`` and stamped with the member's id
  and private-store generation: the BASE of the role update's three-way merge
  (MINE = the member's agent file and card fields, THEIRS = the template as
  installed now).
* :func:`resolve_template_ref` -- from a wrapper row's ``template``
  (``<app>/<agent name>``) back to the listing, for the update.
* :func:`plan_role_update` / :func:`merge_role_update` -- the per-field
  three-way merge: only THEIRS changed -> apply, only MINE changed -> keep,
  both -> the user picks. Scope is the template-provided definition (the agent
  file's keys except ``name``, which is the member's id) plus the card's
  ``role`` and ``triggers`` -- never lived state.
* :func:`seed_briefing` -- copy ``initial_briefing`` ONCE into the member's own
  ``briefing.md``; from then on the file is the member's lived state and no
  template operation touches it.

Leaf-ish on purpose: imports the manifest, the members module and the agents
directory resolver, never the dashboard handlers.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew import members, platform_compat
from kiro_crew.agent import kiro_agents_dir_path
from kiro_crew.apps.admission import app_admission_denied
from kiro_crew.apps.bridges import _namespace, _safe_link_name
from kiro_crew.apps.manager import _read_installed, app_dir, get_app_manifest
from kiro_crew.apps.manifest import CrewTemplate, _path_escapes_app_root
from kiro_crew.config.paths import data_home
from kiro_crew.pinned_fs import create_and_open_dir_pinned, read_file_pinned, write_file_pinned
from kiro_crew.validation import _AGENT_NAME_RE

logger = logging.getLogger(__name__)

#: The pristine copies' directory under the keystone-gated ``trust/`` subtree
#: (beside the DM bindings): one ``<member id>.json`` per member. NOT inside
#: ``members/<slug>/``: that directory is agent-writable and its slug is lossy
#: (two ids can share one), so a BASE kept there could be forged by an agent or
#: shared by two members -- and a forged or shared BASE makes the merge skip a
#: template change or overwrite a customization silently.
PRISTINE_COPIES_DIR_NAME = "member-templates"

#: Largest initial briefing a template may seed (bytes). The member's own
#: briefing is injection-capped downstream; this bounds what a store listing can
#: put on disk in one hire.
INITIAL_BRIEFING_MAX_BYTES = 64 * 1024

#: Largest agent definition a template may ship (bytes) -- the same order as the
#: spec reader caps elsewhere; a store listing is not a place for a novel.
TEMPLATE_SPEC_MAX_BYTES = 512 * 1024

_SAFE_APP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class TemplateUnavailable(Exception):
    """A store template cannot be hired from right now; ``code`` says why.

    ``code`` is a stable machine-readable word the hire route answers with:
    ``app_not_installed``, ``app_disabled``, ``app_admission_denied``,
    ``template_not_offered``, ``template_invalid``,
    ``template_spec_unreadable``, ``template_not_materialized``.
    """

    def __init__(self, code: str, message: str, *, status: int = 404):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class StoreTemplate:
    """Everything a hire needs from one store listing, resolved once."""

    app: str
    version: str
    card: CrewTemplate
    #: The declared agent name (spec ``name`` else the file stem).
    agent_name: str
    #: The materialized agent file's stem: ``<app>--<agent_name>``. This is the
    #: ``kiro_agent`` the member is created against and then forked from.
    materialized: str
    #: The template's agent definition as shipped inside the app.
    spec: dict[str, Any] = field(default_factory=dict)
    #: The definition as MATERIALIZED -- the shipped spec after the app bridge
    #: added the app's own MCP servers, the host's managed refs and its policy.
    #: This is the file a hire copies, so it is the pristine BASE a role update
    #: merges against and the THEIRS it merges in; comparing the member against
    #: the raw shipped spec would read the bridge's plumbing as the member's own
    #: customization.
    materialized_spec: dict[str, Any] = field(default_factory=dict)
    initial_briefing: str = ""

    @property
    def ref(self) -> str:
        return template_ref(self.app, self.agent_name)


def template_ref(app: str, agent_name: str) -> str:
    """The ``template`` a wrapper row records: ``<app>/<agent>``."""
    return _namespace(app, agent_name)


def _read_text_pinned(path: Path, cap: int, *, what: str) -> str | None:
    """Read an app- or member-controlled file without following a planted link.

    An installed app's tree is the app's to change between install and hire, and
    a member's directory is agent-writable: a by-name ``read_text`` on either is
    a disclosure primitive pointed at whatever the name resolves to by then --
    replace the briefing with a symlink to a credential file and the next hire
    copies that credential into a prompt-visible briefing. ``read_file_pinned``
    pins the ancestors and refuses a non-regular final component. ``None`` for a
    missing, refused, oversized or undecodable file.
    """
    try:
        text = read_file_pinned(path, what=what, max_bytes=cap + 1)
    except (OSError, ValueError):
        return None
    if len(text.encode("utf-8", "surrogatepass")) > cap:
        return None
    return text


def _read_json_capped(path: Path, cap: int, *, what: str) -> dict[str, Any] | None:
    text = _read_text_pinned(path, cap, what=what)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def resolve_store_template(app: str, agent_path: str) -> StoreTemplate:
    """Resolve ``{app, agent}`` to a hireable :class:`StoreTemplate`.

    ``agent_path`` is the manifest ``agents`` entry the card names. Raises
    :class:`TemplateUnavailable` for every way the listing cannot be hired
    from: the app is not installed or not enabled (only an enabled app has its
    agents materialized), the manifest offers no card for that agent, the
    shipped spec is unreadable, or the materialized file is missing.
    """
    if not isinstance(app, str) or not _SAFE_APP_NAME_RE.match(app):
        raise TemplateUnavailable("app_not_installed", "source.app must name an installed app")
    manifest = get_app_manifest(app)
    if manifest is None:
        raise TemplateUnavailable("app_not_installed", f"App '{app}' is not installed")
    meta = _read_installed(app)
    if meta is None or not meta.enabled:
        raise TemplateUnavailable(
            "app_disabled", f"App '{app}' is disabled; enable it to hire from it", status=409
        )
    # Admission again, against the manifest on disk NOW. Install, update and
    # enable each run it, but the card's role, triggers and briefing become
    # prompt-adjacent text on the member, and the manifest they come from is the
    # app's to rewrite after enable -- a signed manifest edited afterwards
    # carries a signature that fails to verify, and a fleet that requires one
    # must not see its text hired in. Builtins are exempt exactly as enable
    # exempts them.
    if meta.origin != "builtin":
        denied = app_admission_denied(app, manifest=manifest, action="hire")
        if denied:
            raise TemplateUnavailable(
                "app_admission_denied",
                f"App '{app}' is blocked by the admission policy: {denied}",
                status=409,
            )
    card = next((t for t in manifest.crew.templates if t.agent == agent_path), None)
    if card is None:
        raise TemplateUnavailable(
            "template_not_offered", f"App '{app}' offers no template for {agent_path!r}"
        )
    # The manifest on disk NOW, not the one install validated: an app's tree is
    # the app's to change afterwards, and a card path rewritten to ``../../.env``
    # would otherwise be read (pinned reads contain links, not traversal). The
    # same checks install runs, against the same root.
    root = app_dir(app)
    problems = manifest.crew.validate(manifest.agents, root)
    if problems or any(
        _path_escapes_app_root(p, root) for p in (agent_path, card.initial_briefing) if p
    ):
        raise TemplateUnavailable(
            "template_invalid",
            f"App '{app}' ships a crew section that fails validation; reinstall it",
            status=409,
        )
    spec_path = root / agent_path
    spec = _read_json_capped(spec_path, TEMPLATE_SPEC_MAX_BYTES, what="template agent file")
    if spec is None:
        raise TemplateUnavailable(
            "template_spec_unreadable",
            f"The template's agent file {agent_path!r} could not be read",
            status=409,
        )
    declared = spec.get("name")
    agent_name = declared if isinstance(declared, str) and declared else Path(agent_path).stem
    materialized = _safe_link_name(_namespace(app, agent_name))
    materialized_path = kiro_agents_dir_path() / f"{materialized}.json"
    if not materialized_path.is_file():
        raise TemplateUnavailable(
            "template_not_materialized",
            f"App '{app}' has not installed its agent {agent_name!r} yet; re-enable the app",
            status=409,
        )
    materialized_spec = _read_json_capped(
        materialized_path, TEMPLATE_SPEC_MAX_BYTES, what="materialized template agent file"
    )
    if materialized_spec is None:
        raise TemplateUnavailable(
            "template_not_materialized",
            f"App '{app}': the installed agent file {materialized!r} could not be read; "
            "re-enable the app",
            status=409,
        )
    briefing = ""
    if card.initial_briefing:
        text = _read_text_pinned(
            root / card.initial_briefing,
            INITIAL_BRIEFING_MAX_BYTES,
            what="template initial briefing",
        )
        if text is None:
            logger.warning(
                "template %s/%s: initial_briefing missing, oversized or not a regular file; "
                "not seeded",
                app,
                agent_name,
            )
        else:
            briefing = text
    return StoreTemplate(
        app=app,
        version=manifest.version,
        card=card,
        agent_name=agent_name,
        materialized=materialized,
        spec=spec,
        materialized_spec=materialized_spec,
        initial_briefing=briefing,
    )


def resolve_template_ref(ref: str) -> StoreTemplate:
    """Resolve a wrapper row's ``template`` (``<app>/<agent name>``) to the listing.

    The row records the DECLARED agent name, not the manifest path the card
    names, so the card is found by resolving each card of the app and matching
    the name it declares; the path's stem is tried first because it is the
    common case and costs no extra read. Raises :class:`TemplateUnavailable`
    with the same codes as :func:`resolve_store_template`, plus
    ``template_not_offered`` when no card of the app declares that name.
    """
    app, sep, agent_name = ref.partition("/")
    if not sep or not agent_name or not isinstance(app, str) or not _SAFE_APP_NAME_RE.match(app):
        raise TemplateUnavailable("template_not_offered", f"{ref!r} does not name a template")
    manifest = get_app_manifest(app)
    if manifest is None:
        raise TemplateUnavailable("app_not_installed", f"App '{app}' is not installed")
    cards = list(manifest.crew.templates)
    cards.sort(key=lambda c: Path(c.agent).stem != agent_name)
    matches: list[StoreTemplate] = []
    for card in cards:
        try:
            template = resolve_store_template(app, card.agent)
        except TemplateUnavailable as exc:
            # The app's own verdicts are the answer, whichever card raised
            # them: disabled, banned, or a crew section that fails to
            # validate. Only a card that is not THIS one (unreadable spec,
            # not materialized) is skipped in the search for the named agent.
            if exc.code in ("app_disabled", "app_admission_denied", "template_invalid"):
                raise
            continue
        if template.agent_name == agent_name:
            matches.append(template)
    if len(matches) > 1:
        # Two cards whose agents register under one name: the manifest
        # validator refuses this at install and at every re-validation, but a
        # ref is resolved from a stored string, so the answer is checked here
        # too rather than picking a card by list order.
        raise TemplateUnavailable(
            "template_ambiguous",
            f"App '{app}' offers more than one template named {agent_name!r}; "
            "the app must give its agents distinct names",
            status=409,
        )
    if matches:
        return matches[0]
    raise TemplateUnavailable(
        "template_not_offered", f"App '{app}' offers no template named {agent_name!r}"
    )


def pristine_copies_root() -> Path:
    return data_home() / "trust" / PRISTINE_COPIES_DIR_NAME


def pristine_copy_path(member_id: str) -> Path:
    """Absolute path of one member's pristine copy, containment-checked.

    Keyed by the immutable member ID (the config key, inside the agent-name
    grammar, so the filename cannot traverse), never by the lossy slug. Lives
    under the keystone-gated ``trust/`` subtree the DM bindings use: agent
    file tools cannot reach it, the gateway opens it directly.
    """
    if not isinstance(member_id, str) or not _AGENT_NAME_RE.match(member_id):
        raise members.MemberSlugError(f"invalid member id {member_id!r}")
    root = pristine_copies_root().resolve()
    target = (root / f"{member_id}.json").resolve()
    if target.parent != root:
        raise members.MemberSlugError(f"member id {member_id!r} escapes {root}")
    return target


def read_pristine_copy(member_id: str, *, generation: str) -> dict[str, Any] | None:
    """The pristine copy as :func:`write_pristine_copy` left it, or ``None``.

    ``None`` when the member has none (hired before templates existed, or from
    a local file), when it cannot be read through the pinned path, when it does
    not have the shape the writer produces, or when it was written for another
    member: the file must name THIS member id and THIS private-store
    *generation* (the store name the create minted, unique per creation), so a
    same-id member deleted and re-hired, or a same-name file on a
    case-insensitive filesystem, never lends its BASE to the wrong member.
    """
    try:
        data = _read_json_capped(
            pristine_copy_path(member_id), TEMPLATE_SPEC_MAX_BYTES, what="member pristine copy"
        )
    except members.MemberSlugError:
        return None
    if data is None:
        return None
    if data.get("member") != member_id or data.get("generation") != generation:
        return None
    if not isinstance(data.get("template"), str) or not isinstance(data.get("version"), str):
        return None
    if not isinstance(data.get("agent"), dict) or not isinstance(data.get("card"), dict):
        return None
    return data


def remove_pristine_copy(member_id: str) -> None:
    """Remove a member's pristine copy; a missing one is nothing to remove."""
    try:
        pristine_copy_path(member_id).unlink(missing_ok=True)
    except members.MemberSlugError:
        return


#: A field one side does not have at all. Distinct from ``None`` (a key set to
#: JSON null is a value) so "removed the key" and "set it to null" merge apart.
MISSING: Any = object()

#: The card fields a role update merges alongside the agent definition.
CARD_FIELDS = ("role", "triggers")

#: Field-state vocabulary the plan reports and the frontend renders.
UNCHANGED = "unchanged"
APPLY = "apply"  # only THEIRS changed: the update applies it
KEEP = "keep"  # only MINE changed: the member's customization stays
AGREE = "agree"  # both changed to the same value: nothing to decide
CONFLICT = "conflict"  # both changed apart: the user picks


@dataclass
class FieldDelta:
    """One mergeable field across the three sides."""

    #: ``spec.<key>`` for an agent-file key, ``card.role`` / ``card.triggers``.
    field: str
    state: str
    base: Any = MISSING
    mine: Any = MISSING
    theirs: Any = MISSING

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"field": self.field, "state": self.state}
        for side in ("base", "mine", "theirs"):
            value = getattr(self, side)
            if value is not MISSING:
                out[side] = value
        return out


class UnresolvedConflicts(Exception):
    """A merge was asked to apply while conflicting fields had no resolution."""

    def __init__(self, fields: list[str]):
        super().__init__(f"unresolved conflicts: {', '.join(fields)}")
        self.fields = fields


def _classify(base: Any, mine: Any, theirs: Any) -> str:
    mine_changed = mine != base
    theirs_changed = theirs != base
    if not mine_changed and not theirs_changed:
        return UNCHANGED
    if theirs_changed and not mine_changed:
        return APPLY
    if mine_changed and not theirs_changed:
        return KEEP
    return AGREE if mine == theirs else CONFLICT


def plan_role_update(
    pristine: dict[str, Any],
    mine_spec: dict[str, Any],
    mine_card: dict[str, Any],
    theirs: StoreTemplate,
) -> list[FieldDelta]:
    """Compare BASE (the pristine copy), MINE (the member) and THEIRS (the
    template as installed now) field by field.

    Fields are the union of the agent-definition keys on the three sides --
    THEIRS being the template as MATERIALIZED, the same form the hire copied and
    the pristine copy recorded, so the bridge's plumbing (the app's own MCP
    servers, the host's managed refs) reads as unchanged rather than as the
    member's edit -- except ``name`` -- the member's file declares its own id,
    the template's declares the template's, and neither is anybody's
    customization -- plus
    the card's ``role`` and ``triggers``. Equality is JSON-value equality on
    the whole field: a list of tools that gained one entry is one changed
    field, not a per-entry merge, because a definition is what the author
    reviewed as a whole. Order is stable (spec keys sorted, card last) so the
    plan the client saw is the plan the apply re-derives.
    """
    raw_spec, raw_card = pristine.get("agent"), pristine.get("card")
    base_spec: dict[str, Any] = raw_spec if isinstance(raw_spec, dict) else {}
    base_card: dict[str, Any] = raw_card if isinstance(raw_card, dict) else {}
    deltas: list[FieldDelta] = []
    theirs_spec = theirs.materialized_spec
    keys = set(base_spec) | set(mine_spec) | set(theirs_spec)
    keys.discard("name")
    for key in sorted(keys):
        b, m, t = (
            base_spec.get(key, MISSING),
            mine_spec.get(key, MISSING),
            theirs_spec.get(key, MISSING),
        )
        deltas.append(FieldDelta(f"spec.{key}", _classify(b, m, t), b, m, t))
    theirs_card = {"role": theirs.card.role, "triggers": theirs.card.triggers}
    for key in CARD_FIELDS:
        b = base_card.get(key, "")
        m = mine_card.get(key, "")
        t = theirs_card.get(key, "")
        deltas.append(FieldDelta(f"card.{key}", _classify(b, m, t), b, m, t))
    return deltas


def needs_update(deltas: list[FieldDelta]) -> bool:
    """True when applying the plan would change anything on the member."""
    return any(d.state in (APPLY, CONFLICT) for d in deltas)


def merge_role_update(
    mine_spec: dict[str, Any],
    mine_card: dict[str, Any],
    deltas: list[FieldDelta],
    resolutions: dict[str, str],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Produce the member's new agent definition and card fields.

    ``resolutions`` maps a conflicting field to ``"mine"`` or ``"theirs"``; a
    conflict without one raises :class:`UnresolvedConflicts` before anything is
    decided, so a partial resolution never half-applies. A resolution for a
    field that is not in conflict is ignored: the plan, not the client, says
    what is in conflict. The member's ``name`` is never touched, and a field
    the template REMOVED (THEIRS missing) is removed from the member when it
    applies.
    """
    unresolved = [
        d.field
        for d in deltas
        if d.state == CONFLICT and resolutions.get(d.field) not in ("mine", "theirs")
    ]
    if unresolved:
        raise UnresolvedConflicts(unresolved)
    spec = dict(mine_spec)
    card = {key: str(mine_card.get(key, "") or "") for key in CARD_FIELDS}
    for d in deltas:
        take_theirs = d.state == APPLY or (
            d.state == CONFLICT and resolutions.get(d.field) == "theirs"
        )
        if not take_theirs:
            continue
        kind, _, key = d.field.partition(".")
        if kind == "spec":
            if d.theirs is MISSING:
                spec.pop(key, None)
            else:
                spec[key] = d.theirs
        elif kind == "card":
            card[key] = str(d.theirs or "")
    return spec, card


def _ensure_members_root() -> None:
    members.members_root().mkdir(parents=True, exist_ok=True)


def write_pristine_copy(member_id: str, template: StoreTemplate, *, generation: str) -> Path:
    """Record the unmodified template payload at the installed version.

    The BASE of a later three-way merge: the agent definition as materialized
    -- the file the hire copied, bridge plumbing included -- plus the card's
    mergeable fields (role, triggers), stamped with the member id and the
    private-store *generation* :func:`read_pristine_copy` checks. Published
    through the pinned writer (the trust root itself is created by name -- it
    is the gateway's, not agent-named -- and a planted link at the final name
    is refused rather than written through).
    """
    path = pristine_copy_path(member_id)
    payload = {
        "member": member_id,
        "generation": generation,
        "template": template.ref,
        "version": template.version,
        "agent": template.materialized_spec,
        "card": {"role": template.card.role, "triggers": template.card.triggers},
    }
    root = pristine_copies_root()
    root.mkdir(parents=True, exist_ok=True)
    try:
        platform_compat.restrict_dir_to_owner(root)
    except OSError:
        logger.debug("could not tighten mode on %s", root, exc_info=True)
    write_file_pinned(
        path, json.dumps(payload, indent=2, ensure_ascii=False), what="member pristine copy"
    )
    return path


def seed_briefing(slug: str, text: str) -> bool:
    """Copy a template's initial briefing into the member's own ``briefing.md`` ONCE.

    True when the file was written. A briefing that already exists is the
    member's lived state and is left alone -- decided by the CREATE itself
    (``O_CREAT | O_EXCL`` through the pinned member directory), not by a check
    before it: a member that starts writing its notes between an existence check
    and an atomic replace would have them overwritten by template text. A
    platform where briefings are not read (no ``O_NOFOLLOW``) gets none, so the
    member is not handed a file the runtime will never inject.
    """
    if not text or not members.member_briefing_supported():
        return False
    path = members.member_briefing_path(slug)
    _ensure_members_root()
    dir_fd = create_and_open_dir_pinned(path.parent, what="member directory")
    try:
        try:
            fd = os.open(
                path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=dir_fd,
            )
        except FileExistsError:
            return False
        try:
            payload = text.encode("utf-8")
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        except BaseException:
            # A short write or ENOSPC must not leave a truncated briefing that
            # ``O_EXCL`` then reports as the member's own on every retry.
            os.close(fd)
            fd = -1
            try:
                os.unlink(path.name, dir_fd=dir_fd)
            except OSError:
                logger.warning("could not remove a partially seeded briefing for %s", slug)
            raise
        finally:
            if fd >= 0:
                os.close(fd)
    finally:
        os.close(dir_fd)
    return True
