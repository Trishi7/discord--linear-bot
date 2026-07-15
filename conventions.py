"""NFThing / Membrane team conventions, injected into the classifier and query
engine prompts and reconciled against the code (labels, status, assignment).

Single source of truth: `TEAM_CONVENTIONS`. Edit the playbook here; the prompts
pick it up automatically.
"""

TEAM_CONVENTIONS = """NFThing / Membrane team conventions (from the PM Operating Playbook v3, reconciled with
live Linear). The bot's job exists because of one rule: anything important said in Discord
must become a Linear issue the same day.

ROSTER
- Sid — CEO; decisions/finance; available 4pm IST.
- Arun — UI/UX designer (Figma). Owns design + missing-state fixes.
- Ananda — SOLE frontend dev and the main bottleneck (max 2 active issues at once).
- 3 backend devs; 1 ML engineer.
- Harsh — QA; new; follows PM-written test steps and documents what he sees; does NOT
  classify bug types and is NOT the fixer.
- Trishi — PM (routes and classifies).

Devs seen on tickets: samyuktha, shriraksha (Raksha), ravi, shreyansh, ananda, shashi,
arun, harsh.

CATEGORY
- feature = a NEW screen/flow/capability that does not exist yet; needs new endpoints or a
  design sprint; driven by a business/PRD/advertiser decision.
- improvement = a change to something that ALREADY exists; uses existing APIs or minor
  additions; triggered by analytics, user feedback, or a bug.
- bug = a defect / broken / not behaving as expected.
- noise = chit-chat, acks, questions, plain status -> do nothing.
Decision test: if it needs a new Linear Project it's a feature; if it fits as a change to
existing work it's an improvement.

LABELS — ONLY these six exist in Linear; never invent others:
  Bug, Feature, Improvement, UI, FE, BE.
- The playbook's [FrontEnd]/[Backend]/[ML]/[Testing] do NOT exist. Map FrontEnd->FE,
  Backend->BE; there is no ML or Testing label (skip).
- A Bug ALWAYS pairs a system label showing where the fix lives:
    logic/API error -> Bug + BE
    wrong FE behaviour or cosmetic (spacing/colour/font/layout) -> Bug + FE
    a state never designed / edge case missing from design -> Bug + UI (design changes first)

PRIORITY (severity -> Linear priority)
- P0 Blocking: crash, user can't complete a flow, data loss, payment/withdrawal failure
  -> Urgent.
- P1 Major: core flow broken but a workaround exists -> High.
- P2 Minor: visual glitch, copy error, non-blocking edge case -> Low (Medium if unsure).

STATUS — be conservative. Flow: Backlog -> Todo -> In Progress -> Implemented -> In Review
-> Done | Canceled.
- "Implemented" = dev-complete, deployable to the FKTR test env, NOT yet tested.
- Done requires Harsh's WRITTEN QA sign-off — a human gate. THE BOT MUST NEVER SET DONE OR
  RELEASED.
- Resolution signal from Discord ("fixed", "deployed", "done"): move at most to
  "Implemented", and prefer to just comment and leave the status change to the PM. Never Done.
- "on it / WIP / looking into it" -> "In Progress". "can't reproduce / not a bug" ->
  comment only (Canceled is a PM decision that needs a reason).
- In Progress, Implemented, and In Review are ALL "started" type here, so status moves must
  match by state NAME, not type.

ASSIGNMENT
- First @-mentioned person is the assignee (resolve via DISCORD_LINEAR_MAP); list the rest
  in the description; no mention -> unassigned for PM triage.
- NEVER assign a bug to Harsh. Missing-state/design bugs go to Arun first, then Ananda.

DESCRIPTION (bug = the dominant case)
- Sections: What was reported (reporter's words — don't paraphrase the meaning away) /
  Steps to reproduce (if given) / Expected vs Actual / Screenshot & recording links (every
  image and video attachment URL) / "Raised by @author" / a "Needs triage" note if unsure.
- feature/improvement from Discord: What's being asked / Why (if stated) / links /
  "Raised by @author". Keep it short — the PM expands it into the full project template later.

DEDUP / ITERATIONS
- Always search existing issues before creating. An improvement to existing work is usually
  a CHILD of the existing item, not a new standalone ticket — prefer commenting on / relating
  to the existing issue over creating a duplicate.

TITLE STYLE (from real done tickets) — short, specific, "<Area/Feature> — <what>":
  "DMs 1:1 — FE (media, delete, reply, reactions)"
  "Pulse — reporting feature"
  "Birthday post — pin to top of the seen-posts section"
"""


# Project-name aliases for query mode: the colloquial term the team uses in Discord
# ("DMs", "messaging") -> the CANONICAL Linear project name. Used by the query engine's
# project tools (list_projects/get_project/...) to resolve a spoken feature name to a
# real project when a plain fuzzy match wouldn't get there (e.g. "DMs" would never
# substring-match "DMs & Group Chat (v1)" on its own).
#
# Keys are lower-cased; the resolver lower-cases the user's term before looking it up.
# Values are the canonical project name AS IT APPEARS IN LINEAR (also matched
# case-insensitively) — keep them in sync with the live project list. Fuzzy/substring
# matching still runs for terms NOT in this map, so this only needs the awkward cases.
PROJECT_ALIASES = {
    # DMs & Group Chat (v1) — the launch everyone refers to by shorthand.
    "dms": "DMs & Group Chat (v1)",
    "dm": "DMs & Group Chat (v1)",
    "direct message": "DMs & Group Chat (v1)",
    "direct messages": "DMs & Group Chat (v1)",
    "direct messaging": "DMs & Group Chat (v1)",
    "messaging": "DMs & Group Chat (v1)",
    "messages": "DMs & Group Chat (v1)",
    "group chat": "DMs & Group Chat (v1)",
    "group chats": "DMs & Group Chat (v1)",
    "chat": "DMs & Group Chat (v1)",
    # Other frequently-shorthanded projects.
    "kyc": "KYC + Aadhaar Onboarding",
    "aadhaar": "KYC + Aadhaar Onboarding",
    "wallet": "Stripe Wallet + Campaign Controls",
    "stripe": "Stripe Wallet + Campaign Controls",
    "pulse": "Pulse — V2",
    "retargeting": "Retargeting in Create Campaigns",
    "attention score": "Attention Score Notification",
}

