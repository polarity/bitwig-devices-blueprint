#!/usr/bin/env python3
"""Offline preflight for a source-built document workspace.

This is the gate for modulators, Grid modules and devices whose shape the
hybrid profiles do not describe. Non-modulators opt in with the
`source-document` profile.

Builds the workspace twice, checks determinism and the document roundtrip,
runs the host-regression document lints and compiles every embedded shader.
It needs the repository document codec and the workspace builder. With
`--compiler-inputs` it also compiles the readable Nitro sources with the
exact-version compiler, requires them to equal the built ASTs when the
workspace keeps separate AST builders, and runs Bitwig's semantic analysis. It never launches Bitwig; loader, JIT, GUI and
audio behavior remain host evidence.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
from graphlib import CycleError, TopologicalSorter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import document_lints
import project_contracts


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SOURCE_DOCUMENT_PROFILE = "source-document"
DOCUMENT_TYPES = {
    "device": "application/bitwig-device",
    "modulator": document_lints.MODULATOR_TYPE,
    "grid-module": "application/bitwig-module",
}


def uses_this_gate(project: dict) -> bool:
    """Whether a project manifest selects this gate instead of device_preflight."""
    return (project.get("surface") == "modulator"
            or (project.get("preflight") or {}).get("profile") == SOURCE_DOCUMENT_PROFILE)


@dataclass
class Check:
    name: str
    status: str
    detail: str


def codec() -> Path:
    name = "bwmodule-parser.exe" if os.name == "nt" else "bwmodule-parser"
    path = ROOT / "tools/document_codec/target/release" / name
    if not path.is_file():
        raise FileNotFoundError(f"Build tools/document_codec locally first: {path}")
    return path


def run(command: list[str]) -> str:
    done = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if done.returncode != 0:
        raise RuntimeError(f"{Path(command[0]).name} failed: {(done.stderr or done.stdout).strip()[:400]}")
    return done.stdout


def build(project: dict, workspace: Path, output: Path, compiler_inputs: Path | None = None,
          *, project_file: Path | None = None) -> None:
    adapter = project.get("adapter", {})
    if adapter.get("type") != "external-builder":
        raise ValueError("preflight requires an external-builder adapter")
    replacements = {"{workspace}": str(workspace), "{output}": str(output)}
    command = [value for value in adapter["argv"]]
    for key, replacement in replacements.items():
        command = [part.replace(key, replacement) for part in command]
    if command[0] == "python":
        command[0] = sys.executable
    # Device builders compile their Nitro text themselves, as in device_preflight.
    if compiler_inputs and project.get("surface") != "modulator":
        command += ["--compiler-inputs", str(compiler_inputs)]
    if project_file and project_file.name != project_contracts.MANIFEST:
        command += ["--project-file", str(project_file)]
    run(command)


def shader_findings(document: dict, project: dict, workspace: Path) -> tuple[list[document_lints.Lint], int]:
    objects = list(document_lints.owned_objects(document["body"]))
    shaders = [item["fields"]["#16316"]["value"] for item in objects
               if item.get("class_id") == document_lints.DRAWING_CLASS and "#16316" in item.get("fields", {})]
    findings = []
    registered = project.get("sources", {}).get("sksl", [])
    if bool(shaders) != bool(registered):
        findings.append(document_lints.Lint(
            "error", "SKSL005",
            f"{len(shaders)} embedded shader(s) but sources.sksl lists {len(registered)}; "
            "register every original shader exactly once."))
    for relative in registered:
        source = workspace / relative
        if not source.is_file():
            findings.append(document_lints.Lint("error", "SKSL005", f"Registered shader is missing: {relative}"))
        elif not any(shader.endswith(source.read_text(encoding="utf-8")) for shader in shaders):
            findings.append(document_lints.Lint(
                "error", "SKSL005", f"Registered shader {relative} is not embedded in the built document."))
    for index, shader in enumerate(shaders):
        clean = re.sub(r"//[^\n]*|/\*.*?\*/", "", shader, flags=re.S)
        if not re.search(r"\b(?:half4|float4)\s+main\s*\(\s*float2\b", clean):
            findings.append(document_lints.Lint("error", "SKSL001", f"Embedded shader {index} has no main(float2)."))
        if re.search(r"\btanh\s*\(", clean):
            findings.append(document_lints.Lint("error", "SKSL002", f"Embedded shader {index} uses tanh."))
        try:
            import skia
        except ImportError:
            findings.append(document_lints.Lint("warning", "SKSL004", "skia-python is unavailable; shader not compiled."))
            continue
        declared = "" if re.search(r"\buniform\s+\w+\s+iResolution\b", clean) else "uniform float3 iResolution;\n"
        effect = skia.RuntimeEffect.MakeForShader(declared + shader)
        if effect is None:
            findings.append(document_lints.Lint("error", "SKSL004", f"Embedded shader {index} does not compile."))
    return findings, len(shaders)


def nitro_findings(project: dict, workspace: Path, compiler_inputs: Path | None,
                   scratch: Path) -> tuple[list[Check], list[document_lints.Lint]]:
    """Advisory source lint, plus AST parity and semantic analysis when the
    exact-version compiler inputs are supplied."""
    import device_preflight

    checks: list[Check] = []
    findings: list[document_lints.Lint] = []
    relatives = project.get("sources", {}).get("nitro", [])
    paths = [workspace / relative for relative in relatives]
    grid_module = project["surface"] == "grid-module"
    lint_config = project.get("preflight", {}) if grid_module else {}
    advisory = []
    for path in paths:
        config = lint_config
        if recall := lint_config.get("recallSafetyBySource", {}).get(path.relative_to(workspace).as_posix()):
            config = lint_config | {"recallSafety": lint_config.get("recallSafety", {}) | recall}
        advisory.extend(device_preflight.lint_nitro_source(path, config))
    # The source rules were written for audio devices; a host-accepted
    # modulator may differ, so they inform without blocking here.
    findings.extend(document_lints.Lint(item.severity if grid_module else "warning", item.rule,
                                       f"{Path(item.path).name}: {item.message}")
                    for item in advisory)
    source_status = "fail" if any(item.severity == "error" for item in findings) else "warning" if advisory else "pass"
    checks.append(Check("Nitro source lint" if grid_module else "Nitro source lint (advisory)", source_status,
                        f"{len(paths)} source file(s), {len(advisory)} finding(s)"))
    if compiler_inputs is None:
        checks.append(Check("Readable source equals built AST", "skip", "no --compiler-inputs"))
        checks.append(Check("Exact-version Nitro semantic analysis", "skip", "no --compiler-inputs"))
        return checks, findings

    import importlib.util
    import compiler_profiles
    from bitwig_nitro import serialize_nitrobin

    config, profile = compiler_profiles.validate_inputs(compiler_inputs)
    compiler_profiles.require_target(profile, project.get("target", {}))
    builders: dict = {}
    module_path = workspace / "nitro_ast.py"
    if module_path.is_file():
        specification = importlib.util.spec_from_file_location("modulator_preflight_ast", module_path)
        module = importlib.util.module_from_spec(specification)
        sys.modules[specification.name] = module
        specification.loader.exec_module(module)
        builders = getattr(module, "SOURCES", {})
    parity: list[document_lints.Lint] = []
    if not builders and project.get("surface") != "modulator":
        if not grid_module:
            checks.append(Check("Readable source equals built AST", "skip",
                                "no separate AST builders; the builder compiles the readable sources"))
        relatives = paths = []
    for relative, path in zip(relatives, paths, strict=True):
        if relative not in builders:
            parity.append(document_lints.Lint(
                "error", "NITRO030", f"{relative} has no AST builder entry in nitro_ast.SOURCES."))
            continue
        compiled = scratch / (Path(relative).stem + ".nitrobin")
        compiler_profiles.compile_source(path, compiled, compiler_inputs)
        if compiled.read_bytes() != serialize_nitrobin(builders[relative]()):
            parity.append(document_lints.Lint(
                "error", "NITRO030",
                f"{relative} compiles to a different AST than its builder; the readable source and the "
                "built component have diverged."))
    findings.extend(parity)
    if paths:
        checks.append(Check("Readable source equals built AST", "fail" if parity else "pass",
                            f"{len(paths)} source file(s) compiled with {config['profileId']}"))
    paths = [workspace / relative for relative in project.get("sources", {}).get("nitro", [])]
    verify_llvm = config["profileId"] in ("windows-x86_64-bitwig-6.1.3", "windows-x86_64-bitwig-6.1.7")
    dsp_test = lint_config.get("dspTest")
    semantic, semantic_findings = device_preflight.semantic_check(
        paths, config, scratch, verify_llvm=verify_llvm, llvm_output=scratch if dsp_test is not None else None,
    )
    checks.append(Check(semantic.name, semantic.status, semantic.detail))
    findings.extend(document_lints.Lint(item.severity, item.rule, item.message) for item in semantic_findings)
    if grid_module and semantic.status != "pass":
        findings.append(document_lints.Lint("error", "GRID001", "Grid modules require a passing exact-version semantic check."))
    if dsp_test is not None and semantic.status == "pass":
        dsp_check = grid_dsp_check(dsp_test, scratch)
        checks.append(dsp_check)
        if dsp_check.status == "fail":
            findings.append(document_lints.Lint("error", "GRID003", dsp_check.detail))
    return checks, findings


def grid_dsp_check(test_name: str, llvm_directory: Path) -> Check:
    name = "Grid DSP regression tests"
    tests = ROOT / "tests"
    if not isinstance(test_name, str) or not re.fullmatch(r"test_[A-Za-z0-9_]+\.py", test_name):
        return Check(name, "fail", "preflight.dspTest must name a test_*.py file in tests/.")
    if not (tests / test_name).is_file() or not (llvm_directory / "0.ll").is_file():
        return Check(name, "fail", f"Missing {test_name} or verified Nitro LLVM output.")
    env = os.environ | {"BITWIG_PREFLIGHT_LLVM_DIR": str(llvm_directory.resolve())}
    try:
        result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(tests), "-p", test_name],
                                cwd=ROOT, env=env, capture_output=True, encoding="utf-8",
                                errors="replace", timeout=90)
    except (OSError, subprocess.TimeoutExpired) as error:
        return Check(name, "fail", f"{test_name} could not run: {error}")
    report = result.stderr + result.stdout
    count = re.search(r"(?m)^Ran ([1-9]\d*) tests? in ", report)
    if result.returncode != 0 or not count or not re.search(r"(?m)^OK\s*$", report):
        return Check(name, "fail", f"{test_name} must pass without skipped tests.\n{report.strip()[-1800:]}")
    return Check(name, "pass", f"{count[1]} test(s) executed against the exact compiler's LLVM output")


def grid_module_findings(document: dict, project: dict, workspace: Path,
                         compiler_inputs: Path, scratch: Path) -> list[document_lints.Lint]:
    """Check Grid module topology and each embedded core's compiled ABI."""
    import compiler_profiles as compiler
    from devices.common.hybrid_controls import compiled_ports

    value, field = document_lints._value, document_lints._field
    root = document["body"]
    identity = project["identity"]
    objects = list(document_lints.owned_objects(root))
    ids = {id(item): index for index, item in enumerate(objects, 1)}
    findings = []

    def resolve(entry):
        return document_lints._resolve(entry, objects)

    def require(condition, message):
        if not condition:
            findings.append(document_lints.Lint("error", "GRID002", message))

    require(root.get("class_id") == 1904
            and value(root, "#6369") == identity["uuid"]
            and value(root, "#6370") == project["name"]
            and value(root, "#6375") == identity["category"],
            "The generic module root must match the declared UUID, name and category.")
    meta = document["metadata"]["fields"]
    require(meta.get("device_id", {}).get("value") == f"module:{identity['uuid']}"
            and meta.get("device_name", {}).get("value") == project["name"]
            and meta.get("application_version_name", {}).get("value") == project["target"]["bitwigVersion"],
            "Grid metadata must match the module identity and compiler target.")
    cores = [item for item in objects if item.get("class_id") == 1721]
    _, profile = compiler.validate_inputs(compiler_inputs)
    payloads = {}
    for index, relative in enumerate(project["sources"]["nitro"]):
        built_ast = scratch / f"grid-source-{index}.nitrobin"
        compiler.compile_source(workspace / relative, built_ast, compiler_inputs)
        payload = built_ast.read_bytes()
        try:
            _, signature = compiled_ports(compiler.unpack_ast(payload, profile), grid_abi=True)
        except ValueError as error:
            require(False, str(error))
            return findings
        payloads[payload] = signature

    signatures = {}
    used_payloads = set()
    for core in cores:
        storage = value(core, "#10421", [])
        payload = bytes(storage[1:]) if storage and storage[0] == 0 else b""
        require(payload in payloads and value(core, "#14616") is True
                and value(core, "#11360") is False, "Embedded Nitro must equal the exact compiler output.")
        if payload not in payloads:
            return findings
        used_payloads.add(payload)
        signature = signatures[ids[id(core)]] = payloads[payload]
        for direction, key in ((True, "#15756"), (False, "#15757")):
            ports = [resolve(entry) for entry in value(core, key, [])]
            require(len(ports) == len(signature[direction]) and all(
                port and port.get("class_id") == 2428
                and all(value(port, name) == expected for name, expected in spec.items())
                for port, spec in zip(ports, signature[direction])),
                "Persisted Grid Nitro port descriptors differ from the source signature.")
    require(bool(cores) and used_payloads == payloads.keys(), "Every declared Grid source must be embedded.")

    scheduled = [resolve(entry) for entry in value(root, "#173", [])]
    require(all(scheduled) and len(scheduled) == len({id(item) for item in scheduled})
            and set(signatures).issubset(ids[id(item)] for item in scheduled if item),
            "Each Grid signal component must be scheduled once at the root.")
    inputs = [item for item in scheduled if item and item.get("class_id") in {2002, 289, 87}]
    outputs = [item for item in scheduled if item and item.get("class_id") == 2000]
    drawings = [item for item in scheduled if item and item.get("class_id") == 5512]
    require(len(outputs) == 1 and len(scheduled) == len(inputs) + len(outputs) + len(drawings) + len(cores),
            "Expected one Grid audio output and the declared core, input, control and drawing atoms.")

    def shape(spec):
        return spec["kind#8561"], spec["channel_count#8559"]

    core_outputs = {(core_id, index): shape(spec)
                    for core_id, signature in signatures.items() for index, spec in enumerate(signature[False])}
    wiring = Counter()
    independent_outputs = Counter()
    dependencies = TopologicalSorter()
    input_ids = {ids[id(item)] for item in inputs}
    jack_types = {(1, 8): 2002, (1, 1): 289, (3, 0): 87}
    for core in cores:
        core_id = ids[id(core)]
        connections = document_lints._connections(core, objects)
        specs = signatures[core_id][True]
        require(len(connections) == len(specs), "Core input count must match the compiled signature.")
        for entry, spec in zip(connections, specs):
            source_ref = field(entry, "#248")
            source = resolve(source_ref)
            port = value(entry, "#249")
            source_id = ids.get(id(source))
            expected = shape(spec)
            if source_id in signatures:
                key = (source_id, port)
                require(expected in {(1, 1), (1, 4)} and expected == core_outputs.get(key),
                        "Between Grid cores, connect matching scalar or four-float signal ports.")
                independent_outputs[key] += 1
                dependencies.add(core_id, source_id)
            elif source is None:
                require((source_ref or {}).get("type") == "NullObject" and expected == (1, 1)
                        and type(value(entry, "#6443")) in (float, int),
                        "Only a scalar audio input may use a constant instead of a source.")
            else:
                require(source_id in input_ids and port == 0 and source["class_id"] == jack_types.get(expected),
                        "Core input wiring must match the declared signal lanes and control types.")
                wiring[source_id] += 1
    try:
        list(dependencies.static_order())
    except CycleError:
        require(False, "Grid cores must not form a signal cycle.")
    require(wiring == Counter(input_ids), "Each Grid jack and control must feed one core input.")
    if len(outputs) == 1:
        links = document_lints._connections(outputs[0], objects)
        require(len(links) == 1, "The Grid output must have one source.")
        for link in links:
            key = (ids.get(id(resolve(field(link, "#248")))), value(link, "#249"))
            require(key[1] == 0 and core_outputs.get(key) == (1, 8),
                    "The Grid output must read a core's first output with four stereo frames.")
            independent_outputs[key] += 1

    display_ports = Counter()
    for drawing in drawings:
        links = document_lints._connections(drawing, objects)
        sources = [ids.get(id(resolve(field(link, "#248")))) for link in links]
        connected = sum(source in signatures for source in sources)
        require(len(links) == 9 and 1 <= connected <= 3
                and len(set(sources[:connected])) == 1 and all(source is None for source in sources[connected:]),
                "Each Grid drawing must connect up to three leading inputs to one core and leave the rest free.")
        ports = [value(link, "#249") for link in links[:connected]]
        require(all(type(port) is int for port in ports) and all(left < right for left, right in zip(ports, ports[1:])),
                "Grid drawing inputs must use core outputs in increasing order.")
        keys = list(zip(sources[:connected], ports))
        require(all(core_outputs.get(key) == (1, 4) for key in keys), "Grid drawings need four-float outputs.")
        display_ports.update(keys)
    require(all(count == 1 for count in display_ports.values())
            and not (display_ports.keys() & independent_outputs.keys())
            and core_outputs.keys() == (display_ports.keys() | independent_outputs.keys()),
            "Each Grid output must be used. A drawing output must feed one drawing exclusively.")
    terminal_classes = {2000: 2001, 2002: 2001, 289: 1687, 87: 1702, 5512: 5517}
    expected = Counter((ids[id(item)], terminal_classes[item["class_id"]]) for item in inputs + outputs + drawings)
    art = Counter((value(item, "#6220"), item["class_id"]) for item in objects
                  if item.get("class_id") in terminal_classes.values())
    require(art == expected, "Each Grid jack, control and drawing must have one matching panel item.")
    return findings


def preflight(workspace: Path, project_file: Path | None = None, *,
              compiler_inputs: Path | None = None) -> dict:
    workspace = workspace.resolve()
    project_file = (project_file or workspace / "bitwig-project.json").resolve()
    if project_file.parent != workspace:
        raise ValueError("project file must be in the workspace")
    project = project_contracts.load(project_file)  # resolves platform overlays
    if not uses_this_gate(project):
        raise ValueError("workspace does not select the source-document gate")
    modulator = project["surface"] == "modulator"
    if not modulator and compiler_inputs is None:
        raise ValueError("a non-modulator source document requires --compiler-inputs")
    suffix = Path(project["output"]["path"]).suffix
    parser = codec()
    checks: list[Check] = []
    findings: list[document_lints.Lint] = []
    output_root = ROOT / "build/preflight" / re.sub(r"[^A-Za-z0-9_.-]+", "-", project["projectId"])
    with tempfile.TemporaryDirectory(prefix="modulator-preflight-", dir=_ensure(output_root)) as temporary:
        nitro_checks, nitro_lints = nitro_findings(project, workspace, compiler_inputs, Path(temporary))
        checks.extend(nitro_checks)
        findings.extend(nitro_lints)
        first = Path(temporary) / "a" / f"candidate{suffix}"
        second = Path(temporary) / "b" / f"candidate{suffix}"
        for target in (first, second):
            target.parent.mkdir(parents=True)
            build(project, workspace, target, compiler_inputs, project_file=project_file)
        digest = hashlib.sha256(first.read_bytes()).hexdigest()
        if first.read_bytes() != second.read_bytes():
            findings.append(document_lints.Lint("error", "BUILD001", "Two clean builds produced different bytes."))
            checks.append(Check("Deterministic build", "fail", "run a and run b differ"))
        else:
            checks.append(Check("Deterministic build", "pass", f"SHA-256 {digest}"))
        run([str(parser), "roundtrip", str(first)])
        document = json.loads(run([str(parser), "to-json", str(first)]))
        json_path = Path(temporary) / "document.json"
        rebuilt = Path(temporary) / f"rebuilt{suffix}"
        json_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        run([str(parser), "from-json", str(json_path), str(rebuilt)])
        stable = rebuilt.read_bytes() == first.read_bytes()
        if not stable:
            findings.append(document_lints.Lint("error", "DOC001", "Document to-json/from-json no-op changed bytes."))
        checks.append(Check("Document roundtrip", "pass" if stable else "fail", "codec roundtrip and JSON no-op"))
        if project["surface"] == "grid-module":
            grid = grid_module_findings(document, project, workspace, compiler_inputs, Path(temporary))
            findings.extend(grid)
            checks.append(Check("Grid module ABI", "fail" if grid else "pass",
                                "module identity, exact embedded AST, port signature, routing and panel bindings"))
        if relative_help := project.get("sources", {}).get("help"):
            source = workspace / relative_help
            strings = project_contracts.help_strings(source)
            sidecars = [project_contracts.help_resource_path(artifact) for artifact in (first, second)]
            help_items = [(document_lints._value(item, "#10543"), document_lints._value(item, "#6867"))
                          for item in document_lints.owned_objects(document["body"])
                          if document_lints._value(item, "#10543")]
            valid = bool(help_items) and all(path.is_file() and path.read_bytes() == source.read_bytes()
                                             for path in sidecars)
            valid = valid and all(strings.get(f"{key}.title") and strings.get(f"{key}.desc") == tooltip
                                  for key, tooltip in help_items)
            detail = (f"{len(help_items)} help IDs and their tooltips match both sidecars" if valid else
                      "Help sidecars must match the source and supply a title and matching tooltip for each help ID.")
            checks.append(Check("Module live help", "pass" if valid else "fail", detail))
            if not valid:
                findings.append(document_lints.Lint("error", "GRID004", detail))

    identity = project.get("identity", {})
    metadata = document["metadata"]["fields"]
    if metadata.get("device_uuid", {}).get("value") != identity.get("uuid"):
        findings.append(document_lints.Lint("error", "DOC002", "Document UUID differs from the project identity."))
    if metadata.get("type", {}).get("value") != DOCUMENT_TYPES[project["surface"]]:
        findings.append(document_lints.Lint(
            "error", "DOC002", f"Document metadata type does not match the {project['surface']} surface."))
    identity_errors = [item for item in findings if item.rule == "DOC002"]
    checks.append(Check("Artifact identity", "fail" if identity_errors else "pass", f"{len(identity_errors)} finding(s)"))

    common = document_lints.lint_document(document)
    findings.extend(common)
    checks.append(Check(
        "Host-regression document lints",
        "fail" if any(item.severity == "error" for item in common) else "warning" if common else "pass",
        f"{len(common)} finding(s); document version, graph, ports and panel contracts"))
    shaders, count = shader_findings(document, project, workspace)
    findings.extend(shaders)
    checks.append(Check(
        "Embedded SkSL",
        "fail" if any(item.severity == "error" for item in shaders) else "warning" if shaders else
        "pass" if count else "skip",
        f"{count} shader(s) registered, linted and compiled with local Skia" if count else "no drawing surface"))

    errors = [item for item in findings if item.severity == "error"]
    return {
        "schema": 1,
        "phase": "offline-artifact",
        "projectId": project["projectId"],
        "surface": project["surface"],
        "status": "fail" if errors else "pass",
        "decision": "blocked-before-host-test" if errors else "ready-for-controlled-host-test",
        "artifactSha256": digest,
        "checks": [asdict(item) for item in checks],
        "findings": [asdict(item) for item in findings],
        "hostEvidenceStillRequired": [
            "loader and Nitro JIT acceptance",
            "panel rendering and mouse interaction",
            {"modulator": "modulation output and mapping", "grid-module": "audible signal path and Grid cabling",
             "device": "audible signal path and nested chains"}[project["surface"]],
            "project recall",
        ],
    }


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path, help="workspace containing bitwig-project.json")
    parser.add_argument("--project-file", type=Path, help="same-workspace platform contract")
    parser.add_argument("--report", type=Path, help="optional JSON report path")
    parser.add_argument("--compiler-inputs", type=Path,
                        help="exact-build compiler inputs; enables AST parity and semantic analysis")
    args = parser.parse_args()
    try:
        result = preflight(args.workspace, args.project_file,
                           compiler_inputs=args.compiler_inputs.resolve() if args.compiler_inputs else None)
    except (OSError, KeyError, ValueError, RuntimeError) as error:
        print(f"[error] preflight setup: {error}", file=sys.stderr)
        return 2
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    for check in result["checks"]:
        print(f"[{check['status']}] {check['name']}: {check['detail']}")
    for finding in result["findings"]:
        print(f"[{finding['severity']}] {finding['rule']}: {finding['message']}")
    print(f"Preflight result: {result['status'].upper()} ({result['decision']})")
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
