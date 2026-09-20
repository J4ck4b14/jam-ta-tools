# JAM TA Tools architecture

The design rule is simple: **hosts measure; the shared core interprets; UIs present; engines verify**.

## Layers

1. **Host adapters** (`Blender/ta_tools.py`, `Maya/ta_tools.py`) collect scene/mesh/material facts with native APIs.
2. **Shared core** (`Common/jam_ta_core`) owns profiles, reports, custom-rule registration, render-split analysis, UV-layout analysis, texel statistics, cost models, modular-grid math, Asset Sets, export planning and serialisation.
3. **UI/actions** translate stable issue/component identifiers back into host selections and conservative fixes.
4. **Export contract** (`.jammeta.json`) describes what left the DCC without storing host objects or private absolute texture paths.
5. **Engine verification** measures what Unity/Unreal actually imported instead of presenting DCC estimates as engine truth.
6. **Automation** calls the same report path headlessly and serialises JSON/JUnit for CI.

## Profiles versus rules

A **profile** is data: severity choices, texture limits, lightmap expectations, skin influence caps and similar workflow policy. Built-ins are deliberately moderate; project JSON can replace/tighten them.

A **registered rule** is code for something project-specific that does not belong in the universal pack. `rules.py` gives it the completed report, profile and a plain snapshot. The registry catches extension exceptions and turns them into blocking Pipeline issues so one bad project callback cannot kill the whole validator.

## Render Split Inspector

Render-vertex estimation progressively keys face corners by:

1. source vertex;
2. corner normal;
3. each UV channel;
4. material section (when enabled).

Each stage owns the vertices it adds, making the estimate explainable. Blender/Maya adapters can select the source edges responsible for normal, UV or material splits. Unity/Unreal comparison exists because tangents, importer rules and platform packing can still change the final representation.

## UV model

`uv_layout.py` receives only face vertex IDs and UV coordinates. It:

- joins faces into islands when a shared geometric edge is also UV-continuous;
- triangulates UV polygons for spatial tests;
- detects positive-area overlap across separate islands;
- estimates 0-1 occupied area;
- finds minimum inter-island padding;
- supplies island membership for per-island texel-density aggregation.

The utilisation number is explicitly a packing estimate, especially for three-way overlap. The overlap components are the authoritative diagnostic.

Lightmap validation is profile-controlled. Unity/Unreal profiles audit channel 1 when it exists and report missing channels informationally by default; strict/project profiles can promote those findings.

## Texel density

Host geometry area is converted to square metres before shared calculations. Canonical reports use px/m so Maya centimetres and Blender scene units do not leak into the model. The UI may display px/cm.

Temporary `JAM_TD_HEATMAP` colour data is not counted as authored vertex colour data and is removed/restored around interchange export.

## Materials and textures

Hosts enumerate materials/shaders and connected image/file nodes. `material_analysis.py` handles semantic inference, UDIM recognition, set consistency, packing hints, colour-space policy and reference memory.

The memory figure is a comparison signal, not a platform allocation promise. Sidecars omit absolute source paths and expose only safe source basenames plus dimensions/semantic/UDIM/reference-byte data.

## Mesh cost model

`cost_analysis.py` produces a transparent reference buffer layout from estimated render vertices, triangle/index count, UV channels, vertex colours and skinning. Unity then measures actual vertex-buffer strides/index format; Unreal compares render counts from LOD resources.

## Asset Sets

Explicit host metadata groups a deliverable across render members, LODs, collision, sockets, skeletons and helpers. Naming conventions bootstrap roles but are not the long-term source of truth.

Multi-part LODs are evaluated by aggregate cost. Collision generation currently provides deterministic UBX bounds boxes and conservative USP enclosing spheres in both hosts; custom convex members are validated rather than auto-invented. Export selects only enabled members, optionally shifts the complete top-level set to the LOD0 pivot, exports, restores scene transforms and writes the set structure into the sidecar.

## Modular authoring

`modular.py` contains host-independent snapping, bounds-grid analysis and attachment naming. Profiles express grid size/tolerance in centimetres. Host adapters convert world-space bounds into centimetres first, so policy is not coupled to the DCC's current unit setting.

Dimension compliance and world-placement compliance are separate signals: a 200 x 100 x 300 cm wall piece can be a valid module while still sitting 25 cm off the placement grid. This matters when validating kit dimensions without forcing every authored piece to live at world-grid coordinates during construction.

Bounds-size matching is limited to axis-aligned references and movers; rotated cases are rejected because local scaling does not reliably reproduce an exact world-space AABB.

Sockets/helpers are explicit Asset Set members. Naming is generated consistently, but persistent membership remains authoritative after the object/node is renamed.

## Export planning

`export_profiles.py` owns both **format** and **extension**, while host adapters own the actual exporter calls. `export_plan.py` resolves the final export/sidecar paths and overwrite state before scene mutation begins.

This separation prevents a preset called "USD" from quietly invoking FBX. Unsupported host/format combinations fail explicitly. Overwrite permission is also checked before batch or Asset Set export so a late collision with an existing file does not become an accidental destructive action.

## Export sidecar

`jam-ta-tools.export-sidecar.v3` contains:

- suite/source/profile/export-preset identifiers plus resolved export format/extension;
- per-asset status and warning/error summaries;
- source/model/render vertex and triangle metrics;
- UV/TD/layout/lightmap metrics;
- material/texture/reference-memory metrics;
- compact texture descriptors without absolute paths;
- Asset Set member roles/LODs/collision structure;
- modular-grid/bounds metrics when that policy is active;
- compact blocking/warning issue metadata.

Consumers are expected to ignore unknown fields so the schema can grow additively.

## Engine bridges

### Unity 6

An `AssetPostprocessor` reads sibling sidecars and compares actual imported mesh counts and buffer size. A manual menu command re-runs validation. Texture source basenames are compared against imported model dependencies as an observational signal only.

### Unreal 5.8

The editor-only plugin derives from `UEditorValidatorBase`. It resolves the Static Mesh source file through import data, loads a sibling sidecar and compares LOD0 render vertices/triangles/sections/UVs. DCC blocking errors fail Data Validation; representation mismatches warn.

## Maya UI

The detailed tools are hosted in a `workspaceControl` so the panel can dock and participate in Maya workspaces. `jam_ta_qt.py` adds a compact PySide6 dashboard for Asset Doctor-centric work while retaining the detailed controls rather than prematurely rewriting every mature utility.

## Automation

`jam_validate_blend.py` and `jam_validate_maya.py` support profile JSON, custom Python rule modules, JSON output, JUnit output and warning-as-error gates. CI therefore runs the same validators artists see interactively.
