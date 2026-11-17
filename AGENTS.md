# Agent guide

Use [README.md](README.md) as the documentation index. The demo is the
reference for an original Audio FX. Use the build guide to select its exact
Bitwig version and platform.

| Task | Start here |
| --- | --- |
| Build and validate offline | [Build procedure](docs/BUILD.md) |
| Write DSP, bind controls or change the shader | [Authoring guide](docs/AUTHORING.md) |
| Register a device or check live behavior | [Patchbay and Bitwig test](docs/BITWIG-TEST.md) |
| Review upstream source and notices | [Tool provenance](docs/PROVENANCE.md) |

When starting a separate device, give it a fresh UUID and name. For compatible
revisions, retain its UUID, stable control identifiers and compatible saved types.
Keep Nitro declarations, graph connections, panel bindings and the manifest's
preflight contract in sync.

Run preflight after each DSP, document or panel change. Keep the report and
record live test evidence against its artifact SHA-256. Only launch Bitwig and
change Patchbay's folders when the user requests a live test.

Keep the authored demo and structure-only format knowledge in source. Keep
Bitwig JARs, factory documents, decompiled code, keys and generated device
files out of Git. Use a fresh history if publishing this blueprint.
