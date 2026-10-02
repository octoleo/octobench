<h2><img align="middle" src="https://raw.githubusercontent.com/odb/official-bash-logo/master/assets/Logos/Icons/PNG/64x64.png" alt="Bash" >
Octobench - One Command, One Benchmark File
</h2>

Written by Llewellyn van der Merwe (@llewellynvdm)

Octobench measures your Linux computer and saves one complete JSON result on your Desktop.
The whole program is one Bash script: [`src/octobench`](src/octobench).

Linted by [#ShellCheck](https://github.com/koalaman/shellcheck)

> Supports Ubuntu/Debian with Bash, curl, and apt.

---

## Run

Open a terminal and paste this one line:

```bash
bash <(curl -fsSL "https://raw.githubusercontent.com/octoleo/octobench/refs/heads/main/src/octobench")
```

Run it as your normal desktop user. Enter your sudo password when asked, then
let the benchmark finish. Missing benchmark dependencies are installed automatically.
Progress appears in the terminal, and the final message tells you where the result is.
Allow approximately 10–20 minutes, with additional time for each drive tested.

Plug in and mount any USB storage you want tested **before** starting. Octobench
finds writable mounted USB drives automatically and also tests your home filesystem.
Close heavy applications and ongoing file transfers; connect laptops to AC power.
Keep the same power profile when comparing computers.

## Your result

You get **one file** on your configured Desktop, or `~/Desktop` when no Desktop
location is configured. Its name includes the system hostname and date/time:

```text
octobench_Atom_2026-10-02_18-30-00+0200.json
```

JSON keeps the measurements and their context together for a future comparison
service or an AI. The file contains:

- System, CPU, RAM, motherboard, storage, USB, and PCIe information.
- Benchmark tool versions, workload settings, units, and repeat measurements.
- Key performance values and summary statistics.
- Test commands, raw output, operating conditions, and errors or skipped tests.
- Folder-transfer integrity checks and the status of the run.

Timed benchmark workloads run three times. Each metric includes its individual
samples, median, units, workload parameters, and tool version.

Upload the JSON file to your comparison tool when you are ready. Octobench
performs the benchmark and creates the file; results stay on your computer.

## What it measures

| Area | Measurement |
| --- | --- |
| CPU | Sysbench prime workload using one thread and all available threads; events/second. |
| Memory | Sysbench sequential reads and writes; MiB/second, with buffer size recorded. |
| Storage | fio sequential throughput, random IOPS, and latency on each tested filesystem. |
| Folder transfers | Timed copies and moves of generated large-file and small-file datasets, with integrity verification. |
| USB | Negotiated connection speed and measured throughput of attached mounted storage. |
| Motherboard and RAM | Board model, RAM configuration, and available PCIe link speed/width. |
| Operating conditions | Load, available memory, CPU frequencies, temperatures, and power information where exposed. |

A USB link rate is its signalling capacity; the drive's measured throughput also
depends on the enclosure, filesystem, and storage hardware. A same-filesystem move
measures a metadata rename; a move between filesystems measures copying and deletion.
Modern motherboards have several links, so there is no single motherboard bus-speed score.

## What stays on your computer

Octobench uses generated data in temporary test directories and removes it when
finished. Existing user files are preserved. The script runs directly from the
one-line command and is not installed as a permanent program.

Dependencies installed through apt remain available afterwards. Octobench does
not change power profiles, CPU governors, or other performance settings. It does
not upload results. Hardware details and filesystem paths are included in the
JSON, so review it before sharing publicly.

Press **Ctrl+C** to stop. Completed measurements are saved in the same single
JSON file with an interrupted status, and temporary test data is cleaned up.

## Comparing measurements later

Use the same Octobench version and workload settings on each computer. Compare
matching metrics and units, and check the recorded tool versions, repeats, power
conditions, and test status. Storage results belong to the particular filesystem
and device tested.

[Sysbench](https://github.com/akopytov/sysbench) and
[fio](https://fio.readthedocs.io/en/latest/fio_doc.html) provide established,
repeatable workloads. Their measurements describe these specific workloads;
they are not interchangeable with Geekbench, SPEC, or PassMark scores.
A world ranking requires public reference results from the same benchmark
version and workload. Octobench records the evidence needed for that assessment.

## License

GNU General Public License version 3; see [LICENSE](LICENSE).
