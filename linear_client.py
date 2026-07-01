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
from typing import Optional

import httpx

log = logging.getLogger(__name__)


LINEAR_API_URL = "https://api.linear.app/graphql"

# Linear priority: 0=no priority, 1=urgent, 2=high, 3=medium, 4=low.
_PRIORITY_MAP = {"urgent": 1, "high": 2, "medium": 3, "low": 4}

# Closed set of label names the bot is allowed to apply.
_ALLOWED_LABEL_NAMES = {"BE", "FE", "Feature", "Bug", "Improvement", "UI"}

# Classifier status_signal → Linear workflow state TYPE.
_SIGNAL_TO_STATE_TYPE = {
    "resolved": "completed",
    "in_progress": "started",
    "cannot_reproduce": "canceled",
}


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
"""

_LIST_ISSUES = """
query ListIssues($filter: IssueFilter!, $first: Int!) {
  issues(filter: $filter, first: $first, orderBy: updatedAt) {
    nodes {
      id
      identifier
      title
      url
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
        "created_at": node.get("createdAt"),
        "updated_at": node.get("updatedAt"),
        "latest_comment": latest_comment,
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
        """Move `issue_id` into the team's first workflow state of the type implied
        by `signal`:

            resolved          → first "completed" state
            in_progress       → first "started"   state
            cannot_reproduce  → first "canceled"  state
            anything else     → no-op

        Returns the updated issue dict, or None when there was nothing to do
        (unrecognised signal, no state of the target type on the team, or an
        API failure). Logs and swallows errors — never raises.
        """
        log.info("[linear.set_issue_status] step 1/3: issue=%s signal=%s", issue_id, signal)
        state_type = _SIGNAL_TO_STATE_TYPE.get(signal)
        if state_type is None:
            log.info(
                "[linear.set_issue_status] signal %r has no state-type mapping; no-op",
                signal,
            )
            return None

        log.info(
            "[linear.set_issue_status] step 2/3: locating first state of type %r",
            state_type,
        )
        try:
            states = await self.list_team_states()
        except Exception:
            log.exception("[linear.set_issue_status] could not load team states; no-op")
            return None
        target = next((s for s in states if s.get("type") == state_type), None)
        if target is None:
            log.warning(
                "[linear.set_issue_status] team %s has no state of type %r; no-op",
                self._team_id,
                state_type,
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
