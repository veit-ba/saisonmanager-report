"""
Saisonmanager Report Generator
Erstellt einen HTML-Report mit detaillierten Statistiken für Liga 1890.
"""

import requests
import json
from datetime import datetime
from collections import defaultdict
import concurrent.futures

BASE_URL = "https://saisonmanager.de/api/v2"
LEAGUE_ID = 1890

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def fetch(url: str) -> dict | list:
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.json()


def fetch_all_schedules(game_day_numbers: list[int]) -> dict[int, list]:
    """Fetch all game day schedules in parallel. Returns {game_day: [games]}."""
    def _fetch(day):
        return day, fetch(f"{BASE_URL}/leagues/{LEAGUE_ID}/game_days/{day}/schedule.json")

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(_fetch, day): day for day in game_day_numbers}
        for f in concurrent.futures.as_completed(futures):
            day, data = f.result()
            results[day] = data
    return results


def fetch_all_game_details(game_ids: list[int]) -> dict[int, dict]:
    """Fetch all finished game details in parallel."""
    def _fetch(gid):
        return gid, fetch(f"{BASE_URL}/games/{gid}.json")

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(_fetch, gid): gid for gid in game_ids}
        for f in concurrent.futures.as_completed(futures):
            gid, data = f.result()
            results[gid] = data
    return results


# ---------------------------------------------------------------------------
# PowerPlay / Boxplay analysis
# ---------------------------------------------------------------------------

def time_to_seconds(t: str) -> int:
    parts = t.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def compute_pp_bp_stats(game_details: dict[int, dict]) -> dict[int, dict]:
    """
    Compute per-team PowerPlay and Boxplay stats across all games.

    PowerPlay (PP):  goals scored while opponent has an active penalty
    Boxplay  (BP):  goals allowed while own team has an active penalty

    Returns: {team_id: {"pp_opps": int, "pp_goals": int, "bp_opps": int, "bp_goals": int}}
    """
    team_stats: dict[int, dict] = defaultdict(lambda: {
        "pp_opps": 0, "pp_goals": 0,
        "bp_opps": 0, "bp_goals": 0,
        "team_name": "",
    })

    for game_id, game in game_details.items():
        if not game.get("ended"):
            continue

        home_id = game.get("home_team_id")
        guest_id = game.get("guest_team_id")
        home_name = game.get("home_team_name", "")
        guest_name = game.get("guest_team_name", "")

        if home_id:
            team_stats[home_id]["team_name"] = home_name
        if guest_id:
            team_stats[guest_id]["team_name"] = guest_name

        events = sorted(game.get("events", []), key=lambda e: (e.get("period", 0), time_to_seconds(e.get("time", "0:00"))))

        # Active penalties: list of {team_id, period, end_sec, type, exhausted}
        # team_id = team serving penalty (short-handed)
        active_penalties: list[dict] = []

        for event in events:
            etype = event.get("event_type")
            eteam = event.get("event_team")  # "home" or "guest"
            period = event.get("period", 0)
            t_sec = time_to_seconds(event.get("time", "0:00"))

            # Clean up expired penalties
            active_penalties = [
                p for p in active_penalties
                if p["period"] == period and p["end_sec"] > t_sec
                or p["period"] != period  # different period - keep for now (edge case)
            ]
            # Also remove penalties from earlier periods
            active_penalties = [p for p in active_penalties if p["period"] >= period]

            if etype == "penalty":
                pen_type = event.get("penalty_type", "penalty_2")
                short_handed_id = home_id if eteam == "home" else guest_id
                opposing_id = guest_id if eteam == "home" else home_id

                duration = 120  # default 2 min
                is_major = pen_type == "penalty_5"

                if pen_type in ("penalty_2", "penalty_2and2"):
                    duration = 120
                elif pen_type == "penalty_5":
                    duration = 300
                else:
                    duration = 120

                if pen_type not in ("penalty_10", "penalty_ms_tech", "penalty_ms_full",
                                    "penalty_ms1", "penalty_ms2", "penalty_ms3"):
                    # Count as PP opportunity
                    team_stats[opposing_id]["pp_opps"] += 1
                    team_stats[short_handed_id]["bp_opps"] += 1
                    active_penalties.append({
                        "period": period,
                        "end_sec": t_sec + duration,
                        "short_id": short_handed_id,
                        "power_id": opposing_id,
                        "is_major": is_major,
                        "type": pen_type,
                    })

            elif etype == "goal":
                scoring_id = home_id if eteam == "home" else guest_id
                # Check if any opponent penalty is active
                pp_penalty = next(
                    (p for p in active_penalties if p["power_id"] == scoring_id and p["period"] == period),
                    None
                )
                if pp_penalty:
                    team_stats[scoring_id]["pp_goals"] += 1
                    team_stats[pp_penalty["short_id"]]["bp_goals"] += 1
                    # Minor penalty ends on goal; major does not
                    if not pp_penalty["is_major"]:
                        active_penalties.remove(pp_penalty)

    return dict(team_stats)


# ---------------------------------------------------------------------------
# Per-team scorer aggregation
# ---------------------------------------------------------------------------

def scorers_by_team(scorer_data: list[dict]) -> dict[str, list[dict]]:
    teams: dict[str, list] = defaultdict(list)
    for p in scorer_data:
        teams[p["team_name"]].append(p)
    for team in teams:
        teams[team].sort(key=lambda p: (p.get("goals", 0) + p.get("assists", 0)), reverse=True)
    return dict(teams)


# ---------------------------------------------------------------------------
# HTML generation helpers
# ---------------------------------------------------------------------------

CSS = """
:root {
    --bg: #0f1117;
    --surface: #1a1d27;
    --surface2: #22263a;
    --accent: #4f8ef7;
    --accent2: #f76b4f;
    --text: #e8eaf0;
    --muted: #8892a4;
    --green: #4caf76;
    --red: #e05858;
    --gold: #f5c842;
    --border: rgba(255,255,255,0.07);
    --radius: 12px;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: 'Inter', 'Segoe UI', system-ui, sans-serif;
       font-size: 14px; line-height: 1.5; }
h1 { font-size: 1.8rem; font-weight: 700; color: #fff; }
h2 { font-size: 1.2rem; font-weight: 600; color: var(--accent); margin-bottom: 14px; text-transform: uppercase;
     letter-spacing: .05em; }
h3 { font-size: 1rem; font-weight: 600; color: #fff; margin-bottom: 10px; }
.page { max-width: 1400px; margin: 0 auto; padding: 24px 16px; }
.header { display: flex; align-items: center; gap: 16px; padding: 24px 28px;
          background: var(--surface); border-radius: var(--radius); margin-bottom: 28px; }
.header-text { flex: 1; }
.header-meta { color: var(--muted); font-size: .85rem; margin-top: 4px; }
.badge { background: var(--accent); color: #fff; border-radius: 6px; padding: 3px 10px;
         font-size: .75rem; font-weight: 600; }
.section { margin-bottom: 32px; }
.card { background: var(--surface); border-radius: var(--radius); padding: 22px 24px;
        border: 1px solid var(--border); }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
.grid-auto { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; }
table { width: 100%; border-collapse: collapse; }
thead th { background: var(--surface2); color: var(--muted); font-size: .75rem; font-weight: 600;
           text-transform: uppercase; letter-spacing: .06em; padding: 9px 12px; text-align: left; }
thead th:not(:first-child) { text-align: right; }
tbody tr { border-bottom: 1px solid var(--border); transition: background .15s; }
tbody tr:hover { background: var(--surface2); }
tbody td { padding: 9px 12px; }
tbody td:not(:first-child) { text-align: right; color: var(--muted); }
tbody td:first-child { color: var(--text); }
.rank { color: var(--muted); font-size: .8rem; width: 28px; }
.team-name { font-weight: 500; }
.pts { color: var(--accent) !important; font-weight: 700; font-size: .95rem; }
.good { color: var(--green) !important; font-weight: 600; }
.bad  { color: var(--red) !important; font-weight: 600; }
.stat-row { display: flex; justify-content: space-between; align-items: center;
            padding: 7px 0; border-bottom: 1px solid var(--border); }
.stat-row:last-child { border-bottom: none; }
.stat-label { color: var(--muted); font-size: .85rem; }
.stat-value { font-weight: 600; font-size: .95rem; }
.bar-wrap { height: 6px; background: var(--surface2); border-radius: 3px; flex: 1;
            margin: 0 10px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 3px; background: var(--accent); }
.bar-fill.red { background: var(--red); }
.pp-pct { font-weight: 700; font-size: .85rem; color: var(--gold); }
.scorer-row td { font-size: .85rem; }
.scorer-name { font-weight: 500; }
.chip { display: inline-block; background: var(--surface2); border-radius: 4px;
        padding: 2px 7px; font-size: .75rem; margin-left: 4px; color: var(--muted); }
.schedule-item { display: flex; align-items: center; gap: 10px; padding: 10px 0;
                 border-bottom: 1px solid var(--border); }
.schedule-item:last-child { border-bottom: none; }
.schedule-teams { flex: 1; }
.schedule-home, .schedule-guest { font-weight: 500; }
.schedule-vs { color: var(--muted); font-size: .8rem; margin: 2px 0; }
.schedule-meta { text-align: right; color: var(--muted); font-size: .82rem; min-width: 120px; }
.result-score { font-size: 1.1rem; font-weight: 700; color: var(--accent); text-align: right; min-width: 60px; }
.tabs { display: flex; gap: 6px; margin-bottom: 16px; flex-wrap: wrap; }
.tab { padding: 6px 14px; border-radius: 8px; cursor: pointer; font-size: .85rem; font-weight: 500;
       background: var(--surface2); color: var(--muted); border: 1px solid var(--border);
       transition: all .15s; }
.tab:hover { color: var(--text); }
.tab.active { background: var(--accent); color: #fff; border-color: var(--accent); }
.team-section { display: none; }
.team-section.active { display: block; }
.top3 td:nth-child(1) { color: var(--gold); font-weight: 700; }
@media (max-width: 768px) {
    .grid-2, .grid-3 { grid-template-columns: 1fr; }
    .grid-auto { grid-template-columns: 1fr; }
}
.refresh-btn { display: flex; align-items: center; gap: 7px; background: var(--accent);
    color: #fff; border: none; border-radius: 8px; padding: 9px 18px; font-size: .9rem;
    font-weight: 600; cursor: pointer; transition: opacity .15s; white-space: nowrap; }
.refresh-btn:hover { opacity: .85; }
.refresh-btn:disabled { opacity: .5; cursor: not-allowed; }
.refresh-btn svg { width: 16px; height: 16px; flex-shrink: 0; }
.refresh-status { font-size: .82rem; color: var(--muted); margin-top: 6px; min-height: 18px; }
.refresh-status.ok  { color: var(--green); }
.refresh-status.err { color: var(--red); }
"""

WORKER_URL = "https://saisonmanager-report.veit-bammel.workers.dev"

JS = f"""
function showTab(section, teamId) {{
    document.querySelectorAll('#' + section + ' .team-section').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('#' + section + ' .tab').forEach(el => el.classList.remove('active'));
    document.getElementById(section + '-team-' + teamId).classList.add('active');
    document.getElementById(section + '-tab-' + teamId).classList.add('active');
}}

async function triggerRefresh() {{
    const btn    = document.getElementById('refresh-btn');
    const status = document.getElementById('refresh-status');
    btn.disabled = true;
    status.className = 'refresh-status';
    status.textContent = 'Starting update...';

    try {{
        const res  = await fetch('{WORKER_URL}', {{ method: 'POST' }});
        const data = await res.json();
        if (data.ok) {{
            status.className = 'refresh-status ok';
            status.textContent = 'Update started. Reload page in ~2 min.';
        }} else {{
            status.className = 'refresh-status err';
            status.textContent = 'Error: ' + (data.error || 'Unknown');
        }}
    }} catch (e) {{
        status.className = 'refresh-status err';
        status.textContent = 'Network error: ' + e.message;
    }} finally {{
        btn.disabled = false;
    }}
}}
"""


def pct(n, d):
    if d == 0:
        return 0.0
    return round(n / d * 100, 1)


def fmt_pct(n, d):
    return f"{pct(n, d):.1f}%"


def bar(value, max_val, cls=""):
    w = round(value / max_val * 100) if max_val else 0
    return f'<div class="bar-wrap"><div class="bar-fill {cls}" style="width:{w}%"></div></div>'


# ---------------------------------------------------------------------------
# HTML sections
# ---------------------------------------------------------------------------

def html_header(league_info: dict) -> str:
    name = league_info.get("name", "")
    op = league_info.get("game_operation_name", "")
    return f"""
    <div class="header">
      <div class="header-text">
        <h1>{name}</h1>
        <div class="header-meta">{op} &nbsp;·&nbsp; As of: {datetime.now().strftime('%Y-%m-%d %H:%M')}</div>
      </div>
      <div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px;">
          <span class="badge">Season 2025/2026</span>
          <button id="refresh-btn" class="refresh-btn" onclick="triggerRefresh()">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"
                 stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 2v6h-6"/><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/>
              <path d="M3 22v-6h6"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/>
            </svg>
            Refresh Data
          </button>
          <div id="refresh-status" class="refresh-status"></div>
      </div>
    </div>
    """


def html_standings(table_data: list[dict]) -> str:
    rows = ""
    for t in sorted(table_data, key=lambda x: x.get("sort", 99)):
        pos = t.get("position", "")
        name = t.get("team_name", "")
        g = t.get("games", 0)
        w = t.get("won", 0)
        wot = t.get("won_ot", 0)
        lot = t.get("lost_ot", 0)
        l = t.get("lost", 0)
        gs = t.get("goals_scored", 0)
        gr = t.get("goals_received", 0)
        pts = t.get("points", 0)
        diff = gs - gr
        diff_str = f"+{diff}" if diff > 0 else str(diff)
        diff_cls = "good" if diff > 0 else ("bad" if diff < 0 else "")
        rows += f"""
        <tr>
          <td class="rank">{pos}</td>
          <td class="team-name">{name}</td>
          <td>{g}</td>
          <td>{w}</td>
          <td>{wot}</td>
          <td>{lot}</td>
          <td>{l}</td>
          <td>{gs}:{gr}</td>
          <td class="{diff_cls}">{diff_str}</td>
          <td class="pts">{pts}</td>
        </tr>"""
    return f"""
    <div class="section card">
      <h2>Standings</h2>
      <table>
        <thead><tr>
          <th>#</th><th>Team</th><th>GP</th><th>W</th><th>OTW</th><th>OTL</th><th>L</th>
          <th>Goals</th><th>Diff</th><th>Pts</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""


def html_team_overview(table_data: list[dict], pp_bp: dict[int, dict]) -> str:
    cards = ""
    sorted_teams = sorted(table_data, key=lambda x: x.get("sort", 99))
    max_pp_pct = max((pct(pp_bp.get(t.get("team_id", 0), {}).get("pp_goals", 0),
                          pp_bp.get(t.get("team_id", 0), {}).get("pp_opps", 1)) for t in sorted_teams), default=1) or 1
    max_bp_pct = max((pct(pp_bp.get(t.get("team_id", 0), {}).get("bp_goals", 0),
                          pp_bp.get(t.get("team_id", 0), {}).get("bp_opps", 1)) for t in sorted_teams), default=1) or 1

    for t in sorted_teams:
        tid = t.get("team_id", 0)
        name = t.get("team_name", "")
        g = t.get("games", 0) or 1
        gs = t.get("goals_scored", 0)
        gr = t.get("goals_received", 0)
        pp = pp_bp.get(tid, {})
        pp_opps = pp.get("pp_opps", 0)
        pp_goals = pp.get("pp_goals", 0)
        bp_opps = pp.get("bp_opps", 0)
        bp_goals = pp.get("bp_goals", 0)
        pp_p = pct(pp_goals, pp_opps)
        bp_p = pct(bp_goals, bp_opps)
        bp_save = 100 - bp_p

        cards += f"""
        <div class="card">
          <h3>{name}</h3>
          <div class="stat-row">
            <span class="stat-label">Avg Goals / Game</span>
            <span class="stat-value good">{gs/g:.2f}</span>
          </div>
          <div class="stat-row">
            <span class="stat-label">Avg Goals Against / Game</span>
            <span class="stat-value bad">{gr/g:.2f}</span>
          </div>
          <div class="stat-row">
            <span class="stat-label">Power Play</span>
            <div style="display:flex;align-items:center;gap:6px;">
              {bar(pp_p, max_pp_pct)}
              <span class="pp-pct">{pp_p:.1f}%</span>
              <span class="chip">{pp_goals}/{pp_opps}</span>
            </div>
          </div>
          <div class="stat-row">
            <span class="stat-label">Penalty Kill</span>
            <div style="display:flex;align-items:center;gap:6px;">
              {bar(bp_save, 100)}
              <span class="pp-pct">{bp_save:.1f}%</span>
              <span class="chip">{bp_opps - bp_goals}/{bp_opps}</span>
            </div>
          </div>
          <div class="stat-row">
            <span class="stat-label">Goals Against (Shorthanded)</span>
            <span class="stat-value bad">{bp_goals}</span>
          </div>
        </div>"""
    return f"""
    <div class="section">
      <h2>Team Overview</h2>
      <div class="grid-auto">{cards}</div>
    </div>"""


def html_pp_bp_table(table_data: list[dict], pp_bp: dict[int, dict]) -> str:
    sorted_teams = sorted(table_data, key=lambda x: -pct(
        pp_bp.get(x.get("team_id", 0), {}).get("pp_goals", 0),
        pp_bp.get(x.get("team_id", 0), {}).get("pp_opps", 1)
    ))
    rows = ""
    for t in sorted_teams:
        tid = t.get("team_id", 0)
        name = t.get("team_name", "")
        pp = pp_bp.get(tid, {})
        pp_opps = pp.get("pp_opps", 0)
        pp_goals = pp.get("pp_goals", 0)
        bp_opps = pp.get("bp_opps", 0)
        bp_goals = pp.get("bp_goals", 0)
        bp_kills = bp_opps - bp_goals
        rows += f"""
        <tr>
          <td class="team-name">{name}</td>
          <td>{pp_goals}/{pp_opps}</td>
          <td class="good">{fmt_pct(pp_goals, pp_opps)}</td>
          <td>{bp_kills}/{bp_opps}</td>
          <td class="good">{fmt_pct(bp_kills, bp_opps)}</td>
          <td class="bad">{bp_goals}</td>
        </tr>"""
    return f"""
    <div class="section card">
      <h2>Power Play &amp; Penalty Kill Comparison</h2>
      <table>
        <thead><tr>
          <th>Team</th>
          <th>PP Goals / Opp</th><th>PP%</th>
          <th>PK / Opp</th><th>PK%</th>
          <th>GA Shorthanded</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""


def html_top_scorers(scorer_data: list[dict], top_n=20) -> str:
    sorted_scorers = sorted(scorer_data, key=lambda p: (p.get("goals", 0) + p.get("assists", 0)), reverse=True)[:top_n]
    rows = ""
    for i, p in enumerate(sorted_scorers, 1):
        g = p.get("goals", 0)
        a = p.get("assists", 0)
        pts = g + a
        pen2 = p.get("penalty_2", 0)
        pen5 = p.get("penalty_5", 0)
        games = p.get("games", 0)
        cls = "top3" if i <= 3 else ""
        rows += f"""
        <tr class="{cls}">
          <td>{i}</td>
          <td class="scorer-name">{p.get('first_name','')} {p.get('last_name','')}</td>
          <td>{p.get('team_name','')}</td>
          <td>{games}</td>
          <td class="good">{g}</td>
          <td>{a}</td>
          <td class="pts">{pts}</td>
          <td>{pen2}</td>
          <td>{pen5}</td>
        </tr>"""
    return f"""
    <div class="section card">
      <h2>Top Scorers (League)</h2>
      <table>
        <thead><tr>
          <th>#</th><th>Player</th><th>Team</th><th>GP</th>
          <th>G</th><th>A</th><th>Pts</th>
          <th>Pen2'</th><th>Pen5'</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""


def html_team_scorers(scorer_data: list[dict], table_data: list[dict]) -> str:
    teams_by_score = scorers_by_team(scorer_data)
    sorted_teams = sorted(table_data, key=lambda x: x.get("sort", 99))

    tabs = ""
    sections = ""
    first = True
    for t in sorted_teams:
        name = t.get("team_name", "")
        tid = t.get("team_id", 0)
        safe_id = str(tid)
        active = "active" if first else ""
        tabs += f'<div class="tab {active}" id="scorers-tab-{safe_id}" onclick="showTab(\'scorers\',\'{safe_id}\')">{name}</div>'

        players = teams_by_score.get(name, [])
        rows = ""
        for i, p in enumerate(players, 1):
            g = p.get("goals", 0)
            a = p.get("assists", 0)
            pts = g + a
            pen2 = p.get("penalty_2", 0)
            pen5 = p.get("penalty_5", 0)
            pen_total = pen2 * 2 + pen5 * 5
            games = p.get("games", 0)
            rows += f"""
            <tr>
              <td>{i}</td>
              <td class="scorer-name">{p.get('first_name','')} {p.get('last_name','')}</td>
              <td>{games}</td>
              <td class="good">{g}</td>
              <td>{a}</td>
              <td class="pts">{pts}</td>
              <td>{pen2}</td>
              <td>{pen5}</td>
              <td>{pen_total}'</td>
            </tr>"""

        sections += f"""
        <div class="team-section {active}" id="scorers-team-{safe_id}">
          <table>
            <thead><tr>
              <th>#</th><th>Player</th><th>GP</th>
              <th>G</th><th>A</th><th>Pts</th>
              <th>Pen2'</th><th>Pen5'</th><th>PIM</th>
            </tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>"""
        first = False

    return f"""
    <div class="section card" id="scorers">
      <h2>Scorer Stats by Team</h2>
      <div class="tabs">{tabs}</div>
      {sections}
    </div>"""


def html_penalty_leaders(scorer_data: list[dict]) -> str:
    """Top 10 players by penalty minutes."""
    def total_pen(p):
        return p.get("penalty_2", 0) * 2 + p.get("penalty_5", 0) * 5

    sorted_p = sorted(scorer_data, key=total_pen, reverse=True)[:15]
    rows = ""
    for i, p in enumerate(sorted_p, 1):
        pen2 = p.get("penalty_2", 0)
        pen5 = p.get("penalty_5", 0)
        total = total_pen(p)
        rows += f"""
        <tr>
          <td>{i}</td>
          <td class="scorer-name">{p.get('first_name','')} {p.get('last_name','')}</td>
          <td>{p.get('team_name','')}</td>
          <td>{pen2}</td>
          <td>{pen5}</td>
          <td class="bad">{total}'</td>
        </tr>"""
    return f"""
    <div class="section card">
      <h2>Penalty Minutes Leaders</h2>
      <table>
        <thead><tr>
          <th>#</th><th>Player</th><th>Team</th>
          <th>Penalties 2'</th><th>Penalties 5'</th><th>Total PIM</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""


def html_schedule(schedule_data: list[dict], game_day: int) -> str:
    items = ""
    for g in schedule_data:
        home = g.get("home_team_name", "")
        guest = g.get("guest_team_name", "")
        date_str = g.get("date", "")
        time_str = g.get("time", "")
        arena = g.get("arena_name", "")
        result = g.get("result_string", "")
        ended = g.get("ended", False)
        result_html = f'<div class="result-score">{result}</div>' if ended and result else \
                      f'<div class="result-score" style="color:var(--muted)">–</div>'
        items += f"""
        <div class="schedule-item">
          <div class="schedule-teams">
            <div class="schedule-home">{home}</div>
            <div class="schedule-vs">vs.</div>
            <div class="schedule-guest">{guest}</div>
          </div>
          {result_html}
          <div class="schedule-meta">
            <div>{date_str} {time_str}</div>
            <div>{arena}</div>
          </div>
        </div>"""
    return f"""
    <div class="section card">
      <h2>Game Day {game_day}</h2>
      {items}
    </div>"""


def html_goals_comparison(table_data: list[dict]) -> str:
    sorted_teams = sorted(table_data, key=lambda x: x.get("sort", 99))
    max_scored = max(t.get("goals_scored", 0) for t in sorted_teams) or 1
    max_received = max(t.get("goals_received", 0) for t in sorted_teams) or 1

    rows = ""
    for t in sorted_teams:
        name = t.get("team_name", "")
        g = t.get("games", 0) or 1
        gs = t.get("goals_scored", 0)
        gr = t.get("goals_received", 0)
        rows += f"""
        <tr>
          <td class="team-name">{name}</td>
          <td>
            <div style="display:flex;align-items:center;gap:6px;">
              {bar(gs, max_scored)}
              <span class="good">{gs}</span>
              <span class="chip">{gs/g:.2f}/GP</span>
            </div>
          </td>
          <td>
            <div style="display:flex;align-items:center;gap:6px;">
              {bar(gr, max_received, 'red')}
              <span class="bad">{gr}</span>
              <span class="chip">{gr/g:.2f}/GP</span>
            </div>
          </td>
          <td>{"+" if gs-gr >= 0 else ""}{gs - gr}</td>
        </tr>"""
    return f"""
    <div class="section card">
      <h2>Goals &amp; Goals Against Comparison</h2>
      <table>
        <thead><tr>
          <th>Team</th><th>Goals Scored</th><th>Goals Against</th><th>Difference</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Fetching league data...")
    league_info = fetch(f"{BASE_URL}/leagues/{LEAGUE_ID}.json")
    table_data = fetch(f"{BASE_URL}/leagues/{LEAGUE_ID}/table.json")
    scorer_data = fetch(f"{BASE_URL}/leagues/{LEAGUE_ID}/scorer.json")
    current_schedule = fetch(f"{BASE_URL}/leagues/{LEAGUE_ID}/game_days/current/schedule.json")

    game_day_numbers = league_info.get("game_day_numbers", [])
    gd = current_schedule[0].get("game_day", {}) if current_schedule else {}
    current_game_day = gd.get("game_day_number", gd) if isinstance(gd, dict) else gd

    print(f"Fetching {len(game_day_numbers)} game day schedules...")
    all_schedules = fetch_all_schedules(game_day_numbers)

    # Collect finished game IDs
    finished_game_ids = []
    for day, games in all_schedules.items():
        for g in games:
            if g.get("ended"):
                finished_game_ids.append(g["game_id"])

    print(f"Fetching {len(finished_game_ids)} finished game details for PP/BP stats...")
    game_details = fetch_all_game_details(finished_game_ids)

    print("Computing PowerPlay / Boxplay statistics...")
    pp_bp = compute_pp_bp_stats(game_details)

    print("Building HTML report...")

    body = (
        html_header(league_info)
        + html_standings(table_data)
        + html_goals_comparison(table_data)
        + html_team_overview(table_data, pp_bp)
        + html_pp_bp_table(table_data, pp_bp)
        + html_top_scorers(scorer_data, top_n=25)
        + html_team_scorers(scorer_data, table_data)
        + html_penalty_leaders(scorer_data)
        + html_schedule(current_schedule, current_game_day)
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>League Stats – {league_info.get('name','')}</title>
  <style>{CSS}</style>
</head>
<body>
  <div class="page">
    {body}
  </div>
  <script>{JS}</script>
</body>
</html>"""

    output_path = "docs/index.html"
    import os
    os.makedirs("output", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Report saved: {output_path}")
    return output_path


if __name__ == "__main__":
    main()
