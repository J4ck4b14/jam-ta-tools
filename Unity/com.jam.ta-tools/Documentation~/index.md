# JAM TA Tools - Unity Bridge 2.4

This editor package reads `.jammeta.json` files exported by JAM TA Tools and compares DCC expectations with Unity's imported model.

It checks triangles, `Mesh.vertexCount`, material/submesh sections and UV-channel count, then measures actual vertex-buffer strides and index format to report engine-side mesh-buffer size beside JAM's reference estimate.

The bridge also reads material/texture metadata from JAM sidecars. It reports source texture count/missing-at-export/reference footprint and compares safe source texture filenames with `Texture2D` dependencies reachable from the imported model. That dependency match is observational because projects may remap materials and textures intentionally.

The bridge never changes `ModelImporter` settings automatically.

Use **Tools > JAM TA Tools > Validate Selected Model** to re-run validation for selected FBX assets.


Sidecars may also include export-format and modular-grid metadata. The Unity bridge treats those additive fields as context; model verification remains observational and does not rewrite importer settings.
