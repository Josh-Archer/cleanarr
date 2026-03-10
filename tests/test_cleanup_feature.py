import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Mock external dependencies before importing the runtime package
sys.modules["plexapi"] = MagicMock()
sys.modules["plexapi.server"] = MagicMock()
sys.modules["transmission_rpc"] = MagicMock()
sys.modules["loguru"] = MagicMock()
sys.modules["requests"] = MagicMock()

# Import the module under test
# Note: In a real CI environment, PYTHONPATH usually handles this.
# For this script to be standalone/local, we might need to adjust path.
# Assuming this script is in the same directory as the repository root.
current_dir = os.path.dirname(os.path.abspath(__file__))
repo_root = os.path.dirname(current_dir)
if repo_root not in sys.path:
    sys.path.append(repo_root)

from cleanarr_runtime import cleanup as cleanarr

class TestCleanarrFeature(unittest.TestCase):
    def setUp(self):
        # Reset config
        cleanarr.CONFIG["plex"]["token"] = "dummy_token"
        cleanarr.CONFIG["remove_failed_downloads"] = True
        cleanarr.CONFIG["disable_torrent_cleanup"] = False
        cleanarr.CONFIG["dry_run"] = False

        # Mock global logger
        cleanarr.logger = MagicMock()

    @patch("cleanarr_runtime.cleanup.TransmissionClient")
    @patch("cleanarr_runtime.cleanup.PlexServer")
    def test_clean_failed_downloads(self, MockPlex, MockTransmission):
        # Setup MediaCleanup instance with mocked Transmission
        cleaner = cleanarr.MediaCleanup()
        mock_transmission = MagicMock()
        cleaner.transmission = mock_transmission

        # Setup Torrents
        # t1: Active, directory based torrent
        t1 = MagicMock()
        t1.id = 1
        t1.name = "MyTorrent"
        t1.error = 0
        t1.files.return_value = [
            {"name": "MyTorrent/file1.txt"},
            {"name": "MyTorrent/file2.mkv"}
        ]

        # t2: Active, single file torrent with mismatching top name (e.g. renamed in client)
        t2 = MagicMock()
        t2.id = 2
        t2.name = "RenamedFile"
        t2.error = 0
        t2.files.return_value = [
            {"name": "OriginalFile.mkv"}
        ]

        # t3: Active, file with .part suffix on disk (not in file list usually, but handled by logic)
        t3 = MagicMock()
        t3.id = 3
        t3.name = "PartFile.mkv"
        t3.error = 0
        t3.files.return_value = [{"name": "PartFile.mkv"}]

        # t4: Active, failed to get files (fallback to name)
        t4 = MagicMock()
        t4.id = 4
        t4.name = "FallbackTorrent"
        t4.error = 0
        t4.files.side_effect = Exception("Failed")

        # t5: Errored
        t5 = MagicMock()
        t5.id = 5
        t5.name = "ErroredTorrent"
        t5.error = 1

        mock_transmission.get_torrents.return_value = [t1, t2, t3, t4, t5]

        # Setup Incomplete Dir
        incomplete_dir = "/tmp/incomplete"
        mock_session = MagicMock()
        mock_session.incomplete_dir_enabled = True
        mock_session.incomplete_dir = incomplete_dir
        mock_transmission.get_session.return_value = mock_session

        # Mock filesystem operations
        with patch("os.path.exists") as mock_exists, \
             patch("os.listdir") as mock_listdir, \
             patch("os.remove") as mock_remove, \
             patch("shutil.rmtree") as mock_rmtree, \
             patch("os.path.isdir") as mock_isdir:

            mock_exists.return_value = True
            # Files in incomplete dir:
            # 1. MyTorrent (Matches t1 files[0] top level)
            # 2. OriginalFile.mkv (Matches t2 files[0] top level)
            # 3. PartFile.mkv.part (Matches t3 files[0] top level + .part handling)
            # 4. FallbackTorrent (Matches t4 name)
            # 5. OrphanFile.mkv (Delete)

            mock_listdir.return_value = [
                "MyTorrent",
                "OriginalFile.mkv",
                "PartFile.mkv.part",
                "FallbackTorrent",
                "OrphanFile.mkv"
            ]

            def isdir_side_effect(path):
                return False # Treat all as files for simplicity
            mock_isdir.side_effect = isdir_side_effect

            # Execute
            cleaner.clean_failed_downloads()

            # Verifications

            # 1. Errored torrent removed
            mock_transmission.remove_torrent.assert_called_with(5, delete_data=True)

            # 2. Orphans removed
            mock_remove.assert_called_with(os.path.join(incomplete_dir, "OrphanFile.mkv"))

            # 3. Active torrents kept
            # Check arguments passed to remove/rmtree
            removed_files = [c[0][0] for c in mock_remove.call_args_list]

            self.assertNotIn(os.path.join(incomplete_dir, "MyTorrent"), removed_files)
            self.assertNotIn(os.path.join(incomplete_dir, "OriginalFile.mkv"), removed_files)
            self.assertNotIn(os.path.join(incomplete_dir, "PartFile.mkv.part"), removed_files)
            self.assertNotIn(os.path.join(incomplete_dir, "FallbackTorrent"), removed_files)

if __name__ == "__main__":
    unittest.main()
