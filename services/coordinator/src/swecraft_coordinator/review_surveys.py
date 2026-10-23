"""Survey definitions and prompts staged for review turns.

Each review job stages a `survey.json` (the questions, with their rating
anchors and guidance) and the JSON schema of the expected answer under the
turn's tool directory — nothing else by default: the reviewer reconstructs
the run from its own memory, repository, and forge browsing. The agent
writes exactly one JSON object to `/app/result.json`; the host validates it
against the strict models in `review_models`, so the staged files are
reference material, not a protocol.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from swecraft_coordinator.review_models import (
    BOTTLENECKS,
    CONTRIBUTION_TYPES,
    IMPROVEMENT_CATEGORIES,
)

SurveyKind = Literal["peer", "society"]

EXPOSURE_ANCHORS = (
    "1 = Almost none",
    "2 = Limited",
    "3 = Moderate",
    "4 = Extensive",
    "5 = Very extensive",
)

OVERALL_ANCHORS = (
    "1 = Very poor",
    "2 = Below average",
    "3 = Adequate",
    "4 = Strong",
    "5 = Exceptional",
)

SOCIETY_RATING_ANCHORS = (
    "1 = Severe limitation",
    "2 = Significant limitation",
    "3 = Adequate",
    "4 = Strong",
    "5 = Exceptionally effective",
)

CONFIDENCE_ANCHORS = (
    "1 = Very uncertain",
    "2 = Somewhat uncertain",
    "3 = Moderately confident",
    "4 = Confident",
    "5 = Very confident",
)

YES_NO_UNSURE = (
    "true = Yes",
    "false = No",
    "null = Unsure",
)


def peer_survey(identity: Mapping[str, str]) -> dict[str, Any]:
    """Question spec staged for the reviewing agent; paths are dotted
    locations in the answer object."""

    return {
        "schema_version": 1,
        "kind": "peer",
        "identity": dict(identity),
        "questions": [
            _rating(
                "exposure",
                "How much direct visibility did you have into this agent's work "
                "during this run?",
                anchors=EXPOSURE_ANCHORS,
                guidance=(
                    "Consider direct messages, shared issues or PRs, reviewing or "
                    "using their work, downstream effects on your repository, and "
                    "dependencies or handoffs involving the agent.",
                    "If there is not enough evidence to evaluate the agent "
                    "reliably, mark the review as insufficient_evidence.",
                ),
            ),
            _list_text(
                "reflection.consequential_contributions",
                "Identify up to three concrete things this agent did that "
                "materially affected your work.",
                max_items=3,
                guidance=(
                    "Reference specific evidence when possible: messages, issues, "
                    "PRs, commits, benchmarks, code changes, dependency "
                    "discoveries, or handoffs.",
                    "The purpose is to reconstruct what actually happened before "
                    "forming a score.",
                ),
            ),
            _text(
                "reflection.most_valuable_contribution",
                "What was the most valuable thing this agent contributed, and why "
                "was it useful?",
            ),
            _text(
                "reflection.biggest_limitation",
                "What was the most significant mistake, limitation, coordination "
                "problem, or missed opportunity in this agent's contribution? If "
                "there was no meaningful limitation, answer `None`.",
            ),
            _rating(
                "relevance.need_alignment",
                "How well did the agent's contribution address an important need "
                "of your repository or work?",
                anchors=(
                    "1 = Mostly irrelevant, unnecessary, or counterproductive",
                    "3 = Addressed a legitimate and useful need",
                    "5 = Directly addressed an important bottleneck, priority, "
                    "roadmap item, or newly discovered high-value need",
                ),
            ),
            _rating(
                "relevance.evidence_of_value",
                "How convincing was the evidence that the contribution produced, "
                "or was likely to produce, its claimed benefit?",
                anchors=(
                    "1 = Unsupported or contradicted by available evidence",
                    "3 = Plausible, with partial evidence or sound technical reasoning",
                    "5 = Strongly demonstrated through benchmarks, tests, "
                    "measurements, or convincing technical evidence",
                ),
            ),
            _rating(
                "relevance.marginal_usefulness",
                "How much useful information or capability did the contribution "
                "add beyond what was already available to you?",
                anchors=(
                    "1 = Essentially redundant",
                    "3 = Added meaningful new value",
                    "5 = Substantially changed what you knew, could decide, or "
                    "could accomplish",
                ),
            ),
            _rating(
                "relevance.overall",
                "Overall, how relevant was this agent's contribution to your work "
                "or repository?",
                anchors=OVERALL_ANCHORS,
            ),
            _rating(
                "collaboration.information_value",
                "How useful was the information communicated by this agent?",
                anchors=(
                    "1 = Mostly irrelevant, redundant, or distracting",
                    "3 = Frequently useful",
                    "5 = Repeatedly provided information that materially improved "
                    "decisions or work",
                ),
                guidance=(
                    "Do not mechanically reward message volume, fast responses, "
                    "or large numbers of references.",
                ),
            ),
            _rating(
                "collaboration.accuracy_reliability",
                "How reliable was the information communicated by this agent?",
                anchors=(
                    "1 = Frequently inaccurate or misleading",
                    "3 = Generally correct, with some inaccuracies",
                    "5 = Consistently accurate and appropriately qualified uncertainty",
                ),
            ),
            _rating(
                "collaboration.coordination_effectiveness",
                "Did interacting with this agent make your work easier or harder?",
                anchors=(
                    "1 = Created substantial coordination burden, confusion, "
                    "delay, or unnecessary work",
                    "3 = Neutral / adequately cooperative",
                    "5 = Substantially reduced coordination burden, resolved "
                    "dependencies, or unblocked progress",
                ),
            ),
            _rating(
                "collaboration.context_integration",
                "How effectively did this agent discover and use relevant "
                "information from elsewhere in the society?",
                anchors=(
                    "1 = Ignored important existing context or repeatedly "
                    "duplicated work",
                    "3 = Used obvious relevant context",
                    "5 = Found and integrated important non-obvious context "
                    "across agents, repositories, issues, or threads",
                ),
            ),
            _rating(
                "collaboration.overall",
                "Overall, how effectively did this agent collaborate with you and "
                "the broader society?",
                anchors=OVERALL_ANCHORS,
            ),
            _rating(
                "contribution_quality.correctness_robustness",
                "How well did the contribution accomplish its intended purpose "
                "without introducing important problems?",
                anchors=(
                    "1 = Fundamentally flawed",
                    "3 = Generally correct",
                    "5 = Highly robust and well validated",
                ),
                guidance=(
                    "Use contribution quality, not only code quality. A useful "
                    "contribution may be code, debugging, analysis, benchmarks, "
                    "design, documentation, code review, dependency discovery, or "
                    "coordination information.",
                ),
            ),
            _rating(
                "contribution_quality.maintainability",
                "How well did the contribution fit the repository's architecture, "
                "conventions, documentation practices, and long-term "
                "maintainability needs?",
                anchors=(
                    "1 = Poorly integrated or difficult to maintain",
                    "3 = Acceptable",
                    "5 = Exceptionally clean and maintainable",
                ),
            ),
            _rating(
                "contribution_quality.scope_discipline",
                "Was the scope and complexity of the contribution appropriate for "
                "the problem being solved?",
                anchors=(
                    "1 = Significantly over-engineered, under-engineered, or "
                    "unnecessarily invasive",
                    "3 = Reasonably scoped",
                    "5 = Exceptionally focused and appropriately scoped",
                ),
            ),
            _rating(
                "contribution_quality.overall",
                "Overall, how would you rate the quality of this agent's contribution?",
                anchors=OVERALL_ANCHORS,
            ),
            _rating(
                "marginal_impact",
                "Suppose this agent had not participated in this run. How much "
                "worse would the outcome for your repository or work have been?",
                anchors=(
                    "1 = Essentially unchanged",
                    "2 = Slightly worse",
                    "3 = Meaningfully worse",
                    "4 = Substantially worse",
                    "5 = An important outcome probably would not have occurred",
                ),
                guidance=(
                    "This is separate from quality. An agent can produce "
                    "high-quality work that was not particularly important, or a "
                    "modest contribution that was critical to unblocking the "
                    "system.",
                ),
            ),
            _multi_choice(
                "contribution_types",
                "In which ways did this agent materially contribute?",
                CONTRIBUTION_TYPES,
            ),
            _rating(
                "confidence",
                "How confident are you in this evaluation?",
                anchors=CONFIDENCE_ANCHORS,
                guidance=("Confidence and exposure must remain separate.",),
            ),
        ],
    }


def society_survey(identity: Mapping[str, str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "society",
        "identity": dict(identity),
        "questions": [
            _rating(
                "ratings.task_discovery",
                "How easily could you identify useful work to perform?",
                anchors=SOCIETY_RATING_ANCHORS,
            ),
            _rating(
                "ratings.information_accessibility",
                "How easily could you find the information and context needed to "
                "perform your work?",
                anchors=SOCIETY_RATING_ANCHORS,
            ),
            _rating(
                "ratings.ownership_clarity",
                "How clear was which agent or repository was responsible for "
                "particular problems or capabilities?",
                anchors=SOCIETY_RATING_ANCHORS,
            ),
            _rating(
                "ratings.agent_discovery_routing",
                "How easily could you identify the correct agent to communicate "
                "with when you needed information, coordination, or action?",
                anchors=SOCIETY_RATING_ANCHORS,
            ),
            _rating(
                "ratings.coordination_efficiency",
                "How effectively could agents coordinate dependencies, requests, "
                "and handoffs?",
                anchors=SOCIETY_RATING_ANCHORS,
            ),
            _rating(
                "ratings.redundancy_avoidance",
                "How effectively did the society avoid duplicated or conflicting work?",
                anchors=SOCIETY_RATING_ANCHORS,
            ),
            _rating(
                "ratings.dependency_handling",
                "How effectively did the society handle dependencies between "
                "different repositories or agents?",
                anchors=SOCIETY_RATING_ANCHORS,
            ),
            _rating(
                "ratings.infrastructure_adequacy",
                "How well did the available communication, memory, tooling, and "
                "coordination infrastructure support your work?",
                anchors=SOCIETY_RATING_ANCHORS,
            ),
            _rating(
                "ratings.overall_society_effectiveness",
                "Overall, how effectively did the society convert agent effort "
                "into useful progress?",
                anchors=SOCIETY_RATING_ANCHORS,
            ),
            _multi_choice(
                "top_bottlenecks",
                "What most limited your effectiveness during this run? Select up "
                "to three.",
                BOTTLENECKS,
                max_items=3,
            ),
            _text(
                "concrete_failure",
                "Describe one concrete episode where the structure or "
                "infrastructure of the society made your work harder than "
                "necessary.",
                guidance=(
                    "Include what you were trying to accomplish, what "
                    "information, resource, or coordination you needed, what went "
                    "wrong, and what consequence resulted.",
                ),
            ),
            _text(
                "most_useful_mechanism",
                "What existing feature, mechanism, or behavior of the society "
                "helped you most during this run?",
                guidance=(
                    "This identifies infrastructure worth preserving, not only "
                    "failures.",
                ),
            ),
            _bool_or_null(
                "missing_information.occurred",
                "Was there useful information somewhere in the society that you "
                "discovered too late or never discovered?",
                anchors=YES_NO_UNSURE,
            ),
            _text(
                "missing_information.description",
                "If yes, describe what information you needed, where it existed, "
                "when it would have been useful, how you eventually found it if "
                "at all, and what happened because you did not have it earlier.",
                required=False,
            ),
            _bool_or_null(
                "duplicate_work.occurred",
                "Did you perform substantial work that another agent had already "
                "performed or was simultaneously performing?",
                anchors=YES_NO_UNSURE,
            ),
            _text(
                "duplicate_work.description",
                "If yes, describe what was duplicated and what mechanism could "
                "have prevented the duplication.",
                required=False,
            ),
            _text(
                "proposed_improvement.change",
                "If you could change exactly one thing about the society or its "
                "infrastructure, what change would produce the largest "
                "improvement?",
            ),
            _text(
                "proposed_improvement.problem",
                "What problem would this change solve?",
            ),
            _text(
                "proposed_improvement.reason",
                "Explain how the proposed change would improve agent performance "
                "or society-level coordination.",
            ),
            _choice(
                "proposed_improvement.category",
                "Choose the primary improvement category.",
                IMPROVEMENT_CATEGORIES,
            ),
            _rating(
                "proposed_improvement.expected_impact",
                "How large an improvement do you expect this change would produce?",
                anchors=(
                    "1 = Negligible",
                    "2 = Small",
                    "3 = Moderate",
                    "4 = Large",
                    "5 = Transformative",
                ),
            ),
        ],
    }


def review_prompt(
    kind: SurveyKind, identity: Mapping[str, str], *, memory: bool = True
) -> str:
    """Prompt for one wake-shaped but strictly read-only review turn."""

    memory_note = (
        "Your durable memory workspace from the run is mounted at "
        "`/app/memory` (edits there are never published) — start there: "
        "it is your own record of what happened. "
        if memory
        else ""
    )
    context_note = f"""
This turn works like one of your regular wakes, except the task is a survey
instead of maintenance work. Your repository checkout is at
`/app/repository` (its final state). {memory_note}The forge is reachable
read-only at `$SWECRAFT_FORGE_URL` — you may browse issues, pull requests,
releases, and sibling repositories (git clone / API GET) to check your
recollections. You have no forge credential this turn: do not attempt to
push, comment, or mutate anything."""

    if kind == "peer":
        evaluator = identity["evaluator_agent"]
        return f"""
You are {evaluator}. Complete your peer reviews for run {identity["run_id"]}.
{context_note}
You choose whom to review: agents whose work materially affected yours
during the run, or whose work you used or depended on. Investigate first —
your memory, your repository's history, and the forge record — then drive
the whole process with the staged CLI:

  python3 /app/tools/review_cli.py start

The CLI is your only interface for the survey: it presents each question
with its rating anchors when you ask for `next`, validates each answer as
you give it, and assembles the final result on `submit`. You do not need
to read any other staged file.

It shows the roster and the commands: select your targets (each with a
reason), answer one question at a time (`next`, then `answer`), and
`submit` when done. Your selections and answers persist between commands,
so you can stop at any point, go check the forge or your memory, and
resume where you left off. If a chosen target turns out not to be
reliably reviewable, mark it `insufficient` with a reason; if no agent
meaningfully interacted with you, declare `skip-peers` with a reason —
both are legitimate answers. Use the anchors shown with each question for
every 1-5 score. `submit` writes the final result; do not edit
`/app/result.json` by hand. After `submit` succeeds, finish the turn by
writing a one-line summary to `/app/response.md` — the turn fails without
that file.
"""
    return f"""
You are {identity["agent"]}. Complete one society and infrastructure review
for run {identity["run_id"]}.
{context_note}
Investigate first — your memory, your repository's history, and the forge
record — then drive the survey with the staged CLI:

  python3 /app/tools/review_cli.py start

The CLI is your only interface for the survey: it presents each question
with its rating anchors when you ask for `next`, validates each answer as
you give it (`answer <field> <value>`), and assembles the final result on
`submit`. Your answers persist between commands, so you can stop at any
point, go check the forge or your memory, and resume where you left off.
Ground every answer in what actually happened to you during the run — not
in generic expectations — and use the anchors shown with each question
for every 1-5 score. `submit` writes the final result; do not edit
`/app/result.json` by hand. After `submit` succeeds, finish the turn by
writing a one-line summary to `/app/response.md` — the turn fails without
that file.
"""


def _rating(
    path: str,
    prompt: str,
    *,
    anchors: tuple[str, ...],
    guidance: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "path": path,
        "type": "rating",
        "min": 1,
        "max": 5,
        "prompt": prompt,
        "anchors": list(anchors),
        **({"guidance": list(guidance)} if guidance else {}),
    }


def _text(
    path: str,
    prompt: str,
    *,
    required: bool = True,
    guidance: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "path": path,
        "type": "text",
        "prompt": prompt,
        "required": required,
        **({"guidance": list(guidance)} if guidance else {}),
    }


def _list_text(
    path: str,
    prompt: str,
    *,
    max_items: int,
    guidance: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "path": path,
        "type": "list_text",
        "prompt": prompt,
        "max_items": max_items,
        **({"guidance": list(guidance)} if guidance else {}),
    }


def _choice(path: str, prompt: str, choices: tuple[str, ...]) -> dict[str, Any]:
    return {"path": path, "type": "choice", "prompt": prompt, "choices": list(choices)}


def _multi_choice(
    path: str,
    prompt: str,
    choices: tuple[str, ...],
    *,
    max_items: int | None = None,
    guidance: tuple[str, ...] = (),
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "path": path,
        "type": "multi_choice",
        "prompt": prompt,
        "choices": list(choices),
    }
    if max_items is not None:
        spec["max_items"] = max_items
    if guidance:
        spec["guidance"] = list(guidance)
    return spec


def _bool_or_null(
    path: str,
    prompt: str,
    *,
    anchors: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "path": path,
        "type": "bool_or_null",
        "prompt": prompt,
        "anchors": list(anchors),
    }
