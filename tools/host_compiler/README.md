# Nitro source compiler bridge

`NitroBridge.java` is the GPL-3.0-only Java source extracted from the local
Research snapshot at commit `cac5df0b0d5155c918839652de0138b93fa6dd57`
with local patch SHA-256
`2fe9160d15a437e273eb2d3fcded807600ae31392f26999a1c559c0ae2504e92`.
See [LICENSE](LICENSE). The compiled `.class` file is ignored.

`scripts/build-local-host-tools.py` compiles this source against the exact
Bitwig JARs named by a target profile. Those installed JARs are local licensed
inputs and are never copied into this repository.

`NitroSemanticProbe.java` is the repository-authored preflight helper that runs
the pinned Windows Bitwig 6.1.3 or 6.1.7 Nitro front end, including semantic analysis,
on plain-text sources. Preflight compiles it into a temporary directory and
analyses every declared source serially, because a cold parallel analysis can
fail with `Recursion in semantic analysis`. On either pinned Windows
profile, the optional JVM property `nitro.llvm.output` directs the probe to
write one numbered LLVM IR file per successful source to the temporary
directory. Python preflight parses and verifies the files before removing
the directory. Host context such as the connected channel count, native JIT
execution, graph acceptance and audio remain live gates.
