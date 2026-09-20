# JAM TA Tools

Technical-art pipeline tooling for Blender, Maya, Unity and Unreal.

JAM now follows one production workflow rather than acting as a folder of unrelated scripts:

**profile -> analyze -> inspect -> safely fix -> validate -> export -> verify in engine / CI**

The suite includes renaming, pivot/origin, budget, UV projection, texel-density, interchange export and rigging helpers under the same validation and delivery workflow.

## Capabilities

### Asset Doctor

Blender and Maya share the same `AssetReport` / `ValidationIssue` model and project profiles. Reports cover topology, transforms, render-vertex splits, UVs, skinning, materials, textures and memory-oriented reference metrics.

Custom project profiles can be loaded from JSON. Custom Python rules can be registered without editing JAM; a broken extension becomes an explicit Pipeline error rather than crashing the complete analysis pass.

### UV and texel diagnostics

- physical texel density in canonical px/m, displayed as px/cm or px/m;
- per-face and per-island texel-density aggregation;
- TD spread/outliers and viewport heatmaps;
- UV-island construction from mesh topology + UV continuity;
- cross-island overlap detection;
- estimated 0-1 utilisation;
- minimum island padding at the chosen texture resolution;
- optional lightmap-channel audit for missing channels, overlap and padding;
- selectable normal/UV/material boundaries behind render-vertex inflation.

Overlap is profile-aware. Mirroring/tiled workflows are not silently declared wrong by a universal rule.

### Materials and textures

Asset Doctor now inventories shader texture nodes and reports:

- texture count and material count;
- missing source files;
- semantic inference (base colour, normal, roughness, metallic, AO, emissive, masks, etc.);
- colour-space mismatches;
- non-power-of-two informational findings;
- UDIM detection/tile counts;
- resolution inconsistencies inside a texture set;
- optional ORM-style packing opportunities;
- reference compressed/uncompressed texture memory;
- profile texture-dimension, texture-count and texture-memory limits.

Absolute workstation paths are deliberately not written into `.jammeta.json`; engine sidecars carry safe basenames and technical metadata instead.

### Asset Sets, LOD and collision

Asset Sets persist one deliverable across render pieces, LODs, collision, sockets, skeletons and helpers. Names bootstrap membership but explicit scene metadata becomes the source of truth.

LOD validation aggregates multi-part levels, checks progression/gaps and can create non-destructive LOD duplicates. Collision tooling recognises common Unreal prefixes, tracks cost, audits custom convex meshes and can build UBX box or conservative USP sphere collision directly from render bounds.

### Modular authoring and attachments

The Modular Environment profile now has measurable policy instead of being only a label. Blender and Maya can:

- audit module dimensions against a physical centimetre grid;
- check world placement separately from module size;
- snap selected axes using nearest/floor/ceil modes;
- align a chosen bounds anchor to an active reference;
- match selected axis-aligned AABB dimensions to the active reference on chosen axes;
- create persistent socket/helper members at origins, cursors or bounds anchors;
- batch-normalise rotation/scale while refusing negative scale by default.

Maya converts its current linear unit back to centimetres before shared checks, so a 100 cm project grid remains 100 cm in metres, millimetres, feet, etc.

### Export presets, preview and sidecars

Shared export presets include Generic FBX, Unity Static/Rigged, Unreal Static/Skeletal, Generic OBJ, Generic GLB and Generic USD. A preset owns a real format and extension; the host either runs the matching exporter or fails clearly instead of silently falling back to FBX. Blender implements FBX/OBJ/GLB/USD. Maya implements FBX/OBJ/USD when the required USD plug-in is available; JAM intentionally reports Generic GLB as unsupported there unless a studio adds its own exporter.

Before writing, **Preview Export** resolves the exact target path, format, member count and sidecar path. Existing files are surfaced explicitly and export is blocked unless **Overwrite Existing** is enabled. Asset Set export uses the same preflight.

`.jammeta.json` V3 carries source validation status, Asset Set structure, mesh/UV/skinning/material/texture/modular metrics, export format/extension and the export profile. The schema stays V3 because these fields are additive and existing consumers ignore unknown keys. Diagnostic heatmap colour data is stripped around export and restored afterwards.

### Engine verification

**Unity 6** compares imported triangles, render vertices, submeshes/material sections, UVs and actual vertex/index buffer size against the DCC contract. It also reports source texture metadata and how many exported source texture filenames appear in the model's Unity dependencies. It reports import differences without modifying importer settings.

**Unreal Engine 5.8** gets an editor-only `JAMTATools` plugin with native `UEditorValidatorBase` validators for Static and Skeletal Meshes. It locates the sidecar beside the FBX source, compares LOD0 render data and participates in Unreal Data Validation. Skeletal Mesh verification additionally compares maximum skin influences and checks for an unexpected loss of deform bones.

### CI / headless validation

Blender and Maya packages ship command-line entry points which use the exact same Asset Doctor rules as the interactive tools and can write JSON and JUnit XML.

Blender example:

```bash
blender -b asset.blend --python jam_validate_blend.py -- \
  --profile UNREAL_STATIC \
  --profiles project_profiles.json \
  --rules project_rules.py \
  --output report.json \
  --junit report.xml
```

Maya example:

```bash
mayapy jam_validate_maya.py asset.ma \
  --profile UNREAL_STATIC \
  --profiles project_profiles.json \
  --rules project_rules.py \
  --output report.json \
  --junit report.xml
```

Both return a non-zero process status for blocking errors; `--warnings-as-errors` is available for stricter gates.

## Source layout

```text
Common/jam_ta_core/         host-independent profiles, reports, rules and analysis
Blender/                    Blender extension + background validator
Maya/                       Maya module + workspace UI + PySide6 dashboard + mayapy validator
Unity/com.jam.ta-tools/     Unity 6 import-verification package
Unreal/JAMTATools/          Unreal editor Data Validation plugin
examples/                   project-profile and custom-rule examples
docs/                       architecture notes
tests/                      host-independent regression tests
build_release.py            reproducible release builder
```

## Blender

Run `python build_release.py` and install `dist/JAM_TA_Tools_Blender_2.4.0.zip` via **Extensions > Install from Disk**. The extension targets Blender 4.2+ and contains its own `jam_ta_core` copy plus `jam_validate_blend.py` for CI.

## Maya

Extract `dist/JAM_TA_Tools_Maya_2.4.0.zip` somewhere on `MAYA_MODULE_PATH` and restart Maya.

Production/default docked tools:

```python
import ta_tools
ta_tools.show()
```

Modern PySide6 Asset Doctor dashboard (Maya 2025+):

```python
import jam_ta_qt
jam_ta_qt.show_dashboard()
```

The detailed tool surface remains available because it exposes utilities not duplicated in the compact dashboard.

## Unity

Extract `dist/JAM_TA_Tools_Unity_2.4.0.zip` and add `com.jam.ta-tools` through Unity Package Manager (**Add package from disk...**) or place it in the project's `Packages` folder.

Enable sidecar writing in the DCC exporter. Use **Tools > JAM TA Tools > Validate Selected Model** to manually re-run comparison.

## Unreal

Copy the `JAMTATools` directory from `dist/JAM_TA_Tools_Unreal_2.4.0.zip` into the project's `Plugins` folder and enable **JAM TA Tools** plus Unreal's **Data Validation** plugin. Rebuild the editor module for the target Unreal version.

The Unreal bridge validates both Static and Skeletal Meshes and can participate in Unreal's normal validation workflows and commandlet once compiled/enabled.

## Project extensions

See:

- `examples/project_profiles.example.json`
- `examples/project_rules.example.py`

Project rule callbacks receive the built-in report, active profile and a plain serialisable-ish snapshot. This keeps project policy separate from host data collection.

## Tests

```bash
python -m unittest discover -s tests -v
python -m py_compile Common/jam_ta_core/*.py Blender/*.py Maya/*.py build_release.py
```

The repository tests shared calculations, report/sidecar contracts, custom rules, UV layout analysis and package construction. Interactive Blender/Maya APIs, Unity C# compilation and Unreal C++ compilation still require smoke tests in their real hosts before describing a release as fully production-certified.
