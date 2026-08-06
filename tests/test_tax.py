"""UK PAYE and NI, ported from the Salary tracker's S8 and U8 formulas.

The figures below come from the workbook itself, so these are regression tests against a
known-good source rather than my own arithmetic.
"""

import datetime as dt
from decimal import Decimal

import pytest

from budget import repo, tax

# Monthly bands from Salary tracker C18:F36 for 2026/27.
BANDS = tax.Bands(
    ni_lower_earnings_limit=Decimal("1048"),
    ni_upper_earnings_limit=Decimal("4189"),
    ni_lower_rate=Decimal("0.08"),
    ni_higher_rate=Decimal("0.02"),
    basic_band=Decimal("3141.666666666667"),
    higher_threshold=Decimal("10428.333333333334"),
    basic_rate=Decimal("0.2"),
    higher_rate=Decimal("0.4"),
    additional_rate=Decimal("0.45"),
    allowance_steps=(
        (dt.date(2026, 4, 1), Decimal("-55")),
        (dt.date(2026, 6, 1), Decimal("-246.41666666666666")),
        (dt.date(2026, 8, 1), Decimal("-229.91666666666666")),
        (dt.date(2026, 12, 1), Decimal("-229.91666666666666")),
    ),
)


class TestAllowanceSteps:
    """MATCH(..., $F$24:$F$27, 1): the last step whose date has passed."""

    @pytest.mark.parametrize(
        "on,expected",
        [
            (dt.date(2026, 4, 15), Decimal("-55")),
            (dt.date(2026, 5, 31), Decimal("-55")),
            (dt.date(2026, 6, 1), Decimal("-246.41666666666666")),
            (dt.date(2026, 7, 20), Decimal("-246.41666666666666")),
            (dt.date(2026, 8, 1), Decimal("-229.91666666666666")),
            (dt.date(2027, 3, 1), Decimal("-229.91666666666666")),
        ],
    )
    def test_step_in_force(self, on, expected):
        assert BANDS.allowance_for(on) == expected

    def test_before_the_first_step_there_is_no_adjustment(self):
        assert BANDS.allowance_for(dt.date(2026, 1, 1)) == Decimal("0")


class TestCarAllowance:
    """12% of the first 50,000 of base plus 5% above it."""

    @pytest.mark.parametrize(
        "base,expected",
        [
            ("118905", "9445.25"),   # Tax Calculator B20
            ("116688", "9334.40"),   # April's, its C20
            ("50000", "6000"),       # exactly at the threshold
            ("40000", "4800"),       # entirely below it
            ("0", "0"),
        ],
    )
    def test_it_matches_the_tax_calculator(self, base, expected):
        assert repo.car_allowance(Decimal(base)) == Decimal(expected)

    def test_the_rates_are_parameters(self):
        params = dict(repo.SALARY_PARAMETERS)
        params["car_allowance_upper_rate"] = Decimal("6")
        assert repo.car_allowance(Decimal("100000"), params) == Decimal("9000")


class TestComponents:
    """The decomposition, checked against the Tax Calculator's A18:D25 and G17:H23."""

    # Base 118,905 -- monthly figures as the workbook computes them.
    PARTS = tax.Components(
        base=Decimal("9908.75"),
        car=Decimal("787.10"),
        home_working=Decimal("24"),
        pension=Decimal("990.88"),
        holiday_pay=Decimal("187"),
    )

    def test_monthly_gross_matches_b22(self):
        assert self.PARTS.total_pay == Decimal("10719.85")

    def test_taxable_matches_b25(self):
        """B25 = B22 - pension - holiday - home working: the allowance is not taxable."""
        assert self.PARTS.taxable == Decimal("9517.97")

    def test_the_home_working_allowance_is_outside_the_tax_calculation(self):
        without = tax.Components(
            base=self.PARTS.base, car=self.PARTS.car, pension=self.PARTS.pension,
            holiday_pay=self.PARTS.holiday_pay,
        )
        assert without.taxable == self.PARTS.taxable

    def test_payslip_gross_is_net_of_the_sacrificed_pension(self):
        """What the payslip actually states, verified against June and July: 9,728.97."""
        assert self.PARTS.payslip_gross == Decimal("9728.97")

    def test_ni_matches_the_tax_calculator(self):
        """Its I7 comes to 357.8595 on this taxable figure."""
        assert tax.national_insurance(self.PARTS.taxable, BANDS) == Decimal("357.86")

    def test_net_is_pay_less_pension_holiday_ni_and_paye(self):
        got = tax.pay_for(self.PARTS, BANDS, dt.date(2026, 6, 1))
        assert got.net == (
            self.PARTS.total_pay - self.PARTS.pension - self.PARTS.holiday_pay
            - got.ni - got.paye
        )


class TestHigherBandOverlap:
    """The workbook capped the higher-rate slice at the additional-rate threshold instead of
    at the width of the higher band, so the two overlapped by exactly the basic band."""

    def test_an_ordinary_month_is_unaffected(self):
        """It never reaches the additional rate, which is why this went unnoticed."""
        assert tax.income_tax(
            Decimal("9517.97"), BANDS, dt.date(2026, 6, 1)
        ) == Decimal("3277.42")

    def test_a_month_reaching_the_additional_rate_no_longer_double_charges(self):
        assert tax.income_tax(
            Decimal("38546.45"), BANDS, dt.date(2026, 5, 1)
        ) == Decimal("16196.15")

    def test_the_slices_never_exceed_the_taxable_amount(self):
        """The property the overlap broke: the bands partition the pay, they do not overlap."""
        taxable = Decimal("38546.45")
        adjustment = BANDS.allowance_for(dt.date(2026, 5, 1))
        basic = min(taxable - adjustment, BANDS.basic_band)
        higher = min(
            max(Decimal("0"), taxable - adjustment - BANDS.basic_band),
            BANDS.higher_threshold - BANDS.basic_band,
        )
        additional = max(Decimal("0"), taxable - BANDS.higher_threshold)
        assert basic + higher + additional <= taxable - adjustment


class TestAgainstTheWorkbook:
    """Gross, benefits and additional pay are the workbook's P, Q and R; the expected NI,
    PAYE and net are its S, U and W."""

    @pytest.mark.parametrize(
        "month,gross,benefits,additional,ni,paye,net",
        [
            # April: the ordinary month.
            ("2026-04", "10501.87", "1159.40", "24", "354.35", "3130.65", "5881.47"),
            # May: bonus month, well into additional rate. The workbook said 17,452.82 and
            # 20,179.20; both were wrong, because it capped the higher-rate slice at the
            # additional-rate *threshold* rather than at the width of the higher band, so
            # 3,141.67 was charged at 40% and again at 45%. The real May payslips total
            # 16,169.64 of PAYE, which the corrected figure is 26.51 from and the workbook's
            # was 1,283.18 from. This is the one case in the file where the workbook is not
            # the authority -- an actual payslip is.
            ("2026-05", "39724.33", "1177.88", "24", "938.43", "16196.15", "21435.87"),
            # August: after the third allowance step.
            ("2026-08", "10695.85", "1177.88", "24", "357.86", "3270.82", "5913.29"),
            # March: the last month of the fiscal year.
            ("2027-03", "10695.85", "1177.88", "24", "357.86", "3270.82", "5913.29"),
        ],
    )
    def test_matches(self, month, gross, benefits, additional, ni, paye, net):
        year, mon = (int(p) for p in month.split("-"))
        got = tax.expected_pay(
            Decimal(gross), BANDS, dt.date(year, mon, 1),
            Decimal(benefits), Decimal(additional),
        )
        assert got.ni == Decimal(ni)
        assert got.paye == Decimal(paye)
        assert got.net == Decimal(net)


class TestCumulativeBands:
    """`months` scales every threshold. One month is what a payslip is charged; twelve is
    what HMRC reconciles the year against."""

    def test_one_month_is_the_existing_calculation(self):
        """The default has to be untouched -- every payslip figure depends on it."""
        for taxable in ("9517.97", "38546.45", "500", "0"):
            on = dt.date(2026, 6, 1)
            assert tax.income_tax(Decimal(taxable), BANDS, on, months=1) == tax.income_tax(
                Decimal(taxable), BANDS, on
            )

    def test_twelve_months_of_identical_pay_costs_twelve_times_one(self):
        """The property that makes the two bases agree in a year with no bonus.

        To 2p, not exactly: twelve figures each rounded to the penny against one figure
        rounded once. HMRC rounds every cumulative step too, so this is the real behaviour
        rather than an approximation of it -- but it means the reconciliation view should
        never be read as significant to the last penny.
        """
        monthly = Decimal("9517.97")
        on = dt.date(2026, 8, 1)
        annual = tax.income_tax(monthly * 12, BANDS, on, months=12)
        assert abs(annual - tax.income_tax(monthly, BANDS, on) * 12) <= Decimal("0.02")

    def test_the_additional_rate_starts_at_twelve_times_the_monthly_threshold(self):
        """125,140 a year, not 10,428.33 -- the whole reason a bonus month overpays."""
        on = dt.date(2026, 8, 1)
        above = BANDS.higher_threshold * 12 + Decimal("10000")
        charged = tax.income_tax(above, BANDS, on, months=12)
        more = tax.income_tax(above + Decimal("1000"), BANDS, on, months=12)
        assert more - charged == Decimal("1000") * BANDS.additional_rate

    def test_the_allowance_adjustment_leaves_a_flat_stretch_below_the_top_threshold(self):
        """A quirk carried over from the workbook, pinned so it is not mistaken for new.

        The adjustment is applied to the basic and higher slices but not to the
        additional-rate threshold, so once the adjusted pay has used up both lower bands
        there is a stretch -- here 122,381 to 125,140 a year -- taxed at nothing at the
        margin. It does not bite on any real figure in this file: the bonus year is well
        past it either side. Left alone deliberately, because the monthly form of the same
        formula is what reproduces the payslips.
        """
        on = dt.date(2026, 8, 1)
        flat = [
            tax.income_tax(Decimal(amount), BANDS, on, months=12)
            for amount in ("122381", "123000", "125140")
        ]
        assert len(set(flat)) == 1


class TestYearToDate:
    """The reconciliation, against the real 2026/27 figures.

    Taxable pay by month comes from the workbook rows in TestAgainstTheWorkbook: April on the
    old salary, May carrying the bonus, everything after it steady.
    """

    APRIL = Decimal("9342.47")
    MAY = Decimal("38546.45")
    STEADY = Decimal("9517.97")

    def entries(self, count=12):
        months = [self.APRIL, self.MAY] + [self.STEADY] * 10
        dates = (
            [dt.date(2026, 4, 1), dt.date(2026, 5, 1)]
            + [dt.date(2026, m, 1) for m in range(6, 13)]
            + [dt.date(2027, m, 1) for m in (1, 2, 3)]
        )
        return [
            (taxable, BANDS, on, tax.income_tax(taxable, BANDS, on), False)
            for taxable, on in list(zip(months, dates))[:count]
        ]

    def test_the_first_month_is_the_same_either_way(self):
        """Nothing has accumulated yet, so the two bases cannot disagree."""
        first = tax.year_to_date(self.entries(1))[0]
        assert first.due == Decimal("3130.65")
        assert first.difference == Decimal("0")

    def test_a_bonus_month_overpays_on_the_per_month_basis(self):
        may = tax.year_to_date(self.entries(2))[1]
        assert may.due == Decimal("16119.86")        # cumulative
        assert may.deducted == Decimal("16196.15")   # per-month, as payroll charges it
        assert may.difference == Decimal("76.29")

    def test_the_overpayment_grows_every_month_after_the_bonus(self):
        """Each later month is charged a full month's bands again, where cumulatively most
        of that capacity went on the bonus."""
        year = tax.year_to_date(self.entries())
        after = [point.difference for point in year[1:]]
        assert after == sorted(after)  # monotonic
        assert all(b > a for a, b in zip(after, after[1:]))

    def test_the_year_ends_overpaid_by_the_full_difference(self):
        year = tax.year_to_date(self.entries())
        closing = year[-1]
        assert closing.taxable_to_date == Decimal("143068.62")
        assert closing.due_to_date == Decimal("50583.88")
        assert closing.deducted_to_date == Decimal("52048.20")
        assert closing.difference == Decimal("1464.32")

    def test_the_monthly_charge_falls_below_the_per_month_one_after_a_bonus(self):
        """The bands the bonus used up are not charged again, so June's cumulative share is
        well under what payroll actually takes. Flooring this -- at zero, or at the previous
        month -- would hide exactly what the view exists to show."""
        june = tax.year_to_date(self.entries())[2]
        assert june.due == Decimal("3133.34")
        assert june.due < june.deducted == Decimal("3277.42")

    def test_a_year_without_a_bonus_reconciles_to_within_the_rounding(self):
        """The control. Steady pay on steady bands: the two bases agree bar 2p of penny
        rounding, so any real difference the view reports comes from an event in the year
        rather than from the arithmetic."""
        steady = [
            (self.STEADY, BANDS, dt.date(2026, 8, 1),
             tax.income_tax(self.STEADY, BANDS, dt.date(2026, 8, 1)), True)
            for _ in range(12)
        ]
        assert abs(tax.year_to_date(steady)[-1].difference) <= Decimal("0.02")

    def test_actual_and_modelled_months_are_distinguished(self):
        entries = self.entries(3)
        entries[0] = entries[0][:4] + (True,)
        year = tax.year_to_date(entries)
        assert [point.actual for point in year] == [True, False, False]

    def test_an_empty_year_is_not_an_error(self):
        assert tax.year_to_date([]) == []


class TestNationalInsurance:
    def test_earnings_below_the_lower_limit_pay_nothing(self):
        assert tax.national_insurance(Decimal("500"), BANDS) == Decimal("0")

    def test_between_the_limits_pays_the_lower_rate(self):
        # (2048 - 1048) * 8%
        assert tax.national_insurance(Decimal("2048"), BANDS) == Decimal("80.00")

    def test_above_the_upper_limit_adds_the_higher_rate(self):
        # (4189 - 1048) * 8% + (5189 - 4189) * 2%
        expected = (Decimal("3141") * Decimal("0.08") + Decimal("1000") * Decimal("0.02"))
        assert tax.national_insurance(Decimal("5189"), BANDS) == expected.quantize(
            Decimal("0.01")
        )


class TestBenefits:
    def test_benefits_reduce_the_taxable_amount(self):
        """Salary sacrifice: deducted before tax and never reaching the payslip."""
        without = tax.expected_pay(Decimal("5000"), BANDS, dt.date(2026, 4, 1))
        with_benefit = tax.expected_pay(
            Decimal("5000"), BANDS, dt.date(2026, 4, 1), benefits=Decimal("500")
        )
        assert with_benefit.taxable == Decimal("4500")
        assert with_benefit.paye < without.paye
        assert with_benefit.ni < without.ni

    def test_additional_pay_is_added_after_tax(self):
        base = tax.expected_pay(Decimal("5000"), BANDS, dt.date(2026, 4, 1))
        with_extra = tax.expected_pay(
            Decimal("5000"), BANDS, dt.date(2026, 4, 1), additional=Decimal("100")
        )
        assert with_extra.paye == base.paye
        assert with_extra.net == base.net + Decimal("100")
