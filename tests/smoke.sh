#!/usr/bin/env bash
# Exercise real tools with small files; no user data or real HOME is touched.
set -Eeuo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf -- "$TEST_DIR"' EXIT
export HOME="$TEST_DIR/home"
mkdir -p "$HOME" "$TEST_DIR/storage with spaces"
printf 'existing data\n' > "$TEST_DIR/storage with spaces/do-not-touch.txt"

bash "$ROOT_DIR/src/octobench" --no-install --profile quick --duration 1 \
  --repeats 1 --size-mib 16 --transfer-mib 4 --files 16 \
  --storage "$TEST_DIR/storage with spaces" --no-auto-usb

python3 - "$TEST_DIR" <<'PY'
import csv, json, pathlib, sys, tarfile
root = pathlib.Path(sys.argv[1])
reports = list((root / 'home/Desktop').glob('octobench_*.json'))
assert len(reports) == 1, reports
data = json.loads(reports[0].read_text())
assert data['status'] in ('completed', 'completed-with-warnings'), data['warnings']
ids = {m['id'] for m in data['metrics']}
for metric in ['cpu.sysbench.single', 'cpu.sysbench.all', 'cpu.openssl.sha256',
               'cpu.openssl.aes-256-gcm', 'memory.stream.single.triad', 'memory.stream.all.triad',
               'storage.fio.seq_read.read.bandwidth', 'storage.fio.rand_read_qd1.read.latency_p99',
               'storage.fio.rand_write_qd32.write.iops', 'transfer.cp.same.large.rate',
               'transfer.rsync.same.small.rate', 'transfer.mv.same.small.rate']:
    assert metric in ids, (metric, data['warnings'])
transfers = [t['transfer'] for t in data['tests'] if 'transfer' in t]
assert len(transfers) == 6 and all(t['sha256_verified'] for t in transfers)
assert (root / 'storage with spaces/do-not-touch.txt').read_text() == 'existing data\n'
assert not list(root.rglob('.octobench-*'))
assert list(csv.DictReader(reports[0].with_suffix('.csv').open()))
assert 'Key comparison values' in reports[0].with_suffix('.md').read_text()
with tarfile.open(reports[0].with_suffix('.tar.gz')) as archive:
    assert any(name.endswith('.json') for name in archive.getnames())
second = dict(data, system_name='comparison-peer')
(root / 'peer.json').write_text(json.dumps(second))
print('Smoke report and transfer checks passed.')
PY

bash "$ROOT_DIR/src/octobench" --compare "$HOME/Desktop/"*.json "$TEST_DIR/peer.json"
test -n "$(find "$HOME/Desktop" -maxdepth 1 -name 'octobench_comparison_*.md' -print -quit)"

# A distinct tmpfs mount exercises copy/delete move behavior without needing
# removable hardware. These are functional checks, not disk-performance claims.
CROSS_DIR=""
if [[ -d /dev/shm && -w /dev/shm ]] && python3 - <<'PY'
import os, shutil
raise SystemExit(0 if os.stat(os.environ['HOME']).st_dev != os.stat('/dev/shm').st_dev and shutil.disk_usage('/dev/shm').free > 2 * 1024**3 else 1)
PY
then
    CROSS_DIR="$(mktemp -d /dev/shm/octobench-test.XXXXXX)"
    trap 'rm -rf -- "$TEST_DIR"; [[ -z "$CROSS_DIR" ]] || rm -rf -- "$CROSS_DIR"' EXIT
    bash "$ROOT_DIR/src/octobench" --no-install --profile quick --duration 1 \
      --repeats 1 --transfer-mib 4 --files 16 --size-mib 16 --skip-cpu --skip-memory \
      --skip-storage --storage "$CROSS_DIR" --no-auto-usb
    python3 - "$HOME/Desktop" <<'PY'
import json, pathlib, sys
reports = [json.loads(p.read_text()) for p in pathlib.Path(sys.argv[1]).glob('octobench_*.json')]
data = next(d for d in reports if any(m['id'] == 'transfer.mv.cross.large.rate' for m in d['metrics']))
assert data['targets'][0]['filesystem_class'] == 'memory-backed'
cross = [t['transfer'] for t in data['tests'] if '-cross-' in t['label'] and 'transfer' in t]
assert len(cross) == 6 and all(t['sha256_verified'] for t in cross)
assert not list(pathlib.Path(data['targets'][0]['directory']).glob('.octobench-*'))
print('Cross-filesystem verified transfer checks passed.')
PY
    rm -rf -- "$CROSS_DIR"
    CROSS_DIR=""
fi

# SIGTERM must terminate an active benchmark, clean datasets, and save a report.
export HOME="$TEST_DIR/interrupted-home"
mkdir -p "$HOME"
bash "$ROOT_DIR/src/octobench" --no-install --profile quick --duration 30 \
  --skip-memory --skip-storage --skip-transfer --no-auto-usb > "$TEST_DIR/interrupted.log" 2>&1 &
BENCH_PID=$!
for (( attempt=0; attempt<100; attempt++ )); do
    if grep -q 'cpu-single-repeat' "$TEST_DIR/interrupted.log"; then
        break
    fi
    sleep 0.1
done
kill -TERM "$BENCH_PID"
set +e
wait "$BENCH_PID"
status=$?
set -e
test "$status" -eq 130
python3 - "$HOME/Desktop" <<'PY'
import json, pathlib, sys
reports = list(pathlib.Path(sys.argv[1]).glob('octobench_*.json'))
assert len(reports) == 1
data = json.loads(reports[0].read_text())
assert data['status'] == 'interrupted'
assert any(test['status'] == 'interrupted' for test in data['tests'])
print('Interruption report check passed.')
PY
printf 'All smoke tests passed.\n'
