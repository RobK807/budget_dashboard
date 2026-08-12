"""Reading a bank's own export, and deciding what the rows mean.

The samples below are fabricated -- invented merchants, round-ish amounts -- but every
structural quirk in them is real and was taken from an actual export: the preamble blocks,
the small print *after* Virgin Money's data, Coventry's reversed column pair, Amex counting a
purchase as positive while everyone else counts it as negative, and three different ways of
writing a date.

They are strings rather than files on purpose. A .csv in the repository is one careless
`git add -A` away from being a real statement, and these are small enough to read beside the
assertion that depends on them.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest

from budget import bank_formats as bf
from budget import bank_import as bi

# --------------------------------------------------------------------------------- samples

AMEX = """Date,Description,Amount
10/08/2026,COFFEE HUT              LONDON,4.50
09/08/2026,BOOKSHOP                LONDON,20.00
27/07/2026,PAYMENT RECEIVED - THANK YOU,-500.00
"""

AMEX_WIDE = """Date,Description,Card Member,Account #,Amount
10/08/2026,COFFEE HUT              LONDON,A CARDHOLDER,-41001,4.50
27/07/2026,PAYMENT RECEIVED - THANK YOU,A CARDHOLDER,-41001,-500.00
"""

# No header row at all, and thousands quoted.
HSBC = """12/08/2026,CASH MACHINE  AUG12,-20.00
11/08/2026,SALARY CR,"2,000.00"
07/08/2026,CARD PAYMENT DD,"-1,158.62"
"""

FIRST_DIRECT = """Date,Description,Amount,Balance
03/08/2026,WATER COMPANY,-39.00,531.38
31/07/2026,TRANSFER IN,3581.00,3900.00
"""

HALIFAX = """Transaction Date,Transaction Type,Sort Code,Account Number,\
Transaction Description,Debit Amount,Credit Amount,Balance,
07/08/2026,DD,'00-00-00,12345678,LOTTERY,12.5,,82.68
20/07/2026,FPO,'00-00-00,12345678,TO CURRENT,1453.30,,132.5
19/07/2026,BGC,'00-00-00,12345678,FROM CURRENT,,1500.00,1585.80
"""

# Three lines of account detail, a blank, then the data. Dates as '15 Jul 2026', figures
# with pound signs, and a byte-order mark at the very front.
NATIONWIDE = """﻿"Account Name:","FlexDirect Account ****0000"
"Account Balance:","£150.20"
"Available Balance: ","£150.20"

"Date","Transaction type","Description","Paid out","Paid in","Balance"
"15 Jul 2026","Direct debit","EYEPLAN","£55.00","","£95.23"
"17 Jul 2026","Bank credit","A PERSON","","£1500.00","£1595.23"
"""

MARCUS = '''"TransactionDate","Description","Value","AccountBalance","AccountName","AccountNumber"
"20260805","Withdrawal to 20000002","-2736.16","1040.76"," Main Savings","10000001"
"20260731","Transfer from 30000003","322.92","3776.92"," Main Savings","10000001"
'''

# Preamble, data, then an address and six paragraphs of small print. Reading past the blank
# line turns the address into a transaction.
VIRGIN = """Account Number,40000004
Sort code,00-00-00
Account Balance,2413.25

Date,Details,Money out,Money in,Balance
10/08/2026,INTEREST EARNED                 ,,8.02,2413.25
03/08/2026,Payment from - A PERSON         ,50.00,,2405.23

A PERSON
"1 SOME STREET, LONDON, SE10 0GS"
Interest rate not what you expected?
"""

# The same two headings as Virgin Money, in the opposite order.
COVENTRY = """Exported on Wednesday, 12 August 2026
Transactions for XXXXX0000 - Access Saver, 28/02/2026 to 12/08/2026
Date, Description, Money in, Money out, Balance
28/02/2026, Interest, 1.64, 0.00, 796.31
02/03/2026, A PERSON, 250.00, 0.00, 1044.67
31/03/2026, PAID AWAY, 0.00, 701.68, 342.99
"""

SAMPLES = {
    "amex": AMEX,
    "amex_wide": AMEX_WIDE,
    "hsbc": HSBC,
    "first_direct": FIRST_DIRECT,
    "halifax": HALIFAX,
    "nationwide": NATIONWIDE,
    "marcus": MARCUS,
    "virgin": VIRGIN,
    "coventry": COVENTRY,
}


def parsed(sample: str) -> pd.DataFrame:
    fmt, _ = bf.detect(sample)
    assert fmt is not None, "sample was not recognised"
    return bf.read(sample, fmt)


def totals(frame: pd.DataFrame) -> tuple[Decimal, Decimal]:
    out = frame[frame["direction"] == bf.OUT]["amount"].sum()
    into = frame[frame["direction"] == bf.IN]["amount"].sum()
    return Decimal(str(out)), Decimal(str(into))


# ------------------------------------------------------------------------------- detection


class TestDetection:
    @pytest.mark.parametrize(
        "sample,expected",
        [
            (AMEX, "amex"), (AMEX_WIDE, "amex"), (HSBC, "hsbc"),
            (FIRST_DIRECT, "first_direct"), (HALIFAX, "halifax"),
            (NATIONWIDE, "nationwide"), (MARCUS, "marcus"),
            (VIRGIN, "virgin"), (COVENTRY, "coventry"),
        ],
    )
    def test_each_sample_is_recognised(self, sample, expected):
        fmt, _ = bf.detect(sample)
        assert fmt is not None and fmt.key == expected

    def test_amex_is_not_mistaken_for_first_direct(self):
        """Amex's three headings are a subset of First Direct's four, so a plain 'all headings
        present' test matches both and the shorter one must not win."""
        fmt, also = bf.detect(FIRST_DIRECT)
        assert fmt.key == "first_direct"
        assert [f.key for f in also] == ["amex"]

    def test_a_headerless_file_falls_back_rather_than_failing(self):
        fmt, _ = bf.detect(HSBC)
        assert fmt.key == "hsbc"

    def test_something_that_is_not_a_statement_is_not_guessed_at(self):
        assert bf.detect("hello\nthere\n")[0] is None


# ------------------------------------------------------------------------------ the readers


class TestReading:
    def test_amex_counts_a_purchase_as_money_out(self):
        """The one format where a purchase is positive: the statement counts what is owed.

        Reading it the other way round inverts every card transaction, and the totals still
        look plausible, which is why it is stated per format rather than inferred.
        """
        out, into = totals(parsed(AMEX))
        assert out == Decimal("24.50")  # the two purchases
        assert into == Decimal("500.00")  # the payment

    def test_the_hsbc_family_counts_a_purchase_as_money_in_the_other_direction(self):
        out, into = totals(parsed(HSBC))
        assert out == Decimal("1178.62")
        assert into == Decimal("2000.00")

    def test_the_two_amex_layouts_read_the_same(self):
        """Columns are found by name, so the extra card-member and account columns cost
        nothing."""
        narrow = parsed(AMEX)
        wide = parsed(AMEX_WIDE)
        assert list(wide["amount"]) == [Decimal("4.50"), Decimal("500.00")]
        assert list(narrow["direction"])[0] == list(wide["direction"])[0]

    def test_halifax_splits_debit_from_credit(self):
        out, into = totals(parsed(HALIFAX))
        assert out == Decimal("1465.80")
        assert into == Decimal("1500.00")

    def test_nationwide_skips_its_preamble_and_strips_the_pound_signs(self):
        frame = parsed(NATIONWIDE)
        assert len(frame) == 2
        assert list(frame["date"]) == [dt.date(2026, 7, 15), dt.date(2026, 7, 17)]
        assert totals(frame) == (Decimal("55.00"), Decimal("1500.00"))

    def test_marcus_reads_a_compact_date(self):
        frame = parsed(MARCUS)
        assert list(frame["date"]) == [dt.date(2026, 8, 5), dt.date(2026, 7, 31)]

    def test_virgin_stops_at_the_blank_line_before_the_small_print(self):
        """The address and the footnotes sit after the data. Reading on turns them into
        transactions, and an address parses far enough to be kept."""
        frame = parsed(VIRGIN)
        assert len(frame) == 2
        assert not frame["description"].str.contains("SOME STREET").any()

    def test_coventry_is_not_read_with_virgins_column_order(self):
        """Same two headings, reversed. Read by position rather than by name and every row in
        the file lands on the wrong side -- which reconciles to the same total, so nothing
        downstream notices."""
        frame = parsed(COVENTRY)
        out, into = totals(frame)
        assert into == Decimal("251.64")  # interest and the payment in
        assert out == Decimal("701.68")  # the one payment away

    def test_a_zero_in_the_other_column_is_not_a_transaction(self):
        """Coventry writes 0.00 rather than leaving the unused side empty."""
        assert len(parsed(COVENTRY)) == 3

    def test_the_wrong_format_says_so_rather_than_returning_nonsense(self):
        with pytest.raises(bf.UnreadableFile):
            bf.read(HALIFAX, bf.BY_KEY["nationwide"])

    def test_an_identifier_is_lifted_where_the_file_carries_one(self):
        assert "0000" in bf.identify(NATIONWIDE, bf.BY_KEY["nationwide"])
        assert "Wedding" not in bf.identify(MARCUS, bf.BY_KEY["marcus"])
        assert "10000001" in bf.identify(MARCUS, bf.BY_KEY["marcus"])


class TestDecoding:
    def test_a_pound_sign_survives_either_encoding(self):
        """A lone 0xA3 is not valid UTF-8, so a cp1252 export fails the first attempt and has
        to fall through rather than being mangled into replacement characters."""
        assert "£55.00" in bf.decode(NATIONWIDE.encode("utf-8"))
        # cp1252 has no byte-order mark, so the sample's is dropped before encoding.
        plain = NATIONWIDE.replace("﻿", "")
        assert "£55.00" in bf.decode(plain.encode("cp1252"))

    def test_a_byte_order_mark_does_not_become_part_of_the_first_heading(self):
        frame = bf.read(bf.decode(NATIONWIDE.encode("utf-8-sig")), bf.BY_KEY["nationwide"])
        assert len(frame) == 2


# --------------------------------------------------------------- what is already recorded


def ledger(*rows) -> pd.DataFrame:
    """Postings as repo.load_postings returns them: one row per account movement."""
    return pd.DataFrame(
        [
            {"account": a, "column": c, "amount": Decimal(str(m)), "date": d}
            for a, c, m, d in rows
        ]
    )


def source(account, date, amount, direction, description="thing", row=2, file="f.csv"):
    return bi.SourceRow(
        account=account, date=date, description=description,
        amount=Decimal(str(amount)), direction=direction, source=file, row=row,
    )


DAY = dt.date(2026, 8, 10)


class TestExcludingWhatIsRecorded:
    def test_a_match_is_dropped(self):
        rows = [source("HSBC", DAY, 20, bf.OUT)]
        result = bi.prepare(rows, ledger(("HSBC", "debit", 20, DAY)), skip_older=False)
        assert result.count == 0
        assert bi.ALREADY_RECORDED in result.excluded.iloc[0]["Why"]

    def test_the_direction_has_to_agree(self):
        """£20 in and £20 out on the same day are different transactions."""
        rows = [source("HSBC", DAY, 20, bf.IN)]
        result = bi.prepare(rows, ledger(("HSBC", "debit", 20, DAY)), skip_older=False)
        assert result.count == 1

    def test_the_account_has_to_agree(self):
        rows = [source("HSBC", DAY, 20, bf.OUT)]
        result = bi.prepare(rows, ledger(("Halifax", "debit", 20, DAY)), skip_older=False)
        assert result.count == 1

    def test_matching_consumes(self):
        """Two identical purchases on one day need two ledger entries to be dropped. Testing
        for mere presence would silently discard the second one every time."""
        rows = [
            source("Mastercard", DAY, 261.99, bf.OUT),
            source("Mastercard", DAY, 261.99, bf.OUT, row=3),
        ]
        result = bi.prepare(
            rows, ledger(("Mastercard", "debit", 261.99, DAY)), skip_older=False
        )
        assert result.count == 1

    def test_a_nearby_date_still_counts(self):
        """A card posts on a different day from the one written down."""
        rows = [source("Mastercard", DAY, 261.99, bf.OUT)]
        recorded = ledger(("Mastercard", "debit", 261.99, DAY - dt.timedelta(days=1)))
        result = bi.prepare(rows, recorded, skip_older=False)
        assert result.count == 0
        assert "dated" in result.excluded.iloc[0]["Why"]

    def test_a_distant_date_does_not(self):
        rows = [source("Mastercard", DAY, 261.99, bf.OUT)]
        recorded = ledger(("Mastercard", "debit", 261.99, DAY - dt.timedelta(days=30)))
        assert bi.prepare(rows, recorded, skip_older=False).count == 1

    def test_a_stored_transfer_covers_both_of_its_accounts(self):
        """A transfer is one transaction and two movements. Indexing it by its 'from' account
        alone lets the other bank's file bring the same money in a second time."""
        recorded = ledger(
            ("HSBC", "debit", 3581, DAY), ("First Direct", "credit", 3581, DAY)
        )
        rows = [
            source("HSBC", DAY, 3581, bf.OUT, "TO FIRST DIRECT"),
            source("First Direct", DAY, 3581, bf.IN, "FROM HSBC", file="fd.csv"),
        ]
        assert bi.prepare(rows, recorded, skip_older=False).count == 0

    def test_a_penny_out_is_a_different_transaction(self):
        rows = [source("HSBC", DAY, 20.01, bf.OUT)]
        assert bi.prepare(
            rows, ledger(("HSBC", "debit", 20, DAY)), skip_older=False
        ).count == 1


class TestSkippingOlderRows:
    def test_anything_before_the_last_recorded_movement_is_held_back(self):
        """Banks export further back than the ledger goes for a quiet account. Without this
        an ordinary import backfills months on the strength of an upload."""
        old = dt.date(2026, 2, 20)
        recorded = ledger(("Halifax", "debit", 99, dt.date(2026, 4, 20)))
        rows = [source("Halifax", old, 110.58, bf.OUT)]
        result = bi.prepare(rows, recorded)
        assert result.count == 0
        assert bi.PREDATES in result.excluded.iloc[0]["Why"]

    def test_it_is_per_account(self):
        """Being up to date on HSBC says nothing about Halifax."""
        recorded = ledger(("HSBC", "debit", 99, dt.date(2026, 8, 1)))
        rows = [source("Halifax", dt.date(2026, 3, 1), 10, bf.OUT)]
        assert bi.prepare(rows, recorded).count == 1

    def test_it_can_be_turned_off(self):
        recorded = ledger(("Halifax", "debit", 99, dt.date(2026, 4, 20)))
        rows = [source("Halifax", dt.date(2026, 2, 20), 10, bf.OUT)]
        assert bi.prepare(rows, recorded, skip_older=False).count == 1

    def test_being_already_recorded_is_the_more_useful_thing_to_be_told(self):
        """A row that is both old and already recorded should say so, not merely 'old'."""
        recorded = ledger(("Halifax", "debit", 10, dt.date(2026, 2, 20)),
                          ("Halifax", "debit", 99, dt.date(2026, 4, 20)))
        rows = [source("Halifax", dt.date(2026, 2, 20), 10, bf.OUT)]
        result = bi.prepare(rows, recorded)
        assert bi.ALREADY_RECORDED in result.excluded.iloc[0]["Why"]


# ---------------------------------------------------------------------------- transfers


class TestPairing:
    def test_two_halves_become_one_transfer(self):
        rows = [
            source("HSBC", DAY, 500, bf.OUT, "AMERICAN EXPRESS DD"),
            source("BA Amex", DAY, 500, bf.IN, "PAYMENT RECEIVED", file="amex.csv"),
        ]
        result = bi.prepare(rows, ledger(), skip_older=False)
        assert result.count == 1
        assert result.paired == 1
        row = result.rows.iloc[0]
        assert row["Type"] == "Transfer"
        assert row["Account From"] == "HSBC"
        assert row["Account To"] == "BA Amex"

    def test_both_descriptions_are_kept(self):
        """Neither bank's wording alone says what the movement was."""
        rows = [
            source("HSBC", DAY, 500, bf.OUT, "AMERICAN EXPRESS DD"),
            source("BA Amex", DAY, 500, bf.IN, "PAYMENT RECEIVED", file="amex.csv"),
        ]
        comment = bi.prepare(rows, ledger(), skip_older=False).rows.iloc[0]["Comment"]
        assert "AMERICAN EXPRESS DD" in comment and "PAYMENT RECEIVED" in comment

    def test_a_few_days_apart_still_pairs(self):
        rows = [
            source("HSBC", DAY, 500, bf.OUT),
            source("BA Amex", DAY + dt.timedelta(days=2), 500, bf.IN, file="a.csv"),
        ]
        assert bi.prepare(rows, ledger(), skip_older=False).paired == 1

    def test_the_same_account_is_never_paired_with_itself(self):
        """£500 out and £500 in on one account is a refund, not a transfer."""
        rows = [
            source("HSBC", DAY, 500, bf.OUT),
            source("HSBC", DAY, 500, bf.IN, row=3),
        ]
        assert bi.prepare(rows, ledger(), skip_older=False).paired == 0

    def test_two_identical_transfers_pair_one_for_one(self):
        rows = [
            source("Savings - Marcus", DAY, 300, bf.OUT),
            source("Savings - Marcus", DAY, 300, bf.OUT, row=3),
            source("HSBC", DAY, 300, bf.IN, file="h.csv"),
            source("HSBC", DAY, 300, bf.IN, row=3, file="h.csv"),
        ]
        result = bi.prepare(rows, ledger(), skip_older=False)
        assert result.paired == 2
        assert result.count == 2

    def test_an_unpaired_half_stays_a_plain_movement(self):
        """A Transfer with no Account To fails validation loudly; a Debit merely reads oddly,
        which is the better failure when the other half is simply not here."""
        rows = [source("HSBC", DAY, 500, bf.OUT, "AMERICAN EXPRESS DD")]
        row = bi.prepare(rows, ledger(), skip_older=False).rows.iloc[0]
        assert row["Type"] == "Debit"
        assert row["Account To"] is None

    def test_wording_alone_does_not_make_something_a_transfer(self):
        """Money leaving a joint account for the other holder's own account is described
        exactly like an internal transfer and is an ordinary debit. There is no way to tell
        from the words, so the description is left alone and the direction stands."""
        rows = [
            source("Savings - Wedding", DAY, 322.92, bf.OUT, "Withdrawal to 30000003"),
            source("Savings - Wedding", DAY, 100, bf.IN, "Transfer from 30000003", row=3),
        ]
        result = bi.prepare(rows, ledger(), skip_older=False)
        assert list(result.rows.sort_values("Amount")["Type"]) == ["Credit", "Debit"]
        assert result.rows["Account To"].isna().all()
        assert not result.rows["Comment"].str.contains(r"\[").any()


def rules_frame(*pairs) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"id": n, "pattern": p, "account_id": n, "account": a, "note": None}
            for n, (p, a) in enumerate(pairs, start=1)
        ]
    )


class TestRules:
    def test_a_pattern_names_the_other_side(self):
        rows = [source("HSBC", DAY, 1158.62, bf.OUT, "HSBC CARD PYMT DD 0192")]
        result = bi.prepare(
            rows, ledger(), rules_frame(("HSBC CARD PYMT", "Mastercard")),
            skip_older=False,
        )
        assert result.ruled == 1
        row = result.rows.iloc[0]
        assert row["Type"] == "Transfer"
        assert row["Account From"] == "HSBC" and row["Account To"] == "Mastercard"

    def test_direction_decides_which_side_is_which(self):
        rows = [source("HSBC", DAY, 300, bf.IN, "FROM THE SAVINGS POT")]
        result = bi.prepare(
            rows, ledger(), rules_frame(("SAVINGS POT", "Savings - Marcus")),
            skip_older=False,
        )
        row = result.rows.iloc[0]
        assert row["Account From"] == "Savings - Marcus"
        assert row["Account To"] == "HSBC"

    def test_a_rule_naming_the_row_s_own_account_is_ignored(self):
        """'HSBC' matches half of HSBC's own descriptions, and a transfer to itself would not
        validate anyway."""
        rows = [source("HSBC", DAY, 20, bf.OUT, "CASH HSBC AUG12")]
        result = bi.prepare(
            rows, ledger(), rules_frame(("HSBC", "HSBC")), skip_older=False
        )
        assert result.ruled == 0
        assert result.rows.iloc[0]["Type"] == "Debit"

    def test_pairing_beats_a_rule(self):
        """A rule cannot tell which card 'AMERICAN EXPRESS DD' paid; the amount can."""
        rows = [
            source("HSBC", DAY, 500, bf.OUT, "AMERICAN EXPRESS DD"),
            source("Platinum Amex", DAY, 500, bf.IN, "PAYMENT RECEIVED", file="p.csv"),
        ]
        result = bi.prepare(
            rows, ledger(), rules_frame(("AMERICAN EXPRESS", "BA Amex")),
            skip_older=False,
        )
        assert result.rows.iloc[0]["Account To"] == "Platinum Amex"

    def test_the_longest_pattern_wins(self):
        """Loaded longest first, so a specific rule beats a general one containing it."""
        rules = pd.DataFrame(
            [
                {"id": 1, "pattern": "CARD PYMT", "account_id": 1, "account": "Mastercard",
                 "note": None},
                {"id": 2, "pattern": "CARD", "account_id": 2, "account": "BA Amex",
                 "note": None},
            ]
        ).sort_values("pattern", key=lambda s: -s.str.len())
        rows = [source("HSBC", DAY, 10, bf.OUT, "HSBC CARD PYMT DD")]
        result = bi.prepare(rows, ledger(), rules, skip_older=False)
        assert result.rows.iloc[0]["Account To"] == "Mastercard"


# ------------------------------------------------------------------- fitting the grid


ACCOUNTS = [
    "BA Amex", "First Direct", "HSBC", "Halifax", "ISA", "Mastercard", "Nationwide",
    "Platinum Amex", "Savings - First Direct", "Savings - Marcus", "Savings - Nationwide",
    "Savings - Service Charge", "Savings - Spending", "Savings - Wedding",
]


class TestGuessingTheAccount:
    def test_a_format_used_by_one_account_answers_itself(self):
        assert bi.guess_account(
            "anything.csv", "", bf.BY_KEY["virgin"], ACCOUNTS
        ) == "Savings - Spending"

    def test_the_filename_is_used_where_it_helps(self):
        assert bi.guess_account(
            "Savings_Marcus_Transactions.csv", "", bf.BY_KEY["marcus"], ACCOUNTS
        ) == "Savings - Marcus"

    def test_a_tie_returns_nothing_rather_than_a_coin_toss(self):
        """Filing a month of spending against the wrong account is worse than a dropdown that
        starts empty."""
        assert bi.guess_account(
            "Transactions 10000001.csv", " Main Savings", bf.BY_KEY["marcus"], ACCOUNTS
        ) is None

    def test_the_three_headerless_accounts_cannot_be_told_apart_by_shape_alone(self):
        assert bi.guess_account("export.csv", "", bf.BY_KEY["hsbc"], ACCOUNTS) is None


class TestTheGrid:
    def test_the_result_matches_the_import_template(self):
        from budget import importer

        rows = [source("HSBC", DAY, 20, bf.OUT)]
        grid = bi.as_grid(bi.prepare(rows, ledger(), skip_older=False).rows)
        assert list(grid.columns) == list(importer.template().columns)

    def test_an_empty_result_still_carries_the_column_types(self):
        """Otherwise clearing the files turns the editor into a grid of free text."""
        from budget import importer

        grid = bi.as_grid(pd.DataFrame())
        assert list(grid.dtypes) == list(importer.template().dtypes)

    def test_the_rows_parse_back_into_candidates(self):
        """The grid is the contract between this and the existing importer. A column named
        differently at either end fails here rather than on screen."""
        from budget import importer

        rows = [
            source("HSBC", DAY, 500, bf.OUT, "AMERICAN EXPRESS DD"),
            source("BA Amex", DAY, 500, bf.IN, "PAYMENT RECEIVED", file="a.csv"),
            source("BA Amex", DAY, 4.50, bf.OUT, "COFFEE HUT"),
        ]
        grid = bi.as_grid(bi.prepare(rows, ledger(), skip_older=False).rows)
        candidates, problems = importer.parse(grid)
        assert problems == []
        assert len(candidates) == 2
        transfer = next(c for c in candidates if c.type == "Transfer")
        assert transfer.account_from == "HSBC" and transfer.account_to == "BA Amex"
        assert transfer.amount == Decimal("500")
        # What a bank statement cannot know is left for the user.
        assert all(c.category is None and c.classification is None for c in candidates)

    def test_amounts_reach_the_grid_positive(self):
        """Direction lives in the type; a negative amount is rejected by validation."""
        rows = [source("BA Amex", DAY, 4.50, bf.OUT), source("HSBC", DAY, 9, bf.IN)]
        grid = bi.as_grid(bi.prepare(rows, ledger(), skip_older=False).rows)
        assert (grid["Amount"] > 0).all()


class TestEndToEnd:
    def test_a_file_becomes_reviewable_rows(self):
        """Read, decide, and hand to the grid -- the whole path, on one sample."""
        from budget import importer

        fmt, _ = bf.detect(AMEX)
        rows = bi.rows_from(bf.read(AMEX, fmt), "BA Amex", "amex.csv")
        result = bi.prepare(rows, ledger(), skip_older=False)
        candidates, problems = importer.parse(bi.as_grid(result.rows))
        assert problems == []
        assert {c.type for c in candidates} == {"Debit", "Credit"}
        assert sum(c.amount for c in candidates) == Decimal("524.50")


class TestTheGracePeriod:
    """The age test allows a week either side of the last recorded movement.

    Accounts are not written up in date order -- a transfer recorded on the day it was made
    sits after a card purchase from the same week entered later -- so the last entry is a
    rough edge rather than a watermark. A bare 'older than the last entry' line takes genuine
    gaps with it, and those are exactly the rows worth having.
    """

    CUTOFF = dt.date(2026, 4, 20)

    def recorded(self):
        return ledger(("Halifax", "debit", 99, self.CUTOFF))

    def kept(self, when: dt.date, **kwargs) -> int:
        rows = [source("Halifax", when, 10, bf.OUT)]
        return bi.prepare(rows, self.recorded(), **kwargs).count

    def test_a_few_days_before_the_last_entry_still_comes_in(self):
        assert self.kept(self.CUTOFF - dt.timedelta(days=3)) == 1

    def test_exactly_a_week_before_still_comes_in(self):
        assert self.kept(self.CUTOFF - dt.timedelta(days=7)) == 1

    def test_a_day_past_the_grace_does_not(self):
        assert self.kept(self.CUTOFF - dt.timedelta(days=8)) == 0

    def test_months_before_certainly_does_not(self):
        assert self.kept(dt.date(2026, 2, 20)) == 0

    def test_the_window_is_adjustable(self):
        assert self.kept(self.CUTOFF - dt.timedelta(days=20), grace_days=30) == 1

    def test_what_the_grace_lets_through_is_still_checked_for_duplicates(self):
        """The whole point of the wider window is to catch genuine gaps, not to wave through
        things already recorded. The duplicate check runs first and is unaffected."""
        when = self.CUTOFF - dt.timedelta(days=5)
        recorded = ledger(("Halifax", "debit", 99, self.CUTOFF),
                          ("Halifax", "debit", 10, when))
        rows = [source("Halifax", when, 10, bf.OUT)]
        result = bi.prepare(rows, recorded)
        assert result.count == 0
        assert bi.ALREADY_RECORDED in result.excluded.iloc[0]["Why"]


class TestReinstating:
    """Every exclusion is a judgement, so every exclusion is reversible."""

    def excluded_key(self, result) -> str:
        return result.excluded.iloc[0]["key"]

    def test_a_key_names_the_line_it_came_from(self):
        row = source("HSBC", DAY, 20, bf.OUT, row=7, file="hsbc.csv")
        assert row.key == "hsbc.csv#7"

    def test_the_left_out_list_carries_the_key(self):
        rows = [source("HSBC", DAY, 20, bf.OUT)]
        result = bi.prepare(rows, ledger(("HSBC", "debit", 20, DAY)), skip_older=False)
        assert self.excluded_key(result) == rows[0].key

    def test_a_duplicate_can_be_brought_back(self):
        rows = [source("HSBC", DAY, 20, bf.OUT)]
        recorded = ledger(("HSBC", "debit", 20, DAY))
        first = bi.prepare(rows, recorded, skip_older=False)
        again = bi.prepare(
            rows, recorded, skip_older=False, reinstate={self.excluded_key(first)}
        )
        assert again.count == 1
        assert again.reinstated == 1
        assert again.excluded.empty

    def test_an_old_row_can_be_brought_back(self):
        recorded = ledger(("Halifax", "debit", 99, dt.date(2026, 4, 20)))
        rows = [source("Halifax", dt.date(2026, 2, 20), 10, bf.OUT)]
        first = bi.prepare(rows, recorded)
        again = bi.prepare(rows, recorded, reinstate={self.excluded_key(first)})
        assert again.count == 1

    def test_reinstating_half_a_pair_unpairs_the_other_half(self):
        """The consequence that makes this a re-run rather than a patch. Adding the row back
        beside a Transfer that still accounts for it would count the money twice."""
        rows = [
            source("HSBC", DAY, 500, bf.OUT, "AMERICAN EXPRESS DD"),
            source("BA Amex", DAY, 500, bf.IN, "PAYMENT RECEIVED", file="amex.csv"),
        ]
        first = bi.prepare(rows, ledger(), skip_older=False)
        assert first.paired == 1 and first.count == 1

        again = bi.prepare(
            rows, ledger(), skip_older=False, reinstate={self.excluded_key(first)}
        )
        assert again.paired == 0
        assert again.count == 2
        assert set(again.rows["Type"]) == {"Debit", "Credit"}
        assert again.rows["Account To"].isna().all()

    def test_an_untouched_key_changes_nothing(self):
        rows = [source("HSBC", DAY, 20, bf.OUT)]
        recorded = ledger(("HSBC", "debit", 20, DAY))
        assert bi.prepare(
            rows, recorded, skip_older=False, reinstate={"nothing.csv#99"}
        ).count == 0

    def test_a_reinstated_row_reaches_the_grid(self):
        from budget import importer

        rows = [source("HSBC", DAY, 20, bf.OUT, "CASH MACHINE")]
        recorded = ledger(("HSBC", "debit", 20, DAY))
        first = bi.prepare(rows, recorded, skip_older=False)
        again = bi.prepare(
            rows, recorded, skip_older=False, reinstate={self.excluded_key(first)}
        )
        candidates, problems = importer.parse(bi.as_grid(again.rows))
        assert problems == []
        assert len(candidates) == 1
        assert candidates[0].amount == Decimal("20")
        assert candidates[0].account_from == "HSBC"

    def test_keys_are_unique_across_files_with_the_same_line_numbers(self):
        """Two banks both have a row 2. Keying on the line alone would make reinstating one
        reinstate the other."""
        rows = [
            source("HSBC", DAY, 20, bf.OUT, row=2, file="a.csv"),
            source("Halifax", DAY, 30, bf.OUT, row=2, file="b.csv"),
        ]
        recorded = ledger(("HSBC", "debit", 20, DAY), ("Halifax", "debit", 30, DAY))
        first = bi.prepare(rows, recorded, skip_older=False)
        assert len(set(first.excluded["key"])) == 2

        again = bi.prepare(
            rows, recorded, skip_older=False, reinstate={"a.csv#2"}
        )
        assert again.count == 1
        assert again.rows.iloc[0]["Account From"] == "HSBC"


class TestTheEditorRoundTrip:
    """The tick box on the left-out list, and what comes back from it.

    st.data_editor hands back whatever the user left it as, and an untouched checkbox column
    is not reliably a column of False. Boolean indexing on nulls raises rather than treating
    them as unticked, so a page that reads the ticks naively works right up until the first
    time nobody ticks anything.
    """

    def excluded(self):
        rows = [
            source("HSBC", DAY, 20, bf.OUT, "CASH", row=2),
            source("HSBC", DAY, 30, bf.OUT, "SHOP", row=3),
        ]
        recorded = ledger(("HSBC", "debit", 20, DAY), ("HSBC", "debit", 30, DAY))
        return rows, recorded, bi.prepare(rows, recorded, skip_older=False).excluded

    def test_the_offer_puts_a_tick_box_in_front(self):
        _rows, _recorded, excluded = self.excluded()
        offered = bi.offer_back(excluded)
        assert list(offered.columns)[0] == bi.INCLUDE
        assert not offered[bi.INCLUDE].any()

    def test_nothing_ticked_is_nothing_spared(self):
        _rows, _recorded, excluded = self.excluded()
        assert bi.spared_keys(bi.offer_back(excluded)) == set()

    def test_a_tick_is_read_back(self):
        _rows, _recorded, excluded = self.excluded()
        offered = bi.offer_back(excluded)
        offered.loc[0, bi.INCLUDE] = True
        assert bi.spared_keys(offered) == {offered.loc[0, "key"]}

    def test_an_untouched_column_of_nulls_does_not_raise(self):
        """The shape that broke the obvious spelling: object dtype with None in it."""
        _rows, _recorded, excluded = self.excluded()
        offered = bi.offer_back(excluded)
        offered[bi.INCLUDE] = [None, None]
        assert bi.spared_keys(offered) == set()

    def test_a_mixture_of_nulls_and_ticks_reads_the_ticks(self):
        _rows, _recorded, excluded = self.excluded()
        offered = bi.offer_back(excluded)
        offered[bi.INCLUDE] = [None, True]
        assert bi.spared_keys(offered) == {offered.loc[1, "key"]}

    def test_an_empty_list_is_handled(self):
        assert bi.spared_keys(pd.DataFrame()) == set()

    def test_the_ticked_row_comes_through_on_the_second_pass(self):
        """The whole round trip: exclude, offer, tick, re-run."""
        rows, recorded, excluded = self.excluded()
        offered = bi.offer_back(excluded)
        offered.loc[0, bi.INCLUDE] = True

        again = bi.prepare(
            rows, recorded, skip_older=False, reinstate=bi.spared_keys(offered)
        )
        assert again.count == 1
        assert again.reinstated == 1
        assert len(again.excluded) == 1  # the row that was not ticked
