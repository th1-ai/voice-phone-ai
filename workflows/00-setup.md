# Workflow: first-run setup

Objective: get Voice / Phone AI from a fresh clone to a working demo, then to
real config, in one sitting.

## Steps

1. **Install and check.**
   ```bash
   make setup
   make doctor
   ```
   `make setup` creates the virtualenv, installs `requirements.txt`, and
   copies `.env.example` -> `.env` and every `config/*.example.yaml` ->
   `config/*.yaml` (only if those files do not exist yet - it never
   overwrites your own copies). `make doctor` will show a `FAIL` on "hotel
   identity" right after setup - that is expected, it means the property
   name is still the shipped placeholder. Everything else should be `ok` or
   `warn`, including a `warn` on "call transcript source" (it is reading
   `fixtures/inbound/` only until you connect a real mailbox in step 5).

2. **Run the demo.** No credentials needed.
   ```bash
   make demo
   ```
   Expect to see 12 sample call transcripts classified and most of them
   drafted into callbacks, and the line
   `DEMO OK - 12 items processed, 11 drafted, 0 sent (shadow)`. If you do
   not see that, stop and read `workflows/99-troubleshooting.md` before
   going further.

3. **Read `docs/how-it-works.md` first.** Before filling anything in,
   understand what this agent actually does - it has no live telephony, it
   processes finished call transcripts. That shapes everything else below.

4. **Fill in the property.** Edit `config/hotel.yaml` (name, address,
   contact, languages), then `config/agent.yaml` (your actual room types,
   restaurant hours, room-service menu - see the comments in
   `config/agent.example.yaml`). Then:
   ```bash
   cp knowledge/property.example.md knowledge/property.md
   cp knowledge/faq.example.md      knowledge/faq.md
   cp knowledge/policies.example.md knowledge/policies.md
   cp knowledge/signature.example.md knowledge/signature.md
   ```
   Replace the Hotel Aurora content with your own facts. See
   `knowledge/README.md` for how to write it well - `policies.md` in
   particular is loaded into every prompt, so getting the escalation list
   right matters more than any other file here. Also, optionally but worth
   doing before you go live:
   ```bash
   cp knowledge/disclosure.example.md knowledge/disclosure.md
   ```
   and put the EU AI Act disclosure line in your own guest language(s) -
   every WhatsApp callback carries a generic English version of this line
   automatically even if you skip this step, but it will read oddly to a
   caller who does not read English (`docs/safety.md`).

5. **Pick how the agent thinks.** `config/hotel.yaml`'s `llm.provider`
   starts as `interactive` - it asks you, in this Claude Code session,
   instead of calling a model. That costs nothing extra and is the best way
   to see how Voice / Phone AI reasons. `docs/how-it-works.md` and
   `docs/safety.md` explain the other three providers (`mock`,
   `claude-code`, `anthropic`) and when to move to one of them.

6. **Connect a real mailbox for call transcripts (optional for now).**
   `systems.email.adapter` in `config/hotel.yaml` starts as `mock`, which
   only ever sees the bundled fixtures. Point it at a mailbox that receives
   call-transcript emails from your voicemail-to-text or call-recording
   service - `docs/integrations.md` covers `imap`/`gmail`, and the "Telephony
   - the honest gap" section there explains what still needs a provider like
   Retell, Twilio or Telnyx to answer a live call at all. Run `make doctor`
   after changing it.

7. **Re-check.**
   ```bash
   make doctor
   ```
   Once the property name is real and `knowledge/property.md` exists, the
   "hotel identity" and "knowledge" lines turn green. Move on to
   `workflows/10-calls.md` to run the loop for real.
