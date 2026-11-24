"""GPTW Employee Survey - AI Feedback Analysis (Snowflake Cortex prototype)."""
import json
import os

import altair as alt
import pandas as pd
import streamlit as st

st.set_page_config(page_title="GPTW Survey AI Insights", page_icon=":material/insights:", layout="wide")

DB = "GPTW_DEMO.SURVEY"
LLM = "claude-sonnet-4-5"
SENT_COLORS = alt.Scale(domain=["positive", "neutral", "negative"], range=["#2E9E5B", "#B0B7C3", "#D64545"])
DEMOS = {
    "Manager level": "MANAGER_LEVEL",
    "Gender": "GENDER",
    "Age band": "AGE_BAND",
    "Tenure": "TENURE_BAND",
    "Exec confidence": "EXEC_CONFIDENCE",
    "Innovation opportunities": "INNOVATION_OPPORTUNITIES",
    "Company": "COMPANY_ID",
}
ORDER = {
    "AGE_BAND": ["25 years or younger", "26 years to 34 years", "35 years to 44 years", "45 years to 54 years", "55 years or older"],
    "TENURE_BAND": ["Less than 2 years", "2 years to 5 years", "6 years to 10 years", "11 years to 15 years", "16 years to 20 years", "Over 20 years"],
    "EXEC_CONFIDENCE": ["A great deal", "A fair amount", "Some", "Very little or none"],
    "INNOVATION_OPPORTUNITIES": ["A lot", "Some", "Just a few"],
}


# ---------------------------------------------------------------- data access
@st.cache_resource
def get_session():
    try:
        from snowflake.snowpark.context import get_active_session
        return get_active_session()
    except Exception:
        from snowflake.snowpark import Session
        return (
            Session.builder.config("connection_name", os.getenv("SNOWFLAKE_CONNECTION_NAME", "default"))
            .config("warehouse", "GPTW_WH").config("database", "GPTW_DEMO").config("schema", "SURVEY")
            .create()
        )


@st.cache_data(ttl=3600, show_spinner="Loading AI-enriched survey data from Snowflake...")
def load_data():
    s = get_session()
    comments = s.sql(f"SELECT * FROM {DB}.V_COMMENTS").to_pandas()
    themes = s.sql(f"SELECT * FROM {DB}.COMMENT_THEMES").to_pandas()
    glossary = s.sql(f"SELECT * FROM {DB}.THEME_GLOSSARY").to_pandas()
    return comments, themes, glossary


def ai_complete(prompt: str, max_tokens: int = 1500) -> str:
    """Run Cortex AI_COMPLETE inside Snowflake (bound parameters, no data leaves the account)."""
    row = get_session().sql(
        "SELECT AI_COMPLETE(model => ?, prompt => ?, model_parameters => {'temperature': 0.2, 'max_tokens': " + str(int(max_tokens)) + "}) AS R",
        params=[LLM, prompt],
    ).collect()[0]
    out = row["R"]
    try:  # AI_COMPLETE may return a JSON-quoted string
        return json.loads(out) if out.startswith('"') else out
    except Exception:
        return out


comments, themes, glossary = load_data()

# ---------------------------------------------------------------- sidebar filters
with st.sidebar:
    st.markdown("### :material/filter_alt: Filters")
    q = st.segmented_control("Question", ["Both", "Great place", "Improve"], default="Both")
    filters = {}
    for label, col in DEMOS.items():
        opts = ORDER.get(col) or sorted(comments[col].dropna().unique().tolist())
        filters[col] = st.multiselect(label, opts, placeholder="All")
    st.caption("Industry and employee-size filters will be available once those attributes are added to the response feed.")
    st.divider()
    st.caption(f"Model: `{LLM}` via Snowflake Cortex. All data stays inside Snowflake.")


def apply(df):
    if q == "Great place":
        df = df[df.QUESTION_TYPE == "GREAT_PLACE"]
    elif q == "Improve":
        df = df[df.QUESTION_TYPE == "IMPROVE"]
    for col, vals in filters.items():
        if vals:
            df = df[df[col].isin(vals)]
    return df


c = apply(comments)
t = apply(themes)
n_resp = c.LOGIN_ID.nunique()
if n_resp == 0:
    st.warning("No responses match the current filters.")
    st.stop()


def theme_health(td: pd.DataFrame, base: int) -> pd.DataFrame:
    g = td.groupby(["GPTW_DIMENSION", "GPTW_THEME", "HASHTAG"])
    out = g.agg(
        RESPONDENTS=("LOGIN_ID", "nunique"),
        MENTIONS=("COMMENT_ID", "count"),
        POS=("THEME_SENTIMENT", lambda s: (s == "positive").sum()),
        NEG=("THEME_SENTIMENT", lambda s: (s == "negative").sum()),
    ).reset_index()
    out["PREVALENCE_PCT"] = (100 * out.RESPONDENTS / base).round(1)
    out["NET_SENTIMENT"] = (100 * (out.POS - out.NEG) / out.MENTIONS).round(0)
    return out.sort_values("RESPONDENTS", ascending=False)


health = theme_health(t, n_resp)
specific = health[health.GPTW_DIMENSION != "General"]

# ---------------------------------------------------------------- header
st.markdown("## :material/insights: Employee Survey Feedback: AI Theme & Sentiment Analysis")
st.caption("Great Place To Work sample data · Cortex AI classifies every comment into the GPTW hashtag → theme → dimension taxonomy with per-theme sentiment and evidence.")

with st.container(horizontal=True):
    st.metric("Respondents", f"{n_resp:,}", border=True)
    st.metric("Comments analysed", f"{len(c):,}", border=True)
    st.metric("Theme mentions", f"{len(t):,}", border=True)
    st.metric("Avg themes / comment", f"{c.NUM_THEMES.mean():.1f}", border=True)
    st.metric("Multi-issue comments", f"{(c.NUM_THEMES > 1).mean():.0%}", border=True)
    st.metric("Avg sentiment score", f"{c.SENTIMENT_SCORE.mean():+.2f}", border=True)
    st.metric("Risk flags", f"{(c.RISK_FLAG != 'none').sum()}", border=True)

tabs = st.tabs([
    ":material/dashboard: Overview", ":material/sell: Themes", ":material/groups: Demographics",
    ":material/radar: Risk radar", ":material/summarize: Leadership brief", ":material/chat: Ask the data",
    ":material/table_rows: Comments",
])

# ---------------------------------------------------------------- overview
with tabs[0]:
    left, right = st.columns(2)
    with left, st.container(border=True):
        st.markdown("**Sentiment by GPTW dimension** (theme-level mentions)")
        dim = t.groupby(["GPTW_DIMENSION", "THEME_SENTIMENT"]).size().reset_index(name="N")
        st.altair_chart(
            alt.Chart(dim).mark_bar().encode(
                y=alt.Y("GPTW_DIMENSION:N", sort="-x", title=None),
                x=alt.X("N:Q", stack="normalize", title="Share of mentions", axis=alt.Axis(format="%")),
                color=alt.Color("THEME_SENTIMENT:N", scale=SENT_COLORS, title="Sentiment"),
                tooltip=["GPTW_DIMENSION", "THEME_SENTIMENT", "N"],
            ).properties(height=300),
            use_container_width=True,
        )
    with right, st.container(border=True):
        st.markdown("**Overall comment sentiment by question**")
        qs = c.groupby(["QUESTION_TYPE", "OVERALL_SENTIMENT"]).size().reset_index(name="N")
        st.altair_chart(
            alt.Chart(qs).mark_bar().encode(
                x=alt.X("QUESTION_TYPE:N", title=None),
                y=alt.Y("N:Q", title="Comments"),
                color=alt.Color("OVERALL_SENTIMENT:N", title="Sentiment",
                                scale=alt.Scale(domain=["positive", "mixed", "neutral", "negative"],
                                                range=["#2E9E5B", "#F2B544", "#B0B7C3", "#D64545"])),
                tooltip=["QUESTION_TYPE", "OVERALL_SENTIMENT", "N"],
            ).properties(height=300),
            use_container_width=True,
        )

    s_col, w_col = st.columns(2)
    top_pos = specific.sort_values("POS", ascending=False).head(10)
    top_neg = specific.sort_values("NEG", ascending=False).head(10)
    for col, df, title, field, color in [
        (s_col, top_pos, ":green[:material/thumb_up:] Top strengths", "POS", "#2E9E5B"),
        (w_col, top_neg, ":red[:material/thumb_down:] Top improvement areas", "NEG", "#D64545"),
    ]:
        with col, st.container(border=True):
            st.markdown(f"**{title}** (respondents mentioning)")
            st.altair_chart(
                alt.Chart(df).mark_bar(color=color).encode(
                    y=alt.Y("HASHTAG:N", sort="-x", title=None),
                    x=alt.X(f"{field}:Q", title="Mentions"),
                    tooltip=["HASHTAG", "GPTW_THEME", "GPTW_DIMENSION", field, "PREVALENCE_PCT"],
                ).properties(height=320),
                use_container_width=True,
            )

# ---------------------------------------------------------------- themes
with tabs[1]:
    with st.container(border=True):
        st.markdown("**Theme prevalence & net sentiment** (prevalence = % of respondents in the current filter who mention the theme)")
        st.dataframe(
            health[["GPTW_DIMENSION", "GPTW_THEME", "HASHTAG", "RESPONDENTS", "PREVALENCE_PCT", "POS", "NEG", "NET_SENTIMENT"]],
            hide_index=True, use_container_width=True, height=360,
            column_config={
                "PREVALENCE_PCT": st.column_config.ProgressColumn("Prevalence %", format="%.1f%%", min_value=0, max_value=float(max(health.PREVALENCE_PCT.max(), 1))),
                "NET_SENTIMENT": st.column_config.NumberColumn("Net sentiment", format="%+d", help="(% positive − % negative) of mentions"),
                "POS": "Positive", "NEG": "Negative", "GPTW_DIMENSION": "Dimension", "GPTW_THEME": "Theme", "HASHTAG": "Hashtag",
            },
        )
    with st.container(border=True):
        tag = st.selectbox("Drill into a hashtag", health.HASHTAG.tolist())
        g = glossary[glossary.HASHTAG == tag].iloc[0]
        st.markdown(f"**{tag}** · {g.GPTW_DIMENSION} › {g.GPTW_THEME}  \n_{g.DESCRIPTION}_")
        with st.expander("Related GPTW statements"):
            st.text(g.RELATED_STATEMENTS)
        sent = st.segmented_control("Sentiment", ["negative", "positive", "neutral"], default="negative", key="drill_sent")
        ev = t[(t.HASHTAG == tag) & ((t.THEME_SENTIMENT == sent) if sent else True)].merge(
            c[["COMMENT_ID", "COMMENT_TEXT"]], on="COMMENT_ID")
        st.dataframe(ev[["EVIDENCE", "COMMENT_TEXT", "QUESTION_TYPE", "MANAGER_LEVEL", "GENDER", "TENURE_BAND"]],
                     hide_index=True, use_container_width=True,
                     column_config={"EVIDENCE": st.column_config.TextColumn("AI evidence", width="medium"),
                                    "COMMENT_TEXT": st.column_config.TextColumn("Full comment", width="large")})

# ---------------------------------------------------------------- demographics
with tabs[2]:
    a, b, cc = st.columns([2, 2, 1])
    demo_label = a.selectbox("Break down by", list(DEMOS)[:-1])
    level = b.segmented_control("Level", ["Hashtag", "GPTW_THEME", "GPTW_DIMENSION"], default="GPTW_THEME",
                                format_func=lambda x: {"Hashtag": "Hashtag", "GPTW_THEME": "Theme", "GPTW_DIMENSION": "Dimension"}[x])
    sent_f = cc.selectbox("Sentiment", ["negative", "positive", "all"])
    dcol = DEMOS[demo_label]
    lvl = "HASHTAG" if level == "Hashtag" else (level or "GPTW_THEME")

    base = c.groupby(dcol).LOGIN_ID.nunique().rename("BASE")
    td = t if sent_f == "all" else t[t.THEME_SENTIMENT == sent_f]
    td = td[td.GPTW_DIMENSION != "General"]
    top_items = td.groupby(lvl).LOGIN_ID.nunique().nlargest(15).index
    pv = (td[td[lvl].isin(top_items)].groupby([lvl, dcol]).LOGIN_ID.nunique().rename("N").reset_index()
          .merge(base, left_on=dcol, right_index=True))
    pv["PCT"] = (100 * pv.N / pv.BASE).round(1)
    sort_x = [x for x in ORDER.get(dcol, sorted(pv[dcol].unique())) if x in pv[dcol].unique()]

    with st.container(border=True):
        st.markdown(f"**% of respondents mentioning each {'theme' if lvl != 'GPTW_DIMENSION' else 'dimension'} ({sent_f}) by {demo_label.lower()}**")
        heat = alt.Chart(pv).encode(
            x=alt.X(f"{dcol}:N", sort=sort_x, title=None, axis=alt.Axis(labelAngle=-20, labelLimit=180)),
            y=alt.Y(f"{lvl}:N", sort=list(top_items), title=None),
        )
        st.altair_chart(
            (heat.mark_rect().encode(
                color=alt.Color("PCT:Q", title="% resp.", scale=alt.Scale(scheme="reds" if sent_f == "negative" else "greens" if sent_f == "positive" else "blues")),
                tooltip=[dcol, lvl, "N", "BASE", "PCT"])
             + heat.mark_text(fontSize=11).encode(text=alt.Text("PCT:Q", format=".0f"))
             ).properties(height=max(320, 28 * len(top_items))),
            use_container_width=True,
        )
        st.caption("Group sizes: " + " · ".join(f"{k}: {v}" for k, v in base.items()))

# ---------------------------------------------------------------- risk radar
with tabs[3]:
    with st.container(border=True):
        st.markdown("**Theme risk quadrant**: high prevalence and negative net sentiment = priority action areas")
        quad = specific[specific.MENTIONS >= 3].copy()
        quad["ZONE"] = pd.cut(quad.NET_SENTIMENT, [-101, -20, 20, 101], labels=["Risk", "Watch", "Strength"])
        base_ch = alt.Chart(quad)
        pts = base_ch.mark_circle(opacity=0.8, stroke="white").encode(
            x=alt.X("PREVALENCE_PCT:Q", title="Prevalence (% of respondents)"),
            y=alt.Y("NET_SENTIMENT:Q", title="Net sentiment", scale=alt.Scale(domain=[-100, 100])),
            size=alt.Size("MENTIONS:Q", legend=None, scale=alt.Scale(range=[60, 900])),
            color=alt.Color("ZONE:N", scale=alt.Scale(domain=["Risk", "Watch", "Strength"], range=["#D64545", "#F2B544", "#2E9E5B"])),
            tooltip=["HASHTAG", "GPTW_THEME", "GPTW_DIMENSION", "PREVALENCE_PCT", "NET_SENTIMENT", "POS", "NEG"],
        )
        labels = base_ch.transform_filter("datum.PREVALENCE_PCT >= 3").mark_text(dy=-12, fontSize=10).encode(
            x="PREVALENCE_PCT:Q", y="NET_SENTIMENT:Q", text="HASHTAG:N")
        rule = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(strokeDash=[4, 4], color="grey").encode(y="y:Q")
        st.altair_chart((pts + labels + rule).properties(height=460), use_container_width=True)

    l, r = st.columns(2)
    with l, st.container(border=True):
        st.markdown("**Demographic hotspots**: groups where a complaint is ≥1.5× more common than average (group size ≥ 15)")
        neg = t[(t.THEME_SENTIMENT == "negative") & (t.GPTW_DIMENSION != "General")]
        overall = neg.groupby("HASHTAG").LOGIN_ID.nunique() / n_resp
        rows = []
        for label, col in list(DEMOS.items())[:-1]:
            gb = c.groupby(col).LOGIN_ID.nunique()
            for (tag_, grp), n in neg.groupby(["HASHTAG", col]).LOGIN_ID.nunique().items():
                if gb.get(grp, 0) >= 15 and n >= 3:
                    rate = n / gb[grp]
                    lift = rate / overall[tag_]
                    if lift >= 1.5:
                        rows.append({"Segment": f"{label}: {grp}", "Hashtag": tag_, "Rate %": round(100 * rate, 1),
                                     "Overall %": round(100 * overall[tag_], 1), "Lift": round(lift, 1), "N": n})
        hs = pd.DataFrame(rows)
        if hs.empty:
            st.info("No hotspots for the current filter.")
        else:
            st.dataframe(hs.sort_values(["Lift", "N"], ascending=False).head(25), hide_index=True, use_container_width=True,
                         column_config={"Lift": st.column_config.NumberColumn(format="%.1f×")})
    with r, st.container(border=True):
        st.markdown("**AI risk flags** (sensitive comments that need follow-up)")
        rf = c[c.RISK_FLAG != "none"]
        if rf.empty:
            st.info("No flagged comments for the current filter.")
        else:
            st.altair_chart(alt.Chart(rf.groupby("RISK_FLAG").size().reset_index(name="N")).mark_bar(color="#D64545").encode(
                x=alt.X("N:Q", title="Comments"), y=alt.Y("RISK_FLAG:N", sort="-x", title=None)).properties(height=160),
                use_container_width=True)
            st.dataframe(rf[["RISK_FLAG", "AI_SUMMARY", "COMMENT_TEXT", "MANAGER_LEVEL", "TENURE_BAND"]], hide_index=True,
                         use_container_width=True, height=260)


# ---------------------------------------------------------------- shared LLM context
def build_context(max_quotes: int = 120) -> str:
    stats = health.head(40)[["GPTW_DIMENSION", "GPTW_THEME", "HASHTAG", "RESPONDENTS", "PREVALENCE_PCT", "POS", "NEG", "NET_SENTIMENT"]]
    demo_lines = []
    for label, col in list(DEMOS.items())[:-1]:
        gb = c.groupby(col).agg(N=("LOGIN_ID", "nunique"), SCORE=("SENTIMENT_SCORE", "mean"))
        negs = t[t.THEME_SENTIMENT == "negative"].groupby(col).HASHTAG.agg(lambda s: ", ".join(s.value_counts().head(3).index))
        for grp, row in gb.iterrows():
            demo_lines.append(f"{label}={grp}: n={row.N}, avg_sentiment={row.SCORE:+.2f}, top_complaints=[{negs.get(grp, '')}]")
    quotes = (t.merge(c[["COMMENT_ID", "SENTIMENT_SCORE"]], on="COMMENT_ID")
              .sort_values("SENTIMENT_SCORE").groupby("HASHTAG").head(3).head(max_quotes))
    q_lines = [f"- [{r.HASHTAG}|{r.THEME_SENTIMENT}|{r.MANAGER_LEVEL}|{r.TENURE_BAND}] \"{r.EVIDENCE}\"" for r in quotes.itertuples()]
    active = {k: v for k, v in filters.items() if v}
    return (
        f"FILTER: question={q}, {json.dumps(active) if active else 'all respondents'}; respondents={n_resp}, comments={len(c)}\n"
        f"RISK FLAGS: {c[c.RISK_FLAG != 'none'].RISK_FLAG.value_counts().to_dict()}\n\n"
        f"THEME STATS (prevalence = % of respondents; net = %pos - %neg mentions):\n{stats.to_csv(index=False)}\n"
        f"DEMOGRAPHIC SUMMARY:\n" + "\n".join(demo_lines) + "\n\nEVIDENCE QUOTES:\n" + "\n".join(q_lines)
    )


# ---------------------------------------------------------------- leadership brief
with tabs[4]:
    st.markdown("Generate an executive brief for the **current filter selection** with Cortex AI.")
    key = json.dumps({"q": q, **filters}, sort_keys=True)
    if st.button("Generate leadership brief", icon=":material/auto_awesome:", type="primary"):
        with st.spinner("Cortex is writing the brief..."):
            st.session_state.setdefault("briefs", {})[key] = ai_complete(
                "You are a Great Place To Work culture consultant writing for a CEO. Using ONLY the data below, write a concise leadership brief in markdown with sections: "
                "1) Headline (one sentence), 2) What employees value most (top 3-4 strengths with % and a short quote), "
                "3) Priority improvement areas (top 3-4 with %, which demographic groups feel it most, a short quote), "
                "4) Risk watch-list (sensitive flags, burnout/attrition signals), 5) Recommended actions (3-5 concrete, mapped to GPTW dimensions). "
                "Cite numbers. Do not invent data.\n\n" + build_context(),
                max_tokens=2000)
    if key in st.session_state.get("briefs", {}):
        with st.container(border=True):
            st.markdown(st.session_state["briefs"][key])

# ---------------------------------------------------------------- conversational AI
with tabs[5]:
    st.markdown("Ask questions about the feedback in plain English. Answers are grounded in the filtered data.")
    if "chat" not in st.session_state:
        st.session_state.chat = []
    chat_box = st.container(height=480)
    for m in st.session_state.chat:
        chat_box.chat_message(m["role"]).markdown(m["content"])
    suggestions = ["What do frontline managers complain about most?",
                   "How does feedback differ between employees under 2 years and 10+ years tenure?",
                   "Which themes drive low confidence in the executive team?",
                   "Summarise feedback on pay and career growth with quotes"]
    pick = st.pills("Try:", suggestions, label_visibility="collapsed") if not st.session_state.chat else None
    prompt = st.chat_input("Ask about the survey feedback...") or pick
    if prompt:
        st.session_state.chat.append({"role": "user", "content": prompt})
        chat_box.chat_message("user").markdown(prompt)
        history = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in st.session_state.chat[-6:-1])
        with chat_box.chat_message("assistant"), st.spinner("Thinking..."):
            ans = ai_complete(
                "You are an employee-experience analyst for Great Place To Work. Answer the user's question using ONLY the survey data context. "
                "Be specific, quote percentages and short verbatim evidence, and say so if the data cannot answer it.\n\n"
                f"DATA CONTEXT:\n{build_context()}\n\nCONVERSATION SO FAR:\n{history}\n\nQUESTION: {prompt}")
            st.markdown(ans)
        st.session_state.chat.append({"role": "assistant", "content": ans})
    if st.session_state.chat and st.button("Clear chat", icon=":material/delete:"):
        st.session_state.chat = []
        st.rerun()

# ---------------------------------------------------------------- comment browser
with tabs[6]:
    search = st.text_input("Search comments", placeholder="e.g. salary, canteen, manager")
    cv = c if not search else c[c.COMMENT_TEXT.str.contains(search, case=False, na=False)]
    tag_list = t.groupby("COMMENT_ID").apply(lambda d: ", ".join(f"{h} ({s[:3]})" for h, s in zip(d.HASHTAG, d.THEME_SENTIMENT))).rename("AI_THEMES")
    cv = cv.merge(tag_list, left_on="COMMENT_ID", right_index=True, how="left")
    st.dataframe(cv[["QUESTION_TYPE", "COMMENT_TEXT", "AI_THEMES", "OVERALL_SENTIMENT", "SENTIMENT_SCORE", "RISK_FLAG", "AI_SUMMARY",
                     "MANAGER_LEVEL", "GENDER", "AGE_BAND", "TENURE_BAND"]],
                 hide_index=True, use_container_width=True, height=560,
                 column_config={"COMMENT_TEXT": st.column_config.TextColumn("Comment", width="large"),
                                "AI_THEMES": st.column_config.TextColumn("AI themes (sentiment)", width="large"),
                                "SENTIMENT_SCORE": st.column_config.NumberColumn("Score", format="%+.2f")})
