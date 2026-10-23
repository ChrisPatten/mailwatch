# AGENTS.md

Guidance for coding agents (and humans) changing mailwatch. Read this before editing anything in `mailwatch/`.

## What this project is

mailwatch is a quarantined email reader. A model with **no tools** reads untrusted mail; plain Python does every action (IMAP, fetching, writing, notifying); a downstream agent reads only the code-built output directory. The value of the project is the boundary, so most of this file is about not weakening it.

## Invariants: never break these

1. **No tools for the model.** `llm.py` sends one system prompt and one user turn with a JSON schema. Never add tool or function definitions, and never let model output pick a code path beyond the fields the schemas define.
2. **No URLs to or from the model.** `normalize.py` replaces every link with an ID (`L1`…). The model picks IDs; `pipeline.py` checks them against the code-built link table. Never put a URL in a prompt, and strip URLs from any model text that reaches a record (`grounding.strip_urls`).
3. **One message per model call.** Never batch several messages into one prompt; a message could then leak another's content.
4. **Untrusted text is fenced.** Anything a sender controls (subject, names, body, link anchors, attachment names, fetched pages, earlier summaries) goes through `prompts._fence`, which also defangs fence markers. Metadata labelled "computed by code" must really be computed by code.
5. **IMAP stays read-only.** `imap_ro.ReadOnlyIMAP` exposes only LOGIN, CAPABILITY, LIST, EXAMINE, SEARCH, FETCH (PEEK items) and LOGOUT. Don't add methods, and don't reach the wrapped client from outside.
6. **The broker decides, in code.** `broker.py` rules (action denylist, one-time-token check, IP checks with pinned connect, manual redirects rechecked per hop, GET only, no cookies, type and size caps, skip domains) must not take input from the model beyond the chosen link ID. The `Context` for the view-link exception is built from code-derived facts (`sender_verified`, tier, anchor text).
7. **Ground typed fields.** Amounts, dates, order and tracking numbers and account refs must pass `grounding.ground` before they reach a record.
8. **Notifications carry no mail text.** `emit.notify` bodies are built from counts, IDs and paths only.
9. **Secrets are references.** Config holds `keychain://` or `env://` refs only. Never log a secret, and never write one to disk or argv except where `install.sh` already does.

If a change needs to relax one of these, stop and explain why in the PR description.

## Layout

```
mailwatch/
  cli.py          entry point: run, status, since, ask, link, open, reprocess, digest, doctor, label, eval
  config.py       config discovery + defaults
  pipeline.py     run loop: ingest -> normalize -> triage -> fetch -> extract -> emit; reprocess; prune; notify
  ingest.py       IMAP sync, dedupe, count check
  imap_ro.py      read-only IMAP wrapper
  normalize.py    MIME -> clean text, link IDs, hidden-text stripping, auth verdict, bulk signals
  attachments.py  PDF/ICS/DOCX/XLSX/CSV/HTML/image parsers (never execute or unpack)
  rules.py        free header rules, watchlist, rules.yaml overrides
  prompts.py      system prompts (trusted) and fenced user turns (untrusted)
  schemas.py      JSON schemas sent to the model + pydantic validators
  llm.py          OpenAI Responses / Anthropic Messages, structured output, retry, usage
  broker.py       link fetching policy
  grounding.py    number grounding, URL stripping, length caps
  emit.py         records, index, digest, notify, pruning helpers
  store.py        SQLite (one connection behind a lock; safe across worker threads)
  evalkit.py      labelling + model bake-off
  canaries.py     synthetic injection emails for the bake-off and tests
tests/            pytest; fake IMAP, mock model API, mock HTTP. No network.
scripts/install.sh, launchd/*.template   macOS setup
local/            personal integration files. Git-ignored. Never commit or reference from code.
```

## Working on it

- Python 3.10+, with `from __future__ import annotations` in every module. Keep modules small and dependency-light.
- Run `pytest` before you finish. Every security-relevant change needs a test, and so does any change to `normalize.py`, `prompts.py` or `broker.py`. When you add a way to hide text or smuggle instructions, add a canary to `canaries.py`.
- Tests must not touch the network, a real mailbox or a real API. Use `tests/conftest.py` (`FakeIMAP`, `FakeLLM`, `make_msg`) and `httpx.MockTransport`.
- Model schemas must stay inside the subset both providers accept: every property required, `additionalProperties: false`, no length or range keywords, no nullable unions (use `""` for "none"). Put caps in the pydantic validators instead.
- New stage outputs go to `raw/work/<id>.json` and the store. Records are written only by `pipeline._record` / `emit.write_record`.
- `pipeline._pool` runs stages in parallel (`[run].workers`). Shared state goes through `Store` (locked), `Usage.add` (locked), `Broker` counters (locked) or `Pipeline._bump`.
- Never commit `config.toml`, `state.db*`, `raw/`, `logs/`, `out/`, `eval/` or `local/`.

## Adding things

- **A model provider:** add a `_<provider>` method in `llm.py` that returns `(text, input_tokens, cached_tokens, output_tokens)`, add prices to `PRICES`, and extend `Models.check`.
- **An attachment type:** add a parser in `attachments.py` and a `kind_of` mapping. If it can arrive as generic binary, add a magic-number check to `broker.sniff`.
- **A tier or a record field:** update `schemas.py` (both the JSON schema and the pydantic model), `prompts.py`, `emit._item`/`_bucket`, the README record example and the tests.
