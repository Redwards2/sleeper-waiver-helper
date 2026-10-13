"""Sleeper Waiver Helper — see which of your leagues a player is available in,
plus your cheapest drop candidates in each. Read-only: claims are made in Sleeper.
"""

from __future__ import annotations

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


# One cached call for all leagues: the trending view and the search box both
# read from this, so each league's rosters are fetched once per TTL window.
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

def fmt_value(v) -> str:
    return "unranked" if v is None else f"{v:,.0f}"


def render_player_card(pid: str, p: dict, ktc: dict, adds: int | None = None):
    if not p:
        st.markdown(f"**Unknown player** `{pid}`")
        return
    team = p.get("team") or "FA"
    st.markdown(f"**{p['name']}**  \n{p.get('pos') or '?'} · {team}")
    bits = []
    if adds is not None:
        bits.append(f"▲ {adds:,} adds / {TRENDING_HOURS}h")
    if sc.primary_offense_pos(p):
        bits.append(f"KTC {fmt_value(ktc.get(pid))}")
    if bits:
        st.caption(" · ".join(bits))


def render_league_block(lv: sc.LeagueView, players: dict, ktc: dict, include_stashed: bool):
    label = lv.name
    if lv.open_spots:
        label += f"  ·  🟢 {lv.open_spots} open spot{'s' if lv.open_spots > 1 else ''}"
    with st.expander(label, expanded=False):
        if lv.open_spots:
            st.caption("You have room — no drop needed.")
        drops = sc.drop_candidates(lv, players, ktc, DROPS_PER_LEAGUE, include_stashed)
        if not drops:
            st.caption("No QB/RB/WR/TE drop candidates on this roster.")
        for i, d in enumerate(drops, 1):
            tag = f" · _{d.tag}_" if d.tag else ""
            st.markdown(f"{i}. **{d.name}** ({d.pos}, {d.team or 'FA'}) — KTC {fmt_value(d.value)}{tag}")
        st.markdown(f"[Open league in Sleeper ↗]({lv.url})")


def render_player_row(pid: str, players: dict, league_views: list, ktc: dict,
                      include_stashed: bool, adds: int | None = None) -> bool:
    """Draw one row. Returns False (and draws nothing) if he's unavailable everywhere."""
    p = players.get(pid)
    avail = sc.available_leagues(pid, p, league_views)
    if not avail:
        return False
    left, right = st.columns([1, 3], gap="medium")
    with left:
        render_player_card(pid, p, ktc, adds)
        st.caption(f"Available in {len(avail)} of {len(league_views)} leagues")
    with right:
        for lv in sorted(avail, key=lambda x: x.name.lower()):
            render_league_block(lv, players, ktc, include_stashed)
    st.divider()
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

st.title("🏈 Waiver Helper")
st.caption("Where each player is available across your Sleeper leagues, with your cheapest drops. "
           "Read-only — make the claims in Sleeper.")

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
    league_names = {str(lg["league_id"]): lg.get("name") or str(lg["league_id"]) for lg in leagues}
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
    st.info("Pick at least one league in the sidebar.")
    st.stop()

try:
    rosters_by_league = get_rosters(tuple(sorted(chosen)))
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
query = st.text_input("🔎 Search any player", placeholder="e.g. Tyler Allgeier")
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
        if not render_player_row(pick, players, league_views, ktc, include_stashed):
            st.info(f"**{players[pick]['name']}** isn't available in any of your selected leagues.")
            st.divider()

# ----------------------------------------------------------------------- trending
st.subheader(f"Trending adds · last {TRENDING_HOURS}h")
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
