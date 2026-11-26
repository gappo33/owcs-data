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
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

VERSION = "4.4.0"
API_BASE = "https://liquipedia.net/overwatch/api.php"
SOURCE_BASE = "https://liquipedia.net/overwatch/"
OUTPUT = Path("data/owcs.json")
DEBUG_DIR = Path("data/debug")
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
class RateLimitError(RuntimeError):
    message: str
    retry_after: int | None = None

    def __str__(self) -> str:
        if self.retry_after is not None:
            return f"{self.message} (retry_after={self.retry_after}s)"
        return self.message


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
        self.rate_limited = False
        self.retry_after: int | None = None
        self.query_calls = 0
        self.parse_calls = 0

    def _wait(self, is_parse: bool) -> None:
        now = time.monotonic()
        wait_general = GENERAL_INTERVAL_SECONDS - (now - self.last_request_at)
        wait_parse = PARSE_INTERVAL_SECONDS - (now - self.last_parse_at) if is_parse else 0
        wait_for = max(0.0, wait_general, wait_parse)
        if wait_for:
            time.sleep(wait_for)

    @staticmethod
    def _retry_after_seconds(response: requests.Response) -> int | None:
        raw = (response.headers.get("Retry-After") or "").strip()
        if not raw:
            return None
        try:
            return max(0, int(float(raw)))
        except ValueError:
            try:
                dt = parsedate_to_datetime(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return max(0, int((dt - datetime.now(timezone.utc)).total_seconds()))
            except Exception:
                return None

    def _request(self, params: dict[str, Any], *, is_parse: bool) -> requests.Response:
        if self.rate_limited:
            raise RateLimitError("Liquipedia request suppressed after earlier 429 in this run", self.retry_after)

        attempts = 3
        for attempt in range(attempts):
            self._wait(is_parse=is_parse)
            try:
                response = self.session.get(API_BASE, params=params, timeout=30)
            except requests.RequestException:
                if attempt + 1 >= attempts:
                    raise
                time.sleep(5 if attempt == 0 else 15)
                continue

            self.last_request_at = time.monotonic()
            if is_parse:
                self.last_parse_at = self.last_request_at
                self.parse_calls += 1
            else:
                self.query_calls += 1

            if response.status_code == 429:
                retry_after = self._retry_after_seconds(response)
                self.rate_limited = True
                self.retry_after = retry_after
                raise RateLimitError("Liquipedia returned HTTP 429 Too Many Requests", retry_after)

            if response.status_code in {500, 502, 503, 504} and attempt + 1 < attempts:
                delay = self._retry_after_seconds(response)
                if delay is None:
                    delay = 5 if attempt == 0 else 15
                time.sleep(min(delay, 120))
                continue

            response.raise_for_status()
            return response

        raise RuntimeError("unreachable request retry state")

    def query_revisions(self, pages: list[str]) -> dict[str, int | None]:
        if not pages:
            return {}
        params = {
            "action": "query",
            "prop": "revisions",
            "rvprop": "ids",
            "redirects": 1,
            "titles": "|".join(pages),
            "format": "json",
            "formatversion": 2,
        }
        response = self._request(params, is_parse=False)
        data = response.json()
        if "error" in data:
            raise RuntimeError(f"Liquipedia API revision-check error: {data['error']}")

        def norm(title: str) -> str:
            return re.sub(r"\s+", " ", str(title or "").replace("_", " ")).strip().lower()

        redirects = {norm(x.get("from")): norm(x.get("to")) for x in (data.get("query") or {}).get("redirects", [])}
        by_title: dict[str, int | None] = {}
        for item in (data.get("query") or {}).get("pages", []):
            key = norm(item.get("title"))
            if item.get("missing") is True:
                by_title[key] = None
            else:
                revs = item.get("revisions") or []
                by_title[key] = revs[0].get("revid") if revs else item.get("lastrevid")

        out: dict[str, int | None] = {}
        for page in pages:
            key = norm(page)
            key = redirects.get(key, key)
            out[page] = by_title.get(key)
        return out

    def parse(self, page: str) -> tuple[str, str, str | int | None]:
        params = {
            "action": "parse",
            "page": page,
            "prop": "text|wikitext|revid",
            "disableeditsection": 1,
            "format": "json",
            "formatversion": 2,
        }
        response = self._request(params, is_parse=True)
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



def extract_wikitext_h2_section(wikitext: str, heading: str) -> str:
    """Return the body of an exact level-2 MediaWiki section.

    Liquipedia's rendered HTML for TeamParticipants can be only a client-side
    placeholder in action=parse output, while the API wikitext still contains
    the full {{TeamParticipants}} / {{Opponent}} / {{Person}} structure. Roster
    parsing therefore prefers wikitext and uses rendered HTML only as fallback.
    """
    text = str(wikitext or "")
    m = re.search(rf"(?mi)^==\s*{re.escape(heading)}\s*==\s*$", text)
    if not m:
        return ""
    start = m.end()
    next_h2 = re.search(r"(?m)^==[^=\n].*?==\s*$", text[start:])
    end = start + next_h2.start() if next_h2 else len(text)
    return text[start:end]


def normalize_roster_role(raw_role: str, is_staff: bool) -> str:
    role = clean_wiki_value(raw_role).strip()
    key = re.sub(r"[ _-]+", " ", role.lower()).strip()
    if is_staff:
        staff_map = {
            "head coach": "Head Coach",
            "assistant coach": "Assistant Coach",
            "coach": "Coach",
            "analyst": "Analyst",
            "team manager": "Team Manager",
            "manager": "Manager",
            "general manager": "General Manager",
        }
        return staff_map.get(key, role.title() if role else "Staff")
    player_map = {
        "dps": "DPS",
        "damage": "DPS",
        "tank": "Tank",
        "sup": "Support",
        "support": "Support",
        "flex": "Flex",
    }
    return player_map.get(key, role)


def parse_rosters_wikitext(wikitext: str, region: str, team_hints: list[str], source: str) -> list[dict[str, Any]]:
    """Parse TeamParticipants directly from Liquipedia wikitext.

    The current Korea Stage 3 API HTML contains only the Participants heading,
    but the API wikitext contains the full TeamParticipants/Opponent/Person
    templates. This parser uses that source of truth and avoids card-boundary
    errors from client-rendered HTML.
    """
    section = extract_wikitext_h2_section(wikitext, "Participants")
    if not section:
        return []

    hint_by_key = {normalize_team_key(t): t for t in team_hints}
    rows: list[dict[str, Any]] = []

    for raw_opponent in extract_templates_by_name(section, "opponent"):
        opp = parse_template(raw_opponent)
        if not opp or str(opp.get("name", "")).lower() != "opponent":
            continue
        positional = opp.get("positional") or []
        params = opp.get("params") or {}
        team_raw = positional[0] if positional else params.get("team", "") or params.get("name", "")
        team = canonical_team(region, team_raw)
        exact = hint_by_key.get(normalize_team_key(team))
        if not exact:
            continue
        team = exact

        players_raw = params.get("players", "")
        if not players_raw:
            continue

        for raw_person in extract_templates_by_name(players_raw, "person"):
            person = parse_template(raw_person)
            if not person or str(person.get("name", "")).lower() != "person":
                continue
            ppos = person.get("positional") or []
            pparams = person.get("params") or {}
            player = clean_wiki_value(ppos[0] if ppos else pparams.get("name", "")).strip()
            if not player:
                continue
            is_staff = clean_wiki_value(pparams.get("type", "")).strip().lower() == "staff"
            role = normalize_roster_role(pparams.get("role", ""), is_staff)
            if not role:
                role = "Staff" if is_staff else "Flex"
            played = clean_wiki_value(pparams.get("played", "")).strip().lower()
            status = "DNP" if played in {"false", "0", "no"} else ""
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
        key = "|".join([row["region"], row["team"], row["role"], row["player"]])
        unique[key] = row
    return list(unique.values())

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


def parse_standings(html: str, expected_teams: int, team_hints: list[str] | None = None) -> list[dict[str, Any]]:
    """Parse standings defensively across Liquipedia layout variants."""
    soup = BeautifulSoup(html, "html.parser")
    section = find_section(soup, ["Standings"])
    scope = section or soup
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    hints = team_hints or []

    def record_from(value: str) -> str | None:
        value = normalize_dash(re.sub(r"\s+", "", value or ""))
        m = re.search(r"(?<!\d)(\d+)-(\d+)(?!\d)", value)
        return f"{m.group(1)}-{m.group(2)}" if m else None

    def add(rank: int, team: str, match_record: str, map_record: str, map_diff: str) -> bool:
        match_record = normalize_dash(re.sub(r"\s+", "", match_record))
        map_record = normalize_dash(re.sub(r"\s+", "", map_record))
        map_diff = normalize_dash(re.sub(r"\s+", "", map_diff))
        if not re.fullmatch(r"\d+-\d+", match_record):
            return False
        if not re.fullmatch(r"\d+-\d+", map_record):
            return False
        if not re.fullmatch(r"[+-]?\d+", map_diff):
            return False
        team = clean_wiki_value(team).strip()
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

    # Primary path: inspect table rows. get_text(" ") keeps a split 2–0 usable
    # even if Liquipedia renders each number in a separate span.
    for tr in scope.find_all("tr"):
        cells = tr.find_all(["th", "td"], recursive=False)
        if not cells:
            cells = tr.find_all(["th", "td"])
        vals = [c.get_text(" ", strip=True) for c in cells]
        if len(vals) < 4:
            continue

        records: list[tuple[int, str]] = []
        for ix, value in enumerate(vals):
            r = record_from(value)
            if r:
                records.append((ix, r))
        if len(records) < 2:
            continue

        first_ix, match_record = records[0]
        second_ix, map_record = records[1]

        rank = None
        for value in vals[:max(1, first_ix)]:
            m = re.fullmatch(r"\s*(\d+)\.?\s*", value)
            if m:
                rank = int(m.group(1))
                break
        if rank is None:
            rank = len(out) + 1

        pre = vals[:first_ix]
        team = ""
        for hint in hints:
            hk = normalize_team_key(hint)
            if hk and any(normalize_team_key(v) == hk for v in pre):
                team = hint
                break
        if not team:
            candidates: list[str] = []
            for value in pre:
                v = clean_wiki_value(value).strip()
                if not v or re.fullmatch(r"\d+\.?", v):
                    continue
                if re.fullmatch(r"Rank|Team|Current|Week \d+|Standings|W|L|Record", v, re.I):
                    continue
                candidates.append(v)
            if candidates:
                team = candidates[-1]

        diff = ""
        for value in vals[second_ix + 1:]:
            n = normalize_dash(re.sub(r"\s+", "", value))
            if re.fullmatch(r"[+-]\d+", n):
                diff = n
                break
        if not diff:
            try:
                a, b = map(int, map_record.split("-"))
                diff = f"{a-b:+d}"
            except Exception:
                continue

        add(rank, team, match_record, map_record, diff)
        if expected_teams and len(out) >= expected_teams:
            return out

    # Fallback for older/simple layouts.
    lines = clean_lines(scope.get_text("\n", strip=True))
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


def visual_tokens(html: str) -> list[tuple[str, str]]:
    """Return rendered-ish tokens in DOM order.

    Besides visible text and image alt/title values, preserve Liquipedia's
    ``data-highlightingclass`` attribute. TeamParticipants often renders some
    team names primarily through a team-template element/logo, so relying only
    on visible text can miss a team boundary and accidentally merge the next
    team's players into the previous team.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, str]] = []
    for el in soup.descendants:
        if isinstance(el, Tag):
            highlight = str(el.get("data-highlightingclass") or "").strip()
            if highlight:
                out.append(("attr", re.sub(r"\s+", " ", highlight)))
            if el.name == "img":
                value = str(el.get("alt") or el.get("title") or "").strip()
                if value:
                    out.append(("img", re.sub(r"\s+", " ", value)))
        elif isinstance(el, NavigableString):
            raw = str(el)
            for part in re.split(r"[\r\n]+", raw):
                value = re.sub(r"\s+", " ", part).strip()
                if value and value.lower() != "edit" and value != "[edit]":
                    out.append(("text", value))
    return out



def write_roster_debug(
    region: str,
    page: str,
    html: str,
    wikitext: str,
    team_hints: list[str],
    visual_rows: list[dict[str, Any]] | None = None,
    legacy_rows: list[dict[str, Any]] | None = None,
    why_visual: str = "",
    why_legacy: str = "",
) -> None:
    """Write a one-run diagnostic bundle for a roster parser failure.

    This is intentionally limited to KR because that is the page currently
    failing in production.  The workflow uploads ``data/debug`` as a temporary
    GitHub Actions artifact; the raw HTML/wikitext is NOT committed to the repo.
    """
    if region != "KR":
        return
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        soup = BeautifulSoup(html, "html.parser")
        section = find_section(soup, ["Participants"])
        participants_html = str(section) if section else ""
        token_source = participants_html or html
        toks = visual_tokens(token_source)

        team_occurrences: dict[str, list[dict[str, Any]]] = {}
        for team in team_hints:
            tkey = normalize_team_key(team)
            hits = []
            for i, (kind, value) in enumerate(toks):
                vkey = normalize_team_key(value)
                if vkey == tkey or (len(tkey) >= 5 and tkey and (vkey.startswith(tkey) or tkey in vkey)):
                    hits.append({
                        "index": i,
                        "kind": kind,
                        "value": value,
                        "context": toks[max(0, i - 8): i + 36],
                    })
            team_occurrences[team] = hits[:12]

        meta = {
            "generator_version": VERSION,
            "region": region,
            "page": page,
            "source": source_url(page),
            "html_length": len(html),
            "wikitext_length": len(wikitext),
            "participants_section_found": bool(section),
            "participants_html_length": len(participants_html),
            "visual_token_count": len(toks),
            "team_hints": team_hints,
            "team_occurrences": team_occurrences,
            "visual_parser": {
                "row_count": len(visual_rows or []),
                "reason": why_visual,
                "rows": visual_rows or [],
            },
            "legacy_parser": {
                "row_count": len(legacy_rows or []),
                "reason": why_legacy,
                "rows": legacy_rows or [],
            },
            "tokens": toks,
        }
        (DEBUG_DIR / "kr_roster_debug.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (DEBUG_DIR / "kr_participants.html").write_text(participants_html or html, encoding="utf-8")
        (DEBUG_DIR / "kr_overview_wikitext.txt").write_text(wikitext, encoding="utf-8")
        (DEBUG_DIR / "README.txt").write_text(
            "KR roster parser failed. Upload this artifact to ChatGPT.\n"
            "Most useful file: kr_roster_debug.json\n"
            "Also included: parsed Participants HTML and raw API wikitext.\n",
            encoding="utf-8",
        )
    except Exception as exc:
        # Diagnostics must never break the normal cache update.
        try:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            (DEBUG_DIR / "debug_writer_error.txt").write_text(str(exc), encoding="utf-8")
        except Exception:
            pass

def parse_standings_by_team_tokens(html: str, team_hints: list[str]) -> list[dict[str, Any]]:
    """Targeted standings parser using known team names and nearby rendered tokens."""
    toks = visual_tokens(html)
    vals = [v for _, v in toks]
    rows: list[dict[str, Any]] = []

    def rec(v: str) -> str | None:
        n = normalize_dash(re.sub(r"\s+", "", v or ""))
        m = re.fullmatch(r"(\d+)-(\d+)", n)
        return f"{m.group(1)}-{m.group(2)}" if m else None

    for team in team_hints:
        tkey = normalize_team_key(team)
        candidates = [i for i, v in enumerate(vals) if normalize_team_key(v) == tkey]
        found = None
        for i in candidates:
            # Standings occurrence should have match record + map record + diff
            # immediately after the team label (allow a few decorative tokens).
            nearby = vals[i + 1:i + 12]
            records = [(j, rec(v)) for j, v in enumerate(nearby) if rec(v)]
            if len(records) < 2:
                continue
            match_record = records[0][1]
            map_record = records[1][1]
            second_abs = i + 1 + records[1][0]
            diff = None
            for v in vals[second_abs + 1:second_abs + 6]:
                n = normalize_dash(re.sub(r"\s+", "", v))
                if re.fullmatch(r"[+-]?\d+", n):
                    diff = n
                    break
            if diff is None:
                a, b = map(int, map_record.split("-"))
                diff = f"{a-b:+d}"
            rank = None
            for v in reversed(vals[max(0, i - 5):i]):
                m = re.fullmatch(r"(\d+)\.?", v.strip())
                if m:
                    rank = int(m.group(1)); break
            if rank is None:
                rank = len(rows) + 1
            mw, ml = map(int, match_record.split("-"))
            gw, gl = map(int, map_record.split("-"))
            found = {
                "rank": rank, "team": team,
                "wins": mw, "losses": ml, "record": match_record,
                "map_wins": gw, "map_losses": gl, "map_record": map_record,
                "map_diff": int(diff),
            }
            break
        if found:
            rows.append(found)
    return sorted(rows, key=lambda r: (r["rank"], r["team"]))


def parse_rosters_by_visual_tokens(html: str, region: str, team_hints: list[str], source: str) -> list[dict[str, Any]]:
    """Parse TeamParticipants cards using *card headers*, not first team mentions.

    Liquipedia's Participants section can contain team names in free-form notes.
    The old parser used the earliest occurrence of a team name as its boundary;
    a note such as "Please Not Hero Ban ..." could therefore become a false
    boundary and merge/split adjacent rosters.  TeamParticipants has also changed
    layout over time: older cards expose a literal "Player roster" label, while
    the 2026 Korea cards expose "Main" / "Staff" tabs instead.  We therefore
    recognise either layout, but only when the header is followed by actual role
    tokens so ordinary prose cannot become a roster boundary.
    """
    soup = BeautifulSoup(html, "html.parser")
    section = find_section(soup, ["Participants"])
    if not section:
        return []

    toks = visual_tokens(str(section))
    vals = [v for _, v in toks]

    def token_matches_team(value: str, team: str) -> bool:
        vkey = normalize_team_key(value)
        tkey = normalize_team_key(team)
        if not vkey or not tkey:
            return False
        if vkey == tkey:
            return True
        # Logo labels can append "logo"/decorative suffixes.  Never use loose
        # matching for short IDs because it creates accidental matches.
        return len(tkey) >= 5 and (vkey.startswith(tkey) or tkey in vkey)

    def real_card_start(team: str) -> int | None:
        """Locate a genuine TeamParticipants card header for ``team``.

        Older TeamParticipants output has a ``Player roster`` label.  The current
        Korea Stage 3 layout instead renders ``Main`` / ``Staff`` tabs and no
        ``Player roster`` text at all.  A card is therefore accepted when:
          * the team identity is repeated, or appears in structural metadata; and
          * either roster-layout marker is nearby; and
          * at least one player-role token follows the marker.
        """
        candidates = [i for i, v in enumerate(vals) if token_matches_team(v, team)]
        for i in candidates:
            window = vals[i:min(len(vals), i + 48)]
            team_hits = sum(1 for v in window[:14] if token_matches_team(v, team))
            kind = toks[i][0]
            structural_single = team_hits >= 1 and kind in {"attr", "img"}

            roster_at = next((j for j, v in enumerate(window) if v == "Player roster"), None)
            main_at = next((j for j, v in enumerate(window) if v == "Main"), None)
            marker_at = roster_at if roster_at is not None else main_at
            if marker_at is None or marker_at > 26:
                continue

            # Require a real role icon/token shortly after the roster marker.
            # This is the important guard against team names mentioned in Notes.
            role_after = any(v in ROLE_NAMES for v in window[marker_at + 1:marker_at + 24])
            if not role_after:
                continue

            if team_hits >= 2 or structural_single:
                return i
        return None

    team_positions: list[tuple[int, str]] = []
    for team in team_hints:
        pos = real_card_start(team)
        if pos is not None:
            team_positions.append((pos, team))
    team_positions.sort()

    noise = {
        "Qualified", "Player roster", "Main", "Subs", "Staff", "Show rosters", "Compact view",
        "Enable hover", "Notes", "Notes (1)", "Stage 1", "Stage 2", "Stage 3",
        "Open Qualifier", "Regular Season", "Participants", "Player Info",
    }
    country_like = re.compile(
        r"^(Japan|South Korea|Korea|China|United States|Canada|Australia|Sweden|Finland|Denmark|Norway|France|Germany|United Kingdom|Saudi Arabia|Thailand|Taiwan|Hong Kong|Singapore|Vietnam|Philippines|Malaysia|Indonesia|Brazil|Mexico)$",
        re.I,
    )

    def good_player_text(kind: str, value: str) -> bool:
        v = value.strip()
        if not v or v in noise or v in ROLE_NAMES or v in STAFF_NAMES:
            return False
        if country_like.fullmatch(v):
            return False
        if any(token_matches_team(v, team) for team in team_hints):
            return False
        if re.fullmatch(r"\d+(st|nd|rd|th)?(?:\s*-\s*\d+(st|nd|rd|th)?)?", v, re.I):
            return False
        if len(v) > 50:
            return False
        return kind == "text"

    rows: list[dict[str, Any]] = []
    for idx, (start, team) in enumerate(team_positions):
        end = team_positions[idx + 1][0] if idx + 1 < len(team_positions) else len(toks)
        block = toks[start:end]
        for stop_i, (_, v) in enumerate(block):
            if v in {"Results", "Broadcast", "Additional Information"}:
                block = block[:stop_i]
                break

        # Player entries: role icon -> country/decorations -> player ID.
        for i, (_, value) in enumerate(block):
            role = value if value in ROLE_NAMES else None
            if not role:
                continue
            player = None
            status = ""
            for k2, v2 in block[i + 1:i + 10]:
                if v2 == "DNP":
                    status = "DNP"
                    continue
                if v2 in ROLE_NAMES or v2 in STAFF_NAMES:
                    break
                if any(token_matches_team(v2, other) for other in team_hints if other != team):
                    break
                if good_player_text(k2, v2):
                    player = v2
                    break
            if player:
                rows.append({"region": region, "team": team, "role": role, "player": player, "status": status, "source": source})

        # Staff entries: TeamParticipants renders the staff role after the name.
        for i, (_, value) in enumerate(block):
            if value not in STAFF_NAMES:
                continue
            player = None
            for k2, v2 in reversed(block[max(0, i - 9):i]):
                if v2 in ROLE_NAMES or v2 in STAFF_NAMES:
                    break
                if any(token_matches_team(v2, other) for other in team_hints if other != team):
                    break
                if good_player_text(k2, v2):
                    player = v2
                    break
            if player:
                rows.append({"region": region, "team": team, "role": value, "player": player, "status": "", "source": source})

    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        unique["|".join([row["region"], row["team"], row["role"], row["player"]])] = row
    return list(unique.values())


# Event-roster fallback for 2026 Japan Stage 3.  This is deliberately the
# tournament Participants roster, NOT each organisation's current Active roster:
# team pages can contain players who are not registered for this Stage.
JP_STAGE3_EVENT_ROSTER: dict[str, list[tuple[str, str, str]]] = {
    "VARREL": [
        ("DPS", "Nico", ""), ("DPS", "Qki", ""), ("DPS", "TOPDRAGON", ""),
        ("Tank", "KSG", ""), ("Support", "Qloud", ""), ("Support", "Sley", ""),
        ("Coach", "Pain", ""), ("Coach", "Dae1", ""),
    ],
    "MURASH GAMING": [
        ("DPS", "Viper", ""), ("DPS", "ky0n", ""), ("Tank", "PEPPI", ""),
        ("Support", "epic", ""), ("Support", "orca", ""), ("Coach", "YaHo", ""),
    ],
    "ENTER FORCE.36": [
        ("DPS", "Edison", ""), ("DPS", "NewJ", ""), ("Tank", "Fearless", ""),
        ("Support", "Ydot", ""), ("Support", "Gaisen", ""), ("Coach", "Tydolla", ""),
    ],
    "Uwinks": [
        ("DPS", "Yot1y", ""), ("DPS", "Develop", ""), ("DPS", "Undersea", ""),
        ("Tank", "xzahyo", ""), ("Tank", "yumilalan", ""),
        ("Support", "EuclidEUC", ""), ("Support", "UGH", ""), ("Support", "APDO", ""),
        ("Coach", "Opera", ""),
    ],
    "99DIVINE": [
        ("DPS", "MN3", ""), ("DPS", "ALTHOUGH", ""), ("Tank", "Ichi", ""),
        ("Support", "Sakume", ""), ("Support", "Umi", ""), ("Support", "Supreme", ""),
        ("Coach", "Dreamer", ""),
    ],
    "Please Not Hero Ban": [
        ("DPS", "RLG5656", ""), ("DPS", "Suraimu1", ""), ("Tank", "UYOU", ""),
        ("Support", "Neivis", ""), ("Support", "깡돌이", ""), ("Support", "Langley", ""),
    ],
    "Lazuli": [
        ("DPS", "키드", ""), ("DPS", "dra", ""), ("DPS", "zenith", ""),
        ("Tank", "FARMER", ""), ("Support", "sans", ""), ("Support", "Amateru", ""),
        ("Coach", "Ares", ""),
    ],
    "REVATI": [
        ("DPS", "BreadTurtle", ""), ("DPS", "Anarchy", ""), ("Tank", "Vosa1q", ""),
        ("Support", "NHZ", ""), ("Support", "Elysia", ""), ("Support", "Azue1recker", "DNP"),
        ("Coach", "Menhera", ""),
    ],
}


def jp_stage3_fallback_rosters(source: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for team, members in JP_STAGE3_EVENT_ROSTER.items():
        for role, player, status in members:
            rows.append({
                "region": "JP", "team": team, "role": role, "player": player,
                "status": status, "source": source,
            })
    return rows

def validate_rosters(rows: list[dict[str, Any]], team_hints: list[str]) -> tuple[bool, str]:
    """Reject plausible-looking but cross-contaminated roster parses."""
    if not rows:
        return False, "parsed 0 entries"
    player_roles = ROLE_NAMES
    counts: dict[str, int] = {team: 0 for team in team_hints}
    for row in rows:
        if row.get("team") in counts and row.get("role") in player_roles:
            counts[row["team"]] += 1
    missing = [team for team, n in counts.items() if n < 4]
    oversized = [team for team, n in counts.items() if n > 10]
    if missing:
        return False, "missing/too-small team rosters: " + ", ".join(f"{t}={counts[t]}" for t in missing)
    if oversized:
        return False, "oversized team rosters: " + ", ".join(f"{t}={counts[t]}" for t in oversized)
    return True, "OK"


def extract_valid_rosters(
    *,
    html: str,
    wikitext: str,
    region: str,
    page: str,
    cfg: dict[str, Any],
) -> tuple[list[dict[str, Any]], str]:
    """Parse and validate one event roster using the safest available source."""
    hints = cfg.get("seed_teams") or []
    source = source_url(page)

    wiki_rosters = parse_rosters_wikitext(wikitext, region, hints, source)
    ok_wiki, why_wiki = validate_rosters(wiki_rosters, hints)
    if ok_wiki:
        return wiki_rosters, "OK_WIKITEXT"

    visual_rosters = parse_rosters_by_visual_tokens(html, region, hints, source)
    ok_visual, why_visual = validate_rosters(visual_rosters, hints)
    if ok_visual:
        return visual_rosters, "OK_VISUAL"

    if region == "JP" and page == "Overwatch_Champions_Series/2026/Asia/Stage_3/Japan":
        fallback = jp_stage3_fallback_rosters(source)
        ok_fallback, why_fallback = validate_rosters(fallback, hints)
        if ok_fallback:
            return fallback, "FALLBACK_JP_STAGE3"
        raise RuntimeError("JP Stage 3 fallback invalid: " + why_fallback)

    legacy_rosters = parse_rosters(html, region, hints, source)
    ok_legacy, why_legacy = validate_rosters(legacy_rosters, hints)
    if ok_legacy:
        return legacy_rosters, "OK_LEGACY"

    write_roster_debug(
        region=region,
        page=page,
        html=html,
        wikitext=wikitext,
        team_hints=hints,
        visual_rows=visual_rosters,
        legacy_rows=legacy_rosters,
        why_visual=why_visual,
        why_legacy=why_legacy,
    )
    raise RuntimeError(
        "roster parser safety stop: "
        + "wikitext=" + why_wiki
        + "; visual=" + why_visual
        + "; legacy=" + why_legacy
    )

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
        names: list[str] = []
        opponent_nodes = popup.select(".match-info-header-opponent")
        for node in opponent_nodes[:2]:
            name_node = node.select_one("span.name") or node.select_one(".team-template-text")
            value = name_node.get_text(" ", strip=True) if name_node else node.get_text(" ", strip=True)
            value = clean_wiki_value(value)
            if value:
                names.append(value)
        if len(names) < 2:
            names = []
            for node in popup.select("span.name"):
                value = clean_wiki_value(node.get_text(" ", strip=True))
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


def selected_regions() -> list[str]:
    raw = os.environ.get("OWCS_REGIONS", "JP,KR").strip()
    if raw.upper() == "ALL":
        return list(REGIONS.keys())
    out: list[str] = []
    for part in raw.split(","):
        key = part.strip().upper()
        if key in REGIONS and key not in out:
            out.append(key)
    return out or ["JP", "KR"]


def parse_iso_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def cache_entry_is_fresh(entry: dict[str, Any] | None, current_revid: Any, now_utc: datetime, max_age_hours: float) -> bool:
    if not entry or current_revid is None:
        return False
    if str(entry.get("revid")) != str(current_revid):
        return False
    if entry.get("generator_version") != VERSION:
        return False
    if entry.get("status") != "OK":
        return False
    fetched = parse_iso_dt(entry.get("fetched_at"))
    if not fetched:
        return False
    return (now_utc - fetched).total_seconds() <= max_age_hours * 3600


def cache_entry_retry_blocked(entry: dict[str, Any] | None, now_utc: datetime) -> bool:
    if not entry or entry.get("generator_version") != VERSION:
        return False
    if entry.get("status") != "PARTIAL_ERROR":
        return False
    retry_at = parse_iso_dt(entry.get("retry_after"))
    return bool(retry_at and retry_at > now_utc)


def note_page_success(page_cache: dict[str, Any], page: str, revid: Any, now_utc: datetime) -> None:
    page_cache[page] = {
        "revid": revid,
        "fetched_at": now_utc.isoformat(timespec="seconds"),
        "generator_version": VERSION,
        "status": "OK",
    }


def note_page_partial(page_cache: dict[str, Any], page: str, revid: Any, now_utc: datetime, detail: str, retry_minutes: int = 60) -> None:
    page_cache[page] = {
        "revid": revid,
        "fetched_at": now_utc.isoformat(timespec="seconds"),
        "generator_version": VERSION,
        "status": "PARTIAL_ERROR",
        "detail": detail,
        "retry_after": (now_utc + timedelta(minutes=max(5, retry_minutes))).isoformat(timespec="seconds"),
    }


def rate_limit_until(now_utc: datetime, retry_after: int | None) -> datetime:
    """Return the next allowed fetch time after a 429.

    Liquipedia may omit Retry-After, and GitHub-hosted runners use shared IP
    ranges.  A short one-hour retry loop can therefore keep re-hitting the same
    temporary block.  Default to a conservative six-hour cooldown, configurable
    with OWCS_RATE_LIMIT_COOLDOWN_HOURS.
    """
    try:
        fallback_hours = max(1.0, float(os.environ.get("OWCS_RATE_LIMIT_COOLDOWN_HOURS", "6")))
    except ValueError:
        fallback_hours = 6.0
    fallback_seconds = int(fallback_hours * 3600)
    seconds = retry_after if retry_after is not None else fallback_seconds
    return now_utc + timedelta(seconds=max(60, seconds))


def run() -> int:
    contact = os.environ.get("LIQUIPEDIA_CONTACT", "").strip()
    if not contact:
        print("ERROR: LIQUIPEDIA_CONTACT is required. Set it as a GitHub Actions secret.", file=sys.stderr)
        return 2

    active_regions = selected_regions()
    force_parse = os.environ.get("OWCS_FORCE_PARSE", "0").strip().lower() in {"1", "true", "yes"}
    ignore_cooldown = os.environ.get("OWCS_IGNORE_COOLDOWN", "0").strip().lower() in {"1", "true", "yes"}
    try:
        cache_hours = max(0.0, float(os.environ.get("OWCS_CACHE_MAX_AGE_HOURS", "6")))
    except ValueError:
        cache_hours = 6.0

    now_utc = datetime.now(timezone.utc)
    now_jst = datetime.now(JST)
    old = load_old()
    old_regions = old.get("regions") or {}
    old_page_cache = copy.deepcopy(old.get("page_cache") or {})

    result: dict[str, Any] = {
        "schema_version": 3,
        "generator_version": VERSION,
        "generated_at": now_utc.isoformat(timespec="seconds"),
        "generated_at_jst": now_jst.isoformat(timespec="seconds"),
        "data_source": "Liquipedia Overwatch Wiki / MediaWiki API",
        "api_terms": "https://liquipedia.net/api-terms-of-use",
        "active_regions": active_regions,
        "regions": {},
        "page_cache": old_page_cache,
        "fetch_log": [],
        "errors": [],
        "run_stats": {
            "query_calls": 0,
            "parse_calls": 0,
            "cache_hits": 0,
            "active_regions": active_regions,
            "force_parse": force_parse,
            "cache_max_age_hours": cache_hours,
        },
    }

    # Always preserve non-priority regions in the JSON, but do not touch Liquipedia
    # for them unless OWCS_REGIONS explicitly includes them.
    for region, cfg in REGIONS.items():
        result["regions"][region] = copy.deepcopy(old_regions.get(region) or region_template(region, cfg))

    old_cooldown = parse_iso_dt(old.get("rate_limit_until"))
    if old_cooldown and old_cooldown > now_utc and not ignore_cooldown:
        result["rate_limit_until"] = old_cooldown.isoformat(timespec="seconds")
        result["errors"].append({
            "region": "GLOBAL",
            "page": "rate-limit-cooldown",
            "error": f"network fetch skipped until {old_cooldown.astimezone(JST).isoformat(timespec='seconds')}",
        })
        result["fetch_log"].append({
            "region": "GLOBAL", "page": "all", "status": "COOLDOWN_SKIP",
            "message": "previous 429 cooldown still active; preserved cached data",
        })
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {OUTPUT}; skipped network due to stored 429 cooldown.")
        return 0

    client = ApiClient(contact)

    # ------------------------------------------------------------------
    # Priority roster repair
    # ------------------------------------------------------------------
    # If a required JP/KR roster is empty/corrupt, do NOT spend one request on
    # the global revision preflight first.  Fetch only that region's overview
    # page, repair the roster, write the cache, and end the run.  The next
    # scheduled run can resume the normal revision-check workflow.
    repair_regions: list[str] = []
    for region in active_regions:
        cfg = REGIONS[region]
        if not cfg.get("rosters"):
            continue
        current = result["regions"].get(region) or region_template(region, cfg)
        ok_roster, why_roster = validate_rosters(current.get("rosters") or [], cfg.get("seed_teams") or [])
        if not ok_roster:
            repair_regions.append(region)
            result["fetch_log"].append({
                "region": region,
                "page": "roster",
                "status": "REPAIR_REQUIRED",
                "message": why_roster,
            })

    if repair_regions:
        for region in repair_regions:
            cfg = REGIONS[region]
            overview_page = cfg["overview"]
            current = copy.deepcopy(result["regions"].get(region) or region_template(region, cfg))
            try:
                html, wikitext, revid = client.parse(overview_page)
                rosters, roster_status = extract_valid_rosters(
                    html=html,
                    wikitext=wikitext,
                    region=region,
                    page=overview_page,
                    cfg=cfg,
                )
                current["rosters"] = rosters
                result["regions"][region] = current
                result["page_cache"][overview_page] = {
                    "revid": revid,
                    "fetched_at": now_utc.isoformat(timespec="seconds"),
                    "generator_version": VERSION,
                    "status": "ROSTER_REPAIR_OK",
                    "detail": roster_status,
                }
                result["fetch_log"].append({
                    "region": region,
                    "page": "roster",
                    "status": "REPAIR_OK",
                    "revid": revid,
                    "rosters": len(rosters),
                    "roster_status": roster_status,
                })
            except RateLimitError as exc:
                until = rate_limit_until(now_utc, exc.retry_after)
                result["rate_limit_until"] = until.isoformat(timespec="seconds")
                result["errors"].append({
                    "region": region,
                    "page": "roster-repair",
                    "error": str(exc),
                })
                result["fetch_log"].append({
                    "region": region,
                    "page": "roster-repair",
                    "status": "RATE_LIMITED",
                    "message": str(exc),
                    "retry_after": exc.retry_after,
                })
                break
            except Exception as exc:
                result["errors"].append({
                    "region": region,
                    "page": "roster-repair",
                    "error": str(exc),
                })
                result["fetch_log"].append({
                    "region": region,
                    "page": "roster-repair",
                    "status": "ERROR",
                    "message": str(exc),
                })

        result["run_stats"]["query_calls"] = client.query_calls
        result["run_stats"]["parse_calls"] = client.parse_calls
        result["run_stats"]["repair_regions"] = repair_regions
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        print(
            f"Wrote {OUTPUT}; priority roster-repair run completed. "
            f"query_calls={client.query_calls}, parse_calls={client.parse_calls}."
        )
        return 0
    priority_pages: list[str] = []
    for region in active_regions:
        cfg = REGIONS[region]
        priority_pages.extend([cfg["regular"], cfg["overview"]])

    current_revisions: dict[str, int | None] = {}
    try:
        current_revisions = client.query_revisions(priority_pages)
        result.pop("rate_limit_until", None)
        result["fetch_log"].append({
            "region": "GLOBAL", "page": "revision-check", "status": "OK",
            "pages": len(priority_pages),
        })
    except RateLimitError as exc:
        until = rate_limit_until(now_utc, exc.retry_after)
        result["rate_limit_until"] = until.isoformat(timespec="seconds")
        result["errors"].append({"region": "GLOBAL", "page": "revision-check", "error": str(exc)})
        result["fetch_log"].append({
            "region": "GLOBAL", "page": "revision-check", "status": "RATE_LIMITED",
            "message": str(exc), "retry_after": exc.retry_after,
        })
        result["run_stats"]["query_calls"] = client.query_calls
        result["run_stats"]["parse_calls"] = client.parse_calls
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {OUTPUT}; one 429 encountered, no further Liquipedia calls made.")
        return 0
    except Exception as exc:
        # Revision preflight is an optimization only. A transient non-429 failure
        # should not prevent the normal parse path from trying once.
        result["fetch_log"].append({
            "region": "GLOBAL", "page": "revision-check", "status": "WARN",
            "message": str(exc),
        })

    rate_limited = False

    for region in active_regions:
        cfg = REGIONS[region]
        current = copy.deepcopy(old_regions.get(region) or region_template(region, cfg))
        current.setdefault("region", region)
        current.setdefault("label", cfg["label"])
        current.setdefault("sources", {"overview": source_url(cfg["overview"]), "regular": source_url(cfg["regular"])})
        regular_matches: list[dict[str, Any]] = []
        overview_matches: list[dict[str, Any]] = []

        old_all_matches = current.get("finished_matches", []) + current.get("upcoming_matches", [])
        old_regular_matches = [m for m in old_all_matches if "/Regular_Season" in (m.get("source") or "")]
        old_overview_matches = [m for m in old_all_matches if "/Regular_Season" not in (m.get("source") or "")]
        today_jst = now_jst.date().isoformat()
        regular_page = cfg["regular"]
        overview_page = cfg["overview"]

        # Regular page -----------------------------------------------------
        regular_rev = current_revisions.get(regular_page)
        if regular_page in current_revisions and regular_rev is None and today_jst < cfg["start_date"]:
            regular_matches = old_regular_matches
            result["fetch_log"].append({
                "region": region, "page": "regular", "status": "SKIP",
                "message": "regular-season page not published yet",
            })
        elif (not force_parse and cache_entry_retry_blocked(result["page_cache"].get(regular_page), now_utc)):
            regular_matches = old_regular_matches
            result["run_stats"]["cache_hits"] += 1
            result["fetch_log"].append({
                "region": region, "page": "regular", "status": "PARTIAL_RETRY_WAIT",
                "revid": regular_rev,
                "message": result["page_cache"].get(regular_page, {}).get("detail", "previous partial parse error"),
            })
        elif (not force_parse and cache_entry_is_fresh(result["page_cache"].get(regular_page), regular_rev, now_utc, cache_hours)):
            regular_matches = old_regular_matches
            result["run_stats"]["cache_hits"] += 1
            result["fetch_log"].append({
                "region": region, "page": "regular", "status": "CACHE_FRESH",
                "revid": regular_rev,
            })
        else:
            try:
                html, wikitext, revid = client.parse(regular_page)

                standings = parse_standings(html, cfg["expected_teams"], cfg.get("seed_teams") or [])
                if len(standings) != cfg["expected_teams"] and cfg.get("seed_teams"):
                    token_standings = parse_standings_by_team_tokens(html, cfg.get("seed_teams") or [])
                    if len(token_standings) > len(standings):
                        standings = token_standings
                standings_status = "OK"
                if cfg.get("strict_standings") and today_jst >= cfg["start_date"] and len(standings) != cfg["expected_teams"]:
                    standings_status = "ERROR"
                    result["errors"].append({
                        "region": region, "page": "regular-standings",
                        "error": f"standings safety stop: expected {cfg['expected_teams']}, parsed {len(standings)}",
                    })
                elif standings:
                    current["standings"] = standings

                parsed_regular = parse_matches_wikitext(wikitext, region, source_url(regular_page))
                if not parsed_regular:
                    parsed_regular = parse_matches(html, region, source_url(regular_page))
                if "brkts-match-info-popup" in html and not parsed_regular and today_jst >= cfg["start_date"]:
                    result["errors"].append({
                        "region": region, "page": "regular-matches",
                        "error": "match parser safety stop: popup exists but parsed 0 matches",
                    })
                    regular_matches = old_regular_matches
                    match_status = "ERROR"
                else:
                    regular_matches = parsed_regular
                    match_status = "OK"

                regular_ok = standings_status == "OK" and match_status == "OK"
                if regular_ok:
                    note_page_success(result["page_cache"], regular_page, revid, now_utc)
                else:
                    detail = "regular parse partial: standings=" + standings_status + ", matches=" + match_status
                    note_page_partial(result["page_cache"], regular_page, revid, now_utc, detail)

                result["fetch_log"].append({
                    "region": region, "page": "regular", "status": "OK" if regular_ok else "PARTIAL", "revid": revid,
                    "standings": len(standings), "standings_status": standings_status,
                    "matches": len(regular_matches), "match_status": match_status,
                })
            except RateLimitError as exc:
                until = rate_limit_until(now_utc, exc.retry_after)
                result["rate_limit_until"] = until.isoformat(timespec="seconds")
                result["errors"].append({"region": region, "page": "regular", "error": str(exc)})
                result["fetch_log"].append({
                    "region": region, "page": "regular", "status": "RATE_LIMITED",
                    "message": str(exc), "retry_after": exc.retry_after,
                })
                regular_matches = old_regular_matches
                rate_limited = True
            except Exception as exc:
                msg = str(exc)
                if "missingtitle" in msg and today_jst < cfg["start_date"]:
                    result["fetch_log"].append({
                        "region": region, "page": "regular", "status": "SKIP",
                        "message": "regular-season page not published yet",
                    })
                else:
                    result["errors"].append({"region": region, "page": "regular", "error": msg})
                    result["fetch_log"].append({"region": region, "page": "regular", "status": "ERROR", "message": msg})
                regular_matches = old_regular_matches

        if rate_limited:
            merged = dedupe_matches(regular_matches + old_overview_matches)
            current["finished_matches"] = [m for m in merged if m.get("finished")]
            current["upcoming_matches"] = [m for m in merged if not m.get("finished") and m.get("date_jst")]
            result["regions"][region] = current
            break

        # Overview page ----------------------------------------------------
        overview_rev = current_revisions.get(overview_page)
        overview_cache_entry = result["page_cache"].get(overview_page)
        overview_cache_fresh = (not force_parse and cache_entry_is_fresh(overview_cache_entry, overview_rev, now_utc, cache_hours))

        # Page revision alone is not enough if a required subcomponent is
        # missing/corrupt.  In v4.0 an overview page could be marked fresh
        # before roster validation, leaving an empty KR roster stuck behind
        # a valid cache entry.  Force a repair fetch in that case.
        if overview_cache_fresh and cfg.get("rosters"):
            hints = cfg.get("seed_teams") or []
            ok_cached_roster, why_cached_roster = validate_rosters(current.get("rosters") or [], hints)
            if not ok_cached_roster:
                overview_cache_fresh = False
                result["fetch_log"].append({
                    "region": region, "page": "overview", "status": "CACHE_BYPASS_INVALID_ROSTER",
                    "revid": overview_rev, "message": why_cached_roster,
                })

        if (not force_parse and cache_entry_retry_blocked(overview_cache_entry, now_utc)):
            overview_matches = old_overview_matches
            result["run_stats"]["cache_hits"] += 1
            result["fetch_log"].append({
                "region": region, "page": "overview", "status": "PARTIAL_RETRY_WAIT",
                "revid": overview_rev,
                "message": (overview_cache_entry or {}).get("detail", "previous partial parse error"),
            })
        elif overview_cache_fresh:
            overview_matches = old_overview_matches
            result["run_stats"]["cache_hits"] += 1
            result["fetch_log"].append({
                "region": region, "page": "overview", "status": "CACHE_FRESH",
                "revid": overview_rev,
            })
        else:
            try:
                html, wikitext, revid = client.parse(overview_page)

                parsed_overview = parse_matches_wikitext(wikitext, region, source_url(overview_page))
                if not parsed_overview:
                    parsed_overview = parse_matches(html, region, source_url(overview_page))
                if "brkts-match-info-popup" in html and not parsed_overview and old_overview_matches:
                    result["errors"].append({
                        "region": region, "page": "overview-matches",
                        "error": "overview match parser returned 0; preserved previous non-empty match data",
                    })
                    overview_matches = old_overview_matches
                    overview_match_status = "ERROR"
                else:
                    overview_matches = parsed_overview
                    overview_match_status = "OK"

                bans = parse_bans(html, region, source_url(overview_page))
                current["hero_bans"] = bans

                roster_count = len(current.get("rosters") or [])
                roster_status = "not-requested"
                if cfg.get("rosters"):
                    hints = cfg.get("seed_teams") or []
                    try:
                        rosters, roster_status = extract_valid_rosters(
                            html=html,
                            wikitext=wikitext,
                            region=region,
                            page=overview_page,
                            cfg=cfg,
                        )
                        current["rosters"] = rosters
                        roster_count = len(rosters)
                    except Exception as roster_exc:
                        roster_status = "ERROR"
                        cached_rosters = current.get("rosters") or []
                        ok_cached, why_cached = validate_rosters(cached_rosters, hints)
                        if not ok_cached:
                            current["rosters"] = []
                            roster_count = 0
                            cache_note = f"; cleared invalid cached roster ({why_cached})"
                        else:
                            roster_count = len(cached_rosters)
                            cache_note = "; preserved validated cached roster"
                        result["errors"].append({
                            "region": region, "page": "roster",
                            "error": str(roster_exc) + cache_note,
                        })

                overview_ok = overview_match_status == "OK" and roster_status != "ERROR"
                if overview_ok:
                    note_page_success(result["page_cache"], overview_page, revid, now_utc)
                else:
                    detail = "overview parse partial: matches=" + overview_match_status + ", roster=" + roster_status
                    note_page_partial(result["page_cache"], overview_page, revid, now_utc, detail)

                result["fetch_log"].append({
                    "region": region, "page": "overview", "status": "OK" if overview_ok else "PARTIAL", "revid": revid,
                    "matches": len(overview_matches), "match_status": overview_match_status,
                    "bans": len(bans), "rosters": roster_count, "roster_status": roster_status,
                })
            except RateLimitError as exc:
                until = rate_limit_until(now_utc, exc.retry_after)
                result["rate_limit_until"] = until.isoformat(timespec="seconds")
                result["errors"].append({"region": region, "page": "overview", "error": str(exc)})
                result["fetch_log"].append({
                    "region": region, "page": "overview", "status": "RATE_LIMITED",
                    "message": str(exc), "retry_after": exc.retry_after,
                })
                overview_matches = old_overview_matches
                rate_limited = True
            except Exception as exc:
                result["errors"].append({"region": region, "page": "overview", "error": str(exc)})
                result["fetch_log"].append({"region": region, "page": "overview", "status": "ERROR", "message": str(exc)})
                overview_matches = old_overview_matches

        merged = dedupe_matches(regular_matches + overview_matches)
        current["finished_matches"] = [m for m in merged if m.get("finished")]
        current["upcoming_matches"] = [m for m in merged if not m.get("finished") and m.get("date_jst")]
        result["regions"][region] = current

        if rate_limited:
            break

    result["run_stats"]["query_calls"] = client.query_calls
    result["run_stats"]["parse_calls"] = client.parse_calls
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(
        f"Wrote {OUTPUT} with {len(result['errors'])} error(s); "
        f"query_calls={client.query_calls}, parse_calls={client.parse_calls}, cache_hits={result['run_stats']['cache_hits']}."
    )
    return 0

def self_test() -> int:
    standings_html = '''<h2 id="Standings">Standings</h2><table>
      <tr><td>1.</td><td>ENTER FORCE.36</td><td>2–0</td><td>6–0</td><td>+6</td></tr>
      <tr><td>2.</td><td>VARREL</td><td>2–0</td><td>6–1</td><td>+5</td></tr></table><h2 id="Matches">Matches</h2>'''
    st = parse_standings(standings_html, 8)
    assert len(st) == 2 and st[1]["map_diff"] == 5

    rendered_standings = """<div>Current<br>Standings<br>1.<br>ENTER FORCE.36<br>2–0<br>6–0<br>+6<br>2.<br>VARREL<br>2–0<br>6–1<br>+5</div>"""
    st2 = parse_standings_by_team_tokens(rendered_standings, ["ENTER FORCE.36", "VARREL"])
    assert len(st2) == 2 and st2[0]["team"] == "ENTER FORCE.36" and st2[1]["map_diff"] == 5

    roster_html = """<h2><span id="Participants">Participants</span></h2><div>
      <span>VARREL</span><span>VARREL</span><span>Qualified</span><span>Player roster</span><span>Main</span><span>Staff</span>
      <img alt='DPS'><img alt='Japan'><span>Nico</span>
      <img alt='Tank'><img alt='Japan'><span>KSG</span>
      <img alt='Support'><img alt='South Korea'><span>Sley</span>
      <img alt='South Korea'><span>Pain</span><span>Coach</span>
      <span>MURASH GAMING</span><span>MURASH GAMING</span><span>Qualified</span><span>Player roster</span><span>Main</span><span>Staff</span>
      <img alt='DPS'><img alt='South Korea'><span>Viper</span>
      <img alt='Tank'><img alt='South Korea'><span>PEPPI</span>
      <img alt='Support'><img alt='Japan'><span>epic</span>
      <img alt='South Korea'><span>YaHo</span><span>Coach</span>
    </div>"""
    rr = parse_rosters_by_visual_tokens(roster_html, "JP", ["VARREL", "MURASH GAMING"], "test")
    assert any(x["team"] == "VARREL" and x["player"] == "Nico" and x["role"] == "DPS" for x in rr)
    assert any(x["team"] == "MURASH GAMING" and x["player"] == "YaHo" and x["role"] == "Coach" for x in rr)

    # Regression: team labels can be present only in TeamTemplate attributes/logos.
    # The parser must stay inside Participants and must not merge E36/MRG into VARREL.
    roster_boundary_html = """
      <div><span data-highlightingclass='VARREL'>logo</span><img alt='DPS'><span>Nico</span><img alt='Tank'><span>KSG</span><img alt='Support'><span>Qloud</span><img alt='Support'><span>Sley</span></div>
      <h2><span id='Participants'>Participants</span></h2>
      <div><span data-highlightingclass='VARREL'><img alt='VARREL logo'></span><span>Player roster</span><img alt='DPS'><span>Nico</span><img alt='DPS'><span>Qki</span><img alt='Tank'><span>KSG</span><img alt='Support'><span>Qloud</span><img alt='Support'><span>Sley</span></div>
      <div><span data-highlightingclass='MURASH GAMING'><img alt='MURASH GAMING logo'></span><span>Player roster</span><img alt='DPS'><span>Viper</span><img alt='DPS'><span>ky0n</span><img alt='Tank'><span>PEPPI</span><img alt='Support'><span>epic</span><img alt='Support'><span>orca</span></div>
      <div><span data-highlightingclass='ENTER FORCE.36'><img alt='ENTER FORCE.36 logo'></span><span>Player roster</span><img alt='DPS'><span>Edison</span><img alt='DPS'><span>NewJ</span><img alt='Tank'><span>Fearless</span><img alt='Support'><span>Ydot</span><img alt='Support'><span>Gaisen</span></div>
      <div><span data-highlightingclass='99DIVINE'><img alt='99DIVINE logo'></span><span>Player roster</span><img alt='DPS'><span>MN3</span><img alt='DPS'><span>ALTHOUGH</span><img alt='Tank'><span>Ichi</span><img alt='Support'><span>Sakume</span><img alt='Support'><span>Umi</span><img alt='Support'><span>Supreme</span></div>
      <div><span data-highlightingclass='Please Not Hero Ban'><img alt='Please Not Hero Ban logo'></span><span>Player roster</span><img alt='DPS'><span>RLG5656</span><img alt='DPS'><span>Suraimu1</span><img alt='Tank'><span>UYOU</span><img alt='Support'><span>Neivis</span><img alt='Support'><span>Langley</span></div>
      <h2 id='Results'>Results</h2>
    """
    hints = ["VARREL", "MURASH GAMING", "ENTER FORCE.36", "99DIVINE", "Please Not Hero Ban"]
    rb = parse_rosters_by_visual_tokens(roster_boundary_html, "JP", hints, "test")
    assert any(x["team"] == "VARREL" and x["player"] == "KSG" for x in rb)
    assert any(x["team"] == "MURASH GAMING" and x["player"] == "PEPPI" for x in rb)
    assert any(x["team"] == "ENTER FORCE.36" and x["player"] == "Fearless" for x in rb)
    assert any(x["team"] == "99DIVINE" and x["player"] == "MN3" for x in rb)
    assert any(x["team"] == "Please Not Hero Ban" and x["player"] == "UYOU" for x in rb)
    assert not any(x["team"] == "VARREL" and x["player"] in {"Fearless", "PEPPI"} for x in rb)
    assert not any(x["team"] == "99DIVINE" and x["player"] in {"UYOU", "RLG5656"} for x in rb)

    # Regression: a free-form note can mention a future team before its real
    # card.  That mention must never become a roster boundary.
    note_boundary_html = """
      <h2><span id='Participants'>Participants</span></h2>
      <div><span data-highlightingclass='Uwinks'>Uwinks</span><span>Uwinks</span><span>Player roster</span>
        <img alt='DPS'><span>Yot1y</span><img alt='Tank'><span>xzahyo</span><img alt='Support'><span>UGH</span><img alt='Support'><span>EuclidEUC</span>
        <span>Notes (1)</span><span>Please Not Hero Ban are forced to requalify through the open qualifiers.</span>
      </div>
      <div><span data-highlightingclass='99DIVINE'>99DIVINE</span><span>99DIVINE</span><span>Player roster</span>
        <img alt='DPS'><span>MN3</span><img alt='DPS'><span>ALTHOUGH</span><img alt='Tank'><span>Ichi</span>
        <img alt='Support'><span>Sakume</span><img alt='Support'><span>Umi</span><img alt='Support'><span>Supreme</span>
      </div>
      <div><span data-highlightingclass='Please Not Hero Ban'>Please Not Hero Ban</span><span>Please Not Hero Ban</span><span>Player roster</span>
        <img alt='DPS'><span>RLG5656</span><img alt='DPS'><span>Suraimu1</span><img alt='Tank'><span>UYOU</span>
        <img alt='Support'><span>Neivis</span><img alt='Support'><span>깡돌이</span><img alt='Support'><span>Langley</span>
      </div><h2 id='Results'>Results</h2>
    """
    nb_hints = ["Uwinks", "99DIVINE", "Please Not Hero Ban"]
    nb = parse_rosters_by_visual_tokens(note_boundary_html, "JP", nb_hints, "test")
    assert {x["player"] for x in nb if x["team"] == "99DIVINE" and x["role"] in ROLE_NAMES} == {"MN3", "ALTHOUGH", "Ichi", "Sakume", "Umi", "Supreme"}
    assert {x["player"] for x in nb if x["team"] == "Please Not Hero Ban" and x["role"] in ROLE_NAMES} == {"RLG5656", "Suraimu1", "UYOU", "Neivis", "깡돌이", "Langley"}

    # A short team name may occur only once in structural metadata.  It must
    # still create a boundary so T1 players cannot be merged into Crazy Raccoon.
    kr_single_header_html = """
      <h2><span id='Participants'>Participants</span></h2>
      <div><span data-highlightingclass='Crazy Raccoon'></span><span>Crazy Raccoon</span><span>Player roster</span>
        <img alt='DPS'><span>LIP</span><img alt='Tank'><span>JunBin</span><img alt='Support'><span>CH0R0NG</span><img alt='Support'><span>vigilante</span>
      </div>
      <div><span data-highlightingclass='T1'></span><span>Player roster</span>
        <img alt='DPS'><span>Proud</span><img alt='DPS'><span>ZEST</span><img alt='Tank'><span>DONGHAK</span><img alt='Support'><span>skewed</span>
      </div>
    """
    kr_rows = parse_rosters_by_visual_tokens(kr_single_header_html, "KR", ["Crazy Raccoon", "T1"], "test")
    assert {r["player"] for r in kr_rows if r["team"] == "Crazy Raccoon"} == {"LIP", "JunBin", "CH0R0NG", "vigilante"}
    assert {r["player"] for r in kr_rows if r["team"] == "T1"} == {"Proud", "ZEST", "DONGHAK", "skewed"}

    # 2026 Korea TeamParticipants layout: there is no literal "Player roster";
    # cards use Main/Staff tabs. This mirrors the live page layout.
    kr_main_staff_html = """
      <h2><span id='Participants'>Participants</span></h2>
      <div><span data-highlightingclass='ZETA DIVISION'></span><span>ZETA DIVISION</span><span>Qualified</span><span>Stage 2</span><span>1st</span><span>Main</span><span>Staff</span>
        <img alt='DPS'><img alt='South Korea'><span>Proper</span>
        <img alt='DPS'><img alt='South Korea'><span>KNIFE</span>
        <img alt='Tank'><img alt='South Korea'><span>Mealgaru</span>
        <img alt='Tank'><img alt='South Korea'><span>Bernar</span>
        <img alt='Support'><img alt='South Korea'><span>shu</span>
        <img alt='Support'><img alt='South Korea'><span>Viol2t</span>
      </div>
      <div><span data-highlightingclass='Crazy Raccoon'></span><span>Crazy Raccoon</span><span>Qualified</span><span>Stage 2</span><span>2nd</span><span>Main</span><span>Staff</span>
        <img alt='DPS'><img alt='South Korea'><span>LIP</span>
        <img alt='DPS'><img alt='South Korea'><span>HeeSang</span>
        <img alt='DPS'><img alt='South Korea'><span>Stalk3r</span>
        <img alt='Tank'><img alt='South Korea'><span>MAX</span>
        <img alt='Tank'><img alt='South Korea'><span>JunBin</span>
        <img alt='Support'><img alt='South Korea'><span>CH0R0NG</span>
        <img alt='Support'><img alt='South Korea'><span>vigilante</span>
      </div>
      <div><span data-highlightingclass='T1'></span><span>T1</span><span>Qualified</span><span>Stage 2</span><span>3rd</span><span>Main</span><span>Staff</span>
        <img alt='DPS'><img alt='South Korea'><span>Proud</span>
        <img alt='DPS'><img alt='South Korea'><span>ZEST</span>
        <img alt='Tank'><img alt='South Korea'><span>DONGHAK</span>
        <img alt='Tank'><img alt='South Korea'><span>Jasm1ne</span>
        <img alt='Support'><img alt='South Korea'><span>skewed</span>
        <img alt='Support'><img alt='South Korea'><span>Bliss</span>
      </div>
    """
    kms = parse_rosters_by_visual_tokens(kr_main_staff_html, "KR", ["ZETA DIVISION", "Crazy Raccoon", "T1"], "test")
    assert {r["player"] for r in kms if r["team"] == "Crazy Raccoon" and r["role"] in ROLE_NAMES} == {"LIP", "HeeSang", "Stalk3r", "MAX", "JunBin", "CH0R0NG", "vigilante"}
    assert {r["player"] for r in kms if r["team"] == "T1" and r["role"] in ROLE_NAMES} == {"Proud", "ZEST", "DONGHAK", "Jasm1ne", "skewed", "Bliss"}

    fb = jp_stage3_fallback_rosters("test")
    assert {x["player"] for x in fb if x["team"] == "VARREL" and x["role"] in ROLE_NAMES} == {"Nico", "Qki", "TOPDRAGON", "KSG", "Qloud", "Sley"}
    assert {x["player"] for x in fb if x["team"] == "99DIVINE" and x["role"] in ROLE_NAMES} == {"MN3", "ALTHOUGH", "Ichi", "Sakume", "Umi", "Supreme"}

    nested_standings_html = (
        '<h2><span id="Standings">Standings</span></h2><table>'
        '<tr><th>Rank</th><th>Team</th><th>Match</th><th>Map</th><th>Diff</th></tr>'
        '<tr><td>1.</td><td><span>ENTER FORCE.36</span></td><td><span>2</span>–<span>0</span></td><td><span>6</span>–<span>0</span></td><td>+6</td></tr>'
        '<tr><td>2.</td><td><span>VARREL</span></td><td><span>2</span>–<span>0</span></td><td><span>6</span>–<span>1</span></td><td>+5</td></tr>'
        '</table>'
    )
    st_nested = parse_standings(nested_standings_html, 8, ["ENTER FORCE.36", "VARREL"])
    assert len(st_nested) == 2 and st_nested[0]["team"] == "ENTER FORCE.36" and st_nested[1]["map_record"] == "6-1"

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
    wiki_roster_sample = """==Participants==
{{TeamParticipants|showplayerinfo=true
|{{Opponent|Crazy Raccoon
  |players={{Persons
    |{{Person|LIP|role=dps|played=false}}
    |{{Person|HeeSang|role=dps}}
    |{{Person|MAX|role=tank|played=false}}
    |{{Person|JunBin|role=tank}}
    |{{Person|CH0R0NG|role=sup}}
    |{{Person|vigilante|role=sup}}
    |{{Person|Kong|role=Assistant coach|type=staff}}
  }}
}}
|{{Opponent|T1
  |players={{Persons
    |{{Person|Proud|role=dps}}
    |{{Person|ZEST|role=dps}}
    |{{Person|DONGHAK|role=tank}}
    |{{Person|Jasm1ne|role=tank}}
    |{{Person|skewed|role=sup}}
    |{{Person|Bliss|role=sup}}
    |{{Person|RUSH|role=head coach|type=staff}}
  }}
}}
}}
==Results==
"""
    wr = parse_rosters_wikitext(wiki_roster_sample, "KR", ["Crazy Raccoon", "T1"], "test")
    assert {x["player"] for x in wr if x["team"] == "Crazy Raccoon" and x["role"] in ROLE_NAMES} == {"LIP", "HeeSang", "MAX", "JunBin", "CH0R0NG", "vigilante"}
    assert {x["player"] for x in wr if x["team"] == "T1" and x["role"] in ROLE_NAMES} == {"Proud", "ZEST", "DONGHAK", "Jasm1ne", "skewed", "Bliss"}
    assert any(x["team"] == "Crazy Raccoon" and x["player"] == "Kong" and x["role"] == "Assistant Coach" for x in wr)
    assert any(x["team"] == "Crazy Raccoon" and x["player"] == "LIP" and x["status"] == "DNP" for x in wr)

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

    # v4 cache behavior: only a recent OK entry is considered fresh.
    _now = datetime.now(timezone.utc)
    _entry = {"revid": 123, "fetched_at": _now.isoformat(timespec="seconds"), "generator_version": VERSION, "status": "OK"}
    assert cache_entry_is_fresh(_entry, 123, _now, 6.0)
    assert not cache_entry_is_fresh(_entry, 124, _now, 6.0)
    _partial = {"revid": 123, "fetched_at": _now.isoformat(timespec="seconds"), "generator_version": VERSION, "status": "PARTIAL_ERROR", "retry_after": (_now + timedelta(minutes=30)).isoformat(timespec="seconds")}
    assert not cache_entry_is_fresh(_partial, 123, _now, 6.0)
    assert cache_entry_retry_blocked(_partial, _now)

    print("SELF_TEST_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test() if "--self-test" in sys.argv else run())
