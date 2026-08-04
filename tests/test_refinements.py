"""The refinements that followed Phase 5.

Three themes, each replacing something that was true of a workbook rather than of a
database: month lists fixed to one fiscal year, tax bands with no effective date, and a
month that could hold only one payment.
"""

import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest

from budget import reference, repo, ui
from budget.models import Projection

APRIL = dt.date(2026, 4, 1)


# ------------------------------------------------------------------- period arithmetic


class TestPeriodArithmetic:
    def test_month_add_moves_forward_across_a_year(self):
        assert repo.month_add("2026-11", 3) == "2027-02"

    def test_month_add_moves_backward_across_a_year(self):
        assert repo.month_add("2027-01", -1) == "2026-12"

    def test_month_add_of_zero_is_the_same_month(self):
        assert repo.month_add("2026-07", 0) == "2026-07"

    def test_months_between_counts_the_gap(self):
        assert repo.months_between("2026-04", "2027-03") == 11
        assert repo.months_between("2026-04", "2026-04") == 0
        assert repo.months_between("2026-05", "2026-04") == -1

    def test_period_range_is_inclusive_and_contiguous(self):
        assert repo.period_range("2026-11", "2027-02") == [
            "2026-11", "2026-12", "2027-01", "2027-02"
        ]

    def test_period_range_backwards_is_empty(self):
        assert repo.period_range("2026-05", "2026-04") == []

    def test_period_of_a_date(self):
        assert repo.period_of(dt.date(2026, 7, 9)) == "2026-07"
        assert repo.period_of(pd.Timestamp("2026-07-09")) == "2026-07"

    def test_tax_year_of_starts_in_april(self):
        assert repo.tax_year_of("2026-04") == 2026
        assert repo.tax_year_of("2027-03") == 2026
        assert repo.tax_year_of("2027-04") == 2027


class TestSpan:
    """What the month dropdowns offer. Was repo.fiscal_periods(tax_year) -- April to March
    and no further, because that is how many months a workbook had."""

    def test_it_runs_from_the_earliest_month_to_today(self):
        span = repo.span("2026-04", today=dt.date(2026, 8, 4))
        assert span[0] == "2026-04"
        assert span[-1] == "2026-08"

    def test_the_current_month_is_included_from_its_first_day(self):
        assert repo.span("2026-04", today=dt.date(2026, 8, 1))[-1] == "2026-08"
        assert repo.span("2026-04", today=dt.date(2026, 8, 31))[-1] == "2026-08"

    def test_the_look_forward_extends_past_today(self):
        span = repo.span("2026-04", look_forward=3, today=dt.date(2026, 8, 4))
        assert span[-1] == "2026-11"

    def test_it_crosses_a_fiscal_year_end_without_stopping(self):
        """The whole point: a second year of history extends the list rather than falling
        outside it."""
        span = repo.span("2026-04", today=dt.date(2027, 6, 1))
        assert "2027-03" in span and "2027-04" in span
        assert span[-1] == "2027-06"

    def test_data_earlier_than_today_still_starts_at_the_data(self):
        span = repo.span("2025-04", today=dt.date(2026, 8, 4))
        assert span[0] == "2025-04"

    def test_data_later_than_today_is_not_truncated_away(self):
        """A figure set for a future month must still be selectable."""
        span = repo.span("2026-12", today=dt.date(2026, 8, 4))
        assert span[0] == "2026-08"
        assert span[-1] == "2026-12"


class TestEarliestPeriod:
    def test_it_takes_the_first_month_across_every_source(self):
        assert repo.earliest_period(
            pd.Series(["2026-07", "2026-08"]),
            pd.Series(["2026-05"]),
            default="2026-04",
        ) == "2026-05"

    def test_an_empty_database_falls_back_to_the_default(self):
        assert repo.earliest_period(None, pd.Series(dtype="str"), default="2026-04") == (
            "2026-04"
        )

    def test_blanks_are_ignored(self):
        assert repo.earliest_period(
            pd.Series([None, "2026-06"]), default="2026-04"
        ) == "2026-06"


# ------------------------------------------------------------------ effective-dated bands


class TestDatedBands:
    """Thresholds and rates carry an effective date, so a mid-year change is a new set
    rather than an edit that rewrites what earlier months were taxed at."""

    ASSUMPTIONS = pd.DataFrame(
        [
            {"key": "basic_rate", "effective_from": APRIL, "value": Decimal("20.00"),
             "tax_year": 2026},
            {"key": "basic_rate", "effective_from": dt.date(2026, 9, 1),
             "value": Decimal("18.50"), "tax_year": 2026},
            {"key": "personal_allowance", "effective_from": APRIL,
             "value": Decimal("1047.50"), "tax_year": 2026},
            {"key": "basic_rate_threshold", "effective_from": APRIL,
             "value": Decimal("4189.17"), "tax_year": 2026},
        ]
    )

    def test_the_set_in_force_is_the_last_one_starting_on_or_before_the_date(self):
        assert repo.bands_from(
            self.ASSUMPTIONS, dt.date(2026, 8, 31)
        ).basic_rate == Decimal("0.20")
        assert repo.bands_from(
            self.ASSUMPTIONS, dt.date(2026, 9, 1)
        ).basic_rate == Decimal("0.185")

    def test_no_date_takes_the_most_recent(self):
        assert repo.bands_from(self.ASSUMPTIONS).basic_rate == Decimal("0.185")

    def test_before_the_first_set_the_earliest_applies_rather_than_zero(self):
        """A 0% tax rate is a worse answer than a slightly early one, and it would be
        silent."""
        assert repo.bands_from(
            self.ASSUMPTIONS, dt.date(2026, 1, 1)
        ).basic_rate == Decimal("0.20")

    def test_the_basic_band_still_derives_from_its_two_inputs(self):
        bands = repo.bands_from(self.ASSUMPTIONS, APRIL)
        assert bands.basic_band == Decimal("4189.17") - Decimal("1047.50")

    def test_the_allowance_taper_is_left_to_tax_bands(self):
        """The steps are a taper within a year, not a revision of the bands, so they are
        carried through whole rather than resolved to one value."""
        with_steps = pd.concat(
            [
                self.ASSUMPTIONS,
                pd.DataFrame(
                    [
                        {"key": repo.ADJUSTMENT_KEY, "effective_from": APRIL,
                         "value": Decimal("-100"), "tax_year": 2026},
                        {"key": repo.ADJUSTMENT_KEY,
                         "effective_from": dt.date(2026, 7, 1),
                         "value": Decimal("-250"), "tax_year": 2026},
                    ]
                ),
            ]
        )
        bands = repo.bands_from(with_steps, APRIL)
        assert len(bands.allowance_steps) == 2
        assert bands.allowance_for(dt.date(2026, 8, 1)) == Decimal("-250")

    def test_assumption_dates_excludes_the_taper_steps(self):
        with_steps = pd.concat(
            [
                self.ASSUMPTIONS,
                pd.DataFrame(
                    [{"key": repo.ADJUSTMENT_KEY, "effective_from": dt.date(2026, 7, 1),
                      "value": Decimal("-250"), "tax_year": 2026}]
                ),
            ]
        )
        assert repo.assumption_dates(with_steps) == [APRIL, dt.date(2026, 9, 1)]

    def test_an_empty_year_gives_zeroed_bands_rather_than_raising(self):
        empty = pd.DataFrame(columns=["key", "effective_from", "value", "tax_year"])
        assert repo.bands_from(empty).basic_rate == Decimal("0")


# ------------------------------------------------------------------------ bonus actuals


class TestBonusActuals:
    """A bonus is its own payment with its own deductions. Recording it against the payslip
    would have meant the second payment of a month overwriting the first."""

    def test_a_bonus_carries_what_was_actually_paid(self, session):
        with session.begin():
            reference.set_bonus(
                session, "2026-05", Decimal("29028.48"), "annual bonus",
                gross=Decimal("29028.48"), ni=Decimal("580.57"),
                paye=Decimal("13062.82"), net=Decimal("15385.09"), payday=20,
            )
        frame = repo.load_bonuses(session).set_index("period")
        assert frame.loc["2026-05", "gross"] == Decimal("29028.48")
        assert frame.loc["2026-05", "net"] == Decimal("15385.09")
        assert frame.loc["2026-05", "payday"] == 20

    def test_a_bonus_and_a_payslip_coexist_in_one_month(self):
        """The complaint that prompted this: entering the second payment replaced the
        first, because a payslip is keyed by period."""
        payslip = pd.DataFrame(
            [{"period": "2026-05", "gross": Decimal("10695.85"),
              "net": Decimal("6100.00")}]
        ).set_index("period")
        bonus = pd.DataFrame(
            [{"period": "2026-05", "gross": Decimal("29028.48"),
              "net": Decimal("15385.09")}]
        ).set_index("period")
        combined = payslip.loc["2026-05", "gross"] + bonus.loc["2026-05", "gross"]
        assert combined == Decimal("39724.33")

    def test_the_actuals_are_optional(self, session):
        with session.begin():
            reference.set_bonus(session, "2026-05", Decimal("1000"))
        frame = repo.load_bonuses(session).set_index("period")
        assert frame.loc["2026-05", "gross"] is None

    def test_saving_a_zero_removes_the_bonus(self, session):
        with session.begin():
            reference.set_bonus(session, "2026-05", Decimal("1000"))
        with session.begin():
            reference.set_bonus(session, "2026-05", Decimal("0"))
        assert repo.load_bonuses(session).empty

    def test_a_bonus_can_be_removed_outright(self, session):
        with session.begin():
            reference.set_bonus(session, "2026-05", Decimal("1000"))
        with session.begin():
            outcome = reference.remove_bonus(session, "2026-05")
        assert outcome.ok
        assert repo.load_bonuses(session).empty

    def test_removing_a_bonus_that_is_not_there_says_so(self, session):
        with session.begin():
            assert not reference.remove_bonus(session, "2026-05").ok

    def test_a_payslip_can_be_removed(self, session):
        with session.begin():
            reference.set_payslip(session, "2026-05", gross=Decimal("100"))
        with session.begin():
            outcome = reference.remove_payslip(session, "2026-05")
        assert outcome.ok
        assert repo.load_payslips(session).empty

    def test_removing_a_payslip_that_is_not_there_says_so(self, session):
        with session.begin():
            assert not reference.remove_payslip(session, "2026-05").ok


# ------------------------------------------------------------------- scoped projections


class TestScopedProjections:
    """The planning grid can be filtered, so saving one classification must not delete
    what the filter is hiding."""

    # Reads are done inside the first begin() block: reading between two of them autobegins
    # a transaction, and the next `with session.begin()` then raises.
    def seed(self, session):
        with session.begin():
            classifications = repo.load_reference(session)["classifications"]
            ids = dict(zip(classifications["name"], classifications["id"]))
            reference.set_projection(
                session, dt.date(2026, 7, 3), int(ids["Food"]), Decimal("40")
            )
            reference.set_projection(
                session, dt.date(2026, 7, 4), int(ids["Excess"]), Decimal("25")
            )
        return ids

    def test_clearing_everything_still_works(self, session):
        self.seed(session)
        with session.begin():
            assert reference.clear_projections(session, "2026-07") == 2
            assert repo.load_projections(session).empty

    def test_clearing_one_classification_leaves_the_others(self, session):
        ids = self.seed(session)
        with session.begin():
            removed = reference.clear_projections(
                session, "2026-07", [int(ids["Food"])]
            )
            remaining = repo.load_projections(session)
        assert removed == 1
        assert list(remaining["classification"]) == ["Excess"]

    def test_another_month_is_untouched(self, session):
        with session.begin():
            classifications = repo.load_reference(session)["classifications"]
            ids = dict(zip(classifications["name"], classifications["id"]))
            reference.set_projection(
                session, dt.date(2026, 6, 3), int(ids["Food"]), Decimal("40")
            )
            reference.set_projection(
                session, dt.date(2026, 7, 3), int(ids["Food"]), Decimal("40")
            )
        with session.begin():
            reference.clear_projections(session, "2026-07", [int(ids["Food"])])
            assert len(session.query(Projection).all()) == 1


# ---------------------------------------------------------------------- display helpers


class TestDisplayHelpers:
    def test_money_carries_a_thousands_separator(self):
        assert ui.money(Decimal("10000")) == "£10,000.00"
        assert ui.money(None) == "-"

    def test_percent_is_quoted_not_fractional(self):
        assert ui.percent(Decimal("80")) == "80.00"
        assert ui.percent(Decimal("8.5")) == "8.50"

    def test_the_editable_money_format_asks_for_a_separator(self):
        """printf has no thousands flag, but sprintf-js -- which is what column_config
        parses -- treats ',' as one. Without it every data_editor showed £39255.98."""
        assert "," in ui.MONEY_FORMAT

    def test_the_default_range_is_the_last_ninety_days(self):
        start, end = ui.default_range(90)
        assert (end - start).days == 90
        assert end == dt.date.today()

    def test_the_default_range_is_clamped_to_what_exists(self):
        earliest = dt.date.today() - dt.timedelta(days=10)
        start, end = ui.default_range(90, earliest, dt.date.today())
        assert start == earliest


class TestNamedBlanks:
    """A transfer has no category or classification, so those cells are genuinely empty.
    pandas renders that as 'nan', which reads like a broken row."""

    FRAME = pd.DataFrame(
        [
            {"type": "Transfer", "category": None, "classification": None},
            {"type": "Debit", "category": "Food", "classification": "Food"},
            {"type": "Debit", "category": None, "classification": None},
        ]
    )

    def test_a_transfers_blanks_are_named(self):
        out = ui.name_blanks(self.FRAME, ["category", "classification"])
        assert out.loc[0, "category"] == "Transfer"
        assert out.loc[0, "classification"] == "Transfer"

    def test_other_blanks_get_a_dash(self):
        out = ui.name_blanks(self.FRAME, ["category"])
        assert out.loc[2, "category"] == "—"

    def test_values_that_are_there_are_left_alone(self):
        out = ui.name_blanks(self.FRAME, ["category"])
        assert out.loc[1, "category"] == "Food"

    def test_a_missing_column_is_ignored(self):
        assert "nonsense" not in ui.name_blanks(self.FRAME, ["nonsense"]).columns


class TestDescribeTxn:
    """The picker label. The obvious spelling -- `category or type` -- is wrong: a missing
    category arrives as float NaN, which is truthy, so every transfer read 'nan'."""

    def row(self, **fields):
        base = {
            "id": 7, "date": pd.Timestamp("2026-07-09"), "amount": Decimal("1234.50"),
            "type": "Debit", "account_from": "HSBC", "account_to": None,
            "category": "Food", "classification": "Food",
        }
        base.update(fields)
        return next(pd.DataFrame([base]).itertuples())

    def test_an_ordinary_transaction_names_its_category(self):
        assert "Food" in ui.describe_txn(self.row())

    def test_a_transfer_is_named_rather_than_shown_as_nan(self):
        label = ui.describe_txn(
            self.row(type="Transfer", category=None, classification=None,
                     account_to="Savings")
        )
        assert "nan" not in label
        assert "Transfer" in label
        assert "HSBC → Savings" in label

    def test_the_amount_carries_a_separator(self):
        assert "£1,234.50" in ui.describe_txn(self.row())

    def test_a_missing_category_falls_back_to_the_classification(self):
        assert "Excess" in ui.describe_txn(
            self.row(category=None, classification="Excess")
        )


# ------------------------------------------------------------------- monthly vs annual


@pytest.mark.parametrize(
    "monthly, annual, was_monthly, was_annual, expected",
    [
        (100.0, 1200.0, 100.0, 1200.0, None),          # nothing moved
        (100.0, 2400.0, 100.0, 1200.0, Decimal("200")),  # annual edited
        (200.0, 1200.0, 100.0, 1200.0, Decimal("200")),  # monthly edited
        (300.0, 2400.0, 100.0, 1200.0, Decimal("200")),  # both: annual wins
    ],
)
def test_monthly_and_annual_reconcile(monthly, annual, was_monthly, was_annual, expected):
    assert repo.reconcile_monthly_annual(monthly, annual, was_monthly, was_annual) == (
        expected
    )
