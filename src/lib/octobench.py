#!/usr/bin/env python3
"""Octobench's standard-library backend: measurements, evidence, and reports."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid

VERSION = "1.0.0"
ROOT = Path(__file__).resolve().parents[2]
MIB = 1024 ** 2
GIB = 1024 ** 3
STREAM_SHA256 = "c388924eb140fda95f534cdb808ae7f1f8ebb18da41d8aec1b512a3c8d303c9b"
PROFILES = {
    "quick": dict(duration=5, repeats=1, size_mib=256, transfer_mib=64, files=200),
    "standard": dict(duration=30, repeats=3, size_mib=2048, transfer_mib=512, files=2000),
    "full": dict(duration=60, repeats=3, size_mib=8192, transfer_mib=2048, files=10000),
}


def read_text(path: Path | str, default: str = "") -> str:
    try:
        return Path(path).read_text(errors="replace").strip()
    except OSError:
        return default


def number(text: str, default=0):
    try:
        return int(text)
    except (ValueError, TypeError):
        return default


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", text)[:100] or "system"


def desktop() -> Path:
    if shutil.which("xdg-user-dir"):
        result = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, check=False)
        path = Path(result.stdout.strip())
        if result.returncode == 0 and path.is_absolute() and path != Path.home():
            return path
    return Path.home() / "Desktop"


def memory_info() -> dict:
    result = {}
    for line in read_text("/proc/meminfo").splitlines():
        key, _, value = line.partition(":")
        result[key] = number(value.split()[0]) * 1024
    return result


def effective_memory() -> int:
    available = memory_info().get("MemAvailable", 0)
    # Resolve the calling process's cgroup, including non-root cgroup paths.
    groups = read_text("/proc/self/cgroup").splitlines()
    for group in groups:
        parts = group.split(":", 2)
        if len(parts) != 3:
            continue
        if parts[0] == "0":
            roots = [Path("/sys/fs/cgroup"), Path("/sys/fs/cgroup") / parts[2].lstrip("/")]
            for root in roots:
                limit = number(read_text(root / "memory.max"))
                used = number(read_text(root / "memory.current"))
                if limit:
                    available = min(available, max(0, limit - used))
        elif "memory" in parts[1].split(","):
            root = Path("/sys/fs/cgroup/memory") / parts[2].lstrip("/")
            limit = number(read_text(root / "memory.limit_in_bytes"))
            used = number(read_text(root / "memory.usage_in_bytes"))
            if limit:
                available = min(available, max(0, limit - used))
    return available


def cache_bytes() -> int:
    """Sum distinct highest-level caches used by CPUs in our affinity set."""
    entries = {}
    levels = []
    for cpu in sorted(os.sched_getaffinity(0)):
        for index in Path(f"/sys/devices/system/cpu/cpu{cpu}/cache").glob("index*"):
            if read_text(index / "type") not in {"Unified", "Data"}:
                continue
            level = number(read_text(index / "level"))
            size = read_text(index / "size")
            match = re.fullmatch(r"(\d+)([KMG]?)", size)
            if not match:
                continue
            amount = int(match[1]) * {"": 1, "K": 1024, "M": MIB, "G": GIB}[match[2]]
            key = (level, read_text(index / "shared_cpu_list", str(cpu)))
            entries[key] = amount
            levels.append(level)
    maximum = max(levels, default=0)
    return sum(value for (level, _), value in entries.items() if level == maximum)


def flatten_disks(nodes: list, parents: list | None = None) -> list:
    result = []
    for node in nodes:
        ancestors = (parents or []) + [node]
        transport = next((n.get("tran") for n in reversed(ancestors) if n.get("tran")), None)
        result.append({**{k: v for k, v in node.items() if k != "children"},
                       "transport": transport,
                       "ancestors": [n.get("kname") for n in ancestors]})
        result.extend(flatten_disks(node.get("children", []), ancestors))
    return result


def parse_cpu(text: str) -> float:
    match = re.search(r"events per second:\s*([\d.]+)", text)
    if not match:
        raise ValueError("Sysbench did not return events per second")
    return float(match[1])


def parse_stream(text: str) -> dict:
    if "Solution Validates" not in text:
        raise ValueError("STREAM numerical validation failed or is absent")
    result = {}
    for name, value in re.findall(r"^(Copy|Scale|Add|Triad):\s+([\d.]+)", text, re.M):
        result[name.lower()] = float(value)
    if len(result) != 4:
        raise ValueError("STREAM did not return all four kernels")
    return result


def parse_fio(data: dict, direction: str) -> dict:
    jobs = data.get("jobs", [])
    if len(jobs) != 1 or jobs[0].get("error", 0):
        raise ValueError("fio job missing or returned an I/O error")
    value = jobs[0][direction]
    bandwidth = value.get("bw_bytes", value.get("bw", 0) * 1024)
    clat = value.get("clat_ns", {})
    scale = 1000
    if not clat:
        clat = value.get("clat_us", {})
        scale = 1
    pcts = clat.get("percentile", {})
    p99 = next((v for k, v in pcts.items() if float(k) == 99), None)
    return {"bandwidth": bandwidth / MIB, "iops": float(value.get("iops", 0)),
            "latency_mean": clat.get("mean", 0) / scale,
            "latency_p99": p99 / scale if p99 is not None else None,
            "bytes": value.get("io_bytes", value.get("io_kbytes", 0) * 1024)}


def manifest(path: Path) -> dict:
    result = {}
    for file in sorted(path.rglob("*")):
        if file.is_file():
            digest = hashlib.sha256()
            with file.open("rb") as stream:
                for chunk in iter(lambda: stream.read(MIB), b""):
                    digest.update(chunk)
            result[str(file.relative_to(path))] = digest.hexdigest()
    return result


def flush_tree(path: Path) -> None:
    """Flush destination data and directory entries inside the timed interval."""
    files = [p for p in path.rglob("*") if p.is_file()]
    directories = [p for p in path.rglob("*") if p.is_dir()] + [path]
    for entry in files + directories:
        descriptor = os.open(entry, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def evict_source(path: Path) -> bool:
    if not hasattr(os, "posix_fadvise"):
        return False
    try:
        for file in path.rglob("*"):
            if file.is_file():
                with file.open("rb") as stream:
                    os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        return True
    except OSError:
        return False


def make_dataset(path: Path, kind: str, config: dict) -> int:
    path.mkdir()
    # Dotfiles and nested directories are deliberately included.
    (path / "nested").mkdir()
    if kind == "large":
        size = config["transfer_mib"] * MIB
        with (path / "nested" / "large.bin").open("wb") as stream:
            remaining = size
            while remaining:
                chunk = os.urandom(min(MIB, remaining))
                stream.write(chunk)
                remaining -= len(chunk)
        (path / ".hidden").write_bytes(os.urandom(1024))
        size += 1024
    else:
        size = config["files"] * 16384
        for index in range(config["files"]):
            folder = path if index % 2 else path / "nested"
            (folder / f".file-{index:06d}.bin").write_bytes(os.urandom(16384))
    flush_tree(path)
    return size


class Runner:
    def __init__(self, args, config):
        self.args, self.config = args, config
        self.output = desktop()
        self.output.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S%z")
        self.stem = f"octobench_{safe_name(platform.node())}_{stamp}_{uuid.uuid4().hex[:6]}"
        self.raw = self.output / (self.stem + "_raw")
        self.raw.mkdir(mode=0o700)
        self.temporary = []
        self.counter = 0
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.active = None
        self.monitor_thread = None
        self.data = {
            "schema_version": "1.0", "octobench_version": VERSION,
            "system_name": platform.node(), "started_at": dt.datetime.now().astimezone().isoformat(),
            "status": "running", "profile": args.profile, "configuration": config,
            "metrics": [], "tests": [], "warnings": [], "targets": [],
            "monitoring": [], "raw_directory": self.raw.name,
            "comparison_guidance": [
                "Compare matching benchmark IDs, tool versions, and parameters; use median values.",
                "Missing and failed results are unavailable, never zero performance.",
                "USB link Mbps is a signalling rate, not measured disk throughput.",
                "Same-filesystem move is a metadata rename; cross-filesystem move copies and deletes.",
                "MiB/s uses 2^20 bytes; STREAM MB/s uses 10^6 bytes.",
                "There is no universal score or world percentile; public comparisons need identical workloads.",
            ],
            "references": {
                "sysbench": "https://github.com/akopytov/sysbench",
                "stream": "https://www.cs.virginia.edu/stream/ref.html",
                "fio": "https://fio.readthedocs.io/en/latest/fio_doc.html",
                "openssl": "https://docs.openssl.org/master/man1/openssl-speed/",
                "public_comparisons": "https://openbenchmarking.org/",
            },
        }

    def warning(self, message):
        self.data["warnings"].append(message)
        print(f"  Warning: {message}", flush=True)

    def command(self, argv, label, timeout=120, env=None, acceptable_exits=(0,)):
        self.counter += 1
        filename = f"{self.counter:04d}_{safe_name(label)}.txt"
        log = self.raw / filename
        started = time.monotonic()
        print(f"[{self.counter:03d}] {label}", flush=True)
        info = {"label": label, "argv": argv, "raw_file": filename,
                "started_at": dt.datetime.now().astimezone().isoformat()}
        stdout, stderr = "", ""
        process = None
        try:
            process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, errors="replace", env=env, start_new_session=True)
            with self.lock:
                self.active = process
            deadline = time.monotonic() + timeout
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=min(10, max(0.01, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        os.killpg(process.pid, signal.SIGKILL)
                        stdout, stderr = process.communicate()
                        raise TimeoutError(f"Command exceeded {timeout}s")
                    print(f"      running ({time.monotonic() - started:.0f}s elapsed)", flush=True)
            info["exit_code"] = process.returncode
            if process.returncode not in acceptable_exits:
                raise RuntimeError(f"Exit {process.returncode}: {stderr[-500:]}")
            info["status"] = "ok"
        except (OSError, RuntimeError, TimeoutError) as error:
            info.update(status="failed", error=str(error))
            self.warning(f"{label}: {error}")
        except KeyboardInterrupt:
            if process and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    stdout, stderr = process.communicate()
            info.update(status="interrupted", error="Interrupted by user")
            raise
        finally:
            with self.lock:
                self.active = None
            info["wall_seconds"] = time.monotonic() - started
            log.write_text(f"$ {shlex.join(argv)}\n\nSTDOUT\n{stdout}\n\nSTDERR\n{stderr}\n")
            self.data["tests"].append(info)
        return stdout, stderr, info

    def metric(self, metric_id, label, samples, unit, parameters, tool, target="system", higher=True):
        samples = [float(value) for value in samples if value is not None and math.isfinite(float(value))]
        if not samples:
            return
        median = statistics.median(samples)
        self.data["metrics"].append({
            "id": metric_id, "label": label, "target": target, "unit": unit,
            "higher_is_better": higher, "value": median, "aggregation": "median",
            "samples": samples, "sample_count": len(samples), "min": min(samples), "max": max(samples),
            "coefficient_of_variation_percent": (statistics.pstdev(samples) / statistics.mean(samples) * 100
                                                   if statistics.mean(samples) else 0),
            "parameters": parameters, "tool": tool,
            "tool_version": self.data.get("tool_versions", {}).get(tool, "unknown"),
        })
        print(f"      {label}: {median:,.3f} {unit}", flush=True)

    def temp(self, directory: Path) -> Path:
        path = Path(tempfile.mkdtemp(prefix=".octobench-", dir=directory))
        self.temporary.append(path)
        return path

    def inventory(self):
        print("Collecting hardware and current operating conditions…", flush=True)
        inv = {"kernel": platform.release(), "architecture": platform.machine(),
               "os_release": read_text("/etc/os-release"), "logical_cpus": os.cpu_count(),
               "affinity_cpus": sorted(os.sched_getaffinity(0)), "memory": memory_info(),
               "effective_available_memory_bytes": effective_memory(), "llc_bytes": cache_bytes(),
               "load_average_at_start": list(os.getloadavg()),
               "cgroup_membership": read_text("/proc/self/cgroup"),
               "cgroup_cpu_max": read_text("/sys/fs/cgroup/cpu.max"),
               "virtualization": "unknown"}
        for name, argv in {
            "cpu": ["lscpu", "-J"],
            "block_devices": ["lsblk", "-J", "-b", "-o", "NAME,KNAME,PATH,TYPE,SIZE,MODEL,TRAN,ROTA,FSTYPE,MOUNTPOINTS,MAJ:MIN"],
            "pci_links": ["lspci", "-vv"], "usb_tree": ["lsusb", "-t"],
            "sensors": ["sensors", "-j"], "virtualization": ["systemd-detect-virt"],
        }.items():
            if not shutil.which(argv[0]):
                inv[name] = {"unavailable": f"{argv[0]} is not installed"}
                continue
            out, _, info = self.command(argv, f"inventory-{name}", acceptable_exits=(0, 1) if name == "virtualization" else (0,))
            if name == "virtualization":
                # systemd-detect-virt exits 1 on physical machines.
                inv[name] = out.strip() or "none"
            elif info["status"] == "ok":
                try:
                    inv[name] = json.loads(out)
                except ValueError:
                    inv[name] = out.strip()
            else:
                inv[name] = {"unavailable": info.get("error")}
        inv["dmi"] = {}
        privilege = []
        if os.geteuid() != 0 and self.args.privileged_inventory:
            if shutil.which("sudo"):
                subprocess.run(["sudo", "-v"], check=False)
                privilege = ["sudo", "-n"]
        if shutil.which("dmidecode"):
            for kind in ["baseboard", "memory", "processor"]:
                out, _, info = self.command(privilege + ["dmidecode", "--type", kind], f"inventory-dmi-{kind}")
                # Avoid including hardware serial numbers and UUIDs in sharable reports.
                out = re.sub(r"(?m)^.*(?:Serial Number:|UUID:|Asset Tag:).*(?:\n|$)", "", out)
                (self.raw / info["raw_file"]).write_text(out)
                inv["dmi"][kind] = out if info["status"] == "ok" else {"unavailable": info.get("error")}
        inv["usb_devices"] = []
        for device in sorted(Path("/sys/bus/usb/devices").glob("*")):
            speed = read_text(device / "speed")
            if speed:
                inv["usb_devices"].append({
                    "port": device.name, "speed_mbps": float(speed),
                    "manufacturer": read_text(device / "manufacturer"),
                    "product": read_text(device / "product"),
                    "vendor_id": read_text(device / "idVendor"),
                    "product_id": read_text(device / "idProduct"),
                })
        inv["pci_devices"] = []
        for device in sorted(Path("/sys/bus/pci/devices").glob("*")):
            current = read_text(device / "current_link_speed")
            if current:
                inv["pci_devices"].append({"address": device.name, "current_link_speed": current,
                                           "current_link_width": read_text(device / "current_link_width"),
                                           "max_link_speed": read_text(device / "max_link_speed"),
                                           "max_link_width": read_text(device / "max_link_width")})
        inv["power"] = {p.name: {name: read_text(p / name) for name in ["type", "status", "online", "capacity"]}
                        for p in Path("/sys/class/power_supply").glob("*")}
        inv["cpu_policies"] = {p.name: {name: read_text(p / name) for name in [
            "scaling_driver", "scaling_governor", "energy_performance_preference", "scaling_min_freq", "scaling_max_freq"]}
            for p in Path("/sys/devices/system/cpu/cpufreq").glob("policy*")}
        self.data["inventory"] = inv
        versions = {"octobench-transfer": VERSION, "STREAM": "5.10 / " + STREAM_SHA256}
        for tool, argv in {"sysbench": ["sysbench", "--version"], "fio": ["fio", "--version"],
                           "gcc": ["gcc", "--version"], "openssl": ["openssl", "version"],
                           "cp": ["cp", "--version"], "mv": ["mv", "--version"],
                           "rsync": ["rsync", "--version"]}.items():
            if shutil.which(tool):
                out, _, info = self.command(argv, f"version-{tool}")
                versions[tool] = out.splitlines()[0] if out.strip() and info["status"] == "ok" else "unknown"
        self.data["tool_versions"] = versions
        if inv["virtualization"] != "none":
            self.warning("Virtualization/container detected or unknown; results reflect this environment's limits.")
        if any(p.get("status") == "Discharging" for p in inv["power"].values()):
            self.warning("Running on battery; power limits may reduce measured performance.")

    def monitor(self):
        while not self.stop.is_set():
            sample = {"time": dt.datetime.now().astimezone().isoformat(),
                      "load_average": list(os.getloadavg()),
                      "memory_available_bytes": memory_info().get("MemAvailable"),
                      "swap_free_bytes": memory_info().get("SwapFree"),
                      "frequencies_khz": {}, "temperatures_c": {}, "throttle_counts": {}}
            for policy in Path("/sys/devices/system/cpu/cpufreq").glob("policy*"):
                sample["frequencies_khz"][policy.name] = number(read_text(policy / "scaling_cur_freq"))
            for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
                temp = read_text(zone / "temp")
                if temp:
                    sample["temperatures_c"][zone.name + ":" + read_text(zone / "type")] = number(temp) / 1000
            for sensor in Path("/sys/class/hwmon").glob("hwmon*/temp*_input"):
                label = read_text(sensor.with_name(sensor.name.replace("_input", "_label")), sensor.stem)
                key = read_text(sensor.parent / "name", sensor.parent.name) + ":" + label
                sample["temperatures_c"][key] = number(read_text(sensor)) / 1000
            for counter in Path("/sys/devices/system/cpu").glob("cpu*/thermal_throttle/*_throttle_count"):
                sample["throttle_counts"][str(counter)] = number(read_text(counter))
            self.data["monitoring"].append(sample)
            self.stop.wait(2)

    def cpu(self):
        threads = len(os.sched_getaffinity(0))
        if shutil.which("sysbench"):
            for label, count in [("single", 1), ("all", threads)]:
                samples = []
                params = {"threads": count, "prime_limit": 20000, "duration_seconds": self.config["duration"]}
                for repeat in range(self.config["repeats"]):
                    out, _, info = self.command(["sysbench", "cpu", f"--threads={count}", "--cpu-max-prime=20000",
                                                 f"--time={self.config['duration']}", "run"],
                                                f"cpu-{label}-repeat-{repeat + 1}", self.config["duration"] + 120)
                    if info["status"] == "ok":
                        try:
                            samples.append(parse_cpu(out))
                        except ValueError as error:
                            self.warning(str(error))
                self.metric(f"cpu.sysbench.{label}", f"CPU {label}-thread prime workload", samples,
                            "events/s", params, "sysbench")
        else:
            self.warning("CPU prime benchmark skipped: sysbench unavailable.")
        if shutil.which("openssl"):
            for algorithm in ["sha256", "aes-256-gcm"]:
                samples = []
                for repeat in range(self.config["repeats"]):
                    argv = ["openssl", "speed", "-elapsed", "-seconds", str(self.config["duration"]),
                            "-bytes", "16384", "-evp", algorithm, "-mr"]
                    if algorithm.endswith("gcm"):
                        argv.append("-aead")
                    out, err, info = self.command(argv, f"crypto-{algorithm}-{repeat + 1}", self.config["duration"] + 120)
                    if info["status"] == "ok":
                        match = re.search(r"^\+F:\d+:[^:]+:([\d.]+)", out + "\n" + err, re.M)
                        if match:
                            samples.append(float(match[1]) / MIB)
                        else:
                            self.warning(f"Unable to parse OpenSSL {algorithm} throughput.")
                self.metric(f"cpu.openssl.{algorithm}", f"OpenSSL {algorithm} single-process", samples, "MiB/s",
                            {"block_bytes": 16384, "elapsed_time": True, "aead": algorithm.endswith("gcm"),
                             "duration_seconds": self.config["duration"]}, "openssl")

    def memory(self):
        source = ROOT / "vendor" / "stream" / "stream.c"
        if not shutil.which("gcc") or not source.is_file():
            self.warning("Memory benchmark skipped: gcc or vendored STREAM source unavailable.")
            return
        if hashlib.sha256(source.read_bytes()).hexdigest() != STREAM_SHA256:
            self.warning("Memory benchmark skipped: STREAM source hash does not match the pinned upstream source.")
            return
        llc = cache_bytes()
        required = max(1_000_000, math.ceil(4 * llc / 8))
        budget = min(effective_memory() // 4, 2 * GIB)
        if self.args.profile == "quick":
            budget = min(budget, 192 * MIB)
        elements = min(max(required, 16_000_000), budget // 24)
        if elements < 1_000_000:
            self.warning("Memory benchmark skipped: insufficient available memory.")
            return
        directory = self.temp(self.output)
        binary = directory / "stream"
        flags = ["-O3", "-march=native", "-fopenmp", f"-DSTREAM_ARRAY_SIZE={elements}", "-DNTIMES=20"]
        # Large static arrays on x86-64 need the medium code model.
        if elements * 24 >= 2 * GIB and platform.machine() == "x86_64":
            flags.append("-mcmodel=medium")
        _, _, info = self.command(["gcc", *flags, str(source), "-o", str(binary)], "compile-stream", 180)
        if info["status"] != "ok":
            return
        meets_cache = bool(llc) and elements >= required
        if not meets_cache:
            self.warning("STREAM array sizing does not meet the detected-cache rule or cache size is unknown; label as STREAM-derived.")
        for label, count in [("single", 1), ("all", len(os.sched_getaffinity(0)))]:
            values = {name: [] for name in ["copy", "scale", "add", "triad"]}
            params = {"threads": count, "array_elements": elements, "array_bytes": elements * 8,
                      "total_bytes": elements * 24, "iterations": 20, "compiler_flags": flags,
                      "compiler_version": self.data["tool_versions"].get("gcc"), "llc_bytes": llc,
                      "detected_cache_sizing_rule_met": meets_cache,
                      "publication_label": "STREAM-derived; full Run Rules compliance not certified",
                      "omp_proc_bind": "spread", "omp_places": "cores"}
            env = {**os.environ, "OMP_NUM_THREADS": str(count), "OMP_PROC_BIND": "spread", "OMP_PLACES": "cores"}
            for repeat in range(self.config["repeats"]):
                out, _, info = self.command([str(binary)], f"stream-{label}-{repeat + 1}", 600, env)
                if info["status"] == "ok":
                    try:
                        for kernel, value in parse_stream(out).items():
                            values[kernel].append(value)
                    except ValueError as error:
                        self.warning(str(error))
            for kernel, samples in values.items():
                self.metric(f"memory.stream.{label}.{kernel}", f"STREAM-derived {label}-thread {kernel}",
                            samples, "MB/s", params, "STREAM")

    def discover_targets(self):
        devices = self.data["inventory"].get("block_devices", {})
        flat = flatten_disks(devices.get("blockdevices", [])) if isinstance(devices, dict) else []
        paths = [(Path(path), "storage") for path in (self.args.storage or [str(Path.home())])]
        paths += [(Path(path), "usb-explicit") for path in self.args.usb]
        if not self.args.no_auto_usb:
            for disk in flat:
                if disk.get("transport") == "usb":
                    paths += [(Path(p), "usb-auto") for p in (disk.get("mountpoints") or []) if p]
        seen = set()
        for directory, kind in paths:
            directory = directory.expanduser().resolve()
            if not directory.is_dir() or not os.access(directory, os.W_OK | os.X_OK):
                self.warning(f"Storage target skipped (missing directory or not writable): {directory}")
                continue
            if str(directory) in seen:
                continue
            seen.add(str(directory))
            out, _, info = self.command(["findmnt", "-J", "-T", str(directory), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS,MAJ:MIN"],
                                        f"mount-{len(self.data['targets']) + 1}")
            mount = {}
            if info["status"] == "ok":
                try:
                    mount = json.loads(out)["filesystems"][0]
                except (ValueError, KeyError, IndexError):
                    pass
            disk = next((d for d in flat if d.get("maj:min") == mount.get("maj:min")), {})
            backing = next((d for d in flat if d.get("kname") == (disk.get("ancestors") or [None])[0]), disk)
            usb_link = None
            for ancestor in reversed(disk.get("ancestors", [])):
                device_path = (Path("/sys/class/block") / str(ancestor) / "device").resolve()
                for parent in [device_path, *device_path.parents]:
                    if (parent / "idVendor").exists() and (parent / "speed").exists():
                        usb_link = {"port": parent.name, "speed_mbps": float(read_text(parent / "speed")),
                                    "product": read_text(parent / "product")}
                        break
                if usb_link:
                    break
            target = {"id": f"target{len(self.data['targets']) + 1}", "directory": str(directory),
                      "kind": kind, "mount": mount, "device": disk, "backing_device": backing,
                      "usb_link": usb_link, "free_bytes_at_start": shutil.disk_usage(directory).free,
                      "filesystem_device_id": directory.stat().st_dev}
            self.data["targets"].append(target)
            if usb_link:
                self.metric("usb.negotiated_link", "USB negotiated signalling rate", [usb_link["speed_mbps"]],
                            "Mbps", {"port": usb_link["port"], "measurement": "sysfs negotiated signalling rate"},
                            "kernel-sysfs", target["id"])
            elif kind.startswith("usb"):
                self.warning(f"{directory}: USB association unavailable; target throughput will still be measured.")
        if not self.data["targets"]:
            self.warning("No writable storage targets found.")

    def storage(self, target, directory):
        if not shutil.which("fio") or not self.data["tool_versions"].get("fio", "").startswith("fio-"):
            self.warning("Storage fio tests skipped: Flexible I/O tester unavailable (the Fiona CLI is not fio).")
            return
        filename = directory / "fio-data.bin"
        size = self.config["size_mib"] * MIB
        common = ["fio", "--name=octobench", f"--filename={filename}", f"--size={size}",
                  "--ioengine=libaio", "--direct=1", "--numjobs=1", "--group_reporting=1",
                  "--output-format=json", "--end_fsync=1", "--randrepeat=1", "--randseed=20261002"]
        out, _, info = self.command(common + ["--rw=write", "--bs=1M", "--iodepth=16"],
                                    f"{target['id']}-fio-initialize", 1800)
        if info["status"] != "ok":
            self.warning(f"{target['id']}: direct I/O initialization failed; no cached fallback will be presented as disk performance.")
            return
        try:
            parse_fio(json.loads(out), "write")
        except (ValueError, KeyError) as error:
            self.warning(f"fio initialization invalid: {error}")
            return
        workloads = [("seq_read", "read", "1M", 16), ("seq_write", "write", "1M", 16),
                     ("rand_read_qd1", "randread", "4k", 1), ("rand_read_qd32", "randread", "4k", 32),
                     ("rand_write_qd32", "randwrite", "4k", 32), ("mixed_qd32", "randrw", "4k", 32)]
        for name, rw, block, depth in workloads:
            values = {direction: {key: [] for key in ["bandwidth", "iops", "latency_mean", "latency_p99"]}
                      for direction in (["read", "write"] if rw == "randrw" else ["write" if "write" in rw else "read"])}
            params = {"rw": rw, "block_size": block, "queue_depth": depth, "jobs": 1,
                      "direct_io": True, "engine": "libaio", "file_size_bytes": size,
                      "duration_seconds": self.config["duration"], "ramp_seconds": min(5, self.config["duration"]),
                      "read_mix_percent": 70 if rw == "randrw" else None, "end_fsync": True, "seed": 20261002}
            for repeat in range(self.config["repeats"]):
                argv = common + [f"--rw={rw}", f"--bs={block}", f"--iodepth={depth}", "--time_based=1",
                                 f"--runtime={self.config['duration']}", f"--ramp_time={params['ramp_seconds']}"]
                if rw == "randrw":
                    argv.append("--rwmixread=70")
                out, _, info = self.command(argv, f"{target['id']}-fio-{name}-{repeat + 1}", self.config["duration"] + 180)
                if info["status"] != "ok":
                    continue
                try:
                    raw = json.loads(out)
                    (self.raw / (Path(info["raw_file"]).stem + ".json")).write_text(json.dumps(raw, indent=2))
                    for direction in values:
                        parsed = parse_fio(raw, direction)
                        for key in values[direction]:
                            values[direction][key].append(parsed[key])
                    target["fio_bytes_written"] = target.get("fio_bytes_written", 0) + parse_fio(raw, "write")["bytes"]
                except (ValueError, KeyError, TypeError) as error:
                    self.warning(f"fio parse failed: {error}")
            for direction, measurements in values.items():
                for key, samples in measurements.items():
                    unit = {"bandwidth": "MiB/s", "iops": "IOPS", "latency_mean": "us", "latency_p99": "us"}[key]
                    self.metric(f"storage.fio.{name}.{direction}.{key}", f"fio {name} {direction} {key}",
                                samples, unit, params, "fio", target["id"], not key.startswith("latency"))

    def transfer(self, target, source_root, dest_root, operation, kind, cross=False):
        seconds, rates = [], []
        eviction = []
        file_count = 2 if kind == "large" else self.config["files"]
        params = {"operation": operation, "dataset": kind, "cross_filesystem": cross,
                  "large_file_bytes": self.config["transfer_mib"] * MIB if kind == "large" else None,
                  "file_count": file_count, "small_file_bytes": 16384 if kind == "small" else None,
                  "flush_destination_in_timing": True, "verification": "SHA-256 outside timed interval",
                  "reflinks": False, "source_cache": "per-file POSIX_FADV_DONTNEED requested before timing",
                  "source_target": "home" if cross else target["id"], "destination_target": target["id"]}
        for repeat in range(self.config["repeats"]):
            source = source_root / f"source-{uuid.uuid4().hex}"
            dest = dest_root / f"dest-{uuid.uuid4().hex}"
            try:
                byte_count = make_dataset(source, kind, self.config)
                expected = manifest(source)
                eviction.append(evict_source(source))
                argv = (["cp", "-a", "--reflink=never", "--", str(source), str(dest)] if operation == "cp" else
                        ["rsync", "-a", "--whole-file", "--", str(source) + "/", str(dest) + "/"] if operation == "rsync" else
                        ["mv", "--", str(source), str(dest)])
                started = time.perf_counter()
                _, _, info = self.command(argv, f"{target['id']}-{operation}-{'cross' if cross else 'same'}-{kind}-{repeat + 1}", 1800)
                if info["status"] != "ok":
                    continue
                flush_tree(dest)
                descriptor = os.open(dest_root, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                if operation == "mv":
                    # Persist source-side deletion/rename metadata as well.
                    descriptor = os.open(source_root, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                elapsed = time.perf_counter() - started
                if manifest(dest) != expected:
                    raise ValueError("Transferred file hashes differ from the source dataset")
                if operation == "mv" and source.exists():
                    raise ValueError("Move left the original dataset behind")
                seconds.append(elapsed)
                rates.append(file_count / elapsed if operation == "mv" and not cross else byte_count / MIB / elapsed)
                info["transfer"] = {"bytes": byte_count, "files": file_count, "elapsed_including_flush_seconds": elapsed,
                                    "sha256_verified": True, "source_cache_eviction_requested": eviction[-1]}
            except (OSError, ValueError) as error:
                self.warning(f"Transfer {operation}/{kind} failed: {error}")
            finally:
                for path in [source, dest]:
                    if path.exists():
                        shutil.rmtree(path)
        params["source_cache_eviction_success"] = all(eviction) if eviction else False
        mode = "cross" if cross else "same"
        label = f"{operation} {mode}-filesystem {kind}-file dataset"
        tool = operation
        self.metric(f"transfer.{operation}.{mode}.{kind}.seconds", label + " time", seconds, "s", params,
                    tool, target["id"], False)
        unit = "files/s" if operation == "mv" and not cross else "MiB/s"
        self.metric(f"transfer.{operation}.{mode}.{kind}.rate", label + " rate", rates, unit, params, tool, target["id"])

    def target_tests(self):
        self.discover_targets()
        for target in self.data["targets"]:
            directory = Path(target["directory"])
            # Conservative space check leaves at least 1 GiB untouched.
            needed = max(self.config["size_mib"] * MIB,
                         2 * max(self.config["transfer_mib"] * MIB + 1024, self.config["files"] * (16384 + 8192))) + GIB
            if shutil.disk_usage(directory).free < needed:
                self.warning(f"{directory}: insufficient free space; need {needed / GIB:.2f} GiB including reserve.")
                target["status"] = "skipped-insufficient-space"
                continue
            temp = self.temp(directory)
            if not self.args.skip_storage:
                self.storage(target, temp)
            # Release fio's dataset before copy/move tests.
            fio_file = temp / "fio-data.bin"
            if fio_file.exists():
                fio_file.unlink()
            if not self.args.skip_transfer:
                for kind in ["large", "small"]:
                    for operation in ["cp", "rsync", "mv"]:
                        if shutil.which(operation):
                            self.transfer(target, temp, temp, operation, kind)
                if Path.home().stat().st_dev != directory.stat().st_dev:
                    source_needed = max(self.config["transfer_mib"] * MIB + 1024,
                                        self.config["files"] * (16384 + 8192)) + GIB
                    if shutil.disk_usage(Path.home()).free < source_needed:
                        self.warning("Cross-filesystem tests skipped: insufficient free space on home filesystem.")
                    else:
                        source_root = self.temp(Path.home())
                        for kind in ["large", "small"]:
                            for operation in ["cp", "rsync", "mv"]:
                                if shutil.which(operation):
                                    self.transfer(target, source_root, temp, operation, kind, True)
                else:
                    target["cross_filesystem_transfer_status"] = "not-applicable-home-and-target-share-filesystem"
            target["status"] = "finished"

    def save(self):
        self.data["finished_at"] = dt.datetime.now().astimezone().isoformat()
        self.data["elapsed_seconds"] = time.monotonic() - self.start_time
        metrics = self.data["metrics"]
        for metric in metrics:
            if metric["sample_count"] < self.config["repeats"] and metric["tool"] != "kernel-sysfs":
                metric["incomplete_repeats"] = True
        if self.data["monitoring"]:
            samples = self.data["monitoring"]
            temps = [value for sample in samples for value in sample["temperatures_c"].values()]
            self.data["monitoring_summary"] = {
                "sample_count": len(samples), "sampling_interval_seconds": 2,
                "maximum_reported_temperature_c": max(temps) if temps else None,
                "throttle_counter_deltas": {key: samples[-1]["throttle_counts"].get(key, value) - value
                                            for key, value in samples[0]["throttle_counts"].items()},
                "note": "Sensor availability varies. Frequency samples do not prove or disprove throttling.",
            }
        json_path = self.output / (self.stem + ".json")
        json_path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n")
        csv_path = self.output / (self.stem + ".csv")
        with csv_path.open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["system", "metric_id", "target", "median", "unit", "higher_is_better", "samples", "min", "max", "cv_percent"])
            for metric in metrics:
                writer.writerow([self.data["system_name"], metric["id"], metric["target"], metric["value"], metric["unit"],
                                 metric["higher_is_better"], metric["sample_count"], metric["min"], metric["max"],
                                 metric["coefficient_of_variation_percent"]])
        markdown = render_report(self.data)
        md_path = self.output / (self.stem + ".md")
        md_path.write_text(markdown)
        archive = self.output / (self.stem + ".tar.gz")
        with tarfile.open(archive, "w:gz") as bundle:
            for path in [json_path, csv_path, md_path, self.raw]:
                bundle.add(path, arcname=path.name)
        print(f"\nReports saved on your Desktop:\n  {md_path}\n  {json_path}\n  {csv_path}\n  {archive}", flush=True)

    def run(self):
        self.start_time = time.monotonic()
        interrupted = False
        try:
            self.inventory()
            if not self.args.inventory_only:
                self.monitor_thread = threading.Thread(target=self.monitor, daemon=True)
                self.monitor_thread.start()
                if not self.args.skip_cpu:
                    self.cpu()
                if not self.args.skip_memory:
                    self.memory()
                if not (self.args.skip_storage and self.args.skip_transfer):
                    self.target_tests()
            else:
                self.discover_targets()
            self.data["status"] = "completed-with-warnings" if self.data["warnings"] else "completed"
        except KeyboardInterrupt:
            interrupted = True
            self.data["status"] = "interrupted"
            self.warning("Interrupted; reports contain only finished measurements.")
        except Exception as error:
            self.data["status"] = "failed"
            self.warning(f"Run failed: {type(error).__name__}: {error}")
        finally:
            self.stop.set()
            if self.monitor_thread:
                self.monitor_thread.join(timeout=5)
            for path in reversed(self.temporary):
                try:
                    shutil.rmtree(path)
                except OSError as error:
                    self.warning(f"Could not clean temporary directory {path}: {error}")
            self.save()
        return 130 if interrupted else 1 if self.data["status"] == "failed" else 0


def cell(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_report(data: dict) -> str:
    inv = data.get("inventory", {})
    lines = [f"# Octobench — {cell(data['system_name'])}", "",
             f"Started: {data['started_at']}  ", f"Status: {data['status']}  ",
             f"Profile: {data['profile']} · version {data['octobench_version']}  ",
             f"Elapsed: {data.get('elapsed_seconds', 0):.1f} seconds", "",
             "## Key comparison values", "",
             "Values are medians of successful repetitions. All samples and exact parameters are in the JSON report.", "",
             "| Measurement | Target | Median | Unit | Better | Runs | CV % |",
             "|---|---|---:|---|---|---:|---:|"]
    for metric in data["metrics"]:
        lines.append(f"| {cell(metric['label'])} | {cell(metric['target'])} | {metric['value']:,.3f} | "
                     f"{metric['unit']} | {'higher' if metric['higher_is_better'] else 'lower'} | "
                     f"{metric['sample_count']} | {metric['coefficient_of_variation_percent']:.2f} |")
    if not data["metrics"]:
        lines.append("| No completed measurements | — | — | — | — | — | — |")
    lines += ["", "## Hardware and operating conditions", "",
              f"- OS/kernel: {cell(inv.get('os_release', 'unknown'))}; {inv.get('kernel', 'unknown')}",
              f"- Architecture: {inv.get('architecture', 'unknown')}",
              f"- Logical CPUs: {inv.get('logical_cpus')}; affinity CPUs: {inv.get('affinity_cpus')}",
              f"- Installed RAM: {inv.get('memory', {}).get('MemTotal', 0) / GIB:.2f} GiB",
              f"- Available memory within detected limits: {inv.get('effective_available_memory_bytes', 0) / GIB:.2f} GiB",
              f"- Virtualization: {cell(inv.get('virtualization', 'unknown'))}",
              "", "## Storage targets", "",
              "| Target | Directory | Device / model | Filesystem | Transport | USB link Mbps |",
              "|---|---|---|---|---|---:|"]
    for target in data["targets"]:
        disk = target.get("backing_device", {})
        link = target.get("usb_link") or {}
        lines.append(f"| {target['id']} | {cell(target['directory'])} | {cell(disk.get('path', 'unknown'))} "
                     f"{cell(disk.get('model', ''))} | {cell(target.get('mount', {}).get('fstype', 'unknown'))} | "
                     f"{cell(target.get('device', {}).get('transport', 'unknown'))} | {link.get('speed_mbps', 'unavailable')} |")
    lines += ["", "## CPU identification", "", "```json", json.dumps(inv.get("cpu", {}), indent=2), "```",
              "", "## Motherboard and RAM configuration", ""]
    for kind in ["baseboard", "memory"]:
        lines += [f"### {kind}", "", "```text", str(inv.get("dmi", {}).get(kind, "Unavailable; try --privileged-inventory.")), "```", ""]
    lines += ["## PCIe negotiated links", "", "| Address | Current speed | Current lanes | Supported speed | Supported lanes |",
              "|---|---|---:|---|---:|"]
    for pci in inv.get("pci_devices", []):
        lines.append("| " + " | ".join(cell(pci.get(key, "unknown")) for key in [
            "address", "current_link_speed", "current_link_width", "max_link_speed", "max_link_width"]) + " |")
    lines += ["", "## USB connection tree", "", "```text", str(inv.get("usb_tree", "Unavailable")), "```",
              "", "## Monitoring summary", "", "```json", json.dumps(data.get("monitoring_summary", {}), indent=2), "```",
              "", "## Tool versions", ""]
    lines += [f"- {tool}: {cell(version)}" for tool, version in data.get("tool_versions", {}).items()]
    lines += ["", "## Interpretation and public benchmarks", ""]
    lines += [f"- {guidance}" for guidance in data["comparison_guidance"]]
    lines += ["- STREAM measurements use the upstream source, with numerical validation. They are labelled STREAM-derived because full Run Rules compliance is not automatically certified.",
              "- fio uses direct I/O to avoid the OS page cache; drive/controller caches and filesystem behavior still affect results.",
              "- Transfer timing includes destination fsync. Verification and dataset creation are outside the timed interval. Source cache eviction is best effort; no system-wide cache dropping occurs.",
              "- Public results from OpenBenchmarking/Phoronix require the exact same test profile and version; these local metrics cannot be converted into Geekbench, SPEC, or PassMark scores.",
              "", "## Warnings and unavailable tests", ""]
    lines += [f"- {cell(warning)}" for warning in data["warnings"]] or ["- None."]
    lines += ["", "## Reference documentation", ""]
    lines += [f"- [{name}]({url})" for name, url in data["references"].items()]
    return "\n".join(lines) + "\n"


def comparison_signature(metric: dict):
    params = dict(metric["parameters"])
    # Physical identities vary between systems; retain workload parameters.
    for key in ["source_target", "destination_target", "port", "llc_bytes"]:
        params.pop(key, None)
    return metric["id"], metric["unit"], metric["tool_version"], json.dumps(params, sort_keys=True)


def compare(paths: list[str]) -> int:
    reports = []
    for path in paths:
        with Path(path).open() as stream:
            data = json.load(stream)
        if data.get("schema_version") != "1.0" or "metrics" not in data:
            raise ValueError(f"Unsupported Octobench report: {path}")
        reports.append(data)
    output = desktop()
    output.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S%z")
    dest = output / f"octobench_comparison_{stamp}_{uuid.uuid4().hex[:6]}.md"
    lines = ["# Octobench comparison", "", "Only identical workload signatures share a row. Different parameters or tool versions produce separate rows.",
             "Targets are named explicitly; storage devices are not silently paired by enumeration order.", "",
             "| Measurement / workload | Unit | " + " | ".join(cell(r["system_name"]) for r in reports) + " |",
             "|---|---|" + "---|" * len(reports)]
    groups = {}
    for index, report in enumerate(reports):
        for metric in report["metrics"]:
            signature = comparison_signature(metric)
            groups.setdefault(signature, {"metric": metric, "values": {}})["values"].setdefault(index, []).append(metric)
    for signature, group in sorted(groups.items()):
        metric = group["metric"]
        fingerprint = hashlib.sha256(repr(signature).encode()).hexdigest()[:8]
        values = []
        for index in range(len(reports)):
            matches = group["values"].get(index, [])
            values.append("; ".join(f"{cell(m['target'])}: {m['value']:,.3f} ({m['sample_count']} runs)" for m in matches) or "unavailable / different settings")
        lines.append(f"| {cell(metric['label'])} [{fingerprint}] | {metric['unit']} | " + " | ".join(values) + " |")
    lines += ["", "## Source runs", ""]
    for path, report in zip(paths, reports):
        lines += [f"- {cell(report['system_name'])}: {cell(path)}; {report['started_at']}; {report['status']}"]
    lines += ["", "## Workload signatures", ""]
    for signature, group in sorted(groups.items()):
        fingerprint = hashlib.sha256(repr(signature).encode()).hexdigest()[:8]
        lines += [f"### {fingerprint}: {cell(group['metric']['label'])}", "", "```json",
                  json.dumps({"tool_version": signature[2], "parameters": json.loads(signature[3])}, indent=2), "```", ""]
    dest.write_text("\n".join(lines) + "\n")
    print(f"Comparison saved on your Desktop: {dest}")
    return 0


def positive(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parser():
    result = argparse.ArgumentParser(prog="octobench", description="Octobench: repeatable Linux CPU, memory, storage, USB and folder-transfer measurements. Reports always go to your Desktop.")
    result.add_argument("--version", action="version", version=f"octobench {VERSION}")
    result.add_argument("--profile", choices=PROFILES, default="standard")
    result.add_argument("--storage", action="append", default=[], metavar="DIRECTORY", help="existing writable directory to benchmark (repeatable; default: home)")
    result.add_argument("--usb", action="append", default=[], metavar="DIRECTORY", help="existing mounted USB directory to benchmark (repeatable)")
    result.add_argument("--no-auto-usb", action="store_true", help="do not automatically test writable mounted USB storage")
    result.add_argument("--no-install", action="store_true", help="skip dependency installation")
    result.add_argument("--privileged-inventory", action="store_true", help="use sudo for motherboard/RAM inventory; benchmarks still run as the desktop user")
    result.add_argument("--inventory-only", action="store_true", help="collect hardware information without running workloads")
    for name in ["cpu", "memory", "storage", "transfer"]:
        result.add_argument(f"--skip-{name}", action="store_true")
    for name in ["duration", "repeats", "size-mib", "transfer-mib", "files"]:
        result.add_argument(f"--{name}", type=positive, help="override the selected profile")
    result.add_argument("--compare", nargs="+", metavar="JSON_REPORT", help="generate a Desktop comparison from two or more JSON reports")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if args.compare:
        if len(args.compare) < 2:
            parser().error("--compare needs at least two JSON reports")
        return compare(args.compare)
    if platform.system() != "Linux":
        parser().error("Octobench currently supports Linux only")
    config = dict(PROFILES[args.profile])
    for key in config:
        value = getattr(args, key)
        if value is not None:
            config[key] = value
    print(f"Octobench {VERSION} — {platform.node()} — {args.profile} profile", flush=True)
    print("Tests use temporary files only. Storage tests write data; stop other heavy workloads for repeatable results.", flush=True)
    old_umask = os.umask(0o077)
    try:
        return Runner(args, config).run()
    finally:
        os.umask(old_umask)


if __name__ == "__main__":
    def terminate(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        print(f"Octobench: {error}", file=sys.stderr)
        sys.exit(2)
