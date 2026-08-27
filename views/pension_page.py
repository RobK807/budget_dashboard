"""Pension -- what the pots are worth, and how much of that was growth rather than payments.

A pension is the one balance in this application where the figure on the statement does not
answer the question. Everything else rises because money was put in or fell because it was
spent; a pension does both at once, and a pot being paid into looks identical to a pot that
is growing. Telling them apart needs the payments recorded separately from the valuations,
which is what the contribution ledger is for -- see models.PensionContribution.

Entry lives at the foot of this page rather than under Record. The pots are read from three
statements a few times a year, and a form on the page that displays the result is easier to
keep honest than a form on a page that does not.

The arithmetic is all in repo.pension_history / repo.pension_totals; this file draws it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from budget import reference, repo, ui

data = ui.page_header("Pension", "What the pots are worth, and what they have earned.")

pots = data["pension_pots"]
valuations = data["pension_valuations"]
contributions = data["pension_contributions"]

READ_ONLY = ui.read_only()
PRIVATE = ui.private()

if READ_ONLY:
    st.warning(
        "**Read-only while checked out for offline use.** Check in on the Sync page first."
    )

history = repo.pension_history(pots, valuations, contributions)
totals = repo.pension_totals(history)

TOTAL = "Total"

# The four measures, in the order the picker offers them. A list of pairs rather than a
# dict because the picker shows the label and every other use needs the column, and keeping
# them together is what stops the table and the chart drawing different measures.
MEASURES = [
    ("Since the last valuation", "period_return"),
    ("Since the last valuation, annualised", "period_annualised"),
    ("Since tracking began", "total_return"),
    ("Since tracking began, annualised", "total_annualised"),
]
MEASURE_HELP = {
    "period_return": "What each pot made between one valuation and the next, after taking "
                     "off anything paid in over the same stretch — without that deduction a "
                     "payment in reads as a gain.",
    "period_annualised": "The same gap restated as a full year, so a three-month stretch and "
                         "a five-month one can be compared. A short gap magnifies whatever "
                         "happened in it.",
    "total_return": "Value now measured against the first valuation plus everything paid in "
                    "since.",
    "total_annualised": "The whole-life return spread over the years it took. It assumes the "
                        "money was all there from the start, so a pot still being paid into "
                        "is understated — for one of those, read the top line instead.",
}


def figure(value) -> str:
    """A percentage for the screen, or a dash where there is nothing to report."""
    return "—" if value is None or pd.isna(value) else f"{ui.percent(value)}%"


# =========================================================================== the position

if history.empty:
    st.info(
        "No valuations recorded yet. Add a pension at the foot of this page, then record "
        "what it was worth on a date — two dates are enough for the first return."
    )
else:
    now = totals.iloc[-1]
    previous = totals.iloc[-2] if len(totals) > 1 else None

    cols = st.columns(4)
    ui.metric(
        cols[0],
        "Total value", ui.money(now["value"]),
        delta=ui.money(now["value"] - previous["value"]) if previous is not None else None,
        help=f"Every pot as at {now['date']:%d %b %Y}. The change is since the valuation "
             "before it.",
    )
    ui.metric(
        cols[1],
        "Paid in", ui.money(now["base"]),
        help="What the pots held when tracking started, plus every net payment since. This "
             "is what the growth is measured against.",
    )
    ui.metric(
        cols[2],
        "Growth", ui.money(now["growth"]),
        help="Value less what has gone in — the money the pensions have made.",
    )
    ui.metric(
        cols[3],
        "Return to date", figure(now["total_return"]),
        sensitive=False,
        delta=(
            None if pd.isna(now["total_annualised"])
            else f"{ui.percent(now['total_annualised'])}% a year"
        ),
        delta_color="off",
        help="A percentage says nothing about the size of the pot behind it, so it stays on "
             "screen while the amounts are hidden.",
    )

    carried = now["carried"]
    if carried:
        st.caption(
            "Carried forward on this date, having no figure of its own: "
            + ", ".join(carried)
            + ". A pot with no fresh valuation keeps its last one rather than dropping out "
            "of the total."
        )

    st.divider()

    # ------------------------------------------------------------------ value over time

    st.subheader("What the pension is worth")

    wide = history.pivot(index="date", columns="pot", values="value").sort_index()
    order = [p for p in pots.sort_values("display_order")["name"] if p in wide.columns]
    # Zero-filled for the chart only. A pot that had not started yet contributes nothing to
    # a stacked area, which is the truth; in the table below the same cell is left blank,
    # because there a nought would read as 'valued, and worth nothing'.
    plot = ui.to_float(wide[order].fillna(0).reset_index(), order)

    fig = px.area(
        plot, x="date", y=order,
        labels={"value": "Value (£)", "date": "", "variable": ""},
    )
    fig.update_layout(hovermode="x unified", legend_title_text="", margin=dict(t=10))
    fig.update_traces(hovertemplate="%{y:,.2f}")
    st.plotly_chart(ui.money_axis(fig, label="Value (£)", mask=PRIVATE), width="stretch")
    st.caption(
        "The pots are stacked, so the top edge is the total. A step up that the ledger "
        "explains is money paid in; the rest is growth."
    )

    table = wide.copy()
    table[TOTAL] = table.sum(axis=1)
    table = table.reset_index().rename(columns={"date": "Date"})
    table["Date"] = [d.strftime("%d %b %Y") for d in table["Date"]]
    st.dataframe(
        ui.money_table(table, order + [TOTAL], mask=PRIVATE),
        width="stretch", hide_index=True,
    )

    st.divider()

    # ----------------------------------------------------------------- growth against in

    st.subheader("Growth against what has gone in")

    choices = [TOTAL] + order
    which = st.selectbox(
        "Pension", choices, key="pension_growth_pick",
        help="The gap between the two lines is the growth.",
    )
    if which == TOTAL:
        curve = totals[["date", "value", "base"]].copy()
    else:
        curve = history[history["pot"] == which][["date", "value", "base"]].copy()

    shape = ui.to_float(curve, ["value", "base"])
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=shape["date"], y=shape["base"], name="Paid in",
            mode="lines", line=dict(color=ui.REFERENCE, width=2, dash="dot"),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=shape["date"], y=shape["value"], name="Value",
            mode="lines+markers", line=dict(color=ui.ACCENT, width=2),
            fill="tonexty", fillcolor="rgba(76,120,168,0.20)",
        )
    )
    fig.update_layout(hovermode="x unified", legend_title_text="", margin=dict(t=10))
    st.plotly_chart(ui.money_axis(fig, label="£", mask=PRIVATE), width="stretch")

    closing = shape.iloc[-1]
    if PRIVATE:
        st.caption(
            "The shaded band is the growth — value above what has been paid in. The figures "
            "are hidden while privacy is on."
        )
    else:
        st.caption(
            f"**{which}** has taken in {ui.money(closing['base'])} and is worth "
            f"{ui.money(closing['value'])}, so {ui.money(closing['value'] - closing['base'])} "
            "of it was never paid in. The dotted line only moves when money does."
        )

    st.divider()

    # -------------------------------------------------------------------------- returns

    st.subheader("Returns")

    labels = [label for label, _ in MEASURES]
    head, picker = st.columns([3, 1])
    head.caption(
        "Every figure here is value at the end divided by value at the start plus anything "
        "paid in between, less one. A pot nothing is paid into has no middle term, which is "
        "why the same measure works for all of them."
    )
    with picker:
        chosen = st.selectbox("Measure", labels, key="pension_measure")
    column = dict(MEASURES)[chosen]
    st.caption(MEASURE_HELP[column])

    returns = history.pivot(index="date", columns="pot", values=column).sort_index()
    returns = returns[[p for p in order if p in returns.columns]]
    returns[TOTAL] = totals.set_index("date")[column]

    shape = ui.to_float(returns.reset_index(), list(returns.columns))
    fig = go.Figure()
    for name in returns.columns:
        is_total = name == TOTAL
        fig.add_trace(
            go.Scatter(
                x=shape["date"], y=shape[name], name=name, mode="lines+markers",
                line=dict(
                    width=3 if is_total else 2,
                    dash="solid" if is_total else "dot",
                    color=ui.ACCENT if is_total else None,
                ),
            )
        )
    fig.add_hline(y=0, line_color=ui.REFERENCE, line_width=1.5)
    fig.update_layout(hovermode="x unified", legend_title_text="", margin=dict(t=10))
    fig.update_yaxes(title_text="Return (%)", tickformat=",.2f", hoverformat=",.2f")
    st.plotly_chart(fig, width="stretch")
    st.caption(
        "The solid line is every pot together, worked out from the pounds rather than "
        "averaged across the three — a middling return on a large pot is not the same as one "
        "on a small pot, and averaging the percentages would treat them as if it were."
    )

    shown = returns.reset_index().rename(columns={"date": "Date"})
    shown["Date"] = [d.strftime("%d %b %Y") for d in shown["Date"]]
    st.dataframe(
        ui.money_table(
            shown, [],
            formats={c: "{:,.2f}%" for c in returns.columns},
        ),
        width="stretch", hide_index=True,
    )

    st.divider()

# ============================================================================ the ledger

st.subheader("What has been paid in")

summary = repo.pension_contribution_summary(contributions, pots)

if summary.empty:
    st.info(
        "No payments recorded against any pension. Without them a rise in value cannot be "
        "told apart from money going in, so every return on this page treats the whole "
        "increase as growth. Record them below."
    )
else:
    st.dataframe(
        ui.money_table(
            summary[["pot", "paid_in", "charges", "net", "entries", "first", "last"]],
            ["paid_in", "charges", "net"],
            labels={
                "pot": "Pension", "paid_in": "In", "charges": "Charges", "net": "Net",
                "entries": "Entries", "first": "From", "last": "To",
            },
            integers=["entries"],
            mask=PRIVATE,
        ),
        width="stretch", hide_index=True,
    )
    st.caption(
        "Charges are held apart from payments because they do not stay small: they run at "
        "pennies a month to begin with and grow with the pot, and inside a net figure that "
        "is invisible."
    )

    ledger = repo.pension_ledger(contributions, pots)
    if not ledger.empty:
        with st.expander(f"Every entry ({len(ledger)})"):
            # Newest first, as every other history table on the site is: the running total
            # is still built in date order, this only turns the finished list round.
            recent = ui.newest_first(ledger)
            st.dataframe(
                ui.money_table(
                    recent[["date", "pot", "amount", "kind", "running", "note"]],
                    ["amount", "running"],
                    labels={
                        "date": "Date", "pot": "Pension", "amount": "Amount",
                        "kind": "Kind", "running": "Paid in to date", "note": "Note",
                    },
                    mask=PRIVATE,
                ),
                width="stretch", hide_index=True, height=420,
            )
            st.caption(
                "'Paid in to date' is the running total for that pension, in date order. It "
                "is worked out on the way past rather than stored, so an entry added out of "
                "order corrects everything after it."
            )

st.divider()

# ============================================================================== recording

st.subheader("Record")

live = pots[pots["valid_to"].isna()] if not pots.empty else pots
value_tab, payment_tab, pot_tab = st.tabs(["A valuation", "A payment", "Pensions"])

# -------------------------------------------------------------------------- a valuation

with value_tab:
    if pots.empty:
        st.info("Add a pension first, under **Pensions**.")
    elif PRIVATE:
        # The inputs are pre-filled from whatever is already stored for the date, which puts
        # the figures straight back on the screen the switch was turned on to clear.
        st.caption("Held back while privacy is on. Switch it off at the top of Summary.")
    else:
        st.caption(
            "One date, every pension. Leave a box empty for a pot you have no figure for — "
            "its last one is carried forward rather than counted as nothing."
        )
        on = st.date_input(
            "Valuation date", value=dt.date.today(), format="DD/MM/YYYY",
            key="pension_valuation_date",
        )
        stored = {}
        if not valuations.empty:
            match = valuations[
                pd.to_datetime(valuations["on_date"]).dt.date == on
            ]
            stored = dict(zip(match["pot_id"], match["value"]))
        if stored:
            st.caption(
                f"{len(stored)} figure(s) already recorded for {on:%d %b %Y}. Saving "
                "replaces them; clearing a box leaves what is stored alone."
            )

        with st.form("pension_valuation"):
            boxes = st.columns(max(len(live), 1))
            entered = {}
            for index, (_, pot) in enumerate(live.iterrows()):
                entered[int(pot["id"])] = boxes[index].number_input(
                    pot["name"],
                    value=(
                        float(stored[pot["id"]]) if pot["id"] in stored else None
                    ),
                    min_value=0.0, step=100.0, format="%.2f",
                    key=f"pension_value_{pot['id']}",
                )
            if st.form_submit_button("Save valuation", type="primary", disabled=READ_ONLY):
                saved, failures = 0, []
                with ui.session() as session, session.begin():
                    for pot_id, amount in entered.items():
                        if amount is None:
                            continue
                        outcome = reference.set_pension_valuation(
                            session, pot_id, on, Decimal(str(amount))
                        )
                        if outcome.ok:
                            saved += 1
                        else:
                            failures.append(outcome.message)
                if failures:
                    st.error(" ".join(failures))
                elif saved:
                    ui.show_outcome(
                        reference.Outcome(
                            True, f"Saved {saved} valuation(s) for {on:%d %b %Y}."
                        ),
                        "the valuation",
                    )
                else:
                    st.info("Nothing entered, so nothing was saved.")

        if stored and not READ_ONLY:
            st.caption("Recorded against the wrong date?")
            wrong = st.selectbox(
                "Remove a figure",
                options=list(stored),
                format_func=lambda i: live.set_index("id")["name"].get(i, str(i)),
                key="pension_valuation_remove_pick",
            )
            if st.button("Remove it", key="pension_valuation_remove"):
                with ui.session() as session, session.begin():
                    outcome = reference.set_pension_valuation(session, wrong, on, None)
                ui.show_outcome(outcome, "the valuation")

# ----------------------------------------------------------------------------- a payment

with payment_tab:
    if pots.empty:
        st.info("Add a pension first, under **Pensions**.")
    else:
        st.caption(
            "Money in is positive and a charge is negative. Two payments on one day is "
            "normal — the employer's share and your own arrive separately, and adding them "
            "together loses the split."
        )
        with st.form("pension_payment"):
            fields = st.columns(4)
            pot_id = fields[0].selectbox(
                "Pension",
                options=[int(i) for i in live["id"]] if not live.empty else [],
                format_func=lambda i: live.set_index("id")["name"].get(i, str(i)),
                key="pension_payment_pot",
            )
            when = fields[1].date_input(
                "Date", value=dt.date.today(), format="DD/MM/YYYY",
                key="pension_payment_date",
            )
            amount = fields[2].number_input(
                "Amount (£)", value=0.0, step=100.0, format="%.2f",
                help="Negative for a charge coming out.",
            )
            kind = fields[3].selectbox(
                "Kind", options=list(reference.PENSION_KINDS),
                format_func=lambda k: k.title(),
                key="pension_payment_kind",
                help="Reporting only — the returns are worked out from the net whatever "
                     "this says.",
            )
            note = st.text_input("Note", placeholder="e.g. Employer contribution")
            if st.form_submit_button("Record it", type="primary", disabled=READ_ONLY):
                with ui.session() as session, session.begin():
                    outcome = reference.add_pension_contribution(
                        session, int(pot_id), when, Decimal(str(amount)), kind, note
                    )
                ui.show_outcome(outcome, "the payment")

        ledger = repo.pension_ledger(contributions, pots)
        if not ledger.empty and not READ_ONLY:
            st.caption("Entered by mistake?")
            recent = ledger.sort_values("date", ascending=False).head(20)

            def describe(row_id: int) -> str:
                row = recent[recent["id"] == row_id].iloc[0]
                money = ui.MASK if PRIVATE else f"{Decimal(str(row['amount'])):,.2f}"
                return f"{row['date']:%d %b %Y} · {row['pot']} · {money}"

            drop = st.selectbox(
                "Remove an entry",
                options=[int(i) for i in recent["id"]],
                format_func=describe,
                key="pension_payment_remove_pick",
            )
            if st.button("Remove it", key="pension_payment_remove"):
                with ui.session() as session, session.begin():
                    outcome = reference.remove_pension_contribution(session, int(drop))
                ui.show_outcome(outcome, "the entry")

# ---------------------------------------------------------------------------- the pots

with pot_tab:
    if not pots.empty:
        listing = pots[["name", "valid_from", "valid_to", "note"]].copy()
        st.dataframe(
            listing.rename(
                columns={
                    "name": "Pension", "valid_from": "Tracked from",
                    "valid_to": "Closed", "note": "Note",
                }
            ),
            width="stretch", hide_index=True,
        )

    with st.form("pension_add_pot"):
        st.caption(
            "A pot is tracked from the date given. Nothing can be recorded against it "
            "before then, which is what stops a valuation landing against the wrong pension."
        )
        fields = st.columns([2, 1])
        new_name = fields[0].text_input("Name", placeholder="e.g. the provider")
        tracked_from = fields[1].date_input(
            "Tracked from", value=dt.date.today(), format="DD/MM/YYYY",
            key="pension_new_from",
        )
        new_note = st.text_input("Note", key="pension_new_note", placeholder="Optional")
        if st.form_submit_button("Add pension", type="primary", disabled=READ_ONLY):
            with ui.session() as session, session.begin():
                _, outcome = reference.add_pension_pot(
                    session, new_name, tracked_from, new_note
                )
            ui.show_outcome(outcome, "the pension")

    if not pots.empty:
        st.divider()
        st.caption(
            "Closing a pot stops its last valuation being carried forward for ever. Its "
            "history stays exactly as it is."
        )
        with st.form("pension_amend_pot"):
            fields = st.columns([2, 1, 1])
            which_pot = fields[0].selectbox(
                "Pension",
                options=[int(i) for i in pots["id"]],
                format_func=lambda i: pots.set_index("id")["name"].get(i, str(i)),
                key="pension_amend_pick",
            )
            record = pots[pots["id"] == which_pot].iloc[0]
            from_date = fields[1].date_input(
                "Tracked from",
                value=repo.as_date(record["valid_from"]) or dt.date.today(),
                format="DD/MM/YYYY", key="pension_amend_from",
            )
            closed = fields[2].date_input(
                "Closed",
                value=repo.as_date(record["valid_to"]),
                format="DD/MM/YYYY", key="pension_amend_to",
            )
            if st.form_submit_button("Save", disabled=READ_ONLY):
                with ui.session() as session, session.begin():
                    outcome = reference.update_pension_pot(
                        session, int(which_pot), valid_from=from_date, valid_to=closed
                    )
                ui.show_outcome(outcome, "the pension")
