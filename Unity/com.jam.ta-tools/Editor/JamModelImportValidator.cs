using System;
using System.Collections.Generic;
using System.IO;
using UnityEditor;
using UnityEngine;
using UnityEngine.Rendering;

namespace JamTATools.Editor
{
    [Serializable]
    internal sealed class JamExportSidecar
    {
        public string schema;
        public string suite_version;
        public string source_host;
        public string profile_id;
        public string export_profile_id;
        public string export_format;
        public string export_extension;
        public string export_file;
        public JamAssetSetRecord asset_set;
        public JamAssetRecord[] assets;
    }


    [Serializable]
    internal sealed class JamAssetSetRecord
    {
        public string root_name;
        public string status;
        public JamAssetSetMetrics metrics;
        public JamAssetSetMember[] members;
    }

    [Serializable]
    internal sealed class JamAssetSetMetrics
    {
        public int member_count;
        public int export_member_count;
        public int render_member_count;
        public int lod_count;
        public int collision_count;
        public long collision_triangles;
        public int socket_count;
    }

    [Serializable]
    internal sealed class JamAssetSetMember
    {
        public string name;
        public string role;
        public int lod_level;
        public string collision_type;
        public bool export_enabled;
        public int triangles;
    }

    [Serializable]
    internal sealed class JamAssetRecord
    {
        public string name;
        public string status;
        public int error_count;
        public int warning_count;
        public JamAssetMetrics metrics;
        public JamTextureRecord[] textures;
    }

    [Serializable]
    internal sealed class JamTextureRecord
    {
        public string name;
        public string source_name;
        public string semantic;
        public int width;
        public int height;
        public string color_space;
        public bool is_udim;
        public int udim_tiles;
        public long reference_bytes;
    }

    [Serializable]
    internal sealed class JamAssetMetrics
    {
        public int model_vertices;
        public int triangles;
        public int render_vertices;
        public int material_slots;
        public int uv_channels;
        public int deform_bones;
        public int max_skin_influences;
        public long mesh_buffer_bytes;
        public int vertex_stride_bytes;
        public int index_size_bytes;
        public float texel_density_px_per_m;
        public float texel_density_spread_percent;
        public int texel_density_islands;
        public int uv_island_count;
        public int uv_overlap_faces;
        public float uv_utilization_percent;
        public float uv_min_padding_px;
        public int lightmap_uv_channel;
        public int lightmap_overlap_faces;
        public float lightmap_min_padding_px;
        public int material_count;
        public int texture_count;
        public int missing_texture_count;
        public int udim_texture_count;
        public long texture_reference_bytes;
        public long texture_uncompressed_bytes;
    }

    internal readonly struct JamImportedMetrics
    {
        public readonly int Vertices;
        public readonly long Triangles;
        public readonly int Sections;
        public readonly int UvChannels;
        public readonly long VertexBufferBytes;
        public readonly long IndexBufferBytes;

        public long MeshBufferBytes => VertexBufferBytes + IndexBufferBytes;

        public JamImportedMetrics(
            int vertices,
            long triangles,
            int sections,
            int uvChannels,
            long vertexBufferBytes,
            long indexBufferBytes)
        {
            Vertices = vertices;
            Triangles = triangles;
            Sections = sections;
            UvChannels = uvChannels;
            VertexBufferBytes = vertexBufferBytes;
            IndexBufferBytes = indexBufferBytes;
        }
    }

    internal static class JamModelValidation
    {
        private const string SidecarSuffix = ".jammeta.json";

        internal static bool TryLoadSidecar(string modelAssetPath, out JamExportSidecar sidecar)
        {
            sidecar = null;
            var sidecarAssetPath = GetSidecarAssetPath(modelAssetPath);
            var absolutePath = ToAbsoluteProjectPath(sidecarAssetPath);

            if (!File.Exists(absolutePath))
                return false;

            try
            {
                var json = File.ReadAllText(absolutePath);
                sidecar = JsonUtility.FromJson<JamExportSidecar>(json);
                return sidecar != null && sidecar.schema == "jam-ta-tools.export-sidecar.v3";
            }
            catch (Exception exception)
            {
                Debug.LogWarning($"[JAM TA] Could not read sidecar for {modelAssetPath}: {exception.Message}");
                return false;
            }
        }

        internal static string GetSidecarAssetPath(string modelAssetPath)
        {
            var extension = Path.GetExtension(modelAssetPath);
            return modelAssetPath.Substring(0, modelAssetPath.Length - extension.Length) + SidecarSuffix;
        }

        internal static string GetModelAssetPathFromSidecar(string sidecarAssetPath)
        {
            if (!sidecarAssetPath.EndsWith(SidecarSuffix, StringComparison.OrdinalIgnoreCase))
                return string.Empty;

            var basePath = sidecarAssetPath.Substring(0, sidecarAssetPath.Length - SidecarSuffix.Length);
            foreach (var extension in new[] { ".fbx", ".FBX" })
            {
                var candidate = basePath + extension;
                if (File.Exists(ToAbsoluteProjectPath(candidate)))
                    return candidate;
            }
            return string.Empty;
        }

        internal static JamImportedMetrics Measure(GameObject root, HashSet<string> expectedNames = null)
        {
            var meshes = new HashSet<Mesh>();
            foreach (var filter in root.GetComponentsInChildren<MeshFilter>(true))
            {
                if (filter.sharedMesh != null && (expectedNames == null || expectedNames.Count == 0 || expectedNames.Contains(filter.gameObject.name) || expectedNames.Contains(filter.sharedMesh.name)))
                    meshes.Add(filter.sharedMesh);
            }
            foreach (var renderer in root.GetComponentsInChildren<SkinnedMeshRenderer>(true))
            {
                if (renderer.sharedMesh != null && (expectedNames == null || expectedNames.Count == 0 || expectedNames.Contains(renderer.gameObject.name) || expectedNames.Contains(renderer.sharedMesh.name)))
                    meshes.Add(renderer.sharedMesh);
            }

            if (meshes.Count == 0 && expectedNames != null && expectedNames.Count > 0)
                return Measure(root, null);

            var vertices = 0;
            long triangles = 0;
            var sections = 0;
            var uvChannels = 0;
            long vertexBufferBytes = 0;
            long indexBufferBytes = 0;

            foreach (var mesh in meshes)
            {
                vertices += mesh.vertexCount;
                sections += mesh.subMeshCount;

                long meshIndexCount = 0;
                for (var subMesh = 0; subMesh < mesh.subMeshCount; subMesh++)
                {
                    var indexCount = (long)mesh.GetIndexCount(subMesh);
                    meshIndexCount += indexCount;
                    triangles += indexCount / 3L;
                }

                for (var stream = 0; stream < mesh.vertexBufferCount; stream++)
                    vertexBufferBytes += (long)mesh.vertexCount * mesh.GetVertexBufferStride(stream);

                var indexSize = mesh.indexFormat == IndexFormat.UInt16 ? 2L : 4L;
                indexBufferBytes += meshIndexCount * indexSize;

                var meshUvChannels = 0;
                for (var channel = 0; channel < 8; channel++)
                {
                    var attribute = (VertexAttribute)((int)VertexAttribute.TexCoord0 + channel);
                    if (mesh.HasVertexAttribute(attribute))
                        meshUvChannels++;
                }
                uvChannels = Math.Max(uvChannels, meshUvChannels);
            }

            return new JamImportedMetrics(
                vertices, triangles, sections, uvChannels, vertexBufferBytes, indexBufferBytes);
        }

        internal static void Validate(string assetPath, GameObject root, UnityEngine.Object context)
        {
            if (!TryLoadSidecar(assetPath, out var sidecar))
                return;

            var expectedVertices = 0;
            long expectedTriangles = 0;
            var expectedSections = 0;
            var expectedUvChannels = 0;
            long expectedReferenceBufferBytes = 0;
            var sourceErrors = 0;
            var expectedTextureCount = 0;
            var missingTextureCount = 0;
            long expectedTextureReferenceBytes = 0;

            foreach (var asset in sidecar.assets ?? Array.Empty<JamAssetRecord>())
            {
                if (asset?.metrics == null)
                    continue;
                expectedVertices += asset.metrics.render_vertices;
                expectedTriangles += asset.metrics.triangles;
                expectedSections += asset.metrics.material_slots;
                expectedUvChannels = Math.Max(expectedUvChannels, asset.metrics.uv_channels);
                expectedReferenceBufferBytes += Math.Max(0L, asset.metrics.mesh_buffer_bytes);
                sourceErrors += asset.error_count;
                expectedTextureCount += asset.metrics.texture_count;
                missingTextureCount += asset.metrics.missing_texture_count;
                expectedTextureReferenceBytes += Math.Max(0L, asset.metrics.texture_reference_bytes);
            }

            var expectedNames = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (var asset in sidecar.assets ?? Array.Empty<JamAssetRecord>())
            {
                if (asset != null && !string.IsNullOrEmpty(asset.name))
                    expectedNames.Add(asset.name);
            }

            var actual = Measure(root, expectedNames);
            var prefix = $"[JAM TA] {assetPath}";

            if (sidecar.asset_set != null && !string.IsNullOrEmpty(sidecar.asset_set.root_name))
            {
                var setMetrics = sidecar.asset_set.metrics;
                var setSummary = setMetrics == null
                    ? sidecar.asset_set.root_name
                    : $"{sidecar.asset_set.root_name}: {setMetrics.member_count} members, {setMetrics.lod_count} LOD levels, {setMetrics.collision_count} collision members";
                if (string.Equals(sidecar.asset_set.status, "ERROR", StringComparison.OrdinalIgnoreCase))
                    Debug.LogWarning($"{prefix}: source Asset Set contains structural validation errors. {setSummary}.", context);
                else
                    Debug.Log($"{prefix}: Asset Set {setSummary}.", context);
            }

            if (sourceErrors > 0)
                Debug.LogWarning($"{prefix}: sidecar records {sourceErrors} blocking DCC validation issue(s).", context);

            if (expectedTriangles > 0 && actual.Triangles != expectedTriangles)
            {
                Debug.LogWarning(
                    $"{prefix}: triangles differ. DCC {expectedTriangles:N0} -> Unity {actual.Triangles:N0}.",
                    context);
            }

            if (expectedVertices > 0)
            {
                var difference = Math.Abs(actual.Vertices - expectedVertices);
                var tolerance = Math.Max(8, (int)Math.Ceiling(expectedVertices * 0.02));
                if (difference > tolerance)
                {
                    Debug.LogWarning(
                        $"{prefix}: render vertices differ. DCC estimate {expectedVertices:N0} -> Unity {actual.Vertices:N0} " +
                        $"(delta {actual.Vertices - expectedVertices:+#;-#;0}).",
                        context);
                }
            }

            if (expectedSections > 0 && actual.Sections != expectedSections)
            {
                Debug.LogWarning(
                    $"{prefix}: material/submesh sections differ. DCC {expectedSections} -> Unity {actual.Sections}.",
                    context);
            }

            if (actual.UvChannels < expectedUvChannels)
            {
                Debug.LogWarning(
                    $"{prefix}: Unity exposes {actual.UvChannels} UV channel(s), sidecar expected {expectedUvChannels}.",
                    context);
            }

            var bufferNote = expectedReferenceBufferBytes > 0
                ? $" DCC reference buffers ~{FormatBytes(expectedReferenceBufferBytes)}; Unity mesh buffers ~{FormatBytes(actual.MeshBufferBytes)}."
                : $" Unity mesh buffers ~{FormatBytes(actual.MeshBufferBytes)}.";

            var textureNote = expectedTextureCount > 0
                ? $" Source textures = {expectedTextureCount}, missing at export = {missingTextureCount}, reference texture footprint ~{FormatBytes(expectedTextureReferenceBytes)}."
                : string.Empty;
            textureNote += BuildTextureDependencyNote(assetPath, sidecar);

            var exportPreset = string.IsNullOrEmpty(sidecar.export_profile_id)
                ? string.Empty
                : $" / export {sidecar.export_profile_id}";
            if (!string.IsNullOrEmpty(sidecar.export_format))
                exportPreset += $" ({sidecar.export_format})";
            Debug.Log(
                $"{prefix}: verified against {sidecar.source_host} / {sidecar.profile_id}{exportPreset}. " +
                $"Unity = {actual.Vertices:N0} verts, {actual.Triangles:N0} tris, {actual.Sections} sections, {actual.UvChannels} UVs." +
                bufferNote + textureNote,
                context);
        }

        private static string BuildTextureDependencyNote(string assetPath, JamExportSidecar sidecar)
        {
            var expected = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (var asset in sidecar.assets ?? Array.Empty<JamAssetRecord>())
            {
                foreach (var texture in asset?.textures ?? Array.Empty<JamTextureRecord>())
                {
                    if (!string.IsNullOrEmpty(texture?.source_name))
                        expected.Add(texture.source_name);
                }
            }
            if (expected.Count == 0)
                return string.Empty;

            var dependencies = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (var path in AssetDatabase.GetDependencies(assetPath, true))
            {
                if (AssetDatabase.LoadAssetAtPath<Texture2D>(path) != null)
                    dependencies.Add(Path.GetFileName(path));
            }

            var matched = 0;
            foreach (var sourceName in expected)
            {
                if (dependencies.Contains(sourceName))
                    matched++;
            }
            return $" Texture dependencies matched by source filename = {matched}/{expected.Count}.";
        }

        internal static void ValidatePersistedAsset(string assetPath)
        {
            var root = AssetDatabase.LoadAssetAtPath<GameObject>(assetPath);
            if (root != null)
                Validate(assetPath, root, root);
        }

        private static string FormatBytes(long byteCount)
        {
            double value = Math.Max(0L, byteCount);
            var units = new[] { "B", "KiB", "MiB", "GiB" };
            foreach (var unit in units)
            {
                if (value < 1024.0 || unit == units[units.Length - 1])
                    return unit == "B" ? $"{value:0} {unit}" : $"{value:0.00} {unit}";
                value /= 1024.0;
            }
            return $"{value:0.00} GiB";
        }

        private static string ToAbsoluteProjectPath(string assetPath)
        {
            var projectRoot = Directory.GetParent(Application.dataPath)?.FullName ?? string.Empty;
            return Path.GetFullPath(Path.Combine(projectRoot, assetPath));
        }
    }

    internal sealed class JamModelImportValidator : AssetPostprocessor
    {
        private void OnPostprocessModel(GameObject root)
        {
            JamModelValidation.Validate(assetPath, root, root);
        }

        private static void OnPostprocessAllAssets(
            string[] importedAssets,
            string[] deletedAssets,
            string[] movedAssets,
            string[] movedFromAssetPaths)
        {
            foreach (var path in importedAssets)
            {
                if (!path.EndsWith(".jammeta.json", StringComparison.OrdinalIgnoreCase))
                    continue;

                var modelPath = JamModelValidation.GetModelAssetPathFromSidecar(path);
                if (!string.IsNullOrEmpty(modelPath))
                    JamModelValidation.ValidatePersistedAsset(modelPath);
            }
        }
    }

    internal static class JamModelValidationMenu
    {
        [MenuItem("Tools/JAM TA Tools/Validate Selected Model", true)]
        private static bool CanValidateSelected()
        {
            foreach (var selected in Selection.objects)
            {
                var path = AssetDatabase.GetAssetPath(selected);
                if (path.EndsWith(".fbx", StringComparison.OrdinalIgnoreCase))
                    return true;
            }
            return false;
        }

        [MenuItem("Tools/JAM TA Tools/Validate Selected Model")]
        private static void ValidateSelected()
        {
            foreach (var selected in Selection.objects)
            {
                var path = AssetDatabase.GetAssetPath(selected);
                if (!path.EndsWith(".fbx", StringComparison.OrdinalIgnoreCase))
                    continue;
                JamModelValidation.ValidatePersistedAsset(path);
            }
        }
    }
}
