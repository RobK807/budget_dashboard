"""UK PAYE and National Insurance, monthly.

A port of the Salary tracker's S8 (NI) and U8 (PAYE) formulas. Pure functions over an
explicit set of bands, so the arithmetic can be tested without a spreadsheet and the bands
can change without touching the code.

The tapered personal allowance is the awkward part: it is adjusted in four steps across the
year (Salary tracker C24:C27, each with a start date in F24:F27) as HMRC revises the code.
`allowance_for` picks the step in force on a given date -- the same thing the workbook's
`MATCH(..., $F$24:$F$27, 1)` does.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

PENCE = Decimal("0.01")


def _round(value: Decimal) -> Decimal:
    return Decimal(value).quantize(PENCE, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Bands:
    """Monthly thresholds and rates. Names follow the Salary tracker's labels."""

    ni_lower_earnings_limit: Decimal      # D18
    ni_upper_earnings_limit: Decimal      # D19
    ni_lower_rate: Decimal                # D20
    ni_higher_rate: Decimal               # D21
    basic_band: Decimal                   # D36, = basic_rate_threshold - personal_allowance
    higher_threshold: Decimal             # D30
    basic_rate: Decimal                   # D32
    higher_rate: Decimal                  # D33
    additional_rate: Decimal              # D34
    # (effective_from, adjustment) in date order; the adjustment is negative.
    allowance_steps: tuple[tuple[dt.date, Decimal], ...] = ()
    # The two inputs behind basic_band. Carried so the Salary page can show what was
    # entered rather than only the figure derived from it; the arithmetic below uses
    # basic_band, exactly as the workbook's formulas referenced D36.
    personal_allowance: Decimal = Decimal("0")    # D22
    basic_rate_threshold: Decimal = Decimal("0")  # D28

    def allowance_for(self, on: dt.date) -> Decimal:
        """The personal-allowance adjustment in force on a date.

        The last step whose start date has passed, matching MATCH(..., 1). Before the first
        step there is no adjustment.
        """
        applicable = Decimal("0")
        for start, adjustment in sorted(self.allowance_steps):
            if on >= start:
                applicable = adjustment
        return applicable


@dataclass(frozen=True)
class Components:
    """A month's pay, decomposed into the parts that behave differently.

    The distinction that matters is what each part does to the taxable figure:

        base            pensionable, taxable
        car allowance   taxable, but *not* pensionable -- the pension is a percentage of
                        base alone, which is why the two cannot stay lumped together
        bonus           taxable in the month it is paid
        home working    paid on top and not taxable at all, so it never reaches the
                        NI or PAYE calculation
        pension         deducted before tax
        holiday pay     deducted before tax

    Reproduces the Tax Calculator's A18:D25. Its B25, 'Monthly (for calcs)', is
    `B22 - B23 - B24 - B21` -- the monthly gross less pension, less holiday pay, and less
    the home working allowance that B22 had just added, which is the roundabout way of
    saying the allowance is not taxable.
    """

    base: Decimal = Decimal("0")
    car: Decimal = Decimal("0")
    bonus: Decimal = Decimal("0")
    home_working: Decimal = Decimal("0")
    pension: Decimal = Decimal("0")
    holiday_pay: Decimal = Decimal("0")

    @property
    def gross(self) -> Decimal:
        """Pay before deductions, excluding the allowance paid on top."""
        return self.base + self.car + self.bonus

    @property
    def total_pay(self) -> Decimal:
        return self.gross + self.home_working

    @property
    def payslip_gross(self) -> Decimal:
        """Gross *as the payslip states it* -- which is not base plus car allowance.

        The pension is salary sacrifice, so it comes out before gross is reported, and the
        home working allowance is inside it. Verified against every recorded month:

            April  9,724.00 + 777.87 - 972.40 + 24 = 9,553.47   payslip 9,553.47
            June   9,908.75 + 787.10 - 990.88 + 24 = 9,728.97   payslip 9,728.97
            May    the same plus the 29,028.48 bonus = 38,757.45  payslip 38,757.45

        Worth keeping distinct from `gross`: comparing the model's base-plus-car against a
        payslip figure that is net of pension shows a phantom 966.88 gap every month.
        """
        return self.total_pay - self.pension

    @property
    def taxable(self) -> Decimal:
        """What NI and PAYE are actually charged on -- the Tax Calculator's B25.

        Excludes the home working allowance, which `payslip_gross` includes: B25 is
        `B22 - B23 - B24 - B21`, and that last term takes the allowance back out again.
        """
        return self.gross - self.pension - self.holiday_pay


@dataclass(frozen=True)
class Breakdown:
    gross: Decimal
    benefits: Decimal
    additional: Decimal
    taxable: Decimal
    ni: Decimal
    paye: Decimal
    net: Decimal
    components: Components = field(default_factory=Components)

    @property
    def ni_rate(self) -> Decimal:
        return self.ni / self.gross if self.gross else Decimal("0")

    @property
    def paye_rate(self) -> Decimal:
        return self.paye / self.gross if self.gross else Decimal("0")


def national_insurance(taxable: Decimal, bands: Bands) -> Decimal:
    """Salary tracker S8.

        MIN(UEL - LEL, taxable - LEL) * lower + MAX(0, taxable - UEL) * higher

    The first term is negative below the lower limit, which is why it is floored at zero --
    the workbook relies on the earnings never being that low rather than saying so.
    """
    lel, uel = bands.ni_lower_earnings_limit, bands.ni_upper_earnings_limit
    main = max(Decimal("0"), min(uel - lel, taxable - lel)) * bands.ni_lower_rate
    upper = max(Decimal("0"), taxable - uel) * bands.ni_higher_rate
    return _round(main + upper)


def income_tax(taxable: Decimal, bands: Bands, on: dt.date) -> Decimal:
    """Salary tracker U8, with its band overlap corrected.

    The personal-allowance adjustment is *subtracted* in the formula and is itself negative,
    so it increases the amount taxed at basic rate. Kept in that form rather than flipped,
    so the two can be compared line for line.

    The workbook capped the higher-rate slice at `higher_threshold` -- the point where the
    additional rate starts -- rather than at the *width* of the higher-rate band. The two
    differ by the basic band, so any month reaching the additional rate had 3,141.67 charged
    at 40% and again at 45%. Ordinary months never get there, which is why it went unnoticed:
    the only month it ever bit was the bonus one, where it over-taxed by 1,256.67. Against
    the real May payslip the corrected version is 26.51 out where the original was 1,283.18.

    The thresholds themselves are untouched -- this is the arithmetic between them.
    """
    adjustment = bands.allowance_for(on)

    basic = min(taxable - adjustment, bands.basic_band) * bands.basic_rate
    above_basic = max(Decimal("0"), taxable - adjustment - bands.basic_band)
    higher_width = max(Decimal("0"), bands.higher_threshold - bands.basic_band)
    higher = min(above_basic, higher_width) * bands.higher_rate
    additional = max(Decimal("0"), taxable - bands.higher_threshold) * bands.additional_rate
    return _round(basic + higher + additional)


def pay_for(components: Components, bands: Bands, on: dt.date) -> Breakdown:
    """A month's expected pay from its decomposed parts.

    Reproduces the Tax Calculator's G17:H23:

        net = gross - pension - holiday pay - NI - income tax

    where gross includes the home working allowance (H18 = B22) and NI and PAYE are charged
    on `components.taxable`, which does not. The allowance is paid on top and taxed nowhere,
    so it passes straight through to net.
    """
    ni = national_insurance(components.taxable, bands)
    paye = income_tax(components.taxable, bands, on)
    return Breakdown(
        gross=components.gross,
        benefits=components.pension + components.holiday_pay,
        additional=components.home_working,
        taxable=components.taxable,
        ni=ni,
        paye=paye,
        net=_round(
            components.total_pay
            - components.pension
            - components.holiday_pay
            - ni
            - paye
        ),
        components=components,
    )


def expected_pay(
    gross: Decimal,
    bands: Bands,
    on: dt.date,
    benefits: Decimal = Decimal("0"),
    additional: Decimal = Decimal("0"),
) -> Breakdown:
    """Salary tracker W8, in the shape the old workbook held it: one lumped `benefits`
    figure and one lumped `additional`.

    A thin wrapper over `pay_for` rather than a second implementation. The old workbook's
    `benefits` was pension *and* holiday pay added together -- 990.875 + 187.00 = 1,177.88 --
    and its `additional` was the home working allowance. Splitting those is what the
    decomposition above is for; this remains so the original figures can still be reproduced
    exactly, and so there is only ever one piece of arithmetic to be wrong.
    """
    return pay_for(
        Components(
            base=gross, pension=benefits, home_working=additional
        ),
        bands,
        on,
    )
