# JAM TA Tools

JAM TA Tools is a technical-art toolkit for game-asset validation, authoring and export across **Maya** and **Blender**, with verification bridges for **Unity** and **Unreal Engine**.

The DCC tools share a host-independent Python core for profiles, reporting, mesh and UV analysis, Asset Sets, export planning and `.jammeta.json` sidecars. Maya and Blender handle scene-specific work with their native APIs, while the engine bridges compare the exported expectations with the asset that was actually imported.

Built and maintained by **Juan Abia Merino**.

## Repository layout

```text
jam-ta-tools/
├── Blender/                 # Blender add-on and headless validator
├── Common/jam_ta_core/      # Shared host-independent Python core
├── Maya/                    # Maya tools, module file and headless validator
├── Unity/com.jam.ta-tools/  # Unity 6 editor package
├── Unreal/JAMTATools/       # Unreal editor validation plugin
├── docs/                    # Architecture notes
├── examples/                # Example project profiles and custom rules
├── tests/                   # Shared-core tests
└── build_release.py         # Builds installable packages into dist/
```

## Main features

- Asset Doctor validation profiles with configurable severities and project rules.
- Mesh checks for topology, transforms, LOD structure and common export problems.
- Render-vertex split analysis for UV seams, normals and material boundaries.
- UV layout, overlap, padding, lightmap and texel-density analysis.
- Material and texture inventory checks, including UDIM and source-reference metadata.
- Poly budgets and reference mesh-buffer cost estimates.
- Asset Sets for render meshes, LODs, collision, sockets, skeletons and helpers.
- Modular-kit grid and bounds validation.
- Validated FBX/USD export planning with `.jammeta.json` sidecars.
- Batch export with scene-state restoration.
- Pivot/origin tools, renaming utilities and rigging helpers.
- JSON and JUnit report output for command-line or CI validation.
- Unity and Unreal verification of DCC-side expectations after import.

## Installation

The recommended installation path is to download the package for the application you use from **GitHub Releases**. The repository itself is the development source tree.

### Maya

Download `JAM_TA_Tools_Maya_<version>.zip` and extract its contents into:

```text
C:\Users\<username>\Documents\maya\modules\
```

The resulting layout should be:

```text
Documents/maya/modules/
├── JAMTATools.mod
└── JAMTATools/
    ├── README.md
    └── scripts/
        ├── ta_tools.py
        ├── jam_ta_qt.py
        ├── jam_validate_maya.py
        └── jam_ta_core/
```

`JAMTATools.mod` must sit directly inside the `modules` folder. Restart Maya after installation.

Open **Windows > General Editors > Script Editor**, switch to the Python tab, and run:

```python
import ta_tools
ta_tools.show()
```

Maya 2025+ can also open the PySide6 Asset Doctor dashboard directly:

```python
import jam_ta_qt
jam_ta_qt.show_dashboard()
```

A shelf button can use the same `ta_tools.show()` call for one-click access.

### Blender

Download `JAM_TA_Tools_Blender_<version>.zip`.

In Blender 4.2+:

1. Open **Edit > Preferences > Add-ons**.
2. Choose **Install from Disk**.
3. Select the downloaded ZIP.
4. Enable **JAM TA Tools** if Blender does not enable it automatically.
5. Open the 3D Viewport sidebar with **N** and select the **TA Tools** tab.

The release package includes the shared `jam_ta_core` package, so the ZIP should be installed as a complete add-on rather than copying `ta_tools.py` by itself.

### Unity 6

Download `JAM_TA_Tools_Unity_<version>.zip` and extract it. The package is contained in:

```text
com.jam.ta-tools/
```

In Unity, open **Window > Package Manager**, use the **+** menu, choose **Add package from disk...**, and select:

```text
com.jam.ta-tools/package.json
```

The bridge reads `.jammeta.json` files placed next to imported model source files. Use **Tools > JAM TA Tools > Validate Selected Model** to run validation manually on selected models.

### Unreal Engine

Download `JAM_TA_Tools_Unreal_<version>.zip` and extract the `JAMTATools` folder into the project's plugin directory:

```text
<Project>/Plugins/JAMTATools/
```

Restart Unreal Engine and allow the editor to build the plugin if required. The plugin uses Unreal's Data Validation framework to compare imported Static Mesh and Skeletal Mesh data with sibling `.jammeta.json` sidecars.

## Building releases from source

Clone the repository and run:

```bash
python build_release.py
```

The script creates `dist/` and builds:

```text
JAM_TA_Tools_Blender_<version>.zip
JAM_TA_Tools_Maya_<version>.zip
JAM_TA_Tools_Unity_<version>.zip
JAM_TA_Tools_Unreal_<version>.zip
JAM_TA_Tools_V<version>_Source.zip
```

`dist/` contains release artifacts and does not need to be committed to the source repository.

## Validation and sidecars

Maya and Blender produce the same core report model. Exported assets can include a sibling `.jammeta.json` file containing validation status, mesh metrics, UV and texel-density data, material/texture information, Asset Set structure and export metadata.

The sidecar intentionally stores portable asset information rather than host objects or absolute private texture paths. Unity and Unreal use it as a comparison contract: DCC validation errors can be surfaced in-engine, while representation differences such as imported vertex counts remain visible without silently changing importer settings.

## Project profiles and custom rules

Built-in profiles provide general validation defaults. Project-specific policy can be supplied with JSON profiles, while Python rule modules can register checks that belong to a particular production rather than the base toolkit.

Examples are available in:

```text
examples/project_profiles.example.json
examples/project_rules.example.py
```

## Command-line validation

The Maya and Blender packages include headless validation entry points:

```text
Maya/jam_validate_maya.py
Blender/jam_validate_blend.py
```

They support project profiles, custom rule modules, JSON reports, JUnit output and warning-as-error gates so the same validation rules can be used in local tools and CI.

## Development

Shared-core tests can be run from the repository root with:

```bash
python -m unittest discover -s tests
```

For the design and data-flow breakdown, see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Author

**Juan Abia Merino** — Technical Artist  
[ArtStation](https://juanabiamerino.artstation.com) · [GitHub](https://github.com/J4ck4b14) · [LinkedIn](https://linkedin.com/in/juan-abia-merino)
