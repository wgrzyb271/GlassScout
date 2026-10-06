"""Persistence checks for draggable dashboard ordering and groups."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from dashboard.layout import load_layout, normalize_layout, save_layout


class LayoutTests(unittest.TestCase):
    def test_new_services_are_appended_and_missing_services_removed(self):
        raw = {"items": [
            {"type": "service", "id": "a"},
            {"type": "service", "id": "removed"},
        ]}
        self.assertEqual(normalize_layout(raw, ["a", "b"])["items"], [
            {"type": "service", "id": "a"},
            {"type": "service", "id": "b"},
        ])

    def test_groups_are_named_deduplicated_and_singletons_dissolve(self):
        raw = {"items": [
            {"type": "group", "id": "servers", "name": "  Lab  ", "service_ids": ["a", "b", "a"]},
            {"type": "group", "id": "single", "name": "Unused", "service_ids": ["c"]},
            {"type": "service", "id": "b"},
        ]}
        self.assertEqual(normalize_layout(raw, ["a", "b", "c"])["items"], [
            {"type": "group", "id": "servers", "name": "Lab", "service_ids": ["a", "b"]},
            {"type": "service", "id": "c"},
        ])

    def test_layout_round_trip(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "layout.json"
            value = {"items": [{"type": "group", "id": "apps", "name": "Apps", "service_ids": ["a", "b"]}]}
            saved = save_layout(value, ["a", "b"], path)
            self.assertEqual(load_layout(["a", "b"], path), saved)


if __name__ == "__main__":
    unittest.main()
