"""The pension: separating growth from money paid in.

Nearly everything here is one arithmetic claim -- that a rise in a pot is only a return once
the payments into it have been taken off -- checked from several directions, because that is
the single thing this feature exists to do and every number on the page is derived from it.

The figures in `TestAgainstTheSource` are the ones the original tracking sheet computed for
itself. They are here because agreement with an independent calculation is the difference
between 'the code runs' and 'the code is right', and because the two places where this
deliberately departs from that sheet are then visible as decisions rather than drift.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest

from budget import reference, repo
from budget.models import PensionContribution, PensionPot, PensionValuation

START = dt.date(2024, 1, 1)


def pots(*rows) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "id": index + 1,
                "name": name,
                "display_order": index,
                "valid_from": valid_from,
                "valid_to": valid_to,
                "note": None,
            }
            for index, (name, valid_from, valid_to) in enumerate(rows)
        ],
        columns=["id", "name", "display_order", "valid_from", "valid_to", "note"],
    )


def valuations(*rows) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"pot_id": pot_id, "on_date": on, "value": Decimal(str(value))}
            for pot_id, on, value in rows
        ],
        columns=["pot_id", "on_date", "value"],
    )


def payments(*rows) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "id": index + 1,
                "pot_id": pot_id,
                "on_date": on,
                "amount": Decimal(str(amount)),
                "kind": kind,
                "note": None,
            }
            for index, (pot_id, on, amount, kind) in enumerate(rows)
        ],
        columns=["id", "pot_id", "on_date", "amount", "kind", "note"],
    )


NO_PAYMENTS = payments()


# ------------------------------------------------------------------ the core arithmetic


class TestReturns:
    ONE = pots(("Provider", START, None))

    def test_a_pot_nothing_is_paid_into_is_simply_end_over_start(self):
        history = repo.pension_history(
            self.ONE,
            valuations((1, START, 1000), (1, dt.date(2024, 7, 1), 1100)),
            NO_PAYMENTS,
        )
        assert history.iloc[-1]["period_return"] == pytest.approx(10.0)

    def test_money_paid_in_is_not_a_return(self):
        """The whole point. A pot that took 100 and is worth 100 more has earned nothing,
        and the naive reading -- 1100 over 1000 -- calls that a 10% gain."""
        history = repo.pension_history(
            self.ONE,
            valuations((1, START, 1000), (1, dt.date(2024, 7, 1), 1100)),
            payments((1, dt.date(2024, 3, 1), 100, "contribution")),
        )
        assert history.iloc[-1]["period_return"] == pytest.approx(0.0)
        assert history.iloc[-1]["growth"] == Decimal("0")

    def test_a_charge_taken_out_is_not_a_loss_either(self):
        history = repo.pension_history(
            self.ONE,
            valuations((1, START, 1000), (1, dt.date(2024, 7, 1), 990)),
            payments((1, dt.date(2024, 3, 1), -10, "charge")),
        )
        assert history.iloc[-1]["period_return"] == pytest.approx(0.0)

    def test_the_first_valuation_has_no_return_to_report(self):
        """There is nothing before it to have returned anything over, and reporting zero
        would put a point on the chart that means something it does not."""
        history = repo.pension_history(self.ONE, valuations((1, START, 1000)), NO_PAYMENTS)
        assert pd.isna(history.iloc[0]["period_return"])
        assert history.iloc[0]["total_return"] == pytest.approx(0.0)

    def test_payments_are_counted_once_and_in_the_right_window(self):
        """A payment belongs to the period it lands in, and the windows are half-open, so a
        payment on a valuation date counts against that valuation and not the next one."""
        history = repo.pension_history(
            self.ONE,
            valuations(
                (1, START, 1000),
                (1, dt.date(2024, 4, 1), 1200),
                (1, dt.date(2024, 7, 1), 1400),
            ),
            payments(
                (1, dt.date(2024, 4, 1), 100, "contribution"),
                (1, dt.date(2024, 5, 1), 100, "contribution"),
            ),
        )
        assert history["flows"].tolist() == [Decimal("0"), Decimal("100"), Decimal("100")]

    def test_the_return_to_date_measures_against_everything_put_in(self):
        history = repo.pension_history(
            self.ONE,
            valuations(
                (1, START, 1000),
                (1, dt.date(2024, 4, 1), 1200),
                (1, dt.date(2024, 7, 1), 1500),
            ),
            payments(
                (1, dt.date(2024, 4, 1), 100, "contribution"),
                (1, dt.date(2024, 5, 1), 100, "contribution"),
            ),
        )
        closing = history.iloc[-1]
        assert closing["base"] == Decimal("1200")  # 1000 started with, 200 paid in
        assert closing["growth"] == Decimal("300")
        assert closing["total_return"] == pytest.approx(25.0)

    def test_a_year_of_growth_annualises_to_itself(self):
        history = repo.pension_history(
            self.ONE,
            valuations((1, START, 1000), (1, dt.date(2024, 12, 31), 1100)),
            NO_PAYMENTS,
        )
        # 365 days against a 365.25-day year: ten per cent, give or take the quarter day.
        assert history.iloc[-1]["period_annualised"] == pytest.approx(10.0, abs=0.02)

    def test_half_a_year_of_growth_annualises_to_more_than_itself(self):
        history = repo.pension_history(
            self.ONE,
            valuations((1, START, 1000), (1, dt.date(2024, 7, 1), 1100)),
            NO_PAYMENTS,
        )
        assert history.iloc[-1]["period_annualised"] == pytest.approx(21.03, abs=0.05)

    def test_a_pot_with_nothing_behind_it_reports_no_return_rather_than_infinity(self):
        """A pot opened at nothing and not yet paid into has no denominator. That is an
        unanswerable question, not a return of infinity."""
        history = repo.pension_history(
            self.ONE, valuations((1, START, 0), (1, dt.date(2024, 7, 1), 0)), NO_PAYMENTS
        )
        assert pd.isna(history.iloc[-1]["total_return"])
        assert pd.isna(history.iloc[-1]["period_return"])


# -------------------------------------------------------------------- several pots at once


class TestSeveralPots:
    TWO = pots(("Old", START, None), ("New", START, None))

    def test_a_pot_with_no_figure_of_its_own_is_carried_forward(self):
        """A provider that has not published keeps its last figure. Dropping it instead
        would show the total pension falling by the size of that pot."""
        history = repo.pension_history(
            self.TWO,
            valuations(
                (1, START, 1000), (2, START, 500), (1, dt.date(2024, 7, 1), 1100)
            ),
            NO_PAYMENTS,
        )
        july = history[history["date"] == dt.date(2024, 7, 1)]
        assert set(july["pot"]) == {"Old", "New"}
        carried = july[july["pot"] == "New"].iloc[0]
        assert carried["value"] == Decimal("500")
        assert not carried["stated"]
        assert carried["as_at"] == START

    def test_the_total_names_what_it_carried_forward(self):
        totals = repo.pension_totals(
            repo.pension_history(
                self.TWO,
                valuations(
                    (1, START, 1000), (2, START, 500), (1, dt.date(2024, 7, 1), 1100)
                ),
                NO_PAYMENTS,
            )
        )
        assert totals.iloc[-1]["value"] == Decimal("1600")
        assert totals.iloc[-1]["carried"] == ["New"]

    def test_a_pot_that_joins_late_is_not_reported_as_growth(self):
        """Its opening value is neither a gain nor a contribution, but it does have to go
        into the denominator -- otherwise the month a pot appears reports the whole of it
        as a return."""
        history = repo.pension_history(
            pots(("Old", START, None), ("New", dt.date(2024, 7, 1), None)),
            valuations(
                (1, START, 1000), (1, dt.date(2024, 7, 1), 1000), (2, dt.date(2024, 7, 1), 500)
            ),
            NO_PAYMENTS,
        )
        totals = repo.pension_totals(history)
        assert totals.iloc[-1]["value"] == Decimal("1500")
        assert totals.iloc[-1]["period_return"] == pytest.approx(0.0)

    def test_a_closed_pot_stops_being_carried_forward(self):
        history = repo.pension_history(
            pots(("Old", START, dt.date(2024, 4, 1)), ("New", START, None)),
            valuations(
                (1, START, 1000), (2, START, 500), (2, dt.date(2024, 7, 1), 600)
            ),
            NO_PAYMENTS,
        )
        july = history[history["date"] == dt.date(2024, 7, 1)]
        assert july["pot"].tolist() == ["New"]

    def test_the_combined_return_is_the_ratio_of_the_sums(self):
        """Not the average of the pots' returns. A middling return on a large pot is not
        the same as one on a small pot, and averaging the percentages says it is."""
        history = repo.pension_history(
            self.TWO,
            valuations(
                (1, START, 1000), (2, START, 100),
                (1, dt.date(2024, 7, 1), 1000), (2, dt.date(2024, 7, 1), 200),
            ),
            NO_PAYMENTS,
        )
        totals = repo.pension_totals(history)
        # 1200 over 1100 is 9.09%; the mean of 0% and 100% would be 50%.
        assert totals.iloc[-1]["period_return"] == pytest.approx(9.0909, abs=0.001)

    def test_nothing_at_all_gives_an_empty_frame_with_its_columns(self):
        """Pages index these columns before they check whether there are rows."""
        empty = repo.pension_history(pots(), valuations(), NO_PAYMENTS)
        assert empty.empty
        for column in ("date", "pot", "value", "base", "growth", "total_return"):
            assert column in empty.columns
        assert repo.pension_totals(empty).empty


# ------------------------------------------------------------------------- the ledger


class TestLedger:
    ONE = pots(("Provider", START, None))

    LEDGER = payments(
        (1, dt.date(2024, 3, 1), 100, "contribution"),
        (1, dt.date(2024, 3, 1), 150, "contribution"),
        (1, dt.date(2024, 4, 1), -2, "charge"),
        (1, dt.date(2024, 4, 25), 1, "interest"),
    )

    def test_the_running_total_follows_the_dates(self):
        ledger = repo.pension_ledger(self.LEDGER, self.ONE)
        assert ledger["running"].tolist() == [
            Decimal("100"), Decimal("250"), Decimal("248"), Decimal("249")
        ]

    def test_it_names_the_pot(self):
        assert repo.pension_ledger(self.LEDGER, self.ONE)["pot"].unique().tolist() == [
            "Provider"
        ]

    def test_the_summary_keeps_charges_apart_from_payments(self):
        """Charges run at pennies and grow with the pot, which is invisible inside a net
        figure and is exactly the sort of thing worth watching."""
        summary = repo.pension_contribution_summary(self.LEDGER, self.ONE).iloc[0]
        assert summary["paid_in"] == Decimal("251")
        assert summary["charges"] == Decimal("-2")
        assert summary["net"] == Decimal("249")
        assert summary["entries"] == 4

    def test_an_empty_ledger_still_has_its_columns(self):
        assert list(repo.pension_ledger(NO_PAYMENTS, self.ONE).columns)
        assert repo.pension_contribution_summary(NO_PAYMENTS, self.ONE).empty


# ---------------------------------------------------------------------------- writing


class TestWriting:
    def add(self, session, name="Provider", start=START):
        pot, outcome = reference.add_pension_pot(session, name, start)
        assert outcome.ok, outcome.message
        return pot

    def test_a_pension_can_be_added_and_not_twice(self, session):
        self.add(session)
        _, again = reference.add_pension_pot(session, "provider", START)
        assert not again.ok

    def test_a_valuation_is_keyed_by_pot_and_date(self, session):
        pot = self.add(session)
        reference.set_pension_valuation(session, pot.id, START, Decimal("1000"))
        reference.set_pension_valuation(session, pot.id, START, Decimal("1200"))
        held = session.get(PensionValuation, (pot.id, START))
        assert held.value == Decimal("1200")

    def test_a_valuation_can_be_cleared(self, session):
        """The only way back from a figure typed against the wrong date -- and a wrong date
        is the mistake with consequences, since every return either side reads from it."""
        pot = self.add(session)
        reference.set_pension_valuation(session, pot.id, START, Decimal("1000"))
        assert reference.set_pension_valuation(session, pot.id, START, None).ok
        assert session.get(PensionValuation, (pot.id, START)) is None

    def test_a_valuation_before_the_pot_was_tracked_is_refused(self, session):
        pot = self.add(session, start=dt.date(2024, 6, 1))
        outcome = reference.set_pension_valuation(session, pot.id, START, Decimal("1000"))
        assert not outcome.ok

    def test_two_identical_payments_on_one_day_both_stand(self, session):
        """The employer's share and your own can match to the penny, so this is ordinary
        rather than a duplicate to be swallowed."""
        pot = self.add(session)
        for _ in range(2):
            assert reference.add_pension_contribution(
                session, pot.id, dt.date(2024, 3, 1), Decimal("250")
            ).ok
        rows = session.query(PensionContribution).all()
        assert len(rows) == 2

    def test_a_payment_of_nothing_is_refused(self, session):
        pot = self.add(session)
        assert not reference.add_pension_contribution(
            session, pot.id, START, Decimal("0")
        ).ok

    def test_an_unknown_kind_is_refused(self, session):
        pot = self.add(session)
        assert not reference.add_pension_contribution(
            session, pot.id, START, Decimal("10"), kind="growth"
        ).ok

    def test_a_payment_can_be_removed(self, session):
        pot = self.add(session)
        reference.add_pension_contribution(session, pot.id, START, Decimal("10"))
        row = session.query(PensionContribution).one()
        assert reference.remove_pension_contribution(session, row.id).ok
        assert session.query(PensionContribution).count() == 0

    def test_a_pot_cannot_close_before_it_opened(self, session):
        pot = self.add(session)
        outcome = reference.update_pension_pot(
            session, pot.id, valid_to=dt.date(2023, 1, 1)
        )
        assert not outcome.ok

    def test_closing_a_pot_sticks(self, session):
        pot = self.add(session)
        assert reference.update_pension_pot(
            session, pot.id, valid_to=dt.date(2026, 1, 1)
        ).ok
        assert session.get(PensionPot, pot.id).valid_to == dt.date(2026, 1, 1)

    def test_what_was_written_reads_back(self, session):
        pot = self.add(session)
        reference.set_pension_valuation(session, pot.id, START, Decimal("1000"))
        reference.set_pension_valuation(
            session, pot.id, dt.date(2024, 7, 1), Decimal("1200")
        )
        reference.add_pension_contribution(
            session, pot.id, dt.date(2024, 4, 1), Decimal("100")
        )
        session.commit()

        history = repo.pension_history(
            repo.load_pension_pots(session),
            repo.load_pension_valuations(session),
            repo.load_pension_contributions(session),
        )
        assert history.iloc[-1]["growth"] == Decimal("100")
        assert history.iloc[-1]["period_return"] == pytest.approx(9.0909, abs=0.001)


# ----------------------------------------------------------------- against the source


class TestAgainstTheSource:
    """The figures the original tracking sheet computed, reproduced here.

    Three pots over twelve valuation dates, one of them with a contribution ledger. Only the
    numbers needed to pin the arithmetic are carried across -- the first and last valuation
    of each pot, and the ledger's net position -- because the claim being tested is about
    the formula, not about transcription.
    """

    # First and last valuations, and what had been paid into each by the end.
    FIRST = dt.date(2023, 11, 8)
    LAST = dt.date(2026, 8, 27)

    THREE = pots(("Frozen", FIRST, None), ("Paid into", FIRST, None))

    def history(self):
        return repo.pension_history(
            self.THREE,
            valuations(
                (1, self.FIRST, "69010"), (1, self.LAST, "107858.06"),
                (2, self.FIRST, "0"), (2, self.LAST, "103919"),
            ),
            payments((2, dt.date(2023, 11, 13), "78599.55", "contribution")),
        )

    def test_the_frozen_pot_matches(self):
        """69,010 to 107,858.06 with nothing paid in is 56.2934%."""
        frozen = self.history()
        closing = frozen[frozen["pot"] == "Frozen"].iloc[-1]
        assert closing["total_return"] == pytest.approx(56.2934, abs=0.0001)

    def test_the_pot_that_is_paid_into_matches(self):
        """Opened at nothing, 78,599.55 paid in, worth 103,919: 32.2132%. The sheet reached
        the same figure by dividing the valuation by the ledger's running total, which is
        this same expression with a starting value of zero."""
        history = self.history()
        closing = history[history["pot"] == "Paid into"].iloc[-1]
        assert closing["total_return"] == pytest.approx(32.2132, abs=0.0001)
        assert closing["base"] == Decimal("78599.55")

    def test_the_combined_figure_is_deliberately_not_the_sheets(self):
        """The sheet averaged the pots' returns weighted by their closing values, which
        answers 'what did the average pound do'. The pension's own return is the ratio of
        the sums, and the two differ by most of a percentage point."""
        totals = repo.pension_totals(self.history())
        combined = totals.iloc[-1]
        assert combined["value"] == Decimal("211777.06")
        assert combined["base"] == Decimal("147609.55")
        assert combined["total_return"] == pytest.approx(43.4715, abs=0.001)
