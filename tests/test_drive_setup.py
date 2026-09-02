"""
Tests for the drive-preparation guidance.

Two things make this more than a documentation blob:

  1. The Data Duplication Village ships NTFS. ext4 is the better filesystem on
     a Linux-only machine, but a tool that silently assumes ext4 would quietly
     stop producing DDV-compatible drives. Both facts have to survive in the
     output.
  2. The ext4 defaults are actively wrong for this workload. One inode per
     16 KiB on a 6 TB drive provisions ~366 million inodes for a dataset with
     ~530,000 files, spending roughly 93 GB on tables that stay empty - on a
     drive that already overflows its nominal capacity.

Nothing here formats anything: these functions return text for a human to run.
That is deliberate and is asserted, because a regression that started shelling
out to mkfs would destroy a drive.

Run with:
    python -m pytest tests/test_drive_setup.py -v
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ddv_profiles
import drive_setup
from drive_setup import (
    FILESYSTEMS,
    MIN_INODES,
    compare_table,
    filesystem,
    format_help,
    format_plan,
    inode_count,
    inode_savings,
    recommend,
)


class TestFilesystemCatalog(unittest.TestCase):

    def test_the_three_relevant_filesystems_are_present(self):
        self.assertEqual({f.key for f in FILESYSTEMS}, {"ext4", "ntfs", "exfat"})

    def test_ntfs_is_marked_as_the_ddv_default(self):
        """The whole point of recording this: ext4 is the better Linux choice
        but it is NOT what the village hands out."""
        self.assertTrue(filesystem("ntfs").ddv_default)
        self.assertFalse(filesystem("ext4").ddv_default)
        self.assertFalse(filesystem("exfat").ddv_default)

    def test_exactly_one_filesystem_is_the_ddv_default(self):
        self.assertEqual(sum(1 for f in FILESYSTEMS if f.ddv_default), 1)

    def test_case_sensitivity_is_recorded_correctly(self):
        self.assertTrue(filesystem("ext4").case_sensitive)
        self.assertFalse(filesystem("ntfs").case_sensitive)
        self.assertFalse(filesystem("exfat").case_sensitive)

    def test_exfat_is_flagged_as_unjournaled(self):
        """It is the one real correctness difference between the three, and it
        matters across a multi-day transfer."""
        self.assertFalse(filesystem("exfat").journaling)
        self.assertTrue(filesystem("ext4").journaling)
        self.assertTrue(filesystem("ntfs").journaling)

    def test_no_filesystem_imposes_a_four_gigabyte_limit(self):
        """word lists ships single archives over 9 GB, so a FAT32-class limit
        would silently truncate the dataset."""
        for fs in FILESYSTEMS:
            with self.subTest(fs=fs.key):
                self.assertTrue(fs.max_file_bytes is None
                                or fs.max_file_bytes > 9 * (1 << 30))

    def test_every_filesystem_states_all_three_platforms(self):
        for fs in FILESYSTEMS:
            with self.subTest(fs=fs.key):
                self.assertTrue(fs.linux and fs.windows and fs.macos)
                self.assertTrue(fs.summary)

    def test_unknown_filesystem_is_rejected_helpfully(self):
        with self.assertRaises(KeyError) as caught:
            filesystem("btrfs")
        self.assertIn("unknown filesystem", caught.exception.args[0])

    def test_recommendation_follows_the_intended_use(self):
        self.assertEqual(recommend(cross_platform=False).key, "ext4")
        self.assertEqual(recommend(cross_platform=True).key, "ntfs")


class TestInodeSizing(unittest.TestCase):

    def drive_a(self):
        return ddv_profiles.resolve(drives=["A"])

    def test_inode_count_covers_the_files_with_headroom(self):
        datasets = self.drive_a()
        files = ddv_profiles.total_files(datasets)
        self.assertGreater(inode_count(datasets), files)

    def test_inode_count_is_a_round_number(self):
        """`-N 1600000` is readable; `-i 3958533` is false precision."""
        self.assertEqual(inode_count(self.drive_a()) % 100_000, 0)

    def test_a_file_heavy_drive_needs_more_inodes_than_a_file_light_one(self):
        heavy = inode_count(ddv_profiles.resolve(drives=["A"]))    # ~half a million files
        light = inode_count(ddv_profiles.resolve(drives=["F"]))    # 4,098 files
        self.assertGreater(heavy, light)

    def test_tiny_datasets_still_get_a_floor(self):
        self.assertGreaterEqual(inode_count(ddv_profiles.resolve(drives=["F"])),
                                MIN_INODES)

    def test_empty_selection_does_not_divide_by_zero(self):
        self.assertEqual(inode_count([]), MIN_INODES)

    def test_tuning_reclaims_real_capacity(self):
        """The reason to bother at all - it is ~90 GB on a 6 TB drive."""
        datasets = self.drive_a()
        saved = inode_savings(datasets, ddv_profiles.total_bytes(datasets))
        self.assertGreater(saved, 50 * (10 ** 9))

    def test_savings_never_go_negative(self):
        """A dataset with more files than the default ratio would provision
        must report zero saving, not a negative one."""
        self.assertGreaterEqual(inode_savings(self.drive_a(), 1), 0)


class TestFormatPlan(unittest.TestCase):

    def plan(self, fs="ext4", device="/dev/sdb"):
        return format_plan(fs, device, "infocon", ddv_profiles.resolve(drives=["A"]))

    def test_plan_warns_before_it_instructs(self):
        text = self.plan()
        self.assertIn("DESTROY", text)
        self.assertLess(text.index("DESTROY"), text.index("mkfs"))

    def test_plan_says_nothing_is_executed(self):
        self.assertIn("Nothing below is run for you", self.plan())

    def test_plan_tells_the_user_to_confirm_the_device(self):
        self.assertIn("lsblk", self.plan())

    def test_plan_uses_gpt_because_these_drives_exceed_two_terabytes(self):
        self.assertIn("mklabel gpt", self.plan())

    def test_ext4_plan_drops_the_root_reserve(self):
        self.assertIn("-m 0", self.plan("ext4"))

    def test_ext4_plan_sizes_inodes_from_real_data(self):
        text = self.plan("ext4")
        self.assertIn(f"-N {inode_count(ddv_profiles.resolve(drives=['A']))}", text)

    def test_ntfs_plan_uses_mkfs_ntfs(self):
        self.assertIn("mkfs.ntfs", self.plan("ntfs"))

    def test_exfat_plan_uses_mkfs_exfat(self):
        self.assertIn("mkfs.exfat", self.plan("exfat"))

    def test_ntfs_plan_mounts_with_windows_names(self):
        """Without it the drive accepts names Windows cannot represent, so it
        only looks portable."""
        self.assertIn("windows_names", self.plan("ntfs"))

    def test_partition_suffix_is_correct_for_sd_devices(self):
        self.assertIn("/dev/sdb1", self.plan(device="/dev/sdb"))

    def test_partition_suffix_is_correct_for_nvme_devices(self):
        """nvme0n1's first partition is nvme0n1p1, not nvme0n11."""
        text = format_plan("ext4", "/dev/nvme0n1", "infocon", [])
        self.assertIn("/dev/nvme0n1p1", text)
        self.assertNotIn("/dev/nvme0n11", text)

    def test_plan_explains_the_choice(self):
        self.assertIn("Why this filesystem", self.plan())

    def test_ntfs_plan_states_it_matches_the_ddv(self):
        self.assertIn("Data Duplication Village", self.plan("ntfs"))

    def test_ext4_plan_warns_it_is_not_the_ddv_format(self):
        self.assertIn("not what the ddv ships", self.plan("ext4").lower())

    def test_exfat_plan_warns_about_the_missing_journal(self):
        self.assertIn("journal", self.plan("exfat").lower())

    def test_plan_works_without_a_dataset_selection(self):
        text = format_plan("ext4", "/dev/sdb", "infocon", [])
        self.assertIn("mkfs.ext4", text)

    def test_label_with_spaces_is_quoted(self):
        text = format_plan("ext4", "/dev/sdb", "my drive", [])
        self.assertIn("'my drive'", text)


class TestFormatHelp(unittest.TestCase):

    def test_help_leads_with_what_the_ddv_actually_uses(self):
        text = format_help()
        self.assertIn("NTFS", text)
        self.assertLess(text.index("NTFS"), text.index("ext4"))

    def test_help_compares_all_three(self):
        text = format_help()
        for name in ("ext4", "NTFS", "exFAT"):
            self.assertIn(name, text)

    def test_help_reports_the_selection_shape(self):
        text = format_help(drives=["A"])
        files = ddv_profiles.total_files(ddv_profiles.resolve(drives=["A"]))
        self.assertIn(f"{files:,} files", text)
        self.assertIn("average", text)

    def test_help_without_a_device_says_how_to_get_commands(self):
        self.assertIn("--ddv-device", format_help(fs_key="ext4"))

    def test_help_with_a_device_emits_commands(self):
        self.assertIn("mkfs.ext4", format_help(drives=["A"], fs_key="ext4",
                                               device="/dev/sdb"))

    def test_unknown_filesystem_propagates(self):
        with self.assertRaises(KeyError):
            format_help(fs_key="zfs", device="/dev/sdb")


class TestNothingIsExecuted(unittest.TestCase):
    """A regression that started running these commands would destroy a drive."""

    def test_module_does_not_import_subprocess_or_os_system(self):
        with open(drive_setup.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("import subprocess", source)
        self.assertNotIn("os.system", source)
        self.assertNotIn("os.popen", source)

    def test_functions_return_text_rather_than_acting(self):
        self.assertIsInstance(format_plan("ext4", "/dev/sdb", "x", []), str)
        self.assertIsInstance(format_help(), str)
        self.assertIsInstance(compare_table(), str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
