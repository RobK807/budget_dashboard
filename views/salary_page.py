"""Salary -- replaces the Salary tracker tab.

The expected-pay columns are reproduced by budget/tax.py, a pure function over the stored
bands. It matches the workbook to the penny for all twelve months, including the bonus month
and the four tapered personal-allowance steps.

Everything the model reads is editable here. In the workbook the annual salary was typed into
all twelve rows of column O, the bonus was welded into May's formula as `+29028.48`, and the
bands sat in a block of cells with no way in but the cell itself.
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
bands = data["bands"]
READ_ONLY = ui.read_only()

if READ_ONLY:
    st.warning(
        "**Read-only while checked out for offline use.** Check in on the Sync page first."
    )

PENCE = Decimal("0.01")


def expected_for(period: str, row) -> tax.Breakdown | None:
    """Salary tracker columns S, U and W for one month.

    Gross is derived from the salary in force plus any bonus, rather than stored: with both
    held as data the derivation works again, which it could not while May's bonus lived
    inside the formula.
    """
    gross = repo.expected_gross(period, profiles, bonuses)
    if gross is None:
        return None
    benefits = Decimal(row["benefits"] or 0) if row is not None else Decimal("0")
    additional = Decimal(row["additional"] or 0) if row is not None else Decimal("0")
    return tax.expected_pay(gross, bands, repo.period_start(period), benefits, additional)


rows = []
by_period = payslips.set_index("period") if not payslips.empty else None
for period in data["all_periods"]:
    actual = (
        by_period.loc[period]
        if by_period is not None and period in by_period.index
        else None
    )
    expected = expected_for(period, actual)
    rows.append(
        {
            "period": period,
            "month": repo.period_label(period),
            "payday": actual["payday"] if actual is not None else None,
            "actual_gross": actual["gross"] if actual is not None else None,
            "expected_gross": expected.gross if expected else None,
            "holiday_pay": actual["holiday_pay"] if actual is not None else None,
            "benefits": actual["benefits"] if actual is not None else None,
            "additional": actual["additional"] if actual is not None else None,
            "actual_ni": actual["ni"] if actual is not None else None,
            "expected_ni": expected.ni if expected else None,
            "actual_paye": actual["paye"] if actual is not None else None,
            "expected_paye": expected.paye if expected else None,
            "actual_net": actual["net"] if actual is not None else None,
            "expected_net": expected.net if expected else None,
        }
    )

frame = pd.DataFrame(rows)
paid = frame[frame["actual_net"].notna()]

# --------------------------------------------------------------------------- headline

cols = st.columns(4)
cols[0].metric("Payslips received", f"{len(paid)} of {len(frame)}")
cols[1].metric("Gross to date", ui.money(paid["actual_gross"].sum()))
cols[2].metric(
    "Tax and NI to date", ui.money(paid["actual_ni"].sum() + paid["actual_paye"].sum())
)
cols[3].metric("Net to date", ui.money(paid["actual_net"].sum()))

if not paid.empty:
    difference = (paid["actual_net"] - paid["expected_net"]).sum()
    if abs(difference) < Decimal("1"):
        st.success(
            f"Actual net pay is within {ui.money(abs(difference))} of the model across "
            f"{len(paid)} payslip(s)."
        )
    else:
        st.warning(
            f"Actual net pay differs from the model by {ui.money(difference)} across "
            f"{len(paid)} payslip(s) — worth checking which month diverges below."
        )

st.divider()

tab_compare, tab_inputs, tab_bands, tab_spend = st.tabs(
    ["Actual against expected", "Salary and bonus", "Tax bands", "What's left to spend"]
)

# ------------------------------------------------------------- actual vs expected

with tab_compare:
    display = frame[
        ["month", "payday", "actual_gross", "expected_gross", "holiday_pay", "benefits",
         "additional", "actual_ni", "expected_ni", "actual_paye", "expected_paye",
         "actual_net", "expected_net"]
    ].copy()
    display["net difference"] = display["actual_net"] - display["expected_net"]

    money_columns = [
        "actual_gross", "expected_gross", "holiday_pay", "benefits", "additional",
        "actual_ni", "expected_ni", "actual_paye", "expected_paye", "actual_net",
        "expected_net", "net difference",
    ]
    st.dataframe(
        ui.money_table(
            display,
            money_columns,
            labels={
                "month": "Month", "payday": "Payday",
                "actual_gross": "Gross", "expected_gross": "Gross (expected)",
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
        "Benefits are salary sacrifice: deducted before tax and again from net, so the "
        "amount never reaches the payslip. Additional pay is added after tax. Both feed the "
        "model — holiday pay does not, because it is already inside actual gross."
    )

    if not paid.empty:
        chart = ui.to_float(paid, ["actual_net", "expected_net"]).melt(
            id_vars="month", value_vars=["actual_net", "expected_net"],
            var_name="series", value_name="amount",
        )
        fig = px.bar(
            chart, x="month", y="amount", color="series", barmode="group",
            labels={"amount": "Net pay (£)", "month": "", "series": ""},
        )
        fig.update_layout(margin=dict(t=10))
        st.plotly_chart(fig, use_container_width=True)

    st.divider()
    st.subheader("Record a payslip")
    st.caption(
        "The workbook had no way to enter a future month's figures — the actual columns "
        "were only ever typed over. Benefits and additional pay drive the model, so they "
        "can be set for months that have not been paid yet."
    )

    entry_period = st.selectbox(
        "Month", data["all_periods"], index=len(data["periods"]) - 1,
        format_func=repo.period_label, key="payslip_period",
    )
    current = frame[frame["period"] == entry_period].iloc[0]

    def as_float(value) -> float:
        return float(value) if value is not None and not pd.isna(value) else 0.0

    with st.form("payslip_entry"):
        row_one = st.columns(5)
        in_gross = row_one[0].number_input(
            "Gross pay", value=as_float(current["actual_gross"]), step=100.0, format="%.2f"
        )
        in_ni = row_one[1].number_input(
            "NI", value=as_float(current["actual_ni"]), step=10.0, format="%.2f"
        )
        in_paye = row_one[2].number_input(
            "PAYE", value=as_float(current["actual_paye"]), step=100.0, format="%.2f"
        )
        in_holiday = row_one[3].number_input(
            "Holiday pay", value=as_float(current["holiday_pay"]), step=10.0, format="%.2f"
        )
        in_net = row_one[4].number_input(
            "Net pay", value=as_float(current["actual_net"]), step=100.0, format="%.2f"
        )

        row_two = st.columns(5)
        in_benefits = row_two[0].number_input(
            "Benefits", value=as_float(current["benefits"]), step=10.0, format="%.2f",
            help="Salary sacrifice — reduces taxable pay and net pay alike",
        )
        in_additional = row_two[1].number_input(
            "Additional pay", value=as_float(current["additional"]), step=10.0,
            format="%.2f", help="Added after tax",
        )
        in_payday = row_two[2].number_input(
            "Payday",
            value=int(current["payday"]) if pd.notna(current["payday"]) else 1,
            min_value=1, max_value=31, step=1, format="%d",
        )
        row_two[3].write("")
        row_two[4].write("")

        if st.form_submit_button("Save payslip", type="primary", disabled=READ_ONLY):
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
        st.subheader("Gross salary")
        st.caption(
            "One row per change. The workbook repeated the figure down all twelve months, "
            "so a pay rise meant editing each one from that point and hoping none were "
            "missed."
        )
        if profiles.empty:
            st.info("No salary recorded.")
        else:
            st.dataframe(
                ui.money_table(
                    profiles[["effective_from", "annual_salary", "note"]],
                    ["annual_salary"],
                    labels={"effective_from": "From", "annual_salary": "Annual salary",
                            "note": "Note"},
                ),
                use_container_width=True,
                hide_index=True,
            )

        with st.form("salary_profile"):
            fields = st.columns([1, 1, 2])
            from_date = fields[0].date_input(
                "From", value=dt.date.today().replace(day=1), format="DD/MM/YYYY"
            )
            annual = fields[1].number_input(
                "Annual salary", min_value=0.0, step=1000.0, format="%.2f",
                value=float(profiles["annual_salary"].iloc[-1]) if not profiles.empty else 0.0,
            )
            note = fields[2].text_input("Note", placeholder="e.g. April pay review")
            if st.form_submit_button("Save salary", type="primary", disabled=READ_ONLY):
                with ui.session() as session, session.begin():
                    outcome = reference.set_salary_profile(
                        session, from_date, Decimal(str(annual)), note or None
                    )
                ui.show_outcome(outcome, "the salary change")

        if not profiles.empty:
            to_remove = st.selectbox(
                "Remove a salary record",
                options=list(profiles["id"]),
                format_func=lambda i: (
                    f"{profiles.set_index('id').loc[i, 'effective_from']:%d %b %Y} — "
                    f"{ui.money(profiles.set_index('id').loc[i, 'annual_salary'])}"
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
            "By the month it is paid. May's was `+29028.48` typed into the middle of the "
            "expected-gross formula, which is why that figure could not be derived."
        )
        if bonuses.empty:
            st.info("No bonuses recorded.")
        else:
            shown = bonuses.copy()
            shown["month"] = shown["period"].map(repo.period_label)
            st.dataframe(
                ui.money_table(
                    shown[["month", "amount", "note"]], ["amount"],
                    labels={"month": "Month", "amount": "Amount", "note": "Note"},
                ),
                use_container_width=True,
                hide_index=True,
            )

        with st.form("bonus_entry"):
            fields = st.columns([1, 1, 2])
            bonus_period = fields[0].selectbox(
                "Month", data["all_periods"], format_func=repo.period_label,
                key="bonus_period",
            )
            bonus_amount = fields[1].number_input(
                "Amount", min_value=0.0, step=500.0, format="%.2f"
            )
            bonus_note = fields[2].text_input("Note", key="bonus_note")
            if st.form_submit_button("Save bonus", type="primary", disabled=READ_ONLY):
                with ui.session() as session, session.begin():
                    outcome = reference.set_bonus(
                        session, bonus_period, Decimal(str(bonus_amount)),
                        bonus_note or None,
                    )
                ui.show_outcome(outcome, "the bonus")
            st.caption("Saving zero removes the bonus for that month.")

    st.divider()
    st.subheader("Resulting expected gross")
    derived = pd.DataFrame(
        [
            {
                "month": repo.period_label(period),
                "salary in force": repo.salary_in_force(
                    profiles, repo.period_start(period)
                ),
                "bonus": (
                    bonuses.set_index("period")["amount"].get(period)
                    if not bonuses.empty
                    else None
                ),
                "expected gross": repo.expected_gross(period, profiles, bonuses),
            }
            for period in data["all_periods"]
        ]
    )
    st.dataframe(
        ui.money_table(derived, ["salary in force", "bonus", "expected gross"],
                       labels={"month": "Month"}),
        use_container_width=True,
        hide_index=True,
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
ADJUSTMENT_KEY = "personal_allowance_adjustment"

with tab_bands:
    tax_years = data["tax_years"]
    chosen_year = st.selectbox(
        "Tax year",
        options=tax_years,
        index=len(tax_years) - 1,
        format_func=lambda y: f"{y}/{str(y + 1)[-2:]}",
        help="Bands and rates are held per tax year, so a historic year keeps the figures "
             "that applied to it.",
    )

    if chosen_year != data["tax_year"]:
        with ui.session() as session:
            assumptions = repo.load_salary_assumptions(session, chosen_year)
    else:
        assumptions = data["assumptions"]

    stored = {
        (row["key"], row["effective_from"]): row["value"]
        for _, row in assumptions.iterrows()
    }
    year_start = dt.date(chosen_year, 4, 1)

    # ---- thresholds, monthly and annual ------------------------------------------
    st.subheader("Thresholds and allowances")
    st.caption(
        "Enter either figure and the other follows: a monthly value is multiplied by "
        "twelve, an annual value divided by it. Change both at once and the annual value "
        "wins, with the monthly recalculated from it."
    )

    threshold_rows = [
        {
            "key": key,
            "band": label,
            "monthly": stored.get((key, year_start)),
            "annual": (
                (stored[(key, year_start)] * 12) if (key, year_start) in stored else None
            ),
        }
        for key, label in AMOUNT_LABELS.items()
    ]
    thresholds = ui.to_float(pd.DataFrame(threshold_rows), ["monthly", "annual"])

    baseline_key = f"bands_baseline_{chosen_year}"
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
        key=f"bands_editor_{chosen_year}",
    )

    if st.button("Save thresholds", type="primary", disabled=READ_ONLY):
        baseline = st.session_state[baseline_key]
        saved = 0
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
                    monthly.quantize(PENCE, rounding=ROUND_HALF_UP), year_start,
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
        "Whole percentages, as they are quoted: 20 rather than 0.20. Stored to a hundredth "
        "of a point, so an 8.5% rate is expressible — as a fraction in a two-decimal column "
        "it would not have been."
    )

    rate_rows = [
        {
            "key": key,
            "band": label,
            "rate": stored.get((key, year_start)),
        }
        for key, label in RATE_LABELS.items()
    ]
    rates_frame = ui.to_float(pd.DataFrame(rate_rows), ["rate"])

    edited_rates = st.data_editor(
        rates_frame,
        use_container_width=True,
        hide_index=True,
        disabled=["key", "band"] if not READ_ONLY else True,
        column_order=["band", "rate"],
        column_config={
            "band": "Rate",
            "rate": st.column_config.NumberColumn(
                "Value", format="%.2f", step=0.5, min_value=0.0, max_value=100.0
            ),
        },
        key=f"rates_editor_{chosen_year}",
    )

    if st.button("Save rates", type="primary", disabled=READ_ONLY):
        with ui.session() as session, session.begin():
            for _, row in edited_rates.iterrows():
                if row["rate"] is None or pd.isna(row["rate"]):
                    continue
                outcome = reference.set_assumption(
                    session, chosen_year, row["key"],
                    Decimal(str(row["rate"])).quantize(PENCE), year_start,
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

    st.info(
        f"**Basic rate band: {ui.money(bands.basic_band)} a month.** Derived, not entered — "
        "the basic rate threshold less the personal allowance, which is what the workbook's "
        "`=D28-D22` did. Editing either input moves it."
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
    latest = paid.iloc[-1] if not paid.empty else None
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
        "Rent", value=float(settings.get("spend_rent", 0)), step=50.0, format="%.2f"
    )
    savings_allowance = inputs[1].number_input(
        "Savings", value=float(settings.get("spend_savings", 0)), step=100.0,
        format="%.2f", help="Added to the budgeted credit-card repayment",
    )
    food = inputs[2].number_input(
        "Food", value=float(settings.get("spend_food", 0)), step=50.0, format="%.2f"
    )
    essentials = inputs[3].number_input(
        "Essentials", value=float(settings.get("spend_essentials", 0)), step=10.0,
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
