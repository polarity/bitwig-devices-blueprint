"""Offline readiness gate for original Bitwig device experiments.

The preflight combines Bitwig's exact-version Nitro text parser with repository
roundtrips and evidence-backed checks for host traps that the parser does not
cover. A pass means that a candidate is ready for a controlled Bitwig test. It
does not claim JIT, loader, audio, GUI, or project-recall acceptance.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from decimal import Decimal
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Iterable

from bitwig_nitro import decompile_nitrobin, serialize_nitrobin


ROOT = Path(__file__).resolve().parents[1]
LOCAL_RESEARCH = ROOT / "local/research-tools/windows-6.1"
DEFAULT_PARSER = Path(os.environ.get("BITWIG_RESEARCH_BIN", str(LOCAL_RESEARCH / "bwmodule-parser.exe")))
DEFAULT_BRIDGE = Path(os.environ.get("BITWIG_RESEARCH_BRIDGE", str(LOCAL_RESEARCH / "bridge")))
DEFAULT_INSTALL = Path("C:/Program Files/Bitwig Studio")
EFFECT_PROFILE = "bitwig-6.1-hybrid"
INSTRUMENT_PROFILE = "bitwig-6.1-instrument-hybrid"
EFFECT_613_PROFILE = "bitwig-6.1.3-hybrid"
NOTE_PROFILE = "bitwig-6.1-note-hybrid"
NOTE_613_PROFILE = "bitwig-6.1.3-note-hybrid"
NATIVE_NOTE_EVENT_TYPE = "{i32, i32, i32, float, i32, i32, i32, [ 16 x i8 ]}"
INSTRUMENT_EVENT_613_PROFILE = "bitwig-6.1.3-event-instrument"
INSTRUMENT_NATIVE_VOICE_613_PROFILE = "bitwig-6.1.3-native-voice-instrument"
SUPPORTED_PROFILES = (EFFECT_PROFILE, INSTRUMENT_PROFILE, EFFECT_613_PROFILE, NOTE_PROFILE,
                      NOTE_613_PROFILE, INSTRUMENT_EVENT_613_PROFILE, INSTRUMENT_NATIVE_VOICE_613_PROFILE)
# Backward-compatible public name used by existing repository tests/tools.
SUPPORTED_PROFILE = EFFECT_PROFILE
PROFILE_PANEL_HEIGHT = 53  # Standard chain panel, verified with Normalizer/Growl.
PROFILE_JAR_SHA256 = "4fa6dbab06dcd3b7bdd5b42ea513285415cee070915304209a1b330f5a24ec6c"
PROFILE_BRIDGE_SHA256 = "7f7c70e203a80f1f3bc2cdb725e80a4f01b3e98f2a87b56a124611fc56832be3"
PROFILE_PARSER_SHA256 = "beec9482cca02a8aeb57ddd69e9e15f52d94fa95e60047fd67d1f93cea2b2afe"


@dataclass(frozen=True)
class Finding:
    severity: str
    rule: str
    message: str
    path: str
    line: int | None = None


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


class PreflightError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_hash(path: Path, expected: str, label: str) -> None:
    if not path.is_file():
        raise PreflightError(f"{label} is missing: {path}")
    actual = sha256(path)
    if actual != expected:
        raise PreflightError(f"{label} SHA-256 mismatch: expected {expected}, found {actual}")


def run(command: list[str], *, cwd: Path = ROOT, timeout: int = 120,
        input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        input=input_text,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise PreflightError(f"command failed: {' '.join(command)}\n{detail}")
    return result


def line_of(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def without_comments(source: str) -> str:
    return re.sub(r"//[^\n]*|/\*.*?\*/", lambda match: "\n" * match.group(0).count("\n"), source, flags=re.S)


def block(source: str, name: str) -> tuple[str, int] | None:
    match = re.search(rf"\b{re.escape(name)}\s*\{{", source)
    if not match:
        return None
    start = source.find("{", match.start())
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start + 1:index], start + 1
    return None


def lint_nitro_source(path: Path, config: dict[str, Any] | None = None) -> list[Finding]:
    source = path.read_text(encoding="utf-8")
    clean = without_comments(source)
    findings: list[Finding] = []

    patterns = (
        ("NITRO001", r"\b(tan|tanh)\s*\(", "Bitwig 6.1 parses this call but its Nitro semantic/JIT stage does not provide it; use sin/cos or a reviewed approximation."),
        ("NITRO016", r"\bmix\s*\(", "Bitwig 6.1 parses shader-style mix() but its Nitro semantic/JIT stage does not provide it; use explicit a + (b - a) * amount arithmetic."),
        ("NITRO020", r"\bclamp\s*\(", "Bitwig 6.1 parses clamp() but its Nitro semantic/JIT stage does not provide it; use a reviewed helper with explicit comparisons."),
        ("NITRO002", r"\b(?:f32|i32)\s*\(|\bcast\s*<", "Unsupported cast spelling for this host profile; use the postfix form such as value.f32."),
        ("NITRO003", r"(?<![A-Za-z0-9_.])(?:\d+(?:\.\d*)?|\.\d+)[eE][+-]?\d+", "Scientific notation is rejected by the Bitwig 6.1 Nitro text parser; write a decimal literal."),
        ("NITRO004", r"\b[A-Za-z_]\w*\s*\.[xyzw]\b", "Shader-style vector swizzles are unsafe in this Nitro target; use vector masks/reductions."),
    )
    for rule, expression, message in patterns:
        for match in re.finditer(expression, clean):
            findings.append(Finding("error", rule, message, str(path), line_of(clean, match.start())))

    for name in re.findall(r"\bvalue_inport\s+i32\s+([A-Za-z_]\w*)", clean):
        expression = rf"\(\s*{re.escape(name)}\s*[+-]\s*\d+\s*\)\s*(?!\.f32)\*\s*\d+\.\d+"
        for match in re.finditer(expression, clean):
            findings.append(Finding(
                "error", "NITRO019",
                "Bitwig 6.1 rejects implicit i32-to-f32 promotion in arithmetic. Cast the integer sub-expression with postfix .f32 before combining it with a floating literal.",
                str(path), line_of(clean, match.start()),
            ))

    process = block(clean, "process")
    for match in re.finditer(r"\b[A-Za-z_]\w*\([^;{}]*\)\s*=(?!=)", clean):
        findings.append(Finding(
            "error", "NITRO021",
            "A function call cannot be an assignment target in the 6.1.3 host semantic stage; write through an explicit setter or array slot.",
            str(path), line_of(clean, match.start()),
        ))
    if process:
        process_start = process[1]
        process_end = process_start + len(process[0])
        for match in re.finditer(r"\bsend_emplace\b", clean):
            if not process_start <= match.start() < process_end:
                findings.append(Finding(
                    "error", "NITRO022",
                    "Bitwig 6.1.3 permits send_emplace only inside process; stage helper events and emit them there.",
                    str(path), line_of(clean, match.start()),
                ))
    outports = re.findall(r"\b(?:audio|value|event)_outport\s+[^;=]+?\b([A-Za-z_]\w*)\s*(?:=[^;]*)?;", clean)
    audio_outputs = re.findall(r"\baudio_outport\b", clean)
    if len(re.findall(r"\baudio_inport\b", clean)) >= 31:
        findings.append(Finding(
            "error", "NITRO015",
            "31 or more audio inputs are blocked in the Windows 6.1 profile: Growl's 31-input build generated only a return-2 process stub, while 30 inputs executed. Use value inputs for discrete controls or split the component.",
            str(path), None,
        ))
    if len(audio_outputs) >= 4:
        findings.append(Finding(
            "error", "NITRO014",
            "Four or more audio outputs are blocked in the Windows 6.1 profile: Growl's four-output JIT wrapper faults with a 16-byte-aligned output-pointer table. Pack display data or split components; a different ABI requires independent native validation.",
            str(path), None,
        ))
    input_ports = re.findall(r"\b(?:audio|value|event)_inport\b", clean)
    if not input_ports and audio_outputs and process:
        process_text = process[0]
        constant_assignments = re.findall(
            r"\b([A-Za-z_]\w*)\s*=\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)\s*;",
            process_text,
        )
        residue = re.sub(
            r"\b[A-Za-z_]\w*\s*=\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)\s*;",
            "",
            process_text,
        ).strip()
        if len(constant_assignments) == len(audio_outputs) and not residue:
            findings.append(Finding(
                "error", "NITRO017",
                "Inputless constant-only audio sidecars recurse in the Bitwig 6.1 live semantic stage. Add an explicit connected seed input or use a reviewed native constant source.",
                str(path), None,
            ))
    zero_i32_states = re.findall(r"\bi32\s+([A-Za-z_]\w*)\s*=\s*0\s*;", clean)
    for handler_match in re.finditer(r"\bvalue_inport\s+i32\s+([A-Za-z_]\w*)\s*\{", clean):
        input_name = handler_match.group(1)
        handler = block(clean[handler_match.start():], input_name)
        if handler is None:
            continue
        handler_text = handler[0]
        for state_name in zero_i32_states:
            comparison = (
                rf"\b{re.escape(input_name)}\s*!=\s*{re.escape(state_name)}\b"
                rf"|\b{re.escape(state_name)}\s*!=\s*{re.escape(input_name)}\b"
            )
            if re.search(comparison, handler_text):
                findings.append(Finding(
                    "error", "NITRO018",
                    "A zero-initialized i32 comparison state in a value-input handler recurses in the Bitwig 6.1 live semantic stage. Use a matching non-zero counter origin.",
                    str(path), line_of(clean, handler_match.start()),
                ))
                break
    if process:
        process_text, process_offset = process
        for name in outports:
            for match in re.finditer(rf"\b{re.escape(name)}\b", process_text):
                suffix = process_text[match.end():]
                if re.match(r"\s*(?:\[[^\]]+\]\s*)?=", suffix):
                    continue
                if re.search(r"\b(?:send|send_emplace)\s+$", process_text[:match.start()]):
                    continue  # The send target is a write; its payload is still checked.
                findings.append(Finding(
                    "error", "NITRO005",
                    f"Output port {name!r} is read inside process. Bitwig 6.1 can report semantic recursion; compute through a local value.",
                    str(path), line_of(clean, process_offset + match.start()),
                ))

    # A KeySignature field is not filled by the ksig_hash inport on its own.
    # Factory modules decode the hash in the inport handler; without that call
    # Prism's key display read NO KEY and every tonic/scale lookup ran on an
    # empty chromatic key (Bitwig 6.1.3, 2026-11-10).
    key_fields = re.findall(r"\bKeySignature\s+([A-Za-z_]\w*)\s*;", clean)
    hash_inport = re.search(r"\bvalue_inport\b[^;{]*\bksig_hash\b", clean)
    if key_fields and hash_inport:
        for field_name in key_fields:
            if not re.search(rf"\b{re.escape(field_name)}\s*\.\s*setFromHash\s*\(", clean):
                findings.append(Finding(
                    "error", "NITRO023",
                    f"KeySignature {field_name!r} is never filled: call {field_name}.setFromHash(ksig_hash) "
                    "in the ksig_hash inport handler. The inport alone leaves an empty chromatic key on C.",
                    str(path), line_of(clean, hash_inport.start()),
                ))

    arrays = {
        match.group("name"): match.group("size").strip()
        for match in re.finditer(
            r"\b(?:f32|f64|i32|bool)(?:<[^>]*>)?\s*\[\s*(?P<size>[^\]]+)\s*\]\s*(?P<name>[A-Za-z_]\w*)\s*(?:=[^;]*)?;",
            clean,
        )
    }
    def zero_init_match(name):
        return re.search(rf"\bzero_init\s+{re.escape(name)}\b|\b{re.escape(name)}\s*=\s*zero_init\b", clean)
    for name, size in arrays.items():
        if zero_init_match(name):
            literal = int(size) if size.isdigit() else None
            if literal is None or literal > 32768:
                match = zero_init_match(name)
                findings.append(Finding(
                    "error", "NITRO006",
                    f"Large or dynamic array {name!r} is cleared with zero_init. This crashed the 6.1 host for Growl; use a write-before-read validity guard.",
                    str(path), line_of(clean, match.start()) if match else None,
                ))

    recall = (config or {}).get("recallSafety")
    if arrays and recall is None:
        findings.append(Finding(
            "error", "NITRO007",
            "State arrays require an explicit preflight.recallSafety contract as a defensive policy for uncertain initialization order during project recall.",
            str(path), None,
        ))
    elif arrays and recall:
        canary = recall.get("canary")
        if not canary or not re.search(rf"\bi32\s+{re.escape(str(canary))}\b", clean):
            findings.append(Finding("error", "NITRO008", "Configured recall canary is not declared as i32 state.", str(path), None))
        elif process:
            process_text, process_offset = process
            first_access = min(
                (match.start() for name in arrays for match in re.finditer(rf"\b{re.escape(name)}\s*\[", process_text)),
                default=len(process_text),
            )
            guard = re.search(rf"\bif\s*\(\s*{re.escape(str(canary))}\s*!?=", process_text)
            assignment = re.search(rf"\b{re.escape(str(canary))}\s*=", process_text)
            if not guard or not assignment or guard.start() >= first_access or assignment.start() >= first_access:
                findings.append(Finding(
                    "error", "NITRO009",
                    "Recall canary must be checked and initialized in process before the first state-array access.",
                    str(path), line_of(clean, process_offset + first_access) if first_access < len(process_text) else None,
                ))

        for item in recall.get("arrays", []):
            name = item.get("name")
            if name not in arrays:
                findings.append(Finding("error", "NITRO010", f"Configured recall array {name!r} is not declared.", str(path), None))
                continue
            for required in item.get("requiredBeforeFirstAccess", []):
                first = re.search(rf"\b{re.escape(name)}\s*\[", process[0] if process else "")
                position = (process[0].find(required) if process else -1)
                if position < 0 or (first and position >= first.start()):
                    findings.append(Finding(
                        "error", "NITRO011",
                        f"Recall guard {required!r} must appear before the first access to {name!r}.",
                        str(path), None,
                    ))
            read_guard = item.get("readGuard")
            if read_guard:
                read_match = re.search(rf"=\s*{re.escape(name)}\s*\[", process[0] if process else "")
                guard_position = (process[0].find(read_guard) if process else -1)
                if guard_position < 0 or (read_match and guard_position >= read_match.start()):
                    findings.append(Finding(
                        "error", "NITRO012",
                        f"Write-before-read guard {read_guard!r} must precede the first read from {name!r}.",
                        str(path), None,
                    ))
            if item.get("initialization") == "write-before-read" and zero_init_match(name):
                findings.append(Finding("error", "NITRO013", f"{name!r} must use write-before-read initialization, not zero_init.", str(path), None))

    return findings


def lint_sksl_source(source: str, path: str) -> list[Finding]:
    clean = without_comments(source)
    findings: list[Finding] = []
    if not re.search(r"\b(?:half4|float4)\s+main\s*\(\s*float2\b", clean):
        findings.append(Finding("error", "SKSL001", "Shader has no supported main(float2 ...) entry point.", path, None))
    for match in re.finditer(r"\btanh\s*\(", clean):
        findings.append(Finding(
            "error", "SKSL002",
            "tanh is unavailable in the tested Bitwig 6.1 SkSL compiler; use a portable approximation.",
            path, line_of(clean, match.start()),
        ))
    return findings


def validate_profile_config(config: dict[str, Any], path: str) -> list[Finding]:
    """Pin the reviewed 6.1 hybrid shell facts instead of trusting project input."""
    findings: list[Finding] = []
    profile = config.get("profile", EFFECT_PROFILE)
    if profile not in SUPPORTED_PROFILES:
        findings.append(Finding(
            "error", "CONFIG011",
            f"Unsupported preflight profile {profile!r}; expected one of {SUPPORTED_PROFILES!r}.",
            path, None,
        ))
    component_specs = config.get("nitroComponents")
    if component_specs is not None:
        if not isinstance(component_specs, list) or not component_specs:
            findings.append(Finding("error", "CONFIG007", "nitroComponents must be a non-empty list.", path, None))
        else:
            main_count = 0
            for index, specification in enumerate(component_specs):
                if not isinstance(specification, dict):
                    findings.append(Finding("error", "CONFIG007", f"nitroComponents[{index}] must be an object.", path, None))
                    continue
                maximum = specification.get("maxDrawingVectors")
                outputs = specification.get("expectedOutputs")
                instances = specification.get("instances")
                inputs = specification.get("expectedInputs")
                if not isinstance(maximum, int) or maximum < 0 or maximum > 2:
                    findings.append(Finding("error", "CONFIG001", f"nitroComponents[{index}].maxDrawingVectors must be 0..2.", path, None))
                audio_outputs = specification.get("expectedAudioOutputs")
                if audio_outputs is not None:
                    if not isinstance(audio_outputs, int) or audio_outputs < 0 or audio_outputs > 3 or not isinstance(outputs, int) or outputs < max(1, audio_outputs):
                        findings.append(Finding("error", "CONFIG003", f"nitroComponents[{index}] must declare 0..3 audio outputs within its total output count.", path, None))
                elif not isinstance(outputs, int) or outputs < 1 or outputs > 3 or (
                    isinstance(maximum, int) and 0 <= maximum <= 2 and outputs > maximum + 1
                ):
                    findings.append(Finding("error", "CONFIG003", f"nitroComponents[{index}].expectedOutputs must be 1..3 and fit its vector limit.", path, None))
                if not isinstance(inputs, int) or inputs < 0:
                    findings.append(Finding("error", "CONFIG007", f"nitroComponents[{index}].expectedInputs must be a non-negative integer.", path, None))
                if not isinstance(instances, int) or instances < 1:
                    findings.append(Finding("error", "CONFIG007", f"nitroComponents[{index}].instances must be positive.", path, None))
                if not isinstance(specification.get("source"), str) or not isinstance(specification.get("componentName"), str):
                    findings.append(Finding("error", "CONFIG007", f"nitroComponents[{index}] requires source and componentName strings.", path, None))
                main_count += specification.get("isMainAudio") is True
            if main_count != 1:
                findings.append(Finding("error", "CONFIG008", "Exactly one nitroComponents entry must set isMainAudio=true.", path, None))
    else:
        maximum = config.get("maxDrawingVectors")
        if not isinstance(maximum, int) or maximum < 0 or maximum > 2:
            findings.append(Finding(
                "error", "CONFIG001",
                "The Windows 6.1 profile permits at most two drawing vectors (three audio outputs total) to avoid the reproduced four-output JIT pointer-table alignment fault.",
                path, None,
            ))
    if profile in (EFFECT_PROFILE, EFFECT_613_PROFILE):
        source_class = config.get("requiredAudioInputSourceClassId")
        normalizer_route = profile == EFFECT_613_PROFILE and config.get("audioInputRoute") == "normalizer-direct-audio-proxy"
        if source_class != (154 if normalizer_route else 1371):
            findings.append(Finding(
                "error", "CONFIG002",
                "The reviewed Bitwig 6.1 hybrid shell requires stereo compensation class 1371 as the Nitro audio source.",
                path, None,
            ))
        delay_source_class = config.get("requiredDelayCompensationInputSourceClassId")
        if delay_source_class is not None and delay_source_class != 154:
            findings.append(Finding(
                "error", "CONFIG005",
                "The reviewed Bitwig 6.1 shell wires delay compensation to embedded audio proxy class 154.",
                path, None,
            ))
        proxy_input_count = config.get("requiredProxyInputCount")
        if proxy_input_count is not None and proxy_input_count != 2:
            findings.append(Finding(
                "error", "CONFIG006",
                "The reviewed Bitwig 6.1 audio-effect shell requires two root proxy inputs: audio followed by note.",
                path, None,
            ))
    elif profile == INSTRUMENT_EVENT_613_PROFILE:
        if config.get("requiredDeviceType") != 2 or config.get("requiredProxyInputCount") != 2:
            findings.append(Finding("error", "CONFIG028", "The source-built instrument requires note_to_audio type 2 and two root proxy inputs.", path))
        if config.get("requiredEventInputPort") != "note_in" or config.get("requiredAudioOutputCount") != 1:
            findings.append(Finding("error", "CONFIG029", "The source-built instrument requires one NoteEvent input and one stereo audio output.", path))
    elif profile == INSTRUMENT_NATIVE_VOICE_613_PROFILE:
        if (config.get("requiredDeviceType"), config.get("requiredProxyInputCount"),
                config.get("requiredAudioOutputCount")) != (2, 2, 1):
            findings.append(Finding("error", "CONFIG030", "The native voice shell requires a note-to-audio device, two proxy inputs and one stereo proxy output.", path))
        if config.get("requiredVoiceLevels") not in (2, 32):
            findings.append(Finding("error", "CONFIG030", "Declare the number of native harmonic level targets.", path))
    elif profile == INSTRUMENT_PROFILE:
        if config.get("requiredDeviceType") != 2:
            findings.append(Finding(
                "error", "CONFIG011",
                "The reviewed instrument shell requires note_to_audio device type byte 2.",
                path, None,
            ))
        if config.get("requiredProxyInputCount") != 2:
            findings.append(Finding(
                "error", "CONFIG006",
                "The reviewed instrument shell requires audio then note root proxy entries.",
                path, None,
            ))
        if config.get("requiredAudioOutputCount") != 1:
            findings.append(Finding(
                "error", "CONFIG012",
                "The reviewed instrument shell requires exactly one stereo root audio output.",
                path, None,
            ))
        voice_sources = config.get("requiredVoiceSources")
        if voice_sources != ["PITCH_IN_LAST", "KEYS_HELD_IN"]:
            findings.append(Finding(
                "error", "CONFIG013",
                "Instrument voice sources must be the reviewed PITCH_IN_LAST and KEYS_HELD_IN host-lane pair.",
                path, None,
            ))
        if config.get("requiredVoiceSourcePolyphonic") != [False, False]:
            findings.append(Finding(
                "error", "CONFIG017",
                "The reviewed last-pitch and held-key sources must both be non-polyphonic.",
                path, None,
            ))
        voice_ports = config.get("requiredVoiceInputPorts")
        if voice_ports != ["pitch_semitones", "gate", "trigger_serial"]:
            findings.append(Finding(
                "error", "CONFIG013",
                "The main Nitro ABI must begin with pitch_semitones, gate and trigger_serial.",
                path, None,
            ))
        if config.get("requiredVoiceChannelCount") != 8:
            findings.append(Finding(
                "error", "CONFIG016",
                "The reviewed host contract exposes last-pitch, gate and trigger data as eight-lane vectors.",
                path, None,
            ))
        voice_connections = config.get("requiredVoiceConnections")
        if voice_connections != [
            ["pitch_semitones", "pitchOut[1]"],
            ["gate", "keysHeldOut"],
            ["trigger_serial", "keysHeldOut"],
        ]:
            findings.append(Finding(
                "error", "CONFIG013",
                "Instrument voice ports must be wired to the reviewed last-pitch, gate and trigger-count sources.",
                path, None,
            ))
        if config.get("requireMainCoreNonPolyphonic") is not True:
            findings.append(Finding(
                "error", "CONFIG014",
                "The instrument profile requires a non-polyphonic main Nitro core that reduces the reviewed host vectors.",
                path, None,
            ))
        if config.get("requiredOpaqueHostNitroCores") != 1:
            findings.append(Finding(
                "error", "CONFIG015",
                "The reviewed instrument shell retains exactly one opaque host MonoLogic core.",
                path, None,
            ))
    elif profile in (NOTE_PROFILE, NOTE_613_PROFILE):
        if config.get("requiredDeviceType") != 1:
            findings.append(Finding(
                "error", "CONFIG018",
                "The reviewed note shell requires note_to_note device type byte 1.",
                path, None,
            ))
        if config.get("requiredProxyInputCount") != 2:
            findings.append(Finding(
                "error", "CONFIG019",
                "The reviewed note shell has no audio input and two root proxy entries: audio passthrough class 242 then note passthrough class 61.",
                path, None,
            ))
        if config.get("requiredProxyInputPortClasses") != [242, 61]:
            findings.append(Finding(
                "error", "CONFIG023",
                "The reviewed factory note shell declares the root proxy input sequence 242 then 61; a different sequence needs a reviewed shell.",
                path, None,
            ))
        if config.get("requiredNoteOutputCount") != 1:
            findings.append(Finding(
                "error", "CONFIG020",
                "The reviewed note shell requires exactly one NoteEvent root output.",
                path, None,
            ))
        if config.get("requiredDrawingAudioOutputs") != 2:
            findings.append(Finding(
                "error", "CONFIG021",
                "The reviewed note shell exposes two four-lane drawing vectors and no other audio output.",
                path, None,
            ))
        if config.get("requireMainCoreScheduled") is not True:
            findings.append(Finding(
                "error", "CONFIG022",
                "The note profile requires explicit root scheduling of the generating Nitro core.",
                path, None,
            ))
    if component_specs is None:
        expected_outputs = config.get("expectedNitroOutputs")
        if not isinstance(expected_outputs, int) or expected_outputs < 1 or expected_outputs > 3 or (
            isinstance(maximum, int) and 0 <= maximum <= 2 and expected_outputs > maximum + 1
        ):
            findings.append(Finding(
                "error", "CONFIG003",
                "expectedNitroOutputs must include one audio output and no more than the configured drawing-vector limit.",
                path, None,
            ))
    shader_count = config.get("expectedSkSLShaders", 1)
    minimum_shaders = 0 if profile == INSTRUMENT_NATIVE_VOICE_613_PROFILE else 1
    if not isinstance(shader_count, int) or not minimum_shaders <= shader_count <= 64:
        findings.append(Finding("error", "CONFIG009", f"expectedSkSLShaders must be an integer from {minimum_shaders} to 64.", path, None))
    drawing_sources = config.get("requiredDrawingSources")
    writer_name = config.get("requiredEventParameterWriter")
    note_profile = profile in (NOTE_PROFILE, NOTE_613_PROFILE)
    native_note_thru = config.get("requireNativeNoteThru")
    if native_note_thru is not None and not (note_profile and isinstance(native_note_thru, bool)):
        findings.append(Finding("error", "CONFIG033", "requireNativeNoteThru is a note-profile boolean.", path))
    needs_drawing_sources = drawing_sources is not None or (note_profile and isinstance(shader_count, int) and shader_count > 1)
    if needs_drawing_sources or writer_name is not None:
        named_specs = [spec for spec in component_specs if isinstance(spec, dict)] if isinstance(component_specs, list) else []
        names = [spec.get("componentName") for spec in named_specs]
        single_names = {name for spec in named_specs if isinstance(name := spec.get("componentName"), str)
                        and spec.get("instances") == 1 and names.count(name) == 1}
        if needs_drawing_sources:
            if not (note_profile and isinstance(drawing_sources, list) and drawing_sources
                    and len(drawing_sources) == shader_count
                    and all(isinstance(name, str) and name in single_names for name in drawing_sources)):
                findings.append(Finding("error", "CONFIG031",
                    "requiredDrawingSources must name one unique Nitro instance per note-profile drawing, in root child order.", path))
        if writer_name is not None and not (note_profile and isinstance(writer_name, str) and writer_name in single_names):
            findings.append(Finding("error", "CONFIG032",
                "requiredEventParameterWriter must name a unique Nitro instance in the note profile.", path))
    delay_samples = config.get("requiredProcessingDelaySamples")
    if delay_samples is not None and (not isinstance(delay_samples, int) or not 0 <= delay_samples <= 32767):
        findings.append(Finding("error", "CONFIG010", "requiredProcessingDelaySamples must fit the reviewed Int16 placeholder range.", path, None))
    if (shader_count != 0 or minimum_shaders != 0) and config.get("requireSkiaCompiler") is not True:
        findings.append(Finding(
            "error", "CONFIG004",
            "The reviewed hybrid profile requires the local Skia RuntimeEffect compiler; set requireSkiaCompiler to true.",
            path, None,
        ))
    return findings


def owned_objects(root: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield root
    for entry in root.get("fields", {}).values():
        kind = entry.get("type")
        value = entry.get("value")
        if kind == "Object" and isinstance(value, dict):
            yield from owned_objects(value)
        elif kind == "ObjectList" and isinstance(value, list):
            for item in value:
                child = item.get("Object") if isinstance(item, dict) else None
                if isinstance(child, dict):
                    yield from owned_objects(child)


def direct_refs(item: dict[str, Any]) -> Iterable[int]:
    for entry in item.get("fields", {}).values():
        kind = entry.get("type")
        value = entry.get("value")
        if kind == "ObjectRef":
            yield int(value)
        elif kind == "ObjectList" and isinstance(value, list):
            for nested in value:
                if isinstance(nested, dict) and "ObjectRef" in nested:
                    yield int(nested["ObjectRef"])


def field_by_suffix(item: dict[str, Any], suffix: str) -> dict[str, Any]:
    matches = [value for name, value in item.get("fields", {}).items() if name.endswith(suffix)]
    if len(matches) != 1:
        raise PreflightError(f"expected one field ending {suffix!r} in class {item.get('class_id')}, found {len(matches)}")
    return matches[0]


def resolve_object(entry: dict[str, Any], objects: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolve either a field value or an ObjectList entry; never index invalid IDs."""
    kind = entry.get("type")
    value = entry.get("value")
    child = value if kind == "Object" else entry.get("Object")
    if isinstance(child, dict):
        return child
    reference = value if kind == "ObjectRef" else entry.get("ObjectRef")
    if type(reference) is int and 1 <= reference <= len(objects):
        return objects[reference - 1]
    return {}


def inspect_indexed_controls(document: dict[str, Any]) -> list[Finding]:
    """Reject the indexed-widget binding failures reproduced in Rivet 6.1.

    Boolean toggles on Boolean atoms and other widget families are unaffected.
    This checks serialized bindings, not runtime interaction or text fit.
    """
    findings: list[Finding] = []
    objects = list(owned_objects(document["body"]))
    for index, item in enumerate(objects, 1):
        widget_class = item.get("class_id")
        if widget_class not in (1702, 1712, 1714):
            continue
        try:
            target = resolve_object(field_by_suffix(item, "data_model#6220"), objects)
        except PreflightError:
            continue  # Other bindings/actions are outside this narrow rule.
        if target.get("class_id") != 180:
            continue
        path = f"artifact/object[{index}]"
        if widget_class == 1702:
            findings.append(Finding(
                "error", "PANEL007",
                "Boolean toggle class 1702 is bound to an indexed atom: Rivet 6.1 changed only selection paint, not the parameter. Use an indexed chooser (1712/1714).",
                path,
            ))
            continue
        try:
            attribute = field_by_suffix(item, "attribute_id#11012").get("value")
        except PreflightError:
            attribute = None
        if attribute != 457:
            findings.append(Finding(
                "error", "PANEL008",
                f"Indexed chooser requires explicit value attribute 457; found {attribute!r}.", path,
            ))
        try:
            style = field_by_suffix(item, "label_style#6257" if widget_class == 1712 else "label_style#6262").get("value")
            value_type = resolve_object(field_by_suffix(target, "value_type#702"), objects)
            entries = field_by_suffix(value_type, "items#393").get("value", [])
            if style == 1 and any(
                not field_by_suffix(resolve_object(entry, objects), "icon#9823").get("value")
                for entry in entries
            ):
                findings.append(Finding(
                    "error", "PANEL009",
                    "Icon-only indexed chooser has an item with an empty icon; use text label style 0 or supply valid icons.", path,
                ))
        except PreflightError:
            findings.append(Finding(
                "error", "PANEL010", "Indexed chooser label/item metadata is incomplete.", path,
            ))
    return findings


def inspect_chooser_titles(document: dict[str, Any]) -> list[Finding]:
    """Warn about indexed popup choosers that rely on their own title text.

    Prism on Windows Bitwig 6.1.3 (2026-11-10) showed only the values of its
    16..27 x 3 popup choosers; title#6241 was not drawn. A separate native
    label is the verified way to caption them.
    """
    findings: list[Finding] = []
    objects = list(owned_objects(document["body"]))
    for index, item in enumerate(objects, 1):
        if item.get("class_id") != 1714:
            continue
        try:
            title = field_by_suffix(item, "title#6241").get("value")
        except PreflightError:
            continue
        if isinstance(title, str) and title.strip():
            findings.append(Finding(
                "warning", "PANEL015",
                f"Popup chooser title {title!r} may not be drawn by the host; caption it with a separate native label.",
                f"artifact/object[{index}]",
            ))
    return findings


def inspect_decimal_controls(document: dict[str, Any]) -> list[Finding]:
    """Check the reviewed decimal-knob family, retaining the valid default sentinel."""
    findings = []
    objects = list(owned_objects(document["body"]))
    for index, item in enumerate(objects, 1):
        if item.get("class_id") != 1687:
            continue
        path = f"artifact/object[{index}]"
        try:
            target = resolve_object(field_by_suffix(item, "data_model#6220"), objects)
            if not target:
                raise PreflightError("unresolved knob target")
            if target.get("class_id") != 289:
                findings.append(Finding("warning", "PANEL011", "Knob target is outside the reviewed decimal family; binding coverage is incomplete.", path))
                continue
            attribute = field_by_suffix(item, "attribute_id#11012").get("value")
            if attribute not in (712, 255):
                raise PreflightError(f"Decimal knob requires value 712 or host default 255, found {attribute!r}")
        except PreflightError as exc:
            findings.append(Finding("error", "PANEL012", str(exc), path))
    return findings


def inspect_artifact_identity(document: dict[str, Any], project: dict[str, Any]) -> list[Finding]:
    """Bind downstream installation evidence to the identity in the built bytes."""
    try:
        expected = project["identity"]["uuid"]
        actual = field_by_suffix(document["body"], "device_UUID#385")["value"]
        if not isinstance(expected, str) or not expected or actual != expected:
            raise ValueError("project UUID and built device UUID differ")
        metadata = document["metadata"]["fields"]
        for key in ("device_id", "device_uuid"):
            if key in metadata and metadata[key].get("value") != expected:
                raise ValueError(f"metadata {key} disagrees with built UUID")
    except (PreflightError, KeyError, TypeError, ValueError) as exc:
        return [Finding("error", "DOC002", f"Artifact identity mismatch: {exc}", "artifact/identity")]
    return []


def inspect_control_contract(document: dict[str, Any], contract: Any) -> list[Finding]:
    """Opt-in native-control coverage and declared paint ownership, not shader analysis.

    V1 deliberately covers one native widget per parameter in an always-visible
    panel. Multi-view and custom shader hit-target contracts need a later version.
    """
    path = "preflight/controlContract"
    if contract is None:
        return [Finding("warning", "PANEL013", "No control/paint contract: complete UI coverage and mouse acceptance remain unverified.", path)]
    try:
        if not isinstance(contract, dict) or contract.get("schema") != 1:
            raise ValueError("expected control contract schema 1")
        if contract.get("shaderRole") not in ("backdrop-and-trigger-flash", "non-interactive-display"):
            raise ValueError("v1 requires decorative or display-only shaders with native controls")
        controls = contract["controls"]
        if not isinstance(controls, list) or not controls:
            raise ValueError("controls must be a nonempty list")
        objects = list(owned_objects(document["body"]))
        families = {1687: 289, 1712: 180, 1714: 180, 1880: 239}
        widgets: dict[str, list[dict]] = {}
        atoms: dict[str, dict] = {}
        for item in objects:
            if item.get("class_id") in families.values():
                identifier = field_by_suffix(item, "preset_identifier#701").get("value")
                if identifier:
                    if identifier in atoms:
                        raise ValueError(f"duplicate parameter identifier {identifier}")
                    atoms[identifier] = item
            if item.get("class_id") not in families:
                continue
            target = resolve_object(field_by_suffix(item, "data_model#6220"), objects)
            if target.get("class_id") != families[item["class_id"]]:
                raise ValueError("native widget and parameter class mismatch")
            identifier = field_by_suffix(target, "preset_identifier#701").get("value")
            widgets.setdefault(identifier, []).append(item)
        declared = set()
        for spec in controls:
            identifier = spec["parameter"]
            if not isinstance(identifier, str) or not identifier or identifier in declared:
                raise ValueError("empty or duplicate control identifier")
            declared.add(identifier)
            if spec.get("drawOwners") != ["native"] or spec.get("inputOwner") != "native":
                raise ValueError(f"{identifier}: v1 requires one native paint/input owner; no duplicate shader knob")
            matches = widgets.get(identifier, [])
            if len(matches) != 1 or matches[0]["class_id"] != spec["widgetClass"]:
                raise ValueError(f"{identifier}: missing, duplicate or wrong native widget")
            item = matches[0]
            for key in ("is_visible#6309", "is_enabled#6310"):
                if field_by_suffix(item, key).get("value") is not True:
                    raise ValueError(f"{identifier}: v1 requires explicitly visible/enabled controls")
            if spec["widgetClass"] == 1880:
                if spec.get("gestures") != ["trigger"]:
                    raise ValueError("trigger requires a trigger gesture")
                continue
            default = spec["defaultNormalized"]
            if type(default) not in (int, float) or not 0 <= default <= 1:
                raise ValueError("defaultNormalized must be finite and within 0..1")
            value_type = resolve_object(field_by_suffix(atoms[identifier], "value_type#702"), objects)
            if spec["widgetClass"] == 1687:
                if spec.get("gestures") != ["drag", "reset"]:
                    raise ValueError("knob requires drag and reset gestures")
                size = field_by_suffix(item, "knob_size#6223").get("value")
                if size != spec["knobSize"] or type(size) is not int or size not in range(4):
                    raise ValueError(f"{identifier}: incorrect native size preset")
                # V1 normalized evidence is defined only for linear decimal controls.
                if field_by_suffix(value_type, "domain#294").get("value") != 0:
                    raise ValueError("v1 decimal normalization requires linear domain")
                lo = field_by_suffix(value_type, "min#292")["value"]
                hi = field_by_suffix(value_type, "max#293")["value"]
                actual = (field_by_suffix(value_type, "default_value#891")["value"] - lo) / (hi - lo)
            else:
                entries = field_by_suffix(value_type, "items#393")["value"]
                ids = [field_by_suffix(resolve_object(e, objects), "id#650")["value"] for e in entries]
                if len(ids) < 2 or len(set(ids)) != len(ids):
                    raise ValueError("indexed selector requires distinct states")
                states = [i / (len(ids) - 1) for i in range(len(ids))]
                if spec.get("gestures") != ["select"] or spec.get("statesNormalized") != states:
                    raise ValueError("selector must cover every normalized state")
                actual = ids.index(field_by_suffix(value_type, "default_value#6958")["value"]) / (len(ids) - 1)
            if abs(actual - default) > 1e-9:
                raise ValueError(f"{identifier}: default differs from artifact")
        if declared != set(widgets) or declared != set(atoms):
            raise ValueError("contract must cover all reviewed parameter atoms and native widgets")
    except (PreflightError, KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        return [Finding("error", "PANEL014", f"Invalid control/paint contract: {exc}", path)]
    return []


def inspect_instrument_audio_route(document: dict[str, Any], core: dict[str, Any]) -> list[Finding]:
    """Require the reviewed main-core[0] -> FX[0] -> root-audio chain.

    This deliberately verifies the exact Rivet shell, not arbitrary reachability
    through note inputs or unknown processors. A new topology needs review.
    """
    objects = list(owned_objects(document["body"]))

    def input_source(component: dict[str, Any]) -> dict[str, Any]:
        settings = resolve_object(field_by_suffix(component, "settings#6194"), objects)
        connections = field_by_suffix(settings, "inport_connections#614").get("value", [])
        connection = resolve_object(connections[0], objects) if connections else {}
        if field_by_suffix(connection, "outport_index#249").get("value") != 0:
            return {}
        return resolve_object(field_by_suffix(connection, "source_component#248"), objects)

    valid = False
    try:
        outputs = field_by_suffix(document["body"], "proxy_out_ports#178").get("value", [])
        output = resolve_object(outputs[0], objects) if len(outputs) == 1 else {}
        port = resolve_object(field_by_suffix(output, "port#301"), objects)
        fx = input_source(output)
        valid = (
            output.get("class_id") == 50 and port.get("class_id") == 242
            and fx.get("class_id") == 587 and input_source(fx) is core
        )
    except PreflightError:
        pass
    return [] if valid else [Finding(
        "error", "ABI022",
        "Instrument audio route must be main Nitro output 0 -> FX slot audio input 0 -> root audio proxy. Missing, note-only, bypassed or wrong-output routes are not accepted by this shell profile.",
        "artifact",
    )]


def inspect_event_instrument_route(document: dict[str, Any], core: dict[str, Any],
                                   inputs: list[Any], outputs: list[Any], connections: list[Any]) -> list[Finding]:
    """Check the locally authored NoteEvent -> Nitro -> stereo root route."""
    findings: list[Finding] = []
    objects = list(owned_objects(document["body"]))
    def source(entry: dict) -> tuple[dict, int]:
        try:
            return connection_source(resolve_object(entry, objects), objects)
        except PreflightError:
            return {}, -1
    try:
        body_type = field_by_suffix(document["body"], "device_type#391")["value"]
        metadata_type = document["metadata"]["fields"]["device_type"]["value"]
        proxies = field_by_suffix(document["body"], "proxy_in_ports#177")["value"]
        proxy_classes = [resolve_object(field_by_suffix(resolve_object(entry, objects), "port#301"), objects).get("class_id") for entry in proxies]
        root_outputs = field_by_suffix(document["body"], "proxy_out_ports#178")["value"]
        output = resolve_object(root_outputs[0], objects)
        output_connections = field_by_suffix(resolve_object(field_by_suffix(output, "settings#6194"), objects), "inport_connections#614")["value"]
        input_info = inputs[0]["Object"]["fields"]
        output_info = outputs[0]["Object"]["fields"]
        event_ok = (input_info["name#8557"]["value"] == "note_in" and input_info["kind#8561"]["value"] == 2
                    and input_info["event_size#8565"]["value"] == 44 and input_info["event_type#8566"]["value"] == "NoteEvent")
        stereo_ok = output_info["kind#8561"]["value"] == 1 and output_info["channel_count#8559"]["value"] == 2
        drawing_only = len(outputs) <= 3 and all(
            field_by_suffix(item["Object"], "kind#8561").get("value") == 1
            and field_by_suffix(item["Object"], "channel_count#8559").get("value") == 4
            for item in outputs[1:]
        )
        note_source, note_index = source(connections[0])
        audio_source, audio_index = source(output_connections[0])
        output_class = resolve_object(field_by_suffix(output, "port#301"), objects).get("class_id")
        route_ok = (len(connections) == len(inputs) and len(root_outputs) == 1 and len(output_connections) == 1
                    and note_source is resolve_object(proxies[1], objects) and note_index == 0
                    and output.get("class_id") == 50 and output_class == 242
                    and audio_source is core and audio_index == 0)
        if not (body_type == 2 and metadata_type == "note_to_audio" and proxy_classes == [242, 61]
                and event_ok and stereo_ok and drawing_only and route_ok):
            findings.append(Finding("error", "ABI028", "Event instrument requires a NoteEvent input and only stereo core output 0 at the root audio proxy. Any auxiliary outputs must be four-lane drawing vectors.", "artifact"))
    except (PreflightError, KeyError, IndexError, TypeError):
        findings.append(Finding("error", "ABI028", "Original instrument route is missing required fields.", "artifact"))
    return findings


def inspect_native_voice_route(document: dict[str, Any], core: dict[str, Any],
                               inputs: list, outputs: list, level_count: int) -> list[Finding]:
    """Check the native note router, per-voice DSP, lifetime and summed output."""
    body = document["body"]
    objects = list(owned_objects(body))

    def value(atom, field):
        return field_by_suffix(atom, field)["value"]

    def port_info(entry):
        return resolve_object(entry, objects)

    def settings(atom):
        return value(atom, "settings#6194")

    def per_voice(atom):
        return value(settings(atom), "is_polyphonic#615")

    def upstream(atom):
        result = []
        for entry in value(settings(atom), "inport_connections#614"):
            cable = port_info(entry)
            result.append((resolve_object(field_by_suffix(cable, "source_component#248"), objects),
                           value(cable, "outport_index#249")))
        return result

    def signature(port):
        return tuple(value(port_info(port), name) for name in
                     ("name#8557", "kind#8561", "channel_count#8559"))

    def partial_target(atom, identifier):
        return (atom["class_id"] in (289, 2037)
                and any(atom is child for child in children)
                and per_voice(atom) is True
                and value(atom, "preset_identifier#701") == identifier
                and value(atom, "allow_automation#7489") is True
                and value(atom, "allow_modulation#7490") is True)

    def level_routes(controls):
        if level_count == 2:
            return all(signature(inputs[index]) == (f"level_{index:02}", 1, 1)
                       and port == 0 and partial_target(atom, f"H{index:02}")
                       for index, (atom, port) in enumerate(controls[:2], 1)) and len(controls) == 2
        if level_count != 32 or len(controls) not in (24, 25, 29):
            return False
        if len(controls) in (25, 29):
            rolloff, port = controls[-3]
            if (signature(inputs[-3]) != ("rolloff", 1, 1) or port != 0 or rolloff["class_id"] != 289
                    or not any(rolloff is child for child in children) or per_voice(rolloff) is not False
                    or value(rolloff, "preset_identifier#701") != "ROLLOFF"):
                return False
        banks = [("levels", "H", 4), ("decays", "D", 4)]
        if len(controls) == 29:
            banks.append(("attacks", "A", 8))
        index = 0
        for stem, prefix, width in banks:
            for base in range(1, 33, width):
                atom, port = controls[index]
                index += 1
                if (port != 0 or atom["class_id"] != 1721 or per_voice(atom) is not True
                        or not any(atom is child for child in children)
                        or signature(inputs[index]) != (f"{stem}_{base:02}_{base + width - 1:02}", 1, width)):
                    return False
                lanes = upstream(atom)
                if len(lanes) != width or not all(output == 0 and partial_target(source, f"{prefix}{base + lane:02}")
                                                 for lane, (source, output) in enumerate(lanes)):
                    return False
        return True

    try:
        children = [port_info(entry) for entry in value(body, "child_components#173")]
        proxies = [port_info(entry) for entry in value(body, "proxy_in_ports#177")]
        output, = (port_info(entry) for entry in value(body, "proxy_out_ports#178"))
        (summed, sum_port), = upstream(output)
        (lifetime, audio_port), = upstream(summed)
        (signal, signal_port), (activity, activity_port) = upstream(lifetime)
        (router, note_port), *controls = upstream(core)
        (relay, relay_port), = upstream(router)
        (note, proxy_port), = upstream(relay)
        topology = (relay, router, core, lifetime, summed)
        event = port_info(inputs[0])
        if (value(body, "device_type#391") == 2
                and value(document["metadata"], "device_type") == "note_to_audio"
                and [value(proxy, "port#301")["class_id"] for proxy in proxies] == [242, 61]
                and value(output, "port#301")["class_id"] == 242
                and value(value(output, "port#301"), "channel_count#372") == 3
                and all(any(atom is child for child in children) for atom in topology)
                and tuple(atom["class_id"] for atom in topology) == (1435, 350, 1721, 313, 303)
                and tuple(per_voice(atom) for atom in topology) == (False, True, True, True, False)
                and per_voice(output) is False
                and value(lifetime, "gate_is_audio#7235") is True
                and note is proxies[1] and signal is core and activity is core
                and (proxy_port, relay_port, note_port, signal_port, activity_port, audio_port, sum_port)
                == (0, 0, 8, 0, 1, 0, 0)
                and len(controls) == len(inputs) - 1
                and signature(inputs[0]) == ("note_in", 2, 0)
                and value(event, "event_size#8565") == 44
                and value(event, "event_type#8566") == NATIVE_NOTE_EVENT_TYPE
                and [signature(port) for port in outputs] == [("out_lr", 1, 2), ("voice_active", 1, 1)]):
            if level_routes(controls):
                return []
            return [Finding("error", "ABI031", "Harmonic level parameters must be scheduled, polyphonic and individually modulate the matching Nitro inputs.", "artifact")]
    except (PreflightError, AttributeError, KeyError, IndexError, TypeError, ValueError):
        pass
    return [Finding("error", "ABI030", "Native voice routing, lifetime, stereo sum or NoteEvent ABI does not match the 6.1.3 instrument shell.", "artifact")]


def component_connections(component: dict[str, Any], objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        settings = resolve_object(field_by_suffix(component, "settings#6194"), objects)
        return [resolve_object(entry, objects)
                for entry in field_by_suffix(settings, "inport_connections#614").get("value", [])]
    except PreflightError:
        return []


def connection_source(connection: dict[str, Any], objects: list[dict[str, Any]]) -> tuple[dict[str, Any], int]:
    try:
        index = field_by_suffix(connection, "outport_index#249").get("value")
        if type(index) is int:
            return resolve_object(field_by_suffix(connection, "source_component#248"), objects), index
    except PreflightError:
        pass
    return {}, -1


def matches_fields(item: dict[str, Any], **fields: Any) -> bool:
    try:
        return all(field_by_suffix(item, suffix).get("value") == value for suffix, value in fields.items())
    except PreflightError:
        return False


def inspect_note_route(
    document: dict[str, Any], core: dict[str, Any], config: dict[str, Any] | None = None,
    *, drawing_sources: list[dict[str, Any]] | None = None,
    writer: dict[str, Any] | None = None,
) -> list[Finding]:
    """Require the reviewed note-generator shell: one NoteEvent root output.

    The generating core must drive the single note-output proxy, that proxy must
    serialize a note port on the output side, the root proxy input list must
    match the configured class sequence, and the core must be registered in the
    root child list, because ownership alone does not schedule a nested Nitro
    component in Bitwig 6.1.

    The reviewed shell declares an audio and a note root input. A project
    can require the native NoteEvent ABI and a connection from the note input.
    """
    findings: list[Finding] = []
    objects = list(owned_objects(document["body"]))

    try:
        outputs = field_by_suffix(document["body"], "proxy_out_ports#178").get("value", [])
    except PreflightError:
        outputs = []
    output = resolve_object(outputs[0], objects) if len(outputs) == 1 else {}
    port = resolve_object(field_by_suffix(output, "port#301"), objects) if output else {}
    note_wires = component_connections(output, objects)
    source, source_output = connection_source(note_wires[0], objects) if len(note_wires) == 1 else ({}, -1)
    try:
        source_ports = field_by_suffix(core, "nitro_outputs#15757").get("value", [])
        note_port = resolve_object(source_ports[source_output], objects) if 0 <= source_output < len(source_ports) else {}
    except PreflightError:
        note_port = {}
    native_note_thru = (config or {}).get("requireNativeNoteThru", False)
    event_fields = {"kind#8561": 2, "event_size#8565": 44}
    if native_note_thru:
        event_fields.update({"channel_count#8559": 0, "event_type#8566": NATIVE_NOTE_EVENT_TYPE})
    else:
        event_fields.update({"channel_count#8559": 1, "event_type#8566": "NoteEvent"})
    if not (
        len(outputs) == 1
        and output.get("class_id") == 50
        and port.get("class_id") == 61
        and source is core
        and matches_fields(note_port, **event_fields)
    ):
        findings.append(Finding(
            "error", "ABI025",
            "The single root note proxy must read a 44-byte NoteEvent output from the generating Nitro core.",
            "artifact", None,
        ))

    try:
        proxy_inputs = field_by_suffix(document["body"], "proxy_in_ports#177").get("value", [])
    except PreflightError:
        proxy_inputs = []
    proxy_port_classes: list[int | None] = []
    for entry in proxy_inputs:
        proxy = resolve_object(entry, objects)
        try:
            proxy_port_classes.append(resolve_object(field_by_suffix(proxy, "port#301"), objects).get("class_id"))
        except PreflightError:
            proxy_port_classes.append(None)
    expected_proxy_classes = (config or {}).get("requiredProxyInputPortClasses", [])
    if not isinstance(expected_proxy_classes, list):
        findings.append(Finding("error", "ABI026", "requiredProxyInputPortClasses must be a list.", "artifact", None))
    elif proxy_port_classes != expected_proxy_classes:
        findings.append(Finding(
            "error", "ABI026",
            f"The reviewed note shell proxy input classes differ: expected {expected_proxy_classes}, found {proxy_port_classes}.",
            "artifact", None,
        ))

    if native_note_thru:
        try:
            inputs = field_by_suffix(core, "nitro_inputs#15756").get("value", [])
        except PreflightError:
            inputs = []
        core_wires = component_connections(core, objects)
        parent = resolve_object(proxy_inputs[1], objects) if proxy_port_classes == [242, 61] else {}
        routes = [
            connection_source(core_wires[index], objects)
            for index, entry in enumerate(inputs)
            if index < len(core_wires)
            and matches_fields(resolve_object(entry, objects), **{"name#8557": "note_thru", **event_fields})
        ]
        if not parent or len(routes) != 1 or routes[0][0] is not parent or routes[0][1] != 0:
            findings.append(Finding("error", "ABI034",
                "The native NoteEvent thru input must read output 0 of the root note proxy.", "artifact"))

    try:
        device_type = field_by_suffix(document["body"], "device_type#391").get("value")
    except PreflightError:
        device_type = None
    metadata_type = document.get("metadata", {}).get("fields", {}).get("device_type", {}).get("value")
    if device_type != 1 or metadata_type != "note_to_note":
        findings.append(Finding(
            "error", "ABI027",
            f"Note generator must serialize as note_to_note/type 1; found metadata={metadata_type!r}, body={device_type!r}.",
            "artifact", None,
        ))

    children = field_by_suffix(document["body"], "child_components#173").get("value", [])
    scheduled = [
        resolve_object(entry, objects)
        for entry in children
        if isinstance(entry, dict)
    ]
    if not any(core is entry for entry in scheduled):
        findings.append(Finding(
            "error", "ABI028",
            "The generating Nitro core is owned but absent from root child_components; its note path will not execute.",
            "artifact", None,
        ))

    drawings = [item for item in objects if item.get("class_id") == 5512]
    if drawing_sources is not None:
        scheduled_drawings = [item for item in scheduled if item.get("class_id") == 5512]
        drawing_ids = sorted(map(id, drawings))
        if sorted(map(id, scheduled_drawings)) != drawing_ids or len(drawings) != len(drawing_sources):
            findings.append(Finding("error", "ABI029", "Every declared note drawing must be scheduled exactly once.", "artifact"))
        try:
            surface_models = sorted(id(resolve_object(field_by_suffix(item, "data_model#6220"), objects))
                                    for item in objects if item.get("class_id") == 5517)
        except PreflightError:
            surface_models = []
        if surface_models != drawing_ids:
            findings.append(Finding("error", "ABI029", "Note surfaces must bind one-to-one to the declared drawing components.", "artifact"))
        drawings = scheduled_drawings
    else:
        drawing_sources = [core] if len(drawings) == 1 else []
        if len(drawings) > 1:
            findings.append(Finding("error", "ABI029", "Multiple note drawings require explicit source declarations.", "artifact"))
    for index, (drawing, producer) in enumerate(zip(drawings, drawing_sources), 1):
        try:
            wires = component_connections(drawing, objects)
            ports = field_by_suffix(producer, "nitro_outputs#15757").get("value", [])
            if (any(producer is item for item in scheduled) and len(wires) == 9 and len(ports) >= 2
                    and all(source is producer and output == lane and
                            matches_fields(resolve_object(ports[lane], objects),
                                           **{"kind#8561": 1, "channel_count#8559": 4})
                            for lane, (source, output) in enumerate(connection_source(w, objects) for w in wires[:2]))
                    and all(field_by_suffix(wire, "source_component#248").get("type") == "NullObject" for wire in wires[2:])):
                continue
        except PreflightError:
            pass
        findings.append(Finding("error", "ABI029",
            f"Note drawing {index} must read the scheduled producer's four-lane audio outputs 0 and 1, followed by seven empty inputs.",
            "artifact"))
    if writer is not None:
        findings.extend(inspect_event_parameter_writer(writer, scheduled, objects))
    return findings


def inspect_event_parameter_writer(
    writer: dict[str, Any], scheduled: list[dict[str, Any]], objects: list[dict[str, Any]],
) -> list[Finding]:
    """Check the native trigger -> Nitro event -> persistent decimal atom route."""
    def fail(message: str) -> list[Finding]:
        return [Finding("error", "ABI032", message, "artifact")]

    try:
        if sum(item is writer for item in scheduled) != 1:
            return fail("The event parameter writer must be scheduled once at the root.")
        inputs, outputs = ([resolve_object(entry, objects) for entry in field_by_suffix(writer, suffix)["value"]]
                           for suffix in ("nitro_inputs#15756", "nitro_outputs#15757"))
        wires = component_connections(writer, objects)
        if len(wires) != len(inputs):
            return fail("The event parameter writer must have one connection slot per input.")
        event_inputs = [(port, wire) for port, wire in zip(inputs, wires)
                        if matches_fields(port, **{"kind#8561": 2})]
        if not event_inputs or not outputs:
            return fail("The event parameter writer needs native trigger inputs and f32 event outputs.")
        for port, wire in event_inputs:
            name = field_by_suffix(port, "name#8557")["value"]
            trigger, index = connection_source(wire, objects)
            if not (matches_fields(port, **{"event_type#8566": "void", "event_size#8565": 0})
                    and trigger.get("class_id") == 239 and index == 0
                    and any(item is trigger for item in scheduled)
                    and matches_fields(trigger, **{"preset_identifier#701": name.upper()})):
                return fail(f"Writer input {name} must read its scheduled native trigger.")
            buttons = [item for item in objects if item.get("class_id") == 1880
                       and resolve_object(field_by_suffix(item, "data_model#6220"), objects) is trigger]
            if len(buttons) != 1 or not matches_fields(buttons[0], **{"is_visible#6309": True, "is_enabled#6310": True}):
                return fail(f"Writer input {name} needs exactly one visible and enabled native trigger button.")

        expected = {}
        for index, port in enumerate(outputs):
            name = field_by_suffix(port, "name#8557")["value"].upper()
            if not matches_fields(port, **{"kind#8561": 2, "event_type#8566": "f32", "event_size#8565": 4}):
                return fail(f"Writer output {name} must be an f32 event.")
            expected[index] = name
        if len(set(expected.values())) != len(expected):
            return fail("Writer outputs must have unique parameter names.")

        seen = set()
        for target in scheduled:
            target_wires = component_connections(target, objects)
            for wire in target_wires:
                source, index = connection_source(wire, objects)
                if source is not writer:
                    continue
                if not (target.get("class_id") == 2037 and len(target_wires) == 1
                        and index in expected and index not in seen
                        and matches_fields(target, **{"preset_identifier#701": expected[index]})):
                    return fail(f"Writer output {index} must feed exactly one matching settable decimal atom.")
                seen.add(index)
        if seen != expected.keys():
            missing = ", ".join(name for index, name in expected.items() if index not in seen)
            return fail(f"Missing scheduled event write targets for {missing}.")
    except (KeyError, AttributeError, TypeError, PreflightError):
        return fail("The event parameter writer has incomplete port or connection metadata.")
    return []


def inspect_note_input_precision(binary: bytes, inputs: list[Any], label: str) -> list[Finding]:
    """Compare persisted audio input precision against the compiled Nitro declarations."""
    try:
        declarations = decompile_nitrobin(binary).children[0]
        component = next(item for item in declarations if getattr(item, "kind", None) == "ComponentDeclaration")
        ports = [item for item in component.children[1] if getattr(item, "kind", None) == "PortDeclaration"
                 and item.children[0].kind.startswith("Input")]
    except (AttributeError, IndexError, StopIteration, TypeError, ValueError):
        return [Finding("error", "ABI033", f"{label}: could not inspect compiled Nitro input precision.", "artifact")]
    if len(ports) != len(inputs):
        return [Finding("error", "ABI033", f"{label}: compiled and persisted input counts differ.", "artifact")]
    findings = []
    for declaration, entry in zip(ports, inputs, strict=True):
        port_type, name = declaration.children[:2]
        if port_type.kind != "InputAudioPortType":
            continue
        dtype = port_type.children[0]
        if dtype is not None and dtype.kind == "VectorType":
            dtype = dtype.children[0]
        double = dtype is not None and dtype.kind == "Float64Type"
        if not matches_fields(entry.get("Object", {}), **{"name#8557": name, "is_double_precision#10071": double}):
            precision = "64-bit" if double else "32-bit"
            findings.append(Finding("error", "ABI033",
                f"{label} input {name}: persisted port must match the compiled {precision} audio declaration.", "artifact"))
    return findings


def inspect_parameter_ports(objects: list[dict[str, Any]]) -> list[Finding]:
    """Check native parameter-to-Nitro connections using stream-order object IDs."""
    findings: list[Finding] = []
    # Parameter atoms have distinct engine port families even though both are
    # serialized through the same graph-connection class. Bitwig 6.1 aborts in
    # OutportConverter when an integer parameter feeds an audio Nitro input.
    known_parameter_kinds = {87: 3, 180: 3, 2037: 1, 239: 2, 289: 1, 394: 3}
    for component_index, component in enumerate(item for item in objects if item.get("class_id") == 1721):
        try:
            component_inputs = field_by_suffix(component, "nitro_inputs#15756").get("value", [])
            component_settings = field_by_suffix(component, "settings#6194").get("value", {})
            component_connections = field_by_suffix(
                component_settings, "inport_connections#614"
            ).get("value", [])
        except PreflightError:
            continue
        for port, connection in zip(component_inputs, component_connections):
            try:
                port_kind = field_by_suffix(port.get("Object", {}), "kind#8561").get("value")
                source = field_by_suffix(
                    connection.get("Object", {}), "source_component#248"
                )
            except PreflightError:
                continue
            source_object = None
            if source.get("type") == "Object" and isinstance(source.get("value"), dict):
                source_object = source["value"]
            elif source.get("type") == "ObjectRef":
                source_index = int(source.get("value", 0))
                if 1 <= source_index <= len(objects):
                    source_object = objects[source_index - 1]
            source_class = source_object.get("class_id") if source_object else None
            expected_kind = known_parameter_kinds.get(source_class)
            if expected_kind is not None and port_kind != expected_kind:
                findings.append(Finding(
                    "error",
                    "ABI010",
                    f"Nitro component {component_index} connects parameter class {source_class} to port kind {port_kind}; expected kind {expected_kind}. Bitwig 6.1 aborts on this audio/value conversion.",
                    "artifact",
                    None,
                ))
    return findings


def inspect_document(
    document: dict[str, Any], config: dict[str, Any], compiled_sources: list[bytes],
    *, compiler_profile: dict[str, Any] | None = None,
) -> tuple[list[Finding], list[Finding], list[str]]:
    findings: list[Finding] = []
    note_profile = config.get("profile") in (NOTE_PROFILE, NOTE_613_PROFILE)
    objects = list(owned_objects(document["body"]))
    for index, item in enumerate(objects, 1):
        for reference in direct_refs(item):
            if reference < 1 or reference > len(objects):
                findings.append(Finding("error", "GRAPH001", f"Object {index} has unresolved reference {reference}.", "artifact", None))
            if item.get("class_id") == 105 and reference >= index:
                findings.append(Finding("error", "GRAPH002", f"Connection object {index} has forward reference {reference}.", "artifact", None))

    cores = [item for item in objects if item.get("class_id") == 1721]
    component_specs = config.get("nitroComponents")
    opaque_host_cores = config.get("requiredOpaqueHostNitroCores", 0)
    expected_core_count = (
        sum(item.get("instances", 0) for item in component_specs)
        if isinstance(component_specs, list)
        else len(compiled_sources) + opaque_host_cores
    )
    if len(cores) != expected_core_count:
        findings.append(Finding("error", "ABI001", f"Expected {expected_core_count} Nitro components, found {len(cores)}.", "artifact", None))
        return findings, [], []

    findings.extend(inspect_parameter_ports(objects))

    def binary_of(component: dict[str, Any]) -> bytes:
        storage = bytes(field_by_suffix(component, "code_storage#10421").get("value", []))
        return storage[1:] if storage[:1] == b"\0" else storage

    def inspect_core(component: dict[str, Any], expected_inputs: int | None,
                     expected_outputs: int | None, maximum_vectors: int | None,
                     label: str, expected_audio_outputs: int | None = None,
                     *, source_binary: bytes | None = None) -> tuple[list[Any], list[Any]]:
        inputs = field_by_suffix(component, "nitro_inputs#15756").get("value", [])
        outputs = field_by_suffix(component, "nitro_outputs#15757").get("value", [])
        audio_input_count = sum(
            field_by_suffix(port.get("Object", {}), "kind#8561").get("value") == 1
            for port in inputs
        )
        if audio_input_count >= 31:
            findings.append(Finding(
                "error", "ABI007",
                f"{label} has {audio_input_count} audio inputs; this Windows 6.1 profile caps them at 30 after Growl's 31-input silent process-stub regression.",
                "artifact", None,
            ))
        if expected_inputs is not None and len(inputs) != expected_inputs:
            findings.append(Finding("error", "ABI002", f"{label}: expected {expected_inputs} Nitro inputs, found {len(inputs)}.", "artifact", None))
        if note_profile and source_binary is not None:
            from compiler_profiles import unpack_ast
            ast = unpack_ast(source_binary, compiler_profile or {})
            findings.extend(inspect_note_input_precision(ast, inputs, label))
        if expected_outputs is not None and len(outputs) != expected_outputs:
            findings.append(Finding("error", "ABI003", f"{label}: expected {expected_outputs} Nitro outputs, found {len(outputs)}.", "artifact", None))
        audio_outputs = [p for p in outputs if field_by_suffix(p.get("Object", {}), "kind#8561").get("value") == 1] if expected_audio_outputs is not None else []
        vectors = [p for p in audio_outputs if field_by_suffix(p.get("Object", {}), "channel_count#8559").get("value", 0) >= 4]
        if expected_audio_outputs is not None and (len(audio_outputs) != expected_audio_outputs or len(audio_outputs) >= 4):
            findings.append(Finding("error", "ABI003", f"{label}: expected {expected_audio_outputs} audio outputs, found {len(audio_outputs)} (limit 3).", "artifact", None))
        auxiliary_count = len(vectors) if expected_audio_outputs is not None else len(outputs) - 1
        if maximum_vectors is not None and auxiliary_count > maximum_vectors:
            findings.append(Finding(
                "error", "ABI004",
                f"{label} declares {auxiliary_count} auxiliary vectors, but the configured Windows limit is {maximum_vectors}.",
                "artifact", None,
            ))
        return inputs, outputs

    core: dict[str, Any] | None = None
    named_cores: dict[str, dict[str, Any]] = {}
    inputs: list[Any] = []
    outputs: list[Any] = []
    if isinstance(component_specs, list):
        if len(compiled_sources) != len(component_specs):
            findings.append(Finding("error", "ABI001", f"Expected {len(component_specs)} compiled source groups, found {len(compiled_sources)}.", "artifact", None))
            return findings, [], []
        for source_binary, specification in zip(compiled_sources, component_specs):
            matches = [component for component in cores if binary_of(component) == source_binary]
            expected_instances = specification.get("instances")
            label = specification.get("componentName", "Nitro component")
            if len(matches) != expected_instances:
                findings.append(Finding("error", "ABI005", f"{label}: expected {expected_instances} embedded instance(s), found {len(matches)}.", "artifact", None))
            if len(matches) == 1:
                named_cores[label] = matches[0]
            for component in matches:
                current_inputs, current_outputs = inspect_core(
                    component,
                    specification.get("expectedInputs"),
                    specification.get("expectedOutputs"),
                    specification.get("maxDrawingVectors"),
                    label,
                    specification.get("expectedAudioOutputs"),
                    source_binary=source_binary,
                )
                if specification.get("isMainAudio") is True and core is None:
                    core, inputs, outputs = component, current_inputs, current_outputs
        if core is None:
            findings.append(Finding("error", "ABI005", "The configured main-audio Nitro component is not embedded.", "artifact", None))
            return findings, [], []
    else:
        binary_matches = [
            item for item in cores
            if compiled_sources and binary_of(item) == compiled_sources[0]
        ]
        core = binary_matches[0] if len(binary_matches) == 1 else cores[0]
        inputs, outputs = inspect_core(
            core,
            config.get("expectedNitroInputs"),
            config.get("expectedNitroOutputs"),
            config.get("maxDrawingVectors"),
            "Nitro component",
            source_binary=compiled_sources[0] if compiled_sources else None,
        )
        if compiled_sources and binary_of(core) != compiled_sources[0]:
            findings.append(Finding("error", "ABI005", "Embedded Nitro AST differs from the exact-version host compilation of its declared source.", "artifact", None))
        if opaque_host_cores:
            opaque = [item for item in cores if item is not core]
            if len(opaque) != opaque_host_cores:
                findings.append(Finding(
                    "error", "ABI018",
                    f"Expected {opaque_host_cores} opaque host voice core(s), found {len(opaque)}.",
                    "artifact", None,
                ))

    settings = field_by_suffix(core, "settings#6194").get("value", {})
    connections = field_by_suffix(settings, "inport_connections#614").get("value", [])
    required_source_class = config.get("requiredAudioInputSourceClassId")
    audio_source: dict[str, Any] | None = None
    if required_source_class is not None and connections:
        first = connections[0].get("Object", {})
        source = field_by_suffix(first, "source_component#248")
        source_index = int(source.get("value", 0)) if source.get("type") == "ObjectRef" else 0
        audio_source = resolve_object(source, objects)
        actual = audio_source.get("class_id") if audio_source is not None else None
        if actual != required_source_class:
            findings.append(Finding(
                "error", "ABI006",
                f"Nitro audio input must come from shell class {required_source_class}; found {actual}. Direct proxy routing caused an engine abort in the Growl shell.",
                "artifact", None,
            ))

    required_delay_source = config.get("requiredDelayCompensationInputSourceClassId")
    if required_delay_source is not None:
        actual: int | None = None
        if audio_source is not None:
            try:
                delay_settings = field_by_suffix(audio_source, "settings#6194").get("value", {})
                delay_connections = field_by_suffix(delay_settings, "inport_connections#614").get("value", [])
                if len(delay_connections) == 1:
                    delay_connection = delay_connections[0].get("Object", {})
                    delay_source = field_by_suffix(delay_connection, "source_component#248")
                    delay_index = int(delay_source.get("value", 0)) if delay_source.get("type") == "ObjectRef" else 0
                    if 1 <= delay_index <= len(objects):
                        actual = objects[delay_index - 1].get("class_id")
            except PreflightError:
                pass
        if actual != required_delay_source:
            findings.append(Finding(
                "error", "ABI008",
                f"Delay compensation must come from embedded audio proxy class {required_delay_source}; found {actual}. The outer audio-source atom is not a valid signal source in the reviewed Bitwig 6.1 shell.",
                "artifact", None,
            ))

    required_proxy_inputs = config.get("requiredProxyInputCount")
    if required_proxy_inputs is not None:
        try:
            proxy_inputs = field_by_suffix(document["body"], "proxy_in_ports#177").get("value", [])
            actual_proxy_inputs: int | None = len(proxy_inputs)
        except PreflightError:
            actual_proxy_inputs = None
        if actual_proxy_inputs != required_proxy_inputs:
            findings.append(Finding(
                "error", "ABI009",
                f"Device contents require {required_proxy_inputs} root proxy inputs, found {actual_proxy_inputs}. Bitwig's audio-effect converter indexes both audio and note entries.",
                "artifact", None,
            ))

    if note_profile:
        drawing_names = config.get("requiredDrawingSources")
        drawing_sources = [named_cores.get(name, {}) for name in drawing_names] if drawing_names is not None else None
        writer_name = config.get("requiredEventParameterWriter")
        writer = named_cores.get(writer_name, {}) if writer_name is not None else None
        findings.extend(inspect_note_route(document, core, config, drawing_sources=drawing_sources, writer=writer))

    if config.get("profile") == INSTRUMENT_EVENT_613_PROFILE:
        findings.extend(inspect_event_instrument_route(document, core, inputs, outputs, connections))

    if config.get("profile") == INSTRUMENT_NATIVE_VOICE_613_PROFILE:
        findings.extend(inspect_native_voice_route(document, core, inputs, outputs,
                                                   config["requiredVoiceLevels"]))

    if config.get("profile") == INSTRUMENT_PROFILE:
        findings.extend(inspect_instrument_audio_route(document, core))
        if len(connections) != len(inputs):
            findings.append(Finding(
                "error", "ABI023",
                f"Main instrument core has {len(inputs)} input ports but {len(connections)} connections; positional ABI must match exactly.",
                "artifact",
            ))
        object_ids = {id(item): index for index, item in enumerate(objects, 1)}
        children = field_by_suffix(document["body"], "child_components#173").get("value", [])
        scheduled = {
            entry.get("ObjectRef", object_ids.get(id(entry.get("Object"))))
            for entry in children
        }
        for item in objects:
            if item.get("class_id") in (1435, 587, 1721) and object_ids[id(item)] not in scheduled:
                findings.append(Finding(
                    "error", "ABI021",
                    f"Instrument processor {object_ids[id(item)]} (class {item['class_id']}) is owned but absent from root child_components; its note/audio path will not execute.",
                    "artifact", None,
                ))
        required_device_type = config.get("requiredDeviceType")
        try:
            actual_device_type = field_by_suffix(document["body"], "device_type#391").get("value")
        except PreflightError:
            actual_device_type = None
        metadata_type = document.get("metadata", {}).get("fields", {}).get("device_type", {}).get("value")
        if actual_device_type != required_device_type or metadata_type != "note_to_audio":
            findings.append(Finding(
                "error", "ABI012",
                f"Instrument must serialize as note_to_audio/type {required_device_type}; found metadata={metadata_type!r}, body={actual_device_type!r}.",
                "artifact", None,
            ))

        # Root proxy order is part of the note-to-audio converter contract.
        try:
            proxy_entries = field_by_suffix(document["body"], "proxy_in_ports#177").get("value", [])
        except PreflightError:
            proxy_entries = []
        proxy_port_classes: list[int | None] = []
        for entry in proxy_entries:
            proxy: dict[str, Any] | None = None
            if isinstance(entry, dict) and isinstance(entry.get("ObjectRef"), int):
                reference = entry["ObjectRef"]
                if 1 <= reference <= len(objects):
                    proxy = objects[reference - 1]
            elif isinstance(entry, dict) and isinstance(entry.get("Object"), dict):
                proxy = entry["Object"]
            try:
                port = field_by_suffix(proxy or {}, "port#301").get("value", {})
                proxy_port_classes.append(port.get("class_id"))
            except PreflightError:
                proxy_port_classes.append(None)
        if proxy_port_classes != [242, 61]:
            findings.append(Finding(
                "error", "ABI013",
                f"Instrument proxy inputs must be audio class 242 followed by note class 61; found {proxy_port_classes}.",
                "artifact", None,
            ))

        try:
            proxy_outputs = field_by_suffix(document["body"], "proxy_out_ports#178").get("value", [])
        except PreflightError:
            proxy_outputs = []
        required_outputs = config.get("requiredAudioOutputCount")
        output_ok = len(proxy_outputs) == required_outputs and len(outputs) == 1
        if output_ok:
            try:
                output_info = outputs[0].get("Object", {})
                output_ok = (
                    field_by_suffix(output_info, "kind#8561").get("value") == 1
                    and field_by_suffix(output_info, "channel_count#8559").get("value") == 2
                )
            except PreflightError:
                output_ok = False
        if not output_ok:
            findings.append(Finding(
                "error", "ABI014",
                "Instrument requires one root output driven by one stereo Nitro audio output.",
                "artifact", None,
            ))

        source_identifiers: dict[str, dict[str, Any]] = {}
        for item in objects:
            if item.get("class_id") != 2000:
                continue
            try:
                identifier = field_by_suffix(item, "preset_identifier#3640").get("value")
            except PreflightError:
                continue
            if isinstance(identifier, str):
                source_identifiers[identifier] = item
        source_polyphony = config.get("requiredVoiceSourcePolyphonic", [])
        for source_index, identifier in enumerate(config.get("requiredVoiceSources", [])):
            source = source_identifiers.get(identifier)
            expected_polyphonic = (
                source_polyphony[source_index]
                if source_index < len(source_polyphony)
                else None
            )
            polyphonic = None
            if source is not None:
                try:
                    settings_value = field_by_suffix(source, "settings#6194").get("value", {})
                    polyphonic = field_by_suffix(settings_value, "is_polyphonic#615").get("value")
                except PreflightError:
                    pass
            if source is None or polyphonic is not expected_polyphonic:
                findings.append(Finding(
                    "error", "ABI015",
                    f"Required voice source {identifier!r} is missing or has unexpected polyphony {polyphonic!r}; expected {expected_polyphonic!r}.",
                    "artifact", None,
                ))

        expected_voice_ports = config.get("requiredVoiceInputPorts", [])
        actual_voice_ports: list[Any] = []
        for port in inputs[:len(expected_voice_ports)]:
            try:
                actual_voice_ports.append(field_by_suffix(port.get("Object", {}), "name#8557").get("value"))
            except PreflightError:
                actual_voice_ports.append(None)
        if actual_voice_ports != expected_voice_ports:
            findings.append(Finding(
                "error", "ABI016",
                f"Main Nitro voice ABI prefix differs: expected {expected_voice_ports}, found {actual_voice_ports}.",
                "artifact", None,
            ))
        expected_channel_count = config.get("requiredVoiceChannelCount")
        actual_channel_counts: list[Any] = []
        for port_index, port in enumerate(inputs[:len(expected_voice_ports)]):
            try:
                info = port.get("Object", {})
                voice_shape = tuple(field_by_suffix(info, suffix).get("value") for suffix in (
                    "kind#8561", "is_input#8562", "is_double_precision#10071", "index#8560"))
            except PreflightError:
                voice_shape = None
            if voice_shape != (1, True, False, port_index):
                findings.append(Finding(
                    "error", "ABI024",
                    f"Voice port {port_index} must be an f32 audio input with matching positional index; found {voice_shape!r}.",
                    "artifact",
                ))
            try:
                actual_channel_counts.append(field_by_suffix(
                    port.get("Object", {}), "channel_count#8559"
                ).get("value"))
            except PreflightError:
                actual_channel_counts.append(None)
        if expected_channel_count is not None and actual_channel_counts != [expected_channel_count] * len(expected_voice_ports):
            findings.append(Finding(
                "error", "ABI020",
                f"Host voice ports must use {expected_channel_count}-lane vectors; found {actual_channel_counts}.",
                "artifact", None,
            ))
        actual_voice_connections: list[list[Any]] = []
        for port_name, connection_entry in zip(actual_voice_ports, connections):
            if len(actual_voice_connections) >= len(expected_voice_ports):
                break
            source_identifier = None
            try:
                source_entry = field_by_suffix(
                    connection_entry.get("Object", {}), "source_component#248"
                )
                source_object = None
                if source_entry.get("type") == "ObjectRef":
                    reference = int(source_entry.get("value", 0))
                    if 1 <= reference <= len(objects):
                        source_object = objects[reference - 1]
                elif source_entry.get("type") == "Object":
                    source_object = source_entry.get("value")
                if (source_object or {}).get("class_id") == 1721 and source_object is not core:
                    output_index = int(field_by_suffix(
                        connection_entry.get("Object", {}), "outport_index#249"
                    ).get("value", 0))
                    source_outputs = field_by_suffix(
                        source_object or {}, "nitro_outputs#15757"
                    ).get("value", [])
                    if 0 <= output_index < len(source_outputs):
                        source_identifier = field_by_suffix(
                            source_outputs[output_index].get("Object", {}), "name#8557"
                        ).get("value")
            except PreflightError:
                pass
            actual_voice_connections.append([port_name, source_identifier])
        if actual_voice_connections != config.get("requiredVoiceConnections"):
            findings.append(Finding(
                "error", "ABI019",
                f"Voice port routing differs: found {actual_voice_connections}.",
                "artifact", None,
            ))

        if config.get("requireMainCoreNonPolyphonic") is True:
            try:
                main_settings = field_by_suffix(core, "settings#6194").get("value", {})
                main_polyphonic = field_by_suffix(main_settings, "is_polyphonic#615").get("value")
            except PreflightError:
                main_polyphonic = None
            if main_polyphonic is not False:
                findings.append(Finding(
                    "error", "ABI017",
                    f"Main instrument Nitro core must be explicitly non-polyphonic; found {main_polyphonic!r}.",
                    "artifact", None,
                ))

    required_delay_samples = config.get("requiredProcessingDelaySamples")
    if required_delay_samples is not None:
        placeholders = [item for item in objects if item.get("class_id") == 1437]
        matching = []
        for placeholder in placeholders:
            try:
                delay = field_by_suffix(placeholder, "delay_in_samples#5331").get("value")
                unit = field_by_suffix(placeholder, "placeholder_delay_unit#5333").get("value")
                flags = [
                    field_by_suffix(placeholder, suffix).get("value")
                    for suffix in ("include_input_latency#6435", "include_output_latency#5334", "include_block_size#7158")
                ]
                if delay == required_delay_samples and unit == 1 and flags == [False, False, False]:
                    matching.append(placeholder)
            except PreflightError:
                continue
        if len(matching) != 1:
            findings.append(Finding(
                "error", "ABI011",
                f"Expected one fixed processing-delay placeholder for {required_delay_samples} samples; found {len(matching)} matching class-1437 components.",
                "artifact", None,
            ))

    shaders = [field_by_suffix(item, "#16316").get("value", "") for item in objects if item.get("class_id") == 5512]
    expected_shaders = config.get("expectedSkSLShaders", 1)
    if len(shaders) != expected_shaders:
        findings.append(Finding("error", "SKSL003", f"Expected {expected_shaders} drawing shader(s), found {len(shaders)}.", "artifact", None))
        return findings, [], []
    shader_findings = [
        finding
        for index, shader in enumerate(shaders)
        for finding in lint_sksl_source(shader, f"embedded SkSL {index}")
    ]
    return findings, shader_findings, shaders


def has_passive_display_lines(item: dict[str, Any], objects: list[dict[str, Any]]) -> bool:
    """Display-coordinate strokes share the enclosing plot's grid rectangle."""
    if item.get("class_id") != 1804:
        return False
    entries = item.get("fields", {}).get("items#6221", {}).get("value", [])
    if not entries:
        return False
    for entry in entries:
        line = resolve_object(entry, objects)
        fields = line.get("fields", {})
        if (line.get("class_id") != 1817 or "layout_settings#6226" in fields
                or any(fields.get(key, {}).get("value") for key in ("click_action#10185", "doubleclick_action#10186"))):
            return False
        points = fields.get("items#6221", {}).get("value", [])
        if len(points) < 2:
            return False
        for child in points:
            point = resolve_object(child, objects)
            props = point.get("fields", {})
            if (point.get("class_id") != 1806 or "items#6221" in props
                    or props.get("is_interactive#6499", {}).get("value") is not False):
                return False
    return True


def is_display_handle(item: dict[str, Any], objects: list[dict[str, Any]]) -> bool:
    """Recognize parameter-backed drag targets positioned by their parent plot."""
    class_id = item.get("class_id")
    if class_id not in (1806, 1807):
        return False
    fields = item.get("fields", {})
    layout = resolve_object(fields.get("layout_settings#6226", {}), objects)
    if (layout.get("class_id") != 1803 or fields.get("items#6221")
            or fields.get("is_interactive#6499", {}).get("value") is not True):
        return False
    if class_id == 1807:
        return resolve_object(fields.get("component#6503", {}), objects).get("class_id") == 289
    axes = [fields.get(f"component_{axis}#{number}", {}) for axis, number in (("x", 6512), ("y", 6513))]
    targets = [resolve_object(axis, objects).get("class_id") for axis in axes]
    return 289 in targets and all(axis.get("type") == "NullObject" or target == 289
                                  for axis, target in zip(axes, targets))


def clickable_stack_label(item: dict[str, Any], objects: list[dict[str, Any]]) -> dict[str, Any]:
    """A passive label can supply the bounds of a clickable stack."""
    if item.get("class_id") != 1683:
        return {}
    layout = resolve_object(field_by_suffix(item, "layout_settings#6226"), objects)
    return layout if layout.get("class_id") == 1803 else {}


def inspect_panel_heights(document: dict[str, Any]) -> list[Finding]:
    """Validate the built chain panels, including nested fixed-grid bounds.

    Maximized/inspector panels are different surfaces and are not checked here.
    This is a policy of the reviewed 6.1 hybrid profile, not a universal format
    limit. Unsupported layouts fail closed rather than claiming coverage.
    """
    findings: list[Finding] = []
    objects = list(owned_objects(document["body"]))

    def error(rule: str, path: str, message: str) -> None:
        findings.append(Finding("error", rule, message, path, None))

    def resolve(entry: dict[str, Any]) -> dict[str, Any] | None:
        if entry.get("type") == "Object" and isinstance(entry.get("value"), dict):
            return entry["value"]
        reference = entry.get("value")
        if entry.get("type") == "ObjectRef" and type(reference) is int and 1 <= reference <= len(objects):
            return objects[reference - 1]
        return None

    def visit(item: dict[str, Any], available: int, path: str, ancestors: set[int], *, root: bool = False) -> None:
        if id(item) in ancestors:
            error("PANEL004", path, "Panel item reference cycle prevents height validation.")
            return
        ancestors = ancestors | {id(item)}
        layout = field_by_suffix(item, "layout_settings#6226")
        if root and layout.get("type") == "NullObject":
            height = available
        else:
            settings = resolve(layout)
            if settings is None or settings.get("class_id") != 1694:
                error("PANEL004", path, "Expected a fixed-grid layout for height validation.")
                return
            y = field_by_suffix(settings, "y#6216").get("value")
            height = field_by_suffix(settings, "height#6218").get("value")
            if type(y) is not int or type(height) is not int or y < 0 or height <= 0 or y + height > available:
                error("PANEL003", path, f"Item y={y!r}, height={height!r} exceeds its parent height {available} or has invalid dimensions.")
                return
        if has_passive_display_lines(item, objects):
            return
        children = item.get("fields", {}).get("items#6221", {}).get("value", [])
        for index, child in enumerate(children):
            entry = {"type": "Object", "value": child["Object"]} if "Object" in child else {"type": "ObjectRef", "value": child.get("ObjectRef")}
            target = resolve(entry)
            child_path = f"{path}/items[{index}]"
            if target is None:
                error("PANEL004", child_path, "Panel item cannot be resolved.")
            elif item.get("class_id") == 1804 and is_display_handle(target, objects):
                continue
            elif item.get("class_id") == 3405 and (stack := clickable_stack_label(target, objects)):
                rows = field_by_suffix(stack, "height#6218").get("value")
                if type(rows) is not int or not 0 < rows <= height:
                    error("PANEL003", child_path, f"Stack label height {rows!r} exceeds its parent height {height} or is invalid.")
            else:
                visit(target, height, child_path, ancestors)

    try:
        panels = field_by_suffix(document["body"], "panels#6213").get("value")
        if not isinstance(panels, list) or not panels:
            raise PreflightError("Expected at least one device-chain panel.")
        for index, entry in enumerate(panels):
            path = f"artifact/panels[{index}]"
            panel = resolve({"type": "Object", "value": entry["Object"]} if "Object" in entry else {"type": "ObjectRef", "value": entry.get("ObjectRef")})
            if panel is None:
                error("PANEL001", path, "Device-chain panel cannot be resolved.")
                continue
            height = field_by_suffix(panel, "height#6233").get("value")
            if type(height) is not int or height != PROFILE_PANEL_HEIGHT:
                error("PANEL002", path, f"Device-chain height must be {PROFILE_PANEL_HEIGHT} grid rows for {SUPPORTED_PROFILE}; found {height!r}. Reflow controls instead of expanding the chain.")
                continue
            root = resolve(field_by_suffix(panel, "root_item#6212"))
            if root is None:
                error("PANEL004", path, "Panel root cannot be resolved.")
            else:
                visit(root, height, path, set(), root=True)
    except (PreflightError, KeyError, TypeError, AttributeError) as exc:
        error("PANEL001", "artifact/panels", f"Incomplete panel structure: {exc}")
    return findings


def visibility_clauses(expression: str) -> list[list[tuple[str, str, Decimal]]] | None:
    """A bounded numeric-comparison subset, never execution of panel code.

    Return disjunctive clauses; None means unknown, [] false, [[]] true.
    Unsupported syntax must never prove two controls mutually exclusive.
    """
    if not isinstance(expression, str):
        return None
    token = re.compile(r"\s*(==|!=|<=|>=|&&|\|\||[!<>()]|[A-Za-z_][A-Za-z_0-9]*|[+-]?(?:\d+(?:\.\d*)?|\.\d+))")
    tokens = []
    position = 0
    expression = expression.strip()
    while position < len(expression):
        match = token.match(expression, position)
        if not match or len(tokens) >= 128:
            return None
        tokens.append(match[1])
        position = match.end()
    cursor = 0

    def take():
        nonlocal cursor
        value = tokens[cursor]
        cursor += 1
        return value

    def primary():
        value = take()
        if value == "!":
            return ("not", primary())
        if value == "(":
            node = either()
            if take() != ")":
                raise ValueError("unclosed condition")
            return node
        if value in ("true", "false", "0", "1"):
            return ("constant", value in ("true", "1"))
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", value):
            raise ValueError("unsupported operand")
        operator = take()
        if operator not in ("==", "!=", "<", "<=", ">", ">="):
            raise ValueError("unsupported comparison")
        number = take()
        if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", number):
            raise ValueError("nonconstant comparison")
        return ("compare", value, operator, Decimal(number))

    def both():
        nonlocal cursor
        node = primary()
        while cursor < len(tokens) and tokens[cursor] == "&&":
            cursor += 1
            node = ("and", node, primary())
        return node

    def either():
        nonlocal cursor
        node = both()
        while cursor < len(tokens) and tokens[cursor] == "||":
            cursor += 1
            node = ("or", node, both())
        return node

    def clauses(node, negate=False):
        kind = node[0]
        if kind == "constant":
            return [[]] if node[1] != negate else []
        if kind == "not":
            return clauses(node[1], not negate)
        if kind == "compare":
            operator = node[2]
            if negate:
                operator = {"==": "!=", "!=": "==", "<": ">=", "<=": ">", ">": "<=", ">=": "<"}[operator]
            return [[(node[1], operator, node[3])]]
        left, right = clauses(node[1], negate), clauses(node[2], negate)
        conjunction = (kind == "and") != negate
        if (len(left) * len(right) if conjunction else len(left) + len(right)) > 64:
            raise ValueError("condition expansion limit")
        return [a + b for a in left for b in right] if conjunction else left + right

    try:
        tree = either()
        return clauses(tree) if cursor == len(tokens) else None
    except (IndexError, ValueError, RecursionError):
        return None


def visibility_can_coexist(conditions: list[str]) -> bool:
    """False only when the supported conditions prove a contradiction."""
    alternatives: list[list[tuple[str, str, Decimal]]] = [[]]
    for condition in conditions:
        parsed = visibility_clauses(condition)
        if parsed is None:
            continue  # Treat unknown conditions as potentially true.
        if len(alternatives) * len(parsed) > 64:
            return True
        alternatives = [a + b for a in alternatives for b in parsed]
    for clause in alternatives:
        possible = True
        for variable in {atom[0] for atom in clause}:
            lower, upper, excluded = None, None, set()
            for name, operator, number in clause:
                if name != variable:
                    continue
                if operator == "!=":
                    excluded.add(number)
                if operator in ("==", ">", ">="):
                    bound = (number, operator != ">")
                    if lower is None or number > lower[0]:
                        lower = bound
                    elif number == lower[0]:
                        lower = (number, lower[1] and bound[1])
                if operator in ("==", "<", "<="):
                    bound = (number, operator != "<")
                    if upper is None or number < upper[0]:
                        upper = bound
                    elif number == upper[0]:
                        upper = (number, upper[1] and bound[1])
            if lower is not None and upper is not None:
                if lower[0] > upper[0] or (lower[0] == upper[0] and
                        (not lower[1] or not upper[1] or lower[0] in excluded)):
                    possible = False
                    break
        if possible:
            return True
    return False


def inspect_panel_overlaps(document: dict[str, Any]) -> list[Finding]:
    """Warn about native item collisions in the built fixed-grid chain panels.

    Bounds describe layout/hit rectangles, not rendered glyphs. Shader surfaces
    (5517), plain container backgrounds and ancestor/descendant pairs are
    intentional layers. Interactive containers still collide with other branches.
    """
    findings: list[Finding] = []
    objects = list(owned_objects(document["body"]))

    def field(item, suffix, default=None):
        return next((v.get("value") for k, v in item.get("fields", {}).items()
                     if k.endswith(suffix)), default)

    def resolve(entry):
        if entry.get("type") == "Object":
            return entry.get("value")
        if "Object" in entry:
            return entry["Object"]
        index = entry.get("ObjectRef", entry.get("value"))
        if type(index) is int and 1 <= index <= len(objects):
            return objects[index - 1]
        return None

    def warning(rule, path, message):
        findings.append(Finding("warning", rule, message, path, None))

    for index, entry in enumerate(field(document["body"], "panels#6213", []) or []):
        path = f"artifact/panels[{index}]"
        panel = resolve(entry)
        if panel is None:
            warning("PANEL006", path, "Overlap coverage incomplete: unresolved panel.")
            continue
        visibility: dict[int, list[str]] = {}
        for expression in field(panel, "expressions#6277", []) or []:
            atom = resolve(expression)
            if atom is None:
                warning("PANEL006", path, "Overlap coverage incomplete: unresolved expression.")
                continue
            if field(atom, "attribute_id#6316") != 6309:
                continue  # Enabled/brightness expressions do not hide the item.
            target = resolve(atom.get("fields", {}).get("panel_item#6275", {}))
            if target is None:
                warning("PANEL006", path, "Overlap coverage incomplete: unresolved visibility target.")
            else:
                visibility.setdefault(id(target), []).append(field(atom, "expression#6274", ""))
        rectangles = []

        def item_conditions(item, inherited):
            expressions = visibility.get(id(item), [])
            # A visibility expression overrides the saved/default boolean.
            if expressions:
                # Multiple writers have unknown precedence: never combine them
                # into a false proof of invisibility.
                return inherited + (expressions if len(expressions) == 1 else ["unknown()"])
            if field(item, "is_visible#6309", True) is False:
                return None
            return inherited

        def visit(item, x, y, item_path, ancestors, conditions, root=False):
            if item is None or id(item) in ancestors:
                warning("PANEL006", item_path, "Overlap coverage incomplete: unresolved or cyclic item.")
                return
            conditions = item_conditions(item, conditions)
            if conditions is None:
                return
            layout = item.get("fields", {}).get("layout_settings#6226", {})
            settings = resolve(layout)
            if root and layout.get("type") == "NullObject":
                bounds = None
            elif settings is not None and settings.get("class_id") == 1694:
                values = [field(settings, suffix) for suffix in ("x#6215", "y#6216", "width#6217", "height#6218")]
                if not all(type(v) is int for v in values) or values[2] <= 0 or values[3] <= 0:
                    warning("PANEL006", item_path, "Overlap coverage incomplete: invalid fixed-grid rectangle.")
                    return
                x, y = x + values[0], y + values[1]
                bounds = (x, y, values[2], values[3])
            else:
                warning("PANEL006", item_path, "Overlap coverage incomplete: unsupported layout.")
                return
            children = field(item, "items#6221", []) or []
            container = item.get("class_id") in (1681, 3405) or bool(children)
            interactive = any(field(item, suffix, "") for suffix in ("click_action#10185", "doubleclick_action#10186"))
            if not has_passive_display_lines(item, objects):
                for i, child in enumerate(children):
                    target = resolve(child)
                    if target is not None and item.get("class_id") == 1804 and is_display_handle(target, objects):
                        interactive = True
                        continue
                    if target is not None and item.get("class_id") == 3405 and clickable_stack_label(target, objects):
                        continue
                    visit(target, x, y, f"{item_path}/items[{i}]", ancestors | {id(item)}, conditions)
            if bounds and item.get("class_id") != 5517 and (not container or interactive):
                name = next((field(item, suffix) for suffix in ("text#6219", "tooltip_text#6867", "title#6241") if field(item, suffix)), f"class {item.get('class_id')}")
                rectangles.append((item_path, str(name), bounds, ancestors, conditions))
            item_ids[item_path] = id(item)

        item_ids: dict[str, int] = {}
        root = resolve(panel.get("fields", {}).get("root_item#6212", {}))
        panel_conditions = item_conditions(panel, [])
        if panel_conditions is not None:
            visit(root, 0, 0, path, set(), panel_conditions, root=True)
        for i, (a_path, a_name, a, a_ancestors, a_conditions) in enumerate(rectangles):
            for b_path, b_name, b, b_ancestors, b_conditions in rectangles[i + 1:]:
                if item_ids[a_path] in b_ancestors or item_ids[b_path] in a_ancestors:
                    continue
                width = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
                height = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
                if width <= 0 or height <= 0 or not visibility_can_coexist(a_conditions + b_conditions):
                    continue
                uncertain = any(visibility_clauses(c) is None for c in a_conditions + b_conditions)
                warning("PANEL005", a_path,
                        f"Native item {a_name!r} {a} overlaps {b_name!r} {b} at {b_path} by {width} x {height} grid cells. "
                        + ("Visibility is unresolved; assume both can be visible. " if uncertain else "Both can be visible together. ")
                        + "Review layout or document intentional layering; touching edges are allowed.")
    return findings


def multi_core_findings(paths: list[Path], project_file: Path) -> list[Finding]:
    """Warn when a device embeds more than one distinct Nitro source.

    Bitwig analyses a device's Nitro atoms on a parallel pool. A cold parallel
    analysis of a three-core Prism failed with "Recursion in semantic analysis"
    and crashed the 6.1.3 audio engine (2026-11-10). Identical sources share
    one analysis and are not counted twice.
    """
    distinct = {path.read_bytes() for path in paths}
    if len(distinct) <= 1:
        return []
    return [Finding(
        "warning", "NITRO024",
        f"{len(distinct)} distinct Nitro sources are compiled concurrently by the host. Overlapping "
        "compilations on a cold compiler (first compile of a session) can fail with 'Recursion in semantic "
        "analysis' and crash the engine; warm the compiler with another custom device first during the host "
        "test (BITWIG-TESTING.md, cold Nitro compiler gate), or merge the components.",
        str(project_file), None,
    )]


def semantic_check(
    paths: list[Path], compiler_inputs: dict[str, Any], scratch: Path, *, verify_llvm: bool = False,
    llvm_output: Path | None = None,
) -> tuple[Check, list[Finding]]:
    """Run Bitwig's own Nitro semantic analysis on each source, one at a time.

    The parser stage proves syntax only. This stage catches undeclared names,
    missing members and type errors. The supported Windows profiles can also
    verify the emitted LLVM IR. Analysis stays serial (see NITRO024).
    """
    name = "Exact-version Nitro semantic analysis"
    if verify_llvm:
        name += " and LLVM IR"
        try:
            from llvmlite import binding as llvm
        except ImportError:
            setup_script = "scripts/setup-preflight.ps1" if os.name == "nt" else "scripts/setup-preflight.sh"
            detail = f"LLVM verifier unavailable. Run {setup_script}"
            return Check(name, "fail", detail), [Finding("error", "NITRO029", detail, "toolchain", None)]

    def unavailable(detail: str) -> tuple[Check, list[Finding]]:
        severity, rule, status = ("error", "NITRO029", "fail") if verify_llvm else ("warning", "NITRO026", "skip")
        return Check(name, status, detail), [Finding(severity, rule, detail, "toolchain", None)]

    if compiler_inputs["profileId"] not in ("windows-x86_64-bitwig-6.1.3", "windows-x86_64-bitwig-6.1.7"):
        return unavailable(f"no semantic probe for {compiler_inputs['profileId']}")

    javac = shutil.which(compiler_inputs.get("javac") or "javac")
    if javac is None:
        return unavailable("javac is unavailable, so the semantic probe could not be compiled")
    install = Path(compiler_inputs["libs"]).resolve().parent.parent
    source = ROOT / "tools/host_compiler/NitroSemanticProbe.java"
    findings = []
    verified = 0
    with tempfile.TemporaryDirectory(prefix="nitro-semantic-", dir=scratch) as classes:
        ir_directory = llvm_output if llvm_output is not None else Path(classes)
        classpath = os.pathsep.join((classes, compiler_inputs["jar"], compiler_inputs["libs"]))
        properties = [f"-DinstallationRoot={install}"]
        if compiler_inputs.get("profileId") == "windows-x86_64-bitwig-6.1.7":
            properties.append("-Dnitro.frontend=windows-6.1.7")
        if verify_llvm:
            properties.append(f"-Dnitro.llvm.output={ir_directory}")
        try:
            run([javac, "--release", "17", "-d", classes, str(source)], timeout=60)
            output = run([compiler_inputs["java"], *properties, "-cp", classpath,
                          "NitroSemanticProbe", *map(str, paths)], timeout=300).stdout
        except PreflightError as exc:
            return unavailable(f"semantic probe could not run: {str(exc).splitlines()[-1][:300]}")
        results = [line.split("\t", 2) for line in output.splitlines() if line.startswith(("OK\t", "ERROR\t"))]
        if verify_llvm and len(results) == len(paths):
            for index, fields in enumerate(results):
                if fields[0] != "OK":
                    continue
                try:
                    ir = (ir_directory / f"{index}.ll").read_text(encoding="utf-8")
                    with llvm.parse_assembly(ir) as module:
                        if not any(not function.is_declaration for function in module.functions):
                            raise ValueError("LLVM module contains no function definitions")
                        module.verify()
                except (OSError, ValueError) as exc:
                    findings.append(Finding("error", "NITRO029", f"Could not read emitted LLVM IR: {exc}", fields[1], None))
                except RuntimeError as exc:
                    diagnostic = " ".join(str(exc).splitlines()[:2])
                    findings.append(Finding("error", "NITRO028", f"LLVM rejected emitted IR: {diagnostic}", fields[1], None))
                else:
                    verified += 1
    for fields in results:
        if fields[0] != "ERROR":
            continue
        message = fields[2] if len(fields) > 2 else "unknown error"
        if "Static Assertion Failed" in message:
            # The probe does not model host context such as the connected
            # channel count; Growl's EC.channels == 2 and Orbit's assertions
            # fail offline although both load in Bitwig.
            findings.append(Finding(
                "warning", "NITRO027",
                f"static_assert failed offline; it depends on host context the probe does not model: {message}",
                fields[1], None,
            ))
            continue
        findings.append(Finding(
            "error", "NITRO025", f"Bitwig semantic analysis rejected the source: {message}", fields[1], None,
        ))
    if len(results) != len(paths):
        check, incomplete = unavailable(f"semantic probe reported {len(results)} of {len(paths)} source(s)")
        return check, findings + incomplete
    errors = [item for item in findings if item.severity == "error"]
    detail = f"analysed {len(paths)} source(s) serially with the pinned Bitwig front end"
    if verify_llvm:
        detail += f", verified LLVM IR for {verified} source(s)"
    return Check(name, "fail" if errors else "warning" if findings else "pass",
                 detail), findings


def compile_nitro(source: Path, output: Path, install: Path, bridge: Path) -> bytes:
    java = install / "jre/bin/java.exe"
    jar = install / "bin/bitwig.jar"
    libs = install / "bin/libs.jar"
    required = (java, jar, libs, bridge / "NitroBridge.class")
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise PreflightError("missing Nitro compiler inputs: " + ", ".join(missing))
    run([
        str(java), "-cp", os.pathsep.join(map(str, (bridge, jar, libs))),
        "NitroBridge", "text-to-binary", str(source), str(output),
    ], timeout=45)
    payload = output.read_bytes()
    if serialize_nitrobin(decompile_nitrobin(payload)) != payload:
        raise PreflightError(f"Nitro AST roundtrip changed bytes for {source}")
    return payload


def build_command(project: dict[str, Any], workspace: Path, output: Path) -> list[str]:
    adapter = project.get("adapter", {})
    if adapter.get("type") != "external-builder" or not adapter.get("argv"):
        raise PreflightError("preflight requires an external-builder adapter in bitwig-project.json")
    replacements = {"{workspace}": str(workspace), "{output}": str(output)}
    command = []
    for token in adapter["argv"]:
        for old, new in replacements.items():
            token = token.replace(old, new)
        command.append(token)
    if command[0].lower() in {"python", "python.exe", "py"}:
        command[0] = sys.executable
    return command


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError(f"cannot read {path}: {exc}") from exc


def skia_check(shader: str) -> Check:
    if importlib.util.find_spec("skia") is None:
        setup_script = "scripts/setup-preflight.ps1" if os.name == "nt" else "scripts/setup-preflight.sh"
        return Check("SkSL runtime compiler", "fail", f"skia-python is unavailable; run {setup_script}")
    import skia  # type: ignore
    try:
        skia.RuntimeEffect.MakeForShader("uniform float3 iResolution;\n" + shader)
    except Exception as exc:  # pragma: no cover - optional dependency
        return Check("SkSL runtime compiler", "fail", str(exc))
    return Check("SkSL runtime compiler", "pass", "embedded shader compiled with local Skia RuntimeEffect")


def filar_regression_check(project: dict, device: dict, first: Path, second: Path,
                           parser: Path, compiler_inputs: dict, compiled: list[bytes]) -> tuple[Check, list[Finding]]:
    import unittest
    from tests.test_filar import artifact_suite

    name = "FILAR Bitwig regressions"
    first_preset, second_preset = (path.with_suffix(".bwpreset") for path in (first, second))
    try:
        if first_preset.read_bytes() != second_preset.read_bytes():
            raise PreflightError("Two clean FILAR builds produced different Init presets")
        preset = json.loads(run([str(parser), "to-json", str(first_preset)]).stdout)
        run([str(parser), "roundtrip", str(first_preset)])
        dice = dict(zip(project["sources"]["nitro"], compiled, strict=True))["dice.nitro"]
    except (OSError, ValueError, KeyError, PreflightError) as exc:
        detail = f"Cannot verify the FILAR Init and compiled inputs: {exc}"
        return Check(name, "fail", detail), [Finding("error", "FILAR001", detail, str(first_preset))]

    def evaluate(cases: list[tuple[dict, str]]) -> list[str]:
        javac = shutil.which(compiler_inputs.get("javac") or "javac")
        if javac is None:
            raise PreflightError("FILAR needs javac for its panel expression probe. "
                                 "Rebuild the local compiler inputs with --javac or put javac on PATH.")
        source = ROOT / "tools/host_compiler/PanelExpressionProbe.java"
        with tempfile.TemporaryDirectory(prefix="filar-panel-", dir=first.parent) as classes:
            run([javac, "--release", "17", "-d", classes, str(source)], timeout=30)
            payload = "\n".join("\t".join([*(f"{key}={val}" for key, val in controls.items()), expression])
                                for controls, expression in cases)
            classpath = os.pathsep.join((classes, compiler_inputs["jar"], compiler_inputs["libs"]))
            return run([compiler_inputs["java"], "-cp", classpath, "PanelExpressionProbe"],
                       timeout=30, input_text=payload).stdout.splitlines()

    result = unittest.TestResult()
    try:
        artifact_suite(project, device, preset, dice, evaluate).run(result)
    except (KeyError, ValueError, IndexError, TypeError) as exc:
        detail = f"Cannot read the FILAR panel from the decoded device: {exc}"
        return Check(name, "fail", detail), [Finding("error", "FILAR002", detail, str(first))]
    failures = [Finding("error", "FILAR002", f"{test.id().rsplit('.', 1)[-1]}: {detail.strip().splitlines()[-1][:600]}",
                        str(first)) for test, detail in result.failures + result.errors]
    return Check(name, "fail" if failures else "pass",
                 f"{result.testsRun} artifact checks for Output, Init, spectrum, sonogram and Dice"), failures


def preflight(workspace: Path, report_path: Path | None = None, *, compiler_inputs: Path | None = None,
              project_file: Path | None = None) -> dict[str, Any]:
    workspace = workspace.resolve()
    project_file = (project_file or workspace / "bitwig-project.json").resolve()
    if project_file.parent != workspace:
        raise PreflightError("project file must be in the device workspace")
    import project_contracts
    try:
        project = project_contracts.load(project_file)  # resolves platform overlays
    except (OSError, ValueError) as exc:
        raise PreflightError(f"cannot read {project_file}: {exc}") from exc
    is_filar = project.get("projectId") == "polarity.filar"
    config = project.get("preflight")
    if not isinstance(config, dict) or config.get("profile") not in SUPPORTED_PROFILES:
        raise PreflightError(
            f"bitwig-project.json must declare one of preflight.profile={SUPPORTED_PROFILES!r}"
        )
    if is_filar and config["profile"] != INSTRUMENT_NATIVE_VOICE_613_PROFILE:
        raise PreflightError("Production FILAR requires the Windows 6.1.3 native voice profile")

    checks: list[Check] = []
    findings: list[Finding] = validate_profile_config(config, str(project_file))
    checks.append(Check("Version-bound profile", "pass" if not findings else "fail", f"{len(findings)} finding(s)"))
    compiler = None
    compiler_profile = None
    document_parser = DEFAULT_PARSER
    if config["profile"] in (EFFECT_613_PROFILE, NOTE_613_PROFILE, INSTRUMENT_EVENT_613_PROFILE,
                             INSTRUMENT_NATIVE_VOICE_613_PROFILE):
        if not compiler_inputs:
            raise PreflightError("Current document profiles require matching project target and explicit --compiler-inputs")
        if config.get("audioInputRoute") == "normalizer-direct-audio-proxy" and project.get("projectId") != "polarity.normalizer":
            raise PreflightError("The separate direct-proxy evidence is bound to the Normalizer reference")
        import compiler_profiles as compiler
        compiler_config, compiler_profile = compiler.validate_inputs(compiler_inputs)
        compiler.require_target(compiler_profile, project.get("target", {}))
        if (config["profile"] == INSTRUMENT_NATIVE_VOICE_613_PROFILE
                and (compiler_profile["version"], compiler_profile["platform"]) != ("6.1.3", "windows-x86_64")):
            raise PreflightError("Native voice instrument profile requires Windows Bitwig 6.1.3")
        document_parser, parser_sha256 = compiler.document_parser(compiler_config, compiler_profile)
    else:
        if compiler_inputs:
            raise PreflightError("Compiler override is only supported by the current document profiles")
        require_hash(DEFAULT_INSTALL / "bin/bitwig.jar", PROFILE_JAR_SHA256, "Bitwig 6.1 JAR")
        require_hash(DEFAULT_BRIDGE / "NitroBridge.class", PROFILE_BRIDGE_SHA256, "NitroBridge class")
        parser_sha256 = PROFILE_PARSER_SHA256
    require_hash(document_parser, parser_sha256, "document parser")
    checks.append(Check("Toolchain identity", "pass", f"Bitwig JAR, NitroBridge and document parser hashes match {config['profile']}"))
    sources = project.get("sources", {})
    nitro_paths = [(workspace / relative).resolve() for relative in sources.get("nitro", [])]
    if not nitro_paths:
        raise PreflightError("project declares no Nitro source")
    source_findings: list[Finding] = []
    component_configs = {specification["source"]: specification for specification in config.get("nitroComponents") or ()}
    for relative, path in zip(sources["nitro"], nitro_paths, strict=True):
        if not path.is_file():
            raise PreflightError(f"Nitro source is missing: {path}")
        specification = component_configs.get(relative, {})
        lint_config = config | {"recallSafety": specification.get("recallSafety", config.get("recallSafety"))}
        source_findings.extend(lint_nitro_source(path, lint_config))
    source_findings.extend(multi_core_findings(nitro_paths, project_file))
    findings.extend(source_findings)
    source_errors = [item for item in source_findings if item.severity == "error"]
    checks.append(Check("Nitro source lint", "fail" if source_errors else "warning" if source_findings else "pass", f"{len(nitro_paths)} source file(s), {len(source_findings)} finding(s)"))

    output_root = ROOT / "build/preflight" / re.sub(r"[^A-Za-z0-9_.-]+", "-", project["projectId"])
    if project_file.name != "bitwig-project.json":
        output_root /= project_file.stem.removeprefix("bitwig-project.")
    output_root.mkdir(parents=True, exist_ok=True)
    compiled: list[bytes] = []
    if not any(item.severity == "error" for item in findings):
        for index, path in enumerate(nitro_paths):
            ast = output_root / f"source-{index}.nitrobin"
            if compiler:
                compiler.compile_source(path, ast, compiler_inputs)
                compiled.append(ast.read_bytes())
            else:
                compiled.append(compile_nitro(path, ast, DEFAULT_INSTALL, DEFAULT_BRIDGE))
        checks.append(Check("Exact-version Nitro parser", "pass", f"compiled and byte-roundtripped {len(compiled)} source file(s)"))
        if compiler:
            semantic, semantic_findings = semantic_check(
                nitro_paths, compiler_config, output_root,
                verify_llvm=compiler_config["profileId"] in ("windows-x86_64-bitwig-6.1.3", "windows-x86_64-bitwig-6.1.7"),
            )
            checks.append(semantic)
            findings.extend(semantic_findings)
            if any(item.severity == "error" for item in semantic_findings):
                compiled = []
        else:
            checks.append(Check("Exact-version Nitro semantic analysis", "skip", "legacy profile without compiler inputs"))
    else:
        checks.append(Check("Exact-version Nitro parser", "skip", "source lint failed"))
        checks.append(Check("Exact-version Nitro semantic analysis", "skip", "source lint failed"))

    first = output_root / "run-a.bwdevice"
    second = output_root / "run-b.bwdevice"
    if compiled:
        if is_filar:
            for artifact in (first, second):
                artifact.with_suffix(".bwpreset").unlink(missing_ok=True)
        extra_args = ["--compiler-inputs", str(compiler_inputs.resolve())] if compiler_inputs else []
        project_args = ["--project-file", str(project_file)] if project_file.name != "bitwig-project.json" else []
        run(build_command(project, workspace, first) + extra_args + project_args, timeout=180)
        run(build_command(project, workspace, second) + extra_args + project_args, timeout=180)
        if first.read_bytes() != second.read_bytes():
            findings.append(Finding("error", "BUILD001", "Two clean adapter builds produced different bytes.", str(workspace), None))
            checks.append(Check("Deterministic adapter build", "fail", "run-a and run-b differ"))
        else:
            checks.append(Check("Deterministic adapter build", "pass", f"SHA-256 {sha256(first)}"))

        if not document_parser.is_file():
            raise PreflightError(f"document parser is missing: {document_parser}")
        parsed = json.loads(run([str(document_parser), "to-json", str(first)]).stdout)
        run([str(document_parser), "roundtrip", str(first)])
        with tempfile.TemporaryDirectory(prefix="device-preflight-", dir=output_root) as temporary:
            json_path = Path(temporary) / "document.json"
            rebuilt = Path(temporary) / "document.bwdevice"
            json_path.write_text(json.dumps(parsed, ensure_ascii=False), encoding="utf-8")
            run([str(document_parser), "from-json", str(json_path), str(rebuilt)])
            if rebuilt.read_bytes() != first.read_bytes():
                findings.append(Finding("error", "DOC001", "Document to-json/from-json no-op changed bytes.", str(first), None))
        checks.append(Check("Document roundtrip", "pass" if not any(item.rule == "DOC001" for item in findings) else "fail", "parser roundtrip and JSON no-op completed"))

        panel_findings = inspect_panel_heights(parsed)
        findings.extend(panel_findings)
        checks.append(Check("Device-chain panel height", "pass" if not panel_findings else "fail",
                            f"{PROFILE_PANEL_HEIGHT} grid rows; nested item bounds; {len(panel_findings)} finding(s)"))
        overlap_findings = inspect_panel_overlaps(parsed)
        findings.extend(overlap_findings)
        checks.append(Check("Native panel overlaps", "warning" if overlap_findings else "pass",
                            f"{len(overlap_findings)} warning(s); fixed-grid native bounds, visibility conditions; shader surfaces excluded"))
        binding_findings = inspect_indexed_controls(parsed)
        findings.extend(binding_findings)
        checks.append(Check("Indexed native control bindings", "fail" if binding_findings else "pass",
                            f"{len(binding_findings)} finding(s); reviewed indexed widget/attribute/icon contracts"))
        for name, extra in (
            ("Artifact identity", inspect_artifact_identity(parsed, project)),
            ("Decimal native control bindings", inspect_decimal_controls(parsed)),
            ("Native chooser titles", inspect_chooser_titles(parsed)),
            ("Control coverage and paint ownership", inspect_control_contract(parsed, config.get("controlContract"))),
        ):
            findings.extend(extra)
            status = "fail" if any(f.severity == "error" for f in extra) else "warning" if extra else "pass"
            checks.append(Check(name, status, f"{len(extra)} finding(s); does not prove mouse interaction"))
        graph_findings, shader_findings, shaders = inspect_document(
            parsed, config, compiled, compiler_profile=compiler_profile,
        )
        findings.extend(graph_findings)
        findings.extend(shader_findings)
        checks.append(Check("Graph, port and shell ABI", "pass" if not graph_findings else "fail", f"{len(graph_findings)} finding(s)"))
        checks.append(Check("Embedded SkSL lint", "pass" if not shader_findings else "fail", f"{len(shader_findings)} finding(s)"))
        import document_lints
        common = [Finding(item.severity, item.rule, item.message, "artifact", None)
                  for item in document_lints.lint_document(parsed)]
        findings.extend(common)
        checks.append(Check(
            "Host-regression document lints",
            "fail" if any(item.severity == "error" for item in common) else "warning" if common else "pass",
            f"{len(common)} finding(s); document version, graph, ports and panel contracts",
        ))
        if shaders:
            shader_results = [skia_check(shader) for shader in shaders]
            failures = [item.detail for item in shader_results if item.status == "fail"]
            shader_compile = Check(
                "SkSL runtime compiler",
                "fail" if failures else "pass",
                failures[0] if failures else f"compiled {len(shaders)} embedded shader(s) with local Skia RuntimeEffect",
            )
        else:
            shader_compile = Check("SkSL runtime compiler", "skip", "no valid shader extracted")
        checks.append(shader_compile)
        if shader_compile.status == "fail":
            findings.append(Finding("error", "SKSL004", shader_compile.detail, "embedded SkSL", None))
        if is_filar:
            check, regressions = filar_regression_check(project, parsed, first, second, document_parser,
                                                        compiler_config, compiled)
            checks.append(check)
            findings.extend(regressions)
    else:
        checks.extend([
            Check("Deterministic adapter build", "skip", "Nitro checks failed"),
            Check("Document roundtrip", "skip", "Nitro checks failed"),
            Check("Device-chain panel height", "skip", "Nitro checks failed"),
            Check("Native panel overlaps", "skip", "Nitro checks failed"),
            Check("Indexed native control bindings", "skip", "Nitro checks failed"),
            Check("Decimal native control bindings", "skip", "Nitro checks failed"),
            Check("Native chooser titles", "skip", "Nitro checks failed"),
            Check("Artifact identity", "skip", "Nitro checks failed"),
            Check("Control coverage and paint ownership", "skip", "Nitro checks failed"),
            Check("Graph, port and shell ABI", "skip", "Nitro checks failed"),
            Check("Embedded SkSL lint", "skip", "Nitro checks failed"),
            Check("Host-regression document lints", "skip", "Nitro checks failed"),
            Check("SkSL runtime compiler", "skip", "Nitro checks failed"),
        ])
        if is_filar:
            checks.append(Check("FILAR Bitwig regressions", "skip", "Nitro checks failed"))

    errors = [item for item in findings if item.severity == "error"]
    host_evidence = [
        "Nitro semantic/JIT execution",
        "loader and audio-engine acceptance",
        "audible wet DSP",
        "GUI behavior",
        "non-zero stateful cold recall",
    ]
    if is_filar:
        host_evidence.extend([
            "FILAR browser insertion with the deployed Init, native dB popup and spectrum paint",
            "FILAR first Dice click in silence, live draw distribution and audio-driven sonogram",
            "FILAR partial Attack and Decay response on played notes",
        ])
    result = {
        "schema": 1,
        "phase": "offline-artifact",
        "installationStatus": "not-checked",
        "hostAcceptanceStatus": "not-checked",
        "identity": project.get("identity"),
        "target": project.get("target"),
        "libraryPath": project.get("output", {}).get("libraryPath"),
        "controlContract": config.get("controlContract"),
        "projectId": project["projectId"],
        "profile": config["profile"],
        "status": "pass" if not errors else "fail",
        "decision": "ready-for-controlled-host-test" if not errors else "blocked-before-host-test",
        "checks": [asdict(item) for item in checks],
        "findings": [asdict(item) for item in findings],
        "artifact": str(first) if first.is_file() else None,
        "artifactSha256": sha256(first) if first.is_file() else None,
        "hostEvidenceStillRequired": host_evidence,
    }
    report_path = report_path or output_root / "report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path, help="device workspace containing bitwig-project.json")
    parser.add_argument("--report", type=Path, help="optional JSON report path")
    parser.add_argument("--compiler-inputs", type=Path, help="explicit exact-build compiler inputs")
    parser.add_argument("--project-file", type=Path, help="same-workspace platform contract")
    args = parser.parse_args()
    try:
        result = preflight(args.workspace, args.report, compiler_inputs=args.compiler_inputs,
                           project_file=args.project_file)
    except (PreflightError, OSError, KeyError, ValueError) as exc:
        print(f"[error] preflight setup: {exc}", file=sys.stderr)
        return 2
    for check in result["checks"]:
        print(f"[{check['status']}] {check['name']}: {check['detail']}")
    for finding in result["findings"]:
        location = finding["path"] + (f":{finding['line']}" if finding["line"] else "")
        print(f"[{finding['severity']}] {finding['rule']} {location}: {finding['message']}")
    print(f"Preflight result: {result['status'].upper()} ({result['decision']})")
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
