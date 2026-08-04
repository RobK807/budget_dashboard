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
from dataclasses import dataclass
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
class Breakdown:
    gross: Decimal
    benefits: Decimal
    additional: Decimal
    taxable: Decimal
    ni: Decimal
    paye: Decimal
    net: Decimal

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
    """Salary tracker U8.

    The personal-allowance adjustment is *subtracted* in the formula and is itself negative,
    so it increases the amount taxed at basic rate. Kept in that form rather than flipped,
    so the two can be compared line for line.
    """
    adjustment = bands.allowance_for(on)

    basic = min(taxable - adjustment, bands.basic_band) * bands.basic_rate
    higher = (
        min(
            bands.higher_threshold,
            max(Decimal("0"), taxable - bands.basic_band - adjustment),
        )
        * bands.higher_rate
    )
    additional = max(Decimal("0"), taxable - bands.higher_threshold) * bands.additional_rate
    return _round(basic + higher + additional)


def expected_pay(
    gross: Decimal,
    bands: Bands,
    on: dt.date,
    benefits: Decimal = Decimal("0"),
    additional: Decimal = Decimal("0"),
) -> Breakdown:
    """Salary tracker W8: gross - benefits + additional - NI - PAYE.

    Benefits are deducted before tax and again from net -- salary sacrifice, so the amount
    never reaches the payslip.
    """
    taxable = gross - benefits
    ni = national_insurance(taxable, bands)
    paye = income_tax(taxable, bands, on)
    return Breakdown(
        gross=gross,
        benefits=benefits,
        additional=additional,
        taxable=taxable,
        ni=ni,
        paye=paye,
        net=_round(taxable + additional - ni - paye),
    )
