# Sleeper Waiver Helper

A Streamlit app that shows, across all your Sleeper leagues at once, where each
trending (or searched) player is available — with your three cheapest drop
candidates per league from your KTC values. Sleeper's API is read-only, so you
make the claims yourself in Sleeper.

## Files

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI + caching |
| `sleeper_core.py` | Sleeper API calls, name matching, availability, drop logic (no Streamlit) |
| `ktc_values.csv` | **You add this.** Columns `Player_Sleeper`, `KTC_Value` (optional `Position`) |
| `requirements.txt` | Dependencies |

## Deploy to Streamlit Community Cloud

1. Put these files in a GitHub repo (add your `ktc_values.csv` at the repo root,
   or upload it in the sidebar each session — an upload overrides the repo file).
2. On share.streamlit.io → **Create app** → pick the repo, branch, and `app.py`.
3. Optional: in the app's **Settings → Secrets**, add
   `SLEEPER_USERNAME = "yourname"` so the username is prefilled. The app also
   writes `?user=yourname` into the URL, so a bookmark works too.

Run locally: `pip install -r requirements.txt && streamlit run app.py`

## How it decides things

- **Season:** `league_season` from `/state/nfl` (falls back to `season`).
- **Available:** not on any roster in the league (including taxi and IR), you
  have a team there, and the league's lineup uses his position (a kicker
  doesn't show up for a league with no K slot).
- **Drop candidates:** your QB/RB/WR/TE only, lowest KTC first. Players not on
  the top-500 list count as below everyone on it and are listed first
  ("unranked"). Taxi/IR players are skipped unless you flip the sidebar toggle,
  because dropping them doesn't open an active roster spot.
- **Open spot:** if your active roster has an empty slot, the league header
  says so — no drop needed.
- **Name matching:** accents, punctuation, Jr./Sr./II–V suffixes and initials
  (D.J. / DJ) are normalized; a few common nicknames are aliased in
  `NAME_ALIASES`. Only Sleeper QB/RB/WR/TE are match candidates. The sidebar's
  **KTC match report** lists any CSV names that didn't match.
- **Caching:** players DB once a day (shared, not copied per rerun); leagues 1h;
  all rosters in one parallel cached call for 5 min; trending 10 min. **Refresh**
  in the sidebar clears rosters and trending.

## Limits

The public API doesn't expose waiver-vs-free-agent status or pending claims, so
"available" means "unrostered."
