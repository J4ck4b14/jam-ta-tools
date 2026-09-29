"""Build self-contained JAM TA Tools release packages."""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION = "2.4.0"
DIST = ROOT / "dist"
CORE = ROOT / "Common" / "jam_ta_core"
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _copy_core(destination: Path) -> None:
    shutil.copytree(CORE, destination / "jam_ta_core", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _write_clean_entry(archive: zipfile.ZipFile, path: Path, archive_name: str) -> None:
    info = zipfile.ZipInfo(archive_name, ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.extra = b""
    info.comment = b""
    archive.writestr(info, path.read_bytes())


def _zip_tree(source: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.comment = b""
        for path in sorted(source.rglob("*")):
            if path.is_file():
                _write_clean_entry(archive, path, path.relative_to(source).as_posix())


def build_blender() -> Path:
    output = DIST / f"JAM_TA_Tools_Blender_{VERSION}.zip"
    with tempfile.TemporaryDirectory() as folder:
        stage = Path(folder)
        for name in ("__init__.py", "ta_tools.py", "blender_manifest.toml", "jam_validate_blend.py"):
            shutil.copy2(ROOT / "Blender" / name, stage / name)
        _copy_core(stage)
        shutil.copy2(ROOT / "README.md", stage / "README.md")
        _zip_tree(stage, output)
    return output


def build_maya() -> Path:
    output = DIST / f"JAM_TA_Tools_Maya_{VERSION}.zip"
    with tempfile.TemporaryDirectory() as folder:
        stage = Path(folder)
        shutil.copy2(ROOT / "Maya" / "JAMTATools.mod", stage / "JAMTATools.mod")
        scripts = stage / "JAMTATools" / "scripts"
        scripts.mkdir(parents=True)
        for name in ("ta_tools.py", "jam_validate_maya.py", "jam_ta_qt.py"):
            shutil.copy2(ROOT / "Maya" / name, scripts / name)
        _copy_core(scripts)
        shutil.copy2(ROOT / "README.md", stage / "JAMTATools" / "README.md")
        _zip_tree(stage, output)
    return output


def build_unity() -> Path:
    output = DIST / f"JAM_TA_Tools_Unity_{VERSION}.zip"
    source = ROOT / "Unity" / "com.jam.ta-tools"
    with tempfile.TemporaryDirectory() as folder:
        stage = Path(folder) / "com.jam.ta-tools"
        shutil.copytree(source, stage)
        _zip_tree(Path(folder), output)
    return output


def build_unreal() -> Path:
    output = DIST / f"JAM_TA_Tools_Unreal_{VERSION}.zip"
    source = ROOT / "Unreal" / "JAMTATools"
    with tempfile.TemporaryDirectory() as folder:
        stage = Path(folder) / "JAMTATools"
        shutil.copytree(source, stage)
        _zip_tree(Path(folder), output)
    return output


def build_source() -> Path:
    output = DIST / f"JAM_TA_Tools_V{VERSION}_Source.zip"
    if output.exists():
        output.unlink()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.comment = b""
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            parts = set(relative.parts)
            if "dist" in parts or "__pycache__" in parts or ".git" in parts:
                continue
            if path.suffix in {".pyc", ".zip"}:
                continue
            archive_name = (Path(f"JAM_TA_Tools_V{VERSION}") / relative).as_posix()
            _write_clean_entry(archive, path, archive_name)
    return output


def main() -> None:
    DIST.mkdir(exist_ok=True)
    outputs = [build_blender(), build_maya(), build_unity(), build_unreal(), build_source()]
    for output in outputs:
        print(output)


if __name__ == "__main__":
    main()
