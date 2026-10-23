"""The launcher's own logic: settings, readiness, the stop rule, the map."""

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from swecraft_launch import cli as flow

TEMPLATE = flow.ROOT / "templates/experiment"


def filled(folder: Path) -> None:
    """Fill the template's markers the way an operator would."""

    name = folder.name
    for path in folder.rglob("*"):
        if path.is_file():
            text = path.read_text(errors="replace")
            text = text.replace(
                "experiments/FILL-experiment-name", f"experiments/{name}"
            )
            text = text.replace("FILL-compose-project", f"{name}-forge")
            text = text.replace("FILL-yyyy-mm-ddT", "2026-08-16T").replace(
                "FILL-yyyy-mm-dd", "2026-08-16"
            )
            text = text.replace("STOP_WAKES=FILL", "STOP_WAKES=600")
            text = text.replace("STOP_DOLLARS=FILL", "STOP_DOLLARS=500")
            text = text.replace("FILL", "filled")
            path.write_text(text)
    (folder / "charters" / "example-bot.md").write_text("Maintain example.\n")


class ExperimentFolderTest(unittest.TestCase):
    def setUp(self) -> None:
        experiments = flow.ROOT / "experiments"
        experiments.mkdir(exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix="test-", dir=experiments))
        shutil.rmtree(self.folder)
        shutil.copytree(TEMPLATE, self.folder)
        self.addCleanup(shutil.rmtree, self.folder, True)

    def test_the_template_is_not_ready_until_filled(self) -> None:
        problems = flow.check(flow.Experiment(self.folder))
        self.assertTrue(
            any("FILL marker left in evaluation.yaml" in p for p in problems)
        )
        self.assertTrue(any("STOP_WAKES" in p for p in problems))

    def test_a_filled_folder_is_ready(self) -> None:
        filled(self.folder)
        experiment = flow.Experiment(self.folder)
        self.assertEqual(flow.check(experiment), [])
        self.assertEqual(experiment.agents, ["example-bot"])
        self.assertEqual(experiment.evaluation, "society-sim")
        self.assertEqual(experiment.forge_url, "http://172.17.0.1:3400")

    def test_disagreeing_settings_are_named(self) -> None:
        filled(self.folder)
        env = self.folder / "experiment.env"
        env.write_text(
            env.read_text().replace(
                "SWECRAFT_FORGE_PORT=3400", "SWECRAFT_FORGE_PORT=3500"
            )
        )
        problems = flow.check(flow.Experiment(self.folder))
        self.assertTrue(
            any("SWECRAFT_FORGE_PORT 3500" in p for p in problems), problems
        )

    def test_the_default_layout_places_every_repository(self) -> None:
        filled(self.folder)
        out = flow.default_layout(
            flow.Experiment(self.folder), self.folder / "layout.json"
        )
        world = json.loads(out.read_text())["world"]
        self.assertEqual([r["name"] for r in world["repos"]], ["example"])
        self.assertEqual(world["repos"][0]["hex"], [0, 0])


class DemoTest(unittest.TestCase):
    def test_the_demo_is_ready(self) -> None:
        demo = flow.Experiment(flow.ROOT / "experiments/demo")
        self.assertEqual(flow.check(demo), [])
        self.assertEqual(demo.agents, ["h11-bot"])


class SettingsTest(unittest.TestCase):
    def test_env_lines_comments_and_quotes(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "experiment.env"
            path.write_text("# a comment\nA=1\nB = 'two'  # trailing\n\nC=\n")
            self.assertEqual(flow.read_env(path), {"A": "1", "B": "two", "C": ""})
            path.write_text("not a setting\n")
            with self.assertRaises(flow.Failed):
                flow.read_env(path)


class StopRuleTest(unittest.TestCase):
    def experiment(self, **settings: str) -> flow.Experiment:
        experiment = flow.Experiment.__new__(flow.Experiment)
        experiment.settings = {**flow.DEFAULTS, **settings}
        experiment.coordinator = {}
        return experiment

    def test_wakes_or_dollars_whichever_first(self) -> None:
        x = self.experiment(STOP_WAKES="10", STOP_DOLLARS="5")
        self.assertIsNone(flow.stop_reached(x, {"completed": 9, "cost_usd": 4.9}))
        self.assertIn(
            "STOP_WAKES", flow.stop_reached(x, {"completed": 10, "cost_usd": 0})
        )
        self.assertIn(
            "STOP_DOLLARS", flow.stop_reached(x, {"completed": 0, "cost_usd": 5})
        )

    def test_unset_or_unparsed_never_stops(self) -> None:
        x = self.experiment(STOP_WAKES="", STOP_DOLLARS="FILL")
        self.assertIsNone(flow.stop_reached(x, {"completed": 10**6, "cost_usd": 10**6}))

    def test_the_ledger_counts_completed_wakes_of_the_named_agents(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            database = Path(scratch) / "coordinator.db"
            db = sqlite3.connect(database)
            db.execute("CREATE TABLE wakes (agent TEXT, status TEXT, cost_usd REAL)")
            db.executemany(
                "INSERT INTO wakes VALUES (?, ?, ?)",
                [
                    ("a", "completed", 1.0),
                    ("b", "completed", 2.0),
                    ("a", "failed", 0.5),
                ],
            )
            db.commit()
            db.close()
            x = self.experiment(STOP_AGENTS="a")
            x.coordinator = {"database": str(database)}
            self.assertEqual(
                flow.ledger(x), {"completed": 1, "failed": 1, "cost_usd": 3.5}
            )


class LayoutTest(unittest.TestCase):
    def test_the_spiral_fills_rings_without_repeats(self) -> None:
        cells = flow.hex_spiral(19)
        self.assertEqual(len({tuple(c) for c in cells}), 19)
        self.assertEqual(cells[0], [0, 0])
        for q, r in cells[1:7]:
            self.assertEqual(max(abs(q), abs(r), abs(q + r)), 1)
        for q, r in cells[7:]:
            self.assertEqual(max(abs(q), abs(r), abs(q + r)), 2)


if __name__ == "__main__":
    unittest.main()
