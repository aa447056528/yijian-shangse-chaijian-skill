"""Read actual STL exports in a disposable background Blender process.

Usage: blender -b --factory-startup --python-exit-code 1 --python check_stl_exports.py -- \
       STL_DIRECTORY REPORT.json [--manifest manifest.json]

STL coordinates are interpreted as millimetres; units are not inferred. PASS
covers these basic checks only, never self-intersection or manufacturability.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys


TOLERANCES = {
    "degenerate_face_area_mm2": 1e-10,
    "positive_signed_volume_mm3": 1e-9,
    "lowest_plane_distance_mm": 0.01,
}


def parse_args():
    # Blender consumes its arguments before --; plain Python can run preflight.
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Directory of exported STL files")
    parser.add_argument("report", type=Path, help="Output JSON report")
    parser.add_argument("--manifest", type=Path, help="JSON object with expected exports")
    return parser.parse_args(argv)


def same_path(left, right):
    if left.resolve() == right.resolve():
        return True
    # samefile also catches hard links and case aliases on macOS/Windows.
    try:
        return left.samefile(right)
    except OSError:
        return False


def load_manifest(path, report):
    """Return validated entries, or None; malformed manifests always fail closed."""
    if path is None:
        return None
    report["completeness_status"] = "FAIL"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        exports = document.get("exports") if isinstance(document, dict) else None
        if not isinstance(exports, list) or not exports:
            raise ValueError("manifest must be an object with a nonempty exports array")
        entries = {}
        for index, item in enumerate(exports):
            if not isinstance(item, dict):
                raise ValueError(f"exports[{index}] must be an object")
            name, part_id = item.get("file"), item.get("part_id")
            if (not isinstance(name, str) or not name or name in {".", ".."}
                    or "/" in name or "\\" in name or ":" in name or "\x00" in name
                    or Path(name).suffix.lower() != ".stl"):
                raise ValueError(f"exports[{index}].file must be a root-level STL filename")
            if name in entries:
                raise ValueError(f"duplicate manifest filename: {name}")
            if not isinstance(part_id, str) or not part_id.strip():
                raise ValueError(f"exports[{index}].part_id must be a nonempty string")
            if "sha256" in item:
                digest = item["sha256"]
                if (not isinstance(digest, str) or len(digest) != 64
                        or any(c not in "0123456789abcdefABCDEF" for c in digest)):
                    raise ValueError(f"exports[{index}].sha256 must contain 64 hexadecimal digits; omit if unknown")
            entries[name] = item
        return entries
    except (OSError, ValueError) as exc:
        report["errors"].append(f"Manifest error: {exc}")
        return None


def base_row(path, expected):
    return {
        "file": path.name,
        "part_id": expected.get("part_id") if expected else None,
        "sha256": None,
        "sha256_status": "NOT_RUN",
        "faces": None,
        "bounds_mm": None,
        "dimensions_mm": None,
        "components_vertices": [],
        "nonmanifold_edges": None,
        "nonmanifold_vertices": None,
        "inconsistent_normal_edges": None,
        "degenerate_faces": None,
        "signed_volume_mm3": None,
        "lowest_plane_area_mm2": None,
        "orientation_status": "NOT_RUN",
        "topology_status": "NOT_RUN",
        "status": "FAIL",
        "self_intersection": "NOT_RUN",
        "art": "NOT_RUN",
        "import_settings": {},
        "errors": [],
    }


def components(bm):
    unseen, counts = set(bm.verts), []
    while unseen:
        stack, count = [unseen.pop()], 0
        while stack:
            vertex = stack.pop()
            count += 1
            for edge in vertex.link_edges:
                other = edge.other_vert(vertex)
                if other in unseen:
                    unseen.remove(other)
                    stack.append(other)
        counts.append(count)
    return sorted(counts, reverse=True)


def import_options(bpy):
    try:
        operator = bpy.ops.wm.stl_import
        properties = operator.get_rna_type().properties
    except (AttributeError, RuntimeError) as exc:
        raise RuntimeError("This script requires Blender's wm.stl_import operator") from exc
    requested = {
        "global_scale": 1.0,
        "use_scene_unit": False,
        "forward_axis": "Y",
        "up_axis": "Z",
        "use_mesh_validate": False,
    }
    # Recent versions name this use_scene_unit; inspect RNA instead of assuming.
    if "use_scene_unit" not in properties and "import_scene_unit" in properties:
        requested["import_scene_unit"] = requested.pop("use_scene_unit")
    missing = [name for name in requested if name not in properties]
    if missing:
        raise RuntimeError("Unsupported STL importer; cannot explicitly control "
                           + ", ".join(missing)
                           + ". Mesh validation must be disabled to preserve source defects.")
    return requested


def inspect_file(path, expected, bpy, bmesh, options):
    row = base_row(path, expected)
    row["import_settings"] = dict(options)
    bm = None
    try:
        row["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected and "sha256" in expected:
            row["sha256_status"] = "PASS" if row["sha256"] == expected["sha256"].lower() else "FAIL"
            if row["sha256_status"] == "FAIL":
                row["errors"].append("SHA-256 does not match the manifest")
        bpy.ops.wm.read_factory_settings(use_empty=True)
        result = bpy.ops.wm.stl_import(filepath=str(path), **options)
        if "FINISHED" not in result:
            raise ValueError(f"STL import did not finish: {sorted(result)}")
        meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
        if len(meshes) != 1:
            raise ValueError(f"STL import must produce one mesh object; got {len(meshes)}")
        bm = bmesh.new()
        bm.from_mesh(meshes[0].data)
        row["faces"] = len(bm.faces)
        if not bm.verts or not bm.faces:
            raise ValueError("STL contains no usable vertices or faces")
        if not all(math.isfinite(value) for vertex in bm.verts for value in vertex.co):
            raise ValueError("STL has nonfinite vertex coordinates")

        lo = [min(vertex.co[k] for vertex in bm.verts) for k in range(3)]
        hi = [max(vertex.co[k] for vertex in bm.verts) for k in range(3)]
        areas = [face.calc_area() for face in bm.faces]
        volume = bm.calc_volume(signed=True)
        if not all(math.isfinite(value) for value in areas + [volume]):
            raise ValueError("Geometry produced nonfinite face areas or signed volume")
        counts = components(bm)
        non_edges = sum(not edge.is_manifold for edge in bm.edges)
        non_vertices = sum(not vertex.is_manifold for vertex in bm.verts)
        bad_normals = sum(edge.is_manifold and not edge.is_contiguous for edge in bm.edges)
        degenerate = sum(area <= TOLERANCES["degenerate_face_area_mm2"] for area in areas)
        row.update({
            "bounds_mm": [lo, hi],
            "dimensions_mm": [hi[k] - lo[k] for k in range(3)],
            "components_vertices": counts,
            "nonmanifold_edges": non_edges,
            "nonmanifold_vertices": non_vertices,
            "inconsistent_normal_edges": bad_normals,
            "degenerate_faces": degenerate,
            "signed_volume_mm3": volume,
            "lowest_plane_area_mm2": sum(
                area for face, area in zip(bm.faces, areas)
                if all(abs(vertex.co.z - lo[2]) <= TOLERANCES["lowest_plane_distance_mm"]
                       for vertex in face.verts)
            ),
        })
        topology_errors = []
        if len(counts) != 1:
            topology_errors.append(
                f"Basic gate requires one connected mesh shell; found {len(counts)}. "
                "Multiple shells do not by themselves prove disconnected material; "
                "cavities and nested shells need a separate analysis.")
        if non_edges:
            topology_errors.append(f"Nonmanifold edges: {non_edges}")
        if non_vertices:
            topology_errors.append(f"Nonmanifold vertices: {non_vertices}")
        if bad_normals:
            topology_errors.append(f"Inconsistent face winding across edges: {bad_normals}")
        if degenerate:
            topology_errors.append(f"Degenerate faces: {degenerate}")
        if volume <= TOLERANCES["positive_signed_volume_mm3"]:
            topology_errors.append("Signed volume must be positive and exceed the numerical tolerance")
        if len(counts) == 1 and not (non_edges or non_vertices or bad_normals or degenerate):
            row["orientation_status"] = "PASS" if volume > TOLERANCES["positive_signed_volume_mm3"] else "FAIL"
        else:
            row["orientation_status"] = "NOT_RUN"
        row["topology_status"] = "FAIL" if topology_errors else "PASS"
        row["errors"].extend(topology_errors)
        row["status"] = "FAIL" if row["errors"] else "PASS"
    except Exception as exc:
        row["errors"].append(f"STL inspection error ({type(exc).__name__}): {exc}")
        row["status"] = "FAIL"
    finally:
        if bm is not None:
            bm.free()
    return row


def main():
    args = parse_args()
    root, output = args.root.resolve(), args.report.resolve()
    if output.suffix.lower() != ".json":
        raise ValueError("Report path must use a .json extension; choose a separate JSON output")
    report = {
        "schema_version": 1,
        "scope": "STL_BASIC_CHECKS_ONLY",
        "geometry_basis": "Blender-imported mesh with mesh validation disabled; the importer may merge vertices or discard triangles. This is not a lossless raw-STL triangle audit.",
        "status": "FAIL",
        "completeness_status": "NOT_RUN",
        "errors": [],
        "missing_files": [],
        "unexpected_files": [],
        "files": [],
        "blender_version": None,
        "units": "STL coordinates are assumed to be millimetres; no unit inference or rescaling",
        "tolerances": TOLERANCES,
        "self_intersection": "NOT_RUN",
        "art": "NOT_RUN",
    }
    actual = []
    try:
        if not root.is_dir():
            raise ValueError(f"STL root is not a directory: {root}")
        actual = sorted((path for path in root.iterdir()
                         if path.is_file() and path.suffix.lower() == ".stl"),
                        key=lambda path: path.name)
        if not actual:
            report["errors"].append("No root-level STL files found; empty input is not a successful check")
    except (OSError, ValueError) as exc:
        report["errors"].append(str(exc))

    # An unsafe destination cannot receive an error report without damaging input.
    protected = actual + ([args.manifest] if args.manifest is not None else [])
    if any(same_path(output, path) for path in protected):
        raise ValueError("Refusing to overwrite an input STL or manifest with the report; choose another report path")
    expected = load_manifest(args.manifest, report)
    if expected is not None:
        if any(same_path(output, root / name) for name in expected):
            raise ValueError("Refusing to overwrite a manifest-listed STL with the report; choose another report path")
        names = {path.name for path in actual}
        report["missing_files"] = sorted(set(expected) - names)
        report["unexpected_files"] = sorted(names - set(expected))
        report["completeness_status"] = "FAIL" if report["missing_files"] or report["unexpected_files"] else "PASS"
        if report["completeness_status"] == "FAIL":
            report["errors"].append("Actual root-level STL filenames do not match manifest exports")

    if actual:
        try:
            import bpy
            import bmesh
            report["blender_version"] = bpy.app.version_string
            if not bpy.app.background:
                raise RuntimeError("Run only in a disposable background Blender process (-b); the scene is cleared per file")
            options = import_options(bpy)
        except (ImportError, AttributeError, RuntimeError) as exc:
            report["errors"].append(f"Blender capability error: {exc}. Use blender -b --factory-startup --python-exit-code 1 --python SCRIPT -- ROOT REPORT")
        else:
            for path in actual:
                row = inspect_file(path, expected.get(path.name) if expected else None, bpy, bmesh, options)
                report["files"].append(row)
            if any(row["status"] == "FAIL" for row in report["files"]):
                report["errors"].append("One or more STL files failed; see files[].errors")

    if not report["errors"]:
        report["status"] = "PASS" if report["completeness_status"] == "PASS" else "WARN"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(output), "status": report["status"],
                      "completeness_status": report["completeness_status"],
                      "file_count": len(report["files"])}))
    if report["status"] == "FAIL":
        # Blender needs --python-exit-code 1 to propagate a Python exception.
        raise RuntimeError(f"STL basic checks failed; report saved to {output}")


if __name__ == "__main__":
    main()
