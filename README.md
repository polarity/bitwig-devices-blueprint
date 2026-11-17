# Bitwig Device Blueprints

A small, source-built Bitwig Studio Audio FX for people and coding agents.
**Blueprint Gain** combines a native gain knob, original stereo DSP in Nitro,
and a SkSL output display. It passed offline checks on Windows x64 with
**Bitwig Studio 6.1.3 and 6.1.7**. The build guide also includes a Linux
x64 target for 6.1.7.

1. [Set up and run the offline preflight](docs/BUILD.md).
2. [Adapt the DSP, graph or shader panel](docs/AUTHORING.md).
3. [Load the verified artifact through Patchbay and test it](docs/BITWIG-TEST.md).

See the [authoring notes for other device types](docs/AUTHORING.md#other-device-types)
for checks relevant to instruments, modulators and Grid modules.

Point a coding agent at [AGENTS.md](AGENTS.md). Builds use the compiler JARs
from your own Bitwig installation.
