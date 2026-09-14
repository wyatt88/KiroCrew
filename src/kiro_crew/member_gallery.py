"""The hire gallery's catalog: every template a crew member can be hired from.

Three sources, one shape (design step 6, *Crew Member = Custom Agent + Wrapper
Layer*): the job cards enabled installed apps offer in their manifest's
``crew.templates`` (:mod:`kiro_crew.member_templates`), the agent files this
package ships (built-ins), and the user's own agent files under the agents
directory. A member's private copy is never a template (it is one colleague's
definition, not a posting), and an app's materialized agent is offered only
through its card -- the card is what carries the role, the duty and the face.

Everything here is a READ: the catalog is assembled from the manifests, the
agent files and the crew roster, and answers what the gallery renders plus the
exact ``source`` body a hire of each card sends. Blocking file IO throughout:
call off the loop.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from kiro_crew import agent_state, member_templates
from kiro_crew.agent import kiro_agents_dir_path
from kiro_crew.agent_discovery import AgentInfo, list_agents
from kiro_crew.apps.manager import get_app_manifest, list_apps
from kiro_crew.apps.manifest import CREW_CATEGORIES, CrewTemplate
from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger(__name__)

#: The gallery never offers these as templates: the two files Kiro Crew's own
#: setup writes are the assistant the ``default`` member already IS.
_ASSISTANT_SOURCES = frozenset({"kirocrew"})

_WORD_BREAK_RE = re.compile(r"[-_.]+")


@dataclass
class TemplateCard:
    """One gallery card, whatever its origin. ``source`` is the hire body's
    ``source`` -- what ``POST /api/members`` takes to hire from this card."""

    id: str
    origin: str  # "app" | "builtin" | "local"
    source: dict[str, str]
    role: str
    duty: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    category: str = "other"
    starter_prompts: list[dict[str, str]] = field(default_factory=list)
    avatar: dict[str, Any] = field(default_factory=dict)
    team: int = 0
    publisher: str = ""
    version: str = ""
    #: The template's agent as the member would copy it: its name, and what
    #: the definition brings along (skills, MCP servers).
    agent: str = ""
    capabilities: list[dict[str, str]] = field(default_factory=list)
    #: Member ids hired from this card and still on the roster.
    hired_as: list[str] = field(default_factory=list)
    #: False when the card is listed but a hire would be refused right now
    #: (the app's agent is not materialized, its spec unreadable...); the
    #: refusal's code says why, in the hire's own vocabulary.
    hireable: bool = True
    unavailable_code: str = ""
    unavailable_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "origin": self.origin,
            "source": dict(self.source),
            "role": self.role,
            "duty": self.duty,
            "description": self.description,
            "tags": list(self.tags),
            "category": self.category if self.category in CREW_CATEGORIES else "other",
            "starter_prompts": [dict(s) for s in self.starter_prompts],
            "avatar": dict(self.avatar) if self.avatar else None,
            "team": self.team,
            "publisher": self.publisher,
            "version": self.version,
            "agent": self.agent,
            "capabilities": [dict(c) for c in self.capabilities],
            "hired_as": list(self.hired_as),
            "hireable": self.hireable,
            "unavailable_code": self.unavailable_code,
            "unavailable_reason": self.unavailable_reason,
        }


def _first_sentence(text: str) -> str:
    """The one-line duty a card without one falls back to: its description's
    first sentence, so the card face never reads empty beside a described role."""
    text = " ".join(text.split())
    if not text:
        return ""
    head = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return head if len(head) <= 160 else head[:157].rstrip() + "..."


def humanize_agent_name(name: str) -> str:
    """``pipeline-conductor`` -> ``Pipeline Conductor``: the role a file with
    no card is offered under. Already-cased words keep their case."""
    words = [w for w in _WORD_BREAK_RE.split(name) if w]
    return " ".join(w if any(c.isupper() for c in w[1:]) else w[:1].upper() + w[1:] for w in words)


def capabilities_of(
    spec: dict[str, Any] | None, info: AgentInfo | None = None
) -> list[dict[str, str]]:
    """What a definition brings along, for the card's *Built-in capabilities*:
    its skills and its MCP servers, each once, skills first."""
    skills: list[str] = []
    servers: list[str] = []
    if info is not None:
        skills = list(info.skills)
        servers = list(info.mcp_servers)
    elif isinstance(spec, dict):
        from kiro_crew.agent_discovery import _extract_skills, _mcp_server_names

        skills = _extract_skills(spec)
        servers = _mcp_server_names(spec)
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for kind, names in (("skill", skills), ("mcp", servers)):
        for name in names:
            if isinstance(name, str) and name and (kind, name) not in seen:
                seen.add((kind, name))
                out.append({"kind": kind, "name": name})
    return out


def _hired_from_store(cfg: KiroCrewConfig, ref: str) -> list[str]:
    return [name for name, row in cfg.agents.items() if row.template == ref]


def _hired_from_file(
    cfg: KiroCrewConfig, agent: str, forks: dict[str, dict[str, Any]]
) -> list[str]:
    """Members whose own copy was made from *agent* (the sidecar's lineage),
    plus members bound to the file directly (a shared binding)."""
    out: list[str] = []
    for name, row in cfg.agents.items():
        if row.template:
            continue  # a store hire is attributed to its card
        if row.kiro_agent == agent:
            out.append(name)
            continue
        fork = forks.get(row.kiro_agent)
        if fork and fork.get("private_to") == name and fork.get("forked_from") == agent:
            out.append(name)
    return out


def _app_cards(cfg: KiroCrewConfig, apps: list[dict[str, Any]]) -> list[TemplateCard]:
    cards: list[TemplateCard] = []
    for app in apps:
        name = app.get("name")
        if not isinstance(name, str) or not name or not app.get("enabled", False):
            continue
        manifest = get_app_manifest(name)
        if manifest is None or not manifest.crew.templates:
            continue
        publisher = manifest.displayName or name
        for card in manifest.crew.templates:
            cards.append(
                _app_card(cfg, name, publisher, str(app.get("version") or manifest.version), card)
            )
    return cards


def _app_card(
    cfg: KiroCrewConfig, app: str, publisher: str, version: str, card: CrewTemplate
) -> TemplateCard:
    out = TemplateCard(
        id=f"app:{app}/{card.agent}",
        origin="app",
        source={"kind": "store", "app": app, "agent": card.agent},
        role=card.role,
        duty=card.duty or _first_sentence(card.description),
        description=card.description,
        tags=list(card.tags),
        category=card.scenario,
        starter_prompts=[dict(s) for s in card.starter_prompts],
        avatar=card.member_avatar,
        team=card.team,
        publisher=publisher,
        version=version,
    )
    try:
        template = member_templates.resolve_store_template(app, card.agent)
    except member_templates.TemplateUnavailable as exc:
        out.hireable = False
        out.unavailable_code = exc.code
        out.unavailable_reason = str(exc)
        return out
    out.agent = template.agent_name
    out.version = template.version or version
    out.capabilities = capabilities_of(template.materialized_spec)
    out.hired_as = _hired_from_store(cfg, f"{app}/{template.agent_name}")
    return out


def _file_cards(
    cfg: KiroCrewConfig, agents: list[AgentInfo], forks: dict[str, dict[str, Any]]
) -> list[TemplateCard]:
    cards: list[TemplateCard] = []
    for info in agents:
        if info.private_to or info.scope != "global":
            continue  # a member's own copy, or a project checkout's file
        if info.source in _ASSISTANT_SOURCES or info.source in ("app", "package"):
            # The assistant is the default member; an app's agent is offered
            # through its card; a package's agent is the package's to offer.
            continue
        origin = "builtin" if info.source == "builtin" else "local"
        cards.append(
            TemplateCard(
                id=f"{origin}:{info.name}",
                origin=origin,
                source={"kind": "local", "agent": info.name},
                role=humanize_agent_name(info.name),
                duty=info.description,
                description=info.description,
                publisher="Kiro Crew" if origin == "builtin" else "",
                agent=info.name,
                capabilities=capabilities_of(None, info),
                hired_as=_hired_from_file(cfg, info.name, forks),
            )
        )
    return cards


def build_catalog(cfg: KiroCrewConfig | None = None) -> list[TemplateCard]:
    """Every card the gallery lists: app cards first (in app order), then the
    built-ins, then the user's files, each group by name."""
    cfg = cfg if cfg is not None else KiroCrewConfig.load()
    try:
        forks = agent_state.all_fork_info()
    except (OSError, ValueError):
        forks = {}
    cards = _app_cards(cfg, list_apps())
    files = _file_cards(cfg, list(list_agents(agents_dir=kiro_agents_dir_path())), forks)
    files.sort(key=lambda c: (c.origin != "builtin", c.role.lower()))
    return cards + files
