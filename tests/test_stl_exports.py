#!/usr/bin/env python3
"""Real Blender integration tests; fixtures and reports use temporary directories.

Run with Python 3 (standard library only)::

    python3 tests/test_stl_exports.py --blender /path/to/blender

BLENDER_BIN is also supported. These tests never mock Blender or mesh topology.
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest


BLENDER_BIN = None
CHECK_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_stl_exports.py"


def subtract(a, b):
    return tuple(a[i] - b[i] for i in range(3))


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def tetrahedron(origin=(0, 0, 0), direction=1):
    """Return outward triangles; negative direction shares only origin with +1."""
    vertices = [origin] + [
        tuple(origin[k] + (direction if k == axis else 0) for k in range(3))
        for axis in range(3)
    ]
    faces = ((0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3))
    if direction < 0:
        faces = [tuple(reversed(face)) for face in faces]
    return [tuple(vertices[i] for i in face) for face in faces]


def cube():
    vertices = ((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
                (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1))
    faces = ((0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
             (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
             (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7))
    return [tuple(vertices[i] for i in face) for face in faces]


def write_stl(path, triangles):
    """Write minimal binary STL including normals consistent with face winding."""
    with Path(path).open("wb") as output:
        output.write(b"blender-color-print-split integration fixture".ljust(80, b"\0"))
        output.write(struct.pack("<I", len(triangles)))
        for a, b, c in triangles:
            normal = cross(subtract(b, a), subtract(c, a))
            length = math.sqrt(sum(value * value for value in normal))
            normal = tuple(value / length for value in normal) if length else (0, 0, 0)
            output.write(struct.pack("<12fH", *(normal + a + b + c), 0))


def run_validator(root, report, manifest=None, script=CHECK_SCRIPT):
    command = [str(BLENDER_BIN), "-b", "--factory-startup", "--python-exit-code", "1",
               "--python", str(script), "--", str(root), str(report)]
    if manifest is not None:
        command.extend(["--manifest", str(manifest)])
    return subprocess.run(command, capture_output=True, text=True, timeout=90)


class ExportValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="stl-validation-test-")
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.root = self.work / "exports"
        self.root.mkdir()
        self.report = self.work / "report.json"
        self.manifest = self.work / "manifest.json"

    def write_manifest(self, names, hashes=None):
        entries = []
        for index, name in enumerate(names):
            entry = {"file": name, "part_id": "p%d" % (index + 1)}
            if hashes and name in hashes:
                entry["sha256"] = hashes[name]
            entries.append(entry)
        self.manifest.write_text(json.dumps({"exports": entries}), encoding="utf-8")
        return self.manifest

    def check(self, expected_status, manifest=None):
        result = run_validator(self.root, self.report, manifest)
        detail = "\nstdout:\n%s\nstderr:\n%s" % (result.stdout, result.stderr)
        self.assertTrue(self.report.is_file(), "Missing JSON report" + detail)
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertIsInstance(report, dict, detail)
        self.assertEqual(report["status"], expected_status, detail)
        if expected_status == "FAIL":
            self.assertGreater(result.returncode, 0, detail)
        else:
            self.assertEqual(result.returncode, 0, detail)
        self.assertIsInstance(report["files"], list, detail)
        return report

    def assert_outward_row(self, row, expected_volume):
        self.assertEqual(row["topology_status"], "PASS")
        self.assertEqual(row["nonmanifold_vertices"], 0)
        self.assertEqual(row["orientation_status"], "PASS")
        self.assertAlmostEqual(row["signed_volume_mm3"], expected_volume, places=6)

    def test_empty_directory_fails(self):
        report = self.check("FAIL")
        self.assertEqual(report["files"], [])

    def test_valid_cube_and_tetrahedron_with_manifest_pass(self):
        write_stl(self.root / "cube.stl", cube())
        write_stl(self.root / "tetra.stl", tetrahedron())
        report = self.check("PASS", self.write_manifest(["cube.stl", "tetra.stl"]))
        rows = {row["file"]: row for row in report["files"]}
        self.assertEqual(set(rows), {"cube.stl", "tetra.stl"})
        self.assert_outward_row(rows["cube.stl"], 1.0)
        self.assert_outward_row(rows["tetra.stl"], 1.0 / 6.0)

    def test_valid_file_without_manifest_warns(self):
        write_stl(self.root / "part.stl", tetrahedron())
        report = self.check("WARN")
        self.assertEqual(report["completeness_status"], "NOT_RUN")
        self.assert_outward_row(report["files"][0], 1.0 / 6.0)

    def test_missing_expected_file_fails(self):
        write_stl(self.root / "present.stl", tetrahedron())
        report = self.check("FAIL", self.write_manifest(["present.stl", "missing.stl"]))
        self.assertEqual(len(report["files"]), 1)
        self.assertEqual(report["completeness_status"], "FAIL")
        self.assertEqual(report["missing_files"], ["missing.stl"])
        self.assertEqual(report["unexpected_files"], [])

    def test_unexpected_file_fails(self):
        write_stl(self.root / "expected.stl", tetrahedron())
        write_stl(self.root / "extra.stl", cube())
        report = self.check("FAIL", self.write_manifest(["expected.stl"]))
        self.assertEqual(len(report["files"]), 2)
        self.assertEqual(report["completeness_status"], "FAIL")
        self.assertEqual(report["missing_files"], [])
        self.assertEqual(report["unexpected_files"], ["extra.stl"])

    def test_mixed_case_stl_extensions_are_checked(self):
        names = ["upper.STL", "mixed.StL"]
        for name in names:
            write_stl(self.root / name, tetrahedron())
        report = self.check("PASS", self.write_manifest(names))
        self.assertEqual({row["file"] for row in report["files"]}, set(names))

    def test_bad_stl_does_not_prevent_later_file_report(self):
        (self.root / "a_broken.stl").write_bytes(b"not an STL")
        write_stl(self.root / "z_valid.stl", tetrahedron())
        report = self.check("FAIL", self.write_manifest(["a_broken.stl", "z_valid.stl"]))
        rows = {row["file"]: row for row in report["files"]}
        self.assertEqual(set(rows), {"a_broken.stl", "z_valid.stl"})
        self.assertEqual(rows["a_broken.stl"]["status"], "FAIL")
        self.assertEqual(rows["a_broken.stl"]["topology_status"], "NOT_RUN")
        self.assertTrue(rows["a_broken.stl"]["errors"])
        self.assert_outward_row(rows["z_valid.stl"], 1.0 / 6.0)

    def test_inward_closed_shell_fails(self):
        write_stl(self.root / "part.stl", [tuple(reversed(t)) for t in tetrahedron()])
        report = self.check("FAIL", self.write_manifest(["part.stl"]))
        row = report["files"][0]
        self.assertEqual(row["topology_status"], "FAIL")
        self.assertEqual(row["orientation_status"], "FAIL")
        self.assertLess(row["signed_volume_mm3"], 0)

    def test_two_shells_sharing_only_one_vertex_fail(self):
        triangles = tetrahedron() + tetrahedron(direction=-1)
        write_stl(self.root / "part.stl", triangles)
        report = self.check("FAIL", self.write_manifest(["part.stl"]))
        row = report["files"][0]
        self.assertEqual(row["topology_status"], "FAIL")
        self.assertEqual(row["nonmanifold_edges"], 0)
        self.assertGreater(row["nonmanifold_vertices"], 0)

    def test_disconnected_closed_shells_fail(self):
        write_stl(self.root / "part.stl", tetrahedron() + tetrahedron(origin=(3, 0, 0)))
        report = self.check("FAIL", self.write_manifest(["part.stl"]))
        self.assertEqual(report["files"][0]["topology_status"], "FAIL")

    def test_matching_hash_passes(self):
        path = self.root / "part.stl"
        write_stl(path, tetrahedron())
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        report = self.check("PASS", self.write_manifest([path.name], {path.name: digest}))
        self.assertEqual(report["files"][0]["sha256"], digest)

    def test_mismatching_hash_fails(self):
        write_stl(self.root / "part.stl", tetrahedron())
        self.check("FAIL", self.write_manifest(["part.stl"], {"part.stl": "0" * 64}))

    def test_report_must_not_overwrite_stl_input(self):
        path = self.root / "part.stl"
        write_stl(path, tetrahedron())
        before = path.read_bytes()
        result = run_validator(self.root, path, self.write_manifest([path.name]))
        self.assertGreater(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(path.read_bytes(), before)

    def test_report_must_not_overwrite_manifest(self):
        write_stl(self.root / "part.stl", tetrahedron())
        self.write_manifest(["part.stl"])
        before = self.manifest.read_bytes()
        result = run_validator(self.root, self.manifest, self.manifest)
        self.assertGreater(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.manifest.read_bytes(), before)


def main():
    global BLENDER_BIN
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blender", default=os.environ.get("BLENDER_BIN") or shutil.which("blender"),
                        help="Blender executable (or set BLENDER_BIN)")
    args, unittest_args = parser.parse_known_args()
    if not args.blender:
        parser.error("Real Blender is required: set BLENDER_BIN or pass --blender /path/to/blender")
    BLENDER_BIN = args.blender
    started = time.monotonic()
    try:
        version = subprocess.run([str(BLENDER_BIN), "--version"], capture_output=True,
                                 text=True, check=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        parser.error("Cannot execute Blender: %s" % exc)
    version_lines = [line.strip() for line in version.stdout.splitlines()
                     if line.startswith("Blender ") or "build hash:" in line]
    print("Runtime: Python %s; %s" % (sys.version.split()[0], "; ".join(version_lines)), flush=True)
    try:
        probe = subprocess.run([str(BLENDER_BIN), "-b", "--factory-startup", "--python-exit-code", "1",
                                "--python-expr", "print('STL_TEST_RUNTIME_READY')"],
                               capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        parser.error("Cannot start Blender background Python: %s" % exc)
    if probe.returncode != 0 or "STL_TEST_RUNTIME_READY" not in probe.stdout:
        parser.error("Blender background Python is unavailable; geometry tests were NOT RUN.\n"
                     + probe.stdout + probe.stderr)
    result = unittest.main(argv=[sys.argv[0]] + unittest_args, verbosity=2, exit=False)
    print("Elapsed: %.2f seconds" % (time.monotonic() - started), flush=True)
    return 0 if result.result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
