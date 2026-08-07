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


def income_tax(
    taxable: Decimal, bands: Bands, on: dt.date, months: int = 1
) -> Decimal:
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

    `months` scales every threshold, which is the whole of what separates a monthly charge
    from a cumulative one: a tax year's bands are twelve times a month's, and n months of
    them are n times. At the default of 1 this is the monthly figure a payslip is charged and
    the arithmetic is identical to before. See `year_to_date` for what the other values are
    for -- and for why the monthly figure remains the one the Salary page models against.
    """
    return _round(sum(slice.amount for slice in band_split(taxable, bands, on, months)))


@dataclass(frozen=True)
class BandSlice:
    """How much of a taxable figure fell in one band, and what it cost there."""

    name: str
    eligible: Decimal
    rate: Decimal  # a fraction: 0.4, not 40
    amount: Decimal


def band_split(
    taxable: Decimal, bands: Bands, on: dt.date, months: int = 1
) -> list[BandSlice]:
    """The same arithmetic as `income_tax`, kept as its parts.

    Split out so the annual summary can show where a year's tax was charged without a second
    implementation of the bands to drift from this one -- `income_tax` is now the sum of what
    this returns, so they cannot disagree.
    """
    adjustment = bands.allowance_for(on) * months
    basic_band = bands.basic_band * months
    higher_threshold = bands.higher_threshold * months

    basic = min(taxable - adjustment, basic_band)
    above_basic = max(Decimal("0"), taxable - adjustment - basic_band)
    higher_width = max(Decimal("0"), higher_threshold - basic_band)
    higher = min(above_basic, higher_width)
    additional = max(Decimal("0"), taxable - higher_threshold)

    return [
        BandSlice("Personal allowance", adjustment, Decimal("0"), Decimal("0")),
        BandSlice("Basic rate", basic, bands.basic_rate, basic * bands.basic_rate),
        BandSlice("Higher rate", higher, bands.higher_rate, higher * bands.higher_rate),
        BandSlice(
            "Additional rate",
            additional,
            bands.additional_rate,
            additional * bands.additional_rate,
        ),
    ]


@dataclass(frozen=True)
class YearToDate:
    """One month's place in the tax year's running total.

    `due_to_date` is what the year's tax *should* come to by this point, charged the way HMRC
    reconciles a year: one set of bands stretched across the months elapsed, rather than a
    fresh set every month. `deducted_to_date` is what payroll has actually taken.
    """

    month: int                    # 1 = April, the first month of the UK tax year
    taxable: Decimal              # this month alone
    taxable_to_date: Decimal
    due: Decimal                  # this month's share of the cumulative charge
    due_to_date: Decimal
    deducted: Decimal
    deducted_to_date: Decimal
    actual: bool                  # deducted came from a payslip rather than the model

    @property
    def difference(self) -> Decimal:
        """Overpaid, if positive. What HMRC would settle at the end of the year."""
        return self.deducted_to_date - self.due_to_date


def year_to_date(
    entries: "list[tuple[Decimal, Bands, dt.date, Decimal, bool]]",
) -> list[YearToDate]:
    """Walk one tax year, comparing the cumulative charge against what was deducted.

    `entries` is ordered from April, each `(taxable, bands, on, deducted, actual)`.

    **This is a reconciliation, not the way the payslips are produced.** Payroll here charges
    each month on its own bands, and the recorded payslips say so plainly: June and July came
    to 3,276.86 against a per-month model of 3,277.42, while a cumulative model would have
    predicted 3,133.34 -- out by 143.52, twice. The May bonus was charged 13,011.09 on
    29,028.48, a flat 44.82%, which is the additional rate applied to the payment on its own
    rather than a year's bands being spread across it.

    So the per-month figure stays the one the Salary page models a payslip against, and this
    exists to answer the different question: taxed month by month, does the *year* come out
    right? It usually does not. A month carrying a bonus uses up a single month's basic and
    higher bands and throws the rest at the additional rate, where across a full year far
    more of it would have fallen below the additional-rate threshold. Nothing is wrong when
    these disagree -- HMRC reconciles after 5 April and refunds the difference. The point of
    showing it is to know the refund is coming, and roughly how large.

    The bands are taken per month rather than once for the year, because they are
    effective-dated: a threshold revised mid-year, or a tax code reissued, applies from the
    month it takes effect. The cumulative charge is therefore always computed on the bands in
    force at the month being reported, which is what reissuing a code cumulatively does.
    """
    out: list[YearToDate] = []
    taxable_to_date = Decimal("0")
    deducted_to_date = Decimal("0")
    previous_due = Decimal("0")

    for index, (taxable, bands, on, deducted, actual) in enumerate(entries, start=1):
        taxable_to_date += taxable
        deducted_to_date += deducted
        due_to_date = income_tax(taxable_to_date, bands, on, months=index)
        out.append(
            YearToDate(
                month=index,
                taxable=taxable,
                taxable_to_date=taxable_to_date,
                # Can be negative, and legitimately so: after a bonus month the cumulative
                # charge stops growing as fast as a per-month one, and a payroll running this
                # basis would be issuing a refund. Not floored at zero -- that would hide the
                # very thing this is here to show.
                due=_round(due_to_date - previous_due),
                due_to_date=due_to_date,
                deducted=deducted,
                deducted_to_date=deducted_to_date,
                actual=actual,
            )
        )
        previous_due = due_to_date

    return out


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


# ------------------------------------------------------------------- the annual summary
#
# What the year comes to once everything outside the payslip is counted: a benefit in kind,
# savings interest, dividends, and the relief a Gift Aid donation earns back.
#
# The Tax Calculator spreadsheet models Gift Aid the other way about -- it widens the basic
# rate band by the gross donation, which is what HMRC actually does. The figure asked for
# here is the taxpayer's side of the same thing: what comes back. Both are stated, so the
# refund can be read on its own and the tax figure still means tax charged.

# Standard 2025-26 allowances. Overridable per tax year, because they are not constants:
# the savings allowance is 1,000 for a basic-rate taxpayer and nothing at all above the
# higher-rate threshold.
SAVINGS_ALLOWANCE = Decimal("500")
DIVIDEND_ALLOWANCE = Decimal("1000")

# Gift Aid relief, as a percentage of the donation. 20 for a higher-rate taxpayer, 25 for an
# additional-rate one -- the gap between their rate and the basic rate the charity reclaims.
GIFT_AID_HIGHER = Decimal("20")
GIFT_AID_ADDITIONAL = Decimal("25")


@dataclass(frozen=True)
class AnnualTax:
    """A tax year totalled up, and where the charge fell."""

    employment: Decimal        # taxable pay, as the P60 states it
    benefits: Decimal          # benefit in kind, not on any payslip
    interest: Decimal          # received, gross
    dividends: Decimal
    taxable_interest: Decimal  # after the savings allowance
    taxable_dividends: Decimal
    total_taxable: Decimal
    bands: tuple[BandSlice, ...]
    tax_due: Decimal
    donations: Decimal
    gift_aid_rate: Decimal     # the percentage that applied, 0 below the higher rate
    gift_aid_refund: Decimal

    @property
    def net_of_relief(self) -> Decimal:
        """Tax charged less what Gift Aid gives back."""
        return self.tax_due - self.gift_aid_refund


def annual_summary(
    employment: Decimal,
    bands: Bands,
    on: dt.date,
    *,
    benefits: Decimal = Decimal("0"),
    interest: Decimal = Decimal("0"),
    dividends: Decimal = Decimal("0"),
    donations: Decimal = Decimal("0"),
    savings_allowance: Decimal = SAVINGS_ALLOWANCE,
    dividend_allowance: Decimal = DIVIDEND_ALLOWANCE,
    gift_aid_higher: Decimal = GIFT_AID_HIGHER,
    gift_aid_additional: Decimal = GIFT_AID_ADDITIONAL,
    months: int = 12,
) -> AnnualTax:
    """One tax year's charge, counting what the payslips do not.

    Interest and dividends carry their own allowances and only the excess is taxed, which is
    the spreadsheet's B43 and B44. Interest is taken gross here -- a simplification the user
    accepted, since an account paying net has already had basic rate deducted and the ledger
    does not separate the two.

    The Gift Aid rate is chosen from where the *total* taxable income lands, not from where
    the donation itself does: relief follows the taxpayer's marginal rate for the year.
    """
    taxable_interest = max(Decimal("0"), interest - savings_allowance)
    taxable_dividends = max(Decimal("0"), dividends - dividend_allowance)
    total_taxable = employment + benefits + taxable_interest + taxable_dividends

    split = band_split(total_taxable, bands, on, months)
    tax_due = _round(sum(s.amount for s in split))

    if total_taxable > bands.higher_threshold * months:
        rate = gift_aid_additional
    elif total_taxable > bands.basic_rate_threshold * months:
        rate = gift_aid_higher
    else:
        rate = Decimal("0")

    return AnnualTax(
        employment=employment,
        benefits=benefits,
        interest=interest,
        dividends=dividends,
        taxable_interest=taxable_interest,
        taxable_dividends=taxable_dividends,
        total_taxable=total_taxable,
        bands=tuple(split),
        tax_due=tax_due,
        donations=donations,
        gift_aid_rate=rate,
        gift_aid_refund=_round(donations * rate / 100),
    )
