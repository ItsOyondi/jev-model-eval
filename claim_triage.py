"""Triage a claim file with TypeSafe and send risky or uncertain cases to a person.

One system_one request classifies the first notice of loss, the adjuster notes,
and each supporting document. Separate questions flag complexity, missing facts,
and fraud indicators. Routing then uses those answers plus TypeSafe confidence:
straight-through processing only when the file is simple, complete, and the
model is sure. Anything uncertain or high-risk goes to a human adjuster.
"""

from dataclasses import dataclass
import os
from typing import Literal

from dotenv import load_dotenv
from typesafe_sdk import Choice, Noul, Score, SystemOneResponse, TypeSafeClient

Route = Literal["straight_through", "specialist_review", "human_adjuster"]

# Choice and Score confidence: act only when the distribution is peaked.
# Review in the middle band, and escalate when it is spread out.
# https://docs.typesafe.ai/confidence
ACT_CONFIDENCE = 0.85
REVIEW_CONFIDENCE = 0.6

# A Noul is a yes-probability. Near 0.5 is unsure. Fraud indicators use a
# lower yes bar because missing a real indicator is the costly mistake.
# https://docs.typesafe.ai/primitives/noul
FRAUD_FLAG = 0.7
FRAUD_UNSURE_LOW = 0.4
FACT_PRESENT = 0.7
FACT_ABSENT = 0.4

LOSS_TYPES = {
    "auto_collision": "Impact between vehicles or a vehicle and an object.",
    "auto_comprehensive": "Theft, weather, animal, vandalism, or other non-collision auto loss.",
    "property": "Damage to a home, building, or personal property.",
    "liability": "The insured may owe someone else for injury or damage.",
    "bodily_injury": "Someone was hurt and treatment or injury is part of the claim.",
    "other": "Does not fit the types above.",
}

NOTE_PURPOSES = {
    "coverage": "Whether the policy responds to this loss.",
    "liability": "Who is responsible for the loss.",
    "damages": "The extent or cost of the loss.",
    "investigation": "Facts that are still being gathered.",
    "settlement": "Payment, negotiation, or resolution.",
    "none": "There are no adjuster notes.",
}

DOCUMENT_TYPES = {
    "police_report": "Official incident or police report.",
    "repair_estimate": "Shop or contractor estimate of repair cost.",
    "medical_record": "Treatment notes, bills, or injury records.",
    "photo_or_scene": "Photos or a description of the scene.",
    "invoice_or_receipt": "Bill, invoice, or proof of payment.",
    "correspondence": "Email, letter, or message about the claim.",
    "other": "Does not match the types above.",
}

HANDLING = {
    "straight_through": "Simple, complete, and consistent enough for automated handling.",
    "specialist_review": "A specialist should review coverage, damages, or an open question.",
    "human_adjuster": "A human adjuster should take the file before any payment decision.",
}

REQUIRED_FACTS = {
    "date_of_loss_stated": "The file states when the loss happened.",
    "location_stated": "The file states where the loss happened.",
    "loss_description_stated": "The file describes what happened.",
    "parties_stated": "The file identifies who was involved.",
    "policy_or_insured_stated": "The file identifies the policy or the insured.",
    "reporter_contact_stated": "The file gives a way to reach the person who reported the loss.",
}

FRAUD_INDICATORS = {
    "story_inconsistency": "The FNOL, notes, and documents disagree about material facts.",
    "early_payment_pressure": "The reporter presses for payment before the facts are established.",
    "documents_conflict_with_loss": "A supporting document contradicts the reported loss.",
    "unusual_timing": "The report, the loss date, or the documents line up in an unusual way.",
}


@dataclass(frozen=True)
class ClaimDocument:
    name: str
    text: str


@dataclass(frozen=True)
class ClaimFile:
    claim_id: str
    fnol: str
    adjuster_notes: str
    documents: tuple[ClaimDocument, ...] = ()


@dataclass(frozen=True)
class ClassifiedDocument:
    name: str
    document_type: str
    confidence: float


@dataclass(frozen=True)
class TriageDecision:
    claim_id: str
    loss_type: str
    loss_type_confidence: float
    note_purpose: str
    note_purpose_confidence: float
    documents: tuple[ClassifiedDocument, ...]
    complexity: float
    complexity_confidence: float
    missing_information: tuple[str, ...]
    fraud_indicators: tuple[str, ...]
    uncertain_signals: tuple[str, ...]
    suggested_handling: str
    suggested_confidence: float
    route: Route
    reasons: tuple[str, ...]


def triage_claim(client: TypeSafeClient, claim: ClaimFile) -> TriageDecision:
    response = client.system_one(
        state=_claim_state(claim),
        questions=_questions(claim),
    )
    return _decision(claim, response)


def _claim_state(claim: ClaimFile) -> dict[str, object]:
    return {
        "claim_id": claim.claim_id,
        "fnol": claim.fnol,
        "adjuster_notes": claim.adjuster_notes,
        "documents": [
            {"name": document.name, "text": document.text}
            for document in claim.documents
        ],
    }


def _questions(claim: ClaimFile) -> dict[str, Choice | Noul | Score]:
    questions: dict[str, Choice | Noul | Score] = {
        "loss_type": Choice(
            instructions="What kind of loss does `fnol` describe?",
            criteria=LOSS_TYPES,
        ),
        "note_purpose": Choice(
            instructions="What is the main purpose of `adjuster_notes`?",
            criteria=NOTE_PURPOSES,
        ),
        "complexity": Score(
            instructions="How complex is this claim across `fnol`, `adjuster_notes`, and `documents`?",
            criteria=[
                "One clear incident, no injury, and no coverage dispute.",
                "Several parties, open damages, or a coverage question.",
                "Injury, conflicting facts, litigation, or a coverage dispute.",
            ],
        ),
        "suggested_handling": Choice(
            instructions="How should this claim be handled before any payment?",
            criteria=HANDLING,
        ),
    }
    for name, instruction in REQUIRED_FACTS.items():
        questions[name] = Noul(
            instructions=instruction,
            criteria={
                "true": "The fact is stated clearly in the file.",
                "false": "The fact is missing or too vague to use.",
            },
        )
    for name, instruction in FRAUD_INDICATORS.items():
        questions[name] = Noul(
            instructions=instruction,
            criteria={
                "true": "The file contains this indicator.",
                "false": "The file does not contain this indicator.",
            },
        )
    for index, document in enumerate(claim.documents):
        questions[f"document_{index}_type"] = Choice(
            instructions=f"What kind of document is `documents[{index}]` ({document.name})?",
            criteria=DOCUMENT_TYPES,
        )
    return questions


def _decision(claim: ClaimFile, response: SystemOneResponse) -> TriageDecision:
    loss = response.choices["loss_type"]
    notes = response.choices["note_purpose"]
    complexity = response.scores["complexity"]
    suggested = response.choices["suggested_handling"]

    documents = tuple(
        ClassifiedDocument(
            name=document.name,
            document_type=response.choices[f"document_{index}_type"].choice,
            confidence=response.choices[f"document_{index}_type"].confidence,
        )
        for index, document in enumerate(claim.documents)
    )
    missing = tuple(
        name for name in REQUIRED_FACTS if response.nouls[name].noul <= FACT_ABSENT
    )
    indicators = tuple(
        name for name in FRAUD_INDICATORS if response.nouls[name].noul >= FRAUD_FLAG
    )
    uncertain = _uncertain_signals(response, claim)
    route, reasons = _route(
        suggested_handling=suggested.choice,
        suggested_confidence=suggested.confidence,
        complexity=complexity.score,
        complexity_confidence=complexity.confidence,
        missing=missing,
        indicators=indicators,
        uncertain=uncertain,
        documents=documents,
    )
    return TriageDecision(
        claim_id=claim.claim_id,
        loss_type=loss.choice,
        loss_type_confidence=loss.confidence,
        note_purpose=notes.choice,
        note_purpose_confidence=notes.confidence,
        documents=documents,
        complexity=complexity.score,
        complexity_confidence=complexity.confidence,
        missing_information=missing,
        fraud_indicators=indicators,
        uncertain_signals=uncertain,
        suggested_handling=suggested.choice,
        suggested_confidence=suggested.confidence,
        route=route,
        reasons=reasons,
    )


def _uncertain_signals(response: SystemOneResponse, claim: ClaimFile) -> tuple[str, ...]:
    signals: list[str] = []
    if response.choices["loss_type"].confidence < REVIEW_CONFIDENCE:
        signals.append("loss_type")
    if response.choices["note_purpose"].confidence < REVIEW_CONFIDENCE:
        signals.append("note_purpose")
    if response.scores["complexity"].confidence < REVIEW_CONFIDENCE:
        signals.append("complexity")
    if response.choices["suggested_handling"].confidence < REVIEW_CONFIDENCE:
        signals.append("suggested_handling")
    for name in REQUIRED_FACTS:
        probability = response.nouls[name].noul
        if FACT_ABSENT < probability < FACT_PRESENT:
            signals.append(name)
    for name in FRAUD_INDICATORS:
        probability = response.nouls[name].noul
        if FRAUD_UNSURE_LOW <= probability < FRAUD_FLAG:
            signals.append(name)
    for index in range(len(claim.documents)):
        answer = response.choices[f"document_{index}_type"]
        if answer.confidence < REVIEW_CONFIDENCE:
            signals.append(f"document_{index}_type")
    return tuple(signals)


def _route(
    *,
    suggested_handling: str,
    suggested_confidence: float,
    complexity: float,
    complexity_confidence: float,
    missing: tuple[str, ...],
    indicators: tuple[str, ...],
    uncertain: tuple[str, ...],
    documents: tuple[ClassifiedDocument, ...],
) -> tuple[Route, tuple[str, ...]]:
    """Upgrade the model's suggestion. Uncertainty never lowers a more cautious route."""
    reasons: list[str] = []
    route = _as_route(suggested_handling)
    reasons.append(f"model suggested {suggested_handling}")

    if suggested_confidence < REVIEW_CONFIDENCE:
        route = _raise(route, "human_adjuster")
        reasons.append("handling suggestion is uncertain")
    elif suggested_confidence < ACT_CONFIDENCE and route == "straight_through":
        route = "specialist_review"
        reasons.append("straight-through suggestion needs confirmation")

    if complexity >= 2:
        route = _raise(route, "human_adjuster")
        reasons.append("claim complexity needs an adjuster")
    elif complexity >= 1:
        route = _raise(route, "specialist_review")
        reasons.append("claim is more than a simple loss")
    if complexity_confidence < REVIEW_CONFIDENCE:
        route = _raise(route, "human_adjuster")
        reasons.append("complexity score is uncertain")

    if missing:
        route = _raise(route, "specialist_review")
        reasons.append("required information is missing: " + ", ".join(missing))

    unsure_indicators = tuple(signal for signal in uncertain if signal in FRAUD_INDICATORS)
    if indicators or unsure_indicators:
        route = _raise(route, "human_adjuster")
        reasons.append(
            "fraud indicators need an adjuster: " + ", ".join(indicators or unsure_indicators)
        )

    weak_documents = tuple(
        document.name for document in documents if document.confidence < REVIEW_CONFIDENCE
    )
    if weak_documents:
        route = _raise(route, "human_adjuster")
        reasons.append("document type is uncertain: " + ", ".join(weak_documents))

    other_uncertain = tuple(
        signal
        for signal in uncertain
        if signal not in {"complexity", "suggested_handling"}
        and signal not in FRAUD_INDICATORS
        and not signal.startswith("document_")
    )
    if other_uncertain:
        route = _raise(route, "human_adjuster")
        reasons.append("uncertain answers need an adjuster: " + ", ".join(other_uncertain))

    if route == "straight_through" and complexity_confidence < ACT_CONFIDENCE:
        route = "specialist_review"
        reasons.append("straight-through processing requires higher confidence")

    return route, tuple(reasons)


def _as_route(value: str) -> Route:
    if value == "human_adjuster":
        return "human_adjuster"
    if value == "specialist_review":
        return "specialist_review"
    return "straight_through"


def _raise(current: Route, proposed: Route) -> Route:
    rank = {"straight_through": 0, "specialist_review": 1, "human_adjuster": 2}
    return proposed if rank[proposed] > rank[current] else current


def format_decision(decision: TriageDecision) -> str:
    documents = ", ".join(
        f"{item.name}={item.document_type} ({item.confidence:.2f})"
        for item in decision.documents
    ) or "none"
    missing = ", ".join(decision.missing_information) or "none"
    indicators = ", ".join(decision.fraud_indicators) or "none"
    uncertain = ", ".join(decision.uncertain_signals) or "none"
    reasons = "\n".join(f"- {reason}" for reason in decision.reasons)
    return "\n".join(
        [
            f"claim: {decision.claim_id}",
            f"route: {decision.route}",
            f"loss_type: {decision.loss_type} ({decision.loss_type_confidence:.2f})",
            f"note_purpose: {decision.note_purpose} ({decision.note_purpose_confidence:.2f})",
            f"documents: {documents}",
            f"complexity: {decision.complexity:.2f} ({decision.complexity_confidence:.2f})",
            f"missing_information: {missing}",
            f"fraud_indicators: {indicators}",
            f"uncertain_signals: {uncertain}",
            f"suggested_handling: {decision.suggested_handling} ({decision.suggested_confidence:.2f})",
            "reasons:",
            reasons,
        ]
    )


def main() -> None:
    load_dotenv()
    claim = ClaimFile(
        claim_id="CLM-1001",
        fnol=(
            "Insured Jane Doe, policy ABC-100, reported a rear-end collision on "
            "October 1, 2026 at Main Street and 5th. The other driver stayed at"
            "the scene. No injuries reported. "
            "The driver stayed at the scene and admitted fault. Reporter phone is 555-0100."
        ),
        adjuster_notes=(
            "Coverage appears to apply to a rear-end collision. Waiting on the "
            "other carrier. The repair estimate has not been compared with photos. "
            "The other driver admitted fault and stayed at the scene. "
            "The police documented the scene and noted that the other driver stayed at the scene and admitted fault."
        ),
        documents=(
            ClaimDocument(
                name="police-report.txt",
                text="Unit 1 rear-ended unit 2 while stopped. No injuries. Both drivers remained.",
            ),
            ClaimDocument(
                name="estimate.txt",
                text="Bumper cover and hatch repair. Estimated cost 2400 dollars.",
            ),
        ),
    )
    with TypeSafeClient(api_key=os.getenv("TYPESAFE_API_KEY")) as client:
        decision = triage_claim(client, claim)
    print(format_decision(decision))


if __name__ == "__main__":
    main()
