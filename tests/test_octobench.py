"""Black-box checks for the one-command, one-artifact benchmark.

Mocks keep functional tests fast and never install packages or benchmark the CI
host. The companion smoke test exercises real tools with shortened workloads.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = ROOT / "src/octobench"
MOCK_TOOLS = ROOT / "tests/mock_tools.py"
MOCK_NAMES = ("sysbench", "fio", "openssl", "lsblk", "xdg-user-dir", "hostname",
              "uname", "dmidecode", "lspci", "lsusb", "sensors", "sudo", "apt-get", "findmnt")


class Fixture:
    def __init__(self, missing=(), usb=False):
        self.temporary = tempfile.TemporaryDirectory(prefix="octobench functional ")
        self.root = Path(self.temporary.name)
        self.home = self.root / "home with spaces"
        self.desktop = self.home / "Desktop"
        self.cwd = self.root / "unrelated directory"
        self.binary = self.root / "bin"
        self.templates = self.root / "installed tools"
        for directory in (self.home, self.desktop, self.cwd, self.binary, self.templates):
            directory.mkdir(parents=True)
        self.sentinel = self.home / "do-not-touch.txt"
        self.sentinel.write_text("existing user data\n")
        self.desktop_sentinel = self.desktop / "existing-result.json"
        self.desktop_sentinel.write_text('{"original":true}\n')
        # Give command -v an isolated, complete coreutils PATH. In the dependency
        # test, missing benchmark tools cannot accidentally resolve from the host.
        for original in Path("/usr/bin").iterdir():
            if original.is_file() and original.name not in MOCK_NAMES:
                (self.binary / original.name).symlink_to(original)
        for name in MOCK_NAMES:
            path = self.templates / name
            path.write_text(f'#!/bin/bash\nexec /usr/bin/python3 "{MOCK_TOOLS}" "{name}" "$@"\n')
            path.chmod(0o755)
            if name not in missing:
                (self.binary / name).symlink_to(path)
        # Generate full-size deterministic data quickly. Real checksums and real
        # cp/rsync/mv still test nested/hidden-file integrity and cleanup.
        (self.binary / "dd").unlink(missing_ok=True)
        (self.binary / "dd").write_text('''#!/bin/bash
args=()
for argument in "$@"; do
    [[ "$argument" != if=/dev/urandom ]] || argument=if=/dev/zero
    args+=("$argument")
done
exec /usr/bin/dd conv=sparse "${args[@]}"
''')
        (self.binary / "dd").chmod(0o755)
        (self.binary / "rsync").unlink(missing_ok=True)
        (self.binary / "rsync").write_text('#!/bin/bash\nexec /usr/bin/rsync --sparse "$@"\n')
        (self.binary / "rsync").chmod(0o755)
        (self.binary / "jq").unlink(missing_ok=True)
        (self.binary / "jq").write_text('''#!/bin/bash
if [[ -n "${OCTO_TEST_FAIL_FINAL_JQ:-}" ]]; then
    for argument in "$@"; do
        if [[ "$argument" == *'.finished_at='* ]]; then
            printf 'Injected final report serialization failure\\n' >&2
            exit 42
        fi
    done
fi
exec /usr/bin/jq "$@"
''')
        (self.binary / "jq").chmod(0o755)
        (self.binary / "awk").unlink(missing_ok=True)
        (self.binary / "awk").write_text('''#!/bin/bash
if [[ -n "${OCTO_TEST_ADAPTIVE_MEMORY:-}" && "$*" == *'MemAvailable:'* && "$*" == *'/proc/meminfo'* ]]; then
    printf '786432\\n'
    exit 0
fi
exec /usr/bin/awk "$@"
''')
        (self.binary / "awk").chmod(0o755)
        (self.binary / "cat").unlink(missing_ok=True)
        (self.binary / "cat").write_text('''#!/bin/bash
if [[ -n "${OCTO_TEST_ADAPTIVE_MEMORY:-}" && "$*" == '/sys/fs/cgroup/memory.max' ]]; then
    printf 'max\\n'
    exit 0
fi
exec /usr/bin/cat "$@"
''')
        (self.binary / "cat").chmod(0o755)
        # Freeze the filename clock only. Monotonic/epoch timing stays real.
        (self.binary / "date").unlink(missing_ok=True)
        (self.binary / "date").write_text('''#!/bin/bash
for argument in "$@"; do
    if [[ "$argument" == *%Y%m%d* ]]; then
        printf '20261002T160000Z\\n'
        exit 0
    elif [[ "$argument" == *%Y-%m-%d* ]]; then
        printf '2026-10-02T16:00:00Z\\n'
        exit 0
    fi
done
exec /usr/bin/date "$@"
''')
        (self.binary / "date").chmod(0o755)
        self.env = {**os.environ, "HOME": str(self.home), "PATH": str(self.binary),
                    "TMPDIR": str(self.root), "OCTO_TEST_BIN": str(self.binary),
                    "OCTO_TEST_TEMPLATES": str(self.templates),
                    "OCTO_TEST_TOOL_LOG": str(self.root / "tools.jsonl")}
        for name in ("SUDO_USER", "SUDO_UID", "SUDO_GID"):
            self.env.pop(name, None)
        self.usb = None
        if usb:
            self.usb = self.root / "USB drive with spaces"
            self.usb.mkdir()
            (self.usb / "original.bin").write_bytes(b"untouched USB data")
            self.env["OCTO_TEST_USB_DIR"] = str(self.usb)

    def run(self, timeout=120):
        return subprocess.run(["/bin/bash", "-c", 'exec bash <(cat "$1")',
                               "octobench-test", str(PROGRAM)], cwd=self.cwd,
                              env=self.env, text=True, capture_output=True, timeout=timeout)

    def reports(self):
        return sorted(path for path in self.desktop.iterdir() if path != self.desktop_sentinel)

    def commands(self):
        log = self.root / "tools.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def assert_untouched(self, case):
        case.assertEqual(self.sentinel.read_text(), "existing user data\n")
        case.assertEqual(self.desktop_sentinel.read_text(), '{"original":true}\n')
        case.assertFalse(list(self.home.glob(".octobench*")))
        case.assertEqual({path.name for path in self.home.iterdir()}, {"Desktop", "do-not-touch.txt"})
        case.assertFalse(list(self.cwd.iterdir()))
        case.assertFalse(list(self.root.rglob(".octobench*")))
        case.assertFalse(list(self.root.glob("octobench.*")))
        if self.usb:
            case.assertEqual((self.usb / "original.bin").read_bytes(), b"untouched USB data")
            case.assertEqual(list(self.usb.iterdir()), [self.usb / "original.bin"])

    def close(self):
        self.temporary.cleanup()


class StandaloneTests(unittest.TestCase):
    def fixture(self, **kwargs):
        fixture = Fixture(**kwargs)
        self.addCleanup(fixture.close)
        return fixture

    def test_single_script_has_no_repository_runtime_dependency(self):
        result = subprocess.run(["/bin/bash", "-n", str(PROGRAM)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        source = PROGRAM.read_text()
        self.assertNotIn("lib/octobench.py", source)
        self.assertNotIn("vendor/stream", source)
        self.assertNotIn("--compare", source)
        self.assertNotIn("python3", source)

    def test_process_substitution_runs_from_unrelated_directory_and_writes_one_json(self):
        fixture = self.fixture()
        result = fixture.run()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        reports = fixture.reports()
        self.assertEqual(len(reports), 1, reports)
        self.assertEqual(reports[0].suffix, ".json")
        self.assertIn("octobench-fixture", reports[0].name)
        self.assertTrue("20261002" in reports[0].name or "2026-10-02" in reports[0].name, reports[0].name)
        report = json.loads(reports[0].read_text())
        self.assertTrue(str(report["schema_version"]).startswith("2"))
        for key in ("octobench_version", "system_name", "configuration", "inventory", "targets", "measurements", "runs", "warnings", "status"):
            self.assertIn(key, report)
        self.assertTrue(report["measurements"], report["warnings"])
        measurements = report["measurements"]
        ids = [str(measurement["id"]) for measurement in measurements]
        for category in ("cpu", "memory", "storage", "transfer"):
            self.assertTrue(any(category in metric for metric in ids), (category, ids, report["warnings"]))
        for measurement in measurements:
            for key in ("id", "label", "target", "unit", "higher_is_better", "value", "aggregation",
                        "samples", "parameters", "tool", "tool_version"):
                self.assertIn(key, measurement)
            self.assertIsInstance(measurement["value"], (int, float))
            self.assertGreater(measurement["value"], 0)
            self.assertEqual(measurement["aggregation"], "median")
        cpu = [m for m in measurements if "sysbench" in str(m["tool"]) and "cpu" in str(m["id"])]
        self.assertTrue(cpu, ids)
        self.assertTrue(all(abs(m["value"] - 1234.56) < 0.01 for m in cpu))
        self.assertTrue(all(len(m["samples"]) == 3 for m in cpu))
        memory = [m for m in measurements if "sysbench" in str(m["tool"])
                  and "memory" in str(m["id"]) and m["unit"] == "MiB/s"]
        self.assertTrue(memory, ids)
        self.assertTrue(all(abs(m["value"] - 6789.12) < 0.01 for m in memory))
        storage = [m for m in measurements if "fio" in str(m["tool"]) and m["unit"] == "MiB/s"]
        self.assertTrue(storage, ids)
        self.assertTrue(all(abs(m["value"] - 128) < 0.01 for m in storage))
        self.assertTrue(report["runs"])
        self.assertTrue(any(run.get("stdout") for run in report["runs"]))
        for run in report["runs"]:
            for key in ("label", "command", "working_directory", "exit_code", "elapsed_seconds", "stdout", "stderr"):
                self.assertIn(key, run)
        self.assertTrue(result.stdout.strip(), "Progress must be visible in the terminal")
        self.assertNotIn("Traceback", result.stderr)
        fixture.assert_untouched(self)

    def test_removed_modes_are_rejected_without_installing_or_writing_artifacts(self):
        fixture = self.fixture()
        result = subprocess.run(["/bin/bash", str(PROGRAM), "--compare", "first.json", "second.json"],
                                env=fixture.env, cwd=fixture.cwd, text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("takes no arguments", result.stderr)
        self.assertEqual(fixture.reports(), [])
        self.assertFalse([call for call in fixture.commands() if call["tool"] == "apt-get"])
        fixture.assert_untouched(self)

    def test_missing_tools_are_installed_automatically_without_real_package_changes(self):
        fixture = self.fixture(missing=("sysbench", "fio"))
        result = fixture.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        apt_calls = [command["args"] for command in fixture.commands() if command["tool"] == "apt-get"]
        self.assertTrue(any("update" in args for args in apt_calls), apt_calls)
        install = next((args for args in apt_calls if "install" in args), [])
        self.assertIn("sysbench", install)
        self.assertIn("fio", install)
        self.assertEqual(len(fixture.reports()), 1)
        fixture.assert_untouched(self)

    def test_report_filename_collision_preserves_previous_report(self):
        fixture = self.fixture()
        first = fixture.run()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        original = fixture.reports()[0]
        original_content = original.read_bytes()
        second = fixture.run()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(original.read_bytes(), original_content)
        self.assertEqual(len(fixture.reports()), 2)
        fixture.assert_untouched(self)

    def test_fio_failure_is_reported_without_zero_or_success_metrics(self):
        fixture = self.fixture()
        fixture.env["OCTO_TEST_FIO_ERROR"] = "5"
        result = fixture.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(fixture.reports()[0].read_text())
        self.assertTrue(report["warnings"])
        self.assertFalse([metric for metric in report["measurements"] if "fio" in str(metric["tool"])])
        fixture.assert_untouched(self)

    def test_median_uses_all_samples_and_unavailable_percentile_is_omitted(self):
        fixture = self.fixture()
        fixture.env["OCTO_TEST_CPU_SAMPLES"] = "[30, 10, 20]"
        fixture.env["OCTO_TEST_FIO_NO_P99"] = "1"
        result = fixture.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(fixture.reports()[0].read_text())
        cpu = [metric for metric in report["measurements"] if metric["id"].startswith("cpu.sysbench.")]
        self.assertTrue(cpu)
        for metric in cpu:
            self.assertEqual(metric["samples"], [30, 10, 20])
            self.assertEqual(metric["value"], 20)
        self.assertTrue(any("fio" in str(metric["tool"]) for metric in report["measurements"]))
        self.assertFalse(any("latency_p99" in metric["id"] for metric in report["measurements"]))
        fixture.assert_untouched(self)

    def test_adaptive_memory_allocation_rounds_down_to_valid_power_of_two(self):
        fixture = self.fixture()
        fixture.env["OCTO_TEST_ADAPTIVE_MEMORY"] = "1"
        result = fixture.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(fixture.reports()[0].read_text())
        memory = [metric for metric in report["measurements"] if metric["id"].startswith("memory.sysbench.")]
        self.assertEqual(len(memory), 2)
        for metric in memory:
            self.assertEqual(metric["parameters"]["block_mib"], 128)
        fixture.assert_untouched(self)

    def test_failed_final_report_serialization_still_cleans_all_owned_data(self):
        fixture = self.fixture()
        fixture.env["OCTO_TEST_FAIL_FINAL_JQ"] = "1"
        result = fixture.run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Could not save the Desktop report", result.stderr)
        self.assertEqual(fixture.reports(), [])
        fixture.assert_untouched(self)

    def test_writable_usb_target_is_tested_without_touching_existing_files(self):
        fixture = self.fixture(usb=True)
        result = fixture.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(fixture.reports()[0].read_text())
        self.assertTrue(any(str(fixture.usb) in json.dumps(target) for target in report["targets"]), report["targets"])
        usb_targets = {target["id"] for target in report["targets"] if target.get("directory") == str(fixture.usb)}
        self.assertTrue(any(metric["target"] in usb_targets for metric in report["measurements"]), report["measurements"])
        fixture.assert_untouched(self)

    def test_interrupt_stops_active_child_cleans_temporary_data_and_saves_partial_report(self):
        for interrupt, expected in ((signal.SIGTERM, 143), (signal.SIGINT, 130)):
            with self.subTest(signal=interrupt):
                self.assert_interrupted(interrupt, expected)

    def assert_interrupted(self, interrupt, expected):
        fixture = self.fixture()
        started = fixture.root / "cpu-started"
        fixture.env["OCTO_TEST_BLOCK_CPU"] = str(started)
        output = fixture.root / "interrupted.log"
        with output.open("w") as stream:
            process = subprocess.Popen(["/bin/bash", "-c", 'exec bash <(cat "$1")',
                                        "octobench-test", str(PROGRAM)], env=fixture.env,
                                       cwd=fixture.cwd, stdout=stream, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            try:
                deadline = time.monotonic() + 20
                while not started.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(started.exists(), output.read_text())
                process.send_signal(interrupt)
                status = process.wait(timeout=15)
                self.assertEqual(status, expected, output.read_text())
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        reports = fixture.reports()
        self.assertEqual(len(reports), 1, output.read_text())
        report = json.loads(reports[0].read_text())
        self.assertEqual(report["status"], "interrupted")
        fixture.assert_untouched(self)


if __name__ == "__main__":
    unittest.main()
