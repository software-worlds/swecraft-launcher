"""Container executor tests."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from swecraft_evaluator.executor import ApptainerExecutor

# Exits 5 when any bind request reached it through the environment, so the
# executor test below cannot pass unless the variables were dropped.
STUB_APPTAINER = """#!/usr/bin/env python3
import os, sys
leaked = [key for key in os.environ if key.endswith(("_BIND", "_BINDPATH"))]
raise SystemExit(5 if leaked else 0)
"""

SITE_BINDS = {
    "APPTAINER_BINDPATH": "/cluster,/nfs",
    "APPTAINER_BIND": "/scratch",
    "SINGULARITY_BINDPATH": "/cluster",
    "SINGULARITY_BIND": "/nfs",
}


class ApptainerExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.stub = self.root / "apptainer"
        self.stub.write_text(STUB_APPTAINER, encoding="utf-8")
        self.stub.chmod(self.stub.stat().st_mode | stat.S_IEXEC)
        self.workdir = self.root / "work"
        self.workdir.mkdir()

    def test_the_stub_rejects_a_bind_request_in_its_environment(self) -> None:
        completed = subprocess.run(
            [str(self.stub), "exec"], env={**os.environ, **SITE_BINDS}, check=False
        )
        self.assertEqual(completed.returncode, 5)

    def test_environment_bind_requests_do_not_reach_apptainer(self) -> None:
        with mock.patch.dict(os.environ, SITE_BINDS):
            ApptainerExecutor((str(self.stub),)).run(self.workdir, "cell.sif", 30)
        self.assertTrue((self.workdir / "container.log").is_file())


if __name__ == "__main__":
    unittest.main()
