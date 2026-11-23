# Deploy notes — EscrowDisputeMediation

## Contract

- File: `contracts/EscrowDisputeMediation.py`
- Class: `EscrowDisputeMediation`
- Runner (line 1, pinned): `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6`
  — the hash the official project boilerplate pins
  (`genlayer-project-boilerplate`, `contracts/football_bets.py`), verified
  against the boilerplate on 2026-11-13. Keep the blank line after it.
- `genvm-lint check` passes (3 checks) and validation passes: 10 methods
  (4 view, 6 write). Note: the linter reports a newer runner is available
  (`1zr6nqk...`); the contract targets the boilerplate's runner, and
  moving to a newer runner requires the renamed APIs, not just the hash —
  do not change the hash alone.
- Direct tests: 21 passed
  (`./.venv/bin/python -m pytest tests/direct -q`).

## Constructor

**No constructor arguments.** `__init__` takes none and only zeroes the
counter. Deploy from Studio (https://studio.genlayer.com/contracts,
StudioNet network) or the CLI:

```bash
genlayer deploy --contract contracts/EscrowDisputeMediation.py
```

## Before the checklist: fund the accounts and publish the evidence

**Faucet.** Studio's built-in test accounts fund the deploy. Before
starting, claim test GEN from the Studio faucet so the payer account
holds at least 5 GEN: the checklist locks 1 GEN per escrow and runs
five escrows (steps 1–6). Confirm the payer's balance on the StudioNet
explorer before creating the first escrow.

**Accounts.** The payer is the account Studio deploys from. The worker
is a second Studio built-in test account — pick any test account other
than the payer's, and note its address. The checklist below calls it
`<WORKER>`.

**Evidence pages.** The dispute steps need three public pages the
validators can fetch. Create three public GitHub gists with exactly
these contents, and fill in the URLs below before running step 4:

1. `delivery-evidence.md` — the fully delivered job (updated 2026-11-16: the
   validators twice ruled the old text-only version insufficient, so the
   evidence now cites a real live page and a screenshot):

```text
# Delivery: landing page

The landing page is live at https://chenyejun.github.io/landing-page-demo/.

It has a signup form (email field + "Join the waitlist" button). Submitting the form sends a confirmation email: the page shows "Thanks! Please check your inbox — a confirmation email is on its way." and a confirmation email is sent to the submitted address with the subject "Please confirm your email".

Screenshot of the live page:
https://raw.githubusercontent.com/chenyejun/landing-page-demo/main/screenshot.png
```

   The page itself lives in https://github.com/chenyejun/landing-page-demo
   (GitHub Pages, `index.html` + `screenshot.png` at the repo root).

2. `partial-delivery.md` — the page is up, email confirmation is missing:

```text
# Delivery: landing page

The landing page is live at the agreed URL with a working signup form.
Email confirmation was not implemented: submitting the form stores the
address but sends no confirmation email.
```

3. `no-delivery.md` — nothing was delivered:

```text
# Project status

No code was delivered. The repository is empty, there are no commits,
and no page was deployed.
```

Use the gist's **raw** URL for each (the URL that returns the plain
text, e.g. `https://gist.githubusercontent.com/<user>/<id>/raw/<file>`).
Fill them in here:

- `<GIST_DELIVERED>`: (raw URL of delivery-evidence.md)
- `<GIST_PARTIAL>`: (raw URL of partial-delivery.md)
- `<GIST_NOTHING>`: (raw URL of no-delivery.md)

Open each raw URL in a browser first and confirm it returns the text
above — if a gist is private or the URL 404s, the dispute steps will
revert with `[EXTERNAL]`.

**Deadlines.** The checklist uses two deadline shapes: a one-week
deadline for the dispute escrows, and a 10-minute deadline for the
cancel test. Compute the unix timestamps at deploy time, e.g.
`date -d "+7 days" +%s` and `date -d "+10 minutes" +%s`. The agreement
text used throughout is:

```text
Build a one-page landing page with a signup form and email confirmation.
```

## Post-deploy checklist

Run these on Studio against the deployed address, in order. After
every step that moves money, check the two balances on the StudioNet
explorer (payer and worker) and confirm they match the expected split
— the contract's state alone does not prove the transfer landed.

### 1. `create_escrow` — happy path

Args (value: **1 GEN** attached):

- `worker`: `<WORKER>`
- `agreement`: `Build a one-page landing page with a signup form and email confirmation.`
- `deadline`: `<now + 7 days, unix seconds>`

Expected: returns `1`. Then call `get_escrow(1)` and confirm `payer`
is your address, `worker` is `<WORKER>`, `amount: 1000000000000000000`
(1 GEN = 10^18 atto GEN), `status: "FUNDED"`, and `agreement` is the
text above.

### 2. `release_escrow` — happy path (worker paid in full)

Args: `escrow_id: 1`. Call from the **payer** account.

Expected: succeeds. `get_escrow(1)` → `status: "RELEASED"`. On the
explorer, the worker's balance grew by 1 GEN and the payer's fell by
1 GEN (plus gas).

Then, from the payer account, call `release_escrow(1)` again —
expected: reverts with `[EXPECTED]` (`escrow is not funded`).

### 3. `cancel_escrow` — refund after the deadline

1. `create_escrow(<WORKER>, <same agreement>, <now + 10 minutes>)`
   with **1 GEN** → returns `2`.
2. Immediately call `cancel_escrow(2)` from the payer — expected:
   reverts with `[EXPECTED]` (`escrow cannot be cancelled before its
   deadline`).
3. Wait 10 minutes, then call `cancel_escrow(2)` from the payer —
   expected: succeeds. `get_escrow(2)` → `status: "CANCELLED"`. On
   the explorer, the payer's balance grew back by 1 GEN.

### 4. Dispute — delivered but not meeting the agreement (50/50)

1. `create_escrow(<WORKER>, <same agreement>, <now + 7 days>)` with
   **1 GEN** → returns `3`.
2. From the **payer** account, `open_dispute(3, "The page is up but
   email confirmation is missing.", <GIST_PARTIAL>)` — expected:
   succeeds. `get_dispute(3)` → `payer_statement` is the text above,
   `worker_statement` is `""`, `awaiting` is `<WORKER>`,
   `resolved: false`. `get_escrow(3)` → `status: "DISPUTED"`.
3. From the **worker** account, `submit_response(3, "The form works;
   email was out of scope.", "")` — expected: succeeds.
   `get_dispute(3)` → `worker_statement` is the text above,
   `awaiting` is the zero address.
4. From any account, `resolve_dispute(3)` — expected: succeeds.
   `get_dispute(3)` → `delivered: true`, `met_agreement: false`,
   `worker_share: 50`, `resolved: true`. `get_escrow(3)` → `status:
   "RESOLVED"`. On the explorer, the worker gained 0.5 GEN and the
   payer regained 0.5 GEN.

If step 4 reverts with `[TRANSIENT]`, an evidence host was briefly
unreachable — wait and retry. If it reverts with `[EXTERNAL]`, a gist
URL is wrong — check the raw URLs in a browser and retry. If it
reverts with `[LLM_ERROR]`, retry once.

### 5. Dispute — fully delivered (100% to the worker)

1. `create_escrow(<WORKER>, <same agreement>, <now + 7 days>)` with
   **1 GEN** → returns `4`.
2. From the **worker** account, `open_dispute(4, "I delivered the
   page; the payer refuses to release.", <GIST_DELIVERED>)` —
   expected: succeeds. `get_dispute(4)` → `worker_statement` is the
   text above, `awaiting` is the payer.
3. From the **payer** account, `submit_response(4, "The page is not
   what was agreed.", "")` — expected: succeeds.
4. `resolve_dispute(4)` — expected: succeeds. `get_dispute(4)` →
   `delivered: true`, `met_agreement: true`, `worker_share: 100`.
   On the explorer, the worker gained the full 1 GEN.

### 6. Dispute — nothing delivered, response gate (0%)

1. `create_escrow(<WORKER>, <same agreement>, <now + 7 days>)` with
   **1 GEN** → returns `5`.
2. From the **payer** account, `open_dispute(5, "Nothing was
   delivered.", <GIST_NOTHING>)` — expected: succeeds.
   `get_dispute(5)` → `awaiting` is `<WORKER>`,
   `response_deadline` is `disputed_at + 86400` (24 hours).
3. Immediately call `resolve_dispute(5)` — expected: reverts with
   `[EXPECTED]` (`dispute cannot be resolved before the counterparty
   responds or the response deadline passes`). This is the response
   gate: the opener's case alone never settles the payout while the
   counterparty is still awaited inside its window.
4. From the **worker** account, `submit_response(5, "No work was
   done.", "")` — expected: succeeds. `get_dispute(5)` → `awaiting`
   is the zero address. (The never-responds path — resolving after
   the 24-hour deadline with no response — is covered by the direct
   tests via time warp; on chain it resolves the same way once the
   deadline passes.)
5. Call `resolve_dispute(5)` — expected: succeeds.
   `get_dispute(5)` → `delivered: false`,
   `met_agreement: false`, `worker_share: 0`, `resolved: true`. On
   the explorer, the payer regained the full 1 GEN and the worker
   gained nothing.

### 7. Negative checks

- From a **third** account (neither payer nor worker),
  `open_dispute` on a fresh escrow — expected: reverts with
  `[EXPECTED]` (`only the payer or the worker can open a dispute`).
  (Create escrow 6 for this, then leave it funded — or cancel it
  after its deadline.)
- `resolve_dispute(3)` again — expected: reverts with `[EXPECTED]`
  (`escrow has no open dispute`).
- `get_dispute(1)` — expected: reverts with `[EXPECTED]` (escrow 1
  never had a dispute).

### 8. Views (read-only, no fee)

- `list_escrows()` → all escrows created (5 after step 6, 6 if you
  created one for the negative checks).
- `stats()` → `{escrows: 5, open_escrows: 0, resolved_disputes: 3}`
  (after step 6; `open_escrows` counts `FUNDED` and `DISPUTED`).

## If something looks wrong

- Deploy rejected with `invalid_contract`: the Depends line is the
  usual suspect — confirm line 1 is exactly the pinned hash and line 2
  is blank.
- `create_escrow` reverts `[EXPECTED]` on the worker: the address came
  through the Studio form as an int or a string, and the contract
  normalizes it — but a mistyped address (wrong length, or a decimal
  number that is not the address's integer value) still fails the
  check. Paste the worker's checksummed hex address (`0x...`) from the
  Studio account list.
- `resolve_dispute` reverts `[LLM_ERROR]`: the model returned
  something outside the expected shape. Retry once; if it persists,
  the evidence pages may be too long or too ambiguous — shorten them.
- `resolve_dispute` reverts `[EXTERNAL]`: an evidence URL did not
  return HTTP 200. Open the raw gist URL in a browser; if it 404s,
  the gist is private or the URL is wrong — fix it and retry (the
  dispute stays open).
- `resolve_dispute` hangs or times out on Studio: the consensus round
  is waiting on validators. Wait for the Studio timeout message;
  nothing was written. Retry.
- A verdict you disagree with: do not edit state — there is no edit
  path by design, and a resolved dispute cannot be reopened. The
  verdict, the rationale, and both sides' evidence are stored on the
  dispute record (`get_dispute`) for audit.
