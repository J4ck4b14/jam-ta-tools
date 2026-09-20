import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CORE = os.path.join(ROOT, "Common")
if CORE not in sys.path:
    sys.path.insert(0, CORE)

from jam_ta_core import (  # noqa: E402
    AssetReport,
    ValidationIssue,
    analyze_render_splits,
    estimate_mesh_memory,
    summarize_texel_density,
    texel_density_color,
    build_export_sidecar,
    build_report_document,
    get_profile,
    sidecar_path_for_export,
    write_export_sidecar,
    write_report_json,
    AssetMemberSpec,
    infer_asset_member,
    canonical_root_from_members,
    analyze_asset_set,
    analyze_material_inventory,
    estimate_texture_memory,
    infer_texture_semantic,
    build_junit_xml,
    load_profiles_json,
    register_rule, clear_registered_rules, run_registered_rules, registered_rule_ids,
    analyze_uv_layout, summarize_island_texel_density,
    snap_scalar, snap_vector, analyze_modular_bounds, bounds_anchor, bounds_scale_factors, make_attachment_name,
    get_export_profile, export_extension, export_format, build_export_plan, format_export_plan,
)


class SplitAnalysisTests(unittest.TestCase):
    def test_progressive_splits_add_up(self):
        corners = [
            {"vertex": 0, "normal": (0, 0, 1), "uvs": [(0, 0)], "material": 0},
            {"vertex": 1, "normal": (0, 0, 1), "uvs": [(1, 0)], "material": 0},
            {"vertex": 2, "normal": (0, 0, 1), "uvs": [(1, 1)], "material": 0},
            # vertex 0 appears again with a different normal and UV: two reasons to split
            {"vertex": 0, "normal": (0, 1, 0), "uvs": [(0.5, 0.5)], "material": 1},
        ]
        result = analyze_render_splits(corners, ["UV0"], include_material=True)
        self.assertEqual(result["base_vertices"], 3)
        self.assertEqual(result["render_vertices"], 4)
        self.assertEqual(sum(stage["added"] for stage in result["stages"]), 1)

    def test_material_split_is_attributed(self):
        corners = [
            {"vertex": 0, "normal": (0, 0, 1), "uvs": [], "material": 0},
            {"vertex": 0, "normal": (0, 0, 1), "uvs": [], "material": 1},
        ]
        result = analyze_render_splits(corners, [], include_material=True)
        self.assertEqual(result["base_vertices"], 1)
        self.assertEqual(result["render_vertices"], 2)
        self.assertEqual(result["stages"][-1]["added"], 1)


class CostAndTexelTests(unittest.TestCase):
    def test_reference_mesh_memory_grows_with_uvs_and_skinning(self):
        static = estimate_mesh_memory(1000, 500, uv_channels=1, has_skinning=False)
        skinned = estimate_mesh_memory(1000, 500, uv_channels=2, has_skinning=True)
        self.assertGreater(skinned["vertex_stride_bytes"], static["vertex_stride_bytes"])
        self.assertGreater(skinned["mesh_buffer_bytes"], static["mesh_buffer_bytes"])

    def test_heat_color_is_green_near_target(self):
        r, g, b = texel_density_color(1000.0, 1000.0)
        self.assertGreater(g, r)
        self.assertGreater(g, b)

    def test_texel_density_normalized_to_metres_and_detects_outliers(self):
        samples = [
            {"component": 0, "world_area_m2": 1.0, "uv_area": 0.25},
            {"component": 1, "world_area_m2": 1.0, "uv_area": 0.25},
            {"component": 2, "world_area_m2": 1.0, "uv_area": 1.0},
        ]
        result = summarize_texel_density(
            samples,
            texture_size=1024,
            target_px_per_m=512.0,
            tolerance_percent=10.0,
        )
        self.assertAlmostEqual(result["density_px_per_m"], 1024.0 * (1.5 / 3.0) ** 0.5)
        self.assertEqual(result["outlier_components"], [2])


class MaterialAnalysisTests(unittest.TestCase):
    def test_semantic_and_reference_memory(self):
        self.assertEqual(infer_texture_semantic("Robot_BaseColor.png"), "base_color")
        self.assertEqual(infer_texture_semantic("Robot_Normal_1001.exr"), "normal")
        normal = estimate_texture_memory(2048, 2048, semantic="normal")
        color = estimate_texture_memory(2048, 2048, semantic="base_color")
        self.assertGreater(normal["game_reference_bytes"], 0)
        self.assertEqual(normal["reference_format"], "BC5")
        self.assertEqual(color["reference_format"], "BC7/BC3")

    def test_inventory_reports_missing_colorspace_and_packing(self):
        result = analyze_material_inventory(
            [{
                "name": "M_Robot",
                "textures": [
                    {"name": "Robot_AO", "path": "/missing/Robot_AO.png", "width": 1024, "height": 1024, "semantic": "ao", "color_space": "sRGB", "exists": False},
                    {"name": "Robot_Roughness", "path": "/tmp/Robot_Roughness.png", "width": 1024, "height": 1024, "semantic": "roughness", "color_space": "Raw", "exists": True},
                    {"name": "Robot_Metallic", "path": "/tmp/Robot_Metallic.png", "width": 1024, "height": 1024, "semantic": "metallic", "color_space": "Raw", "exists": True},
                ],
            }],
            profile={"prefer_mask_packing": True},
            object_name="SM_Robot",
        )
        rule_ids = {issue.rule_id for issue in result["issues"]}
        self.assertIn("texture.missing_source", rule_ids)
        self.assertIn("texture.color_space_data", rule_ids)
        self.assertIn("material.mask_pack_candidate", rule_ids)
        self.assertEqual(result["metrics"]["texture_count"], 3)
        self.assertGreater(result["metrics"]["texture_reference_bytes"], 0)

    def test_junit_marks_errors_as_failures(self):
        report = AssetReport("SM_Broken", "test", "GENERIC_GAME")
        report.add_issue(ValidationIssue(
            "texture.missing_source", "Missing", "No file", "Textures", "ERROR"
        ))
        xml = build_junit_xml([report])
        self.assertIn('failures="1"', xml)
        self.assertIn("texture.missing_source", xml)


class AssetSetTests(unittest.TestCase):
    def test_naming_inference(self):
        lod = infer_asset_member("SM_Crate_LOD2")
        self.assertEqual(lod.root_name, "SM_Crate")
        self.assertEqual(lod.role, "LOD")
        self.assertEqual(lod.lod_level, 2)

        collision = infer_asset_member("UCX_SM_Crate_03")
        self.assertEqual(collision.root_name, "SM_Crate")
        self.assertEqual(collision.role, "COLLISION")
        self.assertEqual(collision.collision_type, "CONVEX")

    def test_canonical_root_prefers_active_render(self):
        names = ["UCX_SM_Crate_00", "SM_Crate_LOD1", "SM_Crate"]
        self.assertEqual(canonical_root_from_members(names, "SM_Crate"), "SM_Crate")

    def test_multi_part_lod_uses_aggregate_triangles(self):
        result = analyze_asset_set(
            "SM_Car",
            [
                AssetMemberSpec("SM_Car_Body", "SM_Car", role="RENDER", lod_level=0, triangles=700),
                AssetMemberSpec("SM_Car_Glass", "SM_Car", role="RENDER", lod_level=0, triangles=300),
                AssetMemberSpec("SM_Car_Body_LOD1", "SM_Car", role="LOD", lod_level=1, triangles=350),
                AssetMemberSpec("SM_Car_Glass_LOD1", "SM_Car", role="LOD", lod_level=1, triangles=100),
            ],
            min_lod_reduction_percent=20.0,
        )
        self.assertEqual(result["status"], "CLEAN")
        self.assertEqual(result["metrics"]["lod_metrics"][0]["triangles"], 1000)
        self.assertEqual(result["metrics"]["lod_metrics"][1]["triangles"], 450)
        self.assertEqual(result["metrics"]["lod_metrics"][0]["member_count"], 2)

    def test_lod_progression_and_collision_validation(self):
        result = analyze_asset_set(
            "SM_Crate",
            [
                AssetMemberSpec("SM_Crate", "SM_Crate", role="RENDER", lod_level=0, triangles=1000),
                AssetMemberSpec("SM_Crate_LOD1", "SM_Crate", role="LOD", lod_level=1, triangles=800),
                AssetMemberSpec("SM_Crate_LOD2", "SM_Crate", role="LOD", lod_level=2, triangles=900),
                AssetMemberSpec("UCX_SM_Crate_00", "SM_Crate", role="COLLISION", collision_type="CONVEX", triangles=32, is_closed=True, is_convex=False),
            ],
            min_lod_reduction_percent=30.0,
        )
        rule_ids = {issue.rule_id for issue in result["issues"]}
        self.assertIn("asset_set.lod_weak_reduction", rule_ids)
        self.assertIn("asset_set.lod_not_reduced", rule_ids)
        self.assertIn("asset_set.collision_nonconvex", rule_ids)
        self.assertEqual(result["metrics"]["lod_count"], 3)
        self.assertEqual(result["metrics"]["collision_count"], 1)




class UVLayoutTests(unittest.TestCase):
    def test_islands_overlap_utilization_and_padding(self):
        faces = [
            {"component": 0, "vertices": [0, 1, 2, 3], "uvs": [(0.0, 0.0), (0.4, 0.0), (0.4, 0.4), (0.0, 0.4)]},
            {"component": 1, "vertices": [4, 5, 6, 7], "uvs": [(0.6, 0.0), (1.0, 0.0), (1.0, 0.4), (0.6, 0.4)]},
        ]
        result = analyze_uv_layout(faces, texture_size=1000)
        self.assertEqual(result["island_count"], 2)
        self.assertEqual(result["overlap_pair_count"], 0)
        self.assertAlmostEqual(result["utilization_percent"], 32.0, places=4)
        self.assertAlmostEqual(result["min_island_padding_px"], 200.0, places=4)

        faces[1]["uvs"] = [(0.2, 0.0), (0.6, 0.0), (0.6, 0.4), (0.2, 0.4)]
        overlapped = analyze_uv_layout(faces, texture_size=1000)
        self.assertEqual(overlapped["overlap_pair_count"], 1)
        self.assertEqual(set(overlapped["overlap_components"]), {0, 1})

    def test_uv_seam_builds_separate_islands_and_td_aggregates(self):
        faces = [
            {"component": 0, "vertices": [0, 1, 2], "uvs": [(0.0, 0.0), (0.5, 0.0), (0.0, 0.5)]},
            {"component": 1, "vertices": [1, 3, 2], "uvs": [(0.6, 0.0), (1.0, 0.0), (0.6, 0.4)]},
        ]
        result = analyze_uv_layout(faces, texture_size=1024)
        self.assertEqual(result["island_count"], 2)
        td = summarize_island_texel_density(
            [
                {"component": 0, "world_area_m2": 1.0, "uv_area": 0.125},
                {"component": 1, "world_area_m2": 1.0, "uv_area": 0.08},
            ],
            result["islands"], 1024,
        )
        self.assertEqual(len(td), 2)
        self.assertGreater(td[0]["density_px_per_m"], td[1]["density_px_per_m"])


class ModularAuthoringTests(unittest.TestCase):
    def test_snap_handles_negative_half_cells_consistently(self):
        self.assertEqual(snap_scalar(149.0, 100.0), 100.0)
        self.assertEqual(snap_scalar(151.0, 100.0), 200.0)
        self.assertEqual(snap_scalar(-150.0, 100.0), -200.0)
        self.assertEqual(snap_vector((151.0, 49.0, 12.0), 100.0, (True, True, False)), (200.0, 0.0, 12.0))

    def test_modular_bounds_checks_dimensions_and_world_alignment(self):
        clean = analyze_modular_bounds((0.0, 0.0, 0.0), (200.0, 100.0, 300.0), 100.0, 0.01)
        self.assertTrue(clean["is_modular"])

        shifted = analyze_modular_bounds((25.0, 0.0, 0.0), (225.0, 100.0, 300.0), 100.0, 0.01)
        self.assertTrue(shifted["dimensions_on_grid"])
        self.assertFalse(shifted["position_on_grid"])
        self.assertEqual(shifted["position_error_axes"], ["X"])


    def test_export_profiles_carry_real_format_and_extension(self):
        self.assertEqual(export_format("UNREAL_STATIC"), "FBX")
        self.assertEqual(export_extension("UNREAL_STATIC"), ".fbx")
        self.assertEqual(export_format("GENERIC_GLTF"), "GLB")
        self.assertEqual(export_extension("GENERIC_GLTF"), ".glb")
        self.assertEqual(get_export_profile("GENERIC_USD")["format"], "USD")


    def test_export_plan_surfaces_overwrite_state(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "SM_Crate.obj")
            open(path, "w", encoding="utf-8").close()
            plan = build_export_plan(folder, "SM_Crate", "GENERIC_OBJ", ["SM_Crate"], True)
            self.assertTrue(plan["export_exists"])
            self.assertTrue(plan["has_conflict"])
            self.assertEqual(plan["format"], "OBJ")
            self.assertEqual(plan["member_count"], 1)
            self.assertIn("export exists", format_export_plan(plan))

            os.remove(path)
            open(os.path.join(folder, "SM_Crate.jammeta.json"), "w", encoding="utf-8").close()
            sidecar_only = build_export_plan(folder, "SM_Crate", "GENERIC_OBJ", [], True)
            self.assertFalse(sidecar_only["export_exists"])
            self.assertTrue(sidecar_only["sidecar_exists"])
            self.assertTrue(sidecar_only["has_conflict"])

    def test_bounds_anchors_and_attachment_naming(self):
        self.assertEqual(bounds_anchor((0, 0, 0), (2, 4, 6), "BOTTOM"), (1.0, 2.0, 0.0))
        self.assertEqual(bounds_scale_factors((2, 4, 5), (4, 2, 10), (True, False, True)), (2.0, 1.0, 2.0))
        self.assertEqual(make_attachment_name("SM Door", "Handle A", "SOCKET"), "SOCKET_SM_Door_Handle_A")
        self.assertEqual(make_attachment_name("SM_Door", "FX", "HELPER"), "HELP_SM_Door_FX")


class ReportTests(unittest.TestCase):
    def test_report_summary_and_json(self):
        report = AssetReport("SM_Crate", "test", "GENERIC_GAME")
        report.add_issue(ValidationIssue(
            "transform.scale",
            "Scale not applied",
            "Scale is not 1,1,1",
            "Transform",
            "WARNING",
        ))
        document = build_report_document([report])
        self.assertEqual(document["asset_count"], 1)
        self.assertEqual(document["warning_count"], 1)

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "report.json")
            self.assertEqual(write_report_json(path, [report]), path)
            self.assertTrue(os.path.isfile(path))

    def test_profile_copy_isolated(self):
        profile = get_profile("STRICT_01_STATIC")
        profile["require_uv_01"] = False
        self.assertTrue(get_profile("STRICT_01_STATIC")["require_uv_01"])


    def test_project_profiles_load_from_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "profiles.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"profiles":{"PROJECT_PROP":{"label":"Project Prop","max_texture_dimension":1024}}}')
            loaded = load_profiles_json(path)
            self.assertEqual(loaded, ("PROJECT_PROP",))
            self.assertEqual(get_profile("PROJECT_PROP")["max_texture_dimension"], 1024)


    def test_custom_rule_registry_and_failure_boundary(self):
        clear_registered_rules()

        def project_rule(report, profile, snapshot):
            if snapshot["metrics"].get("triangles", 0) > 100:
                return ValidationIssue(
                    "project.triangles", "Project triangle cap",
                    "This project only allows 100 triangles here.",
                    "Project", "WARNING",
                )
            return None

        register_rule("project.triangles", project_rule)
        self.assertEqual(registered_rule_ids(), ("project.triangles",))
        report = AssetReport("SM_Test", "test", "GENERIC_GAME")
        report.metrics["triangles"] = 101
        run_registered_rules(report, get_profile("GENERIC_GAME"))
        self.assertEqual(report.warning_count, 1)

        def broken_rule(report, profile, snapshot):
            raise RuntimeError("boom")

        register_rule("project.broken", broken_rule)
        report2 = AssetReport("SM_Test", "test", "GENERIC_GAME")
        run_registered_rules(report2, get_profile("GENERIC_GAME"))
        self.assertEqual(report2.error_count, 1)
        self.assertTrue(any(i.rule_id == "extension.project.broken.exception" for i in report2.issues))
        clear_registered_rules()

    def test_export_sidecar_contains_engine_metrics(self):
        report = AssetReport("SM_Crate", "test", "UNITY_STATIC")
        report.metrics.update({
            "model_vertices": 24,
            "triangles": 12,
            "render_vertices": 36,
            "material_slots": 2,
            "uv_channels": 2,
            "mesh_buffer_bytes": 4096,
            "texel_density_px_per_m": 1024.0,
            "modular_grid_cm": 100.0,
            "modular_dimensions_cm": [200.0, 100.0, 300.0],
            "modular_dimensions_on_grid": True,
            "modular_position_on_grid": False,
        })
        report.add_issue(ValidationIssue(
            "uv.overlap",
            "UV overlap",
            "Lightmap UVs overlap",
            "UV",
            "WARNING",
        ))

        document = build_export_sidecar(
            "/tmp/SM_Crate.fbx",
            [report],
            source_host="Blender",
            profile_id="UNITY_STATIC",
            export_profile_id="UNITY_STATIC",
            asset_set={
                "root_name": "SM_Crate",
                "status": "CLEAN",
                "metrics": {"member_count": 3, "lod_count": 2, "collision_count": 1},
            },
        )
        self.assertEqual(document["schema"], "jam-ta-tools.export-sidecar.v3")
        self.assertEqual(document["export_profile_id"], "UNITY_STATIC")
        self.assertEqual(document["export_format"], "FBX")
        self.assertEqual(document["export_extension"], ".fbx")
        self.assertEqual(document["assets"][0]["metrics"]["render_vertices"], 36)
        self.assertEqual(document["assets"][0]["metrics"]["mesh_buffer_bytes"], 4096)
        self.assertEqual(document["assets"][0]["metrics"]["modular_dimensions_cm"], [200.0, 100.0, 300.0])
        self.assertFalse(document["assets"][0]["metrics"]["modular_position_on_grid"])
        self.assertEqual(document["assets"][0]["metrics"]["texel_density_px_per_m"], 1024.0)
        self.assertEqual(document["assets"][0]["warning_count"], 1)
        self.assertEqual(document["asset_set"]["root_name"], "SM_Crate")
        self.assertEqual(sidecar_path_for_export("/tmp/SM_Crate.fbx"), "/tmp/SM_Crate.jammeta.json")
        self.assertEqual(sidecar_path_for_export("/tmp/SM_Crate.glb"), "/tmp/SM_Crate.jammeta.json")

        with tempfile.TemporaryDirectory() as folder:
            export_path = os.path.join(folder, "SM_Crate.fbx")
            sidecar_path = write_export_sidecar(
                export_path,
                [report],
                source_host="Blender",
                profile_id="UNITY_STATIC",
            )
            self.assertTrue(os.path.isfile(sidecar_path))


if __name__ == "__main__":
    unittest.main()
