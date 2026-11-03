"""Structured sources: football results (ESPN), CISA KEV and NVD critical CVEs.

Each fetcher returns Items with `lines` set, so they skip page fetching and the AI.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests

from .config import Source
from .models import Item
from .util import short_key, truncate

log = logging.getLogger("news-bot")

ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer"
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
NVD_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
TIMEOUT = 20


def _get(session: requests.Session, url: str, **params) -> dict:
    resp = session.get(url, params=params or None, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc) \
        if "+" in value or value.endswith("Z") else datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


# --- football ----------------------------------------------------------------------------------

def _stat(team: dict, name: str) -> str:
    return next((s["displayValue"] for s in team.get("statistics", []) if s["name"] == name), "-")


def _match_lines(league_name: str, event: dict, summary: dict | None) -> tuple[str, list[str], dict]:
    comp = event["competitions"][0]
    teams = {c["homeAway"]: c for c in comp["competitors"]}
    home, away = teams["home"], teams["away"]
    hn, an = home["team"]["displayName"], away["team"]["displayName"]
    title = f"{hn} {home['score']}–{away['score']} {an}"
    status = event["status"]["type"].get("shortDetail", "FT")
    lines = [f"{league_name} · {status}"]
    card = {"league": league_name, "status": status,
            "home": {"name": hn, "score": home["score"], "logo": home["team"].get("logo", "")},
            "away": {"name": an, "score": away["score"], "logo": away["team"].get("logo", "")}}
    names = {c["team"]["id"]: c["team"]["displayName"] for c in comp["competitors"]}
    goals = []
    for d in comp.get("details", []):
        if d.get("scoringPlay"):
            who = (d.get("athletesInvolved") or [{}])[0].get("displayName", "?")
            tag = " (pen)" if d.get("penaltyKick") else " (og)" if d.get("ownGoal") else ""
            goals.append(f"{who} {d['clock']['displayValue']}{tag} [{names.get(d['team']['id'], '?')}]")
    if goals:
        lines += ["", "Goals: " + ", ".join(goals)]
    box = {t["homeAway"]: t for t in (summary or {}).get("boxscore", {}).get("teams", [])}
    if "home" in box and "away" in box:
        h, a = box["home"], box["away"]

        def pct(v: str) -> str:
            return v if v == "-" else f"{round(float(v))}%"

        lines += [
            "", "Stats (home – away)",
            f"Possession: {pct(_stat(h, 'possessionPct'))} – {pct(_stat(a, 'possessionPct'))}",
            f"Shots (on target): {_stat(h, 'totalShots')} ({_stat(h, 'shotsOnTarget')}) – "
            f"{_stat(a, 'totalShots')} ({_stat(a, 'shotsOnTarget')})",
            f"Corners: {_stat(h, 'wonCorners')} – {_stat(a, 'wonCorners')}",
            f"Passes: {_stat(h, 'accuratePasses')}/{_stat(h, 'totalPasses')} – "
            f"{_stat(a, 'accuratePasses')}/{_stat(a, 'totalPasses')}",
            f"Fouls: {_stat(h, 'foulsCommitted')} – {_stat(a, 'foulsCommitted')}",
            f"Yellow cards: {_stat(h, 'yellowCards')} – {_stat(a, 'yellowCards')} · "
            f"Red: {_stat(h, 'redCards')} – {_stat(a, 'redCards')}",
        ]
    return title, lines, card


def fetch_espn(session: requests.Session, src: Source) -> list[Item]:
    slug, league_name = src.params["league"], src.params.get("league_name", src.name)
    now = datetime.now(timezone.utc)
    events = {}
    for i in range(3):  # today and the two days before
        day = (now - timedelta(days=i)).strftime("%Y%m%d")
        try:
            for e in _get(session, f"{ESPN}/{slug}/scoreboard", dates=day)["events"]:
                events[e["id"]] = e
        except requests.RequestException as exc:
            log.warning("[%s] scoreboard %s failed: %s", src.name, day, exc)
    items = []
    for e in events.values():
        if not e["status"]["type"].get("completed"):
            continue
        try:
            summary = _get(session, f"{ESPN}/{slug}/summary", event=e["id"])
        except requests.RequestException:
            summary = None
        title, lines, card = _match_lines(league_name, e, summary)
        items.append(Item(
            source=src, title=title,
            link=f"https://www.espn.com/soccer/match/_/gameId/{e['id']}",
            summary=" ".join(lines[:1]), lines=lines, published=_utc(e["date"]),
            keys=[short_key(f"espn:{slug}:{e['id']}")], publisher="ESPN",
            extra={"card": card},
        ))
    return items


# --- CVEs --------------------------------------------------------------------------------------

def fetch_kev(session: requests.Session, src: Source) -> list[Item]:
    items = []
    for v in _get(session, KEV_URL)["vulnerabilities"]:
        cid = v["cveID"]
        lines = [
            f"{v['vendorProject']} {v['product']}: {v['vulnerabilityName']}",
            truncate(" ".join(v["shortDescription"].split()), 400),
        ]
        if v.get("knownRansomwareCampaignUse") == "Known":
            lines.append("Used in ransomware campaigns.")
        lines.append(f"Patch by: {v['dueDate']}")
        items.append(Item(
            source=src, title=f"{cid} added to CISA KEV (exploited in the wild)",
            link=f"https://nvd.nist.gov/vuln/detail/{cid}", summary=lines[1], lines=lines,
            published=_utc(v["dateAdded"] + "T00:00:00+00:00"), keys=[short_key(f"kev:{cid}")],
            publisher="CISA",
        ))
    return items


def fetch_nvd_critical(session: requests.Session, src: Source) -> list[Item]:
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%S.000"
    data = _get(session, NVD_URL, cvssV3Severity="CRITICAL",
                pubStartDate=(now - timedelta(hours=26)).strftime(fmt),
                pubEndDate=now.strftime(fmt), resultsPerPage=40)
    items = []
    for v in data["vulnerabilities"]:
        c = v["cve"]
        desc = next((d["value"] for d in c["descriptions"] if d["lang"] == "en"), "")
        metrics = c.get("metrics", {})
        cvss = (metrics.get("cvssMetricV31") or metrics.get("cvssMetricV30") or [{}])[0].get("cvssData", {})
        lines = [truncate(" ".join(desc.split()), 400)]
        items.append(Item(
            source=src, title=f"{c['id']} · CVSS {cvss.get('baseScore', '?')} CRITICAL",
            link=f"https://nvd.nist.gov/vuln/detail/{c['id']}", summary=lines[0], lines=lines,
            published=_utc(c["published"] + "+00:00"), keys=[short_key(f"nvd:{c['id']}")],
            publisher="NVD",
        ))
    return items


FETCHERS = {"espn": fetch_espn, "cisa_kev": fetch_kev, "nvd_critical": fetch_nvd_critical}
