# Gomu Gomu Survivors iOS helper

Build 1.9.0 (19) from a sealed Unity export. Validate every input file, compile an ARM64 iOS device app, package an unsigned IPA, and verify its contents. This helper never launches the game or a simulator.

The existing private workflow retains stable legacy asset filenames; the manifest, Info.plist and validator require the current version and build. Technique timing, inked art and warning readability are reviewed in the Unity project's edit mode before export.
