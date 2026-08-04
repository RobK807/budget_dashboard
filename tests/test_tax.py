"""UK PAYE and NI, ported from the Salary tracker's S8 and U8 formulas.

The figures below come from the workbook itself, so these are regression tests against a
known-good source rather than my own arithmetic.
"""

import datetime as dt
from decimal import Decimal

import pytest

from budget import tax

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


class TestAgainstTheWorkbook:
    """Gross, benefits and additional pay are the workbook's P, Q and R; the expected NI,
    PAYE and net are its S, U and W."""

    @pytest.mark.parametrize(
        "month,gross,benefits,additional,ni,paye,net",
        [
            # April: the ordinary month.
            ("2026-04", "10501.87", "1159.40", "24", "354.35", "3130.65", "5881.47"),
            # May: bonus month, well into additional rate.
            ("2026-05", "39724.33", "1177.88", "24", "938.43", "17452.82", "20179.20"),
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
