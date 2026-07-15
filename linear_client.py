"""Linear GraphQL client.

What this bot needs:
  - resolve labels by NAME against a closed allowlist (BE / FE / Feature / Bug /
    Improvement / UI) — never auto-create; names outside the set or absent from
    the team are dropped,
  - resolve an assignee from a Discord user id (via DISCORD_LINEAR_MAP) or by
    matching the reporter's display name against the team's members,
  - create an issue with priority, label IDs, and an optional assignee,
  - post a comment on an issue,
  - move an issue into the team's first workflow state of a given TYPE
    (state names are workspace-customisable; types are stable),
  - best-effort issue search by title / key terms.
"""
import logging
import re
from typing import Optional

import httpx

from conventions import PROJECT_ALIASES

log = logging.getLogger(__name__)


LINEAR_API_URL = "https://api.linear.app/graphql"

# Linear priority: 0=no priority, 1=urgent, 2=high, 3=medium, 4=low.
_PRIORITY_MAP = {"urgent": 1, "high": 2, "medium": 3, "low": 4}
# Reverse: Linear priority Int → human label.
_PRIORITY_LABEL = {0: "none", 1: "urgent", 2: "high", 3: "medium", 4: "low"}
# Rank for "sort by priority" (urgent first, no-priority last). Lower = higher.
_PRIORITY_SORT_RANK = {1: 0, 2: 1, 3: 2, 4: 3, 0: 4}

# Closed set of label names the bot is allowed to apply.
_ALLOWED_LABEL_NAMES = {"BE", "FE", "Feature", "Bug", "Improvement", "UI"}

# Classifier status_signal → target Linear workflow state NAME (team convention).
# Match by NAME, not type: "In Progress", "Implemented", and "In Review" all share
# the "started" type here, so type matching can't distinguish them.
#   - "resolved"        → at most "Implemented" (dev-done, not tested). Falls back
#                         to comment-only if the team has no such state.
#   - "in_progress"     → "In Progress".
#   - "cannot_reproduce"→ no mapping → comment-only (Canceled is a PM decision).
# The bot MUST NEVER set Done/Released — see _FORBIDDEN_STATE_* guards.
SIGNAL_TO_STATE_NAME = {
    "resolved": "Implemented",
    "in_progress": "In Progress",
}
# Hard safety rails: the bot never moves an issue into a completed/canceled-type
# state, nor any state named like these, regardless of the mapping above.
_FORBIDDEN_STATE_TYPES = {"completed", "canceled"}
_FORBIDDEN_STATE_NAMES = {"done", "released"}


_CREATE_ISSUE = """
mutation CreateIssue($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue { id identifier url title }
  }
}
"""

_UPDATE_ISSUE = """
mutation UpdateIssue($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) {
    success
    issue { id identifier state { id name type } }
  }
}
"""

_CREATE_COMMENT = """
mutation CreateComment($input: CommentCreateInput!) {
  commentCreate(input: $input) {
    success
    comment { id url }
  }
}
"""

# The ONE sanctioned Linear write beyond create/comment: set an issue's due date. Kept as
# its own mutation (returning dueDate) so the caller can confirm what landed, and so the
# input can NEVER carry a stateId/assigneeId by construction — this path sets dueDate only.
_SET_DUE_DATE = """
mutation SetDueDate($id: String!, $dueDate: TimelessDate!) {
  issueUpdate(id: $id, input: { dueDate: $dueDate }) {
    success
    issue { id identifier dueDate }
  }
}
"""

_TEAM_LABELS = """
query TeamLabels($teamId: String!) {
  team(id: $teamId) {
    labels(first: 250) { nodes { id name } }
  }
}
"""

_TEAM_STATES = """
query TeamStates($teamId: String!) {
  team(id: $teamId) {
    states(first: 50) { nodes { id name type position } }
  }
}
"""

_TEAM_MEMBERS = """
query TeamMembers($teamId: String!) {
  team(id: $teamId) {
    members(first: 100) { nodes { id name displayName email } }
  }
}
"""

# NOTE: teamId is declared ID! (not String!) — the `team.id.eq` filter position
# is an ID comparator; declaring it String! makes Linear reject the whole query
# with GRAPHQL_VALIDATION_FAILED, which the callers swallow into []. `first` is a
# variable so a caller can ask for a broad match set (status "list them all") or
# a cheap top-N (duplicate detection).
_SEARCH_ISSUES = """
query SearchIssues($teamId: ID!, $term: String!, $first: Int!) {
  searchIssues(term: $term, filter: { team: { id: { eq: $teamId } } }, first: $first) {
    nodes {
      id
      identifier
      title
      url
      updatedAt
      state { name type }
      assignee { displayName name }
    }
  }
}
"""

_GET_ISSUE = """
query GetIssue($id: String!) {
  issue(id: $id) {
    id
    identifier
    title
    description
    url
    priority
    estimate
    dueDate
    createdAt
    updatedAt
    startedAt
    state { name type }
    labels { nodes { name } }
    assignee { displayName name email }
    creator { displayName name email }
    cycle { number name }
    project { name }
    parent { identifier title }
    children(first: 50) { nodes { identifier title } }
    history(first: 100) {
      nodes { createdAt fromState { name type } toState { name type } }
    }
    comments(last: 1) {
      nodes { body createdAt user { displayName name } }
    }
  }
}
"""

_ISSUE_HISTORY = """
query IssueHistory($id: String!) {
  issue(id: $id) {
    identifier
    history(first: 100) {
      nodes {
        id
        createdAt
        actor { displayName name }
        fromState { name type }
        toState { name type }
        fromAssignee { displayName name }
        toAssignee { displayName name }
        fromPriority
        toPriority
        fromTitle
        toTitle
        fromDueDate
        toDueDate
        fromEstimate
        toEstimate
      }
    }
  }
}
"""

_LIST_COMMENTS = """
query IssueComments($id: String!) {
  issue(id: $id) {
    identifier
    comments(first: 100) {
      nodes { body createdAt user { displayName name } }
    }
  }
}
"""

_LIST_ISSUES = """
query ListIssues($filter: IssueFilter!, $first: Int!) {
  issues(filter: $filter, first: $first, orderBy: updatedAt) {
    nodes {
      id
      identifier
      title
      url
      priority
      dueDate
      createdAt
      updatedAt
      state { name type }
      labels { nodes { name } }
      assignee { displayName name email }
      creator { displayName name email }
      comments(last: 1) {
        nodes { body createdAt user { displayName name } }
      }
    }
  }
}
"""


# READ-ONLY projection for the Chief-of-Staff gap audit (see escalation.py). It is
# deliberately its OWN query rather than a widening of _LIST_ISSUES: the audit needs
# fields no other caller wants (`description` to judge repro/scope, `startedAt` to age
# an In Progress issue, and the issue's PROJECT TARGET DATE to know a launch is close),
# and _LIST_ISSUES feeds the query engine, where a fatter payload costs tokens on every
# question. `comments(last: 1)` is how we tell "stuck with no explanation" from "stuck,
# and someone said why".
_AUDIT_ISSUES = """
query AuditIssues($filter: IssueFilter!, $first: Int!) {
  issues(filter: $filter, first: $first, orderBy: updatedAt) {
    nodes {
      id
      identifier
      title
      description
      url
      priority
      dueDate
      createdAt
      updatedAt
      startedAt
      state { name type }
      labels { nodes { name } }
      assignee { displayName name email }
      creator { displayName name email }
      project { id name targetDate state }
      comments(last: 1) {
        nodes { body createdAt user { displayName name } }
      }
    }
  }
}
"""


_ASSIGNED_ISSUES = """
query AssignedIssues($filter: IssueFilter!, $first: Int!) {
  issues(filter: $filter, first: $first, orderBy: updatedAt) {
    nodes {
      identifier
      title
      url
      updatedAt
      priority
      state { name type }
    }
  }
}
"""


# READ-ONLY project catalog for the query engine's PROJECT-level answers. Scoped to
# the configured team (team.projects) so the bot only sees NFThing2.0 projects, not
# the whole workspace. This LIGHT query deliberately omits milestones: Linear rejects
# a query whose complexity exceeds 10000, and projectMilestones(first:50) fanned out
# across `first` projects blows that budget. Milestones are fetched per-project by id
# via `_PROJECT_DETAIL` instead. `description` is the full markdown; callers derive a
# one-line summary from it.
_TEAM_PROJECTS = """
query TeamProjects($teamId: String!, $first: Int!) {
  team(id: $teamId) {
    projects(first: $first) {
      nodes {
        id
        name
        description
        url
        startDate
        targetDate
        priority
        progress
        health
        status { name type }
        lead { displayName name email }
      }
    }
  }
}
"""

# Full detail for ONE project by id — including MILESTONES. `projectMilestones` is the
# field name on Project (NOT `milestones`); many projects have none, which is normal.
# Fetched per-project (not fanned across the whole team) to stay under the API's
# query-complexity ceiling.
_PROJECT_DETAIL = """
query ProjectDetail($id: String!) {
  project(id: $id) {
    id
    name
    description
    url
    startDate
    targetDate
    priority
    progress
    health
    status { name type }
    lead { displayName name email }
    projectMilestones(first: 50) {
      nodes { id name targetDate description sortOrder }
    }
  }
}
"""

# Issues within one project, with the per-issue MILESTONE so the engine can roll up
# "6 of 8 dev-complete" by milestone, and the label/state/assignee/dueDate fields a
# project drill-down needs. Filtered by project id (team included for safety).
_PROJECT_ISSUES = """
query ProjectIssues($filter: IssueFilter!, $first: Int!) {
  issues(filter: $filter, first: $first, orderBy: updatedAt) {
    nodes {
      id
      identifier
      title
      url
      priority
      dueDate
      updatedAt
      state { name type }
      labels { nodes { name } }
      assignee { displayName name email }
      projectMilestone { id name targetDate }
    }
  }
}
"""


def _assigned_node_to_dict(node: dict) -> dict:
    """Slim projection used by `active_issues_for_user` — just the fields a
    person-activity reply needs."""
    state = node.get("state") or {}
    return {
        "identifier": node.get("identifier"),
        "title": node.get("title"),
        "state_name": state.get("name"),
        "state_type": state.get("type"),
        "url": node.get("url"),
        "updatedAt": node.get("updatedAt"),
        "priority": node.get("priority"),
    }


def _build_state_history(
    history_nodes: Optional[list], created_at: Optional[str] = None
) -> Optional[list[dict]]:
    """Reconstruct the state timeline from IssueHistory transitions:
    [{state, type, entered_at, left_at}], oldest first. `left_at` is the next
    transition's timestamp (None for the current state). The INITIAL state (the
    issue's state before its first recorded transition — often set at creation
    with no transition event) is seeded from the first event's `fromState`,
    entered at `created_at`, so a "created straight into In Progress" issue still
    shows that leg. Returns None when history wasn't loaded, [] when empty."""
    if history_nodes is None:
        return None
    trans = []
    for n in history_nodes:
        n = n or {}
        to = n.get("toState")
        if to and to.get("name"):
            fr = n.get("fromState") or {}
            trans.append(
                {
                    "from": fr.get("name"),
                    "from_type": fr.get("type"),
                    "to": to["name"],
                    "to_type": to.get("type"),
                    "at": n.get("createdAt"),
                }
            )
    trans.sort(key=lambda e: e["at"] or "")

    timeline: list[dict] = []
    if trans and trans[0]["from"]:
        timeline.append(
            {
                "state": trans[0]["from"],
                "type": trans[0]["from_type"],
                "entered_at": created_at,
                "left_at": trans[0]["at"],
            }
        )
    for i, t in enumerate(trans):
        timeline.append(
            {
                "state": t["to"],
                "type": t["to_type"],
                "entered_at": t["at"],
                "left_at": trans[i + 1]["at"] if i + 1 < len(trans) else None,
            }
        )
    return timeline


def _issue_node_to_dict(node: dict) -> dict:
    """Normalise an Issue GraphQL node into the dict shape callers expect."""
    labels = [
        lab.get("name")
        for lab in (node.get("labels") or {}).get("nodes") or []
        if lab.get("name")
    ]
    comments = (node.get("comments") or {}).get("nodes") or []
    latest_comment = None
    if comments:
        c = comments[0]
        latest_comment = {
            "body": c.get("body") or "",
            "created_at": c.get("createdAt"),
            "author": (c.get("user") or {}).get("displayName")
            or (c.get("user") or {}).get("name"),
        }
    assignee = node.get("assignee") or {}
    creator = node.get("creator") or {}
    state = node.get("state") or {}

    # Extra fields only `_GET_ISSUE` selects — absent (None) for list/search
    # projections, which callers treat as "not loaded".
    prio_raw = node.get("priority")
    priority_label = _PRIORITY_LABEL.get(prio_raw) if prio_raw is not None else None
    cycle = node.get("cycle") or None
    if cycle:
        cycle = {"number": cycle.get("number"), "name": cycle.get("name")}
    project = (node.get("project") or {}).get("name")
    parent = node.get("parent") or None
    if parent:
        parent = {"identifier": parent.get("identifier"), "title": parent.get("title")}
    children = [
        {"identifier": c.get("identifier"), "title": c.get("title")}
        for c in (node.get("children") or {}).get("nodes") or []
        if c and c.get("identifier")
    ]

    # State timeline (only `_GET_ISSUE` selects `history`). Also expose a
    # first-entered map so the engine can cite "moved to In Progress on <date>".
    history_nodes = (node.get("history") or {}).get("nodes") if node.get("history") else None
    state_history = _build_state_history(history_nodes, node.get("createdAt"))
    state_entered = None
    if state_history:
        state_entered = {}
        for h in state_history:
            state_entered.setdefault(h["state"], h["entered_at"])

    return {
        "id": node.get("id"),
        "identifier": node.get("identifier"),
        "title": node.get("title"),
        # Only `_GET_ISSUE` selects `description`; list/search projections leave
        # it out, so this is None for those (callers treat None as "not loaded").
        "description": node.get("description"),
        "url": node.get("url"),
        "state": state.get("name"),
        "state_type": state.get("type"),
        "labels": labels,
        "assignee": assignee.get("displayName") or assignee.get("name"),
        "assignee_email": assignee.get("email"),
        "creator": creator.get("displayName") or creator.get("name"),
        "creator_email": creator.get("email"),
        "priority": priority_label,
        "priority_value": prio_raw,
        "estimate": node.get("estimate"),
        "dueDate": node.get("dueDate"),
        "cycle": cycle,
        "project": project,
        "parent": parent,
        "children": children,
        "created_at": node.get("createdAt"),
        "updated_at": node.get("updatedAt"),
        "started_at": node.get("startedAt"),
        "state_history": state_history,
        "state_entered": state_entered,
        "latest_comment": latest_comment,
    }


def _project_summary(description: Optional[str]) -> Optional[str]:
    """First meaningful line of a project's markdown description — a one-line
    summary for list_projects. Strips leading heading hashes; caps length."""
    for line in (description or "").splitlines():
        s = line.strip().lstrip("#").strip().strip("*").strip()
        if s:
            return s if len(s) <= 240 else s[:237] + "…"
    return None


def _project_node_to_dict(node: dict) -> dict:
    """Normalise a Project GraphQL node into the dict shape the engine tools return.
    Includes the milestone list (name + target date), sorted by the project's own
    ordering; issue counts are attached later by the caller from the project's issues."""
    lead = node.get("lead") or {}
    status = node.get("status") or {}
    prio_raw = node.get("priority")
    progress = node.get("progress")
    milestones: list[dict] = []
    for m in (node.get("projectMilestones") or {}).get("nodes") or []:
        if not m or not m.get("id"):
            continue
        desc = (m.get("description") or "").strip()
        milestones.append(
            {
                "id": m.get("id"),
                "name": m.get("name"),
                "target_date": m.get("targetDate"),
                "description": desc or None,
                "_sort": m.get("sortOrder") if m.get("sortOrder") is not None else 0.0,
            }
        )
    milestones.sort(key=lambda x: x["_sort"])
    for m in milestones:
        m.pop("_sort", None)

    description = (node.get("description") or "").strip()
    return {
        "id": node.get("id"),
        "name": node.get("name"),
        "description": description or None,
        "summary": _project_summary(description),
        "url": node.get("url"),
        "start_date": node.get("startDate"),
        "target_date": node.get("targetDate"),
        "priority": _PRIORITY_LABEL.get(prio_raw) if prio_raw is not None else None,
        "priority_value": prio_raw,
        "progress_pct": round(progress * 100) if progress is not None else None,
        "health": node.get("health"),
        "status": status.get("name"),
        "status_type": status.get("type"),
        "lead": lead.get("displayName") or lead.get("name"),
        "milestones": milestones,
    }


def _project_issue_node_to_dict(node: dict) -> dict:
    """Compact per-issue projection for the project drill-down: identifier, title,
    state, label, assignee, milestone, dueDate, updatedAt (+ priority, url)."""
    labels = [
        lab.get("name")
        for lab in (node.get("labels") or {}).get("nodes") or []
        if lab.get("name")
    ]
    assignee = node.get("assignee") or {}
    state = node.get("state") or {}
    milestone = node.get("projectMilestone") or {}
    prio_raw = node.get("priority")
    return {
        "identifier": node.get("identifier"),
        "title": node.get("title"),
        "url": node.get("url"),
        "state": state.get("name"),
        "state_type": state.get("type"),
        "labels": labels,
        "assignee": assignee.get("displayName") or assignee.get("name"),
        "milestone": milestone.get("name"),
        "milestone_id": milestone.get("id"),
        "milestone_target_date": milestone.get("targetDate"),
        "priority": _PRIORITY_LABEL.get(prio_raw) if prio_raw is not None else None,
        "priority_value": prio_raw,
        "dueDate": node.get("dueDate"),
        "updatedAt": node.get("updatedAt"),
    }


class LinearError(RuntimeError):
    pass


class LinearClient:
    def __init__(self, api_key: str, team_id: str) -> None:
        self._headers = {
            "Authorization": api_key,
            "Content-Type": "application/json",
        }
        self._team_id = team_id
        # Lazy caches — populated on first use, kept for the life of the client.
        self._label_ids: dict[str, str] = {}
        self._labels_loaded = False
        self._states: list[dict] = []
        self._states_loaded = False
        self._members: list[dict] = []
        self._members_loaded = False

    async def _gql(self, query: str, variables: dict) -> dict:
        # First word of the GraphQL doc gives us "query" / "mutation"; second
        # token usually names the operation — enough to identify which call this is.
        op_label = " ".join(query.strip().split()[:2])
        log.info("[linear._gql] → %s variables=%s", op_label, variables)
        async with httpx.AsyncClient(timeout=20.0) as client:
            try:
                r = await client.post(
                    LINEAR_API_URL,
                    headers=self._headers,
                    json={"query": query, "variables": variables},
                )
            except httpx.HTTPError:
                log.exception("[linear._gql] %s transport error", op_label)
                raise
            log.info("[linear._gql] ← %s HTTP %s", op_label, r.status_code)
            if r.status_code >= 400:
                log.error("[linear._gql] %s error body: %s", op_label, r.text[:500])
            r.raise_for_status()
            payload = r.json()
            if "errors" in payload:
                log.error("[linear._gql] %s GraphQL errors: %s", op_label, payload["errors"])
                raise LinearError(f"Linear GraphQL error: {payload['errors']}")
            log.debug("[linear._gql] %s data: %s", op_label, payload.get("data"))
            return payload["data"]

    # -- caches: labels / states / members -----------------------------------

    async def _get_team_labels(self) -> dict[str, str]:
        if not self._labels_loaded:
            log.info("[linear._get_team_labels] fetching label catalog for team %s", self._team_id)
            data = await self._gql(_TEAM_LABELS, {"teamId": self._team_id})
            nodes = data["team"]["labels"]["nodes"]
            self._label_ids = {n["name"]: n["id"] for n in nodes}
            self._labels_loaded = True
            log.info("[linear._get_team_labels] cached %d labels", len(self._label_ids))
        return self._label_ids

    async def list_team_states(self) -> list[dict]:
        """Return the team's workflow states — list of {id, name, type, position}.

        Sorted by `position`. Cached for the life of the client. Used internally
        by `set_issue_status`; exposed publicly so callers can introspect what
        types the team supports.
        """
        if not self._states_loaded:
            log.info("[linear.list_team_states] fetching states for team %s", self._team_id)
            data = await self._gql(_TEAM_STATES, {"teamId": self._team_id})
            nodes = data["team"]["states"]["nodes"]
            self._states = sorted(nodes, key=lambda s: s.get("position") or 0)
            self._states_loaded = True
            log.info(
                "[linear.list_team_states] cached %d states: %s",
                len(self._states),
                [(s.get("name"), s.get("type")) for s in self._states],
            )
        return self._states

    async def _get_team_members(self) -> list[dict]:
        if not self._members_loaded:
            log.info("[linear._get_team_members] fetching members for team %s", self._team_id)
            data = await self._gql(_TEAM_MEMBERS, {"teamId": self._team_id})
            self._members = data["team"]["members"]["nodes"]
            self._members_loaded = True
            log.info("[linear._get_team_members] cached %d members", len(self._members))
        return self._members

    async def list_team_members(self) -> list[dict]:
        """Public read-only roster of the configured team as
        [{id, name, displayName, email}].

        Wraps the same cached fetch `resolve_assignee` uses. Returns [] on any
        error — never raises (query-mode convention, like `list_issues`).
        """
        log.info("[linear.list_team_members] step 1/2: team %s", self._team_id)
        try:
            members = await self._get_team_members()
        except Exception:
            log.exception("[linear.list_team_members] failed; returning []")
            return []
        out = [
            {
                "id": m.get("id"),
                "name": m.get("name"),
                "displayName": m.get("displayName"),
                "email": m.get("email"),
            }
            for m in members
            if m.get("id")
        ]
        log.info("[linear.list_team_members] step 2/2: %d members", len(out))
        return out

    # -- resolvers -----------------------------------------------------------

    async def resolve_label_ids(self, names: list[str]) -> list[str]:
        """Resolve label NAMES → label IDs on the team.

        Only names in {BE, FE, Feature, Bug, Improvement, UI} are considered;
        anything else is silently dropped. Allowed names that aren't actually
        on the team are logged and dropped — labels are NEVER auto-created
        from this path.
        """
        log.info("[linear.resolve_label_ids] step 1/2: requested=%s", names)
        catalog = await self._get_team_labels()

        log.info("[linear.resolve_label_ids] step 2/2: matching against allowlist + catalog")
        ids: list[str] = []
        seen: set[str] = set()
        for name in names or []:
            if name not in _ALLOWED_LABEL_NAMES:
                log.debug("[linear.resolve_label_ids] %r outside allowlist; skip", name)
                continue
            if name in seen:
                continue
            seen.add(name)
            lid = catalog.get(name)
            if lid is None:
                log.warning(
                    "[linear.resolve_label_ids] allowed label %r not present on team %s; skip",
                    name,
                    self._team_id,
                )
                continue
            ids.append(lid)
        log.info(
            "[linear.resolve_label_ids] DONE: resolved %d/%d → %s",
            len(ids),
            len(names or []),
            ids,
        )
        return ids

    async def resolve_assignee(
        self,
        *,
        discord_user_id: int,
        display_name: str,
        discord_linear_map: dict,
    ) -> Optional[str]:
        """Resolve a Discord user → Linear user id.

        Step 1: look up the Discord id (as a string key) in `discord_linear_map`.
                The value is either a Linear email or a Linear user UUID;
                validate it against the team's members and return the matching
                member id.
        Step 2: otherwise, match `display_name` (case-insensitive) against the
                team's member `displayName` / `name`.
        Returns the Linear user id, or None when nothing matches.
        """
        log.info(
            "[linear.resolve_assignee] step 1/3: discord_id=%s display=%r map_entries=%d",
            discord_user_id,
            display_name,
            len(discord_linear_map),
        )
        members = await self._get_team_members()
        by_id = {m["id"]: m for m in members}
        by_email = {
            (m.get("email") or "").strip().lower(): m
            for m in members
            if m.get("email")
        }

        log.info("[linear.resolve_assignee] step 2/3: checking Discord→Linear map")
        ref = discord_linear_map.get(str(discord_user_id))
        if ref:
            ref = str(ref).strip()
            if "@" in ref:
                hit = by_email.get(ref.lower())
                if hit:
                    log.info(
                        "[linear.resolve_assignee] mapped email %r → user id %s (%s)",
                        ref,
                        hit["id"],
                        hit.get("displayName") or hit.get("name"),
                    )
                    return hit["id"]
                log.warning(
                    "[linear.resolve_assignee] mapped email %r not on team — falling back to name match",
                    ref,
                )
            else:
                hit = by_id.get(ref)
                if hit:
                    log.info(
                        "[linear.resolve_assignee] mapped Linear id %r is on team (%s)",
                        ref,
                        hit.get("displayName") or hit.get("name"),
                    )
                    return ref
                log.warning(
                    "[linear.resolve_assignee] mapped Linear id %r not on team — falling back to name match",
                    ref,
                )

        log.info("[linear.resolve_assignee] step 3/3: display-name fallback")
        wanted = (display_name or "").strip().lower()
        if wanted:
            for m in members:
                for field in ("displayName", "name"):
                    v = (m.get(field) or "").strip().lower()
                    if v and v == wanted:
                        log.info(
                            "[linear.resolve_assignee] name match %r → user id %s",
                            display_name,
                            m["id"],
                        )
                        return m["id"]

        log.info(
            "[linear.resolve_assignee] DONE: no match for discord_id=%s name=%r",
            discord_user_id,
            display_name,
        )
        return None

    # -- writes --------------------------------------------------------------

    async def create_issue(
        self,
        *,
        title: str,
        description: str,
        priority: str = "medium",
        label_names: Optional[list[str]] = None,
        assignee_id: Optional[str] = None,
    ) -> dict:
        """Create an issue. Returns the issue dict (id, identifier, url, title).

        Workflow state is intentionally NOT set on creation — new issues land
        in the team's default starting state. Use `set_issue_status` afterwards
        if a non-default state is required.
        """
        log.info(
            "[linear.create_issue] step 1/3: title=%r priority=%s labels=%s assignee=%s desc_len=%d",
            title,
            priority,
            label_names or [],
            assignee_id,
            len(description),
        )

        log.info("[linear.create_issue] step 2/3: resolving label names → ids")
        label_ids = await self.resolve_label_ids(label_names or [])

        payload: dict = {
            "teamId": self._team_id,
            "title": title,
            "description": description,
            "priority": _PRIORITY_MAP.get(priority, 3),
        }
        if label_ids:
            payload["labelIds"] = label_ids
        if assignee_id:
            payload["assigneeId"] = assignee_id

        log.info(
            "[linear.create_issue] step 3/3: issueCreate team=%s priority=%s labels=%s assignee=%s",
            self._team_id,
            payload["priority"],
            label_ids,
            assignee_id,
        )
        data = await self._gql(_CREATE_ISSUE, {"input": payload})
        result = data["issueCreate"]
        if not result.get("success") or not result.get("issue"):
            log.error("[linear.create_issue] issueCreate returned non-success: %s", result)
            raise LinearError("Linear refused to create the issue")
        log.info(
            "[linear.create_issue] DONE: identifier=%s url=%s",
            result["issue"].get("identifier"),
            result["issue"].get("url"),
        )
        return result["issue"]

    async def add_comment(self, issue_id: str, body: str) -> dict:
        """Post a comment on an issue. Returns the created comment {id, url}.
        Raises LinearError on failure (same convention as `create_issue`).
        """
        log.info("[linear.add_comment] step 1/2: issue=%s body_len=%d", issue_id, len(body))
        data = await self._gql(
            _CREATE_COMMENT,
            {"input": {"issueId": issue_id, "body": body}},
        )
        log.info("[linear.add_comment] step 2/2: parsing result")
        result = data["commentCreate"]
        if not result.get("success") or not result.get("comment"):
            log.error("[linear.add_comment] commentCreate returned non-success: %s", result)
            raise LinearError("Linear refused to create the comment")
        log.info(
            "[linear.add_comment] DONE: comment id=%s url=%s",
            result["comment"].get("id"),
            result["comment"].get("url"),
        )
        return result["comment"]

    async def set_issue_status(self, issue_id: str, signal: str) -> Optional[dict]:
        """Move `issue_id` toward the team-convention state NAMED for `signal`:

            resolved          → "Implemented"  (at most — dev-done, not tested)
            in_progress       → "In Progress"
            cannot_reproduce  → no-op (comment only; Canceled is a PM decision)
            anything else     → no-op

        Matched by state NAME (case-insensitive): "In Progress", "Implemented",
        and "In Review" all share the "started" type here, so type matching can't
        tell them apart. If the named state doesn't exist on the team we fall back
        to comment-only (no move). The bot NEVER moves an issue into a
        completed/canceled-type state or one named done/released — a hard guard,
        so resolution signals can't accidentally close QA-gated work.

        Returns the updated issue dict, or None when there was nothing to do
        (unmapped signal, missing/forbidden target state, or an API failure).
        Logs and swallows errors — never raises.
        """
        log.info("[linear.set_issue_status] step 1/3: issue=%s signal=%s", issue_id, signal)
        target_name = SIGNAL_TO_STATE_NAME.get(signal)
        if target_name is None:
            log.info(
                "[linear.set_issue_status] signal %r maps to no state (comment-only); no-op",
                signal,
            )
            return None

        log.info(
            "[linear.set_issue_status] step 2/3: locating state named %r",
            target_name,
        )
        try:
            states = await self.list_team_states()
        except Exception:
            log.exception("[linear.set_issue_status] could not load team states; no-op")
            return None
        want = target_name.strip().lower()
        target = next(
            (s for s in states if (s.get("name") or "").strip().lower() == want), None
        )
        if target is None:
            log.warning(
                "[linear.set_issue_status] team %s has no state named %r; "
                "comment-only fallback (no move)",
                self._team_id,
                target_name,
            )
            return None

        # Hard safety rail: never move into a completed/canceled or done/released
        # state, even if the mapping/name somehow pointed there.
        if (
            target.get("type") in _FORBIDDEN_STATE_TYPES
            or (target.get("name") or "").strip().lower() in _FORBIDDEN_STATE_NAMES
        ):
            log.error(
                "[linear.set_issue_status] REFUSING to move %s into forbidden state "
                "%r (type=%s) — the bot never sets Done/Released",
                issue_id, target.get("name"), target.get("type"),
            )
            return None

        log.info(
            "[linear.set_issue_status] step 3/3: issueUpdate %s → state %r (id=%s)",
            issue_id,
            target.get("name"),
            target["id"],
        )
        try:
            data = await self._gql(
                _UPDATE_ISSUE,
                {"id": issue_id, "input": {"stateId": target["id"]}},
            )
        except Exception:
            log.exception("[linear.set_issue_status] issueUpdate failed; no-op")
            return None
        result = data["issueUpdate"]
        if not result.get("success") or not result.get("issue"):
            log.error(
                "[linear.set_issue_status] issueUpdate returned non-success: %s",
                result,
            )
            return None
        log.info(
            "[linear.set_issue_status] DONE: issue %s now in state %r",
            result["issue"].get("identifier"),
            (result["issue"].get("state") or {}).get("name", "?"),
        )
        return result["issue"]

    async def set_issue_due_date(self, issue_id: str, due_date: str) -> Optional[dict]:
        """WRITE: set/update ONE issue's due date to `due_date` ("YYYY-MM-DD").

        This is the CoS "close the loop" write (COS_UPDATE_DEADLINE_ENABLED). It sets the
        DUE DATE and NOTHING else — the mutation's input can't carry a state or assignee —
        so it can never move or reassign an issue. Pair it with `add_comment` to note the
        new date and its source (the caller does that). Returns the updated issue
        {id, identifier, dueDate} or None on failure. Logs and swallows errors — the
        deadline path degrades to "did nothing", never to raising into the sweeper.
        """
        iid = str(issue_id or "").strip()
        dd = str(due_date or "").strip()
        log.info("[linear.set_issue_due_date] step 1/2: issue=%s dueDate=%s", iid, dd)
        if not iid or not dd:
            log.warning("[linear.set_issue_due_date] missing issue id or date; no-op")
            return None
        try:
            data = await self._gql(_SET_DUE_DATE, {"id": iid, "dueDate": dd})
        except Exception:
            log.exception("[linear.set_issue_due_date] issueUpdate failed; no-op")
            return None
        result = data.get("issueUpdate") or {}
        if not result.get("success") or not result.get("issue"):
            log.error("[linear.set_issue_due_date] returned non-success: %s", result)
            return None
        log.info(
            "[linear.set_issue_due_date] DONE: %s dueDate now %s",
            result["issue"].get("identifier"), result["issue"].get("dueDate"),
        )
        return result["issue"]

    # -- reads ---------------------------------------------------------------

    async def get_issue(self, id_or_identifier: str) -> Optional[dict]:
        """Fetch a single issue by UUID or by identifier (e.g. "NFT-123") for
        query mode. Returns the normalised dict or None on error / not found.
        Read-only, never raises."""
        log.info("[linear.get_issue] step 1/2: id=%r", id_or_identifier)
        if not id_or_identifier or not str(id_or_identifier).strip():
            return None
        try:
            data = await self._gql(_GET_ISSUE, {"id": str(id_or_identifier).strip()})
        except Exception:
            log.exception("[linear.get_issue] failed; returning None")
            return None
        node = data.get("issue")
        if not node:
            log.info("[linear.get_issue] step 2/2: not found")
            return None
        log.info("[linear.get_issue] step 2/2: hit %s", node.get("identifier"))
        return _issue_node_to_dict(node)

    async def list_issues(
        self,
        *,
        creator_id: Optional[str] = None,
        label_names: Optional[list[str]] = None,
        state_types: Optional[list[str]] = None,
        created_after: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict]:
        """Filtered list of the team's issues for query mode. All filters are
        AND-ed and optional; pass None to skip a dimension. Returns [] on any
        error. Read-only, never raises."""
        f: dict = {"team": {"id": {"eq": self._team_id}}}
        if creator_id:
            f["creator"] = {"id": {"eq": creator_id}}
        if state_types:
            f["state"] = {"type": {"in": list(state_types)}}
        if label_names:
            f["labels"] = {"some": {"name": {"in": list(label_names)}}}
        if created_after:
            f["createdAt"] = {"gte": created_after}

        log.info(
            "[linear.list_issues] step 1/2: creator=%s labels=%s state_types=%s after=%s limit=%d",
            creator_id, label_names, state_types, created_after, limit,
        )
        try:
            data = await self._gql(_LIST_ISSUES, {"filter": f, "first": int(limit)})
        except Exception:
            log.exception("[linear.list_issues] failed; returning []")
            return []
        nodes = (data.get("issues") or {}).get("nodes") or []
        results = [_issue_node_to_dict(n) for n in nodes if n]
        log.info("[linear.list_issues] step 2/2: %d results", len(results))
        return results

    async def list_open_issues_for_audit(self, *, limit: int = 50) -> list[dict]:
        """READ-ONLY: the team's OPEN issues, with the extra fields the Chief-of-Staff
        gap audit needs — `description` (to judge a missing repro / vague scope),
        `startedAt` (to age an In Progress issue), the latest comment (to tell "stuck
        with no explanation" from "stuck, and someone explained"), and the issue's
        PROJECT with its `targetDate` (so a launch-milestone issue can be spotted as
        its deadline approaches).

        Open = backlog | unstarted | started | triage. Completed and cancelled issues
        are never chased. Returns [] on any error — the audit degrades to doing
        nothing, never to raising into the sweeper. This method only READS; nothing in
        the escalation path writes to Linear.
        """
        f = {
            "team": {"id": {"eq": self._team_id}},
            "state": {"type": {"in": ["backlog", "unstarted", "started", "triage"]}},
        }
        log.info("[linear.audit] step 1/2: fetching open issues (limit=%d)", limit)
        try:
            data = await self._gql(_AUDIT_ISSUES, {"filter": f, "first": int(limit)})
        except Exception:
            log.exception("[linear.audit] failed; returning [] (audit no-ops)")
            return []

        nodes = (data.get("issues") or {}).get("nodes") or []
        out: list[dict] = []
        for n in nodes:
            if not n:
                continue
            row = _issue_node_to_dict(n)
            # `_issue_node_to_dict` flattens project to its NAME only; the audit needs
            # the target date too, so carry the full project through alongside it.
            proj = n.get("project") or {}
            row["project_id"] = proj.get("id")
            row["project_target_date"] = proj.get("targetDate")
            row["project_state"] = proj.get("state")
            out.append(row)
        log.info("[linear.audit] step 2/2: %d open issue(s)", len(out))
        return out

    async def list_active_issues_for_audit(
        self, *, state_names: list[str], limit: int = 50
    ) -> list[dict]:
        """READ-ONLY: the team's ACTIVE issues for the escalation/tagging audit — those
        whose workflow-state NAME is in `state_names` (e.g. In Progress / Implemented /
        awaiting QA / In Review). Same rich projection as `list_open_issues_for_audit`
        (description, started_at, latest comment, project + target date).

        Filtered by state NAME server-side (not TYPE): these states share the
        "started"/"completed" type, so only the name distinguishes "actively being worked"
        from Backlog/Todo/Done/Canceled. Empty `state_names` → [] (chase nothing) rather
        than accidentally auditing everything. Returns [] on any error — never raises.
        """
        names = [s for s in (state_names or []) if s and s.strip()]
        if not names:
            log.info("[linear.audit_active] no state names given; returning [] (chase nothing)")
            return []
        f = {
            "team": {"id": {"eq": self._team_id}},
            "state": {"name": {"in": names}},
        }
        log.info(
            "[linear.audit_active] step 1/2: fetching active issues by name=%s (limit=%d)",
            names, limit,
        )
        try:
            data = await self._gql(_AUDIT_ISSUES, {"filter": f, "first": int(limit)})
        except Exception:
            log.exception("[linear.audit_active] failed; returning [] (audit no-ops)")
            return []
        nodes = (data.get("issues") or {}).get("nodes") or []
        out: list[dict] = []
        for n in nodes:
            if not n:
                continue
            row = _issue_node_to_dict(n)
            proj = n.get("project") or {}
            row["project_id"] = proj.get("id")
            row["project_target_date"] = proj.get("targetDate")
            row["project_state"] = proj.get("state")
            out.append(row)
        log.info("[linear.audit_active] step 2/2: %d active issue(s)", len(out))
        return out

    async def active_issues_for_user(
        self,
        user_id: str,
        updated_after: Optional[str] = None,
        *,
        label_names: Optional[list[str]] = None,
        limit: int = 25,
    ) -> list[dict]:
        """Issues ASSIGNED to `user_id` on this team that are EITHER in a
        "started"-type workflow state OR were updated since `updated_after`
        (ISO-8601). The two conditions are OR-ed; pass `updated_after=None` to
        fall back to "started"-only. When `label_names` is given, only issues
        carrying one of those labels are returned (a category filter).

        Returns {identifier, title, state_name, state_type, url, updatedAt,
        priority} dicts, newest-updated first. Returns [] on any error or when
        `user_id` is empty. Read-only, never raises."""
        log.info(
            "[linear.active_issues_for_user] step 1/2: user=%s updated_after=%s labels=%s limit=%d",
            user_id, updated_after, label_names or [], limit,
        )
        if not user_id:
            return []

        or_clauses: list[dict] = [{"state": {"type": {"eq": "started"}}}]
        if updated_after:
            or_clauses.append({"updatedAt": {"gte": updated_after}})
        f: dict = {
            "team": {"id": {"eq": self._team_id}},
            "assignee": {"id": {"eq": user_id}},
            "or": or_clauses,
        }
        if label_names:
            f["labels"] = {"some": {"name": {"in": list(label_names)}}}
        try:
            data = await self._gql(_ASSIGNED_ISSUES, {"filter": f, "first": int(limit)})
        except Exception:
            log.exception("[linear.active_issues_for_user] failed; returning []")
            return []
        nodes = (data.get("issues") or {}).get("nodes") or []
        results = [_assigned_node_to_dict(n) for n in nodes if n]
        # Guarantee updatedAt-desc regardless of the API's orderBy direction.
        results.sort(key=lambda r: r.get("updatedAt") or "", reverse=True)
        log.info("[linear.active_issues_for_user] step 2/2: %d results", len(results))
        return results

    async def recent_issues_created_by(
        self,
        user_id: str,
        created_after: Optional[str] = None,
        *,
        limit: int = 25,
    ) -> list[dict]:
        """Issues CREATED by `user_id` on this team, optionally since
        `created_after` (ISO-8601). Thin wrapper over `list_issues`; same
        normalised dict shape. Returns [] on error or empty `user_id`."""
        log.info(
            "[linear.recent_issues_created_by] user=%s created_after=%s limit=%d",
            user_id, created_after, limit,
        )
        if not user_id:
            return []
        return await self.list_issues(
            creator_id=user_id,
            created_after=created_after,
            limit=limit,
        )

    async def search_issues(self, query: str) -> list[dict]:
        """Best-effort text search of the team's issues by title / key terms.

        Returns a small list of {id, identifier, title, state, url}, or [] on
        any error (transport, GraphQL, parsing). Never raises.
        """
        log.info("[linear.search_issues] step 1/2: term=%r team=%s", query, self._team_id)
        if not query or not query.strip():
            log.info("[linear.search_issues] empty query; returning []")
            return []
        try:
            data = await self._gql(
                _SEARCH_ISSUES,
                {"teamId": self._team_id, "term": query, "first": 10},
            )
        except Exception:
            log.exception("[linear.search_issues] search failed; returning []")
            return []
        try:
            nodes = (data.get("searchIssues") or {}).get("nodes") or []
            results = [
                {
                    "id": n["id"],
                    "identifier": n["identifier"],
                    "title": n["title"],
                    "state": (n.get("state") or {}).get("name"),
                    "url": n["url"],
                }
                for n in nodes
            ]
        except Exception:
            log.exception("[linear.search_issues] result parsing failed; returning []")
            return []
        log.info("[linear.search_issues] DONE: %d hits", len(results))
        return results

    async def find_issues_by_text(
        self, text: str, *, include_closed: bool = False, limit: int = 25
    ) -> list[dict]:
        """Fuzzy-find team issues whose title / keywords match `text` (e.g.
        "DMs", "payout bug") for an issue-status lookup.

        Returns ALL matches (up to `limit`) as a RANKED list of {identifier,
        title, state_name, state_type, assignee_name, url, updatedAt}. Open
        issues come first (closed/cancelled are dropped unless `include_closed`);
        within a group Linear's own relevance order is kept, with recency as the
        tiebreak. [] on empty input or any error. Read-only, never raises.

        Matching uses Linear's `searchIssues` full-text index, which spans the
        title, description, and comments and is acronym/synonym-aware — so both
        "DMs" and "direct messages" surface the same direct-message issues; the
        caller does not need to expand aliases itself.
        """
        log.info(
            "[linear.find_issues_by_text] step 1/2: text=%r include_closed=%s limit=%d",
            text, include_closed, limit,
        )
        if not text or not text.strip():
            return []
        # Ask the API for a generous match set so "list them all" isn't silently
        # capped below `limit` by the query's own page size.
        first = max(int(limit), 25)
        try:
            data = await self._gql(
                _SEARCH_ISSUES,
                {"teamId": self._team_id, "term": text.strip(), "first": first},
            )
        except Exception:
            log.exception("[linear.find_issues_by_text] search failed; returning []")
            return []

        nodes = (data.get("searchIssues") or {}).get("nodes") or []
        closed_types = {"completed", "canceled"}
        ranked: list[dict] = []
        for rank, n in enumerate(nodes):
            if not n:
                continue
            state = n.get("state") or {}
            stype = state.get("type")
            is_open = stype not in closed_types
            if not include_closed and not is_open:
                continue
            assignee = n.get("assignee") or {}
            ranked.append(
                {
                    "identifier": n.get("identifier"),
                    "title": n.get("title"),
                    "state_name": state.get("name"),
                    "state_type": stype,
                    "url": n.get("url"),
                    "updatedAt": n.get("updatedAt"),
                    "assignee_name": assignee.get("displayName") or assignee.get("name"),
                    "_is_open": is_open,
                    "_rank": rank,
                }
            )
        # Open first, then Linear's relevance order; recency breaks any tie.
        # (Stable sort: pre-order by recency, then by open-ness + relevance.)
        ranked.sort(key=lambda r: r.get("updatedAt") or "", reverse=True)
        ranked.sort(key=lambda r: (not r["_is_open"], r["_rank"]))
        for r in ranked:
            r.pop("_is_open", None)
            r.pop("_rank", None)
        out = ranked[: max(1, int(limit))]
        log.info("[linear.find_issues_by_text] step 2/2: %d match(es)", len(out))
        return out

    # -- engine-facing reads (tool-driven query mode) ------------------------

    async def resolve_member_id(self, name: str) -> Optional[dict]:
        """Fuzzy-resolve a free-text person `name` to ONE team member.

        Returns {"id", "displayName"} on a single confident match, or a dict
        {"error": "...", "candidates": [...]} when nothing/many match — never
        raises. Matching is case-insensitive over displayName / name / email
        local-part: exact (or first-name) wins; a substring match is the
        fallback. Used by the query engine so a caller can filter by "Ravi"
        without knowing the UUID."""
        wanted = (name or "").strip().lower()
        if not wanted:
            return {"error": "empty name", "candidates": []}
        try:
            members = await self._get_team_members()
        except Exception:
            log.exception("[linear.resolve_member_id] members fetch failed")
            return {"error": "could not load team members", "candidates": []}

        exact: list[dict] = []
        partial: list[dict] = []
        for m in members:
            fields = [
                (m.get("displayName") or "").strip().lower(),
                (m.get("name") or "").strip().lower(),
            ]
            email = (m.get("email") or "").strip().lower()
            local = email.split("@", 1)[0] if email else ""
            is_exact = False
            for f in fields:
                if not f:
                    continue
                if f == wanted or (f.split()[0] if f.split() else f) == wanted:
                    is_exact = True
                    break
            if not is_exact and (email == wanted or local == wanted):
                is_exact = True
            if is_exact:
                exact.append(m)
                continue
            if len(wanted) >= 2 and (
                any(wanted in f for f in fields if f) or (local and wanted in local)
            ):
                partial.append(m)

        matches = exact or partial
        if len(matches) == 1:
            hit = matches[0]
            return {
                "id": hit.get("id"),
                "displayName": hit.get("displayName") or hit.get("name"),
            }
        cands = [
            {"displayName": m.get("displayName") or m.get("name"), "email": m.get("email")}
            for m in matches
        ]
        if not matches:
            return {"error": f"no team member matches '{name}'", "candidates": []}
        return {"error": f"'{name}' is ambiguous", "candidates": cands}

    async def get_issue_history(self, id_or_identifier: str) -> list[dict]:
        """Return the change history of one issue (state/assignee/priority/title/
        due-date/estimate transitions) as compact events, oldest→newest:

            {"at": iso, "actor": name, "changes": ["status: A → B", ...]}

        This is the ONLY way to answer "when did the status change". Events with
        no recognised change are dropped. Returns [] on error / not found or when
        the issue has no history. Read-only, never raises."""
        ident = str(id_or_identifier or "").strip()
        log.info("[linear.get_issue_history] step 1/2: id=%r", ident)
        if not ident:
            return []
        try:
            data = await self._gql(_ISSUE_HISTORY, {"id": ident})
        except Exception:
            log.exception("[linear.get_issue_history] failed; returning []")
            return []
        issue = data.get("issue") or {}
        nodes = (issue.get("history") or {}).get("nodes") or []

        def _nm(v):
            return (v or {}).get("displayName") or (v or {}).get("name") if v else None

        events: list[dict] = []
        for n in nodes:
            if not n:
                continue
            changes: list[str] = []
            fs, ts = _nm(n.get("fromState")), _nm(n.get("toState"))
            if ts and ts != fs:
                changes.append(f"status: {fs or '—'} → {ts}")
            fa, ta = _nm(n.get("fromAssignee")), _nm(n.get("toAssignee"))
            if (fa or ta) and fa != ta:
                changes.append(f"assignee: {fa or 'unassigned'} → {ta or 'unassigned'}")
            fp, tp = n.get("fromPriority"), n.get("toPriority")
            if tp is not None and tp != fp:
                changes.append(
                    f"priority: {_PRIORITY_LABEL.get(fp, fp)} → {_PRIORITY_LABEL.get(tp, tp)}"
                )
            ftt, ttt = n.get("fromTitle"), n.get("toTitle")
            if ttt and ttt != ftt:
                changes.append("title changed")
            fd, td = n.get("fromDueDate"), n.get("toDueDate")
            if td != fd and (fd or td):
                changes.append(f"due date: {fd or 'none'} → {td or 'none'}")
            fe, te = n.get("fromEstimate"), n.get("toEstimate")
            if te != fe and (fe is not None or te is not None):
                changes.append(f"estimate: {fe if fe is not None else '—'} → {te if te is not None else '—'}")
            if not changes:
                continue
            events.append(
                {
                    "at": n.get("createdAt"),
                    "actor": _nm(n.get("actor")) or "unknown",
                    "changes": changes,
                }
            )
        events.sort(key=lambda e: e.get("at") or "")
        log.info(
            "[linear.get_issue_history] step 2/2: %s → %d change event(s)",
            issue.get("identifier") or ident, len(events),
        )
        return events

    async def list_comments(self, id_or_identifier: str) -> list[dict]:
        """All comments on one issue as [{author, createdAt, body}] in date order
        (oldest first) — so the engine can read what actually happened (blockers,
        "waiting on API", "re-tested, fails"). [] on error / not found. Read-only,
        never raises. Bodies are capped so a chatty issue can't blow the payload."""
        ident = str(id_or_identifier or "").strip()
        log.info("[linear.list_comments] step 1/2: id=%r", ident)
        if not ident:
            return []
        try:
            data = await self._gql(_LIST_COMMENTS, {"id": ident})
        except Exception:
            log.exception("[linear.list_comments] failed; returning []")
            return []
        nodes = ((data.get("issue") or {}).get("comments") or {}).get("nodes") or []
        out: list[dict] = []
        for n in nodes:
            if not n:
                continue
            user = n.get("user") or {}
            body = (n.get("body") or "").strip()
            if len(body) > 1200:
                body = body[:1197] + "…"
            out.append(
                {
                    "author": user.get("displayName") or user.get("name"),
                    "createdAt": n.get("createdAt"),
                    "body": body,
                }
            )
        out.sort(key=lambda c: c.get("createdAt") or "")
        log.info("[linear.list_comments] step 2/2: %d comment(s)", len(out))
        return out

    async def list_issues_query(
        self,
        *,
        assignee_id: Optional[str] = None,
        label_names: Optional[list[str]] = None,
        state_types: Optional[list[str]] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
        updated_after: Optional[str] = None,
        due_after: Optional[str] = None,
        due_before: Optional[str] = None,
        priority: Optional[str] = None,
        order: str = "updatedAt",
        limit: int = 20,
    ) -> list[dict]:
        """Flexible engine-facing issue list. All filters AND-ed and optional.

        - state_types: subset of Linear workflow types {backlog, unstarted,
          started, completed, canceled, triage}.
        - priority: "urgent"|"high"|"medium"|"low"|"none".
        - due_after/due_before: ISO dates (YYYY-MM-DD) bounding dueDate.
        - order: "updatedAt" (default), "priority" (urgent first), or "dueDate"
          (soonest first) — applied client-side after fetch.

        Returns compact dicts (identifier, title, state, state_type, assignee,
        priority, dueDate, updatedAt, url), [] on error. Read-only, never raises."""
        f: dict = {"team": {"id": {"eq": self._team_id}}}
        if assignee_id:
            f["assignee"] = {"id": {"eq": assignee_id}}
        if state_types:
            f["state"] = {"type": {"in": list(state_types)}}
        if label_names:
            f["labels"] = {"some": {"name": {"in": list(label_names)}}}
        created: dict = {}
        if created_after:
            created["gte"] = created_after
        if created_before:
            created["lte"] = created_before
        if created:
            f["createdAt"] = created
        if updated_after:
            f["updatedAt"] = {"gte": updated_after}
        due: dict = {}
        if due_after:
            due["gte"] = due_after
        if due_before:
            due["lte"] = due_before
        if due:
            f["dueDate"] = due
        if priority:
            pv = _PRIORITY_MAP.get(str(priority).strip().lower())
            if str(priority).strip().lower() == "none":
                pv = 0
            if pv is not None:
                f["priority"] = {"eq": pv}

        # Over-fetch a little so a client-side re-sort (priority/dueDate) still
        # yields the top `limit` rather than the top `limit` by updatedAt.
        fetch = min(100, max(int(limit), 20) * 2)
        log.info(
            "[linear.list_issues_query] step 1/2: filter=%s order=%s limit=%d",
            f, order, limit,
        )
        try:
            data = await self._gql(_LIST_ISSUES, {"filter": f, "first": fetch})
        except Exception:
            log.exception("[linear.list_issues_query] failed; returning []")
            return []
        nodes = (data.get("issues") or {}).get("nodes") or []
        rows = [
            {
                "identifier": d.get("identifier"),
                "title": d.get("title"),
                "state": d.get("state"),
                "state_type": d.get("state_type"),
                "assignee": d.get("assignee"),
                "priority": d.get("priority"),
                "priority_value": d.get("priority_value"),
                "dueDate": d.get("dueDate"),
                "updatedAt": d.get("updated_at"),
                "url": d.get("url"),
            }
            for d in (_issue_node_to_dict(n) for n in nodes if n)
        ]

        if order == "priority":
            rows.sort(key=lambda r: _PRIORITY_SORT_RANK.get(r.get("priority_value"), 5))
        elif order == "dueDate":
            # Soonest first; issues with no due date sort to the end.
            rows.sort(key=lambda r: r.get("dueDate") or "9999-12-31")
        else:
            rows.sort(key=lambda r: r.get("updatedAt") or "", reverse=True)

        out = rows[: max(1, int(limit))]
        log.info("[linear.list_issues_query] step 2/2: %d result(s)", len(out))
        return out

    # -- engine-facing reads: PROJECT-level (tool-driven query mode) ----------

    async def _fetch_team_projects(self, *, limit: int = 50) -> list[dict]:
        """Fetch + normalise the configured team's projects (LIGHT — no milestones;
        see `_TEAM_PROJECTS`). `limit` is capped at 50 to stay under the Linear
        query-complexity ceiling. Raises on transport/GraphQL error — public callers
        wrap and degrade."""
        data = await self._gql(
            _TEAM_PROJECTS, {"teamId": self._team_id, "first": min(int(limit), 50)}
        )
        nodes = ((data.get("team") or {}).get("projects") or {}).get("nodes") or []
        return [_project_node_to_dict(n) for n in nodes if n and n.get("id")]

    async def _fetch_project_detail(self, project_id: str) -> Optional[dict]:
        """Fetch ONE project by id WITH its milestones. Returns the normalised dict or
        None if not found. Raises on transport/GraphQL error — callers wrap."""
        data = await self._gql(_PROJECT_DETAIL, {"id": project_id})
        node = data.get("project")
        return _project_node_to_dict(node) if node else None

    def _match_project(self, name: str, projects: list[dict]) -> dict:
        """Resolve a free-text project `name` to ONE project from `projects`.

        Order: alias map (conventions.PROJECT_ALIASES) → exact name (case-insensitive)
        → substring either direction → token overlap. Returns {"project": <dict>} on a
        single confident match, or {"error", "candidates"} when nothing / several match
        (same shape as resolve_member_id). Never raises."""
        wanted = (name or "").strip().lower()
        if not wanted:
            return {"error": "empty project name", "candidates": []}

        # An alias points the term at a canonical project name; add it as an extra
        # target so the exact/substring passes below can hit it.
        targets = {wanted}
        alias = PROJECT_ALIASES.get(wanted)
        if alias:
            targets.add(alias.strip().lower())

        def _cands(ps: list[dict]) -> list[dict]:
            return [
                {"name": p.get("name"), "target_date": p.get("target_date"),
                 "status": p.get("status")}
                for p in ps
            ]

        # 1) Exact name match (covers the alias → canonical-name case).
        exact = [p for p in projects if (p.get("name") or "").strip().lower() in targets]
        if len(exact) == 1:
            return {"project": exact[0]}
        if len(exact) > 1:
            return {"error": f"'{name}' matches multiple projects", "candidates": _cands(exact)}

        # 2) Substring either direction (e.g. "onboarding" ⊂ "KYC + Aadhaar Onboarding").
        subs = [
            p for p in projects
            if any(
                t in (p.get("name") or "").strip().lower()
                or (p.get("name") or "").strip().lower() in t
                for t in targets
            )
        ]
        if len(subs) == 1:
            return {"project": subs[0]}
        if len(subs) > 1:
            return {"error": f"'{name}' matches multiple projects", "candidates": _cands(subs)}

        # 3) Token overlap — last resort for partial phrasings.
        want_toks = {t for t in re.findall(r"[a-z0-9]+", wanted) if len(t) >= 3}
        toks_hits = []
        for p in projects:
            pn_toks = set(re.findall(r"[a-z0-9]+", (p.get("name") or "").lower()))
            if want_toks & pn_toks:
                toks_hits.append(p)
        if len(toks_hits) == 1:
            return {"project": toks_hits[0]}
        if len(toks_hits) > 1:
            return {"error": f"'{name}' matches multiple projects", "candidates": _cands(toks_hits)}

        return {"error": f"no project matches '{name}'", "candidates": _cands(projects)}

    async def _resolve_project(self, name: str) -> dict:
        """Fetch the team projects and match `name` against them. Returns
        {"project": <dict>} or {"error", "candidates"}. Never raises."""
        try:
            projects = await self._fetch_team_projects()
        except Exception:
            log.exception("[linear._resolve_project] project fetch failed")
            return {"error": "could not load projects", "candidates": []}
        return self._match_project(name, projects)

    async def _project_issue_rows(
        self,
        project_id: str,
        *,
        label_names: Optional[list[str]] = None,
        state_types: Optional[list[str]] = None,
        limit: int = 100,
    ) -> list[dict]:
        """All issues in a project (by id), newest-updated first, as compact rows.
        Optional label / state-type filters are AND-ed. Raises on error; callers wrap."""
        f: dict = {
            "team": {"id": {"eq": self._team_id}},
            "project": {"id": {"eq": project_id}},
        }
        if state_types:
            f["state"] = {"type": {"in": list(state_types)}}
        if label_names:
            f["labels"] = {"some": {"name": {"in": list(label_names)}}}
        data = await self._gql(_PROJECT_ISSUES, {"filter": f, "first": int(limit)})
        nodes = (data.get("issues") or {}).get("nodes") or []
        rows = [_project_issue_node_to_dict(n) for n in nodes if n]
        rows.sort(key=lambda r: r.get("updatedAt") or "", reverse=True)
        return rows

    @staticmethod
    def _rollup_by_milestone(project: dict, issues: list[dict]) -> None:
        """Attach issue_total / issue_completed to each milestone in `project` from
        `issues` (matched by milestone id). Mutates the milestone dicts in place."""
        by_ms: dict = {}
        for it in issues:
            mid = it.get("milestone_id")
            slot = by_ms.setdefault(mid, {"total": 0, "completed": 0})
            slot["total"] += 1
            if it.get("state_type") == "completed":
                slot["completed"] += 1
        for m in project.get("milestones") or []:
            c = by_ms.get(m.get("id"), {"total": 0, "completed": 0})
            m["issue_total"] = c["total"]
            m["issue_completed"] = c["completed"]

    async def list_projects(self) -> list[dict]:
        """READ-ONLY: the team's projects as [{name, target_date, lead, status,
        priority, progress_pct, summary, milestone_count, url}], for resolving a
        spoken feature name ("DMs") to a real project and for a project overview.
        Returns [] on any error. Never raises."""
        log.info("[linear.list_projects] step 1/2: team %s", self._team_id)
        try:
            projects = await self._fetch_team_projects()
        except Exception:
            log.exception("[linear.list_projects] failed; returning []")
            return []
        out = [
            {
                "name": p.get("name"),
                "target_date": p.get("target_date"),
                "lead": p.get("lead"),
                "status": p.get("status"),
                "priority": p.get("priority"),
                "progress_pct": p.get("progress_pct"),
                "summary": p.get("summary"),
                "milestone_count": len(p.get("milestones") or []),
                "url": p.get("url"),
            }
            for p in projects
        ]
        log.info("[linear.list_projects] step 2/2: %d project(s)", len(out))
        return out

    async def get_project(self, name: str) -> dict:
        """READ-ONLY full detail for ONE project resolved from `name` (alias/fuzzy).

        Returns the project meta (name, summary, description, url, start/target dates,
        lead, status, priority, progress, health), its MILESTONES (each with
        target_date and issue_total / issue_completed), and an issue_totals rollup
        (total + counts by state name and by state type) so the engine can lead with
        the launch picture. Returns {"error", "candidates"} when the name doesn't
        resolve to exactly one project. Never raises."""
        log.info("[linear.get_project] step 1/3: resolving %r", name)
        res = await self._resolve_project(name)
        if "project" not in res:
            log.info("[linear.get_project] step 2/3: no single match (%s)", res.get("error"))
            return res
        p = dict(res["project"])

        # The resolve pass uses the LIGHT project query (no milestones). Re-fetch this
        # one project's full detail so milestones are populated; fall back to the light
        # dict if the detail fetch fails.
        try:
            detail = await self._fetch_project_detail(p["id"])
            if detail:
                p = detail
        except Exception:
            log.exception("[linear.get_project] detail fetch failed; using light dict")

        log.info("[linear.get_project] step 2/3: matched %r; loading issues", p.get("name"))
        try:
            issues = await self._project_issue_rows(p["id"])
        except Exception:
            log.exception("[linear.get_project] issue load failed; counts omitted")
            issues = []

        self._rollup_by_milestone(p, issues)
        by_state_name: dict = {}
        by_state_type: dict = {}
        for it in issues:
            sn = it.get("state") or "Unknown"
            st = it.get("state_type") or "unknown"
            by_state_name[sn] = by_state_name.get(sn, 0) + 1
            by_state_type[st] = by_state_type.get(st, 0) + 1
        p["issue_totals"] = {
            "total": len(issues),
            "by_state_name": by_state_name,
            "by_state_type": by_state_type,
        }
        # Drop the id from the public payload — the engine addresses projects by name.
        p.pop("id", None)
        for m in p.get("milestones") or []:
            m.pop("id", None)
        log.info(
            "[linear.get_project] step 3/3: %r — %d issue(s), %d milestone(s)",
            p.get("name"), len(issues), len(p.get("milestones") or []),
        )
        return p

    async def get_project_issues(
        self,
        name: str,
        *,
        label_names: Optional[list[str]] = None,
        state_types: Optional[list[str]] = None,
        limit: int = 100,
    ) -> dict:
        """READ-ONLY: all issues in the project resolved from `name`, each with
        identifier, title, state, labels, assignee, milestone, dueDate, updatedAt
        (+ priority, url). Optional label_names (e.g. ['FE']) and state_types filters
        AND-ed — use them for "any FE/BE work left for X". Returns
        {project, target_date, issues:[...]} or {"error", "candidates"} when the name
        doesn't resolve. Never raises."""
        log.info(
            "[linear.get_project_issues] step 1/2: name=%r labels=%s state_types=%s",
            name, label_names, state_types,
        )
        res = await self._resolve_project(name)
        if "project" not in res:
            return res
        p = res["project"]
        try:
            issues = await self._project_issue_rows(
                p["id"], label_names=label_names, state_types=state_types, limit=limit
            )
        except Exception:
            log.exception("[linear.get_project_issues] issue load failed; returning []")
            issues = []
        # Strip internal milestone_id from the per-issue rows.
        for it in issues:
            it.pop("milestone_id", None)
        log.info("[linear.get_project_issues] step 2/2: %d issue(s)", len(issues))
        return {
            "project": p.get("name"),
            "target_date": p.get("target_date"),
            "issues": issues,
        }

    async def list_milestones(self, name: str) -> dict:
        """READ-ONLY: milestones of the project resolved from `name`, each with
        target_date and completion (issue_total / issue_completed). Returns
        {project, target_date, milestones:[...]} — milestones is [] (with a note) when
        the project has none, which is common. {"error", "candidates"} when the name
        doesn't resolve. Never raises."""
        log.info("[linear.list_milestones] step 1/2: resolving %r", name)
        res = await self._resolve_project(name)
        if "project" not in res:
            return res
        p = dict(res["project"])
        # Light resolve dict has no milestones — re-fetch this project's detail.
        try:
            detail = await self._fetch_project_detail(p["id"])
            if detail:
                p = detail
        except Exception:
            log.exception("[linear.list_milestones] detail fetch failed; using light dict")
        try:
            issues = await self._project_issue_rows(p["id"])
        except Exception:
            log.exception("[linear.list_milestones] issue load failed; counts omitted")
            issues = []
        self._rollup_by_milestone(p, issues)
        milestones = [
            {k: v for k, v in m.items() if k != "id"} for m in p.get("milestones") or []
        ]
        out: dict = {
            "project": p.get("name"),
            "target_date": p.get("target_date"),
            "milestones": milestones,
        }
        if not milestones:
            out["note"] = (
                "This project has no milestones defined — use its target date "
                "for the launch/release date and roll up its issues by state."
            )
        log.info("[linear.list_milestones] step 2/2: %d milestone(s)", len(milestones))
        return out
