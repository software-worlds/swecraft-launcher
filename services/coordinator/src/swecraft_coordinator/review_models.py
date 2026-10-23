"""Strict review response and manifest models.

End-of-run reviews are the coordinator's third kind of special-purpose agent
turn (after onboarding's bootstrap): a finite, read-only turn whose only
product is one structured JSON answer, validated host-side against these
models. The manifest (`reviews.yaml`) is experiment-owned: it names the
evaluator and society rosters. Nothing else is staged into a turn — no
evidence dossiers exist in this design: the reviewer works from its own
memory, repository, and forge browsing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from swecraft_coordinator import CoordinatorError

Rating = int

CONTRIBUTION_TYPES = (
    "produced useful code",
    "identified a problem",
    "identified an opportunity",
    "supplied important information",
    "connected previously disconnected information",
    "unblocked another agent",
    "prevented incorrect work",
    "reduced duplicated work",
    "coordinated dependencies",
    "improved testing / evaluation",
    "improved design / architecture",
    "improved documentation",
    "other",
)

BOTTLENECKS = (
    "finding relevant information",
    "finding the correct agent",
    "unclear ownership",
    "task discovery",
    "task allocation",
    "prioritization",
    "communication overhead",
    "too much irrelevant communication",
    "waiting for other agents",
    "cross-repository dependencies",
    "duplicate work",
    "conflicting simultaneous work",
    "issue / PR discovery",
    "lack of global society state",
    "memory / context limitations",
    "tool access",
    "testing infrastructure",
    "evaluation infrastructure",
    "resource / compute allocation",
    "other",
)

IMPROVEMENT_CATEGORIES = (
    "agent behavior / prompting",
    "communication protocol",
    "shared context / memory",
    "agent discovery / routing",
    "task discovery",
    "task allocation",
    "dependency representation",
    "issue / PR infrastructure",
    "testing / evaluation",
    "resource allocation",
    "agent specialization",
    "society governance",
    "other",
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PeerReflection(StrictModel):
    consequential_contributions: tuple[str, ...] = Field(min_length=1, max_length=3)
    most_valuable_contribution: str = Field(min_length=1)
    biggest_limitation: str = Field(min_length=1)


class RelevanceScores(StrictModel):
    need_alignment: Rating = Field(ge=1, le=5)
    evidence_of_value: Rating = Field(ge=1, le=5)
    marginal_usefulness: Rating = Field(ge=1, le=5)
    overall: Rating = Field(ge=1, le=5)


class CollaborationScores(StrictModel):
    information_value: Rating = Field(ge=1, le=5)
    accuracy_reliability: Rating = Field(ge=1, le=5)
    coordination_effectiveness: Rating = Field(ge=1, le=5)
    context_integration: Rating = Field(ge=1, le=5)
    overall: Rating = Field(ge=1, le=5)


class ContributionQualityScores(StrictModel):
    correctness_robustness: Rating = Field(ge=1, le=5)
    maintainability: Rating = Field(ge=1, le=5)
    scope_discipline: Rating = Field(ge=1, le=5)
    overall: Rating = Field(ge=1, le=5)


class PeerReview(StrictModel):
    run_id: str = Field(min_length=1)
    evaluator_agent: str = Field(min_length=1)
    target_agent: str = Field(min_length=1)
    exposure: Rating | None = Field(default=None, ge=1, le=5)
    insufficient_evidence: bool = False
    insufficient_evidence_reason: str | None = None
    reflection: PeerReflection | None = None
    relevance: RelevanceScores | None = None
    collaboration: CollaborationScores | None = None
    contribution_quality: ContributionQualityScores | None = None
    marginal_impact: Rating | None = Field(default=None, ge=1, le=5)
    contribution_types: tuple[str, ...] = ()
    confidence: Rating | None = Field(default=None, ge=1, le=5)

    @model_validator(mode="after")
    def complete_or_insufficient(self) -> PeerReview:
        if self.evaluator_agent == self.target_agent:
            raise ValueError("peer review cannot target the evaluator")
        if self.insufficient_evidence:
            if not (self.insufficient_evidence_reason or "").strip():
                raise ValueError("insufficient_evidence requires a reason")
            supplied = [
                name
                for name in (
                    "exposure",
                    "reflection",
                    "relevance",
                    "collaboration",
                    "contribution_quality",
                    "marginal_impact",
                    "confidence",
                )
                if getattr(self, name) is not None
            ]
            if self.contribution_types:
                supplied.append("contribution_types")
            if supplied:
                raise ValueError(
                    "insufficient_evidence cannot include: " + ", ".join(supplied)
                )
            return self
        missing = [
            name
            for name in (
                "exposure",
                "reflection",
                "relevance",
                "collaboration",
                "contribution_quality",
                "marginal_impact",
                "confidence",
            )
            if getattr(self, name) is None
        ]
        if missing:
            raise ValueError(f"complete peer reviews are missing: {', '.join(missing)}")
        if not self.contribution_types:
            raise ValueError("complete peer reviews require contribution_types")
        unknown = set(self.contribution_types) - set(CONTRIBUTION_TYPES)
        if unknown:
            raise ValueError(
                "unknown contribution_types: " + ", ".join(sorted(unknown))
            )
        return self


class SocietyRatings(StrictModel):
    task_discovery: Rating = Field(ge=1, le=5)
    information_accessibility: Rating = Field(ge=1, le=5)
    ownership_clarity: Rating = Field(ge=1, le=5)
    agent_discovery_routing: Rating = Field(ge=1, le=5)
    coordination_efficiency: Rating = Field(ge=1, le=5)
    redundancy_avoidance: Rating = Field(ge=1, le=5)
    dependency_handling: Rating = Field(ge=1, le=5)
    infrastructure_adequacy: Rating = Field(ge=1, le=5)
    overall_society_effectiveness: Rating = Field(ge=1, le=5)


class Occurrence(StrictModel):
    occurred: bool | None
    description: str | None = None

    @model_validator(mode="after")
    def description_when_true(self) -> Occurrence:
        if self.occurred and not (self.description or "").strip():
            raise ValueError("description is required when occurred is true")
        return self


class ProposedImprovement(StrictModel):
    change: str = Field(min_length=1)
    problem: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    category: str = Field(min_length=1)
    expected_impact: Rating = Field(ge=1, le=5)

    @model_validator(mode="after")
    def category_is_known(self) -> ProposedImprovement:
        if self.category not in IMPROVEMENT_CATEGORIES:
            raise ValueError(f"unknown improvement category: {self.category}")
        return self


class SocietyReview(StrictModel):
    run_id: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    ratings: SocietyRatings
    top_bottlenecks: tuple[str, ...] = Field(min_length=1, max_length=3)
    concrete_failure: str = Field(min_length=1)
    most_useful_mechanism: str = Field(min_length=1)
    missing_information: Occurrence
    duplicate_work: Occurrence
    proposed_improvement: ProposedImprovement

    @model_validator(mode="after")
    def bottlenecks_are_known(self) -> SocietyReview:
        unknown = set(self.top_bottlenecks) - set(BOTTLENECKS)
        if unknown:
            raise ValueError("unknown bottlenecks: " + ", ".join(sorted(unknown)))
        return self


class PeerReviewsSpec(StrictModel):
    """Agent-selected peer reviews: one turn per evaluator, no operator
    pairing. Each evaluator chooses its own review targets from the roster
    (the selection itself is data — the perceived interaction graph) and
    completes one full review per chosen target."""

    enabled: bool = False
    # Evaluators to run; default: every configured agent.
    agents: tuple[str, ...] = ()
    revision: str | None = Field(default=None, min_length=1)
    # Guardrail against runaway turns, not a quota; agents choosing fewer
    # targets (or none, with a reason) is expected and meaningful.
    max_targets: int = Field(default=8, ge=1, le=32)

    @model_validator(mode="after")
    def agents_are_unique(self) -> PeerReviewsSpec:
        duplicates = sorted(
            agent for agent in set(self.agents) if self.agents.count(agent) > 1
        )
        if duplicates:
            raise ValueError(
                "peer_reviews.agents must be unique: " + ", ".join(duplicates)
            )
        return self


class TargetSelection(StrictModel):
    target: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class PeerReviewSet(StrictModel):
    """One evaluator turn's full product: chosen targets plus a complete
    review for each, or an explicit declaration of no meaningful peers."""

    run_id: str = Field(min_length=1)
    evaluator_agent: str = Field(min_length=1)
    selections: tuple[TargetSelection, ...] = ()
    no_meaningful_interactions_reason: str | None = None
    reviews: tuple[PeerReview, ...] = ()

    @model_validator(mode="after")
    def selections_and_reviews_agree(self) -> PeerReviewSet:
        targets = [s.target for s in self.selections]
        if len(set(targets)) != len(targets):
            raise ValueError("selected targets must be unique")
        if self.evaluator_agent in targets:
            raise ValueError("an agent cannot select itself for review")
        if not self.selections:
            if not (self.no_meaningful_interactions_reason or "").strip():
                raise ValueError(
                    "empty selection requires no_meaningful_interactions_reason"
                )
            if self.reviews:
                raise ValueError("reviews present without selections")
            return self
        if self.no_meaningful_interactions_reason is not None:
            raise ValueError(
                "no_meaningful_interactions_reason requires an empty selection"
            )
        reviewed = [review.target_agent for review in self.reviews]
        if sorted(reviewed) != sorted(targets):
            raise ValueError(
                "reviews must cover exactly the selected targets "
                f"(selected: {sorted(targets)}, reviewed: {sorted(reviewed)})"
            )
        for review in self.reviews:
            if review.run_id != self.run_id:
                raise ValueError("review run_id mismatch")
            if review.evaluator_agent != self.evaluator_agent:
                raise ValueError("review evaluator mismatch")
        return self


class SocietyReviewSpec(StrictModel):
    enabled: bool = True
    agents: tuple[str, ...] = ()
    revision: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def agents_are_unique(self) -> SocietyReviewSpec:
        duplicates = sorted(
            agent for agent in set(self.agents) if self.agents.count(agent) > 1
        )
        if duplicates:
            raise ValueError(
                "society_review.agents must be unique: " + ", ".join(duplicates)
            )
        return self


class ReviewManifest(StrictModel):
    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1)
    # Distinguishes several batches for one run (say, a mid-run probe and
    # the final batch); the batch directory is named after it.
    batch_id: str | None = Field(default=None, min_length=1)
    output_dir: Path = Path(".swecraft/reviews")
    default_revision: str = "main"
    # Review turns are wake-shaped: the agent's memory branch is mounted at
    # /app/memory (edits there are never published) and the forge is
    # browsable via SWECRAFT_FORGE_URL, so the reviewer investigates its own
    # record instead of relying solely on staged evidence.
    include_memory: bool = True
    peer_reviews: PeerReviewsSpec = Field(default_factory=PeerReviewsSpec)
    society_review: SocietyReviewSpec = Field(default_factory=SocietyReviewSpec)

    @property
    def record_batch_id(self) -> str:
        return self.batch_id or self.run_id


def load_review_manifest(path: Path) -> ReviewManifest:
    """Parse and validate `reviews.yaml`."""

    source = path.expanduser().resolve()
    if not source.is_file():
        raise CoordinatorError(f"review manifest does not exist: {source}")
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise CoordinatorError(
            f"could not read review manifest {source}: {error}"
        ) from None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise CoordinatorError(f"review manifest must be a mapping: {source}")
    try:
        return ReviewManifest.model_validate(data)
    except ValidationError as error:
        raise CoordinatorError(f"invalid review manifest {source}: {error}") from None
