import io
import time
from datetime import datetime

import altair as alt
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import fetchers
from fetchers import COLUMNS, CONFIDENCE, FETCHERS, build_grip_comparison

# ---------------------------------------------------------------------------
# Look & feel -- mirrors the palette in .streamlit/config.toml
# ---------------------------------------------------------------------------
PRIMARY_COLOR = "#0F4C81"   # deep blue -- listing counts
ACCENT_COLOR = "#12A594"    # teal -- yield
HIGH_YTM_BG = "#E3F6F1"
HIGH_YTM_TEXT = "#0B6B57"

CONFIDENCE_TEXT_STYLE = {
    "High": "color: #0B6B57; font-weight: 600",
    "Good": "color: #92600B; font-weight: 600",
    "Reverify": "color: #B42318; font-weight: 600",
}
CONFIDENCE_COLOR = {"High": "green", "Good": "orange", "Reverify": "red"}
CONFIDENCE_ORDER = {"High": 0, "Good": 1, "Reverify": 2}

# Bond yields move day to day; past this age the numbers on screen are worth
# a re-fetch before you quote them to anyone.
STALE_WARN_HOURS = 6
STALE_ERROR_HOURS = 24

st.set_page_config(
    page_title="Bond Competition Tracker",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

if "collapse_sidebar_pending" not in st.session_state:
    st.session_state.collapse_sidebar_pending = False

if st.session_state.collapse_sidebar_pending:
    st.session_state.collapse_sidebar_pending = False
    # Streamlit has no public API to collapse the sidebar programmatically,
    # so we click its own collapse button through the parent frame. If a
    # future Streamlit version renames this test id, this just quietly
    # becomes a no-op -- the sidebar stays open, nothing breaks.
    components.html(
        """
        <script>
        (function () {
            const doc = window.parent.document;
            let btn = doc.querySelector('[data-testid="stSidebarCollapseButton"] button');
            if (!btn) {
                const sidebar = doc.querySelector('[data-testid="stSidebar"]');
                if (sidebar) btn = sidebar.querySelector('button[aria-label*="sidebar" i], button[aria-label*="close" i]');
            }
            if (btn) btn.click();
        })();
        </script>
        """,
        height=0,
    )

st.markdown(
    """
    <style>
    div[data-testid="stMetric"] {
        background: #F4F6F8;
        border-radius: 10px;
        border-left: 4px solid #0F4C81;
        padding: 12px 16px 8px 16px;
    }
    div[data-testid="stMetricValue"] {
        color: #0F4C81;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

if "data" not in st.session_state:
    st.session_state.data = None
if "last_fetched" not in st.session_state:
    st.session_state.last_fetched = None
if "data_source" not in st.session_state:
    st.session_state.data_source = None
if "last_upload_signature" not in st.session_state:
    st.session_state.last_upload_signature = None
if "fetch_summary" not in st.session_state:
    st.session_state.fetch_summary = {}


def style_bonds(data, ytm_columns=(), confidence_col=None):
    """Left-aligns nothing itself (column_config handles that) -- just tints
    the cells worth a second look: top-quartile yields and the confidence
    tier, so the table reads at a glance instead of as a wall of numbers.

    The top-quartile cutoff is computed per unique ISIN (one bond, one vote),
    not per row -- otherwise a bond listed on five platforms would count five
    times toward the threshold and skew it, making the highlight look like it
    favors whichever issuer happens to cross-list the most."""
    styler = data.style
    for col in ytm_columns:
        if col not in data.columns:
            continue
        numeric_col = pd.to_numeric(data[col], errors="coerce")
        if not numeric_col.notna().any():
            continue
        if "ISIN" in data.columns:
            per_isin_best = pd.DataFrame({"ISIN": data["ISIN"], "ytm": numeric_col}).groupby("ISIN")["ytm"].max()
            threshold = per_isin_best.quantile(0.85)
        else:
            threshold = numeric_col.quantile(0.85)

        def _highlight(val, threshold=threshold):
            v = pd.to_numeric(pd.Series([val]), errors="coerce").iloc[0]
            return f"background-color: {HIGH_YTM_BG}; color: {HIGH_YTM_TEXT}; font-weight: 700" if pd.notna(v) and v >= threshold else ""

        styler = styler.map(_highlight, subset=[col])
    if confidence_col and confidence_col in data.columns:
        styler = styler.map(lambda v: CONFIDENCE_TEXT_STYLE.get(v, ""), subset=[confidence_col])
    return styler


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
header_left, header_right = st.columns([3, 1])
with header_left:
    st.title("📈 Bond Competition Tracker")
    st.caption(
        "Pulls live bond listings (ISIN, Issuer, YTM, Rating, Tenure, Face Value, "
        "Minimum Investment) from competing OBPPs into one comparable view."
    )
with header_right:
    if st.session_state.last_fetched:
        age_hours = (datetime.now() - st.session_state.last_fetched).total_seconds() / 3600
        stamp = st.session_state.last_fetched.strftime("%d %b, %H:%M")
        if age_hours < STALE_WARN_HOURS:
            st.badge(f"Fresh · {stamp}", icon="🟢", color="green")
        elif age_hours < STALE_ERROR_HOURS:
            st.badge(f"{age_hours:.0f}h old · {stamp}", icon="🟠", color="orange")
        else:
            st.badge(f"Stale · {age_hours:.0f}h old -- refetch", icon="🔴", color="red")
        st.caption(st.session_state.data_source or "Fetched live")
    else:
        st.badge("No data loaded", icon="⚪", color="gray")

st.divider()

# ---------------------------------------------------------------------------
# Sidebar: fetch controls
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("Data source")

    platform_names = list(FETCHERS.keys())
    selected_platforms = st.multiselect(
        "Platforms to fetch",
        options=platform_names,
        default=platform_names,
        help="TheFixedIncome uses a headless browser and is noticeably slower "
        "than the others (it also rate-limits if fetched too frequently). "
        "Grip needs GRIP_METABASE_URL set in secrets, or upload its CSV below instead.",
    )

    fetch_clicked = st.button("Fetch Latest Data", type="primary", width="stretch")

    with st.expander("Or load a local export"):
        st.caption(
            "Some platforms (IndiaBonds, GoldenPi, TheFixedIncome) can be blocked when "
            "fetched from this Cloud deployment but work when run locally. Run "
            "`consolidated_tracker.py` on your own machine and upload its `.xlsx` output "
            "here to visualize the full dataset the same way."
        )
        uploaded_file = st.file_uploader("Upload consolidated_tracker.xlsx", type=["xlsx"])
        if uploaded_file is not None:
            upload_signature = f"{uploaded_file.name}-{uploaded_file.size}"
            if st.session_state.last_upload_signature != upload_signature:
                try:
                    uploaded_df = pd.read_excel(uploaded_file)
                    required_cols = [c for c in COLUMNS if c != "OBPP"] + ["OBPP"]
                    missing = [c for c in required_cols if c not in uploaded_df.columns]
                    if missing:
                        st.error(f"This file is missing expected column(s): {', '.join(missing)}")
                    else:
                        st.session_state.data = uploaded_df
                        st.session_state.last_fetched = datetime.now()
                        st.session_state.data_source = f"Uploaded: {uploaded_file.name}"
                        st.session_state.last_upload_signature = upload_signature
                        st.session_state.collapse_sidebar_pending = True
                        st.toast(f"Loaded {len(uploaded_df)} bonds from {uploaded_file.name}", icon="✅")
                        st.rerun()
                except Exception as e:
                    st.error(f"Couldn't read that file: {e}")

    if st.session_state.fetch_summary:
        st.divider()
        st.subheader("Last fetch summary")
        for platform, info in st.session_state.fetch_summary.items():
            if info["status"] == "ok":
                st.write(f":green[✓] {platform}: {info['rows']} rows ({info['elapsed']:.1f}s)")
            elif info["status"] == "empty" and platform == "Grip":
                grip_reason = info.get("error") or "GRIP_METABASE_URL isn't set in secrets"
                st.write(f":orange[⚠] {platform}: 0 rows — {grip_reason}")
            elif info["status"] == "empty":
                st.write(
                    f":orange[⚠] {platform}: 0 rows ({info['elapsed']:.1f}s) — "
                    "likely blocked/rate-limited upstream, not necessarily a real 'no bonds'."
                )
            else:
                st.write(f":red[✗] {platform}: {info['error']}")

# ---------------------------------------------------------------------------
# Fetch -- per-platform results are logged (print) and kept for the sidebar
# summary; the main page never shows per-platform fetch detail.
# ---------------------------------------------------------------------------
if fetch_clicked:
    if not selected_platforms:
        st.sidebar.error("Select at least one platform.")
    else:
        all_rows = []
        summary = {}
        with st.spinner(f"Fetching {len(selected_platforms)} platform(s)..."):
            for platform in selected_platforms:
                start = time.time()
                try:
                    rows = FETCHERS[platform]()
                    elapsed = time.time() - start
                    all_rows.extend(rows)
                    if rows:
                        summary[platform] = {"status": "ok", "rows": len(rows), "elapsed": elapsed}
                        print(f"✓ {platform}: {len(rows)} rows ({elapsed:.1f}s)")
                    elif platform == "Grip":
                        reason = getattr(fetchers, "GRIP_LAST_ERROR", None) or "GRIP_METABASE_URL isn't set in secrets"
                        summary[platform] = {"status": "empty", "rows": 0, "elapsed": elapsed, "error": reason}
                        print(f"⚠ {platform}: 0 rows — {reason}")
                    else:
                        summary[platform] = {"status": "empty", "rows": 0, "elapsed": elapsed}
                        print(f"⚠ {platform}: 0 rows ({elapsed:.1f}s) — likely blocked/rate-limited upstream")
                except Exception as e:
                    elapsed = time.time() - start
                    summary[platform] = {"status": "error", "error": str(e), "elapsed": elapsed}
                    print(f"✗ {platform}: {e}")

        new_df = pd.DataFrame(all_rows, columns=COLUMNS)
        existing_df = st.session_state.data
        if existing_df is not None and not existing_df.empty:
            kept_df = existing_df[~existing_df["OBPP"].isin(selected_platforms)]
            df = pd.concat([kept_df, new_df], ignore_index=True)
        else:
            df = new_df

        st.session_state.data = df
        st.session_state.last_fetched = datetime.now()
        st.session_state.data_source = "Fetched live"
        st.session_state.fetch_summary = {**st.session_state.fetch_summary, **summary}
        st.session_state.collapse_sidebar_pending = True
        st.toast(f"Fetched {len(new_df)} bonds from {len(selected_platforms)} platform(s) — {len(df)} total", icon="✅")
        st.rerun()

# ---------------------------------------------------------------------------
# Main view
# ---------------------------------------------------------------------------
df = st.session_state.data

if df is None or df.empty:
    st.info(
        "Click **Fetch Latest Data** in the sidebar to load bond listings from all platforms, "
        "or upload a `.xlsx` file exported by the local script."
    )
    st.stop()

if "Confidence" not in df.columns:
    df["Confidence"] = df["OBPP"].map(lambda o: CONFIDENCE.get(o, ("Unknown", ""))[0])
    df = df[["OBPP", "Confidence"] + [c for c in df.columns if c not in ("OBPP", "Confidence")]]

numeric = pd.DataFrame(index=df.index)
numeric["ytm"] = pd.to_numeric(df["YTM (%)"], errors="coerce")
numeric["tenure"] = pd.to_numeric(df["Tenure (Months)"], errors="coerce")
numeric["face_value"] = pd.to_numeric(df["Face Value"], errors="coerce")
numeric["min_investment"] = pd.to_numeric(df["Minimum Investment Amount"], errors="coerce")

# ---------------------------------------------------------------------------
# Summary metrics
# ---------------------------------------------------------------------------
mcol1, mcol2 = st.columns(2)
mcol1.metric("Platforms", df["OBPP"].nunique())
mcol2.metric("Unique ISIN", df["ISIN"].nunique())

st.divider()

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
comparison = build_grip_comparison(df)

tab_labels = ["📊 Overview", "📋 Detailed Listings"]
if comparison:
    tab_labels.append("⚖️ Grip vs Competition")
tabs = st.tabs(tab_labels)

tab_overview = tabs[0]
tab_detail = tabs[1]
if comparison:
    tab_grip = tabs[2]

# --- Overview -----------------------------------------------------------
with tab_overview:
    st.markdown("#### Listings & yield by platform")
    st.caption(
        "Two separate charts on purpose -- count and yield live on different scales, "
        "so one grouped bar would compress the smaller series into unreadable slivers."
    )
    chart_left, chart_right = st.columns(2)

    with chart_left:
        counts_df = df["OBPP"].value_counts().rename_axis("OBPP").reset_index(name="Listings")
        chart_counts = (
            alt.Chart(counts_df)
            .mark_bar(color=PRIMARY_COLOR)
            .encode(
                x=alt.X("OBPP:N", sort="-y", title=None),
                y=alt.Y("Listings:Q"),
                tooltip=["OBPP", "Listings"],
            )
            .properties(height=300, title="Listings per platform")
        )
        st.altair_chart(chart_counts, use_container_width=True)

    with chart_right:
        ytm_df = (
            pd.DataFrame({"OBPP": df["OBPP"], "YTM": numeric["ytm"]})
            .dropna()
            .groupby("OBPP", as_index=False)["YTM"]
            .mean()
            .sort_values("YTM", ascending=False)
        )
        chart_ytm = (
            alt.Chart(ytm_df)
            .mark_bar(color=ACCENT_COLOR)
            .encode(
                x=alt.X("OBPP:N", sort="-y", title=None),
                y=alt.Y("YTM:Q", title="Avg YTM (%)"),
                tooltip=["OBPP", alt.Tooltip("YTM:Q", format=".2f")],
            )
            .properties(height=300, title="Avg YTM per platform")
        )
        st.altair_chart(chart_ytm, use_container_width=True)

    st.divider()
    st.markdown("#### Yield vs. tenure, by platform")
    st.caption("Where each platform's book sits on the risk/duration curve -- a bond high and to the left is a standout.")
    all_obpps = sorted(df["OBPP"].unique())
    scatter_platforms = st.multiselect(
        "Platforms to compare in this chart",
        options=all_obpps,
        default=all_obpps,
        key="scatter_platforms",
        help="Narrow this down to, say, Grip + one competitor, or any two OBPPs, to compare just those.",
    )
    scatter_df = pd.DataFrame({
        "OBPP": df["OBPP"],
        "Issuer": df["Issuer"],
        "YTM": numeric["ytm"],
        "Tenure": numeric["tenure"],
    }).dropna(subset=["YTM", "Tenure"])
    scatter_df = scatter_df[scatter_df["OBPP"].isin(scatter_platforms)]
    if not scatter_platforms:
        st.caption("Pick at least one platform above to plot.")
    elif not scatter_df.empty:
        chart_scatter = (
            alt.Chart(scatter_df)
            .mark_circle(size=70, opacity=0.65)
            .encode(
                x=alt.X("Tenure:Q", title="Tenure (months)"),
                y=alt.Y("YTM:Q", title="YTM (%)"),
                color=alt.Color("OBPP:N", legend=alt.Legend(title="Platform")),
                tooltip=["OBPP", "Issuer", alt.Tooltip("YTM:Q", format=".2f"), "Tenure"],
            )
            .properties(height=380)
        )
        st.altair_chart(chart_scatter, use_container_width=True)
    else:
        st.caption("Not enough YTM/Tenure data to plot yet.")

    st.divider()
    st.markdown("#### Rating mix by platform")
    st.caption("Credit-quality composition of each platform's live book.")
    rating_df = df.copy()
    rating_df["Rating"] = rating_df["Rating"].astype(str).str.strip()
    rating_df.loc[rating_df["Rating"].isin(["", "nan", "None"]), "Rating"] = "Unrated"
    rating_counts = rating_df.groupby(["OBPP", "Rating"], as_index=False).size().rename(columns={"size": "Count"})
    chart_rating = (
        alt.Chart(rating_counts)
        .mark_bar()
        .encode(
            x=alt.X("OBPP:N", title=None),
            y=alt.Y("Count:Q", title="Listings"),
            color=alt.Color("Rating:N", legend=alt.Legend(title="Rating")),
            tooltip=["OBPP", "Rating", "Count"],
        )
        .properties(height=340)
    )
    st.altair_chart(chart_rating, use_container_width=True)

# --- Detailed Listings ----------------------------------------------------
with tab_detail:
    with st.container(border=True):
        st.markdown("#### Filters")
        f1, f2, f3 = st.columns(3)
        with f1:
            obpp_filter = st.multiselect(
                "OBPP", options=sorted(df["OBPP"].unique()), default=sorted(df["OBPP"].unique())
            )
        with f2:
            ratings = sorted(r for r in df["Rating"].dropna().astype(str).unique() if r and r.lower() != "nan")
            rating_filter = st.multiselect("Rating", options=ratings, default=ratings)
        with f3:
            search = st.text_input("Search Issuer / ISIN", placeholder="e.g. Muthoot, INE...")

        with st.expander("More filters (YTM, tenure, confidence)"):
            af1, af2 = st.columns(2)
            with af1:
                if numeric["ytm"].notna().any():
                    ytm_min, ytm_max = float(numeric["ytm"].min()), float(numeric["ytm"].max())
                    ytm_range = st.slider("YTM (%) range", ytm_min, ytm_max, (ytm_min, ytm_max))
                else:
                    ytm_range = None
                confidence_options = sorted(df["Confidence"].unique(), key=lambda t: CONFIDENCE_ORDER.get(t, 99))
                confidence_filter = st.multiselect("Confidence", options=confidence_options, default=confidence_options)
            with af2:
                if numeric["tenure"].notna().any():
                    t_min, t_max = float(numeric["tenure"].min()), float(numeric["tenure"].max())
                    tenure_range = st.slider("Tenure (months) range", t_min, t_max, (t_min, t_max))
                else:
                    tenure_range = None
                include_blank_rating = st.checkbox("Include bonds with no rating on file", value=True)

    mask = df["OBPP"].isin(obpp_filter) & df["Confidence"].isin(confidence_filter)

    rating_mask = df["Rating"].astype(str).isin(rating_filter)
    if include_blank_rating:
        rating_mask |= df["Rating"].isna() | df["Rating"].astype(str).isin(["", "nan", "None"])
    mask &= rating_mask

    if ytm_range:
        mask &= numeric["ytm"].isna() | numeric["ytm"].between(ytm_range[0], ytm_range[1])
    if tenure_range:
        mask &= numeric["tenure"].isna() | numeric["tenure"].between(tenure_range[0], tenure_range[1])

    if search:
        s = search.lower()
        mask &= (
            df["Issuer"].astype(str).str.lower().str.contains(s, na=False)
            | df["ISIN"].astype(str).str.lower().str.contains(s, na=False)
        )

    filtered = df[mask].copy()

    st.caption(f"Showing {len(filtered)} of {len(df)} bonds · :green[**highlighted**] rows are top-quartile YTM for the current view")
    visible_cols = [c for c in filtered.columns if c not in ("Confidence", "Minimum Investment Amount")]
    st.dataframe(
        style_bonds(filtered, ytm_columns=["YTM (%)"], confidence_col="Confidence"),
        width="stretch",
        height=520,
        hide_index=True,
        column_order=visible_cols,
        column_config={
            "OBPP": st.column_config.TextColumn("OBPP", alignment="left"),
            "Confidence": st.column_config.TextColumn("Confidence", alignment="left"),
            "ISIN": st.column_config.TextColumn("ISIN", alignment="left"),
            "Issuer": st.column_config.TextColumn("Issuer", alignment="left"),
            "Rating": st.column_config.TextColumn("Rating", alignment="left"),
            "YTM (%)": st.column_config.NumberColumn("YTM (%)", format="%.2f%%", alignment="left"),
            "Tenure (Months)": st.column_config.NumberColumn("Tenure (mo)", format="%.0f", alignment="left"),
            "Face Value": st.column_config.NumberColumn("Face Value", format="₹%d", alignment="left"),
            "Minimum Investment Amount": st.column_config.NumberColumn("Min. Investment", format="₹%d", alignment="left"),
        },
    )

    dl1, dl2 = st.columns(2)
    with dl1:
        st.download_button(
            "Download filtered view as CSV",
            data=filtered.to_csv(index=False).encode("utf-8-sig"),
            file_name="consolidated_tracker_filtered.csv",
            mime="text/csv",
            width="stretch",
        )
    with dl2:
        excel_buffer = io.BytesIO()
        with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
            filtered.to_excel(writer, index=False, sheet_name="Consolidated")
        st.download_button(
            "Download filtered view as Excel",
            data=excel_buffer.getvalue(),
            file_name="consolidated_tracker_filtered.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )

# --- Grip vs Competition ---------------------------------------------------
if comparison:
    with tab_grip:
        st.caption(
            "Grip's live deals matched against every other platform's, by ISIN. "
            "'Best OBPP' is whichever competitor offers the highest YTM on that exact bond."
        )
        m = comparison["metrics"]

        gc1, gc2, gc3, gc4 = st.columns(4)
        gc1.metric("Matched bonds (same ISIN)", m["Matched (same ISIN on both)"])
        gc2.metric("Grip-only bonds", m["Grip-only bonds"])
        gc3.metric("OBPP-only bonds", m["OBPP-only bonds"])
        gc4.metric("Grip win rate on matches", m["Grip win rate on matches (YTM >= best competitor)"])

        st.divider()

        matched = comparison["matched"]
        matched_display_cols = [
            "ISIN", "Issuer", "Grip YTM (%)", "Grip Face Value", "Grip Tenure (Months)", "Grip Rating",
            "Best OBPP", "Best OBPP YTM (%)", "OBPP Face Value", "OBPP Tenure (Months)", "OBPP Rating",
            "YTM Delta (Grip - OBPP)",
        ]
        matched_display_cols = [c for c in matched_display_cols if c in matched.columns]

        grip_col_config = {
            "ISIN": st.column_config.TextColumn("ISIN", alignment="left"),
            "Issuer": st.column_config.TextColumn("Issuer", alignment="left"),
            "Grip YTM (%)": st.column_config.NumberColumn("Grip YTM (%)", format="%.2f%%", alignment="left"),
            "Grip Face Value": st.column_config.NumberColumn("Grip Face Value", format="₹%d", alignment="left"),
            "Grip Tenure (Months)": st.column_config.NumberColumn("Grip Tenure (mo)", format="%.0f", alignment="left"),
            "Grip Rating": st.column_config.TextColumn("Grip Rating", alignment="left"),
            "Best OBPP": st.column_config.TextColumn("Best OBPP", alignment="left"),
            "Best OBPP YTM (%)": st.column_config.NumberColumn("Best OBPP YTM (%)", format="%.2f%%", alignment="left"),
            "OBPP Face Value": st.column_config.NumberColumn("OBPP Face Value", format="₹%d", alignment="left"),
            "OBPP Tenure (Months)": st.column_config.NumberColumn("OBPP Tenure (mo)", format="%.0f", alignment="left"),
            "OBPP Rating": st.column_config.TextColumn("OBPP Rating", alignment="left"),
            "YTM Delta (Grip - OBPP)": st.column_config.NumberColumn("YTM Delta", format="%.2f%%", alignment="left"),
        }

        sub_tab1, sub_tab2, sub_tab3 = st.tabs(["Matched bonds (same ISIN)", "Grip-only bonds", "OBPP-only bonds"])
        with sub_tab1:
            st.dataframe(
                style_bonds(matched[matched_display_cols], ytm_columns=["Grip YTM (%)"]),
                width="stretch",
                height=400,
                hide_index=True,
                column_config=grip_col_config,
            )
        with sub_tab2:
            st.dataframe(
                style_bonds(comparison["grip_only"], ytm_columns=["Grip YTM (%)"]),
                width="stretch",
                height=350,
                hide_index=True,
                column_config=grip_col_config,
            )
        with sub_tab3:
            st.dataframe(
                style_bonds(comparison["obpp_only"], ytm_columns=["Best OBPP YTM (%)"]),
                width="stretch",
                height=350,
                hide_index=True,
                column_config=grip_col_config,
            )

        comparison_buffer = io.BytesIO()
        with pd.ExcelWriter(comparison_buffer, engine="openpyxl") as writer:
            comparison["matched"].to_excel(writer, index=False, sheet_name="Matched")
            comparison["grip_only"].to_excel(writer, index=False, sheet_name="Grip only")
            comparison["obpp_only"].to_excel(writer, index=False, sheet_name="OBPP only")
            pd.DataFrame(list(m.items()), columns=["Metric", "Value"]).to_excel(writer, index=False, sheet_name="Metrics")
        st.download_button(
            "Download full Grip comparison as Excel (incl. Grip-only / OBPP-only)",
            data=comparison_buffer.getvalue(),
            file_name="grip_vs_obpp_comparison.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

# --- Data Quality -----------------------------------------------------
st.divider()
with st.expander("ℹ️ Data Quality — which fetches to trust vs. reverify manually", expanded=False):
    st.markdown(
        "**High** — clean, direct fields from an official-style JSON API. "
        "**Good** — mostly direct, but with a documented caveat (an inferred value, or a filter that narrows results). "
        "**Reverify** — no public API/schema exists; the data is reverse-engineered or scraped, spot-checked accurate "
        "at build time but fragile to upstream changes. This reflects *how the data is sourced*, not completeness — "
        "a High-confidence platform can still be missing a field its API simply doesn't expose."
    )
    guide_rows = [
        {"OBPP": platform, "Confidence": tier, "Why": note}
        for platform, (tier, note) in sorted(CONFIDENCE.items(), key=lambda kv: CONFIDENCE_ORDER.get(kv[1][0], 99))
    ]
    st.dataframe(
        style_bonds(pd.DataFrame(guide_rows), confidence_col="Confidence"),
        width="stretch",
        hide_index=True,
        column_config={
            "OBPP": st.column_config.TextColumn("OBPP", alignment="left"),
            "Confidence": st.column_config.TextColumn("Confidence", alignment="left"),
            "Why": st.column_config.TextColumn("Why", alignment="left", width="large"),
        },
    )
