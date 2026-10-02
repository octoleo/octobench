"""Deterministic test doubles. Never used by the distributed benchmark."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

name, args = sys.argv[1], sys.argv[2:]
log = os.environ.get("OCTO_TEST_TOOL_LOG")
if log:
    with open(log, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"tool": name, "args": args}) + "\n")

if name == "sysbench":
    if "--version" in args:
        print("sysbench 1.0.20")
    elif "cpu" in args:
        if os.environ.get("OCTO_TEST_BLOCK_CPU"):
            Path(os.environ["OCTO_TEST_BLOCK_CPU"]).touch()
            while True:
                time.sleep(1)
        value = 1234.56
        if os.environ.get("OCTO_TEST_CPU_SAMPLES"):
            samples = json.loads(os.environ["OCTO_TEST_CPU_SAMPLES"])
            calls = [json.loads(line) for line in Path(log).read_text().splitlines()]
            count = sum(call["tool"] == "sysbench" and "cpu" in call["args"] for call in calls)
            value = samples[(count - 1) % len(samples)]
        print(f"CPU speed:\n    events per second: {value}\nLatency (ms):\n         avg: 0.81")
    else:
        size = next((a.split("=", 1)[1] for a in args if a.startswith("--memory-block-size=")), "256M")
        block = int(size[:-1]) if size.endswith("M") else int(size)
        if block <= 0 or block & (block - 1):
            raise SystemExit(f"FATAL: Invalid value for memory-block-size: {size}")
        print("10240.00 MiB transferred (6789.12 MiB/sec)\nTotal operations: 10000 (10000.00 per second)")
elif name == "fio":
    if "--version" in args:
        print("fio-3.36")
    else:
        filename = next((a.split("=", 1)[1] for a in args if a.startswith("--filename=")), None)
        if filename:
            path = Path(filename)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(b"benchmark test data\n")
        error = int(os.environ.get("OCTO_TEST_FIO_ERROR", "0"))
        block = {"bw_bytes": 128 * 1024**2, "bw": 128 * 1024, "iops": 32000,
                 "io_bytes": 1024**2, "runtime": 10000, "total_ios": 320000,
                 "clat_ns": {"mean": 12000, "percentile": {"99.000000": 50000}},
                 "lat_ns": {"mean": 12000, "percentile": {"99.000000": 50000}}}
        if os.environ.get("OCTO_TEST_FIO_NO_P99"):
            block["clat_ns"].pop("percentile")
            block["lat_ns"].pop("percentile")
        result = json.dumps({"fio version": "fio-3.36", "jobs": [{"jobname": "octobench",
                            "error": error, "read": block, "write": block}]})
        output = next((a.split("=", 1)[1] for a in args if a.startswith("--output=")), None)
        if output:
            Path(output).write_text(result)
        else:
            print(result)
elif name == "openssl":
    if args and args[0] == "speed":
        print("+H:16:64:256:1024:8192:16384")
        algorithm = "aes-256-gcm" if "aes-256-gcm" in args else "sha256"
        print(f"+F:0:{algorithm}:10000000:20000000:30000000:40000000:50000000:60000000")
        print("+R:100000:sha256:10.000000", file=sys.stderr)
    else:
        raise SystemExit(subprocess.call(["/usr/bin/openssl", *args]))
elif name == "lsblk":
    usb = os.environ.get("OCTO_TEST_USB_DIR")
    if "--version" in args:
        print("lsblk from util-linux 2.39.3")
    elif any(a in ("--json", "-J") or "J" in a[1:] and a.startswith("-") for a in args):
        print(json.dumps({"blockdevices": ([{"name": "sdb", "kname": "sdb", "path": "/dev/sdb",
                     "type": "disk", "tran": "usb", "size": 1000000000000,
                     "model": "Fixture USB SSD", "rota": False, "ro": False, "rm": True, "mountpoints": [None],
                     "children": [{"name": "sdb1", "kname": "sdb1", "path": "/dev/sdb1",
                                   "type": "part", "tran": None, "fstype": "ext4", "ro": False, "size": 1000000000000,
                                   "mountpoint": usb, "mountpoints": [usb]}]}] if usb else [])}))
    else:
        print("NAME TYPE TRAN SIZE MODEL\nsdb disk usb 1T Fixture USB SSD" if usb else "NAME TYPE TRAN SIZE MODEL")
elif name == "findmnt":
    usb = os.environ.get("OCTO_TEST_USB_DIR")
    if usb and usb in args:
        if "--json" in args or "-J" in args:
            print(json.dumps({"filesystems": [{"target": usb, "source": "/dev/sdb1",
                                              "fstype": "ext4", "options": "rw"}]}))
        else:
            fields = "TARGET,SOURCE,FSTYPE,OPTIONS"
            for index, argument in enumerate(args):
                if argument in ("--output", "-o"):
                    fields = args[index + 1]
                elif argument.startswith("--output="):
                    fields = argument.split("=", 1)[1]
            values = {"TARGET": usb, "SOURCE": "/dev/sdb1", "FSTYPE": "ext4", "OPTIONS": "rw"}
            print(" ".join(values.get(field, "") for field in fields.split(",")))
    else:
        raise SystemExit(subprocess.call(["/usr/bin/findmnt", *args]))
elif name == "xdg-user-dir":
    print(os.environ.get("OCTO_TEST_DESKTOP", str(Path.home() / "Desktop")))
elif name == "hostname":
    print("octobench-fixture")
elif name == "uname":
    if args == ["-n"]:
        print("octobench-fixture")
    else:
        raise SystemExit(subprocess.call(["/usr/bin/uname", *args]))
elif name == "dmidecode":
    print("# dmidecode 3.5\nBase Board Information\n\tManufacturer: Fixture\n\tProduct Name: Test board\nMemory Device\n\tSize: 16384 MB\n\tSpeed: 3200 MT/s\n\tConfigured Memory Speed: 3200 MT/s")
elif name == "lspci":
    print("00:01.0 PCI bridge: Fixture\n\tLnkCap: Speed 8GT/s, Width x4\n\tLnkSta: Speed 8GT/s, Width x4")
elif name == "lsusb":
    print("/:  Bus 02.Port 1: Dev 1, Class=root_hub, Driver=xhci_hcd/4p, 10000M\n    |__ Port 1: Dev 2, Class=Mass Storage, Driver=uas, 5000M")
elif name == "sensors":
    print("coretemp-isa-0000\nPackage id 0: +42.0°C")
elif name == "sudo":
    if args and args[0] in ("-v", "-n"):
        option = args.pop(0)
        if option == "-v":
            raise SystemExit(0)
    if args and args[0] == "--":
        args.pop(0)
    raise SystemExit(subprocess.call(args))
elif name == "apt-get":
    if args and "install" in args:
        target = Path(os.environ["OCTO_TEST_BIN"])
        template = Path(os.environ["OCTO_TEST_TEMPLATES"])
        for path in template.iterdir():
            installed = target / path.name
            if installed.exists() and installed.resolve() == path.resolve():
                continue
            shutil.copy2(path, installed)
else:
    raise SystemExit(f"Unhandled mock tool: {name}")
