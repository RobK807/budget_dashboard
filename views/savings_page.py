"""Savings and investments -- the Summary tab's two headline tables (B2:O15).

Both were built on `SUM(INDIRECT("xlSavingsEom" & month))`: a named range per account block
per month, thirty-six of them, each pointing at a row whose position depended on how many
transactions that month happened to hold. Here it is one query over the ledger.

The 'available' column is the workbook's 'Less SC & Wed'. That label had stopped being true:
the figure it produced excluded Tembo as well from June onwards, without the heading
changing. Flagging the accounts instead means the column follows when a pot is added.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from budget import repo, ui

data = ui.page_header(
    "Savings and investments", "What is put aside each month, against the target."
)

postings = data["postings"]
accounts = data["accounts"]
periods = data["periods"]
live_periods = ui.periods_with_data(data)

if not live_periods:
    st.info("No transactions yet.")
    st.stop()

series = repo.savings_series(
    postings, data["openings"], accounts, data["savings_targets"], periods,
    bucket_targets=data["bucket_targets"],
)

if series.empty:
    st.info("Nothing to show yet.")
    st.stop()

earmarked = accounts[accounts["exclude_from_savings"].fillna(False).astype(bool)]
earmarked_names = repo.sort_human(earmarked, by="name")["name"].tolist()

latest = series.iloc[-1]

# --------------------------------------------------------------------------- headline

cols = st.columns(4)
ui.metric(
    cols[0],
    "Savings", ui.money(latest["savings_eom"]),
    help="Every account flagged as savings",
)
ui.metric(
    cols[1],
    "Available", ui.money(latest["available_eom"]),
    help="Savings less the earmarked pots below",
)
ui.metric(cols[2], "Investments", ui.money(latest["investments_eom"]))
ui.metric(
    cols[3],
    "Combined", ui.money(latest["combined"]),
    help="Savings and investments together",
)

if earmarked_names:
    st.caption(
        "Earmarked and therefore excluded from 'available': "
        + ", ".join(earmarked_names)
        + ". Set under **Settings → Savings targets**."
    )
else:
    st.caption(
        "No accounts are earmarked, so 'available' is the whole savings balance. Earmark "
        "one under **Settings → Savings targets**."
    )

st.divider()

# ---------------------------------------------------------------------------- savings

# Opening and closing side by side, so a month reads across in one line. The workbook gave
# closing balances only, which meant 'Added' could only be checked by finding the row above.
#
# Six columns whichever basis is chosen, in the order the money moves: what was there, what
# went in, what was there at the end, then the two targets and the shortfall. Splitting the
# basis into a dropdown rather than widening the table keeps that reading order -- nine
# columns interleaving total and available made a row something to decode rather than read.
COLUMNS = ["bom", "added", "eom", "target", "target_eom", "required"]
LABELS = {
    "month": "Month",
    "bom": "BoM",
    "added": "Added",
    "eom": "EoM",
    "target": "Monthly target",
    "target_eom": "Cumulative target",
    "required": "Required",
}

# Each basis carries its own target as well as its own balance. A target set against an
# earmarked pot is not one the available balance was ever asked to meet, so a single savings
# figure across all three made 'Required' wrong on two of them.
BASES = {
    "Total": ("savings_bom", "savings_added", "savings_eom",
              "total_target", "total_target_eom", "total_required"),
    "Available": ("available_bom", "available_added", "available_eom",
                  "available_target", "available_target_eom", "available_required"),
    "Reserved": ("reserved_bom", "reserved_added", "reserved_eom",
                 "reserved_target", "reserved_target_eom", "reserved_required"),
}

head, picker = st.columns([3, 1])
head.subheader("Savings")
basis = picker.selectbox(
    "Basis",
    options=list(BASES),
    key="savings_basis",
    help="Total is every savings account; available excludes the earmarked pots; reserved "
         "is those pots alone.",
)

bom, added, eom, target, target_eom, required = BASES[basis]
savings = series[
    ["month", bom, added, eom, target, target_eom, required]
].set_axis(["month"] + COLUMNS, axis="columns")

st.dataframe(
    ui.money_table(ui.newest_first(savings), COLUMNS, labels=LABELS),
    width="stretch",
    hide_index=True,
)

st.subheader("Investments")
investments = series[
    ["month", "investments_bom", "investments_added", "investments_eom",
     "investments_target", "investments_target_eom", "investments_required"]
].set_axis(["month"] + COLUMNS, axis="columns")

st.dataframe(
    ui.money_table(ui.newest_first(investments), COLUMNS, labels=LABELS),
    width="stretch",
    hide_index=True,
)

st.caption(
    "**Added** is a change in balance rather than a deposit total, so a month that spends "
    "out of savings shows a negative. On the *available* basis it is what went into the "
    "unearmarked accounts, on *reserved* what went into the earmarked pots"
    + (f" ({', '.join(earmarked_names)})" if earmarked_names else "")
    + "; the two sum to the total. **Cumulative target** is the monthly targets summed to "
    "date, and **Required** is that less the balance on the chosen basis, so a positive "
    "figure is money still to find. It measures a running total of contributions against a "
    "balance, which only lines up from the month the targets start in."
)

st.divider()

# ------------------------------------------------------------------ targets by account

st.subheader("Where the target comes from")
st.caption(
    "The overview above is the sum of these, not a second figure typed beside it. The plan "
    "is held per account and effective-dated, so a revision applies from its own date and "
    "the earlier figures stand for the months they covered. Set it under "
    "**Settings → Savings targets**."
)

plan_detail = data["plan_detail"]
plan_detail = plan_detail[plan_detail["period"].isin(series["period"])]

if plan_detail.empty:
    # Distinguish 'nothing has been entered' from 'entered, but this dashboard did not read
    # it'. The two look identical here, and only one of them is fixed by typing it in again.
    stored = len(data["savings_plan"])
    if stored:
        st.warning(
            f"**{stored} target(s) are stored but none apply to the months shown.** The "
            "plan starts later than this data does, or it was entered against accounts that "
            "are no longer flagged as savings or investments. Check "
            "**Settings → Savings targets**."
        )
    else:
        st.info(
            "No plan set yet — add one under **Settings → Savings targets**, which will "
            "also say which database it read if you were expecting one to be there."
        )
else:
    matrix = plan_detail.pivot_table(
        index="period", columns="account", values="amount", aggfunc="sum"
    )
    matrix = matrix[ui.alphabetical(matrix.columns)]
    matrix.insert(0, "Total", matrix.sum(axis=1))
    matrix.index = [repo.period_label(p) for p in matrix.index]
    matrix.index.name = "Month"
    st.dataframe(
        ui.money_table(ui.newest_first(matrix.reset_index()), [c for c in matrix.columns]),
        width="stretch",
        hide_index=True,
    )

    kinds = (
        plan_detail[plan_detail["period"] == plan_detail["period"].max()]
        .groupby("kind")["amount"].sum()
    )
    st.caption(
        "Counting towards **savings**: "
        + ", ".join(
            ui.alphabetical(
                plan_detail.loc[plan_detail["kind"] == "Savings", "account"]
            )
        )
        + f" ({ui.money(kinds.get('Savings', 0))} a month). Towards **investments**: "
        + ", ".join(
            ui.alphabetical(
                plan_detail.loc[plan_detail["kind"] == "Investments", "account"]
            )
        )
        + f" ({ui.money(kinds.get('Investments', 0))} a month)."
    )

st.divider()

# ------------------------------------------------------------------------- over time

st.subheader("Balances over the year")

chart_from = ui.month_from(periods, key="savings_chart_from", tax_year=data["tax_year"])
# `series` keeps every month -- the balances are cumulative and the tables below read it.
# Only the two charts here are windowed.
charted = series[series["period"] >= chart_from] if chart_from else series

BALANCE_SERIES = {
    "available_eom": "Savings (available)",
    "reserved_eom": "Savings (reserved)",
    "investments_eom": "Investments",
    "combined": "Combined (total)",
    "combined_available": "Combined (available)",
}
plot = ui.to_float(charted, list(BALANCE_SERIES)).rename(columns=BALANCE_SERIES)
fig = px.line(
    plot, x="month", markers=True, y=list(BALANCE_SERIES.values()),
    labels={"value": "Balance (£)", "month": "", "variable": ""},
)
fig.update_layout(hovermode="x unified", legend_title_text="", margin=dict(t=10))
st.plotly_chart(ui.money_axis(fig), width="stretch")
st.caption(
    "**Combined (total)** is all savings plus investments; **combined (available)** leaves "
    "out the earmarked pots, so the gap between the two lines is what is already spoken for."
)

st.subheader("Added against target")

# Each target sits directly over the column it measures, drawn as an outline so the solid bar
# behind it stays readable. Traces sharing an `offsetgroup` take the same slot rather than
# standing side by side, which is what puts the two on top of each other instead of beside.
#
# One axis, and a monthly target against a monthly addition. The cumulative line this
# replaces needed a second axis to be visible at all, and compared a running total against a
# single month -- so nothing on the chart could be found in the table beneath it.
targets = data["bucket_targets"].set_index("period")
plot = ui.to_float(
    charted, ["available_added", "reserved_added", "investments_added"]
).copy()
for bucket in ("available", "reserved", "investments"):
    # Over `plot`'s own periods, not `series`'s. They were the same frame until the charts
    # gained a start month; now plot is the windowed one, and a column built from the full
    # series is simply the wrong length.
    plot[f"{bucket}_target"] = [
        float(targets.loc[p, bucket]) if p in targets.index else 0.0
        for p in plot["period"]
    ]

fig = go.Figure()
for bucket, added, label, colour in (
    ("available", "available_added", "Savings (available)", ui.ACCENT),
    ("reserved", "reserved_added", "Savings (reserved)", "#8C9EC4"),
    ("investments", "investments_added", "Investments", ui.POSITIVE),
):
    fig.add_trace(
        go.Bar(
            name=f"{label} — added", x=plot["month"], y=plot[added],
            marker_color=colour, offsetgroup=bucket, legendgroup=bucket,
        )
    )
    fig.add_trace(
        go.Bar(
            name=f"{label} — target", x=plot["month"], y=plot[f"{bucket}_target"],
            offsetgroup=bucket, legendgroup=bucket,
            marker=dict(
                color="rgba(0,0,0,0)",           # hollow, so the bar behind still reads
                line=dict(color=colour, width=2),
                pattern=dict(shape="/", fgcolor=colour, fgopacity=0.25, size=6),
            ),
        )
    )

fig.update_layout(barmode="group", hovermode="x unified", margin=dict(t=10))
fig.update_yaxes(title_text="£ in the month", tickformat=",.2f", hoverformat=",.2f")
st.plotly_chart(fig, width="stretch")
st.caption(
    "The hollow column is the month's **target**, drawn over the solid column of what was "
    "actually **added**, so a bar short of its outline is a month that fell behind. Savings "
    "targets are split the same way the balances are: an account's target counts as reserved "
    "if the pot is earmarked, available if it is not, and the two sum to the savings figure "
    "in the table. One-offs are already included."
)

st.divider()

# --------------------------------------------------------------------- projection

st.subheader("Where this ends up")

# Two lines per bucket with the same slope and different starting points. The gap between
# them is `required` as it stands today, and it stays exactly that wide the whole way across
# -- which is the point rather than a shortcoming of the model. Saving the planned amount
# from here does not close a gap that has already opened; a chart whose lines quietly
# converged would say that it does.
look_forward = st.slider(
    "Project forward (months)",
    min_value=1,
    max_value=36,
    value=max(1, int(data["look_forward"])),
    key="savings_look_forward",
    help="Months beyond the latest actual figures. Set the default under "
         "**Settings → General**.",
)

projection = repo.savings_projection(
    series, data["savings_plan"], accounts, data["savings_adjustments"], look_forward
)

if projection.empty:
    st.info("Nothing to project from yet.")
else:
    PROJECTED = {
        "available": ("Savings (available)", ui.ACCENT),
        "reserved": ("Savings (reserved)", "#8C9EC4"),
        "savings": ("Savings (total)", "#72B7B2"),
        "investments": ("Investments", ui.POSITIVE),
        "combined": ("Combined", "#B279A2"),
    }

    plot = ui.to_float(
        projection,
        [f"{b}_{side}" for b in PROJECTED for side in ("actual", "target")],
    )

    fig = go.Figure()
    for bucket, (label, colour) in PROJECTED.items():
        # One legend entry per bucket, not two. Both lines share a legendgroup, so a click
        # hides the pair -- which is what 'I do not care about investments right now' means.
        # Ten separate entries for five things is a legend you read rather than use.
        fig.add_trace(
            go.Scatter(
                x=plot["month"], y=plot[f"{bucket}_actual"], name=label,
                legendgroup=bucket, mode="lines+markers",
                line=dict(color=colour, width=2),
            )
        )
        fig.add_trace(
            go.Scatter(
                x=plot["month"], y=plot[f"{bucket}_target"], name=f"{label} — on target",
                legendgroup=bucket, showlegend=False, mode="lines",
                line=dict(color=colour, width=2, dash="dot"),
            )
        )

    fig.update_layout(hovermode="x unified", legend_title_text="", margin=dict(t=10))
    st.plotly_chart(ui.money_axis(fig, label="Balance (£)"), width="stretch")

    anchor = projection.iloc[0]["month"]
    st.caption(
        f"Both lines start from **{anchor}**, the latest actual figures, and add each future "
        "month's target. The **solid** line carries the balance forward as it actually "
        "stands; the **dotted** line carries the cumulative target forward instead. A solid "
        "line above its dotted twin is a pot that is ahead of plan, below it one that is "
        "behind — and the distance between them does not change, because saving to plan from "
        "here keeps pace with the target rather than catching up on it. Click a name in the "
        "legend to hide both of its lines."
    )

    if data["savings_plan"].empty:
        st.info(
            "No savings plan is set, so every future month adds nothing and both lines run "
            "flat. Set one under **Settings → Savings targets**."
        )

st.divider()

# ------------------------------------------------------------------- account detail

st.subheader("Accounts at end of the month")

# Fixed to the last month in the series until now, which made 'what did I have in March'
# a question the page could not answer. Defaults to the same month it always showed, so
# the reading on arrival is unchanged.
month_options = list(series["period"])
detail_period = ui.month_select(
    "Month", month_options,
    default=month_options[-1]
    if repo.period_of(dt.date.today()) not in month_options else None,
    key="accounts_month",
    label_visibility="collapsed",
)

# ---- against target, per account -------------------------------------------------
#
# The same six figures the overview carries, one row per pot. The tables at the top say the
# savings are behind; this says which account is behind, which is the question that follows.
st.markdown("**Against target**")

by_account = repo.savings_by_account(
    postings, data["openings"], accounts, data["plan_detail"], detail_period, periods
)

if by_account.empty:
    st.info("No savings or investment accounts to show for this month.")
else:
    # A total, so the row ties back to the tables at the top of the page. It spans both
    # kinds, so it matches savings and investments added together rather than either alone.
    totals = {c: by_account[c].sum() for c in COLUMNS}
    shown = pd.concat(
        [
            by_account,
            pd.DataFrame([{"account": "Total", "kind": "", "earmarked": None, **totals}]),
        ],
        ignore_index=True,
    )
    st.dataframe(
        ui.money_table(
            shown[["account", "kind", "earmarked"] + COLUMNS],
            COLUMNS,
            labels={"account": "Account", "kind": "Type", "earmarked": "Earmarked",
                    **LABELS},
        ),
        width="stretch",
        hide_index=True,
    )
    st.caption(
        "**Cumulative target** starts from each account's seed — what the pot already held "
        "before any of this was recorded — plus every monthly target set for it up to and "
        "including this month. **Required** is that less the closing balance, so a positive "
        "figure is money still to find in that pot. An account with no target of its own "
        "shows nothing in the two target columns, which is not the same as a target of zero. "
        "Seeds are set under **Settings → Savings targets**."
    )

st.markdown("**How the money moved**")

balances = repo.account_balances(
    postings, data["openings"], detail_period, accounts
)
detail = balances[balances["is_savings"] | balances["is_investment"]].copy()
detail["kind"] = detail.apply(
    lambda r: "Investment" if r["is_investment"] else "Savings", axis=1
)
detail["earmarked"] = detail["account"].isin(earmarked_names)

st.dataframe(
    ui.money_table(
        repo.sort_human(
            detail[["account", "kind", "earmarked", "opening", "transfer_in",
                    "transfer_out", "paid_in", "paid_out", "closing"]],
            by=["kind", "account"],
        ),
        ["opening", "transfer_in", "transfer_out", "paid_in", "paid_out", "closing"],
        labels={
            "account": "Account", "kind": "Type", "earmarked": "Earmarked",
            "opening": "Opening", "transfer_in": "Transfers in",
            "transfer_out": "Transfers out", "paid_in": "Paid in",
            "paid_out": "Paid out", "closing": "Closing",
        },
    ),
    width="stretch",
    hide_index=True,
)

st.divider()

# ------------------------------------------------------- the Savings interest tracker
#
# Three sheets of it, all of them aggregations of the ledger rather than a second record.
# Held in tabs because each answers a question you ask once a year, not every time the page
# is opened.

tab_return, tab_interest, tab_donations = st.tabs(
    ["Investment return", "Interest", "Donations"]
)

# ------------------------------------------------------------------- investment return

with tab_return:
    st.caption(
        "What the investments have actually returned, net of what was paid into them. Both "
        "sides come from the ledger rather than being assumed: a contribution is a transfer "
        "in, and a valuation change is the credit or debit commented 'Investment return'. So"
    )
    st.markdown("&nbsp;&nbsp;&nbsp;&nbsp;`closing = opening + contributions + gain`")

    returns = repo.investment_return_series(
        postings, data["openings"], accounts, periods
    )

    if returns.empty:
        st.info("No investment accounts with a balance yet.")
    else:
        shown = returns.copy()
        shown["monthly_return"] = shown["monthly_return"].map(
            lambda v: None if v is None else float(v) * 100
        )
        st.dataframe(
            ui.money_table(
                ui.newest_first(
                    shown[["month", "account", "opening", "contributions", "gain",
                           "closing", "monthly_return"]]
                ),
                ["opening", "contributions", "gain", "closing"],
                labels={
                    "month": "Month", "account": "Account", "opening": "Opening",
                    "contributions": "Paid in", "gain": "Gain", "closing": "Closing",
                    "monthly_return": "Return %",
                },
                formats={"monthly_return": "{:,.2f}"},
            ),
            width="stretch",
            hide_index=True,
            height=420,
        )
        st.caption(
            "**Return %** is the gain over the opening balance, so a month's contribution "
            "does not read as growth. The table starts at the earliest month in the "
            "database and extends as history is backfilled — nothing here is pinned to a "
            "start date."
        )

        # One account is four times the size of the others, so on a single axis the smaller
        # two are flat lines along the bottom and their movement is invisible. Which accounts
        # go on the right is a choice rather than a rule -- defaulted to the ones materially
        # below the largest, which is the situation that makes a second axis worth having.
        latest_each = (
            returns[returns["period"] == returns["period"].max()]
            .set_index("account")["closing"]
        )
        biggest = max(latest_each) if len(latest_each) else 0
        smaller = [
            name for name, value in latest_each.items()
            if biggest and value < biggest / 2
        ]
        right = st.multiselect(
            "Plot on the right-hand axis",
            options=ui.alphabetical(returns["account"]),
            default=ui.alphabetical(smaller),
            key="return_secondary",
            help="For accounts too small to read against the largest one.",
        )

        chart = ui.to_float(returns, ["closing"])
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        for account in ui.alphabetical(chart["account"]):
            mine = chart[chart["account"] == account]
            on_right = account in right
            fig.add_trace(
                go.Scatter(
                    name=account + (" (right)" if on_right else ""),
                    x=mine["date"], y=mine["closing"],
                    mode="lines+markers",
                    line=dict(dash="dot" if on_right else "solid"),
                ),
                secondary_y=on_right,
            )
        fig.update_layout(hovermode="x unified", legend_title_text="", margin=dict(t=10))
        fig.update_yaxes(
            title_text="Balance (£)", tickformat=",.2f", hoverformat=",.2f",
            secondary_y=False,
        )
        fig.update_yaxes(
            title_text="Balance (£) — right", tickformat=",.2f", hoverformat=",.2f",
            secondary_y=True, showgrid=False,
        )
        st.plotly_chart(fig, width="stretch")
        if right:
            st.caption(
                "Dotted lines are read against the **right-hand** axis: "
                + ", ".join(right)
                + ". The two axes have different scales, so a line crossing another says "
                "nothing about their balances."
            )

        pct = returns.dropna(subset=["monthly_return"]).copy()
        if not pct.empty:
            pct["monthly_return"] = pct["monthly_return"].map(lambda v: float(v) * 100)
            fig = px.bar(
                pct, x="month", y="monthly_return", color="account", barmode="group",
                labels={"monthly_return": "Return (%)", "month": "", "account": ""},
            )
            expected_monthly = float(
                repo.monthly_rate(repo.investment_return_rate(data["settings"])) * 100
            )
            # Plotly's default hline is near-black, which vanishes on the dark theme and
            # again wherever it crosses a bar. Amber sits outside the categorical palette
            # the bars are drawn from, so it cannot be confused for a series, and it reads
            # against both backgrounds. The annotation carries its own panel for the same
            # reason -- over a negative month the text was landing on a coloured column.
            fig.add_hline(
                y=expected_monthly,
                line_dash="dash",
                line_color=ui.REFERENCE,
                line_width=2.5,
                annotation_text=f"expected {expected_monthly:,.3f}%",
                annotation_position="top left",
                annotation_font_color=ui.REFERENCE,
                annotation_bgcolor="rgba(0,0,0,0.55)",
            )
            fig.update_layout(margin=dict(t=10))
            fig.update_yaxes(tickformat=",.2f", hoverformat=",.2f")
            st.plotly_chart(fig, width="stretch")
            st.caption(
                "The dashed line is the expected return from **Settings → Savings "
                "targets**, converted to its monthly equivalent by compounding rather than "
                "by dividing by twelve."
            )

        # The month the summary actually measures to, from the same rule it uses: the last
        # one whose end has passed. Taking the series maximum named the end of the *data*,
        # which runs to March, so in August it claimed August over July's figures.
        closed_to = repo.period_label(repo.completed_through(returns))
        st.subheader(f"Summary to end of {closed_to}")
        summary = repo.investment_return_summary(returns)
        if summary.empty:
            st.caption("Nothing has completed a month yet.")
        else:
            display = summary.copy()
            for column in ("total_return", "annualised"):
                display[column] = display[column].map(
                    lambda v: None if v is None else float(v) * 100
                )
            st.dataframe(
                ui.money_table(
                    display[["account", "start", "contributions", "current", "net",
                             "total_return", "months", "annualised"]],
                    ["start", "contributions", "current", "net"],
                    labels={
                        "account": "Account", "start": "Start",
                        "contributions": "Paid in", "current": "Current", "net": "Net",
                        "total_return": "Return %", "months": "Months",
                        "annualised": "Annualised %",
                    },
                    formats={"total_return": "{:,.2f}", "annualised": "{:,.2f}",
                             "months": "{:,.0f}"},
                ),
                width="stretch",
                hide_index=True,
            )
            months = int(summary["months"].max())
            st.caption(
                f"**Net** is the current balance less everything paid in, and **Return %** "
                f"is that against the starting balance. Measured to "
                f"the end of **{closed_to}**, the last month to have closed: a month still "
                f"running has no return to report, and annualising a part-month would "
                f"exaggerate it. So these figures will trail the balances above, which are "
                f"current. **Annualised** scales {months} month(s) up to a year, which is a "
                "fair summary over a year and a noisy one over a quarter."
            )

# --------------------------------------------------------------------------- interest

with tab_interest:
    st.caption(
        "Interest received, by tax year — HMRC's year, running 6 April to 5 April. Nothing "
        "new is entered here: interest is already in the ledger under the **Interest** "
        "category, so this is a grouping of transactions rather than a second record of "
        "them. **Gross** or **net** is the account's own flag, set under "
        "**Settings → Accounts**."
    )

    interest = repo.interest_by_tax_year(data["transactions"], accounts)

    if interest.empty:
        st.info("No interest recorded under the Interest category yet.")
    else:
        totals = repo.interest_totals(interest)
        headline = totals.iloc[-1]
        cols = st.columns(3)
        ui.metric(cols[0], f"Gross — {headline['year']}", ui.money(headline["gross"]))
        ui.metric(cols[1], f"Net — {headline['year']}", ui.money(headline["net"]))
        ui.metric(cols[2], f"Total — {headline['year']}", ui.money(headline["total"]))

        st.markdown("**By tax year**")
        st.dataframe(
            ui.money_table(
                totals[["year", "gross", "net", "total", "accounts"]],
                ["gross", "net", "total"],
                labels={"year": "Tax year", "gross": "Gross", "net": "Net",
                        "total": "Total", "accounts": "Accounts"},
                formats={"accounts": "{:,.0f}"},
            ),
            width="stretch",
            hide_index=True,
        )

        st.markdown("**By account**")
        picked = st.selectbox(
            "Tax year",
            options=list(totals["year"])[::-1],
            key="interest_year",
        )
        year = int(totals.loc[totals["year"] == picked, "tax_year"].iloc[0])
        mine = interest[interest["tax_year"] == year]
        st.dataframe(
            ui.money_table(
                mine[["account", "basis", "amount"]],
                ["amount"],
                labels={"account": "Account", "basis": "Basis", "amount": "Interest"},
            ),
            width="stretch",
            hide_index=True,
        )

        fig = px.bar(
            ui.to_float(mine, ["amount"]), x="account", y="amount", color="basis",
            labels={"amount": "£", "account": "", "basis": ""},
        )
        fig.update_layout(margin=dict(t=10))
        st.plotly_chart(ui.money_axis(fig), width="stretch")

        st.caption(
            "A payment dated 1–5 April belongs to the *previous* tax year. The date decides "
            "which year a payment falls in, so an April straddling the changeover needs no "
            "splitting by hand."
        )

# -------------------------------------------------------------------------- donations

with tab_donations:
    st.caption(
        "Charitable giving, by tax year. Flagged on the payment rather than inferred from a "
        "category, because a category cannot tell a gift from the platform fee charged "
        "alongside it — the two leave the account together, on the same day, under the same "
        "heading. Flag one on **Add transaction**, on **Import**, or on the **Transactions** "
        "page for something already recorded."
    )

    given = repo.donations(data["transactions"])
    by_year = repo.donations_by_tax_year(data["transactions"])

    if given.empty:
        st.info("Nothing flagged as a donation yet.")
    else:
        cols = st.columns(2)
        ui.metric(cols[0], "Given, all years", ui.money(given["amount"].sum()))
        ui.metric(
            cols[1],
            f"Given in {by_year.iloc[-1]['year']}", ui.money(by_year.iloc[-1]["amount"])
        )

        st.markdown("**By tax year**")
        st.dataframe(
            ui.money_table(
                by_year[["year", "amount", "count"]],
                ["amount"],
                labels={"year": "Tax year", "amount": "Given", "count": "Payments"},
                formats={"count": "{:,.0f}"},
            ),
            width="stretch",
            hide_index=True,
        )

        st.markdown("**Every donation**")
        rows = given.copy()
        rows["date"] = rows["date"].dt.date
        st.dataframe(
            ui.money_table(
                rows[["year", "id", "date", "account", "amount", "comment"]],
                ["amount"],
                labels={"year": "Tax year", "id": "ID", "date": "Date",
                        "account": "Account", "amount": "Amount", "comment": "Comment"},
                formats={"id": "{:,.0f}"},
            ),
            width="stretch",
            hide_index=True,
        )
        st.caption(
            "Transaction fees are deliberately absent: they are recorded as their own line "
            "with the flag left clear, so this column is what was actually given."
        )
