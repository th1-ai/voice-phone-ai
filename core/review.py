"""core.review — the review queue and the single write guard.

Two jobs:

**1. The queue.** ``list_queue`` / ``show`` / ``approve`` / ``edit`` / ``reject``
/ ``mark_sent`` are the operations behind ``tools/review.py``. They are the only
code allowed to write ``approved``, ``edited`` and ``rejected``.

**2. The guard.** :func:`assert_write_allowed` is called by every adapter write
method (through the ``@guarded_write`` decorator in ``core/adapters/base.py``).
It is the one place that decides whether anything may leave the building.

The rules, in order:

1. ``--dry-run`` blocks everything. A rehearsal never writes.
2. ``mode: shadow`` is a **global kill-switch**. It beats any per-item or
   per-agent autonomy setting. The only way past it is an item a human has
   explicitly moved to ``approved`` or ``edited``.
3. In ``mode: live``, an action listed in ``review.require_approval_for`` still
   needs an item in ``approved`` / ``edited``. Actions not on that list run
   straight through.
4. An item already ``sent`` / ``auto_sent`` is never written twice.

That is the whole safety model. Everything else in a template is a convenience
on top of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.config import Settings
from core.store import Item, Store

#: statuses that mean "a human said yes to this exact draft".
#: "sending" is included because `Store.claim_for_send()` atomically flips
#: approved/edited -> sending and returns the item in that state — the actual
#: write (adapter.send(item=...)) always happens on a "sending" item, never on
#: one still sitting at "approved"/"edited". Without "sending" here, no send
#: could ever pass the guard: the FSM only allows "sending" to be reached from
#: "approved" or "edited" (core/store.py TRANSITIONS), so this never lets an
#: unapproved item through.
APPROVED_STATES = frozenset({"approved", "edited", "sending"})
#: Actions that move money. A human approval is required in every mode, and no
#: config key can lift that - a hotel can add gates, never remove this one.
ALWAYS_HUMAN_ACTIONS = frozenset({"payment", "refund", "payment_batch", "payout"})
#: statuses a human may act on from the queue
ACTIONABLE_STATES = frozenset({"pending_review", "needs_human", "stale", "failed"})


class WriteBlocked(PermissionError):
    """Raised instead of performing a write the current mode does not allow.

    The message is meant to be shown to the hotel as-is: it says what was
    blocked, why, and what to do about it.
    """

    def __init__(self, action: str, reason: str, hint: str = "") -> None:
        message = f"blocked: {action} — {reason}"
        if hint:
            message += f"\n  -> {hint}"
        super().__init__(message)
        self.action, self.reason, self.hint = action, reason, hint


@dataclass
class Decision:
    """Result of a guard evaluation. ``allowed`` False carries the reason."""

    allowed: bool
    reason: str = ""
    hint: str = ""


# --------------------------------------------------------------------------
# the guard
# --------------------------------------------------------------------------
def evaluate_write(settings: Settings, action: str, item: Item | None = None) -> Decision:
    """Decide whether ``action`` may run now. Pure function, easy to test."""
    if settings.dry_run:
        return Decision(False, "--dry-run is on, nothing is written",
                        "Drop --dry-run when you want the action to happen.")

    if item is not None and item.review_status in ("sent", "auto_sent"):
        return Decision(False, f"item {item.id} is already {item.review_status}",
                        "Re-sending would duplicate the message. Nothing to do.")

    approved = item is not None and item.review_status in APPROVED_STATES

    if settings.mode == "shadow":
        if approved:
            return Decision(
                False,
                "mode is shadow: the approval is recorded, but nothing leaves in shadow mode",
                "Set mode: live in config/hotel.yaml when you trust the drafts "
                "(workflows/90-go-live.md). Approvals made in shadow are cleared at "
                "go-live with `tools/review.py stale`, so nothing old goes out by surprise.")
        return Decision(
            False,
            "mode is shadow (the global kill-switch), so nothing is sent or written",
            "Review the draft with `make review`; your approve / edit / reject decisions "
            "are recorded and teach the agent. Set mode: live in config/hotel.yaml once "
            "you trust the drafts.")

    # live mode
    if action in ALWAYS_HUMAN_ACTIONS and not approved:
        status = item.review_status if item else "no item"
        return Decision(
            False,
            f"'{action}' always needs a human: money never moves unattended "
            f"(this item is '{status}')",
            "Approve it in the review queue. This gate cannot be removed by config.")
    if action in settings.review.require_approval_for and not approved:
        status = item.review_status if item else "no item"
        return Decision(
            False,
            f"'{action}' needs approval and this item is '{status}'",
            "Approve it in the review queue, or remove the action from "
            "review.require_approval_for in config/hotel.yaml.")

    return Decision(True, "live mode, action permitted")


def assert_write_allowed(settings: Settings, action: str, item: Item | None = None) -> None:
    """Raise :class:`WriteBlocked` unless ``action`` may run. Called by every write."""
    decision = evaluate_write(settings, action, item)
    if not decision.allowed:
        raise WriteBlocked(action, decision.reason, decision.hint)


def resolve_autonomy(settings: Settings, agent_default: str = "draft",
                     item_override: str | None = None,
                     gates: list[str] | None = None) -> str:
    """Return ``"draft"`` or ``"send"`` for one item. A gate always beats send.

    ``agent_default`` comes from ``config/agent.yaml`` (``autonomy: draft|send``).
    ``item_override`` is a per-item decision the agent made (low confidence, VIP,
    complaint...). ``gates`` is the list of guardrails that fired for this item.

    Any single "draft" wins, and ``mode: shadow`` forces ``draft`` regardless.
    There is no combination of settings that turns a fired gate into a send.
    """
    if settings.mode != "live" or settings.dry_run:
        return "draft"
    if gates:
        return "draft"
    for value in (item_override, agent_default):
        if value is None:
            continue
        if str(value).lower() not in ("send", "auto", "auto_send"):
            return "draft"
    return "send"


# --------------------------------------------------------------------------
# queue operations
# --------------------------------------------------------------------------
def list_queue(store: Store, *, status: str | None = None, kind: str | None = None,
               limit: int = 50) -> list[Item]:
    """Items waiting on a human, oldest first."""
    statuses = [status] if status else sorted(ACTIONABLE_STATES)
    return store.list_items(status=statuses, kind=kind, limit=limit)


def show(store: Store, item_id: str) -> dict:
    """Full detail for one item: fields, draft and audit trail."""
    item = store.get_item(item_id)
    if item is None:
        raise KeyError(f"no item {item_id}")
    return {"item": item.as_dict(), "events": store.list_events(item_id)}


def approve(store: Store, item_id: str, actor: str = "human", note: str = "") -> Item:
    """Human approves the draft unchanged -> queued for sending."""
    return store.transition(item_id, "approved", actor, {"note": note} if note else None)


def edit(store: Store, item_id: str, new_draft: dict, actor: str = "human",
         note: str = "") -> Item:
    """Human rewrote the draft -> queued for sending, and the edit is recorded.

    The before/after pair goes into ``learnings`` so the weekly coach pass can
    see what the agent keeps getting wrong.
    """
    item = store.get_item(item_id)
    if item is None:
        raise KeyError(f"no item {item_id}")
    before = (item.draft or {}).get("body", "") if isinstance(item.draft, dict) else ""
    after = new_draft.get("body", "") if isinstance(new_draft, dict) else str(new_draft)
    store.set_fields(item_id, draft=new_draft)
    updated = store.transition(item_id, "edited", actor, {"note": note} if note else None)
    if before != after:
        store.record_learning(source_item=item_id, before=before, after=after,
                              lesson=note or "human edited the draft before sending",
                              applied_to=item.intent or item.kind)
    return updated


def reject(store: Store, item_id: str, reason: str = "", actor: str = "human") -> Item:
    """Human discards the draft. Terminal — the agent will not retry."""
    item = store.get_item(item_id)
    if item is not None and item.draft:
        before = item.draft.get("body", "") if isinstance(item.draft, dict) else ""
        store.record_learning(source_item=item_id, before=before, after="",
                              lesson=reason or "human rejected the draft",
                              applied_to=item.intent or item.kind)
    return store.transition(item_id, "rejected", actor, {"reason": reason} if reason else None)


def retry(store: Store, item_id: str, actor: str = "human") -> Item:
    """Re-queue a failed send. Only a human may do this."""
    return store.transition(item_id, "approved", actor, {"retry": True})


def mark_sent(store: Store, item_id: str, message_id: str | None = None) -> Item:
    """Record the provider message id and close the item out."""
    return store.mark_sent(item_id, message_id)


def stale_backlog(store: Store, actor: str = "human", reason: str = "go-live") -> list[str]:
    """Move every un-sent review row (waiting, approved or edited) to ``stale``.

    Run once when flipping shadow -> live: the queue built up during shadow was
    never sent and is out of date. A human can revive one item with
    ``retry``-style transitions (stale -> pending_review) if it still matters.
    """
    return store.mark_stale(0, statuses=("pending_review", "needs_human", "approved", "edited"),
                            actor=actor, reason=reason)


def queue_summary(store: Store) -> dict[str, Any]:
    """Counts for the digest and for ``make doctor``."""
    counts = store.counts()
    waiting = sum(counts.get(s, 0) for s in ACTIONABLE_STATES)
    return {"by_status": counts, "waiting_on_human": waiting,
            "in_send_queue": sum(counts.get(s, 0) for s in ("approved", "edited", "sending")),
            "sent": counts.get("sent", 0) + counts.get("auto_sent", 0)}
