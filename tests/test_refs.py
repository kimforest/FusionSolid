import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fusion2solid import refs  # noqa: E402


class FaceMatchingTest(unittest.TestCase):
    def test_plane_face_matches_by_point_and_normal(self):
        geometry = {"surface_type": "adsk::fusion::Plane", "normal": [0, 0, 1], "centroid": [0, 0, 10]}
        faces = [
            {"index": 0, "name": "F0", "normal": [0, 0, 1], "centroid": [0, 0, 10.005], "point": [0, 0, 10]},
            {"index": 1, "name": "F1", "normal": [0, 1, 0], "centroid": [0, 0, 0]},
        ]
        match = refs.match_face(geometry, faces)
        self.assertIsNotNone(match)
        self.assertEqual(match.index, 0)
        self.assertEqual(match.confidence, "high")
        self.assertEqual(match.selection, "F0")

    def test_incompatible_surface_families_do_not_match(self):
        geometry = {"surface_type": "adsk::fusion::Plane", "normal": [0, 0, 1], "centroid": [0, 0, 0]}
        faces = [{"index": 0, "name": "C0", "cylinder": {"axis": [0, 0, 1], "radius": 5.0},
                  "centroid": [0, 0, 0], "normal": [0, 0, 1]}]
        # The face reports as a cylinder, the reference as a plane: no match.
        self.assertIsNone(refs.match_face(geometry, faces))

    def test_cylinder_face_matches_by_axis_and_radius(self):
        geometry = {"surface_type": "adsk::fusion::Cylinder", "axis": [0, 0, 1], "radius": 5.0,
                    "centroid": [0, 0, 0]}
        faces = [
            {"index": 3, "name": "Hole", "centroid": [0, 0, 0.001],
             "cylinder": {"axis": [0, 0, 1], "radius": 5.0005}},
        ]
        match = refs.match_face(geometry, faces)
        self.assertIsNotNone(match)
        self.assertEqual(match.index, 3)
        self.assertEqual(match.confidence, "high")


class EdgeMatchingTest(unittest.TestCase):
    def test_edge_matches_by_midpoint_and_length(self):
        geometry = {"midpoint": [10.0, 0.0, 0.0], "length_mm": 20.0}
        edges = [
            {"index": 0, "midpoint": [0.0, 0.0, 0.0], "length": 20.0},
            {"index": 1, "midpoint": [10.0, 0.0, 0.001], "length": 20.001},
        ]
        match = refs.match_edge(geometry, edges)
        self.assertIsNotNone(match)
        self.assertEqual(match.index, 1)
        self.assertEqual(match.confidence, "high")
        self.assertEqual(match.selection, [10.0, 0.0, 0.001])

    def test_no_edges_yields_no_match(self):
        self.assertIsNone(refs.match_edge({"midpoint": [0, 0, 0]}, []))


class VertexMatchingTest(unittest.TestCase):
    def test_vertex_matches_nearest(self):
        geometry = {"centroid": [1.0, 2.0, 3.0]}
        vertices = [{"index": 0, "position": [0, 0, 0]}, {"index": 1, "position": [1.0, 2.0, 3.0005]}]
        match = refs.match_vertex(geometry, vertices)
        self.assertIsNotNone(match)
        self.assertEqual(match.index, 1)


if __name__ == "__main__":
    unittest.main()
