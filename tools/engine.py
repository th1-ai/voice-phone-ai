"""tools/engine.py - one call transcript in, one queued callback out.

Deterministic decisioning, LLM for language (ARCHITECTURE.md section 1): the
only two model calls are `classify` and `draft` (always through
`core.llm.complete` with a JSON schema). Everything else - whether a booking
is valid, whether a human must see it before it goes out - is a plain rule
or a lookup, not something the model decides.

Shared by `tools/run.py` (the real loop) and `tools/demo.py` (the
zero-credential walkthrough), so both exercise exactly the same code path.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from core.adapters.base import EmailMessage
from core.config import Settings
from core.llm import LLMResult, LLMSchemaError, complete
from core.store import Item, Store
from core.templates import build_prompt

import store_ext
from booking import BookingOutcome, compute_pending
from pricing import room_type_list

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "prompts" / "schemas"
GUEST_REQUEST_CATEGORIES = ["transport", "housekeeping", "maintenance", "concierge",
                           "dining", "other"]


def _schema(name: str) -> dict:
    return json.loads((SCHEMAS_DIR / f"{name}.json").read_text(encoding="utf-8"))


CLASSIFY_SCHEMA = _schema("classify")
DRAFT_SCHEMA = _schema("draft")

_REF_RE = re.compile(r"\b(RES|TBL|RS|REQ)-\d{3,5}\b", re.I)


def call_to_dict(msg: EmailMessage) -> dict:
    """The fields the prompts and the store need from one call transcript.

    Voice / Phone AI ingests finished call transcripts through
    ``systems.email.adapter`` - see docs/how-it-works.md "What this repo
    actually is". ``msg.from_email``/``msg.from_name`` carry the caller's
    number/name when the mailbox or the fixture provides them; ``msg.body``
    is the transcript itself.
    """
    return {"id": msg.id, "from": msg.from_email, "from_name": msg.from_name,
           "subject": msg.subject, "transcript": msg.body_text,
           "received_at": msg.received_at, "reservation_ref": _find_ref(msg.body_text)}


def _find_ref(text: str) -> str | None:
    """A cheap, deterministic scan for a reservation reference already
    spoken in the call (``RES-1234``) - so a caller naming an existing
    booking can have a PMS note attached without asking the model to copy
    digits (see tools/booking.py:finalize_action)."""
    m = _REF_RE.search(text or "")
    return m.group(0).upper() if m else None


# --------------------------------------------------------------------------
# the two model calls
# --------------------------------------------------------------------------
def run_classify(settings: Settings, store: Store, item: Item, call: dict, *,
                 provider: str | None = None) -> dict:
    restaurant_name = settings.agent_get("restaurant.name", "the restaurant")
    room_types = room_type_list(settings.agent_get("rooms.room_types", {})) or "none configured"
    categories = ", ".join(settings.agent_get("guest_requests.categories",
                                             GUEST_REQUEST_CATEGORIES))
    prompt = build_prompt("classify", settings=settings, item=call, fixture_id=item.external_id,
                          restaurant_name=restaurant_name, room_types=room_types,
                          guest_request_categories=categories)
    result: LLMResult = complete("classify", prompt, CLASSIFY_SCHEMA, settings=settings,
                                 provider=provider, store=store, item_id=item.id,
                                 fixture_id=item.external_id)
    data = result.data or {}
    store.set_fields(item.id, intent=data.get("call_type"),
                     confidence=float(data.get("confidence", 0.0)))
    return data


def run_draft(settings: Settings, store: Store, item: Item, classification: dict,
             pending: BookingOutcome, *, provider: str | None = None) -> dict:
    draft_item = {**classification, "booking_outcome": pending.as_dict()}
    prompt = build_prompt("draft", settings=settings, item=draft_item, fixture_id=item.external_id)
    result: LLMResult = complete("draft", prompt, DRAFT_SCHEMA, settings=settings,
                                 provider=provider, store=store, item_id=item.id,
                                 fixture_id=item.external_id)
    return result.data or {}


# --------------------------------------------------------------------------
# deterministic decisions
# --------------------------------------------------------------------------
def apply_language_gate(classification: dict, settings: Settings) -> dict | None:
    """Force the callback language to the hotel's own default when the
    caller spoke a language this property does not list in
    `hotel.languages` (`config/hotel.yaml`) - nobody on the team could check
    a callback in a language nobody configured, so this always escalates
    instead of letting the draft step answer fluently in a language nobody
    can verify. Mutates `classification["language"]` in place; returns the
    escalation block when it fires, else `None`. See
    docs/how-it-works.md design decision 11.
    """
    lang = str(classification.get("language") or "").strip().lower()
    supported = [str(x).strip().lower() for x in settings.hotel.languages]
    if not lang or lang in supported:
        return None
    reason = f"caller spoke {lang}, not in hotel.languages ({', '.join(supported)})"
    classification["language"] = settings.hotel.default_language
    return {"category": "missing_info", "reason": reason}


def apply_guardrails(classification: dict, pending: BookingOutcome) -> dict | None:
    """Deterministic re-check, on top of whatever the model decided.

    Always escalates a preview that needed a human - an unmatched room
    type, a missing detail, a large party or group - using the SPECIFIC
    reason `tools/booking.py` already computed (`pending.error` when there
    is one) rather than a single canned line, so a reviewer sees the real
    cause instead of a repeated guess.
    """
    if classification.get("escalation"):
        return classification["escalation"]
    if not pending.needs_human:
        return None
    if pending.error:
        return {"category": "missing_info", "reason": pending.error}
    return {"category": "policy_exception",
           "reason": "A large party or group, or an unconfirmed room choice, needs a "
                     "person to arrange - see knowledge/policies.md."}


def needs_human_for(classification: dict, pending: BookingOutcome, draft_data: dict,
                    settings: Settings) -> bool:
    """Plain rule, not a model decision - see docs/safety.md.

    True when the draft step itself says so, when classify escalated, below
    `confidence_threshold`, when a detail is missing, or when
    `tools/booking.py` itself flagged the preview
    (`pending.needs_human` - already correct per call_type: a sold-out room
    or a closed restaurant day is `needs_human=False` because the
    alternative reply is routine, while a missing date or an unknown room
    type is `needs_human=True`).
    """
    threshold = float(settings.agent_get("confidence_threshold", 0.75))
    if bool(draft_data.get("needs_human")):
        return True
    if classification.get("escalation"):
        return True
    if float(classification.get("confidence", 0.0)) < threshold:
        return True
    if classification.get("missing_info"):
        return True
    if pending.needs_human:
        return True
    return False


# --------------------------------------------------------------------------
# the whole pass for one call
# --------------------------------------------------------------------------
def process_call(settings: Settings, store: Store, pms, msg: EmailMessage, *,
                 provider: str | None = None) -> tuple[Item, bool]:
    """Classify, preview (never writes), draft and queue one call transcript.

    Idempotent: an item that already has both an intent AND a draft was
    handled by an earlier pass and is left untouched (returns ``(item,
    False)``). Checking intent alone is not enough - with
    ``llm.provider: interactive`` the classify call can succeed and set
    ``intent`` on one run, then the draft call pends on the very next line
    waiting for a second answer; without also checking ``draft``, a later
    run would see the intent already set and skip straight past the draft
    step forever, leaving the item stuck at ``new`` with no way to reach the
    review queue.

    The full classify result is cached on
    ``item.payload["_classify_cache"]`` the first time it succeeds, so that
    second round trip does not have to ask classify all over again - it
    resumes straight into the preview and draft with the same result. A
    schema error from either model call queues the item as ``needs_human``
    with the error recorded rather than guessing or crashing the batch.
    """
    external_id = str(msg.id)
    existing = store.get_by_external("call", external_id)
    fresh_payload = call_to_dict(msg)
    if existing is not None and "_classify_cache" in (existing.payload or {}):
        fresh_payload["_classify_cache"] = existing.payload["_classify_cache"]
    item = store.upsert_item("call", external_id, kind="call", payload=fresh_payload)
    if item.intent and item.draft is not None:
        return item, False

    cached = (item.payload or {}).get("_classify_cache")
    if cached is not None:
        classification = cached
    else:
        try:
            classification = run_classify(settings, store, item, fresh_payload,
                                          provider=provider)
        except LLMSchemaError as exc:
            store.set_fields(item.id, error=str(exc))
            updated = store.transition(item.id, "needs_human", actor="agent",
                                       detail={"error": "classify_schema_error"})
            return updated, True
        item = store.set_fields(
            item.id, payload={**item.payload, "_classify_cache": classification}) or item

    if classification.get("call_type") == "no_action":
        # A wrong number, dead air or spam needs no callback at all - skip
        # it outright rather than draft a reply to nobody. See
        # docs/how-it-works.md, the call_type table.
        updated = store.transition(item.id, "skipped", actor="agent",
                                   detail={"reason": classification.get("reason", "")})
        return updated, True

    language_escalation = apply_language_gate(classification, settings)
    pending = compute_pending(settings, pms, store, classification)
    escalation = apply_guardrails(classification, pending) or language_escalation
    if escalation:
        classification["escalation"] = escalation

    try:
        draft_data = run_draft(settings, store, item, classification, pending,
                               provider=provider)
    except LLMSchemaError as exc:
        store.set_fields(item.id, error=str(exc))
        updated = store.transition(item.id, "needs_human", actor="agent",
                                   detail={"error": "draft_schema_error",
                                          "call_type": classification.get("call_type")})
        return updated, True

    reservation_ref = classification.get("reservation_ref") or fresh_payload.get("reservation_ref")
    store.set_fields(item.id, draft={**draft_data, "channel": _pick_channel(classification),
                                     "caller": classification.get("caller") or {},
                                     "reservation_ref": reservation_ref,
                                     "pending_booking": pending.as_dict()})

    needs_human = needs_human_for(classification, pending, draft_data, settings)
    status = "needs_human" if needs_human else "pending_review"
    updated = store.transition(item.id, status, actor="agent",
                               detail={"call_type": classification.get("call_type")})
    return updated, True


def _pick_channel(classification: dict) -> str:
    """Which real channel the callback goes out on - see
    docs/how-it-works.md design decision 8. Email wins when the caller left
    an address (no opt-in ambiguity); otherwise WhatsApp/chat when a phone
    number was captured; otherwise there is no automated channel and the
    item stays with staff to call back by hand. Reads the caller details
    ``classify`` extracted from the transcript, never the mailbox message's
    own From header - that address is the voicemail system's, not the
    caller's."""
    caller = classification.get("caller") or {}
    if caller.get("email"):
        return "email"
    if caller.get("phone"):
        return "whatsapp"
    return "none"
