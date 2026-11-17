"""Exact-build, offline Nitro AST compilation through the local bridge.

This is a source parser, not the live audio-engine JIT. Licensed inputs remain local.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import zlib

PROFILES = {
    "windows-x86_64-bitwig-6.1.3": {
        "version": "6.1.3",
        "platform": "windows-x86_64",
        "jarSha256": "37073037335331cc6d14ebd1fb3c8c32f159a346574dceb7a119ea942f938ed6",
        "libsSha256": "e9229581eff455e58e260c55903095f41bd7c723608f9f39d1cc623e75ab9cfc",
        "bridgeSha256": "7f7c70e203a80f1f3bc2cdb725e80a4f01b3e98f2a87b56a124611fc56832be3",
    },
    "windows-x86_64-bitwig-6.1.4": {
        "version": "6.1.4",
        "platform": "windows-x86_64",
        "jarSha256": "5c38187eebe14ce7e5fdaa9d8f1eac0e9b2a34116f57d6a854cef7a31f2845c0",
        "libsSha256": "e9229581eff455e58e260c55903095f41bd7c723608f9f39d1cc623e75ab9cfc",
        "bridgeSha256": "7f7c70e203a80f1f3bc2cdb725e80a4f01b3e98f2a87b56a124611fc56832be3",
    },
    "windows-x86_64-bitwig-6.1.7": {
        "version": "6.1.7",
        "platform": "windows-x86_64",
        "jarSha256": "a5b303466cf66a80e0fd7df00e49fb14eb41c23e59d30b300c0554b6769b3367",
        "libsSha256": "f7b857c77ac67607daa8b02b502f51229c186a0a1c870e1f857bb331ceb23885",
        "bridgeSha256": "7f7c70e203a80f1f3bc2cdb725e80a4f01b3e98f2a87b56a124611fc56832be3",
        "astCompression": "raw-deflate",
    },
    "macos-arm64-bitwig-6.1.3": {
        "version": "6.1.3",
        "platform": "macos-arm64",
        "jarSha256": "a5c096a8ef95ac322149bb8e2bb2eb2a9806de6cb2af26b7eb3945876c9aad4f",
        "libsSha256": "8df006f8a6574b8eceee5bd355eb087fb66c430dc9b7202deedbae17df9af222",
        "bridgeSha256": "7f7c70e203a80f1f3bc2cdb725e80a4f01b3e98f2a87b56a124611fc56832be3",
    },
    "macos-arm64-bitwig-6.1.7": {
        "version": "6.1.7",
        "platform": "macos-arm64",
        "jarSha256": "6024752847797eb41e6fbc91e081211937931cc452a695ab78226321cefdac8d",
        "libsSha256": "f00d33c0b8d74025b02e5268ad61c055c8dbda96b4bb43fd40b605b853576c68",
        "bridgeSha256": "7f7c70e203a80f1f3bc2cdb725e80a4f01b3e98f2a87b56a124611fc56832be3",
        "astCompression": "raw-deflate",
    },
    "linux-x86_64-bitwig-6.1.7": {
        "version": "6.1.7",
        "platform": "linux-x86_64",
        "jarSha256": "a3e52ddc94cfe69e08f75a96b2ee43b04a932d804948fa09c013ea7a04a43c44",
        "libsSha256": "72a94668b2c1739c1b1c0a7bfaba56f4f1f78efaf0da4bb08fd9030bcde5081c",
        "bridgeSha256": "4134383e2a0365af431ea97fc82371e979067b63c2ad6812a1439a076cc6fbeb",
        "astCompression": "raw-deflate",
    },
}

def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()

def validate_inputs(path: Path) -> tuple[dict, dict]:
    inputs = json.loads(path.read_text(encoding="utf-8-sig"))
    if inputs.get("schemaVersion") != 1 or inputs.get("profileId") not in PROFILES:
        raise ValueError("Unknown offline compiler profile")
    profile = PROFILES[inputs["profileId"]]
    for field, expected in (("jar", "jarSha256"), ("libs", "libsSha256"), ("bridge", "bridgeSha256")):
        if not isinstance(inputs.get(field), str) or not Path(inputs[field]).is_file():
            raise ValueError(f"Exact-profile {field} is missing")
        pinned = profile[expected]
        if field == "bridge" and "bridgeSha256" in inputs:
            pinned = inputs["bridgeSha256"]
            if not isinstance(pinned, str) or len(pinned) != 64 or any(c not in "0123456789abcdef" for c in pinned):
                raise ValueError("Exact-profile bridgeSha256 must be a lowercase SHA-256 digest")
        if digest(Path(inputs[field])) != pinned:
            raise ValueError(f"Exact-profile {field} SHA-256 mismatch")
    if not isinstance(inputs.get("java"), str) or not Path(inputs["java"]).is_file():
        raise ValueError("Compiler Java executable is missing")
    document_parser(inputs, profile)
    return inputs, profile


def document_parser(inputs: dict, profile: dict) -> tuple[Path, str]:
    """Resolve the process tool pinned for this exact compiler input set."""
    configured = inputs.get("parser")
    expected = inputs.get("parserSha256")
    if not isinstance(configured, str) or not configured:
        raise ValueError("Exact-profile document parser is missing")
    if not isinstance(expected, str) or len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise ValueError("Exact-profile parserSha256 must be a lowercase SHA-256 digest")
    path = Path(configured)
    if not path.is_file():
        raise ValueError(f"Exact-profile document parser is missing: {path}")
    if digest(path) != expected:
        raise ValueError("Exact-profile document parser SHA-256 mismatch")
    return path, expected

def require_target(profile: dict, target: dict) -> None:
    if (target.get("bitwigVersion"), target.get("platform")) != (profile["version"], profile["platform"]):
        raise ValueError("Compiler profile does not match workspace target version and platform")

def unpack_ast(binary: bytes, profile: dict) -> bytes:
    """Return the AST bytes for offline analysis, preserving compiler output on disk."""
    if profile.get("astCompression") != "raw-deflate":
        return binary
    inflater = zlib.decompressobj(-zlib.MAX_WBITS)
    try:
        ast = inflater.decompress(binary) + inflater.flush()
    except zlib.error as exc:
        raise ValueError("Compiled Nitro AST is not a raw DEFLATE stream") from exc
    if not inflater.eof or inflater.unused_data:
        raise ValueError("Compiled Nitro AST must be a single complete raw DEFLATE stream")
    return ast

def compile_source(source: Path, output: Path, inputs_path: Path) -> dict:
    inputs, profile = validate_inputs(inputs_path)
    source_sha = digest(source)
    bridge = Path(inputs["bridge"]).resolve().parent
    command = [inputs["java"], "-cp", os.pathsep.join(map(str, (bridge, inputs["jar"], inputs["libs"]))), "NitroBridge", "text-to-binary", str(source), str(output)]
    # Remove an old output so a failed child can never masquerade as a fresh build.
    output.unlink(missing_ok=True)
    completed = subprocess.run(command, capture_output=True, timeout=45, check=False)
    if completed.returncode or not output.is_file():
        output.unlink(missing_ok=True)
        raise ValueError(f"Exact-profile source parser failed ({completed.returncode}): {completed.stderr.decode('utf-8', errors='replace')}")
    binary = output.read_bytes()
    try:
        from bitwig_nitro import decompile_nitrobin, serialize_nitrobin
        ast = unpack_ast(binary, profile)
        stable = bool(ast) and serialize_nitrobin(decompile_nitrobin(ast)) == ast
    except Exception:
        output.unlink(missing_ok=True)
        raise
    if not stable:
        output.unlink(missing_ok=True)
        raise ValueError("Compiled Nitro AST failed byte-stable roundtrip")
    # Recheck source toolchain after the subprocess in case inputs changed mid-build.
    try:
        validate_inputs(inputs_path)
        if digest(source) != source_sha:
            raise ValueError("Nitro source changed during compilation")
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return {"profileId": inputs["profileId"], "version": profile["version"], "platform": profile["platform"], "sourceSha256": source_sha, "astSha256": digest(output), "hostVerified": False}
