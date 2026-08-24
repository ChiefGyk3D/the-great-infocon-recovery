"""
Tests for the DEF CON Data Duplication Village source-drive profiles.

The point of these profiles is that someone can rebuild a DDV drive at home
without having to work out which slice of a 37 TB archive belongs on it. Three
things have to hold for that to be true:

  1. The catalog matches what the publisher actually ships. Every dataset is
     addressable, no dataset belongs to two drives, and the drive letters and
     capacities track README.md's DDV table.
  2. Capacity answers are honest. Two of the six drives no longer fit the
     capacity they are nominally sold at, and the tool has to say so rather
     than silently trimming - the user asked for exactly this.
  3. Selections resolve to real sync filters, without double-counting when a
     drive and one of its own datasets are both named.

Run with:
    python -m pytest tests/test_ddv_profiles.py -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ddv_profiles
from ddv_profiles import (
    DATASETS,
    DRIVES,
    TB,
    drive,
    drive_datasets,
    drive_fit,
    fit,
    merge_selections,
    preflight,
    resolve,
    total_bytes,
    usable_bytes,
)


class TestCatalogIntegrity(unittest.TestCase):

    def test_every_drive_letter_is_unique(self):
        letters = [d.letter for d in DRIVES]
        self.assertEqual(len(letters), len(set(letters)))

    def test_the_six_ddv_drives_are_present(self):
        self.assertEqual([d.letter for d in DRIVES], list("ABCDEF"))

    def test_capacities_match_the_documented_ddv_layout(self):
        expected = {"A": 6, "B": 6, "C": 6, "D": 8, "E": 8, "F": 6}
        for d in DRIVES:
            with self.subTest(drive=d.letter):
                self.assertEqual(d.nominal_tb, expected[d.letter])

    def test_every_dataset_key_is_unique(self):
        keys = [d.key for d in DATASETS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_every_drive_dataset_reference_resolves(self):
        known = {d.key for d in DATASETS}
        for d in DRIVES:
            for key in d.dataset_keys + d.alternates:
                with self.subTest(drive=d.letter, dataset=key):
                    self.assertIn(key, known)

    def test_every_dataset_belongs_to_a_drive_that_claims_it(self):
        """A dataset whose .drive disagrees with the drive listing it would be
        reported under the wrong heading."""
        for ds in DATASETS:
            with self.subTest(dataset=ds.key):
                d = drive(ds.drive)
                self.assertIn(ds.key, d.dataset_keys + d.alternates)

    def test_no_dataset_is_carried_by_two_drives(self):
        seen: dict[str, str] = {}
        for d in DRIVES:
            for key in d.dataset_keys:
                self.assertNotIn(key, seen,
                                 f"{key} claimed by drives {seen.get(key)} and {d.letter}")
                seen[key] = d.letter

    def test_every_dataset_has_a_usable_selection(self):
        """A dataset the sync cannot be pointed at is just documentation."""
        for ds in DATASETS:
            with self.subTest(dataset=ds.key):
                sel = ds.selection
                self.assertTrue(
                    sel.sources or sel.only_top or sel.only_cons or sel.only_mirrors,
                    f"{ds.key} resolves to no filters at all",
                )

    def test_measured_sizes_are_positive(self):
        for ds in DATASETS:
            with self.subTest(dataset=ds.key):
                self.assertGreater(ds.measured_bytes, 0)
                self.assertGreater(ds.measured_files, 0)

    def test_published_figures_are_within_a_rounding_step_of_measured(self):
        """infocon.org states sizes to 0.1 TB. A larger gap means the catalog
        drifted from the archive and needs re-measuring, not that the publisher
        rounded."""
        for ds in DATASETS:
            if ds.published_bytes is None:
                continue
            with self.subTest(dataset=ds.key):
                drift = abs(ds.measured_bytes - ds.published_bytes) / ds.published_bytes
                self.assertLess(drift, 0.15,
                                f"{ds.key}: measured {ds.measured_tb:.2f} TB vs "
                                f"published {ds.published_bytes / TB:.2f} TB")


class TestCapacityHonesty(unittest.TestCase):
    """The user's requirement: drives over capacity are reported, not trimmed."""

    def test_drive_a_no_longer_fits_six_terabytes(self):
        f = drive_fit("A")
        self.assertFalse(f.fits)
        self.assertGreater(f.shortfall, 0)

    def test_drive_d_no_longer_fits_eight_terabytes(self):
        f = drive_fit("D")
        self.assertFalse(f.fits)

    def test_hash_table_drives_still_fit(self):
        for letter in ("B", "C", "E", "F"):
            with self.subTest(drive=letter):
                self.assertTrue(drive_fit(letter).fits)

    def test_overflow_is_reported_in_the_catalog_text(self):
        text = ddv_profiles.format_catalog()
        self.assertIn("OVER", text)
        self.assertIn("exceeds this drive by", text)

    def test_nothing_is_silently_dropped_to_make_a_drive_fit(self):
        """Drive A's dataset list must stay complete even though it overflows."""
        keys = {ds.key for ds in drive_datasets("A")}
        self.assertEqual(
            keys, {"cons", "defcon", "skills", "wordlists", "podcasts", "documentaries"})

    def test_fit_accounts_for_filesystem_overhead(self):
        """6e12 raw bytes do not survive formatting; a plan that exactly equals
        the nominal size must not be reported as fitting."""
        self.assertLess(usable_bytes(6 * TB), 6 * TB)

    def test_zero_overhead_can_be_requested(self):
        self.assertEqual(usable_bytes(6 * TB, fs_overhead=0.0), 6 * TB)

    def test_shortfall_is_zero_when_it_fits(self):
        self.assertEqual(drive_fit("F").shortfall, 0)

    def test_alternate_dataset_rescues_drive_d(self):
        """Drive D ships an older vx-underground snapshot precisely because the
        current one no longer fits."""
        alt = ddv_profiles.dataset("vx-underground-2024")
        self.assertTrue(fit([alt], drive("D").nominal_bytes).fits)


class TestResolution(unittest.TestCase):

    def test_drive_letter_selects_all_its_datasets(self):
        selected = resolve(drives=["B"])
        self.assertEqual({d.key for d in selected}, {"lanman", "mysqlsha1", "ntlm"})

    def test_letters_are_case_insensitive(self):
        self.assertEqual([d.key for d in resolve(drives=["b"])],
                         [d.key for d in resolve(drives=["B"])])

    def test_datasets_can_be_picked_individually_across_drives(self):
        selected = resolve(datasets=["md5", "ntlm"])
        self.assertEqual({d.key for d in selected}, {"md5", "ntlm"})
        self.assertEqual({d.drive for d in selected}, {"B", "C"})

    def test_drive_and_its_own_dataset_are_not_double_counted(self):
        both = resolve(drives=["C"], datasets=["md5"])
        self.assertEqual(len(both), len(resolve(drives=["C"])))
        self.assertEqual(total_bytes(both), total_bytes(resolve(drives=["C"])))

    def test_alternates_are_excluded_by_default(self):
        """Including it would count two vx-underground snapshots as one drive."""
        keys = {d.key for d in drive_datasets("D")}
        self.assertNotIn("vx-underground-2024", keys)

    def test_alternates_can_be_requested(self):
        keys = {d.key for d in drive_datasets("D", include_alternates=True)}
        self.assertIn("vx-underground-2024", keys)

    def test_unknown_drive_is_rejected_with_a_helpful_message(self):
        with self.assertRaises(KeyError) as caught:
            resolve(drives=["Z"])
        self.assertIn("unknown DDV drive", caught.exception.args[0])

    def test_unknown_dataset_is_rejected_with_a_helpful_message(self):
        with self.assertRaises(KeyError) as caught:
            resolve(datasets=["rainbows"])
        self.assertIn("unknown DDV dataset", caught.exception.args[0])

    def test_empty_selection_is_empty(self):
        self.assertEqual(resolve(), [])


class TestSelectionMerging(unittest.TestCase):

    def test_rainbow_table_drives_opt_into_rainbow_tables(self):
        """They are excluded from the default crawl, so a profile that forgot
        this flag would silently transfer nothing."""
        for letter in ("B", "C", "E", "F"):
            with self.subTest(drive=letter):
                plan = merge_selections(resolve(drives=[letter]))
                self.assertTrue(plan.include_rainbow_tables)

    def test_archive_drive_does_not_opt_into_rainbow_tables(self):
        plan = merge_selections(resolve(drives=["A"]))
        self.assertFalse(plan.include_rainbow_tables)

    def test_vx_underground_opts_into_mirrors(self):
        plan = merge_selections(resolve(drives=["D"]))
        self.assertTrue(plan.include_mirrors)
        self.assertIn("vx underground - 2025 June", plan.only_mirrors)

    def test_merged_filters_are_deduplicated(self):
        plan = merge_selections(resolve(drives=["B"]))
        self.assertEqual(list(plan.only_top), ["rainbow tables"])

    def test_drive_a_targets_the_archive_sections(self):
        plan = merge_selections(resolve(drives=["A"]))
        for section in ("cons", "skills", "word lists", "podcasts", "documentaries"):
            self.assertIn(section, plan.only_top)

    def test_hash_tables_resolve_to_their_own_subdirectory(self):
        """The bug this guards: --only-top can only name a whole top-level
        section, so selecting Drive B by section would crawl all 22.8 TB of
        'rainbow tables' instead of the 5.8 TB the drive actually carries."""
        plan = merge_selections(resolve(drives=["B"]))
        self.assertEqual(
            set(plan.paths),
            {"rainbow tables/ntlm", "rainbow tables/mysqlsha1", "rainbow tables/lanman"})

    def test_no_hash_table_path_is_a_bare_top_level_section(self):
        for ds in DATASETS:
            if not ds.selection.include_rainbow_tables:
                continue
            for path in ds.selection.paths:
                with self.subTest(dataset=ds.key, path=path):
                    self.assertIn("/", path, "would widen to the whole section")

    def test_ntlm_and_ntlm9_do_not_select_each_other(self):
        """'NTLM' is a prefix of 'NTLM 9'; a substring filter that confuses them
        would silently add 6.7 TB to Drive B."""
        b_names = merge_selections(resolve(datasets=["ntlm"])).torrent_names
        e_names = merge_selections(resolve(datasets=["ntlm9"])).torrent_names
        for needle in b_names:
            self.assertNotIn(needle, e_names[0])
        for needle in e_names:
            self.assertNotIn(needle, b_names[0])

    def test_defcon_is_reachable_over_both_routes(self):
        """infocon.org serves cons/DEF CON as a browsable tree AND ships a
        'DEF CON archive' torrent; the profile must not assume only one."""
        sel = ddv_profiles.dataset("defcon").selection
        self.assertEqual(sel.paths, ("cons/DEF CON",))
        self.assertEqual(sel.torrent_names, ("DEF CON archive",))

    def test_cons_and_def_con_are_separate_roots(self):
        """infocon.org's cons/ index lists 239 conferences and does not link
        DEF CON, although cons/DEF CON/ is directly browsable. So a cons/ crawl
        never reaches it, and DEF CON must keep its own root or Drive A silently
        loses 1.77 TB."""
        plan = merge_selections(resolve(drives=["A"]))
        self.assertIn("cons", plan.paths)
        self.assertIn("cons/DEF CON", plan.paths)

    def test_def_con_survives_nesting_collapse(self):
        """The generic rule folds a/b into a. DEF CON is the documented
        exception because it is unlinked from its parent index."""
        plan = merge_selections(resolve(datasets=["cons", "defcon"]))
        self.assertIn("cons/DEF CON", plan.paths)

    def test_ordinary_nested_paths_still_collapse(self):
        from ddv_profiles import Dataset, Selection
        parent = Dataset(key="p", label="p", drive="A", measured_bytes=1,
                         measured_files=1, selection=Selection(paths=("skills",)))
        child = Dataset(key="c", label="c", drive="A", measured_bytes=1,
                        measured_files=1, selection=Selection(paths=("skills/sub",)))
        self.assertEqual(merge_selections([parent, child]).paths, ("skills",))

    def test_cons_alone_does_not_pull_def_con(self):
        plan = merge_selections(resolve(datasets=["cons"]))
        self.assertEqual(plan.paths, ("cons",))

    def test_every_dataset_is_reachable_by_path_or_torrent(self):
        for ds in DATASETS:
            with self.subTest(dataset=ds.key):
                sel = ds.selection
                self.assertTrue(sel.paths or sel.torrent_names,
                                f"{ds.key} cannot be fetched by either route")


class TestPreflight(unittest.TestCase):

    def test_insufficient_space_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok, message = preflight(resolve(drives=["E"]), tmp)
            if not ok:  # the usual case: no 6.7 TB free in a temp dir
                self.assertIn("short by", message)

    def test_a_tiny_plan_passes_on_any_real_filesystem(self):
        class Tiny:
            measured_bytes = 1
            measured_files = 1
        with tempfile.TemporaryDirectory() as tmp:
            ok, _ = preflight([Tiny()], tmp)
            self.assertTrue(ok)

    def test_unmeasurable_destination_does_not_hard_fail(self):
        """A missing mount point is the caller's decision, not a crash here."""
        ok, message = preflight(resolve(drives=["F"]), "/nonexistent/mount/point")
        self.assertTrue(ok)
        self.assertIn("could not measure", message)


class TestFormatting(unittest.TestCase):

    def test_catalog_lists_every_drive_and_dataset(self):
        text = ddv_profiles.format_catalog()
        for d in DRIVES:
            self.assertIn(f"Drive {d.letter}", text)
        for ds in DATASETS:
            self.assertIn(ds.key, text)

    def test_catalog_records_when_it_was_measured(self):
        self.assertIn(ddv_profiles.CATALOG_DATE, ddv_profiles.format_catalog())

    def test_catalog_surfaces_the_mssql_naming_discrepancy(self):
        """The DDV list says MSSQL; the archive publishes MySQL SHA-1. Flagged
        rather than silently resolved."""
        self.assertIn("MSSQL", ddv_profiles.format_catalog())

    def test_plan_reports_size_and_file_count(self):
        text = ddv_profiles.format_plan(resolve(drives=["F"]))
        self.assertIn("net-ntlmv1", text)
        self.assertIn("TB", text)

    def test_plan_includes_space_verdict_when_given_a_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = ddv_profiles.format_plan(resolve(drives=["E"]), tmp)
            self.assertTrue("space OK" in text or "INSUFFICIENT SPACE" in text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
