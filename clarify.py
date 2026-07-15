"""Clarifying questions — asking instead of guessing.

A Chief of Staff who gets a half-formed report ("login is broken") doesn't file a
half-complete ticket and doesn't invent the missing half. She asks ONE question
and files it properly once she has the answer.

This module holds the GATE and the FALLBACK text for that behaviour; the judgement
of whether something is genuinely missing (and what to ask) is the model's, in
`Classifier.assess_clarification`. The flow it plugs into lives in bot.py:

  report → classify → plan → [gap? → ask ONE question, PARK the plan]
                                 ↳ answered → re-plan with the answer → approval embed
                                 ↳ timed out → propose the PARKED plan anyway

The plan is only ever parked, never executed from here: this path can DELAY a
proposal and enrich it, but it can never file, comment on, or transition anything
on its own. A timeout proposes the original plan rather than dropping it, so a
report can never be lost by going unanswered.

What counts as missing (per the team's conventions):
  - no clear owner — nobody is named and the area doesn't imply one,
  - a bug with no reproduction path, or no expected-vs-actual,
  - scope so vague the ticket would be unactionable ("the dashboard is weird").
"""
import logging

log = logging.getLogger(__name__)

# Only a NEW issue is ever worth pausing on. A comment/transition onto an issue
# that already exists carries its own context — the ticket is already filed, and
# holding the update back to ask a question would just delay information the
# issue's watchers want now.
CLARIFIABLE_KINDS = {"create", "create_needs_triage"}


def is_clarifiable(plan: dict) -> bool:
    """Whether this plan is one we may pause to ask about (a create, not a comment)."""
    return bool(plan) and plan.get("kind") in CLARIFIABLE_KINDS


def missing_owner(plan: dict) -> bool:
    """No assignee resolved AND nobody named — the ticket would land on nobody.
    Used only to enrich the model's prompt, not to force a question: `needs_triage`
    items are deliberately unassigned by convention, and that is not a gap."""
    return not plan.get("assignee_id") and not plan.get("assignee_display")


def fallback_question(plan: dict) -> str:
    """Deterministic one-liner used only if the model can't phrase the question.
    Still ONE question, still specific to the biggest gap we can see without it."""
    title = (plan.get("title") or "this").strip()
    if plan.get("category") == "bug":
        return (
            f"Quick one on \"{title}\" — what are the steps to hit it, and what "
            f"did you expect to happen instead?"
        )
    if missing_owner(plan):
        return f"Quick one on \"{title}\" — who should own this?"
    return f"Quick one on \"{title}\" — can you give me a bit more on the scope?"
