import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    @unittest.skipUnless(
        importlib.util.find_spec("PyInstaller"),
        "install requirements-build.txt to run the packaging build test",
    )
    @unittest.skipUnless(
        sysconfig.get_config_var("Py_ENABLE_SHARED"),
        "this Python runtime does not provide the shared library required by PyInstaller",
    )
    def test_pyinstaller_builds_standalone_scout_executable(self):
        with tempfile.TemporaryDirectory(prefix="auren-scout-build-") as temporary_directory:
            output_dir = Path(temporary_directory) / "dist"
            work_dir = Path(temporary_directory) / "work"
            command = [
                sys.executable,
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--clean",
                "--onefile",
                "--console",
                "--name",
                "AurenScout-V2",
                "--distpath",
                str(output_dir),
                "--workpath",
                str(work_dir),
                "--specpath",
                str(work_dir),
                "--paths",
                str(REPO_ROOT),
                "--collect-all",
                "playwright",
                str(REPO_ROOT / "scout" / "__main__.py"),
            ]
            result = subprocess.run(
                command,
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)

            suffix = ".exe" if os.name == "nt" else ""
            executable = output_dir / f"AurenScout-V2{suffix}"
            self.assertTrue(executable.is_file(), msg="PyInstaller nao criou o executavel")
            self.assertGreater(executable.stat().st_size, 0)
            legacy_executable = output_dir / f"AurenScout{suffix}"
            self.assertFalse(legacy_executable.exists(), msg="O build V2 nao deve criar/sobrescrever AurenScout")


if __name__ == "__main__":
    unittest.main()
