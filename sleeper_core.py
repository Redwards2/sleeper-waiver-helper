"""Core logic for the Sleeper waiver helper.

Everything here is plain Python (no Streamlit) so it can be tested on its own.
The Streamlit app (app.py) wraps these functions with caching and UI.
"""

from __future__ import annotations

import io
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pandas as pd
import requests

BASE_URL = "https://api.sleeper.app/v1"
OFFENSE = ("QB", "RB", "WR", "TE")
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
NO_RANK = 10**9  # Sleeper uses huge/None search_rank for irrelevant players

# Nickname differences seen between KTC-style lists and Sleeper's player DB.
# Keys and values are already in normalize_name() form.
NAME_ALIASES = {
    "hollywood brown": "marquise brown",
    "gabe davis": "gabriel davis",
    "josh palmer": "joshua palmer",
    "tank dell": "nathaniel dell",
    "chig okonkwo": "chigoziem okonkwo",
    "bam knight": "zonovan knight",
    "mitch trubisky": "mitchell trubisky",
    "scotty miller": "scott miller",
    "jeff wilson": "jeffery wilson",
    "eli mitchell": "elijah mitchell",
}

# Which player fantasy positions each Sleeper lineup slot can hold.
SLOT_ELIGIBILITY = {
    "QB": {"QB"},
    "RB": {"RB"},
    "WR": {"WR"},
    "TE": {"TE"},
    "K": {"K"},
    "DEF": {"DEF"},
    "FLEX": {"RB", "WR", "TE"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
    "REC_FLEX": {"WR", "TE"},
    "WRRB_FLEX": {"WR", "RB"},
    "DL": {"DL"},
    "LB": {"LB"},
    "DB": {"DB"},
    "IDP_FLEX": {"DL", "LB", "DB"},
}
# Raw player positions -> fantasy position buckets (used when fantasy_positions is missing)
POSITION_BUCKET = {"DE": "DL", "DT": "DL", "NT": "DL", "CB": "DB", "S": "DB", "SS": "DB",
                   "FS": "DB", "OLB": "LB", "ILB": "LB", "MLB": "LB"}
NON_ACTIVE_SLOTS = {"IR", "TAXI"}
# Never shown anywhere (trending, search, drops): not used in Ryan's dynasty leagues.
EXCLUDED_POSITIONS = {"K", "DEF"}


class SleeperError(RuntimeError):
    pass


# --------------------------------------------------------------------------- HTTP

def get_json(path: str, params: dict | None = None, timeout: int = 30):
    """GET a Sleeper endpoint and return parsed JSON (None if not found / null)."""
    url = f"{BASE_URL}/{path.lstrip('/')}"
    try:
        resp = requests.get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise SleeperError(f"Could not reach Sleeper ({url}): {exc}") from exc
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise SleeperError(f"Sleeper returned HTTP {resp.status_code} for {url}")
    return resp.json()


def fetch_nfl_state() -> dict:
    state = get_json("state/nfl")
    if not state:
        raise SleeperError("Sleeper's NFL state endpoint returned nothing.")
    return state


def current_league_season(state: dict) -> str:
    """Season leagues are keyed by. Differs from `season` during the off-season."""
    return str(state.get("league_season") or state.get("season"))


def fetch_user(username_or_id: str) -> dict | None:
    # /user/<x> accepts either a username or a numeric user_id.
    username_or_id = (username_or_id or "").strip()
    if not username_or_id:
        return None
    return get_json(f"user/{username_or_id}")


def fetch_leagues(user_id: str, season: str) -> list[dict]:
    return get_json(f"user/{user_id}/leagues/nfl/{season}") or []


def fetch_rosters_bulk(league_ids, max_workers: int = 8) -> dict[str, list[dict]]:
    """Fetch every league's rosters in parallel, exactly one request per league."""
    league_ids = list(dict.fromkeys(league_ids))  # de-dupe, keep order

    def one(lid):
        return lid, (get_json(f"league/{lid}/rosters") or [])

    if not league_ids:
        return {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(league_ids))) as pool:
        return dict(pool.map(one, league_ids))


def fetch_trending_adds(lookback_hours: int = 24, limit: int = 25) -> list[dict]:
    return get_json("players/nfl/trending/add",
                    {"lookback_hours": lookback_hours, "limit": limit}) or []


def fetch_players_raw() -> dict:
    return get_json("players/nfl", timeout=120) or {}


# ------------------------------------------------------------------ name handling

def normalize_name(name) -> str:
    """Lowercase, strip accents/punctuation/suffixes, merge initials (D.J. -> dj).

    Examples: "Kenneth Walker III" -> "kenneth walker"; "D.J. Moore" and
    "D. J. Moore" -> "dj moore"; "Amon-Ra St. Brown" -> "amon ra st brown".
    """
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = re.sub(r"[’'`]", "", s)          # Ja'Marr -> jamarr
    s = re.sub(r"[^a-z0-9]+", " ", s)    # periods, hyphens, commas -> space
    tokens = s.split()
    while len(tokens) > 2 and tokens[-1] in SUFFIXES:
        tokens.pop()
    merged, run = [], ""
    for tok in tokens:
        if len(tok) == 1:
            run += tok
            continue
        if run:
            merged.append(run)
            run = ""
        merged.append(tok)
    if run:
        merged.append(run)
    out = " ".join(merged)
    return NAME_ALIASES.get(out, out)


# --------------------------------------------------------------- players database

def slim_players(raw: dict) -> dict[str, dict]:
    """Keep only the fields the app needs (the raw DB is ~5 MB of JSON)."""
    out = {}
    for pid, p in raw.items():
        if not isinstance(p, dict):
            continue
        pos = p.get("position") or ""
        if pos == "DEF":
            name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or pid
        else:
            name = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        if not name:
            continue
        fpos = [f for f in (p.get("fantasy_positions") or []) if f]
        if not fpos and pos:
            fpos = [POSITION_BUCKET.get(pos, pos)]
        rank = p.get("search_rank")
        out[str(pid)] = {
            "name": name,
            "norm": normalize_name(name),
            "pos": pos,
            "fpos": fpos,
            "team": p.get("team"),
            "rank": rank if isinstance(rank, (int, float)) else NO_RANK,
        }
    return out


def primary_offense_pos(player: dict) -> str | None:
    """QB/RB/WR/TE for offensive skill players, else None."""
    if player.get("pos") in OFFENSE:
        return player["pos"]
    for f in player.get("fpos", []):
        if f in OFFENSE:
            return f
    return None


def is_excluded(player: dict | None) -> bool:
    """True for kickers and team defenses (and unknown player IDs)."""
    if not player:
        return True
    if player.get("pos") in EXCLUDED_POSITIONS:
        return True
    fpos = set(player.get("fpos") or [])
    return bool(fpos) and fpos <= EXCLUDED_POSITIONS


def top_trending(trending: list[dict], players: dict[str, dict], n: int) -> list[dict]:
    """First n trending entries that aren't kickers/defenses (request extra so n survive)."""
    return [t for t in trending if not is_excluded(players.get(str(t.get("player_id"))))][:n]


def search_players(query: str, players: dict[str, dict], limit: int = 25) -> list[str]:
    """Player IDs whose normalized name contains every query token; best-known first."""
    q_tokens = normalize_name(query).split()
    if not q_tokens:
        return []
    hits = []
    for pid, p in players.items():
        if is_excluded(p):
            continue
        name_tokens = p["norm"].split()
        if all(any(nt.startswith(qt) for nt in name_tokens) for qt in q_tokens):
            exact = p["norm"] == " ".join(q_tokens)
            hits.append((not exact, p["team"] is None, p["rank"], p["name"], pid))
    hits.sort()
    return [h[-1] for h in hits[:limit]]


# ------------------------------------------------------------------- KTC values

@dataclass
class KtcMatch:
    values: dict[str, float]           # sleeper player_id -> KTC value
    unmatched: list[str]               # CSV names with no offensive Sleeper player
    ambiguous: list[str]               # CSV names that matched >1 Sleeper player (best one used)
    rows: int


def read_ktc_csv(data: bytes) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(data), thousands=",")
    cols = {c.strip().lower(): c for c in df.columns}
    if "player_sleeper" not in cols or "ktc_value" not in cols:
        raise ValueError("CSV needs columns Player_Sleeper and KTC_Value "
                         f"(found: {', '.join(map(str, df.columns))})")
    out = pd.DataFrame({
        "name": df[cols["player_sleeper"]].astype(str).str.strip(),
        "value": pd.to_numeric(df[cols["ktc_value"]], errors="coerce"),
    })
    pos_col = cols.get("position") or cols.get("pos")
    out["pos"] = df[pos_col].astype(str).str.upper().str.strip() if pos_col else None
    out = out.dropna(subset=["value"])
    return out[out["name"] != ""]


def match_ktc(ktc: pd.DataFrame, players: dict[str, dict]) -> KtcMatch:
    """Map each CSV row to exactly one Sleeper QB/RB/WR/TE.

    Name + position: the CSV has no position column, so position comes from
    Sleeper (only offensive players are candidates, which rules out IDP/K/DEF
    namesakes). If the CSV *does* carry a Position column it is used too. When
    two offensive players share a name, the one Sleeper ranks higher (and who is
    on a team) gets the value.
    """
    index: dict[str, list[str]] = {}
    for pid, p in players.items():
        if primary_offense_pos(p):
            index.setdefault(p["norm"], []).append(pid)

    values, unmatched, ambiguous = {}, [], []
    for row in ktc.itertuples(index=False):
        cands = index.get(normalize_name(row.name), [])
        if row.pos and row.pos in OFFENSE:
            cands = [c for c in cands if primary_offense_pos(players[c]) == row.pos] or cands
        if not cands:
            unmatched.append(row.name)
            continue
        if len(cands) > 1:
            ambiguous.append(row.name)
        best = min(cands, key=lambda c: (players[c]["team"] is None, players[c]["rank"]))
        values[best] = max(float(row.value), values.get(best, float("-inf")))
    return KtcMatch(values, unmatched, ambiguous, len(ktc))


# ------------------------------------------------------------------ league logic

# Sidebar filter options, in display order, with which are checked by default.
LEAGUE_TYPES = ["Redraft Lineup", "Redraft Bestball", "Dynasty Lineup", "Dynasty Bestball"]
DEFAULT_LEAGUE_TYPES = {"Dynasty Lineup", "Dynasty Bestball"}
# Before the draft finishes every player is "unrostered", so these leagues would
# show up as available for everyone. Sleeper league status values:
# pre_draft, drafting, in_season, complete.
PRE_WAIVER_STATUSES = {"pre_draft", "drafting"}


def league_type(league: dict) -> str:
    """Sleeper settings.type: 0 = redraft, 1 = keeper, 2 = dynasty; settings.best_ball = 1.

    Keeper leagues are grouped with Redraft since the filter only has the two kinds.
    """
    settings = league.get("settings") or {}
    kind = "Dynasty" if settings.get("type") == 2 else "Redraft"
    fmt = "Bestball" if settings.get("best_ball") == 1 else "Lineup"
    return f"{kind} {fmt}"


@dataclass
class LeagueView:
    league_id: str
    name: str
    eligible_positions: set
    rostered: set          # every player on any roster, incl. taxi and IR
    my_roster: dict | None
    open_spots: int

    @property
    def url(self) -> str:
        return f"https://sleeper.com/leagues/{self.league_id}"


def find_my_roster(rosters: list[dict], user_id: str) -> dict | None:
    for r in rosters:
        if r.get("owner_id") == user_id or user_id in (r.get("co_owners") or []):
            return r
    return None


def build_league_view(league: dict, rosters: list[dict], user_id: str) -> LeagueView:
    slots = league.get("roster_positions") or []
    eligible = set()
    for s in slots:
        eligible |= SLOT_ELIGIBILITY.get(s, set())
    rostered = set()
    for r in rosters:
        rostered.update(r.get("players") or [])
    mine = find_my_roster(rosters, user_id)
    open_spots = 0
    if mine:
        active_slots = sum(1 for s in slots if s not in NON_ACTIVE_SLOTS)
        players = mine.get("players") or []
        stashed = set(mine.get("reserve") or []) | set(mine.get("taxi") or [])
        active = sum(1 for p in players if p not in stashed)
        open_spots = max(0, active_slots - active)
    return LeagueView(
        league_id=str(league["league_id"]),
        name=league.get("name") or league["league_id"],
        eligible_positions=eligible,
        rostered=rostered,
        my_roster=mine,
        open_spots=open_spots,
    )


def player_fits_league(player: dict | None, lv: LeagueView) -> bool:
    if not player:
        return False
    fpos = set(player.get("fpos") or [])
    return bool(fpos & lv.eligible_positions) if lv.eligible_positions else True


def available_leagues(pid: str, player: dict | None, league_views: list[LeagueView]) -> list[LeagueView]:
    """Leagues where I have a team, the player is unrostered, and the league uses his position."""
    return [lv for lv in league_views
            if lv.my_roster is not None and pid not in lv.rostered and player_fits_league(player, lv)]


@dataclass
class DropCandidate:
    pid: str
    name: str
    pos: str
    team: str | None
    value: float | None    # None = not in the KTC top-500 list
    tag: str | None        # "Taxi" / "IR" when included


def drop_candidates(lv: LeagueView, players: dict[str, dict], ktc_values: dict[str, float],
                    n: int = 3, include_stashed: bool = False) -> list[DropCandidate]:
    """My lowest-value QB/RB/WR/TE, cheapest first.

    Players missing from the KTC list rank first (they're below the top 500), with
    Sleeper's own ranking as the tie-breaker (least relevant first). Taxi and IR
    players are excluded by default because dropping them doesn't open the active
    roster spot a waiver add needs.
    """
    if not lv.my_roster:
        return []
    taxi = set(lv.my_roster.get("taxi") or [])
    ir = set(lv.my_roster.get("reserve") or [])
    out = []
    for pid in lv.my_roster.get("players") or []:
        tag = "Taxi" if pid in taxi else "IR" if pid in ir else None
        if tag and not include_stashed:
            continue
        p = players.get(pid)
        if not p:
            continue
        pos = primary_offense_pos(p)
        if not pos:
            continue  # K, DEF, IDP never suggested
        out.append(DropCandidate(pid, p["name"], pos, p["team"], ktc_values.get(pid), tag))

    def key(c: DropCandidate):
        if c.value is None:
            return (0, 0.0, -players[c.pid]["rank"], c.name)
        return (1, c.value, 0, c.name)

    out.sort(key=key)
    return out[:n]
