"""Regression tests for parsing, data integrity, cleanup and fair comparison."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("octobench", ROOT / "src/lib/octobench.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


class ParserTests(unittest.TestCase):
    def test_sysbench_missing_is_not_zero(self):
        self.assertEqual(bench.parse_cpu("events per second: 1234.56"), 1234.56)
        with self.assertRaises(ValueError):
            bench.parse_cpu("CPU test failed")

    def test_stream_requires_numerical_validation(self):
        text = "Copy: 1000.0 0.1\nScale: 2000.0 0.1\nAdd: 3000.0 0.1\nTriad: 4000.0 0.1\nSolution Validates"
        self.assertEqual(bench.parse_stream(text)["triad"], 4000)
        with self.assertRaises(ValueError):
            bench.parse_stream(text.replace("Solution Validates", "Failed Validation"))

    def test_fio_units_and_p99(self):
        data = {"jobs": [{"error": 0, "read": {"bw_bytes": 2 * bench.MIB, "iops": 42,
                "io_bytes": 8192, "clat_ns": {"mean": 12000, "percentile": {"99.000000": 45000}}}}]}
        result = bench.parse_fio(data, "read")
        self.assertEqual(result["bandwidth"], 2)
        self.assertEqual(result["latency_mean"], 12)
        self.assertEqual(result["latency_p99"], 45)
        self.assertEqual(result["bytes"], 8192)

    def test_fio_error_and_missing_percentile(self):
        with self.assertRaises(ValueError):
            bench.parse_fio({"jobs": [{"error": 5}]}, "read")
        data = {"jobs": [{"error": 0, "read": {"bw": 1024, "clat_us": {"mean": 3}}}]}
        self.assertIsNone(bench.parse_fio(data, "read")["latency_p99"])

    def test_usb_transport_inherited_by_partition(self):
        disks = bench.flatten_disks([{"kname": "sdb", "tran": "usb", "children": [
            {"kname": "sdb1", "tran": None, "mountpoints": ["/media/test"]}]}])
        self.assertEqual(disks[1]["transport"], "usb")
        self.assertEqual(disks[1]["ancestors"], ["sdb", "sdb1"])

    def test_negative_arguments_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            bench.parser().parse_args(["--duration", "0"])

    def test_pinned_stream_source(self):
        self.assertEqual(hashlib.sha256((ROOT / "vendor/stream/stream.c").read_bytes()).hexdigest(), bench.STREAM_SHA256)


class TransferTests(unittest.TestCase):
    def test_dataset_contains_hidden_nested_and_verifies_content(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "dataset"
            size = bench.make_dataset(path, "large", {"transfer_mib": 1, "files": 10})
            self.assertEqual(size, bench.MIB + 1024)
            self.assertEqual(len(bench.manifest(path)), 2)
            self.assertTrue((path / ".hidden").is_file())
            self.assertTrue((path / "nested/large.bin").is_file())
            original = bench.manifest(path)
            (path / ".hidden").write_bytes(b"tampered")
            self.assertNotEqual(bench.manifest(path), original)

    def test_move_preserves_unrelated_files_and_reports_no_byte_rate(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(bench, "desktop", return_value=Path(temp) / "Desktop"):
            root = Path(temp)
            sentinel = root / "existing.txt"
            sentinel.write_text("keep me")
            args = bench.parser().parse_args(["--profile", "quick"])
            config = {"duration": 1, "repeats": 1, "size_mib": 4, "transfer_mib": 1, "files": 5}
            runner = bench.Runner(args, config)
            runner.data["tool_versions"] = {"mv": "test-version"}
            owned = runner.temp(root)
            with contextlib.redirect_stdout(io.StringIO()):
                runner.transfer({"id": "target1"}, owned, owned, "mv", "small")
            rate = next(m for m in runner.data["metrics"] if m["id"].endswith(".rate"))
            self.assertEqual(rate["unit"], "files/s")
            self.assertEqual(sentinel.read_text(), "keep me")
            self.assertFalse(list(owned.iterdir()))
            self.assertTrue(runner.data["tests"][0]["transfer"]["sha256_verified"])

    def test_command_timeout_terminates_child(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(bench, "desktop", return_value=Path(temp)):
            args = bench.parser().parse_args([])
            runner = bench.Runner(args, bench.PROFILES["quick"])
            with contextlib.redirect_stdout(io.StringIO()):
                _, _, info = runner.command(["python3", "-c", "import time; time.sleep(30)"], "timeout-test", timeout=0.05)
            self.assertEqual(info["status"], "failed")
            self.assertIn("exceeded", info["error"])


class ComparisonTests(unittest.TestCase):
    def metric(self):
        return {"id": "cpu.sysbench.single", "label": "CPU", "unit": "events/s", "value": 20,
                "tool_version": "sysbench 1.0.20", "parameters": {"threads": 1, "duration_seconds": 30},
                "target": "system", "sample_count": 3}

    def test_signature_changes_with_workload_and_version(self):
        first, second = self.metric(), self.metric()
        self.assertEqual(bench.comparison_signature(first), bench.comparison_signature(second))
        second["parameters"]["threads"] = 8
        self.assertNotEqual(bench.comparison_signature(first), bench.comparison_signature(second))

    def test_all_cpu_policy_compares_different_core_counts(self):
        first, second = self.metric(), self.metric()
        for metric in [first, second]:
            metric['id'] = 'cpu.sysbench.all'
            metric['parameters']['thread_policy'] = 'all-affinity'
        second['parameters']['threads'] = 32
        self.assertEqual(bench.comparison_signature(first), bench.comparison_signature(second))
        second = {**first, "parameters": dict(first["parameters"])}
        second["tool_version"] = "different"
        self.assertNotEqual(bench.comparison_signature(first), bench.comparison_signature(second))

    def test_comparison_does_not_merge_incompatible_results(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(bench, "desktop", return_value=Path(temp)):
            paths = []
            for index in range(2):
                metric = self.metric()
                metric["parameters"]["threads"] = index + 1
                path = Path(temp) / f"system-{index}.json"
                path.write_text(json.dumps({"schema_version": "1.0", "metrics": [metric], "system_name": f"PC{index}",
                                            "started_at": "2026-10-02", "status": "completed"}))
                paths.append(str(path))
            with contextlib.redirect_stdout(io.StringIO()):
                bench.compare(paths)
            report = next(Path(temp).glob("octobench_comparison_*.md")).read_text()
            self.assertEqual(report.count("unavailable / different settings"), 2)
            self.assertIn("Workload signatures", report)

    def test_desktop_falls_back_when_xdg_returns_home(self):
        with mock.patch.object(bench.shutil, "which", return_value="/usr/bin/xdg-user-dir"), \
             mock.patch.object(bench.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, str(Path.home()), "")):
            self.assertEqual(bench.desktop(), Path.home() / "Desktop")

    def test_cgroup_memory_limit_is_respected(self):
        contents = {"/proc/self/cgroup": "0::/", "/sys/fs/cgroup/memory.max": "1000000000",
                    "/sys/fs/cgroup/memory.current": "200000000"}
        with mock.patch.object(bench, "memory_info", return_value={"MemAvailable": 4000000000}), \
             mock.patch.object(bench, "read_text", side_effect=lambda p, default="": contents.get(str(p), default)):
            self.assertEqual(bench.effective_memory(), 800000000)

    def test_cgroup_reclaimable_cache_is_available(self):
        contents = {"/proc/self/cgroup": "0::/", "/sys/fs/cgroup/memory.max": "1000000000",
                    "/sys/fs/cgroup/memory.current": "950000000",
                    "/sys/fs/cgroup/memory.stat": "inactive_file 600000000\nactive_anon 300000000"}
        with mock.patch.object(bench, "memory_info", return_value={"MemAvailable": 4000000000}), \
             mock.patch.object(bench, "read_text", side_effect=lambda p, default="": contents.get(str(p), default)):
            self.assertEqual(bench.effective_memory(), 650000000)


class LauncherTests(unittest.TestCase):
    def test_installer_installs_missing_tools_and_forwards_arguments(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = root / 'bin'
            binary.mkdir()
            for command in ['dirname', 'grep', 'head']:
                (binary / command).symlink_to('/usr/bin/' + command)
            for command in ['gcc', 'openssl', 'lscpu', 'lsblk', 'findmnt', 'lspci', 'lsusb',
                            'dmidecode', 'sensors', 'rsync', 'xdg-user-dir']:
                (binary / command).symlink_to('/bin/true')
            scripts = {
                'fio': '#!/bin/bash\nprintf "fio-3.36\\n"\n',
                'apt-get': '#!/bin/bash\nprintf "%s\\n" "$*" >> "$APT_LOG"\n',
                'sudo': '#!/bin/bash\nexec "$@"\n',
                'python3': '#!/bin/bash\nprintf "%s\\n" "$*" > "$BACKEND_LOG"\n',
            }
            for name, content in scripts.items():
                path = binary / name
                path.write_text(content)
                path.chmod(0o755)
            env = {**os.environ, 'PATH': str(binary), 'APT_LOG': str(root / 'apt.log'),
                   'BACKEND_LOG': str(root / 'backend.log')}
            env.pop('SUDO_USER', None)
            result = subprocess.run(['/bin/bash', str(ROOT / 'src/octobench'), '--profile', 'quick'],
                                    env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            commands = (root / 'apt.log').read_text().splitlines()
            self.assertEqual(commands[0], 'update')
            self.assertTrue(commands[1].startswith('install -y python3 sysbench fio'))
            self.assertIn('--profile quick', (root / 'backend.log').read_text())

    def test_help_does_not_install_dependencies(self):
        result = subprocess.run(['/bin/bash', str(ROOT / 'src/octobench'), '--help'],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('Installing benchmark dependencies', result.stdout)
        self.assertIn('--compare', result.stdout)


if __name__ == "__main__":
    unittest.main()
