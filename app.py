import io
import time
from datetime import datetime

import altair as alt
import pandas as pd
import streamlit as st

import fetchers
from fetchers import COLUMNS, CONFIDENCE, FETCHERS, build_grip_comparison, parse_grip_csv_upload

# ---------------------------------------------------------------------------
# Look & feel -- mirrors the palette in .streamlit/config.toml
# ---------------------------------------------------------------------------
PRIMARY_COLOR = "#0F4C81"   # deep blue -- listing counts
ACCENT_COLOR = "#12A594"    # teal -- yield

CONFIDENCE_COLOR = {"High": "green", "Good": "orange", "Reverify": "red"}
CONFIDENCE_ORDER = {"High": 0, "Good": 1, "Reverify": 2}

# Bond yields move day to day; past this age the numbers on screen are worth
# a re-fetch before you quote them to anyone.
STALE_WARN_HOURS = 6
STALE_ERROR_HOURS = 24

st.set_page_config(page_title="Bond Competition Tracker", page_icon="📈", layout="wide")

if "data" not in st.session_state:
    st.session_state.data = None
if "last_fetched" not in st.session_state:
    st.session_state.last_fetched = None
if "fetch_summary" not in st.session_state:
    st.session_state.fetch_summary = {}
if "data_source" not in st.session_state:
    st.session_state.data_source = None
if "last_upload_signature" not in st.session_state:
    st.session_state.last_upload_signature = None
if "last_grip_upload_signature" not in st.session_state:
    st.session_state.last_grip_upload_signature = None
if "new_since_last_fetch" not in st.session_state:
    st.session_state.new_since_last_fetch = None

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

    st.divider()
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
                        st.session_state.fetch_summary = {}
                        st.session_state.new_since_last_fetch = None
                        st.session_state.last_upload_signature = upload_signature
                        st.rerun()
                except Exception as e:
                    st.error(f"Couldn't read that file: {e}")

    with st.expander("Or upload Grip's live deals"):
        st.caption(
            "'Grip' is already one of the platforms above and fetches automatically from "
            "GRIP_METABASE_URL if that secret is set. Use this instead to refresh just "
            "Grip's numbers without re-fetching every OBPP, or if the secret isn't set yet."
        )
        grip_csv_file = st.file_uploader("Upload the Metabase 'Platform Live Deals' CSV", type=["csv"])
        if grip_csv_file is not None:
            grip_signature = f"{grip_csv_file.name}-{grip_csv_file.size}"
            if st.session_state.last_grip_upload_signature != grip_signature:
                try:
                    grip_rows = parse_grip_csv_upload(grip_csv_file)
                    if not grip_rows:
                        st.warning("No live 'Bonds' rows found in that CSV.")
                    else:
                        base_df_full = st.session_state.data
                        if base_df_full is not None and not base_df_full.empty:
                            prior_grip_isins = set(
                                base_df_full.loc[base_df_full["OBPP"] == "Grip", "ISIN"].astype(str)
                            )
                            base_df = base_df_full[base_df_full["OBPP"] != "Grip"]
                        else:
                            prior_grip_isins = set()
                            base_df = pd.DataFrame(columns=COLUMNS)
                        grip_df_new = pd.DataFrame(grip_rows, columns=COLUMNS)
                        new_grip_isins = set(grip_df_new["ISIN"].astype(str)) - prior_grip_isins
                        st.session_state.new_since_last_fetch = len(new_grip_isins)
                        st.session_state.data = pd.concat([base_df, grip_df_new], ignore_index=True)
                        if st.session_state.last_fetched is None:
                            st.session_state.last_fetched = datetime.now()
                            st.session_state.data_source = "Fetched live"
                        st.session_state.last_grip_upload_signature = grip_signature
                        st.rerun()
                except Exception as e:
                    st.error(f"Couldn't read that CSV: {e}")

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
                    "likely blocked/rate-limited upstream, not necessarily a real 'no bonds'. "
                    "Check the terminal for the printed error, then retry."
                )
            else:
                st.write(f":red[✗] {platform}: {info['error']}")

# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
if fetch_clicked:
    if not selected_platforms:
        st.sidebar.error("Select at least one platform.")
    else:
        all_rows = []
        summary = {}
        status_box = st.status("Fetching bond data...", expanded=True)
        for platform in selected_platforms:
            status_box.write(f"Fetching {platform}...")
            start = time.time()
            try:
                rows = FETCHERS[platform]()
                elapsed = time.time() - start
                all_rows.extend(rows)
                if rows:
                    summary[platform] = {"status": "ok", "rows": len(rows), "elapsed": elapsed}
                    status_box.write(f"✓ {platform}: {len(rows)} rows ({elapsed:.1f}s)")
                elif platform == "Grip":
                    reason = getattr(fetchers, "GRIP_LAST_ERROR", None) or "GRIP_METABASE_URL isn't set in secrets"
                    summary[platform] = {"status": "empty", "rows": 0, "elapsed": elapsed, "error": reason}
                    status_box.write(f"⚠ {platform}: 0 rows — {reason}")
                else:
                    summary[platform] = {"status": "empty", "rows": 0, "elapsed": elapsed}
                    status_box.write(
                        f"⚠ {platform}: 0 rows ({elapsed:.1f}s) — likely blocked/rate-limited, see terminal for details"
                    )
            except Exception as e:
                elapsed = time.time() - start
                summary[platform] = {"status": "error", "error": str(e), "elapsed": elapsed}
                status_box.write(f"✗ {platform}: {e}")

        new_df = pd.DataFrame(all_rows, columns=COLUMNS)
        existing_df = st.session_state.data
        if existing_df is not None and not existing_df.empty:
            prior_isins = set(existing_df.loc[existing_df["OBPP"].isin(selected_platforms), "ISIN"].astype(str))
            kept_df = existing_df[~existing_df["OBPP"].isin(selected_platforms)]
            df = pd.concat([kept_df, new_df], ignore_index=True)
        else:
            prior_isins = set()
            df = new_df
        st.session_state.new_since_last_fetch = len(set(new_df["ISIN"].astype(str)) - prior_isins)

        st.session_state.data = df
        st.session_state.last_fetched = datetime.now()
        st.session_state.data_source = "Fetched live"
        st.session_state.fetch_summary = {**st.session_state.fetch_summary, **summary}
        status_box.update(
            label=f"Done — {len(new_df)} bonds from {len(selected_platforms)} platform(s), {len(df)} total",
            state="complete",
        )
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
mcol1, mcol2, mcol3, mcol4, mcol5 = st.columns(5)
mcol1.metric("Total bonds", len(df))
mcol2.metric("Platforms", df["OBPP"].nunique())
mcol3.metric("Avg YTM (%)", f"{numeric['ytm'].mean():.2f}" if numeric["ytm"].notna().any() else "—")
mcol4.metric("Unique issuers", df["Issuer"].nunique())
new_since = st.session_state.new_since_last_fetch
mcol5.metric(
    "New since last scrape",
    "—" if new_since is None else new_since,
    help="ISINs that weren't present the last time these platforms were fetched or uploaded.",
)

st.divider()

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
comparison = build_grip_comparison(df)

tab_labels = ["📊 Overview", "📋 Detailed Listings"]
if comparison:
    tab_labels.append("⚖️ Grip vs Competition")
tab_labels.append("ℹ️ Data Quality")
tabs = st.tabs(tab_labels)

tab_overview = tabs[0]
tab_detail = tabs[1]
next_idx = 2
if comparison:
    tab_grip = tabs[next_idx]
    next_idx += 1
tab_quality = tabs[next_idx]

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

# --- Detailed Listings ----------------------------------------------------
with tab_detail:
    with st.container(border=True):
        st.markdown("#### Filters")
        f1, f2, f3, f4 = st.columns(4)

        with f1:
            obpp_filter = st.multiselect(
                "OBPP", options=sorted(df["OBPP"].unique()), default=sorted(df["OBPP"].unique())
            )
            search = st.text_input("Search Issuer / ISIN")

        with f2:
            ratings = sorted(r for r in df["Rating"].dropna().astype(str).unique() if r and r.lower() != "nan")
            rating_filter = st.multiselect("Rating", options=ratings, default=ratings)
            include_blank_rating = st.checkbox("Include bonds with no rating on file", value=True)

        with f4:
            confidence_options = sorted(df["Confidence"].unique(), key=lambda t: CONFIDENCE_ORDER.get(t, 99))
            confidence_filter = st.multiselect("Confidence", options=confidence_options, default=confidence_options)

        with f3:
            if numeric["ytm"].notna().any():
                ytm_min, ytm_max = float(numeric["ytm"].min()), float(numeric["ytm"].max())
                ytm_range = st.slider("YTM (%) range", ytm_min, ytm_max, (ytm_min, ytm_max))
            else:
                ytm_range = None
            if numeric["tenure"].notna().any():
                t_min, t_max = float(numeric["tenure"].min()), float(numeric["tenure"].max())
                tenure_range = st.slider("Tenure (months) range", t_min, t_max, (t_min, t_max))
            else:
                tenure_range = None

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

    st.caption(f"Showing {len(filtered)} of {len(df)} bonds")
    st.dataframe(
        filtered,
        width="stretch",
        height=520,
        hide_index=True,
        column_config={
            "YTM (%)": st.column_config.NumberColumn("YTM (%)", format="%.2f%%"),
            "Tenure (Months)": st.column_config.NumberColumn("Tenure (mo)", format="%.0f"),
            "Face Value": st.column_config.NumberColumn("Face Value", format="₹%d"),
            "Minimum Investment Amount": st.column_config.NumberColumn("Min. Investment", format="₹%d"),
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

        gc5, gc6, gc7, gc8 = st.columns(4)
        gc5.metric("Avg YTM delta (Grip − best OBPP)", m["Avg YTM delta on matches (Grip - best competitor)"])
        gc6.metric("Grip avg YTM (whole book)", m["Grip avg YTM (entire live book)"])
        gc7.metric("OBPP avg YTM (all competitors)", m["OBPP avg YTM (all competitors combined)"])
        gc8.metric("Issuer coverage (Grip vs OBPPs)", f"{m['Unique issuers on Grip']} / {m['Unique issuers across OBPPs']}")

        st.divider()

        def pct_config(*column_names):
            return {name: st.column_config.NumberColumn(name, format="%.2f%%") for name in column_names}

        sub_tab1, sub_tab2, sub_tab3 = st.tabs(["Matched bonds (same ISIN)", "Grip-only bonds", "OBPP-only bonds"])
        with sub_tab1:
            st.dataframe(
                comparison["matched"], width="stretch", height=350, hide_index=True,
                column_config=pct_config("Grip YTM (%)", "Best OBPP YTM (%)", "YTM Delta (Grip - OBPP)"),
            )
        with sub_tab2:
            st.dataframe(
                comparison["grip_only"], width="stretch", height=350, hide_index=True,
                column_config=pct_config("Grip YTM (%)"),
            )
        with sub_tab3:
            st.dataframe(
                comparison["obpp_only"], width="stretch", height=350, hide_index=True,
                column_config=pct_config("Best OBPP YTM (%)"),
            )

        if not comparison["matched"].empty:
            with st.expander("YTM delta by bond (matched, Grip − best OBPP)"):
                st.bar_chart(comparison["matched"].set_index("ISIN")["YTM Delta (Grip - OBPP)"])

        comparison_buffer = io.BytesIO()
        with pd.ExcelWriter(comparison_buffer, engine="openpyxl") as writer:
            comparison["matched"].to_excel(writer, index=False, sheet_name="Matched")
            comparison["grip_only"].to_excel(writer, index=False, sheet_name="Grip only")
            comparison["obpp_only"].to_excel(writer, index=False, sheet_name="OBPP only")
            pd.DataFrame(list(m.items()), columns=["Metric", "Value"]).to_excel(writer, index=False, sheet_name="Metrics")
        st.download_button(
            "Download Grip comparison as Excel",
            data=comparison_buffer.getvalue(),
            file_name="grip_vs_obpp_comparison.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

# --- Data Quality -----------------------------------------------------
with tab_quality:
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
    st.dataframe(pd.DataFrame(guide_rows), width="stretch", hide_index=True)
