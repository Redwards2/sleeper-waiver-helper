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


@st.cache_data(show_spinner=False)
def get_ktc(csv_bytes: bytes, players_version: int) -> sc.KtcMatch:
    # players_version keys the cache to the daily players DB without hashing the whole dict
    return sc.match_ktc(sc.read_ktc_csv(csv_bytes), get_players())


# ----------------------------------------------------------------------- helpers

# Compact one-row-per-player layout. Chips scroll inside their own box when a
# player is open in lots of leagues, so every row stays about two lines tall.
CSS = """
<style>
[data-testid="stMainBlockContainer"], .block-container { padding-top: 3.2rem; }
[data-testid="stMainBlockContainer"] [data-testid="stVerticalBlock"] { gap: 0.45rem; }
[class*="st-key-prow_"] { border-bottom: 1px solid rgba(128,128,128,.22); padding-bottom: .45rem; }
.pname { font-weight: 600; line-height: 1.25; }
.pmeta { font-size: .78rem; opacity: .7; line-height: 1.35; }
.pmeta span { white-space: nowrap; }
/* Up to 3 lines of chips; more scroll inside the box (full list is also under Drops). */
.chips { display: flex; flex-wrap: wrap; gap: 4px; max-height: 68px; overflow-y: auto;
         scrollbar-width: thin; align-content: flex-start; }
.chip { box-sizing: border-box; height: 20px; line-height: 18px; font-size: .76rem; padding: 0 8px;
        border-radius: 10px; white-space: nowrap; border: 1px solid rgba(128,128,128,.35);
        text-decoration: none !important; color: inherit !important; }
.chip:hover { border-color: rgba(128,128,128,.85); }
.chip.open { border-color: #2e9e5b; }
.chip.count { font-weight: 600; border-style: dashed; }
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


def md_text(s: str) -> str:
    return re.sub(r"([\\`*_\[\]#$|<>~])", r"\\\1", str(s))


def player_meta(pid: str, p: dict, ktc: dict, adds) -> str:
    bits = [f"{p.get('pos') or '?'} · {p.get('team') or 'FA'}"]
    if adds is not None:
        bits.append(f"▲{fmt_count(adds)}")
    if sc.primary_offense_pos(p):
        bits.append(f"KTC {fmt_value(ktc.get(pid))}")
    return " · ".join(f"<span>{html_text(b)}</span>" for b in bits)


def league_chips(avail: list, total: int) -> str:
    chips = [f'<span class="chip count" title="Available in {len(avail)} of {total} leagues">'
             f'{len(avail)}/{total}</span>']
    for lv in avail:
        short = lv.name if len(lv.name) <= CHIP_MAX_CHARS else lv.name[:CHIP_MAX_CHARS - 1] + "…"
        room = f" — {lv.open_spots} open spot{'s' if lv.open_spots > 1 else ''}" if lv.open_spots else ""
        cls = "chip open" if lv.open_spots else "chip"
        chips.append(f'<a class="{cls}" href="{lv.url}" target="_blank" '
                     f'title="{html_text(lv.name + room)}">{html_text(short)}</a>')
    return f'<div class="chips">{"".join(chips)}</div>'


def drops_markdown(lv: sc.LeagueView, players: dict, ktc: dict, include_stashed: bool) -> str:
    head = f"**{md_text(lv.name)}** [↗]({lv.url})"
    if lv.open_spots:
        head += f" · 🟢 {lv.open_spots} open spot{'s' if lv.open_spots > 1 else ''}"
    drops = sc.drop_candidates(lv, players, ktc, DROPS_PER_LEAGUE, include_stashed)
    if not drops:
        return head + "  \n_No QB/RB/WR/TE drop candidates_"
    parts = []
    for d in drops:
        tag = f" _{d.tag}_" if d.tag else ""
        parts.append(f"{md_text(d.name)} ({d.pos}) {fmt_value(d.value)}{tag}")
    return head + "  \n" + " · ".join(parts)


def render_player_row(pid: str, players: dict, league_views: list, ktc: dict,
                      include_stashed: bool, adds: int | None = None, ctx: str = "trend") -> bool:
    """One compact row. Returns False (and draws nothing) if he's unavailable everywhere."""
    p = players.get(pid)
    avail = sorted(sc.available_leagues(pid, p, league_views), key=lambda x: x.name.lower())
    if not avail:
        return False
    with st.container(key=f"prow_{ctx}_{pid}"):
        name_col, chips_col, drop_col = st.columns([1.7, 6, 0.9], gap="small", vertical_alignment="center")
        with name_col:
            st.markdown(f'<div class="pname">{html_text(p["name"])}</div>'
                        f'<div class="pmeta">{player_meta(pid, p, ktc, adds)}</div>',
                        unsafe_allow_html=True)
        with chips_col:
            my_total = sum(1 for lv in league_views if lv.my_roster is not None)
            st.markdown(league_chips(avail, my_total), unsafe_allow_html=True)
        with drop_col:
            with st.popover("Drops", use_container_width=True):
                st.caption(f"Cheapest {DROPS_PER_LEAGUE} per league (KTC), {p['name']}")
                for lv in avail:
                    st.markdown(drops_markdown(lv, players, ktc, include_stashed))
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
    league_names = {str(lg["league_id"]): lg.get("name") or str(lg["league_id"]) for lg in typed_leagues}
    chosen = st.multiselect("Leagues", options=list(league_names), default=list(league_names),
                            format_func=lambda lid: league_names[lid])
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
    trending = get_trending(TRENDING_HOURS, TRENDING_LIMIT)
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

# ----------------------------------------------------------------------- trending
st.markdown(f"**Trending adds · last {TRENDING_HOURS}h** · chips link to the league; 🟢 = open roster spot")
shown = 0
for item in trending:
    pid = str(item.get("player_id"))
    if render_player_row(pid, players, league_views, ktc, include_stashed, adds=item.get("count")):
        shown += 1
hidden = len(trending) - shown
if shown == 0:
    st.info("None of today's top trending players are available in your leagues.")
elif hidden:
    st.caption(f"{hidden} of the top {len(trending)} trending players are rostered in all your leagues "
               "(or play a position your leagues don't use) and are hidden.")
