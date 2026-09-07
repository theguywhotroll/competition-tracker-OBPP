import io
import time
from datetime import datetime

import pandas as pd
import streamlit as st

from fetchers import COLUMNS, CONFIDENCE, FETCHERS

CONFIDENCE_COLOR = {"High": "green", "Good": "orange", "Reverify": "red"}
CONFIDENCE_ORDER = {"High": 0, "Good": 1, "Reverify": 2}

st.set_page_config(page_title="Bond Competition Tracker", layout="wide")

if "data" not in st.session_state:
    st.session_state.data = None
if "last_fetched" not in st.session_state:
    st.session_state.last_fetched = None
if "fetch_summary" not in st.session_state:
    st.session_state.fetch_summary = {}

st.title("Bond Competition Tracker")
st.caption(
    "Pulls live bond listings (ISIN, Issuer, YTM, Rating, Tenure, Face Value, "
    "Minimum Investment) from competing OBPPs into one comparable view."
)

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
        "than the others (it also rate-limits if fetched too frequently).",
    )

    fetch_clicked = st.button("Fetch Latest Data", type="primary", use_container_width=True)

    if st.session_state.last_fetched:
        st.caption(f"Last fetched: {st.session_state.last_fetched.strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        st.caption("No data fetched yet.")

    if st.session_state.fetch_summary:
        st.divider()
        st.subheader("Last fetch summary")
        for platform, info in st.session_state.fetch_summary.items():
            if info["status"] == "ok":
                st.write(f":green[✓] {platform}: {info['rows']} rows ({info['elapsed']:.1f}s)")
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
                else:
                    summary[platform] = {"status": "empty", "rows": 0, "elapsed": elapsed}
                    status_box.write(
                        f"⚠ {platform}: 0 rows ({elapsed:.1f}s) — likely blocked/rate-limited, see terminal for details"
                    )
            except Exception as e:
                elapsed = time.time() - start
                summary[platform] = {"status": "error", "error": str(e), "elapsed": elapsed}
                status_box.write(f"✗ {platform}: {e}")

        df = pd.DataFrame(all_rows, columns=COLUMNS)
        st.session_state.data = df
        st.session_state.last_fetched = datetime.now()
        st.session_state.fetch_summary = summary
        status_box.update(label=f"Done — {len(df)} bonds from {len(selected_platforms)} platform(s)", state="complete")
        st.rerun()

# ---------------------------------------------------------------------------
# Main view
# ---------------------------------------------------------------------------
df = st.session_state.data

if df is None or df.empty:
    st.info("Click **Fetch Latest Data** in the sidebar to load bond listings from all platforms.")
    st.stop()

if "Confidence" not in df.columns:
    df["Confidence"] = df["OBPP"].map(lambda o: CONFIDENCE.get(o, ("Unknown", ""))[0])
    df = df[["OBPP", "Confidence"] + [c for c in df.columns if c not in ("OBPP", "Confidence")]]

with st.expander("Data confidence guide — which fetches to trust vs. reverify manually"):
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
    st.dataframe(pd.DataFrame(guide_rows), use_container_width=True, hide_index=True)

# Numeric coercions used for filtering/summary (original columns kept as-is for display)
numeric = pd.DataFrame(index=df.index)
numeric["ytm"] = pd.to_numeric(df["YTM (%)"], errors="coerce")
numeric["tenure"] = pd.to_numeric(df["Tenure (Months)"], errors="coerce")
numeric["face_value"] = pd.to_numeric(df["Face Value"], errors="coerce")
numeric["min_investment"] = pd.to_numeric(df["Minimum Investment Amount"], errors="coerce")

# ---------------------------------------------------------------------------
# Summary metrics
# ---------------------------------------------------------------------------
col1, col2, col3, col4 = st.columns(4)
col1.metric("Total bonds", len(df))
col2.metric("Platforms", df["OBPP"].nunique())
col3.metric("Avg YTM (%)", f"{numeric['ytm'].mean():.2f}" if numeric["ytm"].notna().any() else "—")
col4.metric("Unique issuers", df["Issuer"].nunique())

with st.expander("Average YTM by platform"):
    avg_ytm = (
        pd.DataFrame({"OBPP": df["OBPP"], "ytm": numeric["ytm"]})
        .dropna()
        .groupby("OBPP")["ytm"]
        .mean()
        .sort_values(ascending=False)
    )
    st.bar_chart(avg_ytm)

st.divider()

# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------
st.subheader("Filters")
f1, f2, f3, f4 = st.columns(4)

with f1:
    obpp_filter = st.multiselect("OBPP", options=sorted(df["OBPP"].unique()), default=sorted(df["OBPP"].unique()))
    search = st.text_input("Search Issuer / ISIN")

with f2:
    ratings = sorted(r for r in df["Rating"].astype(str).unique() if r and r != "nan")
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
    rating_mask |= df["Rating"].astype(str).isin(["", "nan", "None"])
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
st.dataframe(filtered, use_container_width=True, height=520)

# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------
dl1, dl2 = st.columns(2)
with dl1:
    st.download_button(
        "Download filtered view as CSV",
        data=filtered.to_csv(index=False).encode("utf-8-sig"),
        file_name="consolidated_tracker_filtered.csv",
        mime="text/csv",
        use_container_width=True,
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
        use_container_width=True,
    )
