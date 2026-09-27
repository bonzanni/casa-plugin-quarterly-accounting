import unittest

from tests._base import TempEnv
import amounts  # noqa: E402
import dates    # noqa: E402


class TestDates(unittest.TestCase):
    def test_quarter_of(self):
        self.assertEqual(dates.quarter_of("2026-06-30"), "2026-Q2")
        self.assertEqual(dates.quarter_of("2026-07-01"), "2026-Q3")
        self.assertEqual(dates.quarter_of("2026-12-31T23:00:00Z"), "2026-Q4")

    def test_bare_qn_is_refused(self):
        for bad in ("Q3", "2026Q3", "2026-Q5", ""):
            with self.assertRaises(ValueError):
                dates.parse_quarter(bad)

    def test_bounds(self):
        self.assertEqual(dates.quarter_bounds("2026-Q4"), ("2026-10-01", "2027-01-01"))
        self.assertEqual(dates.quarter_start("2026-08-14"), "2026-07-01")

    def test_effective_date_uses_value_date_only_when_booking_date_is_missing(self):
        self.assertEqual(dates.effective_date({"booking_date": "2026-07-01",
                                               "value_date": "2026-06-30"}), "2026-07-01")
        self.assertEqual(dates.effective_date({"booking_date": None,
                                               "value_date": "2026-06-30"}), "2026-06-30")
        self.assertEqual(dates.effective_date({"booking_date": "", "value_date": ""}), None)

    def test_partial(self):
        self.assertTrue(dates.is_partial("2026-Q3", "2026-08-14"))
        self.assertTrue(dates.is_partial("2026-Q3", "2026-09-30"))
        self.assertFalse(dates.is_partial("2026-Q3", "2026-10-01"))

    def test_short_forms(self):
        self.assertEqual(dates.short_day("2026-09-20"), "20 Sep")
        self.assertEqual(dates.quarter_label("2026-Q3"), "Q3 2026")


class TestAmounts(unittest.TestCase):
    def test_fmt(self):
        self.assertEqual(amounts.fmt(5445, "EUR"), "EUR 54.45")
        self.assertEqual(amounts.fmt(121000, "EUR"), "EUR 1,210.00")
        self.assertEqual(amounts.fmt(5, "EUR"), "EUR 0.05")


if __name__ == "__main__":
    unittest.main()
