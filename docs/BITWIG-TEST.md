# Load and test with Patchbay

Use the same Bitwig version and platform as the offline build. Start with a
disposable project and the exact artifact checked in
[Build and preflight](BUILD.md#run-the-gate-and-build).

## Register the file

1. Choose a Patchbay release that lists support for your Bitwig build and
   follow its bundled `INSTALL.md`. [Patchbay releases](https://github.com/shaboogen-dev/Patchbay/releases)
2. Close Bitwig. Put just the verified `Blueprint Gain.bwdevice` in a dedicated
   test folder outside the Bitwig installation. Compare its SHA-256 against
   the preflight report after copying it.
3. Open Patchbay using the launcher for your OS and select the matching
   installation. Enable **Framework: Nitro Devices**. Older releases call it **Framework:
   Custom Nitro Registration**. In **Tools → Device Folders**, add the test
   folder. The scanner recurses, so keep one file per UUID and device name
   across all listed folders. The 6.1.3 and 6.1.7 builds share a UUID. Scan
   only the matching version folder and remove the old one when switching.
4. Click **Load** and search for **Blueprint Gain** in Bitwig's device browser.
   Confirm insertion and close the browser so the device is committed to the
   track. After replacing the file, relaunch Bitwig through Patchbay.

## Check audio, GUI and cold recall

1. Play a known finite stereo signal with distinguishable left and right
   channels. At gain **1.00**, the output should match the input. At **0.50**,
   both amplitudes should halve (about **−6.02 dB** relative to input). At
   **0.00**, both outputs should be silent. Measure both channels using the
   same signal and measurement window, after the gain has settled.
2. Drag and reset the native knob. Compare the meter lanes with the known
   channel levels and repeat at another UI scale. The amber line should
   follow the linear gain setting.
3. Put the gain back at **0.50**, save, close Bitwig completely and reopen
   the project after launching through Patchbay again. Re-measure before
   touching the knob. The expected amplitude ratio is still **0.50**.
4. Record the Bitwig build, Patchbay/framework versions, artifact hash,
   measured results and any startup, compiler or engine errors. For a device
   missing from the browser, check the Patchbay scan log and duplicates.
   For insertion errors, check `BitwigStudio.log` before the engine log.

To remove the demo, close Bitwig, remove the test folder from Patchbay and
relaunch through Patchbay. Keep the matching artifact outside scanned folders
if you want to reopen the disposable test project later.

## Current evidence

Windows x64 6.1.7 passed the offline gate with zero findings, including the
6.1.7 semantic probe and LLVM verification. Its built artifact matched the
report SHA-256 `85127a64ec21bf725ab971024266cf9207e046e2dd512c0fa0e64f292ce85684`.
The pinned 6.1.7 Nitro reader also accepted the embedded payload offline.
Live tests for that build and revalidation of the Linux target are pending.

Live-tested on 2026-11-14 with Bitwig Studio 6.1.3 Windows x64
(revision `c41d0b6a6b042d5e6705b382ff28a6fc9bcd1671`), Patchbay
`0.36.0-dev.9` and Custom Nitro Registration `1.2.0-dev.5`.
The scanned file matched the passing preflight SHA-256
`37d5c1caab910655e40a5ec1b9a59290f3dbc17e22bbefc32021b3087eb8c4a3`.
Patchbay registered UUID `e104966a-a53d-4e68-9125-2c8c667c04a2`, and
Blueprint Gain appeared in the browser and was inserted on an audio track.

| Check | Observed result |
| --- | --- |
| Gain 1.00 | Stereo RMS matched the source within 0.01% per channel after resampling. |
| Gain 0.50 | RMS ratios relative to the unity render were 0.49999993 left and 0.50000032 right (about −6.02 dB on both channels). |
| Gain 0.00 | Both rendered channels contained only zero samples. |
| Native knob | Mouse drag changed the value from 1.00 to 0.78. Double-click reset 0.00 to 1.00. Both were verified through parameter readback. |
| SkSL display | Both teal bars responded to the unequal stereo signal. The amber gain line went from full to half width. Verified at the session's existing UI scale. |
| Cold project recall | After all Bitwig processes exited, the saved project reopened through Patchbay. Gain read 0.50, the shader drew both lanes, and the audio export contained identical PCM before and after restart. No control was edited before that export. |

The probe was a 48 kHz stereo WAV with a 440 Hz sine at 0.75 peak on the
left and a 660 Hz sine at 0.25 peak on the right. Bitwig exported the same
two-bar region at 44.1 kHz in 16-bit stereo, pre-fader with dither disabled.
RMS was measured on a two-second window beginning 0.5 seconds into each
export. The half-gain PCM SHA-256 before and after restart was
`de1e77a7ee2deb29ef1901f70c2d7064113a1389ac70892e3bccc915f9ae2167`.

On reopen, `BitwigStudio.log` reported missing factory remote controls and a
failed metadata lookup for `Library/devices/Blueprint Gain.bwdevice` under
the Bitwig installation. The engine then connected to the saved project,
and the GUI and both audio channels passed the recall checks above.

The temporary scan folder was removed from Patchbay by restoring its
configuration byte-for-byte after shutdown. The disposable project,
exports and screenshots remain under ignored `local/live-test/`.
A second UI scale remains untested.
