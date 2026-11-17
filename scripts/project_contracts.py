"""Load project manifests, including per-platform overlays.

`bitwig-project.json` is the complete contract of a workspace. A platform
contract such as `bitwig-project.macos-6.1.3.json` is an overlay: it names the
manifest it extends and states only what differs, normally `target` and
`output`. Everything else (identity, presentation, sources, adapter and
preflight) comes from the main manifest, so the platforms cannot drift apart.

```json
{
  "extends": "bitwig-project.json",
  "target": {"bitwigVersion": "6.1.3", "platform": "macos-arm64"},
  "output": {"path": "build/macos-6.1.3/Name.bwdevice", "libraryPath": "My Devices/Name.bwdevice"}
}
```

Each overlay key replaces the top-level key of the same name. Always read a
contract through `load`; a raw `json.load` of an overlay is incomplete.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def help_resource_path(artifact: Path) -> Path:
    return artifact.parent / "resources" / artifact.stem / "resources.properties"


def write_help_resource(artifact: Path, stage: Path, source: Path) -> None:
    target = help_resource_path(artifact)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = stage / target.name
    staged.write_bytes(source.read_bytes())
    os.replace(staged, target)


def help_strings(source: Path) -> dict[str, str]:
    """Read the repository's literal English help keys and descriptions."""
    entries = {}
    for line in source.read_text(encoding="ascii").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, equals, value = stripped.partition("=")
        key, value = key.strip(), value.strip()
        if not equals or not key or not value or key in entries:
            raise ValueError(f"{source}: invalid or duplicate help entry {key!r}")
        entries[key] = value
    return entries


MANIFEST = "bitwig-project.json"
# Platform -> overlay file name. A platform that is not listed uses the manifest.
PLATFORM_CONTRACTS = {
    "macos-arm64": "bitwig-project.macos-6.1.3.json",
    "linux-x86_64": "bitwig-project.linux-6.1.7.json",
}
# Identity and content must be the same on every platform.
OVERLAY_KEYS = frozenset({"extends", "target", "output"})


def host_platform() -> str:
    """The platform name of this machine, as used in `target.platform`."""
    if os.name == "nt":
        return "windows-x86_64"
    if sys.platform == "darwin":
        return "macos-arm64"
    return "linux-x86_64"


def load(path: Path) -> dict:
    """Read a manifest or overlay and return the complete contract."""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    base_name = data.get("extends")
    if base_name is None:
        return data
    if base_name != MANIFEST:
        raise ValueError(f"{path.name}: extends must be {MANIFEST!r}")
    unexpected = sorted(set(data) - OVERLAY_KEYS)
    if unexpected:
        raise ValueError(f"{path.name}: a platform overlay may only set target and output, not {unexpected}")
    base = json.loads((path.parent / MANIFEST).read_text(encoding="utf-8"))
    return base | {key: value for key, value in data.items() if key != "extends"}


def contract_path(workspace: Path, platform: str) -> Path | None:
    """The file that describes this workspace for a platform, or None."""
    workspace = Path(workspace)
    manifest = workspace / MANIFEST
    if not manifest.is_file():
        return None
    overlay = PLATFORM_CONTRACTS.get(platform)
    if overlay and (workspace / overlay).is_file():
        return workspace / overlay
    target = json.loads(manifest.read_text(encoding="utf-8")).get("target", {})
    return manifest if target.get("platform") == platform else None


def load_for(workspace: Path, platform: str) -> dict | None:
    """The complete contract of a workspace for a platform, or None."""
    path = contract_path(workspace, platform)
    return load(path) if path else None
