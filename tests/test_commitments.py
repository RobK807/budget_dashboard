"""Account commitments: the standing payments behind a monthly target.

The target says how much an account should hold; these say what leaves it and when. The
interesting cases are all about *when* -- a day of 31 in a short month, and an account that
was not open yet.
"""

import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest

from budget import reference, repo, seed_commitments
from budget.models import Account

APRIL = dt.date(2026, 4, 1)


def commitments(*rows) -> pd.DataFrame:
    """(account, name, amount, day) tuples as the frame load_account_commitments returns."""
    return pd.DataFrame(
        [
            {
                "id": i,
                "account_id": i,
                "account": account,
                "name": name,
                "amount": Decimal(amount),
                "day": day,
            }
            for i, (account, name, amount, day) in enumerate(rows, start=1)
        ],
        columns=["id", "account_id", "account", "name", "amount", "day"],
    )


def accounts(*names) -> pd.DataFrame:
    """Accounts open from April 2026, unless a (name, valid_from) pair says otherwise."""
    rows = []
    for i, entry in enumerate(names, start=1):
        name, opened = entry if isinstance(entry, tuple) else (entry, APRIL)
        rows.append(
            {"id": i, "name": name, "valid_from": opened, "valid_to": None}
        )
    return pd.DataFrame(rows, columns=["id", "name", "valid_from", "valid_to"])


def targets(period, *rows) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"period": period, "account_id": i, "amount": Decimal(a)}
            for i, a in rows
        ],
        columns=["period", "account_id", "amount"],
    )


class TestWritingOne:
    def test_a_commitment_is_stored_and_read_back(self, session):
        with session.begin():
            outcome = reference.set_account_commitment(
                session, 1, "Mortgage", Decimal("2642.85"), 1
            )
        assert outcome.ok
        stored = repo.load_account_commitments(session)
        assert len(stored) == 1
        assert stored.iloc[0]["name"] == "Mortgage"
        assert stored.iloc[0]["amount"] == Decimal("2642.85")
        assert stored.iloc[0]["day"] == 1

    def test_amending_by_id_changes_the_amount_and_the_day(self, session):
        with session.begin():
            reference.set_account_commitment(session, 1, "Gym", Decimal("600"), 1)
            which = int(repo.load_account_commitments(session).iloc[0]["id"])
        with session.begin():
            outcome = reference.set_account_commitment(
                session, 1, "Gym", Decimal("625"), 3, commitment_id=which
            )
        assert outcome.ok
        stored = repo.load_account_commitments(session)
        assert len(stored) == 1, "amending must not add a second row"
        assert stored.iloc[0]["amount"] == Decimal("625")
        assert stored.iloc[0]["day"] == 3

    def test_zero_is_allowed_and_means_the_amount_is_not_known_yet(self, session):
        # A row given with a date and no figure is worth keeping for the date alone.
        with session.begin():
            outcome = reference.set_account_commitment(
                session, 1, "Stocks & Shares Isa", Decimal("0"), 4
            )
        assert outcome.ok
        assert repo.load_account_commitments(session).iloc[0]["amount"] == Decimal("0")

    def test_a_negative_commitment_is_refused(self, session):
        with session.begin():
            outcome = reference.set_account_commitment(
                session, 1, "Backwards", Decimal("-5"), 1
            )
        assert not outcome.ok
        assert repo.load_account_commitments(session).empty

    @pytest.mark.parametrize("day", [0, 32, -1])
    def test_a_day_outside_the_month_is_refused(self, session, day):
        with session.begin():
            outcome = reference.set_account_commitment(
                session, 1, "Whenever", Decimal("5"), day
            )
        assert not outcome.ok

    def test_a_blank_name_is_refused(self, session):
        with session.begin():
            outcome = reference.set_account_commitment(session, 1, "   ", Decimal("5"), 1)
        assert not outcome.ok

    def test_an_unknown_account_is_refused(self, session):
        with session.begin():
            outcome = reference.set_account_commitment(session, 99, "X", Decimal("5"), 1)
        assert not outcome.ok

    def test_the_same_name_twice_on_one_account_is_refused(self, session):
        with session.begin():
            reference.set_account_commitment(session, 1, "Lottery", Decimal("200"), 1)
            outcome = reference.set_account_commitment(
                session, 1, "lottery", Decimal("12.50"), 7
            )
        assert not outcome.ok
        assert len(repo.load_account_commitments(session)) == 1

    def test_the_same_name_on_a_different_account_is_fine(self, session):
        # 'Lottery' really is a commitment on two accounts, and so is a transfer named
        # after the account it goes to.
        with session.begin():
            reference.set_account_commitment(session, 1, "Lottery", Decimal("200"), 1)
            outcome = reference.set_account_commitment(
                session, 2, "Lottery", Decimal("12.50"), 7
            )
        assert outcome.ok
        assert len(repo.load_account_commitments(session)) == 2

    def test_removing_one_leaves_the_rest(self, session):
        with session.begin():
            reference.set_account_commitment(session, 1, "Gym", Decimal("625"), 1)
            reference.set_account_commitment(session, 1, "Water", Decimal("39"), 1)
            which = int(
                repo.load_account_commitments(session).set_index("name").loc["Gym", "id"]
            )
        with session.begin():
            outcome = reference.remove_account_commitment(session, which)
        assert outcome.ok
        assert list(repo.load_account_commitments(session)["name"]) == ["Water"]

    def test_removing_one_that_is_not_there(self, session):
        with session.begin():
            assert not reference.remove_account_commitment(session, 404).ok


class TestSavingTheWholeGrid:
    """`replace_account_commitments` -- what the Settings grid hands back on Save."""

    def row(self, **kwargs):
        base = {"id": None, "account_id": 1, "name": "Gym", "amount": Decimal("625"),
                "day": 1}
        return {**base, **kwargs}

    def stored(self, session):
        return repo.load_account_commitments(session)

    def test_new_rows_are_added(self, session):
        with session.begin():
            outcome = reference.replace_account_commitments(
                session,
                [self.row(name="Gym"), self.row(name="Water", amount=Decimal("39"))],
            )
            assert outcome.ok
        assert sorted(self.stored(session)["name"]) == ["Gym", "Water"]

    def test_a_row_left_out_is_removed(self, session):
        with session.begin():
            reference.replace_account_commitments(
                session, [self.row(name="Gym"), self.row(name="Water")]
            )
            keep = self.stored(session)
            gym = keep[keep["name"] == "Gym"].iloc[0]
            outcome = reference.replace_account_commitments(
                session, [self.row(id=int(gym["id"]), name="Gym")]
            )
            assert outcome.ok
        assert list(self.stored(session)["name"]) == ["Gym"]
        assert "1 removed" in outcome.message

    def test_an_amount_is_amended_in_place(self, session):
        with session.begin():
            reference.replace_account_commitments(session, [self.row(name="Gym")])
            gym = self.stored(session).iloc[0]
            reference.replace_account_commitments(
                session,
                [self.row(id=int(gym["id"]), name="Gym", amount=Decimal("650"), day=3)],
            )
        after = self.stored(session)
        assert len(after) == 1
        assert after.iloc[0]["amount"] == Decimal("650")
        assert after.iloc[0]["day"] == 3

    def test_adding_and_removing_in_the_same_save(self, session):
        with session.begin():
            reference.replace_account_commitments(session, [self.row(name="Gone")])
            outcome = reference.replace_account_commitments(
                session, [self.row(name="Arrived")]
            )
            assert outcome.ok
        assert list(self.stored(session)["name"]) == ["Arrived"]

    def test_a_name_freed_by_a_deletion_can_be_reused_in_the_same_save(self, session):
        # Deletions have to happen first, or the new row collides with the one on its way
        # out and the save fails for a clash that does not survive it.
        with session.begin():
            reference.replace_account_commitments(session, [self.row(name="Gym")])
            outcome = reference.replace_account_commitments(
                session, [self.row(name="Gym", amount=Decimal("700"))]
            )
            assert outcome.ok, outcome.message
        after = self.stored(session)
        assert len(after) == 1
        assert after.iloc[0]["amount"] == Decimal("700")

    def test_one_bad_row_saves_none_of_them(self, session):
        with session.begin():
            reference.replace_account_commitments(session, [self.row(name="Keep me")])
        with session.begin():
            outcome = reference.replace_account_commitments(
                session,
                [self.row(name="Fine"), self.row(name="Broken", day=99)],
            )
        assert not outcome.ok
        # Neither the good new row nor the deletion of the original went through.
        assert list(self.stored(session)["name"]) == ["Keep me"]

    def test_the_same_name_twice_in_one_save_is_refused(self, session):
        with session.begin():
            outcome = reference.replace_account_commitments(
                session, [self.row(name="Gym"), self.row(name="gym")]
            )
        assert not outcome.ok
        assert "twice" in outcome.message
        assert self.stored(session).empty

    def test_moving_a_row_onto_a_name_already_there_is_refused(self, session):
        with session.begin():
            reference.replace_account_commitments(
                session,
                [self.row(name="Lottery"), self.row(name="Lottery", account_id=2)],
            )
            both = self.stored(session)
        ids = dict(zip(both["account_id"], both["id"]))
        with session.begin():
            # Drag the account-2 row onto account 1, where 'Lottery' already sits.
            outcome = reference.replace_account_commitments(
                session,
                [
                    self.row(id=int(ids[1]), name="Lottery"),
                    self.row(id=int(ids[2]), name="Lottery"),
                ],
            )
        assert not outcome.ok
        assert len(self.stored(session)) == 2

    def test_an_empty_grid_clears_the_list(self, session):
        with session.begin():
            reference.replace_account_commitments(session, [self.row(name="Gym")])
            outcome = reference.replace_account_commitments(session, [])
            assert outcome.ok
        assert self.stored(session).empty

    @pytest.mark.parametrize(
        "bad, expected",
        [
            ({"day": 0}, "day"),
            ({"day": 32}, "day"),
            ({"day": None}, "day"),
            ({"amount": Decimal("-1")}, "negative"),
            ({"name": "  "}, "name"),
            ({"account_id": 404}, "unknown account"),
        ],
    )
    def test_the_grid_rejects_what_a_single_write_would(self, session, bad, expected):
        with session.begin():
            outcome = reference.replace_account_commitments(
                session, [self.row(**bad)]
            )
        assert not outcome.ok
        assert expected in outcome.message.lower()
        assert self.stored(session).empty


class TestSettingTheCycleDay:
    def test_it_is_stored_on_the_account(self, session):
        with session.begin():
            outcome = reference.update_account(session, 1, commitment_start_day=19)
            assert outcome.ok
            accounts = repo.load_reference(session)["accounts"].set_index("id")
        assert accounts.loc[1, "commitment_start_day"] == 19

    @pytest.mark.parametrize("day", [0, 32, -1])
    def test_a_day_outside_the_month_is_refused(self, session, day):
        with session.begin():
            outcome = reference.update_account(session, 1, commitment_start_day=day)
        assert not outcome.ok

    def test_clearing_it_is_allowed_and_means_the_first(self, session):
        with session.begin():
            reference.update_account(session, 1, commitment_start_day=19)
            outcome = reference.update_account(session, 1, commitment_start_day=None)
            assert outcome.ok
            accounts = repo.load_reference(session)["accounts"].set_index("id")
        assert repo.commitment_start_day(accounts.loc[1]) == 1


class TestLoading:
    def test_nothing_stored_still_has_the_columns(self, session):
        stored = repo.load_account_commitments(session)
        assert stored.empty
        assert list(stored.columns) == [
            "id", "account_id", "account", "name", "amount", "day"
        ]

    def test_ordered_by_account_then_by_day(self, session):
        with session.begin():
            reference.set_account_commitment(session, 1, "Late", Decimal("1"), 19)
            reference.set_account_commitment(session, 1, "Early", Decimal("1"), 1)
            reference.set_account_commitment(session, 2, "Middle", Decimal("1"), 7)
        stored = repo.load_account_commitments(session)
        assert list(stored["name"]) == ["Early", "Late", "Middle"]


class TestTheMonthlyTable:
    LIST = commitments(
        ("HSBC", "Council tax", "215", 1),
        ("HSBC", "Base", "500", 19),
        ("HSBC", "Mortgage", "2642.85", 31),
        ("First Direct", "Lottery", "12.50", 7),
    )
    # Open well before the months tested, so the liveness filter is not what is under test
    # here -- it has cases of its own below.
    OPEN = accounts(("HSBC", dt.date(2025, 1, 1)), ("First Direct", dt.date(2025, 1, 1)))

    def table(self, period="2026-07", accounts=None):
        return repo.account_commitment_table(
            self.LIST, period, self.OPEN if accounts is None else accounts
        )

    def test_nothing_listed_still_has_the_columns(self):
        empty = repo.account_commitment_table(commitments(), "2026-07", self.OPEN)
        assert empty.empty
        assert list(empty.columns) == [
            "account", "name", "day", "due", "amount", "still_needed"
        ]

    def test_it_counts_down_what_is_left_after_each_payment(self):
        # HSBC: 215 on the 1st, 500 on the 19th, 2,642.85 on the 31st.
        by_name = self.table().set_index("name")["still_needed"]
        assert by_name["Council tax"] == Decimal("3142.85")  # the 500 and the 2,642.85
        assert by_name["Base"] == Decimal("2642.85")         # just the mortgage
        assert by_name["Mortgage"] == Decimal("0")           # nothing after the last

    def test_the_countdown_restarts_on_each_account(self):
        # The accounts are funded separately, so a figure running across all of them would
        # describe nothing anybody has to do.
        assert self.table().set_index("name")["still_needed"]["Lottery"] == Decimal("0")

    def test_the_first_row_is_everything_except_itself(self):
        hsbc = self.table()
        hsbc = hsbc[hsbc["account"] == "HSBC"]
        first = hsbc.iloc[0]
        assert first["still_needed"] == hsbc["amount"].sum() - first["amount"]

    def test_the_last_row_of_every_account_reaches_zero(self):
        table = self.table()
        for account in table["account"].unique():
            mine = table[table["account"] == account]
            assert mine.iloc[-1]["still_needed"] == Decimal("0"), account

    def test_a_day_that_does_not_exist_falls_back_to_the_month_end(self):
        february = self.table("2026-02").set_index("name")["due"]
        assert february["Mortgage"] == dt.date(2026, 2, 28)
        assert february["Council tax"] == dt.date(2026, 2, 1)

    def test_the_thirty_first_is_itself_in_a_long_month(self):
        assert self.table("2026-07").set_index("name")["due"]["Mortgage"] == dt.date(
            2026, 7, 31
        )

    def test_an_account_not_open_yet_is_left_out(self):
        # Same rule as the savings targets: a payment cannot fall due on an account that
        # does not exist.
        listed = commitments(("Tembo", "Something", "99", 5))
        later = accounts(("Tembo", dt.date(2026, 6, 1)))
        assert repo.account_commitment_table(listed, "2026-05", later).empty
        assert len(repo.account_commitment_table(listed, "2026-06", later)) == 1

    def test_a_closed_account_is_left_out(self):
        closed = pd.DataFrame(
            [{"id": 1, "name": "Old", "valid_from": APRIL,
              "valid_to": dt.date(2026, 5, 31)}]
        )
        listed = commitments(("Old", "Something", "10", 5))
        assert len(repo.account_commitment_table(listed, "2026-05", closed)) == 1
        assert repo.account_commitment_table(listed, "2026-06", closed).empty

    def test_no_account_list_means_no_filtering(self):
        assert len(repo.account_commitment_table(self.LIST, "2026-07")) == 4


class TestTheFundingCycle:
    """An account funded on the 19th covers the 19th to the 18th, not a calendar month."""

    def test_the_default_is_the_first_of_the_month(self):
        assert repo.commitment_start_day(None) == 1
        assert repo.commitment_start_day(pd.Series({"name": "X"})) == 1
        assert repo.commitment_start_day(
            pd.Series({"commitment_start_day": None})
        ) == 1

    def test_a_day_that_is_set_is_used(self):
        assert repo.commitment_start_day(
            pd.Series({"commitment_start_day": 19})
        ) == 19

    def test_a_cycle_from_the_first_is_the_calendar_month(self):
        assert repo.commitment_cycle("2026-09", 1) == (
            dt.date(2026, 9, 1), dt.date(2026, 9, 30)
        )

    def test_a_cycle_from_the_nineteenth_ends_the_day_before_the_next(self):
        assert repo.commitment_cycle("2026-09", 19) == (
            dt.date(2026, 9, 19), dt.date(2026, 10, 18)
        )

    def test_a_cycle_rolls_over_the_year_end(self):
        assert repo.commitment_cycle("2026-12", 19) == (
            dt.date(2026, 12, 19), dt.date(2027, 1, 18)
        )

    def test_a_payment_before_the_start_day_falls_in_the_next_month(self):
        # Paid on the 19th, the 4th that follows is next month's 4th.
        assert repo.commitment_due_date("2026-09", 19, 4) == dt.date(2026, 10, 4)
        assert repo.commitment_due_date("2026-09", 19, 20) == dt.date(2026, 9, 20)
        assert repo.commitment_due_date("2026-09", 19, 19) == dt.date(2026, 9, 19)

    def test_a_payment_rolling_into_a_new_year(self):
        assert repo.commitment_due_date("2026-12", 19, 4) == dt.date(2027, 1, 4)

    def test_a_short_month_still_clamps(self):
        assert repo.commitment_due_date("2026-01", 19, 31) == dt.date(2026, 1, 31)
        assert repo.commitment_due_date("2026-02", 1, 31) == dt.date(2026, 2, 28)

    def test_a_cycle_starting_on_the_thirty_first_compares_on_the_raw_day(self):
        """The clamp must not pull days back into a cycle they do not belong to.

        A cycle starting on the 31st opens on the 28th in February. Comparing the 29th
        against that clamped date would say 29 >= 28 and keep it in February, when the
        account is not funded until the 28th *is* the 31st.
        """
        assert repo.commitment_due_date("2026-02", 31, 29) == dt.date(2026, 3, 29)
        assert repo.commitment_due_date("2026-02", 31, 31) == dt.date(2026, 2, 28)

    def test_the_table_orders_by_the_real_date_not_the_day_number(self):
        listed = commitments(
            ("HSBC", "Barclaycard", "120", 6),
            ("HSBC", "Base", "500", 19),
            ("HSBC", "Wedding", "350", 20),
        )
        funded = pd.DataFrame(
            [{"id": 1, "name": "HSBC", "valid_from": dt.date(2025, 1, 1),
              "valid_to": None, "commitment_start_day": 19}]
        )
        table = repo.account_commitment_table(listed, "2026-09", funded)
        # The 6th is next month's, so it comes last despite the lowest day number.
        assert list(table["name"]) == ["Base", "Wedding", "Barclaycard"]
        assert list(table["due"]) == [
            dt.date(2026, 9, 19), dt.date(2026, 9, 20), dt.date(2026, 10, 6)
        ]
        # And the countdown follows that order, not the day numbers.
        assert list(table["still_needed"]) == [
            Decimal("470"), Decimal("120"), Decimal("0")
        ]

    def test_an_account_with_no_start_day_keeps_the_calendar_month(self):
        listed = commitments(("HSBC", "Barclaycard", "120", 6))
        plain = pd.DataFrame(
            [{"id": 1, "name": "HSBC", "valid_from": dt.date(2025, 1, 1),
              "valid_to": None, "commitment_start_day": None}]
        )
        table = repo.account_commitment_table(listed, "2026-09", plain)
        assert table.iloc[0]["due"] == dt.date(2026, 9, 6)


class TestAgainstTheTarget:
    LIST = commitments(("HSBC", "Rent", "600", 1), ("HSBC", "Gym", "25", 3))
    OPEN = accounts("HSBC", "First Direct")

    def test_the_difference_is_the_target_less_what_is_due(self):
        against = repo.commitments_against_targets(
            self.LIST, targets("2026-07", (1, "700")), self.OPEN, "2026-07"
        )
        row = against.set_index("account").loc["HSBC"]
        assert row["itemised"] == Decimal("625")
        assert row["target"] == Decimal("700")
        assert row["difference"] == Decimal("75")
        assert row["items"] == 2

    def test_a_target_under_its_commitments_reads_negative(self):
        against = repo.commitments_against_targets(
            self.LIST, targets("2026-07", (1, "500")), self.OPEN, "2026-07"
        )
        assert against.set_index("account").loc["HSBC", "difference"] == Decimal("-125")

    def test_an_account_with_no_target_shows_no_difference(self):
        against = repo.commitments_against_targets(
            self.LIST, targets("2026-07"), self.OPEN, "2026-07"
        )
        row = against.set_index("account").loc["HSBC"]
        assert row["target"] is None
        assert row["difference"] is None
        assert row["itemised"] == Decimal("625")

    def test_a_target_with_no_commitments_still_appears(self):
        # Otherwise a target nobody has itemised yet simply vanishes from the comparison.
        against = repo.commitments_against_targets(
            self.LIST, targets("2026-07", (2, "300")), self.OPEN, "2026-07"
        )
        row = against.set_index("account").loc["First Direct"]
        assert row["items"] == 0
        assert row["itemised"] == Decimal("0")
        assert row["difference"] == Decimal("300")

    def test_the_shortfall_filter_survives_a_mix_of_set_and_unset_targets(self):
        """The Summary page selects the overdrawn rows with

            against[against["difference"].notna() & (against["difference"] < 0)]

        and `&` does not short-circuit, so the comparison runs over the unset targets too.
        A column of Decimals and Nones is the normal case -- one account itemised and
        another not -- and this pins that it filters rather than raising.
        """
        listed = commitments(
            ("HSBC", "Rent", "600", 1), ("First Direct", "Gym", "25", 3)
        )
        against = repo.commitments_against_targets(
            listed, targets("2026-07", (1, "500")), self.OPEN, "2026-07"
        )
        assert against["difference"].isna().any(), "no unset target in this fixture"
        under = against[against["difference"].notna() & (against["difference"] < 0)]
        assert list(under["account"]) == ["HSBC"]

    def test_neither_stored_means_an_empty_frame(self):
        against = repo.commitments_against_targets(
            commitments(), targets("2026-07"), self.OPEN, "2026-07"
        )
        assert against.empty
        assert list(against.columns) == [
            "account", "items", "itemised", "target", "difference"
        ]


class TestTheSeedScript:
    """The list of standing payments the script carries, and that re-running is safe."""

    def prepare(self, session):
        with session.begin():
            for name in ("First Direct", "Halifax", "Nationwide"):
                session.add(
                    Account(name=name, short_code=name[:4].upper(), type="bank",
                            valid_from=APRIL)
                )

    def test_every_row_names_an_account_the_script_can_find(self, session):
        self.prepare(session)
        with session.begin():
            result = seed_commitments.apply(session, write=False, update=False)
        assert result["missing_accounts"] == []

    def test_the_amounts_are_all_positive_or_a_deliberate_zero(self):
        zero = [
            (a, n) for a, n, amount, _ in seed_commitments.COMMITMENTS
            if Decimal(amount) == 0
        ]
        # Exactly one row was given without an amount. A second would mean a typo.
        assert zero == [("HSBC", "Stocks & Shares Isa")]
        assert all(
            Decimal(amount) >= 0 for _, _, amount, _ in seed_commitments.COMMITMENTS
        )

    def test_every_day_is_a_day_of_the_month(self):
        assert all(1 <= day <= 31 for *_, day in seed_commitments.COMMITMENTS)

    def test_no_account_lists_the_same_item_twice(self):
        pairs = [(a, n.casefold()) for a, n, _, _ in seed_commitments.COMMITMENTS]
        assert len(pairs) == len(set(pairs))

    def test_applying_it_stores_every_row(self, session):
        self.prepare(session)
        with session.begin():
            result = seed_commitments.apply(session, write=True, update=False)
        assert len(result["added"]) == len(seed_commitments.COMMITMENTS)
        assert len(repo.load_account_commitments(session)) == len(
            seed_commitments.COMMITMENTS
        )

    def test_running_it_twice_writes_nothing_the_second_time(self, session):
        self.prepare(session)
        with session.begin():
            seed_commitments.apply(session, write=True, update=False)
        with session.begin():
            again = seed_commitments.apply(session, write=True, update=False)
        assert again["added"] == []
        assert again["differs"] == []
        assert again["present"] == len(seed_commitments.COMMITMENTS)

    def test_a_figure_edited_in_the_dashboard_is_reported_not_overwritten(self, session):
        # The Settings grid is where these are maintained. A seed script that quietly put
        # a hand-edited figure back would undo real work.
        self.prepare(session)
        with session.begin():
            seed_commitments.apply(session, write=True, update=False)
            stored = repo.load_account_commitments(session)
            gym = stored[stored["name"] == "Gym"].iloc[0]
        with session.begin():
            reference.set_account_commitment(
                session, int(gym["account_id"]), "Gym", Decimal("650"), 1,
                commitment_id=int(gym["id"]),
            )
        with session.begin():
            result = seed_commitments.apply(session, write=True, update=False)
        assert len(result["differs"]) == 1
        assert "Gym" in result["differs"][0]
        after = repo.load_account_commitments(session)
        assert after[after["name"] == "Gym"].iloc[0]["amount"] == Decimal("650")

    def test_update_brings_a_differing_row_back_into_line(self, session):
        self.prepare(session)
        with session.begin():
            seed_commitments.apply(session, write=True, update=False)
            stored = repo.load_account_commitments(session)
            gym = stored[stored["name"] == "Gym"].iloc[0]
        with session.begin():
            reference.set_account_commitment(
                session, int(gym["account_id"]), "Gym", Decimal("650"), 1,
                commitment_id=int(gym["id"]),
            )
        with session.begin():
            result = seed_commitments.apply(session, write=True, update=True)
        assert len(result["updated"]) == 1
        after = repo.load_account_commitments(session)
        assert after[after["name"] == "Gym"].iloc[0]["amount"] == Decimal("625")

    def test_report_mode_writes_nothing(self, session):
        self.prepare(session)
        with session.begin():
            seed_commitments.apply(session, write=False, update=False)
        assert repo.load_account_commitments(session).empty

    def test_it_sets_the_cycle_start_days(self, session):
        self.prepare(session)
        with session.begin():
            result = seed_commitments.apply(session, write=True, update=False)
            assert len(result["cycles"]) == len(seed_commitments.START_DAYS)
            accounts = repo.load_reference(session)["accounts"].set_index("name")
        assert accounts.loc["First Direct", "commitment_start_day"] == 1
        assert accounts.loc["HSBC", "commitment_start_day"] == 19

    def test_a_cycle_day_set_by_hand_is_reported_not_overwritten(self, session):
        self.prepare(session)
        with session.begin():
            accounts = repo.load_reference(session)["accounts"].set_index("name")
            reference.update_account(
                session, int(accounts.loc["HSBC", "id"]), commitment_start_day=25
            )
            result = seed_commitments.apply(session, write=True, update=False)
        assert any("HSBC" in line for line in result["differs"])
        after = repo.load_reference(session)["accounts"].set_index("name")
        assert after.loc["HSBC", "commitment_start_day"] == 25

    def test_every_account_in_the_list_has_a_cycle_day(self):
        named = {account for account, *_ in seed_commitments.COMMITMENTS}
        assert named == set(seed_commitments.START_DAYS)

    def test_the_totals_match_what_was_asked_for(self, session):
        """Guards the table itself against a mistyped figure."""
        by_account: dict[str, Decimal] = {}
        for account, _, amount, _ in seed_commitments.COMMITMENTS:
            by_account[account] = by_account.get(account, Decimal("0")) + Decimal(amount)
        assert by_account == {
            "First Direct": Decimal("3880.45"),
            "Halifax": Decimal("32.50"),
            # 2600 plus the Stocks & Shares Isa, which has no amount yet.
            "HSBC": Decimal("2600.00"),
            "Nationwide": Decimal("94.00"),
        }
