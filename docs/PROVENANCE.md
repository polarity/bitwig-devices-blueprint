# Source provenance

Blueprint Gain, its Nitro source, its panel and its document constructor are
original examples. The builder serializes a plain document from typed
structures and compiles the demo's Nitro against the user's local,
hash-pinned Bitwig compiler JARs. No factory document or factory DSP is a
build input.

The device and source-document gates, document lints, compiler profiles,
manifest helpers, hybrid port helpers and semantic probe were refreshed from
`bitwig.devices` at `a45682217cfdb04ff954877cab8747e7dcb10d46`.
The other setup code and small graph helpers come from
`0afc203f486d06539461a39dfe572a179bc573de`.

Blueprint-specific adaptations include the display-only control contract,
Linux 6.1.7 compiler profile from the existing Linux contribution,
version-specific device reports, and exact-profile AST decoding for note-port
inspection. The repository carries one Audio FX example. Product sources,
Grid module builds and the FILAR-specific regression fixtures stay upstream.

- [Nitro AST subset](../tools/nitro_ast/README.md) and [MIT notice](../tools/nitro_ast/LICENSE).
- [Document codec](../tools/document_codec/README.md) and [GPL-3.0-only license](../tools/document_codec/LICENSE).
- [Nitro bridge](../tools/host_compiler/README.md) and [GPL-3.0-only license](../tools/host_compiler/LICENSE).
