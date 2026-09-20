# JAM TA Tools

A cross-DCC technical art toolkit for game-asset workflows, with matched
feature sets for **Maya** (Python / OpenMaya 2.0) and **Blender** (bpy addon).
Built and maintained by Juan Abia Merino.

Each version is a single self-contained file implemented with that DCC's
native idioms — `cmds`/OpenMaya UI and undo chunking in Maya; operators,
panels, properties and drivers in Blender.

```
jam-ta-tools/
├── maya/ta_tools.py       # Maya 2022+ (Python 3)
├── blender/ta_tools.py    # Blender 2.80+
└── README.md
```

## Features

| Feature | Maya | Blender |
|---|---|---|
| Batch rename with prefix + padded numbering | ✅ | ✅ |
| Poly budget tracking (tris / faces / verts) | ✅ | ✅ |
| **Engine-vertex estimation** (UV seams, split normals, material splits) | ✅ | ✅ |
| Automatic LP/HP prefix classification on export | ✅ | ✅ |
| Pivot / origin placement (axis extremes, midpoint, selection) | ✅ | ✅ |
| Mesh validation: non-manifold edges & vertices, lamina faces, scale checks | ✅ | ✅ |
| UV validation: missing UVs, outside 0–1, flipped faces, zero-area faces | ✅ | ✅ |
| Texel density: check and match (px per world unit) | ✅ | ✅ |
| Validated FBX export, single or **batch (one file per object, move-to-origin)** | ✅ | ✅ |
| IK handle + **mathematically placed pole vector** | ✅ | ✅ |
| Pose-driven muscle helper joint/bone (bicep-style bulge) | ✅ | ✅ |
| Planar UV projection with island fitting | — | ✅ |
| Settings persistence across sessions | optionVar | JSON config |
| Single-undo batch operations | undoInfo chunks | REGISTER/UNDO |

## Install

### Maya
1. Copy `maya/ta_tools.py` somewhere Maya can access
   (e.g. `Documents/maya/scripts`).
2. In the Script Editor (Python tab):

```python
import ta_tools as jamta
jamta.show()
```

If the file lives outside Maya's script path, append its folder to
`sys.path` first.

### Blender
1. Edit > Preferences > Add-ons > Install…
2. Select `blender/ta_tools.py` and enable **JAM TA Tools**.
3. The panel appears in the 3D Viewport sidebar (N) under **TA Tools**.

## Usage highlights

**Engine-vertex estimation.** Raw vertex counts lie: engines split vertices
at UV seams, hard edges and material boundaries. The `ENGINE` count mode
builds a set of unique render vertices (position + normal + UVs + optional
material index) so budgets reflect what the importer will actually produce.

**Validation before export.** Export can be gated on validation: geometry
checks (non-manifold, lamina, scale) plus optional UV checks. Flipped UVs
are detected with a signed-area (shoelace) test — mirrored UVs are common
and legitimate, so UV checks can be toggled off per export. Full UV overlap
detection is intentionally out of scope: without spatial acceleration it
does not stay usable on production meshes.

**Batch export.** One FBX per selected object, each optionally moved to the
world origin for the export and restored afterwards — the standard
game-asset workflow. Original positions and selection survive even a
mid-batch failure.

**IK + pole vector.** The pole is placed by projecting the chain's middle
joint onto the start→end axis and taking the rejection vector — the
component perpendicular to the chain on its bend plane — scaled by chain
length. In Blender the constraint's `pole_angle` is also computed (signed
angle of the projected pole axis) so the chain doesn't snap when the pole
target is assigned.

**Muscle helper.** Creates a deform joint/bone halfway along the upper
limb whose scale is driven by the bend angle — set-driven keys in Maya,
a clamped scripted driver in Blender:
`1 + (bulge − 1) · min(|rot| / max_angle, 1)`. Add the helper to the
skin weights to see the bulge.

**Texel density.** `density = texture_size × √(uv_area / world_area)`,
area-weighted across the selection. Matching scales each object's UVs
uniformly around their UV-bounds center.

## Design notes

- Batch operations are single-undo: `undoInfo` chunking via decorator in
  Maya; native `REGISTER`/`UNDO` operator options in Blender.
- Settings persist between sessions (Maya `optionVar`; Blender JSON in the
  user config folder via Save/Apply Defaults). Destructive toggles are
  deliberately excluded and always reset to off.
- Both files run through strict linting and compile outside their DCC via
  guarded imports, so they can be syntax-checked in CI.

## Roadmap

- UV overlap detection with spatial hashing
- Self-intersection detection at pose time
- Houdini port of the validation suite

## Author

**Juan Abia Merino** — Technical Artist
[ArtStation](https://juanabiamerino.artstation.com) ·
[GitHub](https://github.com/J4ck4b14) ·
[LinkedIn](https://linkedin.com/in/juan-abia-merino)
