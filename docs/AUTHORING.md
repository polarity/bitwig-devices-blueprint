# Author a native Audio FX

The original demo multiplies both stereo channels by a linear gain from 0
to 1. Its native knob owns the parameter and its saved value. The shader
shows the two instantaneous output magnitudes and the gain setting.

| Change | Edit |
| --- | --- |
| DSP and display lanes | [gain.nitro](../devices/blueprint-gain/gain.nitro) |
| Parameter type, routing and panel layout | [demo_document.py](../devices/blueprint-gain/demo_document.py) |
| Colors and drawing | [panel.sksl](../devices/blueprint-gain/panel.sksl) |
| Identity, target, source list, output and expected ABI | [bitwig-project.json](../devices/blueprint-gain/bitwig-project.json) |
| Compile and serialize | [build.py](../devices/blueprint-gain/build.py) |

## Parameter ranges and defaults

Blueprint Gain uses a linear multiplier. At `0.00` it mutes the signal,
`0.50` halves its amplitude (−6.02 dB), and `1.00` is unity.
When adapting the demo, choose the unit, range, default and response curve
together. For example, a dB control needs an explicit dB-to-amplitude
conversion in DSP and a separate mapping to the shader's 0..1 display range.

| Location | What to update together |
| --- | --- |
| `gain_parameter()` in `demo_document.py` | The native decimal type defines `min#292`, `max#293` and `default_value#891`. Match the initial atom value `value#712` to the intended default. Keep the stable preset identifier `GAIN` for compatible revisions. |
| `gain.nitro` | Keep the audio-rate `f32` input default aligned with the native default. Update the DSP bounds and the normalized display lane when the native range changes. |
| `preflight.controlContract` in the manifest | For a linear decimal parameter, set `defaultNormalized` to `(default - min) / (max - min)`. The gate checks this value against the serialized native parameter. |

The control check verifies the declared normalized default and native widget
binding. It does not compare native limits with the DSP bounds or verify the
response curve. Test both endpoints, a fractional value and cold recall of a
non-default setting in Bitwig.

## DSP and routing

Copy the demo workspace to start a separate effect. Assign a fresh UUID,
project ID, name and output filename. The builder uses the manifest for
metadata and the document body. Follow these connections before adding DSP.

| Route | Contract |
| --- | --- |
| Embedded stereo proxy → delay compensation → Nitro input 0 | The root publishes audio and note proxy inputs in that order. |
| Native decimal atom → Nitro input 1 | Keep the fractional, recalled gain on an audio-rate `f32` input. |
| Nitro output 0 → root stereo output | Preserve the stereo channel shape. |
| Nitro output 1 → drawing input 0 | One `f32<4>` vector becomes `audioLow` in SkSL. |

Declare executable components in the root child list. Connection references
point back to earlier owned objects. The builder resolves object IDs after
constructing the tree. Changing a Nitro port means reviewing its descriptor,
position, connection and manifest together.

## Shader panel

1. Keep the native knob bound to the decimal atom and its value attribute
   `712`. The demo gives the knob and the drawing separate grid rectangles.
2. Put the original shader in the drawing component and bind its surface
   with `data_model#6220`. Leave the eight unused drawing inputs unconnected.
   The first vector carries L magnitude, R magnitude, gain and a reserved lane.
3. Map `fragCoord / iResolution.xy` into fixed design coordinates. The demo's
   41 × 35 drawing rectangle maps to 164 × 140 design units on a 72 × 53 panel.
4. Declare the shader under `sources.sksl`, run preflight and test distinct left
   and right signals at two UI scales. Local Skia compilation cannot measure
   Bitwig's paint or native hit regions.

The display is deliberately instantaneous. A production peak meter needs
envelope or peak-hold behavior in DSP, followed by a test of its response.

## Known limitations

The limits below come from the 6.1.x Windows device work and the bundled
6.1.3 hybrid preflight. They are conservative for this device shape.

| Failure | What to check |
| --- | --- |
| Nitro parses but semantic analysis fails | Use supported builtins and explicit casts such as `value.f32`. Nitro `mix()` and `clamp()` are known traps. |
| Semantic recursion reading a port | Compute audio once in a local and fan it out to audio and display. |
| Converter abort or silent processor | Preserve both proxy inputs, verify delay compensation routing and schedule the processor in the root. |
| Silent process stub or output-pointer fault | The profile caps each core at 30 audio inports and three audio outports. Pack display lanes into one four-float vector. |
| Drawing can crash the engine | Feed it a four-lane audio output. A scalar is an invalid substitute. |
| Knob paints but does not edit | Check the native atom, binding and mouse bounds. Keep opaque artwork off the knob and test a real drag. |
| Stored value appears correct but audio restarts at default | Measure output after a full restart before touching any control. Keep persisted controls and runtime DSP buffers distinct. |

Keep one Nitro core where possible. A cold multi-core compiler failure can
pass a warm retest, so test larger graphs in a fresh process. Devices with
runtime arrays need explicit initialization or write-before-read guarantees.
## Other device types

Blueprint Gain is the Audio FX example. Choose a gate based on the routing
and state model of the device you are building.

| Surface | Relevant checks |
| --- | --- |
| Note FX or instrument | Use the matching profile in `scripts/device_preflight.py`. Verify native note routing, event port types and root scheduling. Event instruments must route both their stereo output and voice lifetime signal correctly. |
| Modulator | Use `scripts/modulator_preflight.py`. Check the 17 × 17 main panel, mapping-button bounds, scheduled components and the actual mapping behavior in Bitwig. |
| Container or Grid module | Select `source-document` in the manifest and use `scripts/modulator_preflight.py`. Use the more specific device profile when its graph constraints apply. |

Invoke the source-document gate with the workspace, `--compiler-inputs` for
the exact target and `--report <file>` to retain its JSON results. Add
`--project-file <overlay>` when the build uses a target overlay.

Checks added after the first blueprint cover issues found in newer devices:

- In a reactive panel expression, an indexed parameter such as `GRID` is an
  integer. Read `GRID` directly. Ramona actions receive a parameter reference
  and can use `GRID.value`. `PANEL021` catches the reactive expression error.
- A Grid stereo audio jack on the tested 6.1.7 target carries four stereo
  frames per engine frame. Declare `f32<2>[4]` with descriptor width 8 and
  write all eight samples on every path, including idle. The Grid gate
  checks the compiled AST, persisted port descriptors, jack connections and
  acyclic routing. A workspace can also run its own DSP regression tests
  against emitted LLVM via `preflight.dspTest`.
- Give each independent meter or history tile its intended feed. Check each
  lane with a distinctive input. The shared lint includes targeted checks
  for duplicated feeds in Sonogram and Vectorscope documents.
- Keep Grid Inspector controls inside a fixed panel at most 39 grid units
  wide on this target. Give titled toggles visible text or an actual icon,
  and keep module sort order in the accepted range of −9 to 9.
  For live help, package the matching title and description keys under
  `resources/<module filename without extension>/resources.properties`
  beside the module.
