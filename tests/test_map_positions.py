import tempfile
import unittest
from pathlib import Path

from app import map_positions


class MapPositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._data = map_positions.data_dir
        map_positions.data_dir = lambda: Path(self.tmp.name)

    def tearDown(self) -> None:
        map_positions.data_dir = self._data
        self.tmp.cleanup()

    def test_round_trip_inside_juarez(self) -> None:
        saved = map_positions.save_position("192.168.1.1", 31.6904, -106.4245)
        self.assertEqual(map_positions.load_positions()["192.168.1.1"], saved)
        map_positions.clear_position("192.168.1.1")
        self.assertNotIn("192.168.1.1", map_positions.load_positions())

    def test_rejects_pins_outside_the_city(self) -> None:
        with self.assertRaises(ValueError):
            map_positions.save_position("192.168.1.1", 19.4326, -99.1332)


if __name__ == "__main__":
    unittest.main()
