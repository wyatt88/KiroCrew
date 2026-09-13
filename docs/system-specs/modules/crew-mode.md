# Crews

A **crew** is a named entry in the config's `agents` map. It binds a kiro-cli
agent template plus a workspace, a memory store, a model and a reasoning effort,
and it carries free-text `triggers` that decide whether the orchestrator may
route work to it. The selection path is the `select_crew` MCP tool.

This spec used to own a second thing spelled *crew*: **Crew Mode**, the
`"crew"` chat-slot mode whose control plane (`crew_chat.py`) fanned one
session's topics out to sub-sessions. It is retired — see
[Retired: Crew Mode](#retired-crew-mode) — in favour of the Crew Members page
(`/members`, served by `dashboard/handlers/members.py` and `members.py` in the
table below), where each crew is a standing agent with its own thread.

A crew is not a *Remote Instance* (see [instances.md](instances.md)), and not
an Issue Radar *crew*, which is that app's own repository work crew
(see [issue-radar.md](issue-radar.md)).

## Components

Legacy topic respawn requires its original run identity or surviving legacy
run state. If pruning removed both, continuation refuses with a named memory
error and leaves the queued request retryable; the owner must start a new topic.
Missing history must never silently turn a private topic into Global memory.

| File | Role |
|---|---|
| `src/kiro_crew/config/sections.py` | `KiroCrewAgentConfig` — the crew record: `kiro_agent`, `workspace`, `memory_store`, `model`, `reasoning_effort`, `description`, `triggers`, `source`, `session_color`, `avatar`, per-crew watchdog overrides |
| `src/kiro_crew/config/loader.py` | `resolve_agent_bindings` (crew to workspace / memory store / template) and `resolve_effective_model` (the default-model precedence) |
| `src/kiro_crew/mcp_core.py` | `_do_select_crew` — the roster and bind bodies |
| `src/kiro_crew/mcp_tools/control.py` | The `select_crew` tool declaration and dispatch |
| `src/kiro_crew/validation.py` | `SELECT_CREW_SCHEMA` — argument validation for that tool |
| `src/kiro_crew/members.py` | Per-crew member space: activity log, DM-thread binding, permanent rules, self-maintained briefing, the member turn chokepoint |
| `src/kiro_crew/subagent.py` | `_validate_agent` — what an `agent=` name is checked against, and `UNADVERTISED_AGENTS` |
| `src/kiro_crew/config/prompt-orchestrator.md` | The orchestrator prompt that names `select_crew` and the delegation rule |
| `src/kiro_crew/dashboard/handlers/agents.py` | Crew CRUD on `/api/agents`, and the roster row serializer |
| `src/kiro_crew/dashboard/handlers/members.py` | `/api/members` roster, `POST /api/members`, thread get-or-create, rules, activity |
| `website/src/pages/KiroCrewAgentsPage.tsx` | The Crews UI, mounted as the **Crews** tab of `CapabilitiesPage` (Agent Capabilities) |
| `website/src/components/crew/crewEditorSections.ts` | The crew editor's pane registry, including the Routing pane that edits `triggers` |
| `website/src/components/CrewWakeSection.tsx` | "What wakes this agent" — schedules, deliberately distinct from `triggers` |

## Crew records and binding

A crew lives only in `config.json` under `agents.<name>`. It is not a kiro-cli
agent file: `kiro_agent` points at one. `resolve_agent_bindings` turns a crew
name into `ResolvedBindings`, in this order:

1. the named crew, when it is a key of `config.agents`;
2. otherwise a **materialized** kiro agent of that name (an app-registered agent
   under the user's `~/.kiro/agents/`, or a project agent), which keeps
   dispatching itself with the default workspace and Global Memory V1;
3. otherwise `default_agent`, with `requested_resolved` set to `False` so a
   caller never advertises a binding that is not running.

An unresolvable workspace falls back to `default_workspace`. Memory identity
resolves exactly: the reserved `default` assistant uses Global Memory V1;
existing members keep their declared V1 binding until the owner chooses V2.
New and opted-in members own unique private V2 stores. Missing, unreadable,
shared or mismatched private identity stops execution with an actionable error.
Selecting a member as `default_agent` preserves that member's memory version and
binding. With no agents configured, the resolver returns the existing defaults.

Member creation automatically provisions empty private memory. Members cannot
choose a shared store or rebind their private store. Legacy members may continue
using V1 or explicitly choose empty V2 memory from their settings; former Global
or named V1 contents remain untouched. Config fields, atomic publication, ownership manifest and
recovery semantics are owned by [config](config.md#named-memory-stores-memory_storespy).

A new member DM inherits the member's configured workspace, falling back to
`default_workspace` when that name is undeclared. Its project directory uses the
shared `default_project_dir` validation, so provider cwd and project essentials
refer to the same workspace. Resolution finishes before publishing the slot;
the first slot broadcast includes its project directory. A concurrent opener's
existing slot is preserved. Reopening a live or restored
thread keeps its saved workspace and project, including an explicitly empty
project, rather than resetting a session choice to the member default.

Opting into V2 opens a fresh member conversation. Existing V1 conversation and
native provider context cannot become private context by changing the config.
The member-thread binding records its private store generation and reuses that
conversation across later opens and restarts. An already protected V2 thread
keeps its existing key. Old schedules and child runs retain their recorded store;
the opt-in does not relabel past or already assigned work.

Private memory also pins an active dashboard turn to its member in ordinary
chat slots. A provider-side agent switch stops the stream with a visible notice
and resets the provider before another turn; later events cannot continue under
another agent while using that member's memory. This covers member DMs and
ordinary V2 chats. Ordinary V1 chats keep their existing switch behavior. The
validation and reset contract is owned by
[session](session.md#private-member-session-ownership).

The member side panel's Crew summary tab and the editor link to
`/settings/overview?view=memory&store=<name>`. The private memory workspace has
Memories, Profile and Recovery tabs: browsing/search/correction/copy stay in
Memories, preferences and project anchors stay in Profile, and backups plus
retired experiences stay in Recovery. Advanced facet analysis is collapsed.
Profile and Recovery load on first visit; visited Profile stays mounted so tab
changes cannot discard its drafts. Changing the selected member requires explicit
discard while a profile draft or memory mutation dialog is open. Source references
are rendered as origin labels and item references rather than JSON payloads.

The workspace header, store picker and copy-source picker reuse the owning
member's exact avatar descriptor and name, including uploaded pictures. Returning
from the member editor refreshes that identity. Empty memory can open
`/members?member=<exact-name>` directly; this link selects the member by name,
then uses the existing verified thread-opening endpoint. A failed thread open
retains its localized error heading and structured diagnostic report. Details
reveals the redacted reason on demand; Ask the agent receives the same report
when navigation permits. The cached conversation and its drafts remain available.

Facts, rules and experiences all support correction and explicit forgetting.
Experience correction keeps the same record identity and provenance. A store
marked unavailable still makes a scoped read to obtain its actual refusal, with
Retry and Recovery actions; it never displays cached records as a successful
read. Recovery paginates retired memories and refreshes live recall after an
item is restored. Complete snapshot restoration stays visibly staged across
page visits until gateway restart, and the owner can cancel the pending stage
without changing current memory or its saved backup.

Inline schedules created inside the editor persist `member_id` separately from
the provider template. A legacy schedule carrying only `agent_id` stays in Global
Memory V1 even when that string matches a member alias. The editor lists private
member jobs by exact `member_id`, and an existing job's member is immutable.
Legacy jobs retain their previous template/sequence display attribution and show
Global Memory V1 in the member's Schedules pane. Displaying an old schedule there
does not migrate it or grant access to that member's private store.

`resolve_effective_model` is the single source of truth for what model a new
session on a crew starts with, highest tier first: the crew's own `model`, the
bound kiro agent's pinned model (skipped for the built-in `kirocrew` agent), the
global `agent.model`, then the installed agent file's model. A per-session pick
outranks all four and is not considered there.

The loader is defensive about hand-edited config: a non-string `model` or
`triggers` collapses to `""`, an unknown `reasoning_effort` collapses to inherit,
and a junk watchdog override collapses to `0`.

## Hire: `POST /api/members` (copy-on-hire)

A crew member is a Kiro custom agent plus the wrapper row; **hire** is the one
verb that makes a member from a definition. Source kind `local` adopts an
installed agent file (`~/.kiro/agents/<agent>.json`):

```json
{"source": {"kind": "local", "agent": "reviewer"},
 "display_name": "Checkout triage", "role": "Oncall Triage Engineer",
 "workspace": "default", "triggers": "", "session_color": ""}
```

`source.kind` must be a string in `_HIRE_SOURCE_KINDS` -- `local` or `store` (an
unhashable value is a 400 `unsupported_source_kind`, never a `TypeError`);
`source.agent` must be in the template-name grammar (`invalid_source_agent`) -- the
source is a FILE the installed listing offers, dots included (`reviewer.v2`), and the
row is bound to the copy, whose stem the copy writer mints from the member id, so the
binding stays inside the agent-name grammar.
A display name whose minted id would share another member's SLUG is 409
`slug_collision` before anything is written (`_slug_collision_refusal`, the create's
`admit` hook: it runs INSIDE the config-lock hold, against the snapshot the row is
published from and with the id the create minted -- two pre-lock checks could both
pass for `Triage` and `triage` and then serialize into two rows on one slug): the slug keys
`members/<slug>/`, the rules file and the DM binding, and it is lossy (`Foo` and
`foo` share one), so two members on it would inherit each other's briefing and be
refused their thread and rules as a collision -- the hire is where that is still
sayable.
Source kind **`store`** hires from a template an installed app offers in its
manifest's `crew.templates` (`app-kit-platform.md` 3.1): `source: {kind: "store",
app, agent}` where `agent` is the manifest `agents` path the card names. The whole
store hire runs under the app's lifecycle lock (`apps.manager.app_lifecycle_lock`,
the one install/update/uninstall take): an update of the app between resolving the
listing and copying its materialized agent would copy the new bytes while recording
the old version and spec as the member's pristine BASE. Inside it,
`member_templates.resolve_store_template` resolves the listing BEFORE anything is
written: the app must be installed (404 `app_not_installed`) and enabled (409
`app_disabled` -- only an enabled app has its agents materialized), the app must
pass **admission again** against the manifest on disk now (409
`app_admission_denied`: install, update and enable each ran it, but the card's
role, triggers and briefing become prompt-adjacent text on the member and the
manifest they come from is the app's to rewrite after enable -- a signed manifest
edited afterwards no longer carries a valid signature; builtins are exempt exactly
as enable exempts them), the manifest
must carry a card for that agent (404 `template_not_offered`), the `crew` section
must still validate against the tree as it is NOW -- the same checks install ran,
against the same root, plus canonical containment of the card's `agent` and
`initial_briefing` paths (409 `template_invalid`; the manifest install validated is
not the manifest on disk after the app rewrote it), the shipped spec must read (409
`template_spec_unreadable`) and the materialized `<app>--<agent name>` file must
exist (409 `template_not_materialized`). The shipped spec and the initial briefing
are read through `pinned_fs.read_file_pinned` (ancestors pinned, a non-regular
final component refused): the app's tree is the app's to change after install, and
a by-name read there is a disclosure primitive pointed at whatever the name
resolves to. The materialized name becomes the `agent` the local steps below run
against, and the card's `role` and `triggers` fill the create body where the caller
sent NO such key (the card is the default; the caller's word wins, and an explicit
empty string is a word -- "no triggers" -- not an absence). The route
composes the create core, owner-gated once at the top, and is **atomic** -- the
member either exists with its own copy of the source or does not exist -- and **at
no moment is a row bound to the SHARED source readable**: the copy is made inside
the create's config-lock hold, before the row exists, and the row is published
already bound to it. The other order (publish, then fork and rebind) left a
source-bound row on disk between the two; a concurrent thread open in that gap
resolves the source binding and runs a session that keeps using the shared template
after the hire completed, which is the exact hazard the copy exists to remove.
Steps 2 onward run as ONE transaction under `chat_utils.drained` (the coroutine
twin of `drained_to_thread`): a cancellation of the request mid-way -- a gateway
shutdown, a client that closed the connection -- is absorbed until the transaction
reaches its own end (success or roll-back) and re-raised afterwards, so a copy is
never left without its row and a linked member never without its link:

| Step | Core | On failure |
|---|---|---|
| 1. resolve the source | `_load_template_specs` | 404 `template_not_found` / 409 `ambiguous_template_name`; nothing written. The create path tolerates a missing template (a crew may be bound ahead of an install); a hire may not, because its promise is a copy of that file |
| 2. copy-on-hire | inside `_create_crew(copy_source=…)`, after the id is minted, the slug admitted and the source has passed the foreign-private-copy check, under the config FILE lock (`update_config_locked` with a read-only mutate, the cross-process one the fork and publish paths hold for the same reason: a writer in another process cannot bind the destination between the bindings read and the file's first byte) and, inside it, the spec lock: `_write_private_copy` (the one writer of a private copy, shared with the editor's first-edit fork) copies the source into a member-owned file whose stem derives from the member id, re-reading the source in-lock, reserving every current binding and the boot-rebuilt stems, suffixing past collisions and reserved basenames, and records lineage in the `agent_state` sidecar in the same hold | 404 `template_not_found` (the source vanished between 1 and 2), 409 `ambiguous_template_name`, 500 `bookkeeping_failed` / `fork_failed`; nothing published |
| 3. publish the wrapper row | the rest of `_create_crew` (the body of `POST /api/agents`: private memory provisioned, the row persisted) with `kiro_agent` = the copy; then, outside the lock, the fork governance refresh (`_refresh_forked_templates`) re-runs exactly as the fork endpoint runs it after its rebind, so a pass that interleaved between the copy's lineage record and the row's persist -- and recorded the copy as an uncorroborated fork -- cannot leave the new member blocked at the spawn gate | its own 4xx/409, verbatim -- and the copy is **unwound** (`_unwind_private_copy`: file and lineage, unless a row already took the name), so a retry does not find a stranded file claiming the id |

A store hire has a **step 4**, inside the same atom and under the config lock the
create just released -- held across the guarded row update AND both file
publications, so every other locked writer (a delete, a rebind, a same-id recreate)
waits and the row the guard checked is the row the files are published for:
`_link_member_to_template`
records `template` (`<app>/<agent name>`) and `template_version` (the app's
manifest version) on the row in a locked read-modify-write that requires the row
to still carry this hire's generation and copy, writes the **pristine copy**
`trust/member-templates/<member id>.json` (`member_templates.write_pristine_copy`:
the agent definition as MATERIALIZED -- the `<app>--<agent>` file the hire copies,
the app bridge's own servers and managed refs included -- plus the card's
`role`/`triggers` at that version, stamped with the member id and the private-store
generation -- the BASE a later role update three-way merges against; under the
keystone-gated `trust/` subtree the DM bindings use and keyed by the immutable id,
NOT in `members/<slug>/`: that directory is agent-writable and its slug is lossy,
so a BASE kept there could be forged by an agent or shared by two members, and a
forged or shared BASE makes the merge skip a template change or overwrite a
customization silently; published through `pinned_fs.write_file_pinned`), and seeds `members/<slug>/briefing.md` from the card's
`initial_briefing` ONCE (`seed_briefing`: an existing briefing is the member's lived
state and is never overwritten -- decided by an exclusive create through the pinned
member directory, not by a check before it; every byte is written and a write that
fails midway removes the file, so a truncated briefing is never what the next
attempt's `O_EXCL` reports as the member's own; a platform where briefings are not
read gets none). A failure rolls the hire back (`_roll_back_hire`: the delete
route's own mutation through `_delete_crew_record` -- row removed, a private V2
store archived under the retirement marker, cached handles released -- but ONLY
while the row is still the one this hire made, bound to its copy and carrying the
private store name the create minted; then the copy itself through
`_remove_private_copy`, only while the sidecar still names this member as the
copy's owner and no row is bound to it) and answers 500 `template_link_failed` with
`rolled_back: true`; a roll-back that is refused or fails answers 500
`hire_incomplete` naming the member `id`.

The create body is this package's own contract (pinned by its tests); the hire
reads it strictly -- a missing `name`, `kiro_agent`, `memory_store` or
`display_name` is a 500 `hire_incomplete`, never a guessed default. The answer
carries `kiro_agent` (the copy's name) beside the id for exactly this reader.
Why a server verb rather than the two client-reachable calls: a client that dies
between create and fork leaves a member bound to the SHARED source it was told
it owns -- the exact hazard copy-on-hire removes -- and only the server can roll
the first half back. Two members hired from one file therefore coexist, each with
its own copy and row; a second hire whose display name mints a taken id is a 409
`agent_exists` (the message names the typed name and the id).

Success: `{"ok": true, "id"}` -- the minted id (what `/members?member=` resolves);
the copy the member is bound to and what the caller sent (label, role, source) are
read back from the roster row, not echoed. The `GET /api/members` row carries `template` and
`template_version` (store provenance, `""` for a hand-made or locally adopted
member; the drawer's Source row reads `Template <app>/<agent> (v<version>)`) and
`template_origin` -- the template a member's own
copy was made from (`forked_from` where the sidecar's `private_to` is this
member), `""` when bound to a shared template directly -- so the drawer reads
`reviewer — customized copy` (the editor's own word for a forked copy is
"Customized") rather than presenting the copy's stem as a template. It passes the
same redactor as the other identity fields: a declared template name is text a
package or a hand-edited spec wrote.

**Zero-config.** Only `source` is required. Without a `display_name` (absent or
blank) the member is named after its `role`, else after the source file, and the
row records **`named_by_user: false`** (`KiroCrewAgentConfig.named_by_user`,
default `true` -- every pre-existing row, every create with a typed name, and a
hire that took one are "named"; a hand-edited junk value reads as `true` so a
stray edit can never resurrect the hint). The create honours the flag: a defaulted
name is allowed to collide -- the id is suffixed the way the migration suffixes
(`code-reviewer-2`) and the label follows (`Code Reviewer #2`) -- because there is
no user to hand a 409 to; a typed name that collides is still 409 `agent_exists`.
The first rename (`PUT /api/agents/{id}` with `display_name`) flips the flag to
`true`; a PUT that leaves the name alone does not. `GET /api/members` carries
`named_by_user` (withheld from `GET /api/agents`, like `starred`), which is what
the thread header reads for its *Just hired · named after its role* hint and its
in-place rename (design step 6). The hire's entry point is the **hire gallery**
under the Crew Members page (step 6, `/members/hire`) -- the one place a member
is hired from, whatever its source: it lists every enabled installed app's cards
(`crew.templates`, with the card's duty, tags, category, starter prompts and ghost
face) beside the built-in and local agent files, and a card's hire sends `source:
{kind: "store", app, agent}`; the crew manager's own **New crew** stays a plain
create (bind to a shared template). The drawer's Source row for a store-hired
member reads `Template triage from Oncall pack (v1.2.0)` -- the app by its display
name, from the installed-apps listing read only while a template-hired member is
open; the app id is the fallback when the app is gone, and a FAILED read is said
under the row (`ErrorNotice` with the agent hand-off; the row still shows the id)
rather than passed off as the app having no display name. Pinned in `test/test_member_hire.py`
(the gates: two members from one file coexist; one store template hired twice
coexist, each with the card's defaults, provenance, pristine copy and seeded
briefing; the card's ghost face is the member's face and the hire body cannot pass
one; **zero-config**: a hire with only a
source and a role lands `Code Reviewer` / `named_by_user: false`, a second one
`Code-Reviewer-2` / `Code Reviewer #2`, a typed name is marked named, a hire with
no role is named after the file, a blank name is an absence, the first rename
flips the flag and an untouched-name PUT does not, a typed collision is still 409;
no moment exists where a row is bound to the shared source -- the copy exists
before the row, the row persists bound to the copy; a failed copy leaves no
member and a retry is clean; an unwind that cannot remove the file keeps its
lineage; a name sharing another member's slug is refused, the check runs inside
the config lock and concurrent `Triage`/`triage` hires publish exactly one member;
the copy is reserved and written under the config file lock; a dotted template the
listing offers can be hired; a source that vanishes between resolve and copy is 404
with nothing written; a row that fails to persist unwinds the copy; a cancelled
hire finishes its transaction; the source must not be another member's private
copy; an unhireable listing -- uninstalled, disabled, admission-denied, no card,
unmaterialized, unreadable spec, a card path rewritten to leave the app root after
install -- is refused before anything is written; an explicit empty role or
triggers is kept, not replaced by the card's; a link planted at the member
directory fails the hire with nothing written through it; a briefing seed that
fails midway leaves no file behind; a failed template link rolls the hire back,
copy included, and a failed roll-back names the member; unhashable kinds are 400;
lineage on the roster), `test/test_agents_roster_contract.py` (`named_by_user`,
`template` and `template_version` withheld from the crew manager's roster) and
`MembersPage.identity.test.tsx` (the Source row by display name; the id with no
notice when the app is gone; an `ErrorNotice` when the apps read fails).

## Role update and detach (design step 4)

A member hired from a template can take the template's newer version without
losing what it has become, and can leave the template for good. Both verbs live
in `handlers/members.py` and `member_templates.py`; neither touches lived state
(briefing, rules, activity, the DM thread) or the member's id.

**The plan: `GET /api/members/{member}/role-update`.** Owner-gated; nothing is
written. The three sides are the **pristine copy**
`trust/member-templates/<member id>.json` the hire recorded (BASE --
`read_pristine_copy`, pinned read, shape-validated, and it must name THIS member id,
THIS row's private-store generation and the row's own `template` -- a copy written
for a same-id member that was deleted and re-hired, or a same-name file on a
case-insensitive filesystem, is not this member's base -- else 409
`pristine_copy_missing`), the
member's OWN agent file plus its `role` and `triggers` (MINE -- the file is read
through the editor's no-symlink / in-directory fence and must be the copy the
sidecar names as `private_to` this member, else 409 `not_private_copy`: a row
hand-pointed at the app's shared materialized file would otherwise have the update
rewrite a file every member of that template shares), and the template as
MATERIALIZED now -- the `<app>--<agent>` file the bridge writes, the same form the
hire copied and the pristine copy recorded, so the bridge's own servers and managed
refs read as `unchanged`, never as the member's edit (THEIRS -- `resolve_template_ref`
walks the app's cards to the one whose agent declares the row's recorded name and
resolves it with every check the hire runs, so a disabled, uninstalled, tampered or
unmaterialized template answers with the hire's own codes). A member with no `template` is 409 `not_linked`.

`plan_role_update` compares field by field. The fields are the union of the agent
definition's keys on the three sides -- except `name`, which is the member's id on
one side and the template's on the other and nobody's customization -- plus the
card's `role` and `triggers`; a side that lacks a key is `MISSING`, distinct from a
key set to JSON null. Equality is whole-value: a tool list that gained one entry is
one changed field, because a definition is what its author reviewed as a whole.
Each field is `unchanged`, `apply` (only THEIRS changed), `keep` (only MINE
changed), `agree` (both changed to the same value) or `conflict` (both changed
apart -- the user picks). The response carries every field with its three values
and `update_available` -- true when any field is `apply` or `conflict`, when the
installed version differs from the row's `template_version` (a version-only move
still advances the record), or when the pristine copy's version is behind the
row's (a pristine write that failed after the row advanced: applying is what
rewrites the base, and a stale base would turn the next template change into false
conflicts). It also carries `member_fingerprint`, a digest of MINE as the plan saw
it (the definition plus the row's `role` and `triggers`, canonical JSON, sha256
truncated) and `template_fingerprint`, the same digest of THEIRS (the materialized
definition, the card's role and triggers, the version). Update is offered, never
automatic: nothing in the gateway applies a plan by itself. A stored ref that two
cards' agents both answer to is 409 `template_ambiguous`, never resolved by card
order (the manifest validator refuses such an app at install and re-validation; the
ref is a stored string, so the resolver checks too), and an app's own verdicts --
disabled, admission-denied, an invalid crew section -- surface as themselves from
whichever card raised them, never as `template_not_offered`.

**Applying: `POST /api/members/{member}/role-update`** with
`{"resolutions": {"<field>": "mine" | "theirs"}, "expected_version": "<v>",
"member_fingerprint": "<from the plan>", "template_fingerprint": "<from the plan>"}`.
The plan is re-derived under the app's lifecycle lock (the template cannot move
mid-merge) and the config lock, and drained like a hire. All three anchors are
REQUIRED: an apply is a decision about a plan the user reviewed, and the plan names
the version it was made against, the template body it read and the member it was
made about. `expected_version`
(400 `invalid_expected_version` when absent; without it the route would apply a
THEIRS nobody looked at) must equal the
installed version (409 `template_changed`, naming the version now installed);
`template_fingerprint` (400 `invalid_template_fingerprint` when absent) must equal
the re-derived plan's digest of THEIRS (409 `template_changed` too: the version alone
does not pin the bytes an app materialized under it -- a shipped file rewritten or
re-plumbed under the same version would otherwise merge a body nobody reviewed);
`member_fingerprint` (400 `invalid_member_fingerprint` when absent) must equal the
re-derived plan's digest of MINE (409 `member_changed_since_plan`: a prompt rewritten
in the crew editor or a role renamed between review and apply would otherwise let a
stale `theirs` choice overwrite the newer customization; the panel re-reads the plan
and the user reviews again); every
`conflict` needs a resolution (409 `unresolved_conflicts`,
naming them -- a partial resolution never half-applies; a resolution for a field
not in conflict is ignored, the plan decides what is in conflict). `merge_role_update`
takes THEIRS for `apply` and for a conflict resolved `theirs`, keeps MINE
otherwise, and removes a key the template removed when that applies; the member's
declared `name` is written back unchanged. Writes, in this order: (1) the member's
agent file, inside the spec lock, after the lineage is re-checked and the merged
definition has passed `sanitize_agent_config_governance` -- the same whole-config
funnel every spec writer runs before persisting, so a grant governance withholds
cannot arrive through a template; atomic replace; (2) the row's `role`,
`triggers` and `template_version`, in a locked read-modify-write that requires the
binding and the private-store generation unchanged (409 `member_changed`); (3) the
pristine copy advances to the new version (`write_pristine_copy`). The order is
what makes a crash recoverable by the next plan: an applied file beside an
un-advanced pristine copy reads as `agree` and re-applies as a no-op, whereas the
reverse order would read the update as the member's own customization and keep it
forever. The response is `{ok, version}` -- the version the member is now on, which
is what the drawer reads; the plan already told the user what applied and what was
kept.

**Detach: `POST /api/members/{member}/detach`.** Clears `template` and
`template_version` on the row (409 `not_linked` when there is nothing to
clear, so a second detach is a refusal, not a second severing) and removes the
pristine copy (`remove_pristine_copy`: an `unlink` on the id-keyed name under
`trust/`). The severing runs under the config lock as a read-modify-write that
requires the row to still carry the store generation and the template the request
validated against (409 `member_changed`): a same-id member replaced in between --
deleted and re-hired, or linked to another template -- is somebody else's, and its
provenance and pristine base are not this request's to remove. The agent file, `role`,
`triggers`, the copy's lineage and every piece of lived state stay as they are;
the roster row then reads as a locally created member with a customized copy.
One-way: re-linking is a hire.

Frontend (`pages/members/RoleUpdatePanel.tsx`, under the drawer's Source row for
a member with a `template`): reads the plan; says *Template up to date (vX)*, or
*<App> vY is available (this member is on vX)* with a **Review update** button,
or why the template is unavailable (no base, not its own copy, app gone or
disabled), and always offers **Detach from template** as a two-step inline confirm
that says what stays. The review dialog lists only fields that change or need a
decision (`apply`, `keep`, `conflict`; `unchanged`/`agree` say nothing), shows
each side's value, and keeps **Apply vY** disabled until every conflict has a
side chosen; a `template_changed` refusal is said in the dialog and the plan
re-read. Both mutations refetch the roster and the plan before their pending
state ends. Pinned in `test/test_member_role_update.py` (**the step-4 gate**:
update a template one member customized -- the member's prompt edit and added
tool survive, the template's new hook and triggers apply, the prompt conflict is
resolved the member's way, version and pristine copy advance, briefing untouched,
a second apply is a no-op; a moved template, a template body rewritten under the
same version, two cards answering one ref (`template_ambiguous`; a banned app
surfaces its own verdict), a member edited or renamed since the plan, bad bodies, an unlinked member, an
unavailable template, a missing or foreign pristine copy (another template, another
member, an earlier generation) and a shared binding are refused before anything is
written; a pristine copy behind the row keeps the update offered until an apply
advances it; the pristine path is id-keyed, validated, and distinct for two ids that
share a slug; the bridge's plumbing reads as unchanged and a
re-plumbed materialized file as the template's change; the governance funnel runs on the merged
definition; a conflict resolved the template's way takes theirs; a renamed role is
kept when the card did not move; detach clears provenance and leaves everything
else, and refuses a member replaced under it) and `RoleUpdatePanel.test.tsx`
(current vs available, Apply gated on conflicts and sent with the plan's version
and fingerprint, `template_changed` and `member_changed_since_plan` said in place,
unavailable reasons, two-step detach).

## Fire (design step 5): `POST /api/members/{member}/fire`

Fire is the reverse of hire, and the design's decision 3 is the archive policy:
**archive the DM thread and activity like a closed session; destroy only on
explicit request.** Body `{"purge": false}` (optional; a non-boolean is 400
`invalid_purge`). Owner-gated; the default member cannot be fired (409
`cannot_fire_default` -- make another member the default first); an unknown id
is 404 -- unless a fire of it was interrupted (below). A member that shares its
slug with another LIVE member is refused (409 `slug_collision`): `members/<slug>/`,
the rules and the binding are keyed by the lossy slug, so retiring them would take
the colleague's along; that member is renamed or fired first. The whole fire runs
under `_dm_thread_lock`, the lock the thread open (`POST /api/members/{slug}/thread`)
holds from its binding read to its binding write, so an open cannot re-bind the slug
between the fire's binding read and its retirement.

Order, and why: (1) the **thread** is closed first through the tab's own close
path (`chat_handlers.close_slot` on the slot the DM binding names -- only when the
binding names THIS member exactly: one left by a deleted same-slug member attributes
nothing to fire, closes no slot and reports no thread -- the same
sequence the tab's close runs: tombstone, retire the nudge loop, notify the owning
app, persist as closed, tear down the session), so a live session never keeps
running against a row that is about to vanish; its transcript stays in the History
tab under `dashboard:<slot key>`. A close that fails (500 `thread_close_failed`)
leaves the member whole. (2) **Row + store**, under the config lock and only while
the row is still the one the request saw (binding and store generation unchanged,
else 409 `member_changed`): first the fire's **intent marker** is written to
`trust/member-fires/<member id>.json` (slug, copy, purge, the `fired.json` record;
500 `fire_not_recorded` when it cannot be, nothing removed), then
`_delete_crew_record`, the delete route's own mutation
-- the row removed, a private V2 store archived under the ordinary retirement
marker (never erased; the memory admin surface restores it), cached handles
released. Steps 3 and 4 then run in the SAME config-lock hold (a hire of the same id
takes that lock to publish, so it cannot interleave and lose the copy, pristine copy
or space it just received to this fire's cleanup), and the hire's `admit` hook
refuses to publish under an id whose fire is still pending (409 `fire_pending`).
From here the fire is **resumable**: steps 3 and 4 are idempotent and run
from the marker (`_finish_fire`, under the config lock; a row under the id at that
point -- a hand-edited config -- is somebody else's member and is refused, 409
`member_changed`, marker kept); a step that fails after the row is gone answers
500 `fire_incomplete` (`resumable: true`) and leaves the marker, never a success
that left a same-name re-hire to inherit stale state, and the next fire of that id
-- which has no row -- resumes the cleanup instead of answering 404; the marker is
cleared only once everything is done. (3) The member's **own agent file** goes (`_remove_private_copy`, only
while the sidecar names this member as the copy's owner and no row is bound to it
-- the shared template a hire copied from is never touched) and so does its
**pristine copy**. (4) **Lived state** (`members.retire_member_space`): archive
moves `members/<slug>/` (activity, briefing) to `members/.retired/<slug>--<stamp>/`
in one rename and writes `fired.json` there (member id, display name, the thread's
slot and history keys, template and version, when), so the thread History still
lists can be tied back to the colleague it belonged to; the member's rules file
moves to `trust/member-rules/.retired/<slug>--<stamp>.json` -- the same protected
subtree it lived in, never the agent-writable archive; the DM binding is removed
only when it names this member (`remove_dm_binding_of`; one naming another member
of the slug is that member's, an unreadable one attributes nothing and goes) -- a
same-name hire later starts from nothing. `.retired` is outside the slug grammar,
so no member can be named it; a symlink planted at the member directory's name is
removed as a link, never moved or followed -- and on POSIX every step of the move is
relative to **pinned descriptors** (`_move_member_dir_pinned`: the members root and
the archive root opened once as directories with `O_NOFOLLOW`, the entry `lstat`-ed,
unlinked or renamed relative to them, the record written through a descriptor of the
archive directory), so a directory swapped for a link between a check and the
rename is unlinked, never renamed or written through; and both archive roots are **pinned**
(`retired_root`, `retired_rules_root`): a link or a plain file planted at
`.retired` is refused with `MemberSlugError` before anything moves, re-checked after
the `mkdir`, so a fire never archives lived state or protected rules wherever an
agent pointed the link. With **`purge: true`** the directory and the rules
are removed instead of archived, and the thread's transcript is deleted through
the same sequence `DELETE /api/sessions/{key}` runs (`_purge_thread_history`:
claim the slot/transcript route, bind cron ownership, unlink under the transcript
lock, remove the slot) -- and when that path refuses (a cron still owns the
transcript, an unreadable store or index) the thread is reported **kept**, never
claimed gone. A thread that was bound but never wrote a transcript (opened,
nothing said; probed with the log's `has_log`) has nothing in History to archive
or purge and reads `none`, the same as a member that never opened one. The answer says what happened:
`{ok, thread: {history_key, state: archived|purged|kept|none}, lived_state:
archived|purged|none}`. The crew editor's `DELETE /api/agents/{name}` stays the
crew-centric removal; fire is the member's verb and the only one that touches
lived state.

Frontend (`pages/members/FirePanel.tsx`, at the foot of the drawer, withheld for
the default member -- the roster row carries `is_default` for exactly this):
**Fire <name>** opens a two-step confirm that says what goes and what stays, with
a **purge** tick ("Also delete the thread, activity, briefing and rules. This
cannot be undone.") that relabels the confirm to *Yes, fire and delete*. The fire
hands the outcome up (`onFired`) and the page navigates to the roster the moment
it completes -- the drawer unmounts with the member, so the outcome is said THERE,
in a dismissible notice (`member-fired-notice`, Lucide `X` to dismiss): thread
archived / deleted / KEPT because the history path refused / none. Pinned in
`test/test_member_fire.py` (**the step-5 gate**: row, copy, lineage and
pristine copy gone, the shared template untouched, the store archived under the
retirement marker, activity moved to `.retired` with a `fired.json` naming the
member, its thread's history key and its template, the rules archived under the
protected `trust/` subtree, the binding gone;
the open thread is closed through the tab's path before the row goes and a close
that fails leaves the member whole; purge removes the lived state and reports a
thread the history path could not delete as kept, and as purged when it could;
a member that never lived leaves nothing to archive; the default is refused and
bad bodies are 400; a member that changed under the request is not fired; the
same name can be hired again afterwards and starts from nothing; a link planted
at the member directory is removed, never followed; a link or a file planted at
`.retired` refuses the archive with nothing moved; only a binding naming the member
is removed; a member sharing its slug with a live colleague is refused before
anything is closed; a binding naming a ghost of the slug closes no slot and
reports no thread; a fire interrupted after the row went answers `fire_incomplete`,
leaves the marker and is finished by the next fire, while an unknown member with
nothing pending is still 404; a hire under a pending id is refused until the fire
finishes; the cleanup runs with the config lock held; a resume with a row under
the id refuses and keeps the marker; a directory swapped for a link mid-retire is
unlinked, never followed; the fire holds the thread lock) and
`FirePanel.test.tsx` (two-step, archive by default, the outcome handed up, purge as
an explicit tick with a kept thread said, a refused fire kept in view).

## Selection: the `select_crew` contract

`select_crew` has two modes, both answered as JSON by `_do_select_crew`.

`route_crew` resolves each trigger-matched member independently. Healthy matches
retain their rank and owned store. Matching members whose memory cannot be
resolved appear in `unavailable` with a bounded, path- and credential-redacted
reason. No healthy match and no trigger match are distinct outcomes; unavailable
memory never authorizes substitution with Global memory. A named `select_crew`
refusal returns `crew` and `error` without a bound store or routing activity.

**Roster** (`crew` omitted or empty):

```json
{"default_agent": "default",
 "crews": [{"name": "oncall", "triggers": "incident, prod outage"}],
 "guidance": "Select a crew ONLY when its triggers clearly and specifically match…"}
```

Three rules define that list, and each is load-bearing:

- A crew whose `triggers` is empty or whitespace is **omitted entirely**. There
  is no fallback to `description`: no triggers means not a routing candidate.
- `default_agent` is omitted, because it is the caller.
- The response carries `default_agent` and `guidance` so the model has an
  explicit fallback and a high-confidence bar rather than inferring one.

**Bind** (`crew` names a roster entry):

```json
{"crew": "oncall",
 "bound": {"kiro_agent": "oncall-agent", "workspace": "/…/oncall",
           "memory_store": "oncall-mem", "model": ""}}
```

`crew` is the member's **id** (the `agents` key). A handle that is not a key is
tried once as a **display name** (`member_identity.members_named`, exact after
whitespace normalization): the free-text name that was the key before the id split
minted one from it -- `select_crew("case competition")` in a skill written before the
migration -- still binds, and `bound` reports the id, which is what `spawn_run`
takes. Two members may share a display name, so an ambiguous handle answers
`{"error": "ambiguous crew '…': N members carry that display name — select one by
id: a, b", "available": "…"}` rather than guessing (binding the wrong member's
memory is the worse failure). An unknown name answers
`{"error": "unknown crew '…'", "available": "…"}`. The membership test against
`cfg.agents` is the deny-by-default gate; `SELECT_CREW_SCHEMA` deliberately does
not impose a name grammar, because the display-name fallback needs the raw string
and a stricter schema would list a crew in the roster and then refuse to bind it.

A bind also records a routing-decision pointer through
`members.record_activity` with `via="select_crew"`. Two properties of that write
matter:

- The entry keys the session under `decided_in`, not `session`, because the
  decision is made in the parent session while the crew runs somewhere else. A
  consumer counting sessions a crew took part in therefore cannot miscount a
  session the crew never ran in.
- The caller's memory mode is resolved at the call, and only `persistent`
  sessions are recorded. An unreadable session degrades to the private spelling,
  so the failure mode is a missing entry, never a durably logged private session
  key.

These entries are **intent, not execution**: binding a crew does not oblige the
model to delegate to it, and no `via="spawn"` execution entry exists today.

## Delegating to a bound crew

Explicit member delegation uses `spawn_run(crew=<member>)`. The member alias
resolves its provider template and private memory together. The separate
`agent=` argument identifies a provider template, not a durable member identity;
it must not be used to infer access to a member's private memory.

A private member's own sub-tasks and schedules retain its store. It cannot select
Global V1 or a peer through `spawn_run` or `cron_add`. The trusted owner or Crew
coordinator assigns cross-member work; named tool delegation respects the
recipient's routing opt-in. HTTP spawning verifies the actual calling process
before accepting a parent session, and the run primitive checks the boundary
again before allocating a provider.

A named-but-unknown agent is **refused**, never silently answered by the default
agent, with the machine-readable code `agent_not_found`. That refusal is a
privilege boundary: the default agent frequently runs at broader approval, so a
typo'd or injected name falling back to it would be an escalation at the manager
primitive. An empty `agent` still means "use the default".

Crew Mode resolves the alias itself instead of relying on the coincidence:
`CrewOrchestrator._dispatch_agent` calls `resolve_agent_bindings` per dispatch
and passes `bindings.kiro_agent`. It returns the raw crew name when
`requested_resolved` is `False`, so an unknown crew is refused by
`_validate_agent` rather than quietly running the default agent under a stale
name, and it resolves an empty crew too so the concrete template stays inside
`capabilities.spawn.scopes.agents`.

## Boundaries

- A crew's `triggers` is free text read by a model. It is not a matcher, and no
  regex interprets it.
- `POST /api/agents` requires an explicit `kiro_agent`; the silent `"kirocrew"`
  default is refused, because it made every template-less crew an alias for the
  default agent. A template absent from the installed listing is accepted with a
  warning rather than refused, since an edition may resolve a row the listing
  cannot see.
- A credential-shaped crew name is refused at creation, and roster values are
  masked for every caller but the owner. An already-stored name is not renamed
  retroactively, which is why the owner keeps reading it verbatim: a name must
  be legible to be renamed.
- `kirocrew`, `kirocrew-conductor`, `kirocrew-pipeline-conductor` and
  `kirocrew-security-conductor` are in `UNADVERTISED_AGENTS`, so they never
  appear in a rendered roster.

## Tests that pin this

| Test | What it holds |
|---|---|
| `test/test_select_crew.py` | Roster excludes the default crew and every triggerless crew, carries `default_agent` plus guidance; a named crew returns its bindings; an old free-text handle resolves through the display name and binds by id; a display name two members share is refused as ambiguous, naming both ids; an unknown name returns `error` plus `available`; the schema accepts spaces and dots in a crew name |
| `test/test_crew_reasoning_effort.py` | Per-crew effort reaches a crew dispatch |
| `test/test_members.py`, `test/test_members_dm_thread.py` | Slug validation and containment, activity recording and dedupe, DM-binding canonicality, rules and briefing reads |
| `test/test_chat_send_agent_model_default.py` | The crew model default a new session starts on |

## Retired: Crew Mode

Crew Mode was the `"crew"` chat-slot mode: one session whose messages became
durable queue entries, a single-flight decision agent that routed each to a
topic, and one continuable sub-session per topic, with results forwarded back
under `↩ re:` attribution. Its control plane lived in `crew_chat.py`; its
design of record is
[`../../request-for-change/rfc-orchestrator-chat-sessions.md`](../../request-for-change/rfc-orchestrator-chat-sessions.md).

It retired in favour of the Crew Members page, which inverts the model: instead
of one nameless session fanning out to topics, each crew is a named member with
its own standing thread. What remains, and why:

- **No ingress.** `"crew"` is in neither `_CREATABLE_MODES` (`chat_handlers`)
  nor `_VALID_MODES` (`chat_folders`) nor the fork override allowlist, so a
  session can no longer be born or switched into it. A caller still sending
  `mode: "crew"` on auto-create gets a plain slot (the value is dropped like any
  unknown mode); on the create and switch endpoints it is `invalid_mode`.
- **Existing sessions come back as plain chat.** `chat_persistence._restored_mode`
  maps a persisted `mode: "crew"` to `""` on both restore paths. The transcript
  is untouched and still renders; nothing is migrated and nothing is deleted.
  The store the mode kept under `<data home>/crew/<folded key>-<digest>/`
  (`queue.json`, `topics.json`, `forwards.json`, `slot_key`) held only routing
  state — it is neither read nor removed, and a reader who wants the disk back
  may delete that directory by hand.
- **Old transcripts keep their shape.** The frontend's `TurnBlock.isCrewReply`
  still honours the persisted `meta.crew_reply` marker so a forwarded topic
  answer in an old session renders outside the collapse pane, as it did when it
  was written. Nothing writes the marker any more.
- **The autonudge crew/member boundary keeps its vocabulary.** `autonudge_authz`
  still lists `"crew"` beside `"member"` in the modes that refuse an outside
  arm. With no slot able to carry the mode the entry is unreachable, and it is
  left in place rather than re-litigating a security boundary in a removal PR.

The sidebar's create-menu entry that used to create a crew-mode session is now
a "Crew Members" door: it opens `/members` when `PREVIEW_CREW` (Settings →
Developer → Feature Previews) is on and lands on that flag's card when it is
off.
