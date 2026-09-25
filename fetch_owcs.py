#!/usr/bin/env python3
"""OWCS Liquipedia -> normalized JSON cache.

This runs OUTSIDE Google Apps Script so Liquipedia's required custom User-Agent
can be sent correctly. Intended for GitHub Actions once per day.

Data source: Liquipedia Overwatch MediaWiki API (action=parse only).
"""
from __future__ import annotations

import copy
import html as html_lib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

VERSION = "3.0.0"
API_BASE = "https://liquipedia.net/overwatch/api.php"
SOURCE_BASE = "https://liquipedia.net/overwatch/"
OUTPUT = Path("data/owcs.json")
JST = ZoneInfo("Asia/Tokyo")
PARSE_INTERVAL_SECONDS = 31.0
GENERAL_INTERVAL_SECONDS = 2.1

ROLE_NAMES = {"DPS", "Tank", "Support", "Flex"}
STAFF_NAMES = {
    "Head Coach", "Assistant Coach", "Coach", "Analyst",
    "Team Manager", "Manager", "General Manager"
}

REGIONS: dict[str, dict[str, Any]] = {
    "JP": {
        "label": "Japan",
        "expected_teams": 8,
        "start_date": "2026-09-21",
        "strict_standings": True,
        "overview": "Overwatch_Champions_Series/2026/Asia/Stage_3/Japan",
        "regular": "Overwatch_Champions_Series/2026/Asia/Stage_3/Japan/Regular_Season",
        "rosters": True,
        "seed_teams": [
            "VARREL", "MURASH GAMING", "ENTER FORCE.36", "Uwinks",
            "99DIVINE", "Please Not Hero Ban", "Lazuli", "REVATI",
        ],
    },
    "KR": {
        "label": "Korea",
        "expected_teams": 9,
        "start_date": "2026-10-02",
        "strict_standings": True,
        "overview": "Overwatch_Champions_Series/2026/Asia/Stage_3/Korea",
        "regular": "Overwatch_Champions_Series/2026/Asia/Stage_3/Korea/Regular_Season",
        "rosters": True,
        "seed_teams": [
            "ZETA DIVISION", "Crazy Raccoon", "T1", "ZANSIDE GAMING",
            "Team Falcons", "O2 Blast", "Cheeseburger", "Poker Face", "SEIJI ESPORTS",
        ],
    },
    "NA": {
        "label": "NA",
        "expected_teams": 6,
        "start_date": "2026-10-10",
        "strict_standings": False,
        "overview": "Overwatch_Champions_Series/2026/NA/Stage_3",
        "regular": "Overwatch_Champions_Series/2026/NA/Stage_3/Regular_Season",
        "rosters": False,
        "seed_teams": [],
    },
    "EMEA": {
        "label": "EMEA",
        "expected_teams": 6,
        "start_date": "2026-10-10",
        "strict_standings": False,
        "overview": "Overwatch_Champions_Series/2026/EMEA/Stage_3",
        "regular": "Overwatch_Champions_Series/2026/EMEA/Stage_3/Regular_Season",
        "rosters": False,
        "seed_teams": [],
    },
    "CHINA": {
        "label": "China",
        "expected_teams": 8,
        "start_date": "2026-10-03",
        "strict_standings": False,
        "overview": "Overwatch_Champions_Series/2026/China/Stage_3",
        "regular": "Overwatch_Champions_Series/2026/China/Stage_3/Regular_Season",
        "rosters": False,
        "seed_teams": [],
    },
}


@dataclass
class ApiClient:
    contact: str

    def __post_init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": f"OWCSCommentaryCache/{VERSION} ({self.contact})",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        })
        self.last_request_at = 0.0
        self.last_parse_at = 0.0

    def _wait(self, is_parse: bool) -> None:
        now = time.monotonic()
        wait_general = GENERAL_INTERVAL_SECONDS - (now - self.last_request_at)
        wait_parse = PARSE_INTERVAL_SECONDS - (now - self.last_parse_at) if is_parse else 0
        wait_for = max(0.0, wait_general, wait_parse)
        if wait_for:
            time.sleep(wait_for)

    def parse(self, page: str) -> tuple[str, str, str | int | None]:
        self._wait(is_parse=True)
        params = {
            "action": "parse",
            "page": page,
            "prop": "text|wikitext|revid",
            "disableeditsection": 1,
            "format": "json",
            "formatversion": 2,
        }
        response = self.session.get(API_BASE, params=params, timeout=30)
        self.last_request_at = time.monotonic()
        self.last_parse_at = self.last_request_at
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise RuntimeError(f"Liquipedia API error for {page}: {data['error']}")
        parsed = data.get("parse") or {}
        text = parsed.get("text")
        wikitext = parsed.get("wikitext")
        if not text:
            raise RuntimeError(f"No parsed HTML returned for {page}")
        if not wikitext:
            raise RuntimeError(f"No wikitext returned for {page}")
        return str(text), str(wikitext), parsed.get("revid")


def source_url(page: str) -> str:
    # Slashes and underscores are already suitable in the wiki URL; quote only unusual chars.
    return SOURCE_BASE + quote(page, safe="/_-().")


def normalize_dash(value: str) -> str:
    return re.sub(r"\s+", "", str(value).replace("–", "-").replace("—", "-").replace("−", "-"))


def clean_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip() and line.strip().lower() != "edit" and line.strip() != "[edit]"]


def normalize_team_key(value: str) -> str:
    value = html_lib.unescape(str(value or "")).lower()
    return re.sub(r"[^a-z0-9\u3040-\u30ff\u3400-\u9fff]", "", value)


TEAM_ALIASES = {
    "JP": {
        "enterforce36": "ENTER FORCE.36", "e36": "ENTER FORCE.36", "ef36": "ENTER FORCE.36",
        "varrel": "VARREL", "vl": "VARREL", "var": "VARREL",
        "murashgaming": "MURASH GAMING", "mrg": "MURASH GAMING", "mg": "MURASH GAMING",
        "pleasenotheroban": "Please Not Hero Ban", "pnhb": "Please Not Hero Ban", "pnh": "Please Not Hero Ban",
        "99divine": "99DIVINE", "99d": "99DIVINE", "uwinks": "Uwinks", "uw": "Uwinks",
        "lazuli": "Lazuli", "laz": "Lazuli", "revati": "REVATI", "rev": "REVATI",
    },
    "KR": {
        "zetadivision": "ZETA DIVISION", "zeta": "ZETA DIVISION",
        "crazyraccoon": "Crazy Raccoon", "cr": "Crazy Raccoon", "t1": "T1",
        "zansidegaming": "ZANSIDE GAMING", "zsg": "ZANSIDE GAMING",
        "teamfalcons": "Team Falcons", "falcons": "Team Falcons", "flc": "Team Falcons",
        "o2blast": "O2 Blast", "o2b": "O2 Blast", "cheeseburger": "Cheeseburger", "cb": "Cheeseburger",
        "pokerface": "Poker Face", "pf": "Poker Face", "seijiesports": "SEIJI ESPORTS", "seiji": "SEIJI ESPORTS",
    },
}


def canonical_team(region: str, raw: str) -> str:
    clean = clean_wiki_value(raw)
    if not clean:
        return ""
    key = normalize_team_key(clean)
    if key in TEAM_ALIASES.get(region, {}):
        return TEAM_ALIASES[region][key]
    for team in REGIONS.get(region, {}).get("seed_teams", []):
        if normalize_team_key(team) == key:
            return team
    return clean


def read_balanced_template(text: str, start: int) -> str:
    if text[start:start+2] != "{{":
        return ""
    depth = 0
    i = start
    while i < len(text) - 1:
        two = text[i:i+2]
        if two == "{{":
            depth += 1
            i += 2
            continue
        if two == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                return text[start:i]
            continue
        i += 1
    return ""


def peek_template_name(text: str, start: int) -> str:
    i = start + 2
    chars: list[str] = []
    while i < len(text):
        if text[i] == "|" or text[i:i+2] == "}}" or len(chars) > 80:
            break
        chars.append(text[i])
        i += 1
    return re.sub(r"<!--[\s\S]*?-->", "", "".join(chars)).strip()


def extract_templates_by_name(text: str, wanted: str) -> list[str]:
    out: list[str] = []
    wanted = wanted.lower()
    i = 0
    while i < len(text) - 1:
        if text[i:i+2] != "{{":
            i += 1
            continue
        if peek_template_name(text, i).lower() == wanted:
            raw = read_balanced_template(text, i)
            if raw:
                out.append(raw)
                i += len(raw)
                continue
        i += 1
    return out


def first_template(text: str) -> str:
    i = str(text or "").find("{{")
    return read_balanced_template(str(text or ""), i) if i >= 0 else ""


def split_top_level(text: str, delimiter: str) -> list[str]:
    out: list[str] = []
    start = 0
    tpl = 0
    link = 0
    i = 0
    while i < len(text):
        two = text[i:i+2]
        if two == "{{":
            tpl += 1; i += 2; continue
        if two == "}}":
            tpl = max(0, tpl - 1); i += 2; continue
        if two == "[[":
            link += 1; i += 2; continue
        if two == "]]":
            link = max(0, link - 1); i += 2; continue
        if text[i] == delimiter and tpl == 0 and link == 0:
            out.append(text[start:i]); start = i + 1
        i += 1
    out.append(text[start:])
    return out


def top_level_index_of(text: str, char: str) -> int:
    tpl = 0
    link = 0
    i = 0
    while i < len(text):
        two = text[i:i+2]
        if two == "{{": tpl += 1; i += 2; continue
        if two == "}}": tpl = max(0, tpl - 1); i += 2; continue
        if two == "[[": link += 1; i += 2; continue
        if two == "]]": link = max(0, link - 1); i += 2; continue
        if text[i] == char and tpl == 0 and link == 0:
            return i
        i += 1
    return -1


def parse_template(raw: str) -> dict[str, Any] | None:
    if not raw or not raw.startswith("{{") or not raw.endswith("}}"):
        return None
    parts = split_top_level(raw[2:-2], "|")
    if not parts:
        return None
    name = parts.pop(0).strip()
    params: dict[str, str] = {}
    positional: list[str] = []
    for part in parts:
        eq = top_level_index_of(part, "=")
        if eq >= 0:
            key = part[:eq].strip()
            if key:
                params[key] = part[eq+1:].strip()
        else:
            positional.append(part.strip())
    return {"name": name, "params": params, "positional": positional}


def clean_wiki_value(value: Any) -> str:
    s = str(value if value is not None else "").strip()
    s = re.sub(r"<!--[\s\S]*?-->", " ", s)
    s = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", s)
    s = re.sub(r"\[\[([^\]]+)\]\]", r"\1", s)
    for _ in range(6):
        if "{{" not in s:
            break
        s = re.sub(r"\{\{(?:Hero|Map|Team|TeamIcon|Flag|Abbr)\s*\|\s*([^{}|]+?)(?:\|[^{}]*)?\}\}", r"\1", s, flags=re.I)
        s = re.sub(r"\{\{[^{}]+\}\}", "", s)
    s = s.replace("'''", "").replace("''", "").replace("&nbsp;", " ")
    return html_lib.unescape(re.sub(r"\s+", " ", s).strip())


def parse_opponent(raw: str, region: str) -> tuple[str, int | None]:
    tpl_raw = first_template(raw)
    if not tpl_raw:
        return canonical_team(region, clean_wiki_value(raw)), None
    tpl = parse_template(tpl_raw)
    if not tpl:
        return "", None
    name = str(tpl["name"]).lower()
    if "teamopponent" not in name and name != "opponent":
        return canonical_team(region, clean_wiki_value(raw)), None
    pos = tpl["positional"]
    params = tpl["params"]
    team_raw = (pos[0] if pos else "") or params.get("team", "") or params.get("name", "")
    score_raw = clean_wiki_value(params.get("score", ""))
    return canonical_team(region, team_raw), int(score_raw) if re.fullmatch(r"\d+", score_raw) else None


def parse_liquipedia_date(raw: str, region: str) -> datetime | None:
    if not raw:
        return None
    tz_name = ""
    def repl(m: re.Match[str]) -> str:
        nonlocal tz_name
        tz_name = m.group(1).strip().upper()
        return " "
    s = re.sub(r"\{\{Abbr/([^}|]+)[^}]*\}\}", repl, str(raw), flags=re.I)
    s = clean_wiki_value(s)
    s = re.sub(r"\s+-\s+", " ", s)
    s = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", s, flags=re.I).strip()
    offsets = {"UTC":0,"GMT":0,"JST":9,"KST":9,"CST":8,"SGT":8,"HKT":8,"CEST":2,"CET":1,"BST":1,"EDT":-4,"EST":-5,"CDT":-5,"MDT":-6,"MST":-7,"PDT":-7,"PST":-8}
    if not tz_name:
        tz_name = {"JP":"JST","KR":"KST","CHINA":"CST"}.get(region, "UTC")
    offset = offsets.get(tz_name, 0)
    m = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", s)
    if m:
        y, mo, d = map(int, m.group(1,2,3)); hh = int(m.group(4) or 0); mm = int(m.group(5) or 0)
    else:
        months = {m.lower(): i for i,m in enumerate(["January","February","March","April","May","June","July","August","September","October","November","December"],1)}
        m = re.search(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(20\d{2})(?:\s+(\d{1,2}):(\d{2}))?", s, re.I)
        if not m:
            return None
        mo = months[m.group(1).lower()]; d = int(m.group(2)); y = int(m.group(3)); hh = int(m.group(4) or 0); mm = int(m.group(5) or 0)
    local = datetime(y, mo, d, hh, mm, tzinfo=timezone(timedelta(hours=offset)))
    return local.astimezone(JST)


def extract_team_bans(params: dict[str, str], side: int) -> list[str]:
    out: list[str] = []
    for key, value in params.items():
        k = re.sub(r"[ _-]", "", key.lower())
        if side == 1:
            hit = bool(re.fullmatch(r"t1b\d*|t1ban\d*|team1ban\d*|ban1|hero1ban", k))
        else:
            hit = bool(re.fullmatch(r"t2b\d*|t2ban\d*|team2ban\d*|ban2|hero2ban", k))
        if not hit:
            continue
        v = clean_wiki_value(value)
        if v and v not in out:
            out.append(v)
    return out


def parse_map_template(raw: str, map_no: int, team_a: str, team_b: str) -> dict[str, Any] | None:
    tpl_raw = first_template(raw)
    if not tpl_raw:
        return None
    tpl = parse_template(tpl_raw)
    if not tpl or str(tpl["name"]).lower() != "map":
        return None
    params = tpl["params"]
    if clean_wiki_value(params.get("finished", "")).lower() == "skip":
        return None
    pos = tpl["positional"]
    map_name = clean_wiki_value(params.get("map", "") or params.get("name", "") or (pos[0] if pos else ""))
    score_a = clean_wiki_value(params.get("score1", ""))
    score_b = clean_wiki_value(params.get("score2", ""))
    winner_code = clean_wiki_value(params.get("winner", "")).strip()
    winner = ""
    if winner_code == "1": winner = team_a
    elif winner_code == "2": winner = team_b
    elif winner_code == "0": winner = "DRAW"
    else:
        try:
            a = float(score_a); b = float(score_b)
            winner = team_a if a > b else team_b if b > a else "DRAW"
        except ValueError:
            pass
    bans_a = extract_team_bans(params, 1)
    bans_b = extract_team_bans(params, 2)
    return {
        "map_no": map_no, "map": map_name, "score_a": score_a, "score_b": score_b,
        "winner": winner, "picker": "", "picker_basis": "",
        "ban_a": " / ".join(bans_a), "ban_b": " / ".join(bans_b),
        "received_a": " / ".join(bans_b), "received_b": " / ".join(bans_a),
    }


def parse_matches_wikitext(wikitext: str, region: str, source: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_match in extract_templates_by_name(wikitext, "match"):
        tpl = parse_template(raw_match)
        if not tpl or str(tpl["name"]).lower() != "match":
            continue
        params = tpl["params"]
        team_a, score_a = parse_opponent(params.get("opponent1", ""), region)
        team_b, score_b = parse_opponent(params.get("opponent2", ""), region)
        team_a = team_a or canonical_team(region, params.get("opponent1literal", ""))
        team_b = team_b or canonical_team(region, params.get("opponent2literal", ""))
        if not team_a or not team_b or team_a.lower() == "tbd" or team_b.lower() == "tbd":
            continue
        dt = parse_liquipedia_date(params.get("date", ""), region)
        maps: list[dict[str, Any]] = []
        for n in range(1, 10):
            raw_map = params.get(f"map{n}")
            if not raw_map:
                continue
            mp = parse_map_template(raw_map, n, team_a, team_b)
            if mp:
                maps.append(mp)
        for i, mp in enumerate(maps):
            if i == 0:
                mp["picker"] = ""
                mp["picker_basis"] = "Map 1: not inferred"
            else:
                prev = maps[i-1]
                if prev.get("winner") == team_a:
                    mp["picker"] = team_b
                    mp["picker_basis"] = "Previous map loser"
                elif prev.get("winner") == team_b:
                    mp["picker"] = team_a
                    mp["picker_basis"] = "Previous map loser"
                else:
                    mp["picker"] = ""
                    mp["picker_basis"] = "Previous map draw/unknown"
        if score_a is None and maps:
            score_a = sum(1 for m in maps if m.get("winner") == team_a)
        if score_b is None and maps:
            score_b = sum(1 for m in maps if m.get("winner") == team_b)
        finished_flag = clean_wiki_value(params.get("finished", "")).lower()
        finished = finished_flag in {"true","1","yes"} or ((score_a or 0) > 0 or (score_b or 0) > 0)
        iso = dt.isoformat(timespec="seconds") if dt else None
        key = "|".join([region, iso or "", team_a, team_b])
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "region": region, "date_jst": iso, "team_a": team_a, "team_b": team_b,
            "score_a": score_a, "score_b": score_b, "finished": finished,
            "source": source, "key": key, "maps": maps,
        })
    return sorted(out, key=lambda r: (r.get("date_jst") or "9999", r.get("team_a") or ""))


def find_section(soup: BeautifulSoup, ids: Iterable[str]) -> BeautifulSoup | None:
    marker = None
    for section_id in ids:
        marker = soup.find(id=section_id)
        if marker:
            break
    if not marker:
        return None

    header: Tag | None
    if isinstance(marker, Tag) and marker.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        header = marker
    else:
        header = marker.find_parent(["h1", "h2", "h3", "h4", "h5", "h6"]) if isinstance(marker, Tag) else None
    if not header:
        return None

    root = BeautifulSoup("<div></div>", "html.parser")
    container = root.div
    assert container is not None
    container.append(copy.copy(header))
    for sibling in header.next_siblings:
        if isinstance(sibling, Tag) and sibling.name == "h2":
            break
        container.append(copy.copy(sibling))
    return root


def parse_standings(html: str, expected_teams: int) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    section = find_section(soup, ["Standings"])
    text = (section or soup).get_text("\n", strip=True)
    lines = clean_lines(text)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(rank: int, team: str, match_record: str, map_record: str, map_diff: str) -> bool:
        match_record = normalize_dash(match_record)
        map_record = normalize_dash(map_record)
        map_diff = normalize_dash(map_diff)
        if not re.fullmatch(r"\d+-\d+", match_record):
            return False
        if not re.fullmatch(r"\d+-\d+", map_record):
            return False
        if not re.fullmatch(r"[+-]?\d+", map_diff):
            return False
        team = team.strip()
        if not team or len(team) > 80 or team in seen:
            return False
        if re.fullmatch(r"Current|Week \d+|Standings|Show|Hide|Show Duplicates|Hide Duplicates", team, re.I):
            return False
        mw, ml = map(int, match_record.split("-"))
        mapw, mapl = map(int, map_record.split("-"))
        out.append({
            "rank": rank,
            "team": team,
            "wins": mw,
            "losses": ml,
            "record": match_record,
            "map_wins": mapw,
            "map_losses": mapl,
            "map_record": map_record,
            "map_diff": int(map_diff),
        })
        seen.add(team)
        return True

    i = 0
    while i < len(lines):
        m = re.fullmatch(r"(\d+)\.?", lines[i])
        if m and i + 4 < len(lines):
            if add(int(m.group(1)), lines[i + 1], lines[i + 2], lines[i + 3], lines[i + 4]):
                i += 5
                if expected_teams and len(out) >= expected_teams:
                    break
                continue
        if i + 3 < len(lines):
            if add(len(out) + 1, lines[i], lines[i + 1], lines[i + 2], lines[i + 3]):
                i += 4
                if expected_teams and len(out) >= expected_teams:
                    break
                continue
        i += 1
    return out


def parse_timestamp(raw: str | None) -> datetime | None:
    if not raw or raw == "error":
        return None
    raw = str(raw).strip()
    try:
        if re.fullmatch(r"\d{10}", raw):
            return datetime.fromtimestamp(int(raw), tz=timezone.utc).astimezone(JST)
        if re.fullmatch(r"\d{13}", raw):
            return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc).astimezone(JST)
        if re.fullmatch(r"\d{14}", raw):
            return datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).astimezone(JST)
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(JST)
    except (ValueError, OverflowError):
        return None


def parse_matches(html: str, region: str, source: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    popups = soup.select(".brkts-match-info-popup")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for popup in popups:
        timestamp_tag = popup.find(attrs={"data-timestamp": True})
        raw_ts = timestamp_tag.get("data-timestamp") if isinstance(timestamp_tag, Tag) else popup.get("data-timestamp")
        dt = parse_timestamp(raw_ts)
        names = []
        for node in popup.select("span.name"):
            value = node.get_text(" ", strip=True)
            if value:
                names.append(value)
            if len(names) >= 2:
                break
        if len(names) < 2:
            continue
        scores: list[int] = []
        for node in popup.select(".match-info-header-scoreholder-score"):
            value = node.get_text(" ", strip=True)
            if re.fullmatch(r"\d+", value):
                scores.append(int(value))
            if len(scores) >= 2:
                break
        score_a: int | None = scores[0] if len(scores) >= 1 else None
        score_b: int | None = scores[1] if len(scores) >= 2 else None
        finished = popup.find(attrs={"data-finished": "finished"}) is not None or popup.get("data-finished") == "finished"
        if not finished and score_a is not None and score_b is not None and (score_a > 0 or score_b > 0):
            finished = True
        iso = dt.isoformat(timespec="seconds") if dt else None
        key = "|".join([region, iso or "", names[0], names[1], str(score_a), str(score_b)])
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "region": region,
            "date_jst": iso,
            "team_a": names[0],
            "team_b": names[1],
            "score_a": score_a,
            "score_b": score_b,
            "finished": finished,
            "source": source,
            "key": key,
        })
    return out


def exact_team_anchor_positions(section_html: str, teams: list[str]) -> list[tuple[int, str]]:
    positions: list[tuple[int, str]] = []
    for team in teams:
        # Team labels in Liquipedia cards are typically normal element text.
        pattern = re.compile(r">\s*" + re.escape(team) + r"\s*<", re.I)
        m = pattern.search(section_html)
        if m:
            positions.append((m.start(), team))
    return sorted(positions)


def _next_labels_until_name(span: Tag, max_strings: int = 12) -> list[str]:
    labels: list[str] = []
    count = 0
    for el in span.next_elements:
        if el is span:
            continue
        if isinstance(el, Tag) and el.name == "span" and "name" in (el.get("class") or []):
            break
        if isinstance(el, NavigableString):
            value = str(el).strip()
            if value:
                labels.append(value)
                count += 1
                if count >= max_strings:
                    break
    return labels


def _previous_role_until_name(span: Tag) -> str | None:
    scanned = 0
    for el in span.previous_elements:
        if el is span:
            continue
        if isinstance(el, Tag) and el.name == "span" and "name" in (el.get("class") or []):
            break
        if isinstance(el, Tag) and el.name == "img":
            alt = str(el.get("alt") or "").strip()
            title = str(el.get("title") or "").strip()
            if alt in ROLE_NAMES:
                return alt
            if title in ROLE_NAMES:
                return title
        scanned += 1
        if scanned > 80:
            break
    return None


def parse_rosters(html: str, region: str, team_hints: list[str], source: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    section = find_section(soup, ["Participants"])
    if not section:
        return []
    section_html = str(section)
    anchors = exact_team_anchor_positions(section_html, team_hints)
    rows: list[dict[str, Any]] = []

    for idx, (start, team) in enumerate(anchors):
        end = anchors[idx + 1][0] if idx + 1 < len(anchors) else len(section_html)
        block = BeautifulSoup(section_html[start:end], "html.parser")
        for span in block.select("span.name"):
            player = span.get_text(" ", strip=True)
            if not player or player == team:
                continue
            next_labels = _next_labels_until_name(span)
            staff_role = next((x for x in next_labels if x in STAFF_NAMES), None)
            status = "DNP" if any(x == "DNP" for x in next_labels[:5]) else ""
            if staff_role:
                role = staff_role
            else:
                role = _previous_role_until_name(span)
                if not role:
                    continue
            rows.append({
                "region": region,
                "team": team,
                "role": role,
                "player": player,
                "status": status,
                "source": source,
            })

    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        unique["|".join([row["region"], row["team"], row["role"], row["player"]])] = row
    return list(unique.values())


def parse_bans(html: str, region: str, source: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    section = find_section(soup, ["Ban_Statistics", "Ban Statistics"])
    if not section:
        return []
    lines = clean_lines(section.get_text("\n", strip=True))
    scopes = {"Overall", "Playoffs", "Regular Season"}
    scope = ""
    rows: list[dict[str, Any]] = []
    i = 0
    while i + 1 < len(lines):
        if lines[i] in scopes:
            scope = lines[i]
            i += 1
            continue
        if not scope or lines[i] in {"Hero", "Amount of Bans", "#", "Country / Region", "Representation", "Players"}:
            i += 1
            continue
        if re.fullmatch(r"\d+", lines[i + 1]) and not re.fullmatch(r"\d+", lines[i]):
            rows.append({
                "region": region,
                "scope": scope,
                "hero": lines[i],
                "bans": int(lines[i + 1]),
                "source": source,
            })
            i += 2
        else:
            i += 1
    unique = {"|".join([r["region"], r["scope"], r["hero"]]): r for r in rows}
    return list(unique.values())


def dedupe_matches(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        unique[row["key"]] = row
    return sorted(unique.values(), key=lambda r: (r.get("date_jst") or "9999", r.get("team_a") or ""))


def load_old() -> dict[str, Any]:
    if not OUTPUT.exists():
        return {}
    try:
        return json.loads(OUTPUT.read_text(encoding="utf-8"))
    except Exception:
        return {}


def region_template(key: str, cfg: dict[str, Any]) -> dict[str, Any]:
    return {
        "region": key,
        "label": cfg["label"],
        "standings": [],
        "finished_matches": [],
        "upcoming_matches": [],
        "rosters": [],
        "hero_bans": [],
        "sources": {
            "overview": source_url(cfg["overview"]),
            "regular": source_url(cfg["regular"]),
        },
    }


def run() -> int:
    contact = os.environ.get("LIQUIPEDIA_CONTACT", "").strip()
    if not contact:
        print("ERROR: LIQUIPEDIA_CONTACT is required. Set it as a GitHub Actions secret.", file=sys.stderr)
        return 2

    old = load_old()
    old_regions = old.get("regions") or {}
    result: dict[str, Any] = {
        "schema_version": 2,
        "generator_version": VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_at_jst": datetime.now(JST).isoformat(timespec="seconds"),
        "data_source": "Liquipedia Overwatch Wiki / MediaWiki API",
        "api_terms": "https://liquipedia.net/api-terms-of-use",
        "regions": {},
        "fetch_log": [],
        "errors": [],
    }
    client = ApiClient(contact)

    for region, cfg in REGIONS.items():
        current = copy.deepcopy(old_regions.get(region) or region_template(region, cfg))
        current.setdefault("region", region)
        current.setdefault("label", cfg["label"])
        current.setdefault("sources", {"overview": source_url(cfg["overview"]), "regular": source_url(cfg["regular"])})
        regular_matches: list[dict[str, Any]] = []
        overview_matches: list[dict[str, Any]] = []

        # Regular page: standings + regular-season matches.
        try:
            html, wikitext, revid = client.parse(cfg["regular"])
            standings = parse_standings(html, cfg["expected_teams"])
            today_jst = datetime.now(JST).date().isoformat()
            if cfg.get("strict_standings") and today_jst >= cfg["start_date"] and len(standings) != cfg["expected_teams"]:
                raise RuntimeError(f"standings safety stop: expected {cfg['expected_teams']}, parsed {len(standings)}")
            regular_matches = parse_matches_wikitext(wikitext, region, source_url(cfg["regular"])) or parse_matches(html, region, source_url(cfg["regular"]))
            if "brkts-match-info-popup" in html and not regular_matches:
                raise RuntimeError("match parser safety stop: popup exists but parsed 0 matches")
            current["standings"] = standings
            result["fetch_log"].append({"region": region, "page": "regular", "status": "OK", "revid": revid, "standings": len(standings), "matches": len(regular_matches)})
        except Exception as exc:
            result["errors"].append({"region": region, "page": "regular", "error": str(exc)})
            result["fetch_log"].append({"region": region, "page": "regular", "status": "ERROR", "message": str(exc)})
            # Preserve previous matches if this page fails.
            regular_matches = [m for m in (current.get("finished_matches", []) + current.get("upcoming_matches", [])) if "/Regular_Season" in (m.get("source") or "")]

        # Overview page: playoff/bracket matches + bans; JP/KR rosters.
        try:
            html, wikitext, revid = client.parse(cfg["overview"])
            overview_matches = parse_matches_wikitext(wikitext, region, source_url(cfg["overview"])) or parse_matches(html, region, source_url(cfg["overview"]))
            if "brkts-match-info-popup" in html and not overview_matches:
                raise RuntimeError("overview match parser safety stop: popup exists but parsed 0 matches")
            bans = parse_bans(html, region, source_url(cfg["overview"]))
            current["hero_bans"] = bans
            roster_count = len(current.get("rosters") or [])
            roster_status = "not-requested"
            if cfg.get("rosters"):
                try:
                    rosters = parse_rosters(html, region, cfg.get("seed_teams") or [], source_url(cfg["overview"]))
                    if len(rosters) < 20:
                        raise RuntimeError(f"roster parser safety stop: parsed {len(rosters)} entries")
                    current["rosters"] = rosters
                    roster_count = len(rosters)
                    roster_status = "OK"
                except Exception as roster_exc:
                    roster_status = "ERROR"
                    result["errors"].append({"region": region, "page": "roster", "error": str(roster_exc)})
            result["fetch_log"].append({"region": region, "page": "overview", "status": "OK", "revid": revid, "matches": len(overview_matches), "bans": len(bans), "rosters": roster_count, "roster_status": roster_status})
        except Exception as exc:
            result["errors"].append({"region": region, "page": "overview", "error": str(exc)})
            result["fetch_log"].append({"region": region, "page": "overview", "status": "ERROR", "message": str(exc)})
            overview_matches = [m for m in (current.get("finished_matches", []) + current.get("upcoming_matches", [])) if "/Regular_Season" not in (m.get("source") or "")]

        merged = dedupe_matches(regular_matches + overview_matches)
        current["finished_matches"] = [m for m in merged if m.get("finished")]
        current["upcoming_matches"] = [m for m in merged if not m.get("finished") and m.get("date_jst")]
        result["regions"][region] = current

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT} with {len(result['errors'])} error(s).")
    # Do not fail the workflow for a single page error: old good data is preserved.
    return 0


def self_test() -> int:
    standings_html = '''<h2 id="Standings">Standings</h2><table>
      <tr><td>1.</td><td>ENTER FORCE.36</td><td>2–0</td><td>6–0</td><td>+6</td></tr>
      <tr><td>2.</td><td>VARREL</td><td>2–0</td><td>6–1</td><td>+5</td></tr></table><h2 id="Matches">Matches</h2>'''
    st = parse_standings(standings_html, 8)
    assert len(st) == 2 and st[1]["map_diff"] == 5

    tied_html = '''<h2 id="Standings">Standings</h2><div>ZETA DIVISION</div><div>0–0</div><div>0–0</div><div>0</div><div>Crazy Raccoon</div><div>0–0</div><div>0–0</div><div>0</div><h2>Next</h2>'''
    tied = parse_standings(tied_html, 9)
    assert len(tied) == 2 and tied[1]["rank"] == 2

    match_html = '''<div class="brkts-match-info-popup"><span data-timestamp="1790298000" data-finished="finished"></span><span class="name">VARREL</span><span class="match-info-header-scoreholder-score">3</span><span class="name">MURASH GAMING</span><span class="match-info-header-scoreholder-score">1</span></div>'''
    matches = parse_matches(match_html, "JP", "src")
    assert len(matches) == 1 and matches[0]["score_b"] == 1 and matches[0]["finished"]

    wiki_match = r'''{{Match
|date=2026-09-28 18:00 {{Abbr/JST}}
|opponent1={{TeamOpponent|VARREL|score=2}}
|opponent2={{TeamOpponent|MURASH GAMING|score=1}}
|finished=true
|map1={{Map|map=Busan|score1=2|score2=0|winner=1|t1ban=Winston|t2ban=Ana}}
|map2={{Map|map=King's Row|score1=1|score2=2|winner=2|t1ban=Tracer|t2ban=Kiriko}}
|map3={{Map|map=Suravasa|score1=3|score2=2|winner=1|t1ban=Sigma|t2ban=Lucio}}
}}'''
    wm = parse_matches_wikitext(wiki_match, "JP", "src")
    assert len(wm) == 1 and len(wm[0]["maps"]) == 3
    assert wm[0]["maps"][1]["picker"] == "MURASH GAMING"
    assert wm[0]["maps"][2]["picker"] == "VARREL"
    assert wm[0]["maps"][0]["ban_a"] == "Winston" and wm[0]["maps"][0]["received_a"] == "Ana"

    roster_html = '''<h2 id="Participants">Participants</h2>
      <div><b>VARREL</b><div><img alt="Tank"><span class="name">KSG</span></div><div><img alt="DPS"><span class="name">Nico</span></div><div><img alt="Support"><span class="name">Sley</span></div><div><span class="name">PAIN</span> Coach</div></div>
      <div><b>MURASH GAMING</b><div><img alt="Tank"><span class="name">PEPPI</span></div><div><img alt="DPS"><span class="name">Viper</span></div><div><img alt="Support"><span class="name">epic</span></div><div><span class="name">YaHo</span> Coach</div></div>'''
    roster = parse_rosters(roster_html, "JP", ["VARREL", "MURASH GAMING"], "src")
    assert len(roster) == 8 and any(x["player"] == "PAIN" and x["role"] == "Coach" for x in roster)

    ban_html = '''<h2 id="Ban_Statistics">Ban Statistics</h2><div>Overall</div><div>Jetpack Cat</div><div>24</div><div>Mauga</div><div>19</div><h2 id="Next">Next</h2>'''
    bans = parse_bans(ban_html, "JP", "src")
    assert len(bans) == 2 and bans[1]["bans"] == 19
    print("SELF_TEST_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test() if "--self-test" in sys.argv else run())
