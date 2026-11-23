# EscrowDisputeMediation

Escrowed work, settled by consensus when the sides disagree. A payer
locks GEN in escrow for a job described by an agreement text frozen at
creation. The happy path needs no consensus at all: the payer releases
the escrow and the worker is paid in full, or the payer cancels after
the deadline and is refunded. Consensus only enters when the two sides
disagree — then either side opens a dispute, each side states its case
with one evidence URL, and validator consensus judges two facts:
whether the deliverable was provided, and whether it met the agreement
as written. The split follows deterministically from those two facts.

## Why consensus is needed

An escrow dispute is a judgement call about what was delivered against
what was agreed, and neither side can be trusted to judge its own
case. A single arbiter could be picked, bribed, or simply wrong. Here
the verdict is produced by a leader and re-produced independently by
every validator, each reading the same frozen agreement and the same
two sides' evidence, and it is only written to state when both
booleans match. The model never outputs a funding decision: the split
is derived by code from the two agreed booleans, so the model cannot
move money by phrasing.

## What it does

- `create_escrow` — lock funds for a job. Payable; the attached value
  is the full amount at stake. The agreement text is frozen at
  creation. Returns the escrow id.
- `release_escrow` — payer only, while `FUNDED`: pay the worker in
  full. No dispute, no consensus.
- `cancel_escrow` — payer only, while `FUNDED` and after the deadline:
  refund the payer in full.
- `open_dispute` — either the payer or the worker, while `FUNDED`:
  record the opener's statement and evidence URL under the opener's
  side. The other side is then awaited.
- `submit_response` — the awaited party, once: record the other side's
  statement and evidence URL. If that party never responds, the
  dispute can still be resolved once the 24-hour response deadline
  passes, on the record as it stands.
- `resolve_dispute` — anyone may call it; the caller decides nothing.
  Runs validator consensus on the dispute and pays the split out
  immediately. Resolution is gated: while the counterparty is still
  awaited inside its 24-hour response window, the call reverts — the
  opener's case alone never settles the payout.
- Views: `get_escrow`, `get_dispute`, `list_escrows`, `stats`.

The split rule, from the two agreed booleans:

| delivered | met_agreement | Worker gets | Payer gets |
| --- | --- | --- | --- |
| true | true | 100% | 0% |
| true | false | 50% | 50% |
| false | either | 0% | 100% |

The worker's amount is integer division of the locked amount by the
share; the payer receives the remainder, so the two payouts always sum
to the locked amount exactly and no dust is ever left in the contract.
Zero legs are not emitted.

A dispute is resolved once; there is no appeal, and an escrow cannot
be cancelled while a dispute is open. The worker's protection against
a stalling payer is the dispute path: open a dispute before the
deadline and the escrow can no longer be cancelled.

## State design

| Field | Type | Purpose |
| --- | --- | --- |
| `escrows` | `TreeMap[u256, Escrow]` | Escrows by id |
| `disputes` | `TreeMap[u256, Dispute]` | Disputes by escrow id |
| `escrow_ids` | `DynArray[u256]` | Insertion order for listing |
| `escrow_counter` | `u256` | Monotonic ids |

`Escrow` stores the payer, the worker, the frozen agreement, the
locked amount, the deadline, the status (`FUNDED` / `RELEASED` /
`DISPUTED` / `RESOLVED` / `CANCELLED`), and the creation time.
`Dispute` stores the opener, both sides' statements and evidence
URLs, which side is still awaited (or the zero address once both
sides have spoken), the dispute and response-deadline times, the
verdict (`delivered`, `met_agreement`, `worker_share`, `rationale`),
and whether it is resolved.

Three rules shape the design. First, the agreement is frozen: nothing in
the contract can edit it after creation, because it is the document
consensus judges a later dispute against. Second, the dispute record
is two-sided by construction — the payer's case and the worker's case
live in separate fields, and a side that never submits leaves its
fields empty rather than being filled in by the other side. Silence is
a fact about the record, not a default win. Third, the counterparty
gets its say: opening a dispute starts a 24-hour response window
(`response_deadline = disputed_at + 86400`), and `resolve_dispute`
reverts while the other side is still awaited inside that window.
Only a submitted response, or the deadline passing, opens the gate —
so a payout is never finalized from one side's case while the other
side is still waiting to be heard, and silence past the deadline
cannot stall the dispute forever.

## How the equivalence check works

Resolution runs one consensus round over the same judge code (`_judge`:
fetch each side's evidence page with `gl.nondet.web.get`, ask the
model for the two booleans with `gl.nondet.exec_prompt` and
`response_format="json"`, validate the answer). The round is
`gl.vm.run_nondet_unsafe(leader_fn, validator_fn)`.

**The leader and the validator each do the whole job.** Both sides
fetch the evidence pages and judge the same two facts from the same
agreement and the same two cases. The stored verdict is the leader's
agreed pair of booleans, with the rationale attached for audit.

**Agreement is exact on both booleans.** The validator requires
`delivered` and `met` to match by plain `==`. These booleans decide
where the money goes, so there is no tolerance here — unlike a price,
a judgement about whether work was delivered does not have a natural
epsilon.

**Leader failures.** If the leader fails, the validator re-runs the
judge and compares failure classes: `[EXPECTED]` and `[EXTERNAL]` must
match exactly, `[TRANSIENT]` only has to be transient on both sides,
and `[LLM_ERROR]` never agrees, so a bad model answer rotates the
leader instead of locking bad state in.

Storage is never read inside a nondet block: `resolve_dispute` copies
the escrow and the dispute to memory with `gl.storage.copy_to_memory`
first and hands the consensus round plain in-memory values. The leader
and validator functions never reference `self`.

No `strict_eq` is used on any LLM output anywhere in the contract:
the two booleans are compared with plain `==` inside the validator
function, which is the correct tool for two booleans.

## Failure paths

| Prefix | Meaning | Examples | Caller should |
| --- | --- | --- | --- |
| `[EXPECTED]` | Bad input or invalid state | Zero worker, payer escrowing to themselves, zero value, past deadline, empty agreement, non-payer releasing or cancelling, cancelling before the deadline, a stranger opening a dispute, responding when not awaited, resolving without an open dispute, resolving while the counterparty is still awaited inside its 24-hour response window, a non-HTTP evidence URL | Fix the input or the caller; no retry (for a gated resolve: wait for the response or the deadline) |
| `[EXTERNAL]` | The outside world is at fault, deterministically | An evidence URL returned 4xx (page gone, moved, or access denied) or an empty body | Fix the evidence URL — the dispute cannot be resolved until the evidence loads; the dispute itself stays open |
| `[TRANSIENT]` | The outside world failed, possibly temporarily | An evidence host returned 5xx or 429 | Retry later; nothing was written |
| `[LLM_ERROR]` | The model answer was unusable | Non-JSON answer, a non-boolean `delivered` or `met`, a missing flag | Retry; the leader rotates, so a persistent failure means the evidence is the problem |

A failed `resolve_dispute` writes nothing: the dispute stays open and
the call can be retried. A failed `create_escrow` / `release_escrow` /
`cancel_escrow` / `open_dispute` / `submit_response` likewise writes
nothing.

## Methods

| Method | Access | Arguments | Returns |
| --- | --- | --- | --- |
| `create_escrow` | write, payable | `worker: Address`, `agreement: str`, `deadline: u256` (unix seconds) | `escrow_id: u256` |
| `release_escrow` | write | `escrow_id: u256` | — |
| `cancel_escrow` | write | `escrow_id: u256` | — |
| `open_dispute` | write | `escrow_id: u256`, `statement: str`, `evidence_url: str` (`""` for none) | — |
| `submit_response` | write | `escrow_id: u256`, `statement: str`, `evidence_url: str` (`""` for none) | — |
| `resolve_dispute` | write | `escrow_id: u256` | — |
| `get_escrow` | view | `escrow_id: u256` | Escrow dict |
| `get_dispute` | view | `escrow_id: u256` | Dispute dict |
| `list_escrows` | view | — | `{escrows: [...]}` |
| `stats` | view | — | `{escrows, open_escrows, resolved_disputes}` |

Notes on arguments: the agreement is capped at 2,000 characters,
statements at 2,000, evidence URLs at 500, and each evidence page is
truncated to 6,000 characters. The deadline is unix seconds and must
be in the future at creation. The worker must be a real address,
different from the payer, and the attached value must be greater than
zero. Evidence URLs must start with `http://` or `https://`; an empty
string means that side has no evidence to link.

## Deploying on StudioNet

Prerequisites: a GenLayer Studio account and the Studio app at
https://studio.genlayer.com/contracts.

1. Open Studio, switch to the StudioNet network, and create a new
   contract from `contracts/EscrowDisputeMediation.py` (paste the file
   or upload it).
2. Deploy with **no constructor arguments** — `__init__` takes none.
   The first line of the file pins the runner:
   `# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }`.
   That is the hash the official project boilerplate pins
   (`genlayer-project-boilerplate`, `contracts/football_bets.py`), verified
   against the boilerplate on 2026-11-13. Keep the blank line after it:
   the runner parses the leading `#` lines as one JSON document, and a
   comment glued directly under the Depends line breaks deployment.
3. Note the deployed contract address. The full step-by-step checklist,
   including the exact calls to run after deploy and what each should
   return, is in `DEPLOY_NOTES.md`.

CLI alternative (same network, same file):

```bash
genlayer network set testnet-bradbury   # or the StudioNet equivalent for your CLI version
genlayer deploy --contract contracts/EscrowDisputeMediation.py
```

## Verifying after deploy

Run each write method once on Studio and check the result against
`DEPLOY_NOTES.md`. In short: create an escrow with 1 GEN and release
it, and confirm the worker's balance grew by 1 GEN on the explorer;
create a second escrow with a short deadline, wait it out, cancel it,
and confirm the payer was refunded; then run three disputes — one
that settles 50/50 (delivered but missing a named part), one that
settles 100% to the worker (fully delivered), and one that settles 0%
(nothing delivered) — and confirm the balances
on the explorer match the splits. The 0% dispute also demonstrates
the response gate: calling `resolve_dispute` before the worker
responds (and before the 24-hour response deadline) reverts with
`[EXPECTED]`.

## Tests

Direct tests (mocked web + LLM, no network) live in
`tests/direct/test_escrow_dispute_mediation.py` and follow the official
`genlayer-test` pattern (`direct_vm` / `direct_deploy` fixtures). They
cover creation and input validation, the Studio-form argument forms
(worker as an int or a hex string, deadline as a JSON string),
release (payer only, once), cancel (payer only, after the deadline),
opening a dispute (either party, stranger rejected, evidence URL
validation), responding (only the awaited party, once), the response
gate (resolving blocked while the counterparty is still awaited,
allowed immediately after a response, allowed after the 24-hour
deadline elapses), the three
settlement outcomes (100 / 50 / 0), validator disagreement on a
boolean (via mock swapping between leader and replay), evidence
unreachable (`[EXTERNAL]` for 404, `[TRANSIENT]` for 429), malformed
model verdicts (`[LLM_ERROR]`), resolving without a dispute, and all
views.

Two direct-mode limits shape what is asserted here. Value movement is
not simulated: `emit_transfer` is a no-op in direct mode, so payouts
are asserted through on-chain state (escrow status, dispute
`worker_share`) rather than balances — the Studio checklist in
`DEPLOY_NOTES.md` covers verifying the real transfers after deploy.
And only the leader of a consensus round runs; the validator is
captured, not executed, so agreement and disagreement are tested
explicitly with `run_validator()` after (re)registering the mocks.

```bash
./.venv/bin/python -m pytest tests/direct -q
./.venv/bin/genvm-lint check contracts/EscrowDisputeMediation.py
```

## Limitations

- **No appeal.** A resolved dispute is final. If the evidence was
  incomplete at resolution time, the only remedy is a new escrow — the
  contract has no path to reopen a settled dispute.
- **Evidence must be public.** The validators fetch the evidence URLs
  over the public web. A page behind a login, a CAPTCHA, or a private
  link cannot be judged.
- **The split is fixed at 100 / 50 / 0.** The contract does not
  negotiate: a delivery that is 90% complete and a delivery that is
  51% complete both settle 50/50. The coarseness is deliberate — a
  fine-grained percentage from a model would be a number the model
  invented, not a fact both sides agreed on.
- **One dispute per escrow.** A second dispute over the same escrow
  cannot be opened, even if new evidence appears later. Both sides
  should put their full case on record before calling
  `resolve_dispute`.
- **Runner version.** The contract pins the boilerplate's runner hash
  (`1jb45aa8...`, rc7-era APIs such as `gl.vm.run_nondet_unsafe` and
  `gl.get_contract_at`). The SDK's current docs describe a newer runner
  with renamed APIs (`run_nondet`, `gl.contract.get_at`, and others);
  moving to it requires updating the consensus calls, not just the hash.
