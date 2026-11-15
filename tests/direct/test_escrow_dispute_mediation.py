"""Direct tests for EscrowDisputeMediation — mocked web + LLM, no network.

Runs the contract in-process with mocked evidence pages and LLM
verdicts. Two direct-mode limits shape what is asserted here:

- Value movement is NOT simulated: emit_transfer is a no-op in direct
  mode, so payouts are asserted through on-chain state (escrow status,
  dispute worker_share) rather than balances. The Studio checklist in
  DEPLOY_NOTES.md covers verifying the real transfers after deploy.
- Only the leader of a consensus round runs; the validator is captured,
  not executed. Agreement and disagreement are tested explicitly with
  vm.run_validator() after (re)registering the LLM mock, which is how
  the validator's view of the evidence is simulated.
"""

import json
import re
from datetime import datetime, timezone

from tests.direct.conftest import to_hex

ONE_GEN = 10**18
AGREEMENT = "Build a one-page landing page with a signup form and email confirmation by December 13."
DEADLINE = int(datetime(2026, 12, 13, tzinfo=timezone.utc).timestamp())

EVIDENCE_DELIVERED = "https://example.org/delivery/landing-page.md"
EVIDENCE_PARTIAL = "https://example.org/delivery/landing-page-partial.md"
EVIDENCE_NOTHING = "https://example.org/delivery/no-delivery.md"

PAGE_DELIVERED = (
    "# Delivery: landing page\n\n"
    "The landing page is live at the agreed URL. It has a signup form "
    "and email confirmation works: submitting the form sends a "
    "confirmation email. Screenshots of the form and the confirmation "
    "email are attached below.\n"
)
PAGE_PARTIAL = (
    "# Delivery: landing page\n\n"
    "The landing page is live at the agreed URL with a working signup "
    "form. Email confirmation was not implemented: submitting the form "
    "stores the address but sends no confirmation email.\n"
)
PAGE_NOTHING = (
    "# Project status\n\n"
    "No code was delivered. The repository is empty, there are no "
    "commits, and no page was deployed.\n"
)


def _verdict_json(delivered, met, rationale="Judged from the evidence."):
    return json.dumps({"delivered": delivered, "met": met, "rationale": rationale})


def _create(vm, contract, payer, worker, amount=5 * ONE_GEN, deadline=DEADLINE):
    vm.sender = payer
    vm.value = amount
    escrow_id = contract.create_escrow(worker, AGREEMENT, deadline)
    vm.value = 0
    return escrow_id


def _mock_evidence(vm, url, body):
    vm.mock_web(re.escape(url), {"status": 200, "body": body})


def _mock_evidence_status(vm, url, status):
    vm.mock_web(re.escape(url), {"status": status, "body": ""})


def _mock_verdict(vm, delivered, met):
    vm.mock_llm("mediating an escrow dispute", _verdict_json(delivered, met))


def _open_dispute(vm, contract, caller, escrow_id=1, statement="Dispute.", url=""):
    vm.sender = caller
    contract.open_dispute(escrow_id, statement, url)


def _resolve(vm, contract, escrow_id=1):
    contract.resolve_dispute(escrow_id)


# -- creation and happy paths --------------------------------------------


def test_create_escrow_locks_funds_and_freezes_agreement(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = direct_deploy("contracts/EscrowDisputeMediation.py")
    direct_vm.warp("2026-11-16T00:00:00Z")

    escrow_id = _create(direct_vm, contract, direct_alice, direct_bob)

    assert escrow_id == 1
    escrow = contract.get_escrow(1)
    assert escrow["payer"] == to_hex(direct_alice)
    assert escrow["worker"] == to_hex(direct_bob)
    assert escrow["agreement"] == AGREEMENT
    assert escrow["amount"] == 5 * ONE_GEN
    assert escrow["deadline"] == DEADLINE
    assert escrow["status"] == "FUNDED"
    assert escrow["created_at"] > 0
    assert escrow["found"] is True
    assert len(contract.list_escrows()["escrows"]) == 1


def test_create_escrow_rejects_bad_inputs(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = direct_deploy("contracts/EscrowDisputeMediation.py")
    direct_vm.warp("2026-11-16T00:00:00Z")

    # Zero worker.
    direct_vm.sender = direct_alice
    direct_vm.value = 5 * ONE_GEN
    with direct_vm.expect_revert("[EXPECTED] worker must not be the zero address"):
        contract.create_escrow(0, AGREEMENT, DEADLINE)

    # Payer escrowing to themselves.
    with direct_vm.expect_revert("[EXPECTED] payer and worker must be different"):
        contract.create_escrow(direct_alice, AGREEMENT, DEADLINE)

    # Zero value.
    direct_vm.value = 0
    with direct_vm.expect_revert("[EXPECTED] escrow amount must be greater than zero"):
        contract.create_escrow(direct_bob, AGREEMENT, DEADLINE)

    # Past deadline.
    direct_vm.value = 5 * ONE_GEN
    with direct_vm.expect_revert("[EXPECTED] deadline must be in the future"):
        contract.create_escrow(direct_bob, AGREEMENT, 1)

    # Empty agreement.
    with direct_vm.expect_revert("[EXPECTED] agreement must not be empty"):
        contract.create_escrow(direct_bob, "   ", DEADLINE)
    direct_vm.value = 0


def test_create_escrow_accepts_string_and_int_worker(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    """Studio's write form sends addresses as ints or hex strings, and
    numbers as JSON strings. The contract normalizes every entry
    itself, so the same call must succeed and store real Addresses."""
    contract = direct_deploy("contracts/EscrowDisputeMediation.py")
    direct_vm.warp("2026-11-16T00:00:00Z")

    # Worker as a plain int, deadline as a string.
    direct_vm.sender = direct_alice
    direct_vm.value = 5 * ONE_GEN
    escrow_id = contract.create_escrow(int(to_hex(direct_bob), 16), AGREEMENT, str(DEADLINE))
    direct_vm.value = 0
    assert escrow_id == 1
    escrow = contract.get_escrow(1)
    assert escrow["worker"] == to_hex(direct_bob)
    assert type(escrow["deadline"]) is int

    # Worker as a hex string, on a second escrow.
    direct_vm.sender = direct_alice
    direct_vm.value = ONE_GEN
    escrow_id = contract.create_escrow(to_hex(direct_bob), AGREEMENT, DEADLINE)
    direct_vm.value = 0
    assert escrow_id == 2
    assert contract.get_escrow(2)["worker"] == to_hex(direct_bob)
    assert isinstance(contract.get_escrow(2)["worker"], str)


def test_release_pays_worker_in_full(
    direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie
):
    contract = direct_deploy("contracts/EscrowDisputeMediation.py")
    escrow_id = _create(direct_vm, contract, direct_alice, direct_bob)

    # Only the payer can release.
    direct_vm.sender = direct_charlie
    with direct_vm.expect_revert("[EXPECTED] only the payer can release"):
        contract.release_escrow(escrow_id)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("[EXPECTED] only the payer can release"):
        contract.release_escrow(escrow_id)

    direct_vm.sender = direct_alice
    contract.release_escrow(escrow_id)
    assert contract.get_escrow(escrow_id)["status"] == "RELEASED"

    # Released escrows cannot be released, disputed, or cancelled.
    with direct_vm.expect_revert("[EXPECTED] escrow is not funded"):
        contract.release_escrow(escrow_id)
    with direct_vm.expect_revert("[EXPECTED] escrow is not funded"):
        contract.open_dispute(escrow_id, "Too late.", "")


def test_cancel_after_deadline_refunds_payer(
    direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie
):
    contract = direct_deploy("contracts/EscrowDisputeMediation.py")
    direct_vm.warp("2026-11-16T00:00:00Z")
    escrow_id = _create(direct_vm, contract, direct_alice, direct_bob)

    direct_vm.sender = direct_charlie
    with direct_vm.expect_revert("[EXPECTED] only the payer can cancel"):
        contract.cancel_escrow(escrow_id)

    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("[EXPECTED] escrow cannot be cancelled before its deadline"):
        contract.cancel_escrow(escrow_id)

    direct_vm.warp("2026-12-14T00:00:00Z")
    contract.cancel_escrow(escrow_id)
    assert contract.get_escrow(escrow_id)["status"] == "CANCELLED"

    with direct_vm.expect_revert("[EXPECTED] escrow is not funded"):
        contract.cancel_escrow(escrow_id)


# -- disputes ------------------------------------------------------------


def test_open_dispute_records_both_sides(
    direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie
):
    contract = direct_deploy("contracts/EscrowDisputeMediation.py")
    escrow_id = _create(direct_vm, contract, direct_alice, direct_bob)

    # A stranger cannot open a dispute.
    direct_vm.sender = direct_charlie
    with direct_vm.expect_revert("[EXPECTED] only the payer or the worker can open"):
        contract.open_dispute(escrow_id, "Not my job.", "")

    # The payer opens; the worker is awaited.
    _open_dispute(
        direct_vm, contract, direct_alice, escrow_id,
        statement="Nothing was delivered.", url=EVIDENCE_NOTHING,
    )
    dispute = contract.get_dispute(escrow_id)
    assert dispute["opener"] == to_hex(direct_alice)
    assert dispute["payer_statement"] == "Nothing was delivered."
    assert dispute["payer_evidence"] == EVIDENCE_NOTHING
    assert dispute["worker_statement"] == ""
    assert dispute["awaiting"] == to_hex(direct_bob)
    assert dispute["resolved"] is False
    assert contract.get_escrow(escrow_id)["status"] == "DISPUTED"

    # A second dispute cannot be opened.
    with direct_vm.expect_revert("[EXPECTED] escrow is not funded"):
        contract.open_dispute(escrow_id, "Again.", "")


def test_open_dispute_validates_evidence_url(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = direct_deploy("contracts/EscrowDisputeMediation.py")
    escrow_id = _create(direct_vm, contract, direct_alice, direct_bob)

    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("[EXPECTED] evidence URL must start with http"):
        contract.open_dispute(escrow_id, "Bad link.", "ftp://example.org/x.md")
    with direct_vm.expect_revert("[EXPECTED] statement must not be empty"):
        contract.open_dispute(escrow_id, "   ", "")


def test_submit_response_only_the_awaited_party(
    direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie
):
    contract = direct_deploy("contracts/EscrowDisputeMediation.py")
    escrow_id = _create(direct_vm, contract, direct_alice, direct_bob)
    _open_dispute(direct_vm, contract, direct_alice, escrow_id, "Nothing delivered.", "")

    # The payer already spoke; the opener cannot respond to themselves.
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("[EXPECTED] only the awaited party can respond"):
        contract.submit_response(escrow_id, "More.", "")
    # A stranger cannot respond either.
    direct_vm.sender = direct_charlie
    with direct_vm.expect_revert("[EXPECTED] only the awaited party can respond"):
        contract.submit_response(escrow_id, "Hello.", "")

    # The worker responds; nobody is awaited afterwards.
    direct_vm.sender = direct_bob
    contract.submit_response(escrow_id, "It is done.", EVIDENCE_DELIVERED)
    dispute = contract.get_dispute(escrow_id)
    assert dispute["worker_statement"] == "It is done."
    assert dispute["worker_evidence"] == EVIDENCE_DELIVERED
    assert dispute["awaiting"] == "0x0000000000000000000000000000000000000000"

    # A second response is rejected.
    with direct_vm.expect_revert("[EXPECTED] only the awaited party can respond"):
        contract.submit_response(escrow_id, "Again.", "")


def test_submit_response_without_dispute_rejected(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = direct_deploy("contracts/EscrowDisputeMediation.py")
    escrow_id = _create(direct_vm, contract, direct_alice, direct_bob)

    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("[EXPECTED] escrow has no open dispute"):
        contract.submit_response(escrow_id, "Hello.", "")


# -- resolution ----------------------------------------------------------


def _setup_dispute(
    vm, contract, payer, worker, opener, opener_statement, opener_url,
    responder=None, responder_statement="", responder_url="",
):
    """Create an escrow, open a dispute, optionally answer it. Returns
    the escrow id."""
    escrow_id = _create(vm, contract, payer, worker)
    _open_dispute(vm, contract, opener, escrow_id, opener_statement, opener_url)
    if responder is not None:
        vm.sender = responder
        contract.submit_response(escrow_id, responder_statement, responder_url)
    return escrow_id


def test_resolve_full_payment_when_delivered_and_met(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = direct_deploy("contracts/EscrowDisputeMediation.py")
    direct_vm.check_pickling = True
    escrow_id = _setup_dispute(
        direct_vm, contract, direct_alice, direct_bob,
        opener=direct_bob,
        opener_statement="I delivered the page; the payer refuses to release.",
        opener_url=EVIDENCE_DELIVERED,
        responder=direct_alice,
        responder_statement="The page is not what was agreed.",
        responder_url="",
    )

    _mock_evidence(direct_vm, EVIDENCE_DELIVERED, PAGE_DELIVERED)
    _mock_verdict(direct_vm, True, True)
    _resolve(direct_vm, contract, escrow_id)
    assert direct_vm.run_validator() is True

    dispute = contract.get_dispute(escrow_id)
    assert dispute["resolved"] is True
    assert dispute["delivered"] is True
    assert dispute["met_agreement"] is True
    assert dispute["worker_share"] == 100
    assert dispute["resolved_at"] > 0
    assert contract.get_escrow(escrow_id)["status"] == "RESOLVED"

    # A resolved dispute cannot be resolved or disputed again.
    with direct_vm.expect_revert("[EXPECTED] escrow has no open dispute"):
        contract.resolve_dispute(escrow_id)
    with direct_vm.expect_revert("[EXPECTED] escrow is not funded"):
        contract.open_dispute(escrow_id, "Again.", "")


def test_resolve_half_payment_when_delivered_but_not_met(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = direct_deploy("contracts/EscrowDisputeMediation.py")
    escrow_id = _setup_dispute(
        direct_vm, contract, direct_alice, direct_bob,
        opener=direct_alice,
        opener_statement="The page is up but email confirmation is missing.",
        opener_url=EVIDENCE_PARTIAL,
        responder=direct_bob,
        responder_statement="The form works; email was out of scope.",
        responder_url="",
    )

    _mock_evidence(direct_vm, EVIDENCE_PARTIAL, PAGE_PARTIAL)
    _mock_verdict(direct_vm, True, False)
    _resolve(direct_vm, contract, escrow_id)

    dispute = contract.get_dispute(escrow_id)
    assert dispute["delivered"] is True
    assert dispute["met_agreement"] is False
    assert dispute["worker_share"] == 50
    assert contract.get_escrow(escrow_id)["status"] == "RESOLVED"


def test_resolve_refund_when_not_delivered(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = direct_deploy("contracts/EscrowDisputeMediation.py")
    # The worker never responds: the round judges on the payer's case
    # and the agreement alone.
    escrow_id = _setup_dispute(
        direct_vm, contract, direct_alice, direct_bob,
        opener=direct_alice,
        opener_statement="Nothing was delivered.",
        opener_url=EVIDENCE_NOTHING,
    )

    _mock_evidence(direct_vm, EVIDENCE_NOTHING, PAGE_NOTHING)
    _mock_verdict(direct_vm, False, False)
    _resolve(direct_vm, contract, escrow_id)

    dispute = contract.get_dispute(escrow_id)
    assert dispute["delivered"] is False
    assert dispute["met_agreement"] is False
    assert dispute["worker_share"] == 0
    assert contract.get_escrow(escrow_id)["status"] == "RESOLVED"


def test_validator_disagrees_when_a_fact_differs(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = direct_deploy("contracts/EscrowDisputeMediation.py")
    escrow_id = _setup_dispute(
        direct_vm, contract, direct_alice, direct_bob,
        opener=direct_bob,
        opener_statement="I delivered the page.",
        opener_url=EVIDENCE_DELIVERED,
    )

    _mock_evidence(direct_vm, EVIDENCE_DELIVERED, PAGE_DELIVERED)
    _mock_verdict(direct_vm, True, True)
    _resolve(direct_vm, contract, escrow_id)

    # The validator reads the same evidence as not meeting the agreement.
    direct_vm.clear_mocks()
    _mock_evidence(direct_vm, EVIDENCE_DELIVERED, PAGE_DELIVERED)
    _mock_verdict(direct_vm, True, False)
    assert direct_vm.run_validator() is False


def test_resolve_fails_cleanly_when_evidence_unreachable(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = direct_deploy("contracts/EscrowDisputeMediation.py")
    escrow_id = _setup_dispute(
        direct_vm, contract, direct_alice, direct_bob,
        opener=direct_alice,
        opener_statement="Nothing was delivered.",
        opener_url=EVIDENCE_NOTHING,
    )

    # 404: the URL itself is wrong, no point retrying it.
    _mock_evidence_status(direct_vm, EVIDENCE_NOTHING, 404)
    _mock_verdict(direct_vm, False, False)
    snap = direct_vm.snapshot()
    with direct_vm.expect_revert("[EXTERNAL]"):
        contract.resolve_dispute(escrow_id)
    direct_vm.revert(snap)
    assert contract.get_dispute(escrow_id)["resolved"] is False

    # 429: the host is throttling, worth retrying later.
    direct_vm.clear_mocks()
    _mock_evidence_status(direct_vm, EVIDENCE_NOTHING, 429)
    _mock_verdict(direct_vm, False, False)
    snap = direct_vm.snapshot()
    with direct_vm.expect_revert("[TRANSIENT]"):
        contract.resolve_dispute(escrow_id)
    direct_vm.revert(snap)
    assert contract.get_dispute(escrow_id)["resolved"] is False


def test_resolve_fails_cleanly_when_llm_verdict_malformed(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = direct_deploy("contracts/EscrowDisputeMediation.py")
    escrow_id = _setup_dispute(
        direct_vm, contract, direct_alice, direct_bob,
        opener=direct_bob,
        opener_statement="I delivered the page.",
        opener_url=EVIDENCE_DELIVERED,
    )

    # delivered is not a boolean.
    _mock_evidence(direct_vm, EVIDENCE_DELIVERED, PAGE_DELIVERED)
    direct_vm.mock_llm(
        "mediating an escrow dispute",
        json.dumps({"delivered": "yes", "met": True, "rationale": "x"}),
    )
    snap = direct_vm.snapshot()
    with direct_vm.expect_revert("[LLM_ERROR]"):
        contract.resolve_dispute(escrow_id)
    direct_vm.revert(snap)
    assert contract.get_dispute(escrow_id)["resolved"] is False

    # met is missing entirely.
    direct_vm.clear_mocks()
    _mock_evidence(direct_vm, EVIDENCE_DELIVERED, PAGE_DELIVERED)
    direct_vm.mock_llm(
        "mediating an escrow dispute",
        json.dumps({"delivered": True, "rationale": "x"}),
    )
    snap = direct_vm.snapshot()
    with direct_vm.expect_revert("[LLM_ERROR]"):
        contract.resolve_dispute(escrow_id)
    direct_vm.revert(snap)
    assert contract.get_dispute(escrow_id)["resolved"] is False


def test_resolve_without_dispute_rejected(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    contract = direct_deploy("contracts/EscrowDisputeMediation.py")
    escrow_id = _create(direct_vm, contract, direct_alice, direct_bob)

    with direct_vm.expect_revert("[EXPECTED] escrow has no open dispute"):
        contract.resolve_dispute(escrow_id)
    with direct_vm.expect_revert("[EXPECTED] escrow has no dispute"):
        contract.get_dispute(escrow_id)
    with direct_vm.expect_revert("[EXPECTED] escrow does not exist"):
        contract.get_escrow(999)


def test_string_ids_accepted_from_studio_form(
    direct_vm, direct_deploy, direct_alice, direct_bob
):
    """Studio's write form sends numbers as JSON strings."""
    contract = direct_deploy("contracts/EscrowDisputeMediation.py")
    escrow_id = _create(direct_vm, contract, direct_alice, direct_bob)

    assert contract.get_escrow("1")["status"] == "FUNDED"
    direct_vm.sender = direct_alice
    contract.release_escrow("1")
    assert contract.get_escrow(escrow_id)["status"] == "RELEASED"


def test_views_and_stats(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/EscrowDisputeMediation.py")
    first = _create(direct_vm, contract, direct_alice, direct_bob)
    second = _create(direct_vm, contract, direct_alice, direct_bob, amount=ONE_GEN)

    _open_dispute(direct_vm, contract, direct_bob, first, "Delivered.", EVIDENCE_DELIVERED)
    _mock_evidence(direct_vm, EVIDENCE_DELIVERED, PAGE_DELIVERED)
    _mock_verdict(direct_vm, True, True)
    _resolve(direct_vm, contract, first)

    escrows = contract.list_escrows()
    assert len(escrows["escrows"]) == 2
    assert escrows["escrows"][0]["status"] == "RESOLVED"
    assert escrows["escrows"][1]["status"] == "FUNDED"

    assert contract.stats() == {
        "escrows": 2,
        "open_escrows": 1,
        "resolved_disputes": 1,
    }
