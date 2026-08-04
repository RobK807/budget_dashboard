"""Salary -- replaces the Salary tracker tab.

The expected-pay columns are reproduced by budget/tax.py, a pure function over the stored
bands. It matches the workbook to the penny for all twelve months, including the bonus month
and the four tapered personal-allowance steps.

Everything the model reads is editable here. In the workbook the annual salary was typed into
all twelve rows of column O, the bonus was welded into May's formula as `+29028.48`, and the
bands sat in a block of cells with no way in but the cell itself.

Salary and bonus are two payments, not one. A bonus usually lands on its own day with its own
deductions, so it carries its own actual gross, NI, PAYE and net; the month's actual figures
are the two added together. Holding both on `payslip`, which is keyed by period, would have
meant the second payment of a month overwriting the first.
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

import pandas as pd
import plotly.express as px
import streamlit as st

from budget import reference, repo, tax, ui

data = ui.page_header("Salary", "Payslips against the expected PAYE and NI model.")

payslips = data["payslips"]
profiles = data["salary_profiles"]
bonuses = data["bonuses"]
READ_ONLY = ui.read_only()

if READ_ONLY:
    st.warning(
        "**Read-only while checked out for offline use.** Check in on the Sync page first."
    )

PENCE = Decimal("0.01")

# ------------------------------------------------------------------- bands by tax year
#
# A period is taxed under the bands of its own tax year, at the values in force on its first
# day. Both matter now that the month lists run past the end of one fiscal year: taking the
# current year's bands for every month would have applied 2026/27 rates to a 2027/28 payslip.

available_years = data["tax_years"]
with ui.session() as session:
    assumptions_by_year = {
        year: repo.load_salary_assumptions(session, year) for year in available_years
    }

_band_cache: dict[str, tax.Bands] = {}


def bands_for(period: str) -> tax.Bands:
    if period not in _band_cache:
        wanted = repo.tax_year_of(period)
        usable = [y for y in available_years if y <= wanted] or available_years
        _band_cache[period] = repo.bands_from(
            assumptions_by_year[usable[-1]], repo.period_start(period)
        )
    return _band_cache[period]


bands = bands_for(repo.period_of(dt.date.today()))


def as_decimal(value) -> Decimal:
    if value is None or pd.isna(value):
        return Decimal("0")
    return Decimal(str(value))


PARAMS = repo.salary_parameters(data["settings"])


def expected_for(period: str, row) -> tax.Breakdown | None:
    """One month's expected pay, from its parts rather than from a single gross figure.

        base + car allowance + bonus            taxable pay
          less pension (10% of base only)
          less holiday pay                      = what NI and PAYE are charged on
        plus home working allowance             paid on top, taxed nowhere

    The decomposition is what makes this match a payslip. A single 'salary' of 128,350.25
    hid a base of 118,905 and a car allowance of 9,445.25, and charging the pension against
    the combined figure would deduct 1,069.59 a month instead of 990.88.

    A month with a recorded payslip is modelled against its actual holiday pay rather than
    the standing assumption, since that is the one deduction that genuinely varies.
    """
    holiday = None
    if row is not None and row["holiday_pay"] is not None and not pd.isna(row["holiday_pay"]):
        holiday = as_decimal(row["holiday_pay"])
    components = repo.salary_components(period, profiles, bonuses, PARAMS, holiday)
    if components is None:
        return None
    return tax.pay_for(components, bands_for(period), repo.period_start(period))


rows = []
by_period = payslips.set_index("period") if not payslips.empty else None
bonus_by_period = bonuses.set_index("period") if not bonuses.empty else None

for period in data["all_periods"]:
    actual = (
        by_period.loc[period]
        if by_period is not None and period in by_period.index
        else None
    )
    bonus = (
        bonus_by_period.loc[period]
        if bonus_by_period is not None and period in bonus_by_period.index
        else None
    )
    expected = expected_for(period, actual)

    def paid(column: str) -> Decimal | None:
        """Salary plus bonus, or None where neither has been entered."""
        salary_part = actual[column] if actual is not None else None
        bonus_part = bonus[column] if bonus is not None else None
        if (salary_part is None or pd.isna(salary_part)) and (
            bonus_part is None or pd.isna(bonus_part)
        ):
            return None
        return as_decimal(salary_part) + as_decimal(bonus_part)

    parts = expected.components if expected else None
    rows.append(
        {
            "period": period,
            "month": repo.period_label(period),
            "payday": actual["payday"] if actual is not None else None,
            # The build-up, in the order a payslip reads.
            "base": parts.base if parts else None,
            "car": parts.car if parts else None,
            "home_working": parts.home_working if parts else None,
            "pension": parts.pension if parts else None,
            "expected_holiday": parts.holiday_pay if parts else None,
            "taxable": parts.taxable if parts else None,
            "actual_gross": paid("gross"),
            # Like for like: the payslip states gross net of the salary-sacrifice pension
            # and inclusive of the home working allowance, which base-plus-car is not.
            "expected_gross": parts.payslip_gross if parts else None,
            "bonus_gross": bonus["gross"] if bonus is not None else None,
            "holiday_pay": actual["holiday_pay"] if actual is not None else None,
            "benefits": actual["benefits"] if actual is not None else None,
            "additional": actual["additional"] if actual is not None else None,
            "actual_ni": paid("ni"),
            "expected_ni": expected.ni if expected else None,
            "actual_paye": paid("paye"),
            "expected_paye": expected.paye if expected else None,
            "actual_net": paid("net"),
            "expected_net": expected.net if expected else None,
        }
    )

frame = pd.DataFrame(rows)
paid_months = frame[frame["actual_net"].notna()]

# --------------------------------------------------------------------------- headline

cols = st.columns(4)
cols[0].metric("Payslips received", f"{len(paid_months)} of {len(frame)}")
cols[1].metric("Gross to date", ui.money(paid_months["actual_gross"].sum()))
cols[2].metric(
    "Tax and NI to date",
    ui.money(paid_months["actual_ni"].sum() + paid_months["actual_paye"].sum()),
)
cols[3].metric("Net to date", ui.money(paid_months["actual_net"].sum()))

if not paid_months.empty:
    difference = (paid_months["actual_net"] - paid_months["expected_net"]).sum()
    if abs(difference) < Decimal("1"):
        st.success(
            f"Actual net pay is within {ui.money(abs(difference))} of the model across "
            f"{len(paid_months)} payslip(s)."
        )
    else:
        st.warning(
            f"Actual net pay differs from the model by {ui.money(difference)} across "
            f"{len(paid_months)} payslip(s) — worth checking which month diverges below."
        )

st.divider()

tab_compare, tab_inputs, tab_bands, tab_spend = st.tabs(
    ["Actual against expected", "Salary and bonus", "Tax bands", "What's left to spend"]
)

# ------------------------------------------------------------- actual vs expected

with tab_compare:
    display = frame[
        ["month", "payday", "actual_gross", "expected_gross", "bonus_gross",
         "holiday_pay", "benefits", "additional", "actual_ni", "expected_ni",
         "actual_paye", "expected_paye", "actual_net", "expected_net"]
    ].copy()
    display["net difference"] = display["actual_net"] - display["expected_net"]

    money_columns = [
        "actual_gross", "expected_gross", "bonus_gross", "holiday_pay", "benefits",
        "additional", "actual_ni", "expected_ni", "actual_paye", "expected_paye",
        "actual_net", "expected_net", "net difference",
    ]
    st.dataframe(
        ui.money_table(
            display,
            money_columns,
            labels={
                "month": "Month", "payday": "Payday",
                "actual_gross": "Gross", "expected_gross": "Gross (expected)",
                "bonus_gross": "of which bonus",
                "holiday_pay": "Holiday pay", "benefits": "Benefits",
                "additional": "Additional pay",
                "actual_ni": "NI", "expected_ni": "NI (model)",
                "actual_paye": "PAYE", "expected_paye": "PAYE (model)",
                "actual_net": "Net", "expected_net": "Net (model)",
            },
        ),
        use_container_width=True,
        hide_index=True,
    )
    st.caption(
        "The actual columns are salary **plus** bonus, since the two are separate payments "
        "in the same month; 'of which bonus' shows how much of the gross came from the "
        "bonus. Enter each under **Salary and bonus** and here, respectively, so neither "
        "overwrites the other. Benefits and additional pay are what the old workbook "
        "recorded — the model now builds the same figures from their parts instead, which "
        "is the table below."
    )

    st.divider()
    st.subheader("How the expected figures are built")
    st.caption(
        "The Tax Calculator's A18:D25 and G17:H23. Pension is a percentage of **base only** "
        "— the car allowance is not pensionable, which is why the two cannot stay lumped "
        "together. The home working allowance sits outside the tax calculation entirely: it "
        "is paid on top and passes straight through to net. Set all three under "
        "**Settings → Salary**."
    )

    build_up = frame[
        ["month", "base", "car", "bonus_gross", "home_working", "pension",
         "expected_holiday", "taxable", "expected_ni", "expected_paye", "expected_net",
         "actual_net"]
    ].copy()
    build_up["difference"] = build_up["actual_net"] - build_up["expected_net"]

    build_up_money = [
        "base", "car", "bonus_gross", "home_working", "pension", "expected_holiday",
        "taxable", "expected_ni", "expected_paye", "expected_net", "actual_net",
        "difference",
    ]
    st.dataframe(
        ui.money_table(
            build_up,
            build_up_money,
            labels={
                "month": "Month",
                "base": "Base", "car": "Car allowance", "bonus_gross": "Bonus",
                "home_working": "Home working", "pension": "Pension",
                "expected_holiday": "Holiday pay", "taxable": "Taxable",
                "expected_ni": "NI", "expected_paye": "PAYE",
                "expected_net": "Net (model)", "actual_net": "Net (actual)",
                "difference": "Difference",
            },
        ),
        use_container_width=True,
        hide_index=True,
    )
    st.caption(
        "**Taxable** = base + car allowance + bonus − pension − holiday pay, and both NI and "
        "PAYE are charged on it. **Net (model)** = base + car allowance + bonus + home "
        "working − pension − holiday pay − NI − PAYE."
    )

    if not paid_months.empty:
        chart = ui.to_float(paid_months, ["actual_net", "expected_net"]).melt(
            id_vars="month", value_vars=["actual_net", "expected_net"],
            var_name="series", value_name="amount",
        )
        fig = px.bar(
            chart, x="month", y="amount", color="series", barmode="group",
            labels={"amount": "Net pay (£)", "month": "", "series": ""},
        )
        fig.update_layout(margin=dict(t=10))
        st.plotly_chart(ui.money_axis(fig), use_container_width=True)

    st.divider()
    st.subheader("Record a payslip")
    st.caption(
        "The salary payment only — a bonus paid separately goes under **Salary and bonus**, "
        "which keeps its own figures. The workbook had no way to enter a future month at "
        "all; benefits and additional pay drive the model, so they can be set for months "
        "that have not been paid yet."
    )

    entry_period = st.selectbox(
        "Month", data["all_periods"], index=len(data["periods"]) - 1,
        format_func=repo.period_label, key="payslip_period",
    )
    stored_payslip = (
        by_period.loc[entry_period]
        if by_period is not None and entry_period in by_period.index
        else None
    )

    def as_float(value) -> float:
        return float(value) if value is not None and not pd.isna(value) else 0.0

    def field(column: str) -> float:
        return as_float(stored_payslip[column]) if stored_payslip is not None else 0.0

    with st.form("payslip_entry"):
        row_one = st.columns(5)
        in_gross = row_one[0].number_input(
            "Gross pay", value=field("gross"), step=100.0, format="%.2f"
        )
        in_ni = row_one[1].number_input(
            "NI", value=field("ni"), step=10.0, format="%.2f"
        )
        in_paye = row_one[2].number_input(
            "PAYE", value=field("paye"), step=100.0, format="%.2f"
        )
        in_holiday = row_one[3].number_input(
            "Holiday pay", value=field("holiday_pay"), step=10.0, format="%.2f"
        )
        in_net = row_one[4].number_input(
            "Net pay", value=field("net"), step=100.0, format="%.2f"
        )

        row_two = st.columns(5)
        in_benefits = row_two[0].number_input(
            "Benefits", value=field("benefits"), step=10.0, format="%.2f",
            help="Salary sacrifice — reduces taxable pay and net pay alike",
        )
        in_additional = row_two[1].number_input(
            "Additional pay", value=field("additional"), step=10.0,
            format="%.2f", help="Added after tax",
        )
        in_payday = row_two[2].number_input(
            "Payday",
            value=(
                int(stored_payslip["payday"])
                if stored_payslip is not None and pd.notna(stored_payslip["payday"])
                else 1
            ),
            min_value=1, max_value=31, step=1, format="%d",
        )
        row_two[3].write("")
        row_two[4].write("")

        buttons = st.columns([1, 1, 3])
        save_payslip = buttons[0].form_submit_button(
            "Save payslip", type="primary", disabled=READ_ONLY
        )
        remove_payslip = buttons[1].form_submit_button(
            "Remove", disabled=READ_ONLY or stored_payslip is None,
            help="Deletes the month's payslip outright, for when the wrong month was filled "
                 "in. Re-enterable from the paper copy.",
        )

        if save_payslip:
            # A zero and a blank mean different things: zero holiday pay is a fact, an
            # untouched field is not. Nothing is stored for a value left at zero.
            def value_or_none(value):
                return Decimal(str(value)) if value else None

            with ui.session() as session, session.begin():
                outcome = reference.set_payslip(
                    session, entry_period,
                    gross=value_or_none(in_gross),
                    ni=value_or_none(in_ni),
                    paye=value_or_none(in_paye),
                    holiday_pay=value_or_none(in_holiday),
                    net=value_or_none(in_net),
                    benefits=value_or_none(in_benefits),
                    additional=value_or_none(in_additional),
                    payday=in_payday,
                )
            ui.show_outcome(outcome, "the payslip")

        if remove_payslip:
            with ui.session() as session, session.begin():
                outcome = reference.remove_payslip(session, entry_period)
            ui.show_outcome(outcome, "the payslip removal")

        consistency = (
            Decimal(str(in_ni)) + Decimal(str(in_holiday)) + Decimal(str(in_paye))
            + Decimal(str(in_net))
        )
        if in_gross and abs(consistency - Decimal(str(in_gross))) > Decimal("1"):
            st.caption(
                f"NI + holiday pay + PAYE + net comes to {ui.money(consistency)} against a "
                f"gross of {ui.money(in_gross)} — the workbook's column N check."
            )

# ---------------------------------------------------------------- salary and bonus

with tab_inputs:
    left, right = st.columns(2)

    with left:
        st.subheader("Base salary")
        st.caption(
            "One row per change, and **base only** — the car allowance is derived from it, "
            "so a pay rise moves both. The workbook repeated a single combined figure down "
            "all twelve months, which is why 128,350.25 could never be found on a payslip: "
            "it was a base of 118,905 with the allowance already added in."
        )
        if profiles.empty:
            st.info("No salary recorded.")
        else:
            shown = profiles.copy()
            shown["car"] = shown["base_salary"].map(
                lambda b: None if b is None or pd.isna(b)
                else repo.car_allowance(Decimal(b), PARAMS)
            )
            shown["total"] = shown["base_salary"].fillna(Decimal("0")) + shown["car"].fillna(
                Decimal("0")
            )
            st.dataframe(
                ui.money_table(
                    shown[["effective_from", "base_salary", "car", "total", "note"]],
                    ["base_salary", "car", "total"],
                    labels={"effective_from": "From", "base_salary": "Base",
                            "car": "Car allowance", "total": "Total", "note": "Note"},
                ),
                use_container_width=True,
                hide_index=True,
            )

        with st.form("salary_profile"):
            fields = st.columns([1, 1, 2])
            from_date = fields[0].date_input(
                "From", value=dt.date.today().replace(day=1), format="DD/MM/YYYY"
            )
            last_base = (
                float(profiles["base_salary"].iloc[-1])
                if not profiles.empty and pd.notna(profiles["base_salary"].iloc[-1])
                else 0.0
            )
            annual = fields[1].number_input(
                "Base salary", min_value=0.0, step=1000.0, format="%.2f", value=last_base,
                help="The car allowance is calculated from this, not entered.",
            )
            note = fields[2].text_input("Note", placeholder="e.g. April pay review")
            if annual:
                derived = repo.car_allowance(Decimal(str(annual)), PARAMS)
                st.caption(
                    f"Car allowance would be {ui.money(derived)} a year "
                    f"({ui.money(derived / 12)} a month), giving a total of "
                    f"{ui.money(Decimal(str(annual)) + derived)}."
                )
            if st.form_submit_button("Save salary", type="primary", disabled=READ_ONLY):
                with ui.session() as session, session.begin():
                    outcome = reference.set_salary_profile(
                        session, from_date, Decimal(str(annual)), note or None, PARAMS
                    )
                ui.show_outcome(outcome, "the salary change")

        if not profiles.empty:
            to_remove = st.selectbox(
                "Remove a salary record",
                options=list(profiles["id"]),
                format_func=lambda i: (
                    f"{profiles.set_index('id').loc[i, 'effective_from']:%d %b %Y} — "
                    f"{ui.money(profiles.set_index('id').loc[i, 'base_salary'])}"
                ),
                key="remove_salary",
            )
            if st.button("Remove", disabled=READ_ONLY, key="do_remove_salary"):
                with ui.session() as session, session.begin():
                    outcome = reference.remove_salary_profile(session, int(to_remove))
                ui.show_outcome(outcome, "the salary change")

    with right:
        st.subheader("Bonus")
        st.caption(
            "By the month it is paid, with its own deductions — a bonus usually arrives on "
            "its own day, so recording it against the payslip would mean the second payment "
            "of a month overwriting the first. May's was `+29028.48` typed into the middle "
            "of the expected-gross formula, which is why that figure could not be derived."
        )
        if bonuses.empty:
            st.info("No bonuses recorded.")
        else:
            shown = bonuses.copy()
            shown["month"] = shown["period"].map(repo.period_label)
            bonus_money = ["amount", "gross", "ni", "paye", "net"]
            st.dataframe(
                ui.money_table(
                    shown[["month"] + bonus_money + ["payday", "note"]],
                    bonus_money,
                    labels={"month": "Month", "amount": "Expected", "gross": "Gross",
                            "ni": "NI", "paye": "PAYE", "net": "Net",
                            "payday": "Payday", "note": "Note"},
                ),
                use_container_width=True,
                hide_index=True,
            )

        bonus_period = st.selectbox(
            "Month", data["all_periods"], format_func=repo.period_label,
            index=len(data["periods"]) - 1, key="bonus_period",
        )
        stored_bonus = (
            bonus_by_period.loc[bonus_period]
            if bonus_by_period is not None and bonus_period in bonus_by_period.index
            else None
        )

        def bonus_field(column: str) -> float:
            if stored_bonus is None or pd.isna(stored_bonus[column]):
                return 0.0
            return float(stored_bonus[column])

        with st.form("bonus_entry"):
            top = st.columns([1, 2])
            bonus_amount = top[0].number_input(
                "Expected amount", min_value=0.0, step=500.0, format="%.2f",
                value=bonus_field("amount"),
                help="Feeds expected gross for the month. Saving zero removes the bonus.",
            )
            bonus_note = top[1].text_input(
                "Note", value="" if stored_bonus is None else (stored_bonus["note"] or ""),
                key="bonus_note",
            )

            actuals = st.columns(5)
            bonus_gross = actuals[0].number_input(
                "Gross pay", min_value=0.0, step=500.0, format="%.2f",
                value=bonus_field("gross"),
            )
            bonus_ni = actuals[1].number_input(
                "NI", min_value=0.0, step=10.0, format="%.2f", value=bonus_field("ni")
            )
            bonus_paye = actuals[2].number_input(
                "PAYE", min_value=0.0, step=100.0, format="%.2f", value=bonus_field("paye")
            )
            bonus_net = actuals[3].number_input(
                "Net pay", min_value=0.0, step=100.0, format="%.2f", value=bonus_field("net")
            )
            bonus_payday = actuals[4].number_input(
                "Payday", min_value=1, max_value=31, step=1, format="%d",
                value=(
                    int(stored_bonus["payday"])
                    if stored_bonus is not None and pd.notna(stored_bonus["payday"])
                    else 1
                ),
            )

            bonus_buttons = st.columns([1, 1, 3])
            save_bonus = bonus_buttons[0].form_submit_button(
                "Save bonus", type="primary", disabled=READ_ONLY
            )
            drop_bonus = bonus_buttons[1].form_submit_button(
                "Remove", disabled=READ_ONLY or stored_bonus is None
            )

            if save_bonus:
                def or_none(value):
                    return Decimal(str(value)) if value else None

                with ui.session() as session, session.begin():
                    outcome = reference.set_bonus(
                        session, bonus_period, Decimal(str(bonus_amount)),
                        bonus_note or None,
                        gross=or_none(bonus_gross),
                        ni=or_none(bonus_ni),
                        paye=or_none(bonus_paye),
                        net=or_none(bonus_net),
                        payday=bonus_payday,
                    )
                ui.show_outcome(outcome, "the bonus")

            if drop_bonus:
                with ui.session() as session, session.begin():
                    outcome = reference.remove_bonus(session, bonus_period)
                ui.show_outcome(outcome, "the bonus removal")

        st.caption(
            "The expected amount drives 'Gross (expected)'. The four figures beside it are "
            "what was actually paid, and are added to the payslip's on the comparison tab."
        )

    st.divider()
    st.subheader("Resulting expected gross")
    derived = pd.DataFrame(
        [
            {
                "month": repo.period_label(period),
                "base": repo.base_in_force(profiles, repo.period_start(period)),
                "car": (
                    repo.car_allowance(base, PARAMS)
                    if (base := repo.base_in_force(profiles, repo.period_start(period)))
                    is not None
                    else None
                ),
                "salary in force": repo.salary_in_force(
                    profiles, repo.period_start(period), PARAMS
                ),
                "bonus": repo.bonus_for(period, bonuses) or None,
                "expected gross": repo.expected_gross(period, profiles, bonuses, PARAMS),
            }
            for period in data["all_periods"]
        ]
    )
    st.dataframe(
        ui.money_table(
            derived, ["base", "car", "salary in force", "bonus", "expected gross"],
            labels={"month": "Month", "base": "Base (annual)",
                    "car": "Car allowance (annual)", "salary in force": "Total (annual)",
                    "expected gross": "Expected gross (monthly)"},
        ),
        use_container_width=True,
        hide_index=True,
    )
    st.caption(
        "Expected gross is the annual total over twelve plus any bonus. It excludes the "
        "home working allowance, which is paid on top rather than being part of salary."
    )

# ------------------------------------------------------------------------- tax bands

RATE_KEYS = repo.RATE_KEYS
AMOUNT_LABELS = {
    "ni_lower_earnings_limit": "NI lower earnings limit",
    "ni_upper_earnings_limit": "NI upper earnings limit",
    "personal_allowance": "Personal allowance",
    "basic_rate_threshold": "Basic rate threshold",
    "higher_threshold": "Higher rate threshold",
}
RATE_LABELS = {
    "ni_lower_rate": "NI lower rate (%)",
    "ni_higher_rate": "NI higher rate (%)",
    "basic_rate": "Basic rate (%)",
    "higher_rate": "Higher rate (%)",
    "additional_rate": "Additional rate (%)",
}
ADJUSTMENT_KEY = repo.ADJUSTMENT_KEY

with tab_bands:
    pickers = st.columns([1, 1, 2])
    tax_years = data["tax_years"]
    chosen_year = pickers[0].selectbox(
        "Tax year",
        options=tax_years,
        index=len(tax_years) - 1,
        format_func=lambda y: f"{y}/{str(y + 1)[-2:]}",
        help="Bands and rates are held per tax year, so a historic year keeps the figures "
             "that applied to it.",
    )

    assumptions = assumptions_by_year.get(
        chosen_year,
        pd.DataFrame(columns=["key", "effective_from", "value", "tax_year"]),
    )

    # Every threshold and rate is effective-dated, so a mid-year change is a new set rather
    # than an edit that quietly rewrites what earlier months were taxed at. The editors below
    # show the set in force on this date and save against it.
    stored_dates = repo.assumption_dates(assumptions) or [dt.date(chosen_year, 4, 1)]
    effective = pickers[1].date_input(
        "Effective from",
        value=stored_dates[-1],
        format="DD/MM/YYYY",
        key=f"bands_effective_{chosen_year}",
        help="Saving against a date that has no set yet creates one, leaving the earlier "
             "figures intact for the months they applied to.",
    )
    pickers[2].caption("")
    pickers[2].caption(
        f"{len(stored_dates)} set(s) stored for {chosen_year}/{str(chosen_year + 1)[-2:]}: "
        + ", ".join(f"{d:%d/%m/%Y}" for d in stored_dates)
    )

    dated = assumptions.copy()
    if not dated.empty:
        dated["effective_from"] = pd.to_datetime(dated["effective_from"]).dt.date


    def in_force(key: str, on: dt.date):
        """The stored value for a key as at a date, or None if nothing applies yet."""
        if dated.empty:
            return None
        mine = dated[(dated["key"] == key) & (dated["effective_from"] <= on)]
        if mine.empty:
            return None
        return mine.sort_values("effective_from")["value"].iloc[-1]


    def value_set(on: dt.date, labels: dict[str, str]) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {"key": key, "band": label, "value": in_force(key, on)}
                for key, label in labels.items()
            ]
        )


    # ---- thresholds, monthly and annual ------------------------------------------
    st.subheader("Thresholds and allowances")
    st.caption(
        "Enter either figure and the other follows: a monthly value is multiplied by "
        "twelve, an annual value divided by it. Change both at once and the annual value "
        "wins, with the monthly recalculated from it."
    )

    threshold_source = value_set(effective, AMOUNT_LABELS)
    thresholds = ui.to_float(
        threshold_source.assign(
            monthly=threshold_source["value"],
            annual=threshold_source["value"].map(
                lambda v: None if v is None or pd.isna(v) else v * 12
            ),
        ).drop(columns="value"),
        ["monthly", "annual"],
    )

    baseline_key = f"bands_baseline_{chosen_year}_{effective}"
    if st.session_state.get(baseline_key) is None:
        st.session_state[baseline_key] = thresholds[["monthly", "annual"]].copy()

    edited = st.data_editor(
        thresholds,
        use_container_width=True,
        hide_index=True,
        disabled=["key", "band"] if not READ_ONLY else True,
        column_order=["band", "monthly", "annual"],
        column_config={
            "band": "Band",
            "monthly": ui.editable_money("Monthly"),
            "annual": ui.editable_money("Annual"),
        },
        key=f"bands_editor_{chosen_year}_{effective}",
    )

    if st.button("Save thresholds", type="primary", disabled=READ_ONLY):
        baseline = st.session_state[baseline_key]
        saved = 0
        outcome = reference.Outcome(True, "Nothing changed.")
        with ui.session() as session, session.begin():
            for index, row in edited.iterrows():
                monthly = repo.reconcile_monthly_annual(
                    row["monthly"], row["annual"],
                    baseline.loc[index, "monthly"], baseline.loc[index, "annual"],
                )
                if monthly is None:
                    continue

                outcome = reference.set_assumption(
                    session, chosen_year, row["key"],
                    monthly.quantize(PENCE, rounding=ROUND_HALF_UP), effective,
                )
                saved += 1
        if saved:
            st.session_state[baseline_key] = None
            ui.show_outcome(outcome, f"{saved} band change(s)")
            st.rerun()
        else:
            st.info("Nothing changed.")

    # ---- rates --------------------------------------------------------------------
    st.subheader("Rates")
    st.caption(
        "Whole percentages, as they are quoted: 20.00 rather than 0.20. Stored to a "
        "hundredth of a point, so an 8.50% rate is expressible — as a fraction in a "
        "two-decimal column it would not have been."
    )

    rates_frame = ui.to_float(
        value_set(effective, RATE_LABELS).rename(columns={"value": "rate"}), ["rate"]
    )

    edited_rates = st.data_editor(
        rates_frame,
        use_container_width=True,
        hide_index=True,
        disabled=["key", "band"] if not READ_ONLY else True,
        column_order=["band", "rate"],
        column_config={
            "band": "Rate",
            "rate": ui.editable_percent("Value"),
        },
        key=f"rates_editor_{chosen_year}_{effective}",
    )

    if st.button("Save rates", type="primary", disabled=READ_ONLY):
        outcome = reference.Outcome(True, "Nothing changed.")
        with ui.session() as session, session.begin():
            for _, row in edited_rates.iterrows():
                if row["rate"] is None or pd.isna(row["rate"]):
                    continue
                outcome = reference.set_assumption(
                    session, chosen_year, row["key"],
                    Decimal(str(row["rate"])).quantize(PENCE), effective,
                )
        ui.show_outcome(outcome, "the rate change")
        st.rerun()

    # ---- personal allowance adjustment --------------------------------------------
    st.subheader("Personal allowance adjustment")
    st.caption(
        "The allowance is revised in steps across the year as HMRC reissues the tax code. "
        "The step in force on a month's first day is the one applied — the workbook's "
        "`MATCH(..., $F$24:$F$27, 1)`. Adjustments are negative: they reduce the allowance, "
        "which increases the amount taxed."
    )

    adjustments = assumptions[assumptions["key"] == ADJUSTMENT_KEY].copy()
    adjustments = adjustments.sort_values("effective_from")
    adjustment_frame = pd.DataFrame(
        {
            "effective_from": adjustments["effective_from"],
            "monthly": adjustments["value"].astype(float),
            "annual": (adjustments["value"] * 12).astype(float),
        }
    ).reset_index(drop=True)

    edited_adjustments = st.data_editor(
        adjustment_frame,
        use_container_width=True,
        hide_index=True,
        num_rows="fixed" if READ_ONLY else "dynamic",
        column_config={
            "effective_from": st.column_config.DateColumn(
                "Effective from", format="DD/MM/YYYY"
            ),
            "monthly": ui.editable_money("Monthly"),
            "annual": ui.editable_money("Annual"),
        },
        key=f"adjustments_editor_{chosen_year}",
    )

    if st.button("Save adjustments", type="primary", disabled=READ_ONLY):
        keep: set[dt.date] = set()
        outcome = reference.Outcome(True, "Nothing changed.")
        with ui.session() as session, session.begin():
            for _, row in edited_adjustments.iterrows():
                when = row["effective_from"]
                if when is None or pd.isna(when):
                    continue
                when = pd.to_datetime(when).date()
                monthly = row["monthly"]
                annual = row["annual"]
                # A new row is added with one of the two filled in, so whichever is present
                # is the one meant; annual wins if both are.
                if annual is not None and not pd.isna(annual) and annual:
                    value = Decimal(str(annual)) / 12
                elif monthly is not None and not pd.isna(monthly):
                    value = Decimal(str(monthly))
                else:
                    continue
                keep.add(when)
                outcome = reference.set_assumption(
                    session, chosen_year, ADJUSTMENT_KEY,
                    value.quantize(PENCE, rounding=ROUND_HALF_UP), when,
                )
            for when in set(adjustments["effective_from"]) - keep:
                reference.remove_assumption(session, chosen_year, ADJUSTMENT_KEY, when)
        ui.show_outcome(outcome, "the allowance steps")
        st.rerun()

    current_bands = repo.bands_from(assumptions, effective)
    st.info(
        f"**Basic rate band: {ui.money(current_bands.basic_band)} a month.** Derived, not "
        "entered — the basic rate threshold less the personal allowance, which is what the "
        "workbook's `=D28-D22` did. Editing either input moves it."
    )

    # ---- historic sets ------------------------------------------------------------
    st.divider()
    st.subheader("Historic thresholds and rates")
    st.caption(
        "The set that applied on a chosen date, read-only. Each figure is stored against the "
        "date it took effect, so a past month keeps the bands it was actually taxed under "
        "rather than the ones in force now."
    )

    as_at = st.selectbox(
        "As at",
        options=list(reversed(stored_dates)),
        format_func=lambda d: d.strftime("%d %B %Y"),
        key=f"historic_as_at_{chosen_year}",
    )

    hist_left, hist_right = st.columns(2)
    with hist_left:
        st.markdown("**Thresholds and allowances**")
        historic_amounts = value_set(as_at, AMOUNT_LABELS)
        historic_amounts["annual"] = historic_amounts["value"].map(
            lambda v: None if v is None or pd.isna(v) else v * 12
        )
        st.dataframe(
            ui.money_table(
                historic_amounts[["band", "value", "annual"]], ["value", "annual"],
                labels={"band": "Band", "value": "Monthly", "annual": "Annual"},
            ),
            use_container_width=True,
            hide_index=True,
        )
    with hist_right:
        st.markdown("**Rates**")
        historic_rates = ui.to_float(value_set(as_at, RATE_LABELS), ["value"])
        st.dataframe(
            historic_rates[["band", "value"]].style.format({"value": "{:,.2f}"}),
            use_container_width=True,
            hide_index=True,
            column_config={"band": "Rate", "value": "Value"},
        )

    with st.expander("Every stored figure, by effective date"):
        if assumptions.empty:
            st.caption("Nothing stored for this tax year.")
        else:
            everything = assumptions.copy()
            everything["band"] = everything["key"].map(
                {**AMOUNT_LABELS, **RATE_LABELS,
                 ADJUSTMENT_KEY: "Personal allowance adjustment"}
            ).fillna(everything["key"])
            everything["kind"] = everything["key"].map(
                lambda k: "Rate (%)" if k in RATE_KEYS else "Amount (monthly)"
            )
            st.dataframe(
                repo.sort_human(
                    everything[["band", "kind", "effective_from", "value"]],
                    by=["band", "effective_from"],
                ).style.format({"value": "{:,.2f}"}),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "band": "Band", "kind": "Kind",
                    "effective_from": st.column_config.DateColumn(
                        "Effective from", format="DD/MM/YYYY"
                    ),
                    "value": "Value",
                },
            )

# ------------------------------------------------------------ what's left to spend

with tab_spend:
    st.subheader("What's left to spend")
    st.caption(
        "Salary tracker H17:I28. The latest net salary, less what is already committed, "
        "divided by thirty. The divisor is thirty whatever the month's length — this is a "
        "spending allowance, not an apportionment."
    )

    settings = data["settings"]
    latest = paid_months.iloc[-1] if not paid_months.empty else None
    net_salary = (
        Decimal(str(latest["actual_net"])) if latest is not None else Decimal("0")
    )
    spend_period = st.selectbox(
        "Costs from", data["periods"], index=len(data["periods"]) - 1,
        format_func=repo.period_label, key="spend_period",
        help="Bills and other costs come from this month's expected costs.",
    )

    inputs = st.columns(4)
    rent = inputs[0].number_input(
        "Rent (£)", value=float(settings.get("spend_rent", 0)), step=50.0, format="%.2f"
    )
    savings_allowance = inputs[1].number_input(
        "Savings (£)", value=float(settings.get("spend_savings", 0)), step=100.0,
        format="%.2f", help="Added to the budgeted credit-card repayment",
    )
    food = inputs[2].number_input(
        "Food (£)", value=float(settings.get("spend_food", 0)), step=50.0, format="%.2f"
    )
    essentials = inputs[3].number_input(
        "Essentials (£)", value=float(settings.get("spend_essentials", 0)), step=10.0,
        format="%.2f"
    )

    if st.button("Save these as the defaults", disabled=READ_ONLY):
        with ui.session() as session, session.begin():
            for key, value in (
                ("spend_rent", rent), ("spend_savings", savings_allowance),
                ("spend_food", food), ("spend_essentials", essentials),
            ):
                outcome = reference.set_setting(session, key, value)
        ui.show_outcome(outcome, "the spending inputs")

    calculation = repo.spending_calculation(
        data["budgets"], data["categories"], net_salary, spend_period,
        rent=Decimal(str(rent)), savings=Decimal(str(savings_allowance)),
        food=Decimal(str(food)), essentials=Decimal(str(essentials)),
    )

    if latest is None:
        st.info("No payslip recorded yet, so there is no net salary to work from.")
    else:
        st.caption(
            f"Net salary from {latest['month']}: {ui.money(net_salary)}."
        )

    breakdown = pd.DataFrame(
        [
            {"line": "Net salary", "amount": calculation["net_salary"], "sign": "+"},
            {"line": "Rent", "amount": calculation["rent"], "sign": "+"},
            {"line": "Bills", "amount": calculation["bills"], "sign": "−"},
            {"line": "Other costs", "amount": calculation["other"], "sign": "−"},
            {"line": "Savings", "amount": calculation["savings"], "sign": "−"},
            {"line": "Food", "amount": calculation["food"], "sign": "−"},
            {"line": "Essentials", "amount": calculation["essentials"], "sign": "−"},
        ]
    )
    st.dataframe(
        ui.money_table(breakdown, ["amount"],
                       labels={"line": "", "sign": " ", "amount": "Amount"}),
        use_container_width=True,
        hide_index=True,
    )

    totals = st.columns(3)
    totals[0].metric(
        "Card limit", ui.money(calculation["card_limit"]),
        help="Net salary and rent, less bills, other costs and savings",
    )
    totals[1].metric("Left this month", ui.money(calculation["monthly"]))
    totals[2].metric("Left per day", ui.money(calculation["daily"]))

    st.caption(
        f"Savings is the {ui.money(calculation['savings_input'])} allowance plus the "
        f"{ui.money(calculation['card_repayment'])} budgeted for credit cards. That line is "
        "deliberately taken out of other costs and counted here instead — clearing a card "
        "balance is saving, not spending."
    )
