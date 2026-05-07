import tempfile
import unittest
from pathlib import Path

from torrent_utils import (
    bdecode_torrent,
    find_magnet,
    find_magnet_task_id,
    looks_like_torrent,
    magnet_info_hash,
    safe_filename,
    temp_path,
    torrent_file_is_private,
    torrent_is_private,
)


class TorrentUtilsTests(unittest.TestCase):
    def test_safe_filename_normalizes_and_adds_extension(self) -> None:
        self.assertEqual(safe_filename(" bad/name "), "bad_name.torrent")
        self.assertEqual(safe_filename("movie.torrent"), "movie.torrent")

    def test_find_magnet_extracts_and_trims_punctuation(self) -> None:
        text = "download magnet:?xt=urn:btih:ABC123&dn=test)."
        self.assertEqual(find_magnet(text), "magnet:?xt=urn:btih:ABC123&dn=test")

    def test_magnet_info_hash_normalizes_btih_value(self) -> None:
        uri = "magnet:?xt=urn:btih:ABCDEF123456&dn=test"
        self.assertEqual(magnet_info_hash(uri), "abcdef123456")

    def test_find_magnet_task_id_prefers_new_matching_task(self) -> None:
        magnet = "magnet:?xt=urn:btih:abcdef&dn=test"
        tasks = [
            {"id": "old", "additional": {"detail": {"uri": magnet}}},
            {"id": "new", "hash": "abcdef"},
        ]

        self.assertEqual(find_magnet_task_id(tasks, magnet, {"old"}), "new")

    def test_bdecode_torrent_extracts_private_info_dict(self) -> None:
        _, info = bdecode_torrent(b"d4:infod7:privatei1eee")

        self.assertTrue(torrent_is_private(info))

    def test_torrent_file_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private.torrent"
            path.write_bytes(b"d4:infod7:privatei1eee")

            self.assertTrue(looks_like_torrent(path))
            self.assertTrue(torrent_file_is_private(path))

    def test_temp_path_uses_requested_directory_and_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = temp_path(Path(tmp), "movie.torrent")

            self.assertEqual(path.parent, Path(tmp))
            self.assertTrue(path.name.endswith("_movie.torrent"))


if __name__ == "__main__":
    unittest.main()
