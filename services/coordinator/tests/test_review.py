from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError
from swecraft_runner.runner import RunResult

from swecraft_coordinator import CoordinatorError
from swecraft_coordinator.config import CoordinatorConfig
from swecraft_coordinator.review import (
    plan_review_jobs,
    run_review_batch,
    validate_review_manifest,
)
from swecraft_coordinator.review_models import (
    PeerReview,
    ReviewManifest,
)

COMPLETE_PEER_ANSWER = {
    "exposure": 4,
    "insufficient_evidence": False,
    "reflection": {
        "consequential_contributions": ["found the missing test"],
        "most_valuable_contribution": "made the blocker concrete",
        "biggest_limitation": "None",
    },
    "relevance": {
        "need_alignment": 4,
        "evidence_of_value": 4,
        "marginal_usefulness": 5,
        "overall": 4,
    },
    "collaboration": {
        "information_value": 5,
        "accuracy_reliability": 4,
        "coordination_effectiveness": 4,
        "context_integration": 4,
        "overall": 4,
    },
    "contribution_quality": {
        "correctness_robustness": 4,
        "maintainability": 4,
        "scope_discipline": 5,
        "overall": 4,
    },
    "marginal_impact": 4,
    "contribution_types": [
        "identified a problem",
        "improved testing / evaluation",
    ],
    "confidence": 4,
}

COMPLETE_SOCIETY_ANSWER = {
    "ratings": {
        "task_discovery": 3,
        "information_accessibility": 3,
        "ownership_clarity": 4,
        "agent_discovery_routing": 3,
        "coordination_efficiency": 4,
        "redundancy_avoidance": 3,
        "dependency_handling": 4,
        "infrastructure_adequacy": 3,
        "overall_society_effectiveness": 4,
    },
    "top_bottlenecks": ["finding relevant information"],
    "concrete_failure": "A relevant issue was found late.",
    "most_useful_mechanism": "Forge notifications.",
    "missing_information": {
        "occurred": True,
        "description": "The useful issue existed before the wake.",
    },
    "duplicate_work": {"occurred": False, "description": None},
    "proposed_improvement": {
        "change": "Add a run-level digest.",
        "problem": "Context is scattered.",
        "reason": "Agents could find prior work sooner.",
        "category": "shared context / memory",
        "expected_impact": 4,
    },
}


def coordinator_config(
    root: Path,
    agents: tuple[tuple[str, str], ...] = (
        ("alice", "hub/alice"),
        ("bob", "hub/bob"),
    ),
) -> CoordinatorConfig:
    path = root / "coordinator.yaml"
    agent_rows = "\n".join(
        f"  - name: {name}\n    repo: {repo}" for name, repo in agents
    )
    path.write_text(
        f"""
forge_url: http://127.0.0.1:3400
model: test/model
agent_env: {{}}
agents:
{agent_rows}
""",
        encoding="utf-8",
    )
    return CoordinatorConfig.load(path)


def manifest() -> ReviewManifest:
    return ReviewManifest.model_validate(
        {
            "schema_version": 1,
            "run_id": "run-001",
            "output_dir": "reviews-out",
            "peer_reviews": {"enabled": True},
            "society_review": {"enabled": True},
        }
    )


class FakeRunner:
    """Stands in for run_once: answers from the staged survey identity."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.requests = []
        self.counter = 0

    def __call__(self, request) -> RunResult:
        self.requests.append(request)
        self.counter += 1
        survey = json.loads(
            (request.tool_directory / "survey.json").read_text(encoding="utf-8")
        )
        identity = survey["identity"]
        if survey["kind"] == "peer":
            roster = json.loads(
                (request.tool_directory / "roster.json").read_text(encoding="utf-8")
            )
            target = next(
                name for name in roster["agents"] if name != roster["evaluator"]
            )
            structured = {
                **identity,
                "selections": [
                    {"target": target, "reason": "used their fix in my repo"}
                ],
                "reviews": [
                    {
                        "run_id": identity["run_id"],
                        "evaluator_agent": identity["evaluator_agent"],
                        "target_agent": target,
                        **COMPLETE_PEER_ANSWER,
                    }
                ],
            }
        else:
            structured = {**identity, **COMPLETE_SOCIETY_ANSWER}
        return self._result(structured)

    def _result(self, structured) -> RunResult:
        run_dir = self.root / "runs" / f"run-{self.counter}"
        output = run_dir / "output"
        output.mkdir(parents=True, exist_ok=True)
        (run_dir / "input/repository").mkdir(parents=True, exist_ok=True)
        (run_dir / "input/repository/checkout.txt").write_text("copy")
        response = output / "response.md"
        response.write_text("Completed the survey.\n", encoding="utf-8")
        # Mirror the real runner: result.json is the terminal receipt and the
        # agent's structured answer lives in agent-result.json.
        result_path = output / "result.json"
        result_path.write_text(
            json.dumps({"run_id": f"run-{self.counter}", "status": "completed"}),
            encoding="utf-8",
        )
        structured_path = output / "agent-result.json"
        structured_path.write_text(json.dumps(structured), encoding="utf-8")
        return RunResult(
            run_id=f"run-{self.counter}",
            run_dir=run_dir,
            source_sha="a" * 40,
            response_path=response,
            changes_path=None,
            changed_files=(),
            result_path=result_path,
            structured=structured,
            structured_path=structured_path,
        )


class BrokenRunner(FakeRunner):
    def __call__(self, request) -> RunResult:
        self.requests.append(request)
        self.counter += 1
        return self._result({"nonsense": True})


class ReviewBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.config = coordinator_config(self.root)

    def test_batch_writes_reviews_and_sealed_record(self) -> None:
        runner = FakeRunner(self.root)

        result = run_review_batch(manifest(), self.config, root=self.root, run=runner)

        self.assertEqual(result.failed_jobs, 0)
        self.assertEqual(result.peer_turns, 2)
        self.assertEqual(result.peer_reviews, 2)
        self.assertEqual(result.society_reviews, 2)
        self.assertTrue((result.batch_dir / "record.json").is_file())
        self.assertTrue((result.batch_dir / "reviews/peer/alice.json").is_file())
        self.assertTrue((result.batch_dir / "reviews/peer/bob.json").is_file())
        self.assertTrue((result.batch_dir / "reviews/society/alice.json").is_file())
        # Completed review runs are slimmed like wakes: reproducible checkout
        # copies go, the collected outputs stay.
        run_dir = self.root / "runs" / "run-1"
        self.assertFalse((run_dir / "input/repository").exists())
        self.assertTrue((run_dir / "output/result.json").is_file())
        # ...and the lean trace projection joins the batch beside the receipt.
        trace = result.batch_dir / "jobs/peer-alice/run/output"
        self.assertTrue((trace / "result.json").is_file())
        self.assertTrue((trace / "response.md").is_file())
        request = runner.requests[0]
        self.assertTrue((request.tool_directory / "roster.json").is_file())
        self.assertTrue((request.tool_directory / "review_cli.py").is_file())
        self.assertTrue(request.structured_output)
        self.assertFalse(request.allow_remote_writes)
        self.assertEqual(request.memory_repository, request.repository)
        self.assertEqual(request.memory_revision, "agent/memory")
        self.assertIn("SWECRAFT_FORGE_URL", request.agent_env)
        self.assertNotIn("SWECRAFT_FORGE_TOKEN", request.agent_env)
        self.assertEqual(request.revision, "main")
        # The CLI is the only agent-facing interface; the schema stays
        # host-side (and in the digest), never mounted into the turn.
        self.assertFalse((request.tool_directory / "schema.json").exists())

    def test_parallel_dispatch_seals_the_same_batch(self) -> None:
        runner = FakeRunner(self.root)
        result = run_review_batch(
            manifest(), self.config, root=self.root, run=runner, parallel=4
        )
        self.assertEqual(result.failed_jobs, 0)
        self.assertEqual(result.peer_turns, 2)
        self.assertEqual(result.society_reviews, 2)
        self.assertTrue((result.batch_dir / "record.json").is_file())

    def test_rerun_purges_stale_staged_tool_files(self) -> None:
        stale = self.root / "reviews-out/run-001/jobs/society-alice/tools/evidence.json"
        stale.parent.mkdir(parents=True)
        stale.write_text('{"planted": "by an earlier head"}', encoding="utf-8")

        runner = FakeRunner(self.root)
        run_review_batch(manifest(), self.config, root=self.root, run=runner)

        society_request = next(
            request
            for request in runner.requests
            if "society-alice" in str(request.tool_directory)
        )
        self.assertFalse((society_request.tool_directory / "evidence.json").exists())
        self.assertTrue((society_request.tool_directory / "survey.json").is_file())

    def test_cached_reviews_reuse_only_matching_input_digest(self) -> None:
        runner = FakeRunner(self.root)
        result = run_review_batch(manifest(), self.config, root=self.root, run=runner)

        self.assertEqual(len(runner.requests), 4)
        receipt_path = result.batch_dir / "jobs/peer-alice/job.json"
        first_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertRegex(first_receipt["input_digest"], r"^[0-9a-f]{64}$")

        run_review_batch(manifest(), self.config, root=self.root, run=runner)
        self.assertEqual(len(runner.requests), 4)

        payload = manifest().model_dump(mode="json")
        payload["peer_reviews"]["max_targets"] = 3
        changed = ReviewManifest.model_validate(payload)
        run_review_batch(changed, self.config, root=self.root, run=runner)

        self.assertEqual(len(runner.requests), 6)
        second_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertNotEqual(
            first_receipt["input_digest"], second_receipt["input_digest"]
        )

    def test_rerun_after_shrunk_roster_prunes_stale_outputs(self) -> None:
        runner = FakeRunner(self.root)
        first = ReviewManifest.model_validate(
            {
                "run_id": "run-001",
                "output_dir": "reviews-out",
                "peer_reviews": {"enabled": True},
                "society_review": {"enabled": False},
            }
        )
        first_result = run_review_batch(first, self.config, root=self.root, run=runner)
        self.assertEqual(first_result.peer_reviews, 2)

        payload = first.model_dump(mode="json")
        payload["peer_reviews"]["agents"] = ["alice"]
        second = ReviewManifest.model_validate(payload)
        second_result = run_review_batch(
            second, self.config, root=self.root, run=runner
        )

        self.assertEqual(second_result.peer_reviews, 1)
        self.assertFalse((second_result.batch_dir / "reviews/peer/bob.json").exists())
        self.assertFalse((second_result.batch_dir / "jobs/peer-bob").exists())

    def test_failed_job_leaves_batch_unsealed_and_rerun_completes(self) -> None:
        broken = BrokenRunner(self.root)
        result = run_review_batch(manifest(), self.config, root=self.root, run=broken)

        self.assertEqual(result.failed_jobs, 4)
        self.assertFalse((result.batch_dir / "record.json").exists())
        receipt = json.loads(
            (result.batch_dir / "jobs/peer-alice/job.json").read_text(encoding="utf-8")
        )
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("invalid structured review", receipt["error"])

        fixed = FakeRunner(self.root)
        rerun = run_review_batch(manifest(), self.config, root=self.root, run=fixed)
        self.assertEqual(rerun.failed_jobs, 0)
        self.assertTrue((rerun.batch_dir / "record.json").is_file())

    def test_identity_mismatch_fails_the_job(self) -> None:
        class WrongIdentity(FakeRunner):
            def __call__(self, request) -> RunResult:
                self.requests.append(request)
                self.counter += 1
                structured = {
                    "run_id": "run-001",
                    "evaluator_agent": "bob",
                    "selections": [{"target": "alice", "reason": "shared work"}],
                    "reviews": [
                        {
                            "run_id": "run-001",
                            "evaluator_agent": "bob",
                            "target_agent": "alice",
                            **COMPLETE_PEER_ANSWER,
                        }
                    ],
                }
                return self._result(structured)

        payload = manifest().model_dump(mode="json")
        payload["society_review"] = {"enabled": False}
        payload["peer_reviews"] = {"enabled": True, "agents": ["alice"]}
        peer_only = ReviewManifest.model_validate(payload)
        result = run_review_batch(
            peer_only, self.config, root=self.root, run=WrongIdentity(self.root)
        )
        self.assertEqual(result.failed_jobs, 1)
        receipt = json.loads(
            (result.batch_dir / "jobs/peer-alice/job.json").read_text(encoding="utf-8")
        )
        self.assertIn("identity mismatch", receipt["error"])

    def test_off_roster_selection_fails_the_job(self) -> None:
        class OffRoster(FakeRunner):
            def __call__(self, request) -> RunResult:
                self.requests.append(request)
                self.counter += 1
                structured = {
                    "run_id": "run-001",
                    "evaluator_agent": "alice",
                    "selections": [{"target": "mallory", "reason": "made up"}],
                    "reviews": [
                        {
                            "run_id": "run-001",
                            "evaluator_agent": "alice",
                            "target_agent": "mallory",
                            **COMPLETE_PEER_ANSWER,
                        }
                    ],
                }
                return self._result(structured)

        payload = manifest().model_dump(mode="json")
        payload["society_review"] = {"enabled": False}
        payload["peer_reviews"] = {"enabled": True, "agents": ["alice"]}
        peer_only = ReviewManifest.model_validate(payload)
        result = run_review_batch(
            peer_only, self.config, root=self.root, run=OffRoster(self.root)
        )
        self.assertEqual(result.failed_jobs, 1)
        receipt = json.loads(
            (result.batch_dir / "jobs/peer-alice/job.json").read_text(encoding="utf-8")
        )
        self.assertIn("not in the roster", receipt["error"])

    def test_validate_rejects_unknown_agents(self) -> None:
        bad = ReviewManifest.model_validate(
            {
                "run_id": "run-001",
                "peer_reviews": {"enabled": True, "agents": ["alice", "carol"]},
            }
        )
        with self.assertRaisesRegex(CoordinatorError, "carol"):
            validate_review_manifest(bad, self.config)

    def test_society_review_without_evidence_stages_no_evidence_file(self) -> None:
        no_evidence = ReviewManifest.model_validate(
            {
                "run_id": "run-001",
                "output_dir": "reviews-out",
                "society_review": {"enabled": True},
            }
        )
        runner = FakeRunner(self.root)
        result = run_review_batch(no_evidence, self.config, root=self.root, run=runner)
        self.assertEqual(result.failed_jobs, 0)
        request = runner.requests[0]
        self.assertFalse((request.tool_directory / "evidence.json").exists())
        self.assertNotIn("evidence.json", request.prompt)

    def test_plan_covers_all_agents_when_society_roster_is_empty(self) -> None:
        payload = manifest().model_dump(mode="json")
        payload["peer_reviews"] = {"enabled": False}
        society_only = ReviewManifest.model_validate(payload)
        jobs = plan_review_jobs(society_only, self.config)
        self.assertEqual(
            sorted(job.agent for job in jobs if job.kind == "society"),
            ["alice", "bob"],
        )


class ReviewModelTests(unittest.TestCase):
    def test_manifest_rejects_duplicate_society_agents(self) -> None:
        with self.assertRaisesRegex(ValidationError, "society_review.agents"):
            ReviewManifest.model_validate(
                {
                    "run_id": "run-001",
                    "society_review": {
                        "enabled": True,
                        "agents": ["alice", "alice"],
                    },
                }
            )

    def test_peer_review_requires_scores_unless_insufficient(self) -> None:
        with self.assertRaises(ValidationError):
            PeerReview.model_validate(
                {
                    "run_id": "run-001",
                    "evaluator_agent": "alice",
                    "target_agent": "bob",
                    "insufficient_evidence": False,
                }
            )
        review = PeerReview.model_validate(
            {
                "run_id": "run-001",
                "evaluator_agent": "alice",
                "target_agent": "bob",
                "insufficient_evidence": True,
                "insufficient_evidence_reason": "no shared issue or handoff",
            }
        )
        self.assertTrue(review.insufficient_evidence)

    def test_insufficient_peer_review_rejects_scores(self) -> None:
        with self.assertRaisesRegex(ValidationError, "insufficient_evidence"):
            PeerReview.model_validate(
                {
                    "run_id": "run-001",
                    "evaluator_agent": "alice",
                    "target_agent": "bob",
                    "exposure": 3,
                    "insufficient_evidence": True,
                    "insufficient_evidence_reason": "no shared issue or handoff",
                    "relevance": {
                        "need_alignment": 4,
                        "evidence_of_value": 4,
                        "marginal_usefulness": 4,
                        "overall": 4,
                    },
                }
            )


class ReviewCliTests(unittest.TestCase):
    """Drive the staged in-turn CLI end to end as a subprocess."""

    def setUp(self) -> None:
        import swecraft_coordinator

        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        root = Path(self._directory.name)
        self.tools = root / "tools"
        self.tools.mkdir()
        self.cli = (
            Path(swecraft_coordinator.__file__).parent / "staged" / "review_cli.py"
        )
        from swecraft_coordinator.review_surveys import peer_survey

        identity = {"run_id": "run-001", "evaluator_agent": "alice"}
        (self.tools / "survey.json").write_text(json.dumps(peer_survey(identity)))
        (self.tools / "roster.json").write_text(
            json.dumps(
                {
                    "run_id": "run-001",
                    "evaluator": "alice",
                    "agents": ["alice", "bob"],
                    "max_targets": 8,
                }
            )
        )
        (self.tools / "review_cli.py").write_text(self.cli.read_text())
        self.env = {
            **__import__("os").environ,
            "REVIEW_STATE": str(root / "state.json"),
            "REVIEW_RESULT": str(root / "result.json"),
        }
        self.result_path = root / "result.json"

    def run_cli(self, *args, expect_ok=True):
        import subprocess
        import sys

        completed = subprocess.run(
            [sys.executable, str(self.tools / "review_cli.py"), *args],
            capture_output=True,
            text=True,
            env=self.env,
        )
        if expect_ok:
            self.assertEqual(
                completed.returncode, 0, completed.stdout + completed.stderr
            )
        return completed

    def test_full_flow_produces_a_valid_peer_review_set(self) -> None:
        from swecraft_coordinator.review_models import PeerReviewSet
        from swecraft_coordinator.review_surveys import peer_survey

        self.run_cli("start")
        self.run_cli("select", "bob", "--reason", "adopted their fix")
        rejected = self.run_cli("select", "alice", "--reason", "me", expect_ok=False)
        self.assertIn("cannot review yourself", rejected.stdout)

        for q in peer_survey({"run_id": "r", "evaluator_agent": "a"})["questions"]:
            kind = q["type"]
            if kind == "rating":
                self.run_cli("answer", q["path"], "4")
            elif kind == "multi_choice":
                self.run_cli("answer", q["path"], q["choices"][0])
            elif kind == "choice":
                self.run_cli("answer", q["path"], q["choices"][0])
            elif kind == "list_text":
                self.run_cli("answer", q["path"], "found and fixed the retry bug")
            elif kind == "bool_or_null":
                self.run_cli("answer", q["path"], "false")
            else:
                self.run_cli("answer", q["path"], "A concrete, grounded answer.")

        bad = self.run_cli("answer", "exposure", "9", expect_ok=False)
        self.assertIn("between 1 and 5", bad.stdout)
        self.run_cli("submit")
        review_set = PeerReviewSet.model_validate(
            json.loads(self.result_path.read_text())
        )
        self.assertEqual(review_set.reviews[0].target_agent, "bob")

    def test_skip_peers_submits_empty_selection(self) -> None:
        from swecraft_coordinator.review_models import PeerReviewSet

        self.run_cli("skip-peers", "--reason", "no meaningful interactions")
        self.run_cli("submit")
        review_set = PeerReviewSet.model_validate(
            json.loads(self.result_path.read_text())
        )
        self.assertEqual(review_set.selections, ())
        self.assertEqual(review_set.reviews, ())

    def test_society_mode_full_flow_with_conditional_description(self) -> None:
        from swecraft_coordinator.review_models import SocietyReview
        from swecraft_coordinator.review_surveys import society_survey

        (self.tools / "survey.json").write_text(
            json.dumps(society_survey({"run_id": "run-001", "agent": "alice"}))
        )
        (self.tools / "roster.json").unlink()

        started = self.run_cli("start")
        self.assertIn("reviewing the society", started.stdout)
        refused = self.run_cli("select", "bob", "--reason", "x", expect_ok=False)
        self.assertIn("peer reviews only", refused.stdout)

        for _ in range(40):
            shown = self.run_cli("next").stdout
            if "all questions answered" in shown:
                break
            field = shown.split("field:")[1].split()[0]
            qtype = shown.split("(")[1].split(")")[0]
            if qtype == "rating":
                self.run_cli("answer", field, "4")
            elif qtype == "multi_choice":
                self.run_cli("answer", field, "testing infrastructure")
            elif qtype == "choice":
                self.run_cli("answer", field, "testing / evaluation")
            elif qtype == "bool_or_null":
                value = "true" if field == "missing_information.occurred" else "false"
                self.run_cli("answer", field, value)
            else:
                self.run_cli("answer", field, "A concrete, grounded answer.")
        else:
            self.fail("survey never completed")

        self.run_cli("submit")
        review = SocietyReview.model_validate(json.loads(self.result_path.read_text()))
        self.assertEqual(review.agent, "alice")
        self.assertTrue(review.missing_information.occurred)
        self.assertIsNotNone(review.missing_information.description)

    def test_submit_refuses_unanswered_questions(self) -> None:
        self.run_cli("select", "bob", "--reason", "shared work")
        refused = self.run_cli("submit", expect_ok=False)
        self.assertIn("unanswered", refused.stdout)


if __name__ == "__main__":
    unittest.main()
