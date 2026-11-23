# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

"""EscrowDisputeMediation — escrowed work, settled by consensus.

A payer locks GEN in escrow for a job described by an agreement text
frozen at creation. The happy path needs no consensus at all: the
payer releases the escrow and the worker is paid in full, or the
payer cancels after the deadline and is refunded. Consensus only
enters when the two sides disagree — then either side opens a
dispute, each side states its case with one evidence URL, and
validator consensus judges two facts:

- delivered: was the deliverable provided at all?
- met_agreement: did what was provided meet the agreement as written?

The split follows deterministically from those two booleans, never
from a model output: delivered and met pays the worker 100%;
delivered but not meeting the agreement splits 50/50; not delivered
refunds the payer 100%. The model never outputs a funding decision,
and no model text is ever executed or treated as an instruction.

The design is deliberately two-sided. A dispute is not one party
asserting something to a judge: both sides can put a statement and
evidence on record, and the counterparty has 24 hours to answer.
Until it does, or that window passes, the dispute cannot be resolved
on the opener's case alone — the payout is never finalized from one
side's case while the other side is still waiting to be heard. After
the deadline the round can still be resolved if one side never
responds — silence is judged on the evidence that exists, not
rewarded with a default win. A dispute is resolved once; there is no
appeal, and the escrow cannot be cancelled while a dispute is open.

State machine, per escrow:

    FUNDED -> RELEASED (payer releases)
           -> CANCELLED (payer cancels after the deadline)
           -> DISPUTED -> RESOLVED (consensus settles the split)

Money rules: amounts are atto GEN (1 GEN = 10^18). create_escrow is
payable and the attached value is the full amount at stake. A payout
happens exactly once per escrow, and the two payouts of a split
always sum to the locked amount: the worker's share is computed by
integer division and the payer receives the remainder, so no dust is
ever left in the contract.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from genlayer import *


# Error prefixes. Failures are raised as gl.vm.UserError with one of
# these so the leader and the validators can tell deterministic failures
# apart from transient ones: [EXPECTED] and [EXTERNAL] must match
# exactly, [TRANSIENT] only has to be transient on both sides, and
# [LLM_ERROR] never agrees, so a bad model answer rotates the leader
# instead of locking bad state in.
ERR_EXPECTED = "[EXPECTED]"
ERR_EXTERNAL = "[EXTERNAL]"
ERR_TRANSIENT = "[TRANSIENT]"
ERR_LLM = "[LLM_ERROR]"


# Bounds. Statements and the agreement are plain text the validators
# read; they must stay bounded so a round stays comparable across
# validators. Evidence pages are truncated the same way for both sides.
MAX_AGREEMENT_CHARS = 2000
MAX_STATEMENT_CHARS = 2000
MAX_URL_CHARS = 500
MAX_EVIDENCE_CHARS = 6000

# How long the counterparty has to answer a dispute, in seconds.
# A dispute cannot be resolved while the other side is still inside
# this window and has not responded: the opener's case alone never
# settles the payout. After the deadline the round judges on the
# record as it stands, so silence cannot stall a dispute forever.
RESPONSE_WINDOW_SECONDS = u256(86400)

ESCROW_FUNDED = "FUNDED"
ESCROW_RELEASED = "RELEASED"
ESCROW_DISPUTED = "DISPUTED"
ESCROW_RESOLVED = "RESOLVED"
ESCROW_CANCELLED = "CANCELLED"


@allow_storage
@dataclass
class Escrow:
    """One funded job: who pays, who delivers, what was agreed."""

    escrow_id: u256
    payer: Address
    worker: Address
    agreement: str
    amount: u256
    # Unix seconds. After this time the payer may cancel an undisputed
    # escrow and reclaim the funds.
    deadline: u256
    status: str
    created_at: u256


@allow_storage
@dataclass
class Dispute:
    """One dispute over an escrow, with both sides' cases and the
    verdict record."""

    escrow_id: u256
    opener: Address
    # The payer's case. Exactly one of the two statements is filled by
    # open_dispute and the other by submit_response; a side that never
    # submits leaves its statement empty, and the round judges on what
    # exists.
    payer_statement: str
    payer_evidence: str
    worker_statement: str
    worker_evidence: str
    # The party that has not spoken yet, or the zero address once both
    # sides have submitted.
    awaiting: Address
    disputed_at: u256
    # Unix seconds. The counterparty must answer by this time; until
    # then the dispute cannot be resolved on the opener's case alone.
    response_deadline: u256
    resolved_at: u256
    # Verdict of the consensus round. The split is computed by
    # deterministic code from these two booleans, never copied from
    # model output.
    delivered: bool
    met_agreement: bool
    worker_share: u256
    rationale: str
    resolved: bool


def _fail(prefix: str, detail: str) -> None:
    raise gl.vm.UserError(prefix + " " + detail)


def _now_seconds() -> u256:
    """Current transaction time as unix seconds.

    The GenVM wires the standard library clock to the transaction
    datetime, so every validator observes the same value.
    """
    return u256(int(datetime.now(timezone.utc).timestamp()))


def _as_u256(value, label: str) -> u256:
    """Coerce one numeric argument to u256.

    Studio's write form serializes numbers as JSON strings, and the
    pinned py-genlayer's u256() is a pass-through (typing.NewType), so
    it does not convert them. int() normalizes both forms (a no-op on
    values that are already ints). A string that is not a whole number
    raises a clean [EXPECTED] revert here instead of letting a bare
    ValueError escape the contract.
    """
    try:
        return u256(int(value))
    except (TypeError, ValueError):
        raise gl.vm.UserError("[EXPECTED] " + label + " must be a whole number")


def _as_address(value, label: str) -> Address:
    """Coerce one address argument to a real Address object.

    Same class of problem as _as_u256: Studio's write form does not
    send an Address-typed argument through the native calldata address
    encoding, so the hex string typed into the form arrives in the
    method as a plain int, not an Address. The zero-address check does
    not catch that (an int never equals an Address), and the first
    storage write of the value then crashes in AddrDesc.set with
    AttributeError: 'int' object has no attribute 'as_bytes'. The
    contract normalizes the value itself, before any comparison or
    storage.

    An int is read as the standard big-endian hex-to-int value (the
    same number Python's int("0x...", 16) and JS BigInt produce), so
    int.to_bytes(20, "big") restores the address bytes. Note the SDK's
    Address.as_int is little-endian and is NOT used here: it is an
    internal quirk, while every wire convention (hex strings, the ABI
    codec, EVM uint160) is big-endian.

    A value that is not an address at all (wrong length, negative int,
    int past 2**160, garbage string) fails here as a clean [EXPECTED]
    revert instead of a bare exception escaping the contract. bool is
    rejected explicitly: it is an int subclass, and True/False must
    never silently become addresses 1 and 0.
    """
    if isinstance(value, Address):
        return value
    if isinstance(value, bool):
        raise gl.vm.UserError("[EXPECTED] " + label + " is not a valid address")
    if isinstance(value, int):
        if value < 0 or value >= (1 << (Address.SIZE * 8)):
            raise gl.vm.UserError("[EXPECTED] " + label + " is not a valid address")
        return Address(value.to_bytes(Address.SIZE, "big"))
    try:
        return Address(value)
    except Exception:
        raise gl.vm.UserError("[EXPECTED] " + label + " is not a valid address")


def _clean_text(value: str, field: str, max_chars: int) -> str:
    text = str(value).strip()
    if len(text) == 0:
        _fail(ERR_EXPECTED, field + " must not be empty")
    if len(text) > max_chars:
        _fail(ERR_EXPECTED, field + " must be " + str(max_chars) + " characters or fewer")
    return text


def _clean_evidence_url(url: str) -> str:
    """Validate one evidence URL, or accept an empty string for 'no
    evidence'. A side that has nothing to link is allowed to say so;
    the round then judges that side on its statement alone."""
    text = str(url).strip()
    if text == "":
        return ""
    if len(text) > MAX_URL_CHARS:
        _fail(ERR_EXPECTED, "evidence URL must be " + str(MAX_URL_CHARS) + " characters or fewer")
    if not (text.startswith("https://") or text.startswith("http://")):
        _fail(ERR_EXPECTED, "evidence URL must start with http:// or https://")
    return text


def _fetch_evidence_text(url: str, side: str) -> str:
    """Fetch one side's evidence page for a mediation round.

    Runs inside the leader and inside the validator, so both sides
    observe the same URL. Failures carry a stable prefix: 429 and 5xx
    are [TRANSIENT] (worth retrying later), anything else is
    [EXTERNAL] (the URL itself is wrong). Messages are fixed strings,
    never built from fetched content, so both consensus sides raise the
    identical error.
    """
    response = gl.nondet.web.get(url)
    if response.status == 429 or response.status >= 500:
        raise gl.vm.UserError("[TRANSIENT] " + side + " evidence host is temporarily unavailable")
    if response.status != 200:
        raise gl.vm.UserError("[EXTERNAL] " + side + " evidence URL did not return HTTP 200")
    if response.body is None or len(response.body) == 0:
        raise gl.vm.UserError("[EXTERNAL] " + side + " evidence URL returned an empty body")
    text = response.body.decode("utf-8", errors="replace")
    if len(text) > MAX_EVIDENCE_CHARS:
        text = text[:MAX_EVIDENCE_CHARS]
    return text


def _build_mediation_prompt(
    agreement: str,
    payer_statement: str,
    payer_evidence_text: str,
    worker_statement: str,
    worker_evidence_text: str,
) -> str:
    """Build the exact prompt both consensus sides run.

    The prompt is a pure function of the frozen agreement and the two
    sides' cases, so leader and validator judge the same question. It
    asks for two booleans only; it never asks for a funding decision,
    because the contract derives the split from the booleans itself.
    A side that submitted nothing is shown as such — silence is a fact
    about the record, not a default win for either side.
    """
    payer_case = payer_statement
    if payer_statement == "":
        payer_case = "(The payer submitted no statement.)"
    worker_case = worker_statement
    if worker_statement == "":
        worker_case = "(The worker submitted no statement.)"
    return (
        "You are mediating an escrow dispute. A payer locked funds for "
        "a job described in the agreement below. Judge ONLY the "
        "agreement, the two sides' statements, and the evidence pages. "
        "Do not use outside knowledge, and do not infer work that is "
        "not shown in the evidence.\n"
        "\n"
        "Agreement (frozen when the funds were locked):\n"
        + agreement
        + "\n\n"
        "Payer's statement:\n"
        + payer_case
        + "\n\n"
        "Payer's evidence page:\n"
        + payer_evidence_text
        + "\n\n"
        "Worker's statement:\n"
        + worker_case
        + "\n\n"
        "Worker's evidence page:\n"
        + worker_evidence_text
        + "\n\n"
        "Return ONLY a JSON object with exactly these fields:\n"
        '{"delivered": true, "met": true, "rationale": "one or two sentences"}.\n'
        "Rules: delivered is true only when the evidence shows the "
        "deliverable was actually provided. met is true only when what "
        "was provided meets the agreement as written; a partial "
        "delivery, or a delivery missing a named part of the agreement, "
        "means met is false. A side's bare assertion without evidence "
        "behind it does not make a fact true. rationale summarizes the "
        "judgement in one or two sentences."
    )


def _validate_verdict(verdict: dict) -> dict:
    """Check the model's verdict shape; return a normalized copy.

    Raises [LLM_ERROR] on any deviation: wrong top-level type, a
    non-boolean delivered or met flag, or a non-string rationale.
    Every message is a fixed string, never built from model output, so
    the error is identical on both consensus sides.
    """
    if not isinstance(verdict, dict):
        raise gl.vm.UserError("[LLM_ERROR] mediation verdict is not a JSON object")
    delivered = verdict.get("delivered")
    if not isinstance(delivered, bool):
        raise gl.vm.UserError("[LLM_ERROR] mediation verdict has no boolean delivered flag")
    met = verdict.get("met")
    if not isinstance(met, bool):
        raise gl.vm.UserError("[LLM_ERROR] mediation verdict has no boolean met flag")
    rationale = verdict.get("rationale", "")
    if not isinstance(rationale, str):
        raise gl.vm.UserError("[LLM_ERROR] mediation verdict rationale is not a string")
    return {"delivered": delivered, "met": met, "rationale": rationale}


def _error_text(exc) -> str:
    """Read the message out of a UserError / Result, across the runner
    versions that name the payload `.data` or `.message`."""
    for attr in ("data", "message"):
        value = getattr(exc, attr, None)
        if value is not None:
            return str(value)
    return str(exc)


def _leader_error_is_agreeable(leaders_res, judge) -> bool:
    """Decide whether to agree with a leader that failed. Deterministic
    failures (a bad argument, a 4xx) must match exactly. A transient
    failure only has to be transient on both sides. An LLM failure
    always disagrees, so consensus rotates rather than locking bad
    state."""
    leader_msg = _error_text(leaders_res)
    try:
        judge()
    except gl.vm.UserError as exc:
        validator_msg = _error_text(exc)
        if validator_msg.startswith(ERR_EXPECTED) or validator_msg.startswith(ERR_EXTERNAL):
            return validator_msg == leader_msg
        if validator_msg.startswith(ERR_TRANSIENT) and leader_msg.startswith(ERR_TRANSIENT):
            return True
        return False
    except Exception:
        return False
    # The leader failed where this validator succeeded: disagree.
    return False


# --------------------------------------------------------------------------
# Consensus worker (module level: it takes plain values, touches no
# storage, and is shared by leader and validators, so both sides run
# the same code).
# --------------------------------------------------------------------------
def _judge(
    agreement: str,
    payer_statement: str,
    payer_evidence: str,
    worker_statement: str,
    worker_evidence: str,
) -> dict:
    """One full mediation round: fetch the evidence pages, ask the
    model for the two booleans, validate. Shared by leader and
    validator so both sides run byte-identical logic. Returns the
    agreed facts; the split is derived from them by the caller."""
    payer_text = ""
    if payer_evidence != "":
        payer_text = _fetch_evidence_text(payer_evidence, "payer")
    worker_text = ""
    if worker_evidence != "":
        worker_text = _fetch_evidence_text(worker_evidence, "worker")
    prompt = _build_mediation_prompt(
        agreement, payer_statement, payer_text, worker_statement, worker_text
    )
    try:
        verdict = gl.nondet.exec_prompt(prompt, response_format="json")
    except Exception as exc:
        _fail(ERR_LLM, "model call failed: " + str(exc))
    return _validate_verdict(verdict)


def _escrow_to_dict(escrow: Escrow) -> dict:
    return {
        "escrow_id": escrow.escrow_id,
        "payer": escrow.payer.as_hex,
        "worker": escrow.worker.as_hex,
        "agreement": escrow.agreement,
        "amount": escrow.amount,
        "deadline": escrow.deadline,
        "status": escrow.status,
        "created_at": escrow.created_at,
        "found": True,
    }


def _dispute_to_dict(dispute: Dispute) -> dict:
    return {
        "escrow_id": dispute.escrow_id,
        "opener": dispute.opener.as_hex,
        "payer_statement": dispute.payer_statement,
        "payer_evidence": dispute.payer_evidence,
        "worker_statement": dispute.worker_statement,
        "worker_evidence": dispute.worker_evidence,
        "awaiting": dispute.awaiting.as_hex,
        "disputed_at": dispute.disputed_at,
        "response_deadline": dispute.response_deadline,
        "resolved_at": dispute.resolved_at,
        "delivered": dispute.delivered,
        "met_agreement": dispute.met_agreement,
        "worker_share": dispute.worker_share,
        "rationale": dispute.rationale,
        "resolved": dispute.resolved,
        "found": True,
    }


class EscrowDisputeMediation(gl.Contract):
    """Escrow for a job, with consensus mediation when the sides
    disagree.

    Escrows are created once, disputed at most once, and settled
    exactly once. The dispute record keeps both sides' cases and the
    verdict, so the settlement is auditable after the fact.
    """

    escrows: TreeMap[u256, Escrow]
    disputes: TreeMap[u256, Dispute]
    escrow_ids: DynArray[u256]
    escrow_counter: u256

    def __init__(self):
        self.escrow_counter = u256(0)

    def _require_escrow(self, escrow_id: u256) -> Escrow:
        if escrow_id not in self.escrows:
            raise gl.vm.UserError("[EXPECTED] escrow does not exist")
        return self.escrows[escrow_id]

    def _pay_split(self, escrow: Escrow, worker_share: u256) -> None:
        """Pay the escrow out by the agreed split.

        The worker's amount is integer division of the locked amount
        by the share; the payer receives the remainder, so the two
        payouts always sum to the locked amount exactly. Zero legs are
        not emitted: a 0% share means that side is paid nothing.
        """
        worker_amount = escrow.amount * worker_share // u256(100)
        payer_amount = escrow.amount - worker_amount
        if worker_amount > u256(0):
            gl.get_contract_at(escrow.worker).emit_transfer(value=worker_amount)
        if payer_amount > u256(0):
            gl.get_contract_at(escrow.payer).emit_transfer(value=payer_amount)

    @gl.public.write.payable
    def create_escrow(self, worker: Address, agreement: str, deadline: u256) -> u256:
        """Create an escrow and lock its full funding in the contract.

        The caller is the payer. The agreement text is frozen at
        creation: it is the document consensus judges a later dispute
        against, so nothing in this contract can edit it afterwards.
        The attached value is the full amount at stake. Returns the new
        escrow id.
        """
        deadline = _as_u256(deadline, "deadline")
        # The worker arrives from Studio's form as a plain int, not an
        # Address (see _as_address); normalize before the zero check,
        # or the check silently passes and the Escrow record write
        # crashes.
        worker = _as_address(worker, "worker")
        if worker == Address(bytes(20)):
            raise gl.vm.UserError("[EXPECTED] worker must not be the zero address")
        if worker == gl.message.sender_address:
            raise gl.vm.UserError("[EXPECTED] payer and worker must be different addresses")
        clean_agreement = _clean_text(agreement, "agreement", MAX_AGREEMENT_CHARS)
        if _now_seconds() >= deadline:
            raise gl.vm.UserError("[EXPECTED] deadline must be in the future")
        if gl.message.value == u256(0):
            raise gl.vm.UserError("[EXPECTED] escrow amount must be greater than zero")
        self.escrow_counter = self.escrow_counter + u256(1)
        escrow_id = self.escrow_counter
        self.escrows[escrow_id] = Escrow(
            escrow_id=escrow_id,
            payer=gl.message.sender_address,
            worker=worker,
            agreement=clean_agreement,
            amount=gl.message.value,
            deadline=deadline,
            status=ESCROW_FUNDED,
            created_at=_now_seconds(),
        )
        self.escrow_ids.append(escrow_id)
        return escrow_id

    @gl.public.write
    def release_escrow(self, escrow_id: u256) -> None:
        """Release the escrow to the worker in full.

        Only the payer may call this, and only while the escrow is
        FUNDED. This is the happy path: no dispute, no consensus, the
        money moves because the payer says the job is done.
        """
        escrow_id = _as_u256(escrow_id, "escrow id")
        escrow = self._require_escrow(escrow_id)
        if gl.message.sender_address != escrow.payer:
            raise gl.vm.UserError("[EXPECTED] only the payer can release the escrow")
        if escrow.status != ESCROW_FUNDED:
            raise gl.vm.UserError("[EXPECTED] escrow is not funded")
        escrow.status = ESCROW_RELEASED
        self._pay_split(escrow, u256(100))

    @gl.public.write
    def cancel_escrow(self, escrow_id: u256) -> None:
        """Cancel an undisputed escrow after its deadline and refund
        the payer in full.

        Only the payer may call this, and only while the escrow is
        FUNDED and the deadline has passed. The worker's protection
        against a stalling payer is the dispute path: open a dispute
        before the deadline and the escrow can no longer be cancelled.
        """
        escrow_id = _as_u256(escrow_id, "escrow id")
        escrow = self._require_escrow(escrow_id)
        if gl.message.sender_address != escrow.payer:
            raise gl.vm.UserError("[EXPECTED] only the payer can cancel the escrow")
        if escrow.status != ESCROW_FUNDED:
            raise gl.vm.UserError("[EXPECTED] escrow is not funded")
        if _now_seconds() < escrow.deadline:
            raise gl.vm.UserError("[EXPECTED] escrow cannot be cancelled before its deadline")
        escrow.status = ESCROW_CANCELLED
        self._pay_split(escrow, u256(0))

    @gl.public.write
    def open_dispute(self, escrow_id: u256, statement: str, evidence_url: str) -> None:
        """Open a dispute over a funded escrow.

        Either the payer or the worker may call this. The opener's
        statement and evidence go on record under the opener's side;
        the other side is then awaited and may answer with
        submit_response. A dispute can be opened only once per escrow.
        """
        escrow_id = _as_u256(escrow_id, "escrow id")
        escrow = self._require_escrow(escrow_id)
        sender = gl.message.sender_address
        if sender != escrow.payer and sender != escrow.worker:
            raise gl.vm.UserError("[EXPECTED] only the payer or the worker can open a dispute")
        if escrow.status != ESCROW_FUNDED:
            raise gl.vm.UserError("[EXPECTED] escrow is not funded")
        clean_statement = _clean_text(statement, "statement", MAX_STATEMENT_CHARS)
        clean_evidence = _clean_evidence_url(evidence_url)
        payer_statement = ""
        payer_evidence = ""
        worker_statement = ""
        worker_evidence = ""
        awaiting = escrow.payer
        if sender == escrow.payer:
            payer_statement = clean_statement
            payer_evidence = clean_evidence
            awaiting = escrow.worker
        else:
            worker_statement = clean_statement
            worker_evidence = clean_evidence
            awaiting = escrow.payer
        self.disputes[escrow_id] = Dispute(
            escrow_id=escrow_id,
            opener=sender,
            payer_statement=payer_statement,
            payer_evidence=payer_evidence,
            worker_statement=worker_statement,
            worker_evidence=worker_evidence,
            awaiting=awaiting,
            disputed_at=_now_seconds(),
            response_deadline=_now_seconds() + RESPONSE_WINDOW_SECONDS,
            resolved_at=u256(0),
            delivered=False,
            met_agreement=False,
            worker_share=u256(0),
            rationale="",
            resolved=False,
        )
        escrow.status = ESCROW_DISPUTED

    @gl.public.write
    def submit_response(self, escrow_id: u256, statement: str, evidence_url: str) -> None:
        """Answer a dispute from the side that has not spoken yet.

        Only the awaited party may call this, and only once. If that
        party never responds, the dispute can still be resolved once
        the 24-hour response deadline passes: resolve_dispute then
        judges on the record as it stands.
        """
        escrow_id = _as_u256(escrow_id, "escrow id")
        escrow = self._require_escrow(escrow_id)
        if escrow.status != ESCROW_DISPUTED:
            raise gl.vm.UserError("[EXPECTED] escrow has no open dispute")
        dispute = self.disputes[escrow_id]
        if gl.message.sender_address != dispute.awaiting:
            raise gl.vm.UserError("[EXPECTED] only the awaited party can respond to the dispute")
        clean_statement = _clean_text(statement, "statement", MAX_STATEMENT_CHARS)
        clean_evidence = _clean_evidence_url(evidence_url)
        if dispute.awaiting == escrow.payer:
            dispute.payer_statement = clean_statement
            dispute.payer_evidence = clean_evidence
        else:
            dispute.worker_statement = clean_statement
            dispute.worker_evidence = clean_evidence
        dispute.awaiting = Address(bytes(20))

    @gl.public.write
    def resolve_dispute(self, escrow_id: u256) -> None:
        """Run validator consensus on an open dispute and settle it.

        Anyone may call this: the caller decides nothing, consensus
        does. Resolution is gated: while the counterparty is still
        awaited inside its 24-hour response window, the call reverts
        — either the response arrives or the deadline passes first.
        The leader and the validator each fetch both sides'
        evidence and judge the same two facts; the round only finalizes
        when both booleans match. After consensus, the split is
        computed by deterministic code from the agreed booleans and
        paid out immediately. A resolved dispute cannot be reopened.
        """
        escrow_id = _as_u256(escrow_id, "escrow id")
        escrow = self._require_escrow(escrow_id)
        if escrow.status != ESCROW_DISPUTED:
            raise gl.vm.UserError("[EXPECTED] escrow has no open dispute")
        dispute = self.disputes[escrow_id]
        if dispute.resolved:
            raise gl.vm.UserError("[EXPECTED] dispute is already resolved")

        # The counterparty gets its say. While it is still awaited
        # inside its 24-hour response window, the opener's case alone
        # cannot settle the payout: either the response arrives, or
        # the deadline passes and the round judges on the record as
        # it stands.
        if (
            dispute.awaiting != Address(bytes(20))
            and _now_seconds() < dispute.response_deadline
        ):
            raise gl.vm.UserError(
                "[EXPECTED] dispute cannot be resolved before the "
                "counterparty responds or the response deadline passes"
            )

        # Copy the records out of storage before the nondet block. The
        # leader and validator run in separate VMs and cannot touch
        # storage; they only ever see these plain values. Nothing here
        # references self, so the closures stay serializable.
        escrow_mem = gl.storage.copy_to_memory(escrow)
        dispute_mem = gl.storage.copy_to_memory(dispute)
        agreement = escrow_mem.agreement
        payer_statement = dispute_mem.payer_statement
        payer_evidence = dispute_mem.payer_evidence
        worker_statement = dispute_mem.worker_statement
        worker_evidence = dispute_mem.worker_evidence

        def leader_fn() -> dict:
            return _judge(
                agreement,
                payer_statement,
                payer_evidence,
                worker_statement,
                worker_evidence,
            )

        def validator_fn(result: gl.vm.Result) -> bool:
            if not isinstance(result, gl.vm.Return):
                return _leader_error_is_agreeable(
                    result,
                    lambda: _judge(
                        agreement,
                        payer_statement,
                        payer_evidence,
                        worker_statement,
                        worker_evidence,
                    ),
                )
            try:
                own = _judge(
                    agreement,
                    payer_statement,
                    payer_evidence,
                    worker_statement,
                    worker_evidence,
                )
            except gl.vm.UserError:
                return False
            except Exception:
                # Fail closed: any unexpected validator-side failure
                # counts as disagreement, never as agreement.
                return False
            if not isinstance(result.calldata, dict):
                return False
            leader = result.calldata
            # Both facts must match exactly. These booleans decide
            # where the money goes, so there is no tolerance here.
            return (
                own["delivered"] == leader.get("delivered")
                and own["met"] == leader.get("met")
            )

        verdict = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        # Consensus agreed. From here on everything is deterministic:
        # the two booleans decide the split, and no raw model text is
        # ever read as an instruction.
        delivered = bool(verdict["delivered"])
        met = bool(verdict["met"])
        if delivered and met:
            worker_share = u256(100)
        elif delivered:
            worker_share = u256(50)
        else:
            worker_share = u256(0)
        dispute.delivered = delivered
        dispute.met_agreement = met
        dispute.worker_share = worker_share
        dispute.rationale = str(verdict["rationale"])
        dispute.resolved_at = _now_seconds()
        dispute.resolved = True
        escrow.status = ESCROW_RESOLVED
        self._pay_split(escrow, worker_share)

    @gl.public.view
    def get_escrow(self, escrow_id: u256) -> dict:
        escrow_id = _as_u256(escrow_id, "escrow id")
        return _escrow_to_dict(self._require_escrow(escrow_id))

    @gl.public.view
    def get_dispute(self, escrow_id: u256) -> dict:
        escrow_id = _as_u256(escrow_id, "escrow id")
        if escrow_id not in self.disputes:
            raise gl.vm.UserError("[EXPECTED] escrow has no dispute")
        return _dispute_to_dict(self.disputes[escrow_id])

    @gl.public.view
    def list_escrows(self) -> dict:
        return {"escrows": [_escrow_to_dict(self.escrows[eid]) for eid in self.escrow_ids]}

    @gl.public.view
    def stats(self) -> dict:
        funded = u256(0)
        resolved = u256(0)
        for escrow_id in self.escrow_ids:
            status = self.escrows[escrow_id].status
            if status == ESCROW_FUNDED or status == ESCROW_DISPUTED:
                funded = funded + u256(1)
            if status == ESCROW_RESOLVED:
                resolved = resolved + u256(1)
        return {
            "escrows": u256(len(self.escrow_ids)),
            "open_escrows": funded,
            "resolved_disputes": resolved,
        }
