"""Sleeper Waiver Helper — see which of your leagues a player is available in,
plus your cheapest drop candidates in each. Read-only: claims are made in Sleeper.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

import streamlit as st

import sleeper_core as sc

APP_DIR = Path(__file__).parent
DEFAULT_CSV = APP_DIR / "ktc_values.csv"
TRENDING_HOURS = 24
TRENDING_LIMIT = 25
TRENDING_FETCH = 50  # extra, so 25 remain after kickers/defenses are removed
TOP_SCORERS = 10
DROPS_PER_LEAGUE = 3

st.set_page_config(page_title="Waiver Helper", page_icon="🏈", layout="wide")


# ----------------------------------------------------------------------- caching
# Players DB: Sleeper asks for at most ~1 pull/day. cache_resource shares one
# slimmed copy across sessions without deep-copying it on every rerun.
@st.cache_resource(ttl=24 * 3600, show_spinner="Downloading Sleeper player database (once a day)…")
def get_players() -> dict:
    return sc.slim_players(sc.fetch_players_raw())


@st.cache_data(ttl=600, show_spinner=False)
def get_state() -> dict:
    return sc.fetch_nfl_state()


@st.cache_data(ttl=3600, show_spinner=False)
def get_user(username_or_id: str):
    return sc.fetch_user(username_or_id)


@st.cache_data(ttl=3600, show_spinner=False)
def get_leagues(user_id: str, season: str) -> list:
    return sc.fetch_leagues(user_id, season)


# One cached call for all your leagues: the trending view, the search box and
# every filter setting read from this, so each league is fetched once per TTL.
@st.cache_data(ttl=300, show_spinner="Loading rosters…")
def get_rosters(league_ids: tuple) -> dict:
    return sc.fetch_rosters_bulk(list(league_ids))


@st.cache_data(ttl=600, show_spinner=False)
def get_trending(hours: int, limit: int) -> list:
    return sc.fetch_trending_adds(hours, limit)


@st.cache_data(ttl=3600, show_spinner="Loading weekly stats…")
def get_week_stats(season: str, week: int) -> dict:
    return sc.fetch_week_stats(season, week)


@st.cache_data(show_spinner=False)
def get_ktc(csv_bytes: bytes, players_version: int) -> sc.KtcMatch:
    # players_version keys the cache to the daily players DB without hashing the whole dict
    return sc.match_ktc(sc.read_ktc_csv(csv_bytes), get_players())


# ----------------------------------------------------------------------- helpers

# Compact one-row-per-player layout, drawn as one HTML block per player.
# The Drops button is a <label> for a hidden checkbox; checking it reveals the
# panel under the row with pure CSS, so it opens instantly with no app rerun.
CSS = """
<style>
[data-testid="stMainBlockContainer"], .block-container { padding-top: 3.2rem; }
[data-testid="stMainBlockContainer"] [data-testid="stVerticalBlock"] { gap: 0.45rem; }
.prow { display: grid; grid-template-columns: 190px minmax(0, 1fr) auto; column-gap: 12px;
        align-items: center; border-bottom: 1px solid rgba(128,128,128,.22); padding-bottom: .45rem;
        margin-bottom: 1rem; /* cancels Streamlit's -1rem margin under markdown blocks */ }
.pname { font-weight: 600; line-height: 1.25; }
.pmeta { font-size: .78rem; opacity: .7; line-height: 1.35; }
.pmeta span { white-space: nowrap; }
/* Up to 3 lines of chips; more scroll inside the box (all of them are in the Drops panel). */
.chips { display: flex; flex-wrap: wrap; gap: 4px; max-height: 68px; overflow-y: auto;
         scrollbar-width: thin; align-content: flex-start; }
.chip { box-sizing: border-box; height: 20px; line-height: 18px; font-size: .76rem; padding: 0 8px;
        border-radius: 10px; white-space: nowrap; border: 1px solid rgba(128,128,128,.35);
        text-decoration: none !important; color: inherit !important; }
.chip:hover { border-color: rgba(128,128,128,.85); }
.chip.open { border-color: #2e9e5b; }
.chip.count { font-weight: 600; border-style: dashed; }
.dtoggle { display: none; }
.dbtn { cursor: pointer; user-select: none; font-size: .85rem; padding: 5px 14px; border-radius: 8px;
        border: 1px solid rgba(128,128,128,.4); white-space: nowrap; margin: 0; }
.dbtn:hover { border-color: rgba(128,128,128,.9); }
.dbtn::after { content: " ▾"; }
.dtoggle:checked ~ .dbtn { border-color: #ff4b4b; }
.dtoggle:checked ~ .dbtn::after { content: " ▴"; }
.dpanel { display: none; grid-column: 1 / -1; margin-top: 8px; gap: 8px;
          grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); }
.dtoggle:checked ~ .dpanel { display: grid; }
.dcard { border: 1px solid rgba(128,128,128,.25); border-radius: 8px; padding: 6px 10px; font-size: .8rem; }
.dcard.open { border-color: #2e9e5b; }
.dhead { font-weight: 600; margin-bottom: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dhead a { color: inherit !important; text-decoration: none !important; }
.dhead a:hover { text-decoration: underline !important; }
.droom { color: #2e9e5b; font-size: .75rem; margin-bottom: 2px; }
.dcard ol { margin: 0; padding-left: 1.2rem; }
.dcard li { margin: 0; line-height: 1.45; }
.dpos, .dval { opacity: .65; font-size: .74rem; }
.dtag { font-style: italic; opacity: .65; font-size: .74rem; }
.dnone { opacity: .6; font-style: italic; }
@media (max-width: 640px) {
  .prow { grid-template-columns: minmax(0, 1fr) auto; row-gap: 4px; }
  .prow .chips { grid-column: 1 / -1; grid-row: 2; }
}
</style>
"""
CHIP_MAX_CHARS = 26


def fmt_value(v) -> str:
    return "unranked" if v is None else f"{v:,.0f}"


def fmt_count(n) -> str:
    if n is None:
        return ""
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.1f}K" if n >= 1e3 else str(n)


def html_text(s: str) -> str:
    # $ is swapped for an entity so "$5 ... $10" league names never render as math
    return html.escape(str(s)).replace("$", "&#36;")


def open_spots_text(n: int) -> str:
    return f"{n} open spot{'s' if n > 1 else ''}"


def player_meta(pid: str, p: dict, ktc: dict, stat: str | None) -> str:
    bits = [f"{p.get('pos') or '?'} · {p.get('team') or 'FA'}"]
    if stat:
        bits.append(stat)
    if sc.primary_offense_pos(p):
        bits.append(f"KTC {fmt_value(ktc.get(pid))}")
    return " · ".join(f"<span>{html_text(b)}</span>" for b in bits)


def league_chips(avail: list, total: int) -> str:
    chips = [f'<span class="chip count" title="Available in {len(avail)} of {total} leagues">'
             f'{len(avail)}/{total}</span>']
    for lv in avail:
        short = lv.name if len(lv.name) <= CHIP_MAX_CHARS else lv.name[:CHIP_MAX_CHARS - 1] + "…"
        room = f" — {open_spots_text(lv.open_spots)}" if lv.open_spots else ""
        cls = "chip open" if lv.open_spots else "chip"
        chips.append(f'<a class="{cls}" href="{lv.url}" target="_blank" '
                     f'title="{html_text(lv.name + room)}">{html_text(short)}</a>')
    return f'<div class="chips">{"".join(chips)}</div>'


def drop_card(lv: sc.LeagueView, players: dict, ktc: dict, include_stashed: bool) -> str:
    head = (f'<div class="dhead" title="{html_text(lv.name)}">'
            f'<a href="{lv.url}" target="_blank">{html_text(lv.name)} ↗</a></div>')
    room = f'<div class="droom">● {open_spots_text(lv.open_spots)}: no drop needed</div>' if lv.open_spots else ""
    drops = sc.drop_candidates(lv, players, ktc, DROPS_PER_LEAGUE, include_stashed)
    if drops:
        items = "".join(
            f'<li>{html_text(d.name)} <span class="dpos">{d.pos}</span> '
            f'<span class="dval">{fmt_value(d.value)}</span>'
            + (f' <span class="dtag">{d.tag}</span>' if d.tag else "") + "</li>"
            for d in drops)
        body = f"<ol>{items}</ol>"
    else:
        body = '<div class="dnone">No QB/RB/WR/TE on roster</div>'
    cls = "dcard open" if lv.open_spots else "dcard"
    return f'<div class="{cls}">{head}{room}{body}</div>'


def render_player_row(pid: str, players: dict, league_views: list, ktc: dict,
                      include_stashed: bool, stat: str | None = None, ctx: str = "trend") -> bool:
    """One compact row. Returns False (and draws nothing) if he's unavailable everywhere."""
    p = players.get(pid)
    avail = sorted(sc.available_leagues(pid, p, league_views), key=lambda x: x.name.lower())
    if not avail:
        return False
    my_total = sum(1 for lv in league_views if lv.my_roster is not None)
    tid = "dt-" + re.sub(r"[^A-Za-z0-9_-]", "_", f"{ctx}-{pid}")
    cards = "".join(drop_card(lv, players, ktc, include_stashed) for lv in avail)
    st.markdown(
        f'<div class="prow">'
        f'<input type="checkbox" id="{tid}" class="dtoggle">'
        f'<div><div class="pname">{html_text(p["name"])}</div>'
        f'<div class="pmeta">{player_meta(pid, p, ktc, stat)}</div></div>'
        f'{league_chips(avail, my_total)}'
        f'<label for="{tid}" class="dbtn">Drops</label>'
        f'<div class="dpanel">{cards}</div>'
        f'</div>',
        unsafe_allow_html=True)
    return True


# ----------------------------------------------------------------------- sidebar

def secret_default(key: str) -> str:
    try:
        return str(st.secrets.get(key, ""))
    except Exception:  # no secrets.toml locally
        return ""


with st.sidebar:
    st.header("Setup")
    default_user = st.query_params.get("user") or secret_default("SLEEPER_USERNAME")
    username = st.text_input("Sleeper username or user ID", value=default_user).strip()
    if username and st.query_params.get("user") != username:
        st.query_params["user"] = username  # bookmarkable

    uploaded = st.file_uploader("KTC values CSV", type=["csv"],
                                help="Columns: Player_Sleeper, KTC_Value. "
                                     "Leave empty to use ktc_values.csv from the repo.")
    include_stashed = st.toggle("Include Taxi/IR players as drop candidates", value=False,
                                help="Off by default: dropping a Taxi or IR player doesn't free "
                                     "the active roster spot a waiver add needs.")
    if st.button("🔄 Refresh rosters & trending", use_container_width=True):
        get_rosters.clear()
        get_trending.clear()
        st.rerun()

st.markdown(CSS, unsafe_allow_html=True)
st.markdown("#### 🏈 Waiver Helper")

if not username:
    st.info("Enter your Sleeper username in the sidebar to get started.")
    st.stop()

# ----------------------------------------------------------------------- load data
try:
    state = get_state()
    season = sc.current_league_season(state)
    user = get_user(username)
    if not user:
        st.error(f"No Sleeper user found for **{username}**.")
        st.stop()
    user_id = str(user["user_id"])
    leagues = get_leagues(user_id, season)
    players = get_players()
except sc.SleeperError as exc:
    st.error(str(exc))
    st.stop()

if not leagues:
    st.warning(f"No {season} NFL leagues found for **{user.get('display_name', username)}**.")
    st.stop()

# KTC values
csv_bytes = uploaded.getvalue() if uploaded else (DEFAULT_CSV.read_bytes() if DEFAULT_CSV.exists() else None)
if csv_bytes is None:
    st.warning("Upload your `ktc_values.csv` in the sidebar (or commit it next to app.py).")
    st.stop()
try:
    ktc_match = get_ktc(csv_bytes, len(players))
except ValueError as exc:
    st.error(f"Couldn't read the KTC CSV: {exc}")
    st.stop()
ktc = ktc_match.values

with st.sidebar:
    st.subheader("League types")
    picked_types = {t for t in sc.LEAGUE_TYPES
                    if st.checkbox(t, value=t in sc.DEFAULT_LEAGUE_TYPES, key=f"type_{t}")}
    typed_leagues = [lg for lg in leagues if sc.league_type(lg) in picked_types]
    drafting = [lg for lg in typed_leagues if lg.get("status") in sc.PRE_WAIVER_STATUSES]
    typed_leagues = [lg for lg in typed_leagues if lg.get("status") not in sc.PRE_WAIVER_STATUSES]
    league_names = {str(lg["league_id"]): lg.get("name") or str(lg["league_id"]) for lg in typed_leagues}
    chosen = st.multiselect("Leagues", options=list(league_names), default=list(league_names),
                            format_func=lambda lid: league_names[lid])
    if drafting:
        st.caption("Hidden until their draft is done: "
                   + ", ".join(lg.get("name") or str(lg["league_id"]) for lg in drafting))
    st.caption(f"Season {season} · {user.get('display_name', username)}")
    with st.expander("KTC match report"):
        st.write(f"{len(ktc)} of {ktc_match.rows} CSV rows matched to Sleeper players.")
        if ktc_match.unmatched:
            st.write("**Unmatched names** (check spelling vs. Sleeper):")
            st.write(", ".join(ktc_match.unmatched))
        if ktc_match.ambiguous:
            st.write("**Shared names** (value given to the higher-ranked player):")
            st.write(", ".join(ktc_match.ambiguous))

if not chosen:
    st.info("No leagues selected. Check a league type (or pick leagues) in the sidebar.")
    st.stop()

try:
    # Fetch every league once (not just the selected ones) so flipping the
    # type filter or league picker never triggers another round of requests.
    rosters_by_league = get_rosters(tuple(sorted(str(lg["league_id"]) for lg in leagues)))
    trending = sc.top_trending(get_trending(TRENDING_HOURS, TRENDING_FETCH), players, TRENDING_LIMIT)
except sc.SleeperError as exc:
    st.error(str(exc))
    st.stop()

league_lookup = {str(lg["league_id"]): lg for lg in leagues}
league_views = [sc.build_league_view(league_lookup[lid], rosters_by_league.get(lid, []), user_id)
                for lid in chosen]
no_team = [lv.name for lv in league_views if lv.my_roster is None]
if no_team:
    st.caption(f"Skipping leagues where you don't have a roster: {', '.join(no_team)}")

# ----------------------------------------------------------------------- search
query = st.text_input("Search any player", placeholder="🔎 Search any player, e.g. Tyler Allgeier",
                      label_visibility="collapsed")
if query:
    hits = sc.search_players(query, players)
    if not hits:
        st.warning(f"No players match “{query}”.")
    else:
        exact = [h for h in hits if players[h]["norm"] == sc.normalize_name(query)]
        if len(exact) == 1 or len(hits) == 1:
            pick = exact[0] if exact else hits[0]
        else:
            pick = st.selectbox(
                "Which player?", hits,
                format_func=lambda pid: f"{players[pid]['name']} — {players[pid]['pos']}, "
                                        f"{players[pid]['team'] or 'FA'}")
        if not render_player_row(pick, players, league_views, ktc, include_stashed, ctx="search"):
            st.info(f"**{players[pick]['name']}** isn't available in any of your selected leagues.")

tab_trend, tab_pts = st.tabs(["🔥 Trending adds", "📈 Top scorers available"])

# ----------------------------------------------------------------------- trending
with tab_trend:
    st.markdown(f"**Trending adds · last {TRENDING_HOURS}h** · chips link to the league; "
                "🟢 = open roster spot")
    shown = 0
    for item in trending:
        pid = str(item.get("player_id"))
        if render_player_row(pid, players, league_views, ktc, include_stashed,
                             stat=f"▲{fmt_count(item.get('count'))}"):
            shown += 1
    hidden = len(trending) - shown
    if shown == 0:
        st.info("None of today's top trending players are available in your leagues.")
    elif hidden:
        st.caption(f"{hidden} of the top {len(trending)} trending players are rostered in all your "
                   "leagues (or play a position your leagues don't use) and are hidden.")

# ----------------------------------------------------------------------- top scorers
with tab_pts:
    stats_season = str(state.get("season") or season)
    cur_week = int(state.get("week") or 0)
    if state.get("season_type") not in (None, "regular") or cur_week < 1:
        st.info("Weekly scoring shows once the NFL regular season is under way.")
    else:
        try:
            cur_complete = sc.week_is_complete(get_week_stats(stats_season, cur_week), players)
        except sc.SleeperError as exc:
            st.error(str(exc))
            st.stop()
        # "Previous week" = newest week whose games are (nearly) all played.
        default_week = cur_week if cur_complete else max(1, cur_week - 1)
        weeks = list(range(cur_week, 0, -1))
        chosen_leagues = [league_lookup[lid] for lid in chosen]
        scoring_opts = list(sc.SCORING_KEYS)
        c_week, c_score, _ = st.columns([1.2, 2.4, 3])
        week = c_week.selectbox(
            "Week", weeks, index=weeks.index(default_week),
            format_func=lambda w: f"Week {w}" + (" (in progress)" if w == cur_week and not cur_complete else ""))
        scoring = c_score.radio("Scoring", scoring_opts, horizontal=True,
                                index=scoring_opts.index(sc.default_scoring(chosen_leagues)),
                                help="Defaults to what most of your selected leagues use.")
        try:
            week_stats = get_week_stats(stats_season, week)
        except sc.SleeperError as exc:
            st.error(str(exc))
            st.stop()
        if not week_stats:
            st.info(f"Sleeper has no stats for week {week} yet.")
        else:
            top = sc.top_available_scorers(week_stats, players, sc.SCORING_KEYS[scoring],
                                           league_views, TOP_SCORERS)
            st.markdown(f"**Top {TOP_SCORERS} available · week {week} {scoring} points** · "
                        "available in at least one of your leagues")
            if not top:
                st.info("No one who scored that week is available in your selected leagues.")
            for pid, pts in top:
                render_player_row(pid, players, league_views, ktc, include_stashed,
                                  stat=f"{pts:.1f} pts", ctx="pts")
