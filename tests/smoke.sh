#!/usr/bin/env bash
# Real-tool integration check; isolated HOME, short workloads, generated data only.
set -Eeuo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf -- "$TEST_DIR"' EXIT
export HOME="$TEST_DIR/home with spaces"
mkdir -p "$HOME" "$TEST_DIR/bin" "$TEST_DIR/unrelated working directory"
printf 'existing user data\n' > "$HOME/do-not-touch.txt"

# Keep the user-facing program fixed and option-free. Test-only wrappers shorten
# external-tool workloads; these reports are functional fixtures, not scores.
python3 - "$TEST_DIR/bin" <<'PY'
from pathlib import Path
import shutil
import sys
binary = Path(sys.argv[1])
for name in ("sysbench", "fio", "openssl"):
    real = shutil.which(name)
    if not real:
        raise SystemExit(f"Missing smoke-test dependency: {name}")
    wrapper = binary / name
    wrapper.write_text("#!/usr/bin/python3\n" +
        "import os, sys\n" +
        f"real = {real!r}\n" +
        "args = sys.argv[1:]\n" +
        "for index, argument in enumerate(args):\n" +
        "    if argument.startswith('--time=') or argument.startswith('--runtime='):\n" +
        "        args[index] = argument.split('=')[0] + '=1'\n" +
        "    elif argument.startswith('--size='):\n" +
        "        args[index] = '--size=16M'\n" +
        "    elif argument == '-seconds' and index + 1 < len(args):\n" +
        "        args[index + 1] = '1'\n" +
        "os.execv(real, [real, *args])\n")
    wrapper.chmod(0o755)
PY
export PATH="$TEST_DIR/bin:$PATH"
cd "$TEST_DIR/unrelated working directory"
bash <(cat "$ROOT_DIR/src/octobench")

python3 - "$TEST_DIR" <<'PY'
import json
from pathlib import Path
import sys
root = Path(sys.argv[1])
home = root / 'home with spaces'
reports = list((home / 'Desktop').iterdir())
assert len(reports) == 1 and reports[0].suffix == '.json', reports
report = json.loads(reports[0].read_text())
assert str(report['schema_version']).startswith('2'), report['schema_version']
assert report['status'].startswith('completed'), report['status']
measurements = report['measurements']
ids = [measurement['id'] for measurement in measurements]
for category in ('cpu', 'memory', 'storage', 'transfer'):
    assert any(category in metric for metric in ids), (category, ids, report['warnings'])
for metric in measurements:
    assert isinstance(metric['value'], (int, float)) and metric['value'] > 0, metric
    assert metric['aggregation'] == 'median', metric
assert any('fio' in str(metric['tool']) for metric in measurements), report['warnings']
assert any('sysbench' in str(metric['tool']) for metric in measurements), report['warnings']
assert any('openssl' in str(metric['tool']) for metric in measurements), report['warnings']
assert (home / 'do-not-touch.txt').read_text() == 'existing user data\n'
assert not list(home.glob('.octobench*'))
assert not list((root / 'unrelated working directory').iterdir())
print('Real-tool standalone smoke test passed; exactly one JSON report, user data preserved.')
PY
