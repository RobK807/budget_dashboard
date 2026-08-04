"""Create, amend and retire accounts, categories and classifications.

This replaces the six Add/Remove UserForms and, with them, `update_months` -- the macro that
copied Monthly_Template over every month from the change onwards and destroyed any
transactions already there. Every one of those forms had to warn:

    "This will override all months from X onwards, if there are any existing transactions
     in the months in scope this may cause errors in the spreadsheet."

None of that applies here. Nothing is stored positionally, so adding an account is an INSERT
with a `valid_from` and earlier months are untouched by construction. Removing one sets
`valid_to`: it stays selectable for the months it existed in and disappears from new entry.

Rows are never hard-deleted once anything references them -- that would orphan history. The
only true delete is for something created by mistake and never used.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from budget.models import (
    Account,
    AccountTarget,
    Bonus,
    Budget,
    Card,
    CardStatement,
    Category,
    Classification,
    ClassificationAllowance,
    CyclingDay,
    CyclingOutgoing,
    CyclingRate,
    Payslip,
    Projection,
    SalaryAssumption,
    SalaryProfile,
    SavingsTarget,
    Setting,
    Txn,
)
from budget.service import bump_revision

ACCOUNT_TYPES = ("bank", "credit_card")
# The workbook called these 'positive' and 'negative', describing the sign of the running
# total. 'debit' and 'credit' say which kind of balance carries forward, which is the same
# rule in the vocabulary the rest of the app already uses.
ROLLOVERS = ("none", "all", "credit", "debit")
SPEND_TYPES = ("Credit", "Debit", "All")


@dataclass
class Outcome:
    ok: bool
    message: str
    warnings: list[str] = field(default_factory=list)


MODELS = {"account": Account, "category": Category, "classification": Classification}


# ------------------------------------------------------------------------------ usage


def usage(session: Session) -> dict[str, dict[int, int]]:
    """How many transactions reference each row, deleted ones included.

    A soft-deleted transaction still points at its category, so a row with only deleted
    references still cannot be hard-deleted without orphaning them.
    """
    counts: dict[str, dict[int, int]] = {}

    account_rows = session.execute(
        select(Account.id, func.count(Txn.id))
        .outerjoin(
            Txn, or_(Txn.account_from_id == Account.id, Txn.account_to_id == Account.id)
        )
        .group_by(Account.id)
    )
    counts["account"] = {row[0]: row[1] for row in account_rows}

    for name, model, column in (
        ("category", Category, Txn.category_id),
        ("classification", Classification, Txn.classification_id),
    ):
        rows = session.execute(
            select(model.id, func.count(Txn.id))
            .outerjoin(Txn, column == model.id)
            .group_by(model.id)
        )
        counts[name] = {row[0]: row[1] for row in rows}

    return counts


def last_use(session: Session, kind: str, row_id: int) -> dt.date | None:
    """Date of the most recent transaction referencing this row."""
    if kind == "account":
        condition = or_(Txn.account_from_id == row_id, Txn.account_to_id == row_id)
    elif kind == "category":
        condition = Txn.category_id == row_id
    else:
        condition = Txn.classification_id == row_id
    return session.scalar(select(func.max(Txn.txn_date)).where(condition))


def first_use(session: Session, kind: str, row_id: int) -> dt.date | None:
    if kind == "account":
        condition = or_(Txn.account_from_id == row_id, Txn.account_to_id == row_id)
    elif kind == "category":
        condition = Txn.category_id == row_id
    else:
        condition = Txn.classification_id == row_id
    return session.scalar(select(func.min(Txn.txn_date)).where(condition))


# ------------------------------------------------------------------------ name checks


def _name_taken(session: Session, model, name: str, exclude_id: int | None = None) -> bool:
    stmt = select(model.id).where(func.lower(model.name) == name.strip().lower())
    if exclude_id is not None:
        stmt = stmt.where(model.id != exclude_id)
    return session.scalar(stmt) is not None


def _code_taken(session: Session, code: str, exclude_id: int | None = None) -> bool:
    stmt = select(Account.id).where(func.lower(Account.short_code) == code.strip().lower())
    if exclude_id is not None:
        stmt = stmt.where(Account.id != exclude_id)
    return session.scalar(stmt) is not None


# ---------------------------------------------------------------------------- accounts


def add_account(
    session: Session,
    name: str,
    short_code: str,
    account_type: str,
    valid_from: dt.date,
    is_savings: bool = False,
    savings_limit: Decimal | None = None,
    is_investment: bool = False,
    investment_limit: Decimal | None = None,
    is_isa: bool = False,
) -> tuple[Account | None, Outcome]:
    name, short_code = name.strip(), short_code.strip().upper()

    if not name:
        return None, Outcome(False, "Name is required")
    if not short_code:
        return None, Outcome(False, "Short code is required")
    if account_type not in ACCOUNT_TYPES:
        return None, Outcome(False, f"Type must be one of {', '.join(ACCOUNT_TYPES)}")
    if _name_taken(session, Account, name):
        return None, Outcome(False, f"An account named {name!r} already exists")
    if _code_taken(session, short_code):
        return None, Outcome(False, f"Short code {short_code!r} is already used")
    if is_savings and is_investment:
        return None, Outcome(False, "An account cannot be both savings and investment")

    account = Account(
        name=name,
        short_code=short_code,
        type=account_type,
        valid_from=valid_from,
        is_savings=is_savings,
        savings_limit=savings_limit if is_savings else None,
        is_investment=is_investment,
        investment_limit=investment_limit if is_investment else None,
        is_isa=is_isa,
    )
    session.add(account)
    session.flush()
    bump_revision(session)

    return account, Outcome(
        True,
        f"Added {name}, available from {valid_from:%d %b %Y}.",
        ["Earlier months are unaffected — nothing was rewritten."],
    )


def update_account(session: Session, account_id: int, **fields) -> Outcome:
    account = session.get(Account, account_id)
    if account is None:
        return Outcome(False, "Account not found")

    warnings = []
    if "name" in fields:
        name = fields["name"].strip()
        if not name:
            return Outcome(False, "Name is required")
        if _name_taken(session, Account, name, exclude_id=account_id):
            return Outcome(False, f"An account named {name!r} already exists")
        if name != account.name:
            warnings.append(
                "Renaming affects historic reporting labels; the reconciliation script "
                "matches the workbook by name and will flag the difference."
            )
        account.name = name

    if "short_code" in fields:
        code = fields["short_code"].strip().upper()
        if _code_taken(session, code, exclude_id=account_id):
            return Outcome(False, f"Short code {code!r} is already used")
        account.short_code = code

    if fields.get("is_savings") and fields.get("is_investment"):
        return Outcome(False, "An account cannot be both savings and investment")

    for key in ("type", "is_savings", "is_investment", "is_isa", "savings_limit",
                "investment_limit", "valid_from", "exclude_from_savings",
                "statement_day", "payment_day"):
        if key in fields:
            setattr(account, key, fields[key])

    if not account.is_savings:
        account.savings_limit = None
    if not account.is_investment:
        account.investment_limit = None

    earliest = first_use(session, "account", account_id)
    if earliest and account.valid_from > earliest:
        return Outcome(
            False,
            f"{account.name} is already used on {earliest:%d %b %Y}, before the opening "
            "date given.",
        )

    session.flush()
    bump_revision(session)
    return Outcome(True, f"Updated {account.name}.", warnings)


# ------------------------------------------------------------- retire / reinstate / delete


def retire(session: Session, kind: str, row_id: int, valid_to: dt.date) -> Outcome:
    """Close a row from a date. It stays selectable for the months it existed in.

    This is what `remove_account` could not do: the macro deleted the columns outright, so
    the account vanished from months it had genuinely been used in.
    """
    model = MODELS[kind]
    row = session.get(model, row_id)
    if row is None:
        return Outcome(False, f"{kind.title()} not found")
    if valid_to < row.valid_from:
        return Outcome(False, "Closing date is before the opening date")

    latest = last_use(session, kind, row_id)
    if latest and valid_to < latest:
        return Outcome(
            False,
            f"{row.name} is used on {latest:%d %b %Y}, after the closing date given. "
            "Choose a later date, or remove those transactions first.",
        )

    row.valid_to = valid_to
    session.flush()
    bump_revision(session)
    return Outcome(
        True,
        f"{row.name} closed from {valid_to:%d %b %Y}.",
        ["It remains available for earlier months and for historic reporting."],
    )


def reinstate(session: Session, kind: str, row_id: int) -> Outcome:
    row = session.get(MODELS[kind], row_id)
    if row is None:
        return Outcome(False, f"{kind.title()} not found")
    if row.valid_to is None:
        return Outcome(True, f"{row.name} is already open.")
    row.valid_to = None
    session.flush()
    bump_revision(session)
    return Outcome(True, f"{row.name} reopened.")


def delete(session: Session, kind: str, row_id: int) -> Outcome:
    """Hard delete, permitted only for a row nothing references.

    Anything with history must be retired instead; deleting it would orphan transactions
    that point at it, which is the mistake the workbook made when remove_category stripped
    a category while leaving every Debug row still naming it.
    """
    row = session.get(MODELS[kind], row_id)
    if row is None:
        return Outcome(False, f"{kind.title()} not found")

    count = usage(session)[kind].get(row_id, 0)
    if count:
        return Outcome(
            False,
            f"{row.name} is used by {count} transaction(s). Close it from a date instead — "
            "deleting it would orphan them.",
        )

    name = row.name
    session.delete(row)
    session.flush()
    bump_revision(session)
    return Outcome(True, f"Deleted {name}. It was never used.")


# -------------------------------------------------------------------------- categories


def add_category(
    session: Session, name: str, grouping: str, spend_type: str, valid_from: dt.date
) -> tuple[Category | None, Outcome]:
    name = name.strip()
    if not name:
        return None, Outcome(False, "Name is required")
    if spend_type not in SPEND_TYPES:
        return None, Outcome(False, f"Spend type must be one of {', '.join(SPEND_TYPES)}")
    if _name_taken(session, Category, name):
        return None, Outcome(False, f"A category named {name!r} already exists")

    category = Category(
        name=name, grouping=grouping.strip(), spend_type=spend_type, valid_from=valid_from
    )
    session.add(category)
    session.flush()
    bump_revision(session)
    return category, Outcome(True, f"Added category {name}.")


def update_category(session: Session, category_id: int, **fields) -> Outcome:
    category = session.get(Category, category_id)
    if category is None:
        return Outcome(False, "Category not found")

    if "name" in fields:
        name = fields["name"].strip()
        if not name:
            return Outcome(False, "Name is required")
        if _name_taken(session, Category, name, exclude_id=category_id):
            return Outcome(False, f"A category named {name!r} already exists")
        category.name = name

    for key in ("grouping", "spend_type", "valid_from"):
        if key in fields:
            setattr(category, key, fields[key])

    if category.spend_type not in SPEND_TYPES:
        return Outcome(False, f"Spend type must be one of {', '.join(SPEND_TYPES)}")

    session.flush()
    bump_revision(session)
    return Outcome(True, f"Updated {category.name}.")


# --------------------------------------------------------------------- classifications


def add_classification(
    session: Session,
    name: str,
    direction: int,
    rollover: str,
    valid_from: dt.date,
    counts_as_spend: bool = True,
) -> tuple[Classification | None, Outcome]:
    name = name.strip()
    if not name:
        return None, Outcome(False, "Name is required")
    if direction not in (1, -1):
        return None, Outcome(False, "Direction must be +1 or -1")
    if rollover not in ROLLOVERS:
        return None, Outcome(False, f"Rollover must be one of {', '.join(ROLLOVERS)}")
    if _name_taken(session, Classification, name):
        return None, Outcome(False, f"A classification named {name!r} already exists")

    # legacy_ref mirrors Selections!I, the integer the workbook's SUMIFS matched on. Kept
    # unique so a migrated prior year can still be read back.
    next_ref = (session.scalar(select(func.max(Classification.legacy_ref))) or 0) + 1

    classification = Classification(
        name=name,
        legacy_ref=next_ref,
        direction=direction,
        rollover=rollover,
        counts_as_spend=counts_as_spend,
        valid_from=valid_from,
    )
    session.add(classification)
    session.flush()
    bump_revision(session)
    return classification, Outcome(True, f"Added classification {name}.")


def update_classification(session: Session, classification_id: int, **fields) -> Outcome:
    classification = session.get(Classification, classification_id)
    if classification is None:
        return Outcome(False, "Classification not found")

    warnings = []
    if "name" in fields:
        name = fields["name"].strip()
        if not name:
            return Outcome(False, "Name is required")
        if _name_taken(session, Classification, name, exclude_id=classification_id):
            return Outcome(False, f"A classification named {name!r} already exists")
        classification.name = name

    if "direction" in fields:
        if fields["direction"] not in (1, -1):
            return Outcome(False, "Direction must be +1 or -1")
        if fields["direction"] != classification.direction:
            warnings.append(
                "Changing direction flips the sign of every historic total for this "
                "classification, not just future ones."
            )
        classification.direction = fields["direction"]

    if "rollover" in fields:
        if fields["rollover"] not in ROLLOVERS:
            return Outcome(False, f"Rollover must be one of {', '.join(ROLLOVERS)}")
        classification.rollover = fields["rollover"]

    for key in ("counts_as_spend", "valid_from"):
        if key in fields:
            setattr(classification, key, fields[key])

    session.flush()
    bump_revision(session)
    return Outcome(True, f"Updated {classification.name}.", warnings)


# ---------------------------------------------------------------------------- settings


def set_setting(session: Session, key: str, value: str) -> Outcome:
    setting = session.get(Setting, key)
    if setting is None:
        session.add(Setting(key=key, value=str(value)))
    else:
        setting.value = str(value)
    session.flush()
    bump_revision(session)
    return Outcome(True, f"Saved {key.replace('_', ' ')}.")


# ------------------------------------------------------------------------------- cards


def add_card(
    session: Session,
    name: str,
    opening_balance: Decimal,
    opening_date: dt.date,
    term_months: int,
    min_payment_pct: Decimal,
    payment_day: int | None = None,
    credit_limit: Decimal | None = None,
) -> tuple[Card | None, Outcome]:
    """A balance-transfer card, tracked for payoff.

    `min_payment_pct` is stored as a fraction (0.025), which is how the workbook's S column
    held it and what cards.schedule expects; the UI shows it as a percentage.
    """
    name = (name or "").strip()
    if not name:
        return None, Outcome(False, "A card needs a name.")
    if session.scalar(select(Card).where(func.lower(Card.name) == name.lower())):
        return None, Outcome(False, f"There is already a card called {name}.")
    if term_months < 1:
        return None, Outcome(False, "The term must be at least one month.")

    order = session.scalar(select(func.max(Card.display_order)))
    card = Card(
        name=name,
        opening_balance=Decimal(opening_balance),
        opening_date=opening_date,
        term_months=int(term_months),
        min_payment_pct=Decimal(min_payment_pct),
        payment_day=payment_day,
        credit_limit=Decimal(credit_limit) if credit_limit else None,
        display_order=(order or 0) + 1,
    )
    session.add(card)
    session.flush()
    bump_revision(session)
    return card, Outcome(True, f"Added {name}.")


def update_card(session: Session, card_id: int, **fields) -> Outcome:
    card = session.get(Card, card_id)
    if card is None:
        return Outcome(False, "No such card.")

    name = fields.get("name")
    if name is not None:
        name = str(name).strip()
        if not name:
            return Outcome(False, "A card needs a name.")
        clash = session.scalar(
            select(Card).where(func.lower(Card.name) == name.lower(), Card.id != card_id)
        )
        if clash:
            return Outcome(False, f"There is already a card called {name}.")
        card.name = name

    for key in ("opening_balance", "min_payment_pct", "credit_limit"):
        if key in fields:
            value = fields[key]
            setattr(card, key, Decimal(str(value)) if value is not None else None)
    for key in ("opening_date",):
        if key in fields and fields[key] is not None:
            setattr(card, key, fields[key])
    for key in ("term_months", "payment_day"):
        if key in fields:
            value = fields[key]
            setattr(card, key, int(value) if value is not None else None)

    session.flush()
    bump_revision(session)
    return Outcome(True, f"Updated {card.name}.")


def delete_card(session: Session, card_id: int) -> Outcome:
    """Cards carry no transactions -- they are tracked separately from the accounts spending
    is recorded against -- so there is no history to orphan."""
    card = session.get(Card, card_id)
    if card is None:
        return Outcome(False, "No such card.")
    name = card.name
    session.delete(card)
    session.flush()
    bump_revision(session)
    return Outcome(True, f"Removed {name}.")


# ---------------------------------------------------------------------- salary inputs


def set_salary_profile(
    session: Session, effective_from: dt.date, annual_salary: Decimal, note: str | None = None
) -> Outcome:
    """One row per change of salary; re-stating a date replaces it."""
    existing = session.scalar(
        select(SalaryProfile).where(SalaryProfile.effective_from == effective_from)
    )
    if existing:
        existing.annual_salary = Decimal(annual_salary)
        existing.note = note
    else:
        session.add(
            SalaryProfile(
                effective_from=effective_from,
                annual_salary=Decimal(annual_salary),
                note=note,
            )
        )
    session.flush()
    bump_revision(session)
    return Outcome(True, f"Salary from {effective_from:%d %b %Y} saved.")


def remove_salary_profile(session: Session, profile_id: int) -> Outcome:
    profile = session.get(SalaryProfile, profile_id)
    if profile is None:
        return Outcome(False, "No such salary record.")
    session.delete(profile)
    session.flush()
    bump_revision(session)
    return Outcome(True, "Salary record removed.")


BONUS_ACTUALS = ("gross", "ni", "paye", "net")


def set_bonus(
    session: Session,
    period: str,
    amount: Decimal,
    note: str | None = None,
    **actuals,
) -> Outcome:
    """A bonus for a month: the expected amount, and optionally what was actually paid.

    The actual figures live here rather than on the payslip because a bonus usually arrives
    on its own day. `payslip` is keyed by period, so putting them there would have meant the
    second payment of a month overwriting the first.
    """
    existing = session.get(Bonus, period)
    if amount is None or Decimal(amount) == 0:
        if existing:
            session.delete(existing)
            session.flush()
            bump_revision(session)
            return Outcome(True, f"Bonus for {period} removed.")
        return Outcome(True, "No bonus to record.")

    if existing is None:
        existing = Bonus(period=period, amount=Decimal(amount))
        session.add(existing)
    else:
        existing.amount = Decimal(amount)
    existing.note = note

    for key in BONUS_ACTUALS:
        if key in actuals:
            value = actuals[key]
            setattr(existing, key, Decimal(str(value)) if value is not None else None)
    if "payday" in actuals:
        existing.payday = int(actuals["payday"]) if actuals["payday"] else None

    session.flush()
    bump_revision(session)
    return Outcome(True, f"Bonus for {period} saved.")


def remove_bonus(session: Session, period: str) -> Outcome:
    existing = session.get(Bonus, period)
    if existing is None:
        return Outcome(False, f"No bonus recorded for {period}.")
    session.delete(existing)
    session.flush()
    bump_revision(session)
    return Outcome(True, f"Bonus for {period} removed.")


def remove_payslip(session: Session, period: str) -> Outcome:
    """Delete a payslip outright, for when the wrong month was filled in.

    A real delete rather than a soft one: a payslip carries nothing else and is re-enterable
    from the paper copy, so there is no history to preserve.
    """
    existing = session.get(Payslip, period)
    if existing is None:
        return Outcome(False, f"No payslip recorded for {period}.")
    session.delete(existing)
    session.flush()
    bump_revision(session)
    return Outcome(True, f"Payslip for {period} removed.")


def set_payslip(session: Session, period: str, **fields) -> Outcome:
    """The actual side of the Salary tracker, which had no way in before now.

    Only the fields given are touched, so a month can be filled in as the payslip arrives
    rather than all at once.
    """
    payslip = session.get(Payslip, period)
    if payslip is None:
        payslip = Payslip(period=period)
        session.add(payslip)

    for key in ("gross", "ni", "paye", "net", "holiday_pay", "cycle_to_work",
                "benefits", "additional", "salary", "expected_gross"):
        if key in fields:
            value = fields[key]
            setattr(payslip, key, Decimal(str(value)) if value is not None else None)
    if "payday" in fields:
        payslip.payday = int(fields["payday"]) if fields["payday"] else None

    session.flush()
    bump_revision(session)
    return Outcome(True, f"Payslip for {period} saved.")


def set_assumption(
    session: Session,
    tax_year: int,
    key: str,
    value: Decimal,
    effective_from: dt.date | None = None,
) -> Outcome:
    """A band or rate for a tax year. Rates are percentages -- see models.SalaryAssumption."""
    effective_from = effective_from or dt.date(tax_year, 4, 1)
    existing = session.get(SalaryAssumption, (tax_year, key, effective_from))
    if existing:
        existing.value = Decimal(value)
    else:
        session.add(
            SalaryAssumption(
                tax_year=tax_year,
                key=key,
                effective_from=effective_from,
                value=Decimal(value),
            )
        )
    session.flush()
    bump_revision(session)
    return Outcome(True, f"Saved {key.replace('_', ' ')}.")


def remove_assumption(
    session: Session, tax_year: int, key: str, effective_from: dt.date
) -> Outcome:
    existing = session.get(SalaryAssumption, (tax_year, key, effective_from))
    if existing is None:
        return Outcome(False, "No such band.")
    session.delete(existing)
    session.flush()
    bump_revision(session)
    return Outcome(True, "Band removed.")


# ---------------------------------------------------------------------- cycling rates


def set_cycling_rate(
    session: Session, kind: str, effective_from: dt.date, amount: Decimal
) -> Outcome:
    if kind not in ("commute", "band", "gym"):
        return Outcome(False, f"Unknown kind {kind!r}.")
    existing = session.get(CyclingRate, (kind, effective_from))
    if existing:
        existing.amount = Decimal(amount)
    else:
        session.add(
            CyclingRate(kind=kind, effective_from=effective_from, amount=Decimal(amount))
        )
    session.flush()
    bump_revision(session)
    return Outcome(True, f"{kind.title()} rate from {effective_from:%d %b %Y} saved.")


def remove_cycling_rate(session: Session, kind: str, effective_from: dt.date) -> Outcome:
    existing = session.get(CyclingRate, (kind, effective_from))
    if existing is None:
        return Outcome(False, "No such rate.")
    session.delete(existing)
    session.flush()
    bump_revision(session)
    return Outcome(True, "Rate removed.")


def record_cycling_day(
    session: Session, day: dt.date, kind: str | None
) -> Outcome:
    """At most one entry per day -- the date is the primary key, so this is enforced by the
    schema rather than by a check that could be skipped."""
    existing = session.get(CyclingDay, day)
    if kind is None:
        if existing is None:
            return Outcome(False, f"Nothing recorded for {day:%d %b %Y}.")
        session.delete(existing)
        session.flush()
        bump_revision(session)
        return Outcome(True, f"Removed the entry for {day:%d %b %Y}.")

    if kind not in ("commute", "band", "gym"):
        return Outcome(False, f"Unknown kind {kind!r}.")

    flags = {"commute": kind == "commute", "band": kind == "band", "gym": kind == "gym"}
    replaced = existing is not None
    if existing is None:
        session.add(CyclingDay(date=day, **flags))
    else:
        for name, value in flags.items():
            setattr(existing, name, value)

    session.flush()
    bump_revision(session)
    verb = "Replaced" if replaced else "Recorded"
    return Outcome(True, f"{verb} {kind} for {day:%d %b %Y}.")


def add_cycling_outgoing(
    session: Session, day: dt.date, item: str, amount: Decimal, flag: str
) -> Outcome:
    if not item or not str(item).strip():
        return Outcome(False, "An outgoing needs a description.")
    if Decimal(amount) <= 0:
        return Outcome(False, "An outgoing needs a positive amount.")
    session.add(
        CyclingOutgoing(
            date=day, item=str(item).strip(), amount=Decimal(amount), flag=flag
        )
    )
    session.flush()
    bump_revision(session)
    return Outcome(True, f"Added {item} on {day:%d %b %Y}.")


def remove_cycling_outgoing(session: Session, outgoing_id: int) -> Outcome:
    row = session.get(CyclingOutgoing, outgoing_id)
    if row is None:
        return Outcome(False, "No such outgoing.")
    session.delete(row)
    session.flush()
    bump_revision(session)
    return Outcome(True, "Outgoing removed.")


# --------------------------------------------------------------- periodic parameters


def set_account_target(
    session: Session, period: str, account_id: int, amount: Decimal | None
) -> Outcome:
    existing = session.get(AccountTarget, (period, account_id))
    if amount is None:
        if existing:
            session.delete(existing)
        session.flush()
        bump_revision(session)
        return Outcome(True, "Target cleared.")
    if existing:
        existing.amount = Decimal(amount)
    else:
        session.add(
            AccountTarget(period=period, account_id=account_id, amount=Decimal(amount))
        )
    session.flush()
    bump_revision(session)
    return Outcome(True, "Target saved.")


def set_savings_target(
    session: Session,
    period: str,
    savings: Decimal | None = None,
    investments: Decimal | None = None,
) -> Outcome:
    existing = session.get(SavingsTarget, period)
    if existing is None:
        existing = SavingsTarget(period=period)
        session.add(existing)
    existing.savings = Decimal(savings) if savings is not None else None
    existing.investments = Decimal(investments) if investments is not None else None
    session.flush()
    bump_revision(session)
    return Outcome(True, f"Targets for {period} saved.")


def set_card_statement(
    session: Session, period: str, account_id: int, bill_eom: Decimal | None
) -> Outcome:
    existing = session.get(CardStatement, (period, account_id))
    if bill_eom is None:
        if existing:
            session.delete(existing)
        session.flush()
        bump_revision(session)
        return Outcome(True, "Bill cleared.")
    if existing:
        existing.bill_eom = Decimal(bill_eom)
    else:
        session.add(
            CardStatement(period=period, account_id=account_id, bill_eom=Decimal(bill_eom))
        )
    session.flush()
    bump_revision(session)
    return Outcome(True, "Bill saved.")


def set_allowance(
    session: Session, period: str, classification_id: int, daily_amount: Decimal | None
) -> Outcome:
    existing = session.get(ClassificationAllowance, (period, classification_id))
    if daily_amount is None or Decimal(daily_amount) == 0:
        if existing:
            session.delete(existing)
        session.flush()
        bump_revision(session)
        return Outcome(True, "Allowance cleared.")
    if existing:
        existing.daily_amount = Decimal(daily_amount)
    else:
        session.add(
            ClassificationAllowance(
                period=period,
                classification_id=classification_id,
                daily_amount=Decimal(daily_amount),
            )
        )
    session.flush()
    bump_revision(session)
    return Outcome(True, "Allowance saved.")


def set_budget(
    session: Session, period: str, category_id: int, expected: Decimal | None
) -> Outcome:
    """The month's target spend for a category -- month-tab column D."""
    existing = session.get(Budget, (period, category_id))
    if existing is None:
        existing = Budget(period=period, category_id=category_id)
        session.add(existing)
    existing.expected = Decimal(expected) if expected is not None else None
    session.flush()
    bump_revision(session)
    return Outcome(True, "Target saved.")


def set_projection(
    session: Session,
    proj_date: dt.date,
    classification_id: int,
    amount: Decimal,
    comment: str | None = None,
) -> Outcome:
    existing = session.get(Projection, (proj_date, classification_id))
    if existing:
        existing.amount = Decimal(amount)
        existing.comment = comment
    else:
        session.add(
            Projection(
                proj_date=proj_date,
                classification_id=classification_id,
                amount=Decimal(amount),
                comment=comment,
            )
        )
    session.flush()
    bump_revision(session)
    return Outcome(True, f"Projection for {proj_date:%d %b %Y} saved.")


def clear_projections(
    session: Session, period: str, classification_ids: list[int] | None = None
) -> int:
    """Wipe a month's projections before re-entering them, so a shorter replacement does not
    leave orphans from the longer version it replaced.

    `classification_ids` narrows it to the ones being rewritten. That matters once the entry
    grid can be filtered: saving a screen showing only Bills must not silently delete the
    Excess rows that the filter happened to be hiding.
    """
    year, month = (int(p) for p in period.split("-"))
    start = dt.date(year, month, 1)
    end = dt.date(year + (month == 12), month % 12 + 1, 1)
    statement = select(Projection).where(
        Projection.proj_date >= start, Projection.proj_date < end
    )
    if classification_ids is not None:
        statement = statement.where(
            Projection.classification_id.in_(list(classification_ids))
        )
    rows = list(session.scalars(statement))
    for row in rows:
        session.delete(row)
    session.flush()
    if rows:
        bump_revision(session)
    return len(rows)
