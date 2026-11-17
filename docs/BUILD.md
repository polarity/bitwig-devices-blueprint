# Build and preflight

Blueprint Gain has build targets for Windows x64 on Bitwig 6.1.3 and 6.1.7,
and for Linux x64 on 6.1.7. See [host evidence](BITWIG-TEST.md#current-evidence)
for validation by target. Use
Python 3.12 x64 (`python3.12 -m venv` or `uv`), a Rust 1.85+ toolchain with
Cargo (`edition = "2024"`), and a JDK with `javac` (17+) on PATH. The setup
downloads Python and Rust dependencies on first use. Once those are installed,
builds and preflight use local files and run with Bitwig closed.

## One-time setup

### Linux (bash)

From the repository root, adjust `BITWIG` to your Bitwig Studio 6.1.7
installation path (bundled JRE is under `lib/jre/bin/java` on Linux):

```bash
python3.12 -m venv .venv
PYTHON=./.venv/bin/python
"$PYTHON" -m pip install --disable-pip-version-check setuptools wheel
"$PYTHON" -m pip install --no-deps --no-build-isolation -e tools/nitro_ast
./scripts/setup-preflight.sh

BITWIG=/opt/bitwig-studio
"$PYTHON" scripts/build-local-host-tools.py \
  --profile-id linux-x86_64-bitwig-6.1.7 \
  --jar "$BITWIG/bin/bitwig.jar" \
  --libs "$BITWIG/bin/libs.jar" \
  --java "$BITWIG/lib/jre/bin/java" \
  --javac "$(command -v javac)"
```

### Windows (PowerShell)

From the repository root in PowerShell, set `$version` and `$bitwig` to your
matching Bitwig installation.

```powershell
uv venv --seed --python 3.12 .venv
if ($LASTEXITCODE -ne 0) { throw 'Could not create Python environment' }
$python = '.\.venv\Scripts\python.exe'
& $python -m pip install --no-deps -e tools/nitro_ast
if ($LASTEXITCODE -ne 0) { throw 'Nitro AST installation failed' }
.\scripts\setup-preflight.ps1
$version = '6.1.7'  # Use '6.1.3' with that exact installation.
$bitwig = 'C:\Program Files\Bitwig Studio'
& $python scripts/build-local-host-tools.py `
  --profile-id "windows-x86_64-bitwig-$version" `
  --jar (Join-Path $bitwig 'bin\bitwig.jar') `
  --libs (Join-Path $bitwig 'bin\libs.jar') `
  --java (Join-Path $bitwig 'jre\bin\java.exe') `
  --javac (Get-Command javac -CommandType Application).Source
if ($LASTEXITCODE -ne 0) { throw 'Compiler tool setup failed' }
```

If a JAR hash differs, verify that the paths point at the exact target build.
For another Bitwig version or platform, treat the document, toolchain and host
test as a separate target. No key or donor device is needed.

## Run the gate and build

### Linux (bash)

```bash
PYTHON=./.venv/bin/python
INPUTS=local/host-tools/linux-x86_64-bitwig-6.1.7/compiler-inputs.json
DEMO=devices/blueprint-gain
PROJECT="$DEMO/bitwig-project.linux-6.1.7.json"

"$PYTHON" scripts/device_preflight.py "$DEMO" --project-file "$PROJECT" --compiler-inputs "$INPUTS" || exit 1
"$PYTHON" "$DEMO/build.py" --project-file "$PROJECT" --compiler-inputs "$INPUTS" || exit 1
"$PYTHON" - <<'PY'
import hashlib, json
from pathlib import Path

report = json.loads(Path("build/preflight/polarity.blueprint-gain/linux-6.1.7/report.json").read_text())
artifact = Path("devices/blueprint-gain/build/linux-6.1.7/Blueprint Gain.bwdevice")
digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
if report["status"] != "pass" or digest != report["artifactSha256"]:
    raise SystemExit("Built file does not match the passing preflight")
print(f"Verified passing artifact: {digest}  {artifact}")
PY
```

### Windows (PowerShell)

```powershell
$python = '.\.venv\Scripts\python.exe'
$version = '6.1.7'  # Match the version used in setup.
$inputs = "local/host-tools/windows-x86_64-bitwig-$version/compiler-inputs.json"
$demo = 'devices/blueprint-gain'
$targetArgs = @('--compiler-inputs', $inputs)
$reportPath = 'build/preflight/polarity.blueprint-gain'
if ($version -eq '6.1.7') {
    $targetArgs += @('--project-file', "$demo/bitwig-project.windows-6.1.7.json")
    $reportPath += '/windows-6.1.7'
}
& $python scripts/device_preflight.py $demo @targetArgs
if ($LASTEXITCODE -ne 0) { throw 'Preflight failed' }
& $python "$demo/build.py" @targetArgs
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
$report = Get-Content "$reportPath/report.json" -Raw | ConvertFrom-Json
$artifact = "$demo/build/windows-$version/Blueprint Gain.bwdevice"
if ($report.status -ne 'pass' -or (Get-FileHash -Algorithm SHA256 -LiteralPath $artifact).Hash -ne $report.artifactSha256) {
    throw 'Built file does not match the passing preflight'
}
```

Start with the first failed check or rule ID in the JSON report. Profiles
check the exact compiler inputs, Nitro syntax, semantic analysis and LLVM IR
on Windows 6.1.3 and 6.1.7 (other platforms skip the semantic probe with
`NITRO026`), two clean builds, byte-stable document
roundtrips, graph and panel contracts, and embedded SkSL compilation in local
Skia.

The 6.1.7 compiler emits raw DEFLATE Nitro payloads. Analyze the unpacked AST,
and embed the compiler's original payload in the output. Match the build to
the target version when preparing a Patchbay folder.

`PASS (ready-for-controlled-host-test)` means the artifact is ready for the
[Bitwig test](BITWIG-TEST.md). Native JIT, browser insertion, real audio,
shader paint, mouse interaction and saved-project recall are live checks.

## Try a deliberate preflight failure

In a disposable checkout, make one change at a time and run the preflight
command above. Restore each edit before trying the next one.

| Edit | Expected result |
| --- | --- |
| In `gain.nitro`, replace `fmin(1.0, fmax(0.0, gain))` with `clamp(gain, 0.0, 1.0)`. | `NITRO020` blocks the source before compilation. This builtin is a known Nitro trap on the target. |
| In `demo_document.py`, change the drawing connection from `wire(dsp, 1)` to `wire(dsp, 0)`. | `ABI035` flags the stereo audio output wired to a drawing input that requires four lanes. The document can build and still fail this check. |
| In `bitwig-project.json`, change `defaultNormalized` from `1.0` to `0.5`. | `PANEL014` flags the mismatch with the serialized knob default. |

Each error ends with `FAIL (blocked-before-host-test)` and exit code 1.
Use the rule ID, source path or artifact finding to locate the mismatch.
Fix the source and rerun the gate before copying a file into Patchbay.
