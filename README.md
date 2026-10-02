# Octobench

**Measure and compare the performance of Linux computers using one command.**
Octobench tests CPU, memory bandwidth, storage throughput and latency, USB
connections, and real folder copies and moves. It installs missing dependencies
on Ubuntu/Debian and saves timestamped reports to your Desktop.

The executable is **`src/octobench`**, following the Octoleo shell-script layout.
Its Bash launcher uses a bundled Python standard-library backend for precise
timing, verification, JSON reporting, and comparisons. No pip installation is
needed. The complete repository is required; downloading just the launcher is
not sufficient.

## 1. Get Octobench

Open a terminal and run:

```bash
sudo apt update
sudo apt install -y git
git clone https://github.com/octoleo/octobench.git
cd octobench
```

## 2. Run your first benchmark

```bash
bash src/octobench --profile quick --privileged-inventory
```

Run this as your **normal desktop user**. Octobench asks for sudo only when it
needs to install packages or read privileged motherboard/RAM information.
If you run the command through sudo, the launcher returns to the original user
before benchmarking, so reports go to that user's Desktop.

The quick profile is a first check. For a more reliable comparison:

```bash
bash src/octobench --privileged-inventory
```

The default standard profile repeats each workload three times. It benchmarks
your home filesystem and automatically discovers writable, mounted USB storage.
Plug in and mount your external drives **before** starting.

There is no automatic mounting or formatting. Existing user files are not moved
or overwritten: all write/copy/move tests use freshly created `.octobench-*`
temporary directories, which are removed afterwards.

## 3. Test a particular storage or USB drive

Use an existing writable directory on the filesystem you want to measure:

```bash
# Internal/home filesystem plus the external drive from the folder-move example
bash src/octobench --storage "$HOME" \
  --usb "/media/llewellyn/PARK/Development" \
  --no-auto-usb --privileged-inventory
```

`--storage` replaces the default home storage target. `--usb` adds another target.
Both options can be repeated. Paths with spaces must be quoted.

```bash
# Test only one chosen storage directory; disable automatic USB discovery
bash src/octobench --storage "/media/llewellyn/PARK/Development" --no-auto-usb
```

Cross-filesystem transfer tests use the home filesystem as the source and the
selected drive as the destination. If both are on the same filesystem, those
tests are marked not applicable. To investigate the opposite transfer direction,
run on a system/user whose home source is on the desired source filesystem;
this release's automated cross-filesystem direction is home → target.

## 4. Find your reports

At completion, Octobench prints the exact paths. Reports always go to the user's
configured XDG Desktop, or `~/Desktop` if no separate Desktop is configured.
The directory is created when necessary, including on headless systems.

Names include the hostname, local date/time, timezone, and a short unique suffix:

```text
octobench_Atom_2026-10-02_14-30-00+0200_a1b2c3.md
octobench_Atom_2026-10-02_14-30-00+0200_a1b2c3.json
octobench_Atom_2026-10-02_14-30-00+0200_a1b2c3.csv
octobench_Atom_2026-10-02_14-30-00+0200_a1b2c3.tar.gz
octobench_Atom_2026-10-02_14-30-00+0200_a1b2c3_raw/
```

| Output | Purpose |
|---|---|
| Markdown `.md` | Readable comparison values, hardware, links, warnings, methodology |
| JSON `.json` | Complete structured measurements, repetitions, parameters, hardware and telemetry for AI/comparison |
| CSV `.csv` | Flat metric table for spreadsheets |
| Archive `.tar.gz` | All reports and raw command evidence in one shareable file |
| `_raw/` directory | Exact commands, output, errors, and fio JSON |

Reports have private user permissions. They contain the hostname, paths, hardware
models, and operating details. DMI serial-number, UUID, and asset-tag lines are
removed. Review the reports before sharing them publicly. Results are never
uploaded automatically.

## 5. Compare two or more computers

1. Use the same Octobench version and profile on each machine.
2. Copy the `.json` reports to one computer.
3. Generate a comparison:

```bash
bash src/octobench --compare "$HOME/Desktop/system-a.json" "$HOME/Desktop/system-b.json"
```

The filenames above represent the reports you have copied; use their actual
filenames. You can also compare all JSON reports in a dedicated folder:

```bash
bash src/octobench --compare "$HOME/Desktop/octobench-reports/"*.json
```

The comparison Markdown file is saved to your Desktop. It includes all systems,
metric units, medians, repetitions, target identities, and workload signatures.
Different tool versions or workload settings appear in different rows rather
than being presented as equivalent. Multiple disks are listed separately within
each system's cell; the program never assumes that `target1` on one computer is
the same device as `target1` on another.

The all-thread CPU test compares the same **all available threads** policy even
when systems have different core counts; the actual thread counts appear in
each cell. Single-thread tests and other workloads retain their exact settings.

### Ask an AI to interpret the reports

Attach the JSON reports or archives and use:

> Compare these Octobench reports. Create a table of CPU single/all-thread
> events/s, OpenSSL throughput, STREAM-derived Triad bandwidth, sequential disk
> read/write MiB/s, 4 KiB random read/write IOPS, QD1 mean/p99 latency, USB
> negotiated link rate, and verified large/small-file copy/move times. Keep
> storage devices separate. Compare only matching tool versions and workload
> parameters; flag incomplete runs, high variation, battery use, virtualization,
> possible throttling, and missing data. Explain the likely bottlenecks. Do not
> invent world rankings or translate these metrics into unrelated benchmark scores.

## What is measured?

| Area | Workload / observation | Key values |
|---|---|---|
| CPU | Sysbench prime workload, one thread and all affinity-available threads | Events/s |
| Crypto/CPU | OpenSSL SHA-256 and AES-256-GCM, single process, 16 KiB blocks | MiB/s |
| Memory | Pinned upstream STREAM source, Copy/Scale/Add/Triad, one and all threads | Decimal MB/s |
| Sequential storage | fio 1 MiB direct-I/O reads/writes, queue depth 16 | MiB/s, IOPS, mean/p99 completion latency |
| Random storage | fio 4 KiB reads at QD1/QD32, writes at QD32 | IOPS and completion latency in µs |
| Mixed storage | fio 4 KiB QD32, 70% reads / 30% writes | Read/write throughput, IOPS and latency |
| Folder copy | GNU cp with reflinks disabled; rsync with whole-file copying | Seconds and MiB/s |
| Folder move | Same-filesystem rename and, where possible, cross-filesystem copy/delete | Seconds; files/s for rename, MiB/s for cross-filesystem move |
| USB | Kernel negotiated device link plus the mounted drive's storage/transfer tests | Signalling Mbps and measured device throughput |
| Motherboard/PCIe | Motherboard model and supported/current PCIe link speed and width | GT/s and lanes where available |
| RAM configuration | DMI installed modules and configured rates | Capacity, MT/s where firmware provides it |
| Operating conditions | Load, available RAM, swap, CPU frequency, temperatures, throttle counters where exposed | Time-series samples every two seconds |

## Profiles and duration

| Profile | Seconds per timed CPU/fio workload | Repeats | fio file | Large transfer file | Small files |
|---|---:|---:|---:|---:|---:|
| `quick` | 5 | 1 | 256 MiB | 64 MiB | 200 × 16 KiB |
| `standard` (default) | 30 | 3 | 2 GiB | 512 MiB | 2,000 × 16 KiB |
| `full` | 60 | 3 | 8 GiB | 2 GiB | 10,000 × 16 KiB |

```bash
bash src/octobench --profile full --privileged-inventory
```

Expect roughly 15–30 minutes for standard on one fast drive, plus extra time for
each USB/storage target. Slow drives and small-file workloads can take longer.
CPU/crypto timing alone takes about six minutes in standard mode. Each drive's
six fio workloads take about ten minutes including warm-up, before transfers.
STREAM has its own iteration-based timing and does not use `--duration`.

Storage tests repeatedly overwrite **their temporary test file**. A 2 GiB file
does not mean only 2 GiB are written: total writes depend on speed and runtime.
Measured fio write bytes are recorded per target; initialization and warm-up
are additional writes. There is a one-GiB free-space reserve check. Tests do not
exercise an entire drive or certify its health or crash stability.

Override settings when needed:

```bash
bash src/octobench --profile quick --duration 10 --repeats 3 \
  --size-mib 1024 --transfer-mib 256 --files 1000 --no-auto-usb
```

## Useful commands

```bash
# Hardware report only; no workload writes
bash src/octobench --inventory-only --privileged-inventory

# Only CPU and memory benchmarks
bash src/octobench --skip-storage --skip-transfer

# Only storage and folder-transfer benchmarks
bash src/octobench --skip-cpu --skip-memory

# Folder copy/move tests without fio
bash src/octobench --skip-cpu --skip-memory --skip-storage

# Dependencies are already installed
bash src/octobench --no-install

# All options
bash src/octobench --help
```

Press **Ctrl+C** to stop. Octobench terminates its active workload, removes its
temporary directories, and saves an interrupted report with completed results.
SIGTERM is handled similarly. A forced SIGKILL or power loss cannot run cleanup;
remove only the `.octobench-*` directories belonging to that interrupted run.

## Make it easy to run again

Add this alias to `~/.bashrc` after cloning:

```bash
printf 'alias octobench=%q\n' "$(pwd)/src/octobench" >> "$HOME/.bashrc"
source "$HOME/.bashrc"
octobench --profile quick
```

Run the alias setup from the repository directory. It points at the checkout so
the backend and vendored STREAM source remain available. To update:

```bash
git pull --ff-only
```

## Fair comparisons and accepted benchmark methods

Use AC power, the same power profile, and the same benchmark/tool versions.
Close heavy applications and wait for file transfers or updates to finish.
Run on the actual hardware if the goal is hardware comparison; VM/container
results measure the resources available inside that environment. Keep the
current configuration if you want to compare the systems **as they stand**:
Octobench does not change governors, overclock, disable protections, or drop
system-wide caches.

There is no single universally accepted CPU/RAM/motherboard score. Octobench
uses established tools and explicit units:

- [Sysbench](https://github.com/akopytov/sysbench): a reproducible CPU microbenchmark,
  not a comprehensive CPU/application ranking.
- [STREAM](https://www.cs.virginia.edu/stream/ref.html): sustained memory-bandwidth
  methodology. Octobench preserves upstream code, checks numerical validation,
  records compiler flags and array sizing, and conservatively labels its results
  **STREAM-derived**. Full published Run Rules compliance is not certified.
  Quick mode or limited RAM may prevent arrays from exceeding the cache sizing
  requirement; that is explicitly reported.
- [fio](https://fio.readthedocs.io/en/latest/fio_doc.html): standard storage workload
  tooling. Direct I/O avoids the OS page cache; drive/controller caches still
  apply. Direct-I/O failures are recorded, never replaced with cached results.
- [OpenSSL speed](https://docs.openssl.org/master/man1/openssl-speed/): useful
  cryptographic throughput; hardware acceleration and OpenSSL versions affect it.

For public comparisons against other systems, use
[Phoronix Test Suite](https://github.com/phoronix-test-suite/phoronix-test-suite)
and [OpenBenchmarking.org](https://openbenchmarking.org/) with the **same exact
test profile/version** used by the reference results. Their documented command
`phoronix-test-suite benchmark RESULT_ID` can rerun a published result's tests.
Public benchmark integration is a separate workflow: Octobench does not install
Phoronix, upload results, or automatically query a ranking database. Local
Sysbench/fio/STREAM values cannot be converted into Geekbench, SPEC, or PassMark
scores, and a theoretical USB maximum is not a world ranking.

### Understanding storage and USB results

- Same-filesystem `mv` renames directory metadata. Its result is reported in
  seconds/files per second; it does not measure copied bytes or disk bandwidth.
- Cross-filesystem `mv` copies and deletes. It is limited by both source and
  destination, their shared buses, filesystem overhead, and small-file handling.
- Transfer datasets contain random data, hidden files and nested directories.
  SHA-256 verification and creation occur outside the timed interval; destination
  fsync occurs inside it. Reflinks are disabled for cp. Source file-cache eviction
  is best effort and recorded, so cached transfer results remain identifiable.
- USB `480 Mbps`, `5000 Mbps`, or `10000 Mbps` describes the negotiated signalling
  rate. Protocol overhead, hubs, controller sharing, the enclosure, and the disk
  reduce usable throughput. A slow storage result alone does not prove a slow USB bus.
- PCIe speed/width and RAM configured rate are configuration observations. Modern
  motherboards have several links, not one overall bus-speed number.
- Missing hardware details, unsupported I/O, failed verification, or skipped tests
  are reported as unavailable, never as zero performance.

## Dependencies and supported systems

Automatic installation: Ubuntu/Debian with Bash and apt. The backend needs
Python **3.10+** and Linux `/proc` and `/sys` interfaces. Other Linux distributions
can install equivalent packages manually and use `--no-install`.

```bash
sudo apt update
sudo apt install -y python3 sysbench fio gcc openssl pciutils usbutils \
  dmidecode lm-sensors rsync xdg-user-dirs util-linux coreutils
```

No `sensors-detect` probing is performed. Firmware/hardware may not expose every
sensor or link. The program distinguishes the Flexible I/O tester (`fio-...`)
from the unrelated Python Fiona program also named `fio`.

## Development and validation

```bash
sudo apt install -y shellcheck
bash -n src/octobench
shellcheck src/octobench
python3 -m unittest discover -s tests -v
bash tests/smoke.sh
```

The smoke test uses a temporary HOME/Desktop and small generated datasets. It
checks the real installed tools, JSON/CSV/archive generation, verified transfers,
comparison generation, interruption cleanup, and preservation of existing files.
GitHub Actions runs these checks on Ubuntu. Physical USB link/throughput and
privileged DMI availability still need a run on a real desktop with a USB drive.

## Licence

Octobench is GPL-3.0; see [LICENSE](LICENSE). Vendored STREAM retains its own
licence and publication conditions; see [vendor/stream/README.md](vendor/stream/README.md).
