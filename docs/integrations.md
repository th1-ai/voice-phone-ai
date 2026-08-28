# Connecting your systems

Every connector in this repo is one of three things, and the table says which.
We will not tell you an integration exists when it does not.

| Badge | Means |
|---|---|
| **built** | Written against the real API and tested against it. |
| **universal** | Works with any system through a common protocol: IMAP/SMTP, CSV, a webhook. |
| **stub** | Interface only. Calling it raises a clear error with a recipe for adding it. |
| **not built** | Genuinely absent - no interface exists in this family's shared `core/` at all. |

Check what is actually working on your machine at any time:

```bash
make doctor
```

## Telephony - the honest gap

**There is no phone line in this repo, and no adapter for one.** Read
`docs/how-it-works.md` ("What this repo actually is") before anything else
here - it explains exactly what this agent does instead (processes finished
call transcripts) and why.

To answer a live call for real you need a telephony/voice provider - Retell
AI, Twilio, Telnyx, ElevenLabs realtime, or Vonage are the common choices -
wired to post the finished transcript to the mailbox `systems.email.adapter`
reads (or to call `tools/engine.py:process_call` directly from your own
webhook handler once a call ends). None of this family's shared `core/`
covers that transport; it is genuinely outside what any of the 28 templates
in this family builds.

## Status

### Call transcript source - `systems.email.adapter`

<a id="email"></a>

This is the channel Voice / Phone AI actually ingests calls through - see
`docs/how-it-works.md`. Point it at a mailbox that receives call-transcript
emails from your voicemail-to-text service or your call-recording/
transcription add-on (many small-hotel phone systems already offer this),
or at an inbox a staff member forwards a written-up call summary to.

| Adapter | Status | Needs | Notes |
|---|---|---|---|
| `mock` | universal | nothing | Reads `fixtures/inbound/*.json`. What `make demo` uses. |
| `imap` | universal | mailbox + app password | Any provider. **Start here.** |
| `gmail` | built | Google OAuth desktop client | Adds Gmail labels and threads. |

**`imap`.** In `.env`:

```
EMAIL_ADDRESS=reservations@example.com
EMAIL_PASSWORD=            # an APP password, never your login password
IMAP_HOST=imap.example.com
SMTP_HOST=smtp.example.com
SMTP_PORT=587
```

**`gmail`.** Google Cloud Console: enable the Gmail API, configure the
consent screen, create an OAuth client of type **Desktop app**, download the
JSON to `credentials.json`. Then
`pip install google-api-python-client google-auth-oauthlib` and run
`make doctor`; a browser opens once and writes `token.json`.

This same channel also sends the guest-facing callback when a person
approves an item and the caller left an email address - see
`tools/review.py send`.

**Testing with your own sample calls, before `mock` or a real mailbox.** Set
`systems.email.fixtures_dir` in `config/hotel.yaml` to point the `mock`
adapter at a folder of your own `.json` or `.eml` sample call transcripts
(same shape as `fixtures/inbound/`) instead of the shipped fixtures:

```yaml
systems:
  email:
    adapter: mock
    fixtures_dir: data/my-test-calls    # instead of fixtures/inbound/
```

This lets you rehearse the agent against your own recordings without
touching `fixtures/inbound/` - editing that folder would change what
`make demo`'s documented "12 items processed" output shows. `make doctor`'s
"call transcript source" line names whichever folder is actually being
read.

### PMS - `systems.pms.adapter` (reads only)

<a id="pms"></a>

Voice / Phone AI never writes to your PMS directly - see
`docs/how-it-works.md` design decision 2. It reads `list_reservations()` for
a genuine availability check (`tools/booking.py:preview_room`) and, when a
caller names an existing reservation, appends a note with `add_note()`.

| Adapter | Status | Needs | Notes |
|---|---|---|---|
| `mock` | universal | nothing | Reads `fixtures/hotel/reservations.json`. What `make demo` uses. |
| `csv` | universal | a CSV export | Reads `data/imports/reservations.csv`. Works with every PMS. |
| `cloudbeds` | built | OAuth app + refresh token | Live reads. |
| `cli` | universal | a JSON-speaking CLI | Advanced. Bridges to a vendor command line tool. |

**`csv` - the one that always works.** Export from your PMS and drop
`reservations.csv` in `data/imports/`:
`id, status, check_in, check_out, room_type_id, room_type_name, room_id,
adults, children, source, total, balance, currency, guest_email,
guest_first_name, guest_last_name, guest_phone, guest_country`. Headers are
matched loosely and dates must be `YYYY-MM-DD`.

**`cloudbeds`.** Create an app in the Cloudbeds developer portal, authorise
it once against your property, and put the result in `.env`:

```
CLOUDBEDS_CLIENT_ID=
CLOUDBEDS_CLIENT_SECRET=
CLOUDBEDS_REFRESH_TOKEN=
CLOUDBEDS_PROPERTY_ID=
```

Scopes: `read:reservation`, `read:guest`. `write:reservation` only if you
want `add_note()` to reach a real reservation.

### Messaging - `systems.messaging.adapter` (callback delivery + urgent staff alerts)

<a id="messaging"></a>

Used two ways: to send a caller their callback when they left a phone number
but no email, and to alert staff immediately when an urgent guest request
(`config/agent.yaml: guest_requests.urgent_categories`) is approved and sent.

| Adapter | Status | Needs | Notes |
|---|---|---|---|
| `mock` | universal | nothing | Logs to `data/exports/sent_messages.jsonl`. What `make demo` runs against, always blocked by shadow mode. |
| `unipile` | built | your own UniPile account | WhatsApp on your own number. |
| `webhook` | universal | any URL | POST to Zapier, Make, n8n, or your own endpoint. |

**`unipile`.** You create the account, you connect your number by QR code,
you own the credentials: `UNIPILE_DSN`, `UNIPILE_API_KEY`,
`UNIPILE_ACCOUNT_ID`. WhatsApp Business policy limits what you may send
outside a guest-initiated window - a caller who rang your hotel line has not
necessarily opted into WhatsApp; read your provider's rules before turning
this on for callbacks, and consider `webhook` (below) into a real SMS
provider instead if that matters for your property.

**`webhook`.** Set `MESSAGING_WEBHOOK_URL` and the agent POSTs
`{chat_id, text, kind, hotel, sent_at}`. This is also the honest route to a
dedicated SMS provider - **there is no SMS adapter in this family's shared
`core/`** (see `docs/how-it-works.md` design decision 8): bridge through
Zapier/Make/n8n to Twilio or Telnyx, or write your own `messaging_*.py`
adapter using the recipe below.

### Sheets - `systems.sheets.adapter`

<a id="sheets"></a>

Not used by this agent's own code. Available if you want to export
`make report --json` somewhere other than the terminal.

| Adapter | Status | Needs |
|---|---|---|
| `csv` | universal | nothing - writes `data/exports/*.csv` |
| `google` | built | service account JSON |

### Everything else

`pos`, `accounting`, `reviews`, `calendar`, `payments`, `procurement`,
`locks` and `courier` are **stubs** in `core/adapters/` - Voice / Phone AI
does not use any of them. Telephony itself (see above) is not even a stub -
there is no interface for it anywhere in this family.

## Implement your own

<a id="implement-your-own"></a>

The interface is small on purpose, and your Claude Code session can do this
with you in an afternoon. Open `claude` in this folder and paste:

> Read `docs/integrations.md#implement-your-own` and `core/adapters/base.py`.
> I need a Messaging adapter that sends SMS through **<your provider>**. Its
> API docs are at **<url>** and I have credentials in `.env` as
> **<VAR names>**. Copy `core/adapters/messaging_webhook.py` as the shape,
> implement `ping`, `capabilities`, `send` and `notify_staff` with
> `@guarded_write("send_message")`, register it in
> `core/adapters/__init__.py`, and stop so I can check it with `make doctor`.

### The five steps

**1. Copy the closest existing adapter.** `core/adapters/pms_csv.py` for a
PMS, `email_imap.py` for a mailbox, `messaging_webhook.py` for a chat/SMS
channel.

**2. Implement `ping()` and `capabilities()` first.**

```python
def ping(self) -> HealthCheck:
    """Never raises. Returns ok=False with a fix_hint a hotel can act on."""

def capabilities(self) -> set[str]:
    """The method names that actually do something on this adapter."""
```

`make doctor` reads both.

**3. Implement the reads.** Map the vendor's fields onto the dataclasses in
`core/adapters/base.py`. Put anything you do not map into `.extra` rather
than dropping it. Dates are ISO `YYYY-MM-DD`.

**4. Implement the writes, each with the guard.**

```python
from core.adapters.base import guarded_write

@guarded_write("send_message")
def send(self, chat_id: str, text: str) -> dict:
    ...
```

The decorator is not optional. Without it your adapter can write while the
agent is in shadow mode, which defeats the entire safety model.

**5. Register it.** One line in `core/adapters/__init__.py`:

```python
REGISTRY["messaging"]["yourprovider"] = "core.adapters.messaging_yourprovider:YourProviderMessaging"
```

Then set `systems.messaging.adapter: yourprovider` in `config/hotel.yaml`
and run `make doctor`.

### Rules that matter

- **`ping()` never raises.** It returns `HealthCheck(ok=False, ...)` with a
  hint. A broken adapter must still produce a readable doctor table.
- **Every write is decorated.** No exceptions.
- **Never log a credential.** `core/log.py` masks anything whose key looks
  like a secret, but do not rely on it.
- **Redact on ingestion.** Any caller-written text goes through
  `core.redact.redact()` before it is stored or shown to a model - this
  already happens for every transcript, since ingestion is
  `systems.email.adapter`.
- **Write a test.** Copy `tests/test_core_adapters_mock_csv.py`. It should
  run with no network: feed your parser a fixture, check the dataclass that
  comes out.

### `core/` is shared

`core/` is identical in all 28 agents in this family. If you change
something in `core/`, keep it generic - a hotel-specific tweak belongs in
`tools/` or in your own adapter file, not in the shared runtime.
