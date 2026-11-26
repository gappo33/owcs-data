"""Incremental OWCS archive, independent of legacy data/owcs.json."""
import os, re, json, hashlib, copy, sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
import owcs_parsers as p
from bs4 import BeautifulSoup
from collections import defaultdict
from urllib.parse import unquote

PARSER_REV = 4
SCHEDULER_REV = 2
REGION_ORDER = ["WORLD", "ASIA", "EMEA", "CHINA", "NA", "JP", "KR", "PACIFIC"]
# Explicit aliases confirmed on the China event pages. No ambiguous global abbreviations.
CHINA_ALIASES = {"wbg":"Weibo Gaming", "weibogamingwbg":"Weibo Gaming", "ag":"All Gamers", "allgamersag":"All Gamers", "jdg":"JD Gaming", "jdgamingjdg":"JD Gaming", "oug":"OU Gaming", "ougamingoug":"OU Gaming", "sv":"Solus Victorem", "solusvictoremsv":"Solus Victorem", "hng":"HUNENG Gaming", "hunenggaminghng":"HUNENG Gaming", "bf":"Black Flag", "ssf":"SHENGSHI Esports"}
CHINA_ALIASES.update({'sse':'SHENGSHI Esports','shengshiesports':'SHENGSHI Esports','hunenggaming':'HUNENG Gaming'})
p.TEAM_ALIASES.setdefault("CHINA", {}).update(CHINA_ALIASES)
# Full composite labels are not standalone team names. Region-scoped examples
# observed in the dashboard; generic matching below handles other teams.
COMPOSITE_LABELS = {
    'NA': {'daldallasfueldallasfuel':'Dallas Fuel', 'dsgdisguiseddisguised':'Disguised', 'ntmrntmrntmr':'NTMR', 'ssgspacestationgamingspacestationgaming':'Spacestation Gaming', 'tlteamliquidteamliquid':'Team Liquid'},
    'CHINA': {'hnghunenghunenggaming':'HUNENG Gaming', 'jdgjdgamingjdgaming':'JD Gaming', 'sse shengshi shengshi esports'.replace(' ',''):'SHENGSHI Esports', 'svsolusvictoremsolusvictorem':'Solus Victorem', 'ougougamingougaming':'OU Gaming'},
}

OUT=Path('data/owcs_all.json'); OLD=Path('data/owcs.json')
PREFIX='Overwatch Champions Series/'
# Confirmed 2026 Stage 3 paths. Future years use discovery, not hardcoded names.
ROOT=PREFIX+'2026/'
REGIONAL_ROOTS=[ROOT+x for x in ('China/Stage 3','NA/Stage 3','EMEA/Stage 3','Asia/Stage 3/Pacific','Asia/Stage 3/Japan','Asia/Stage 3/Korea')]
SEEDS=[ROOT+'World Finals',ROOT+'Asia/Stage 3']+[y for x in REGIONAL_ROOTS for y in (x,x+'/Regular Season')]

PHASE=re.compile(r'^(regular season(?: - (?:swiss stage|round robin|group stage))?|regional playoffs?|playoffs?|group stage|main event|knockout stage|swiss stage|bracket|round robin|results|matches|play-in|play in|seeding(?: matches)?|last chance qualifier)$',re.I)
EXCLUDE=re.compile(r'^(?:statistics|stats|broadcast talent|broadcasts?|media|gallery|prize pool|rankings|standings|overview|rules|schedule|rosters?|participants|player statistics|team statistics|map statistics|transfers|tickets|references|trivia|format|all matches|notes)$',re.I)
def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def read(path):
    if not path.exists(): return {}
    return json.loads(path.read_text(encoding='utf-8'))
def save(path, data):
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix('.tmp'); tmp.write_text(json.dumps(data,ensure_ascii=False,separators=(',',':'))+'\n',encoding='utf-8'); tmp.replace(path)
def norm(title): return re.sub(r' +',' ',str(title).replace('_',' ')).strip()
def key(s): return re.sub(r'[^\w]','',str(s).casefold(),flags=re.UNICODE)
def event_for(title):
    parts=norm(title).split('/')
    while len(parts)>3 and PHASE.fullmatch(parts[-1]): parts.pop()
    return '/'.join(parts)
def region_for(title):
    parts=norm(title).split('/'); m={'japan':'JP','korea':'KR','pacific':'PACIFIC','china':'CHINA','na':'NA','north america':'NA','emea':'EMEA','asia':'ASIA'}
    for x in reversed(parts):
        if x.casefold() in m: return m[x.casefold()]
    return 'WORLD'
def candidate(title):
    parts=norm(title).split('/')
    return len(parts)>=3 and parts[0]==PREFIX.rstrip('/') and re.fullmatch(r'20\d{2}',parts[1]) and int(parts[1])>=2026 and not any(EXCLUDE.fullmatch(x) for x in parts[2:])
def parse_info(wiki):
    for name in ['infobox league','infobox tournament']:
        ts=p.extract_templates_by_name(wiki,name)
        if ts:
            q=(p.parse_template(ts[0]) or {}).get('params',{})
            return {k:p.clean_wiki_value(q.get(k,'')) for k in ('name','sdate','edate','team_number','series','publishertier')}
    return {}
def canonical(name, region):
    # Region-specific aliases only. For international matches, use full names from the source.
    if key(name) in {'ssgspacestationspacestationgaming','ssgspacestationgamingspacestationgaming'}:
        return 'Spacestation Gaming'
    return COMPOSITE_LABELS.get(region,{}).get(key(name), p.canonical_team(region,name))

def page_aliases(html, region):
    """Use team links in team-template nodes, not arbitrary player links."""
    soup=BeautifulSoup(html,'html.parser'); pairs=defaultdict(set)
    for a in soup.select('.team-template-text a, .team-template-team-short a, .team-template-team-standard a'):
        label=a.get_text(' ',strip=True); title=a.get('title','').strip()
        href=a.get('href','')
        if not title or not label or title.startswith(('File:','Category:','Special:')) or not href.startswith('/overwatch/'):
            continue
        path=unquote(href.split('?',1)[0].split('#',1)[0].rsplit('/',1)[-1]).replace('_',' ')
        if not path or key(path)!=key(title) or '/' in title: continue
        full=canonical(title,region)
        if len(key(full))>=3 and key(label): pairs[key(label)].add(full)
    return {k:next(iter(v)) for k,v in pairs.items() if len(v)==1}

def alias_name(name,region,aliases):
    n=canonical(name,region)
    return aliases.get(key(n),aliases.get(key(name),n))

def roster_section(wiki):
    # Prefer self-contained participant templates; headings may be H2 or H3.
    containers=p.extract_templates_by_name(wiki,'teamparticipants')
    if containers: return '== Participants ==\n'+'\n'.join(containers)+'\n'
    m=re.search(r'(?mi)^(={2,5})\s*(?:Participants|Teams)\s*\1\s*$',wiki)
    if not m: return ''
    level=len(m.group(1)); tail=wiki[m.end():]; end=re.search(r'(?m)^={2,'+str(level)+r'}[^=\n].*?={2,'+str(level)+r'}\s*$',tail)
    return '== Participants ==\n'+(tail[:end.start()] if end else tail)+'\n'

def html_roster_card(html, team, region, source, aliases):
    """Only members within the uniquely identified team participant card."""
    soup=BeautifulSoup(html,'html.parser'); cards=[]
    for card in soup.select('.team-participant-card'):
        names={key(alias_name(a.get_text(' ',strip=True),region,aliases)) for a in card.select('.team-participant-card__header .block-team .name a') if a.get_text(strip=True)}
        if names=={key(team)}: cards.append(card)
    if len(cards)!=1: return []
    rows=[]
    for member in cards[0].select('.team-participant-card__member'):
        node=member.select_one('.team-participant-card__member-name .name')
        if not node: continue
        player=node.get_text(' ',strip=True)
        image=member.select_one('.team-participant-card__member-role-left img')
        text=member.select_one('.team-participant-card__member-role-right')
        raw=(image.get('alt') or image.get('title') or '') if image else ''
        role=raw if raw in {'Tank','DPS','Support','Flex'} else ''
        if not role and text:
            staff=text.get_text(' ',strip=True)
            if staff in p.STAFF_NAMES: role=staff
        if player and role: rows.append(dict(region=region,team=team,role=role,player=player,status='',source=source))
    rows=list({(key(r['player']),r['role']):r for r in rows}.values())
    players=[r for r in rows if r['role'] in {'Tank','DPS','Support','Flex'}]
    if not 4<=len(players)<=12 or len({key(r['player']) for r in players})!=len(players): return []
    return rows

def parse_roster_cards(wiki,html,region,source,aliases):
    section=roster_section(wiki); hints=[]; rows=[]; warnings=[]
    # Parse each card independently. Do not discard all teams because one roster is missing.
    for raw in p.extract_templates_by_name(section,'opponent'):
        tpl=p.parse_template(raw) or {}; q=tpl.get('params',{}); pos=tpl.get('positional',[])
        name=alias_name((pos[0] if pos else '') or q.get('team','') or q.get('name',''),region,aliases)
        if not name or key(name) in {'tbd','tba','bye','unknown'}: continue
        if name not in hints: hints.append(name)
        player_blob=q.get('players',''); staff_blob=q.get('staff','')
        if not player_blob: continue
        for blob,staff in ((player_blob,False),(staff_blob,True)):
            for raw_person in p.extract_templates_by_name(blob,'person'):
                person=p.parse_template(raw_person) or {}; pp=person.get('params',{}); ps=person.get('positional',[])
                player=p.clean_wiki_value((ps[0] if ps else '') or pp.get('name','')).strip()
                if not player: continue
                is_staff=staff or p.clean_wiki_value(pp.get('type','')).lower()=='staff'
                role=p.normalize_roster_role(pp.get('role',''),is_staff) or ('Staff' if is_staff else 'Flex')
                status='DNP' if p.clean_wiki_value(pp.get('played','')).lower() in {'false','0','no'} else ''
                rows.append(dict(region=region,team=name,role=role,player=player,status=status,source=source))
    rows=list({(key(r['team']),key(r['player']),r['role']):r for r in rows}.values())
    by=defaultdict(list)
    for r in rows: by[r['team']].append(r)
    valid=[]
    for team in hints:
        rs=by[team]; n=sum(r['role'] in {'Tank','DPS','Support','Flex'} for r in rs)
        if 4<=n<=12: valid.extend(rs)
        else:
            recovered=html_roster_card(html,team,region,source,aliases) if html else []
            if recovered: valid.extend(recovered)
            else: warnings.append(f'Roster missing/invalid: {team} ({n} players)')
    # Only structural hints are allowed. Do not guess player ownership from adjacent team text.
    if section and not valid: warnings.append('Participants found but no validated roster')
    return valid,hints,warnings

def match_id(m, event):
    # Dated matches dedupe across parent/child pages within an event, independent of side order.
    ts=m.get('date_jst') or ''; parsed=p.parse_iso_dt(ts); ts=parsed.astimezone(timezone.utc).isoformat() if parsed else ts; teams=sorted(key(x) for x in [m['team_a'],m['team_b']]); base=[event,ts,*teams]
    return hashlib.sha256('|'.join(base).encode()).hexdigest()[:24]
def normalize_match(m,event,region,aliases=None):
    m=copy.deepcopy(m); old=[m['team_a'],m['team_b']]; a,b=[alias_name(t,region,aliases or {}) for t in old]
    if not a or not b or key(a)==key(b) or any(key(t) in {'tbd','tba','bye','unknown'} for t in [a,b]): return None
    m['team_a'],m['team_b']=a,b; m['event_id']=event
    # Unknown dates cannot safely identify recurring games. Retain as unverified, separate per source.
    if not m.get('date_jst'): return None
    for x in m.get('maps',[]):
        for f in ('winner','picker'):
            if x.get(f) in old: x[f]=[a,b][old.index(x[f])]
            elif x.get(f) and x[f]!='DRAW':
                resolved=alias_name(x[f],region,aliases or {})
                if key(resolved)==key(a): x[f]=a
                elif key(resolved)==key(b): x[f]=b
        # Only apply the established JP/KR rule; elsewhere require explicit source evidence.
        if region not in ('JP','KR') and x.get('picker_basis')=='Previous map loser': x['picker']=''; x['picker_basis']='Unknown for this event'
    m['maps']=[x for x in m.get('maps',[]) if x.get('map') and x.get('winner') in (a,b,'DRAW')]
    if not isinstance(m.get('score_a'),int) or not isinstance(m.get('score_b'),int): m['finished']=False
    ts=p.parse_iso_dt(m['date_jst'])
    if ts and ts>datetime.now(timezone.utc): m['finished']=False
    m['id']=match_id(m,event); return m

def parse_page(title, html, wiki, rev):
    event=event_for(title); region=region_for(event); src=p.source_url(title.replace(' ','_')); info=parse_info(wiki)
    aliases=page_aliases(html,region)
    rosters,hints,warnings=parse_roster_cards(wiki,html,region,src,aliases)
    # Match sources: wikitext supplies maps and bans; rendered popups can fill series results.
    rows={}; undated=0
    raw_wiki=p.parse_matches_wikitext(wiki,region,src)
    raw_html=p.parse_matches(html,region,src)
    # Wikitext opponent names also provide identity evidence when map detail is absent.
    hints=list(dict.fromkeys(hints+[alias_name(m[f],region,aliases) for m in raw_wiki for f in ('team_a','team_b') if m.get(f)]))
    html_rows=defaultdict(list); wiki_rows=defaultdict(list)
    for raw, bucket in [(r,html_rows) for r in raw_html]+[(r,wiki_rows) for r in raw_wiki]:
        if not raw.get('date_jst'): undated+=1; continue
        m=normalize_match(raw,event,region,aliases)
        if m: bucket[m['id']].append(m)
    conflicts=0
    for mid in dict.fromkeys([*html_rows,*wiki_rows]):
        hs=html_rows[mid]; ws=wiki_rows[mid]
        h=max(hs,key=lambda m:bool(m.get('finished')),default=None)
        w=max(ws,key=lambda m:(bool(m.get('finished')),len(m.get('maps',[]))),default=None)
        if h and w:
            same=key(h['team_a'])==key(w['team_a'])
            scores=(w['score_a'],w['score_b']) if same else (w['score_b'],w['score_a'])
            agree=(h['score_a'],h['score_b'])==scores
            # Do not join conflicting final results or explicit unfinished markers.
            if agree and not (h.get('finished') and w.get('finish_explicit_false')):
                m=copy.deepcopy(w)
                m['finished']=bool(h.get('finished') or w.get('finished'))
                # Keep Wikitext orientation: map winner and both BAN sides remain aligned.
                rows[mid]=m
            else:
                conflicts+=1
                m=copy.deepcopy(h if h.get('finished') else w)
                if h.get('finished') and w.get('finish_explicit_false'): m['finished']=False
                rows[mid]=m
        else: rows[mid]=h or w
    if conflicts: warnings.append(f'HTML/Wikitext result conflicts: {conflicts}; verify source')
    # Never use standings (especially cumulative Swiss snapshots) as independent matches.
    if undated: warnings.append(f'{undated} undated match candidates excluded; verify source')
    if ('{{match' in wiki.lower() or 'brkts-match-info-popup' in html) and not rows: warnings.append('Match templates present but no dated matches parsed')
    if title==event and not rosters: warnings.append('No validated event roster; check Participants source')
    finished=[m for m in rows.values() if m.get('finished')]
    source_unresolved=0
    for raw in p.extract_templates_by_name(wiki,'match'):
        q=(p.parse_template(raw) or {}).get('params',{})
        flag=p.clean_wiki_value(q.get('finished','')).lower()
        a,sa=p.parse_opponent(q.get('opponent1',''),region); b,sb=p.parse_opponent(q.get('opponent2',''),region)
        a=a or q.get('opponent1literal',''); b=b or q.get('opponent2literal','')
        if flag in {'false','0','no'} or not a or not b or key(a) in {'tbd','tba','bye','unknown'} or key(b) in {'tbd','tba','bye','unknown'} or not p.parse_liquipedia_date(q.get('date',''),region):
            source_unresolved+=1
    audit={'unresolved_templates':source_unresolved+conflicts,'undated':undated,'wiki_matches':len(raw_wiki),'html_matches':len(raw_html)}
    no_maps=sum(not m.get('maps') for m in finished)
    incomplete=sum(sum(x.get('winner') in (m['team_a'],m['team_b']) for x in m.get('maps',[])) != m['score_a']+m['score_b'] for m in finished)
    if incomplete: warnings.append(f'{incomplete}/{len(finished)} finished series have incomplete map detail (score comparison)')
    if no_maps: warnings.append(f'{no_maps}/{len(finished)} finished series have no detailed maps')
    if not rows and title!=event: warnings.append('No dated matches on phase page')
    return {'title':title,'event_id':event,'region':region,'source':src,'revision':rev,'fetched_at':now(),'parser_rev':PARSER_REV,'info':info,'matches':list(rows.values()),'rosters':rosters,'team_hints':hints,'aliases':aliases,'audit':audit,'warnings':list(dict.fromkeys(warnings))}

def diagnostic(title, html, wiki, row):
    # Only current response; no extra API calls. Uploaded as a private-run artifact, not committed.
    if not row['warnings']: return
    folder=Path('data/archive_debug')/hashlib.sha256(title.encode()).hexdigest()[:12]
    folder.mkdir(parents=True,exist_ok=True)
    (folder/'summary.json').write_text(json.dumps({k:row[k] for k in ('title','region','source','revision','warnings','team_hints','aliases')},ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'page.wikitext.txt').write_text(wiki,encoding='utf-8')
    (folder/'page.html').write_text(html,encoding='utf-8')

def discover(client,year):
    params={'action':'query','list':'allpages','apprefix':PREFIX+str(year)+'/','apnamespace':0,'apfilterredir':'nonredirects','aplimit':100,'format':'json','formatversion':2}; out={}
    while True:
        result=client._request(params,is_parse=False).json()
        if 'error' in result: raise RuntimeError(str(result['error']))
        for row in result.get('query',{}).get('allpages',[]):
            title=norm(row['title'])
            if candidate(title): out[title]=row.get('pageid')
        if 'continue' not in result: break
        params.update(result['continue'])
    return out

def team_identity_maps(pages, legacy=None):
    """Use registered rosters, link targets and map-detailed matches as identity evidence.
    Resolve case variants globally, composite labels only against event-local evidence.
    Never merge on substring or edit distance alone."""
    evidence=defaultdict(lambda:defaultdict(lambda:defaultdict(int)))
    global_names=defaultdict(lambda:defaultdict(int))
    observed=defaultdict(set)
    link_aliases=defaultdict(lambda:defaultdict(set))
    def add(eid, region, name, weight):
        name=canonical(name,region)
        if not key(name) or key(name) in {'tbd','tba','bye','unknown','draw'}: return
        evidence[eid][key(name)][name]+=weight
        global_names[key(name)][name]+=weight
    for row in pages.values():
        eid,reg=row['event_id'],row['region']
        for r in row.get('rosters',[]):
            observed[eid].add(r['team']); add(eid,reg,r['team'],5)
        for name in row.get('team_hints',[]):
            observed[eid].add(name); add(eid,reg,name,3)
        for k,v in row.get('aliases',{}).items():
            add(eid,reg,v,4); link_aliases[eid][k].add(canonical(v,reg))
        for m in row.get('matches',[]):
            for name in (m['team_a'],m['team_b']):
                observed[eid].add(name)
                if m.get('maps'): add(eid,reg,name,2)
    # Legacy rows are candidates for normalization, not trusted identity evidence.
    legacy_paths={'JP':'Asia/Stage 3/Japan','KR':'Asia/Stage 3/Korea','NA':'NA/Stage 3','EMEA':'EMEA/Stage 3','CHINA':'China/Stage 3'}
    for reg,r in (legacy or {}).get('regions',{}).items():
        if reg not in legacy_paths: continue
        eid=ROOT+legacy_paths[reg]
        for m in r.get('finished_matches',[])+r.get('upcoming_matches',[]):
            observed[eid].update(t for t in [m.get('team_a'),m.get('team_b')] if t)
        observed[eid].update(x['team'] for x in r.get('rosters',[]) if x.get('team'))
    preferred={k:sorted(v,key=lambda n:(-v[n],n.casefold(),n))[0] for k,v in global_names.items()}
    result={}
    def is_composite(raw, full):
        rk,fk=key(raw),key(full)
        if not fk or len(fk)<2 or rk==fk: return False
        words=re.findall(r'[^\W_]+',full,flags=re.UNICODE)
        labels={fk, ''.join(w[0] for w in words).casefold()}
        if words: labels.update((key(words[0]),key(words[-1])))
        labels.discard('')
        # Typical DOM: abbreviation + mobile label + desktop name, or repeated name.
        # Also accept unknown leading acronym only if the full name is repeated.
        if rk==fk+fk or (rk.endswith(fk+fk) and 1<=len(rk)-2*len(fk)<=6): return True
        return any(rk==a+fk+fk or rk==a+b+fk or rk==a+fk+b for a in labels for b in labels)
    for eid,names in observed.items():
        region=region_for(eid); trusted={k:preferred[k] for k in evidence[eid]}
        # unique link aliases override abbreviations, but not a genuine registered team.
        aliases=dict(trusted)
        for k,vs in link_aliases[eid].items():
            targets={preferred.get(key(v),v) for v in vs}
            if len(targets)==1 and (k not in trusted or key(trusted[k])==key(next(iter(targets)))): aliases[k]=next(iter(targets))
        # Prefer an explicit source identity; otherwise collapse proven compound labels.
        for raw in sorted(names):
            clean=canonical(raw,region); ck=key(clean)
            direct=aliases.get(ck) or aliases.get(key(raw))
            if direct: aliases[key(raw)]=direct; continue
            candidates={v for v in trusted.values() if is_composite(raw,v)}
            if len(candidates)==1: aliases[key(raw)]=next(iter(candidates))
            elif ck in preferred: aliases[key(raw)]=preferred[ck]
            elif clean!=raw: aliases[key(raw)]=clean
        for raw in sorted(names):
            clean=canonical(raw,region); ck=key(clean)
            if ck and key(raw) not in aliases:
                aliases[key(raw)]=aliases.get(ck,preferred.get(ck,clean))
        result[eid]=aliases
    return result


def merge_archive(state, legacy=None):
    events={}; matches={}; rosters={}
    event_aliases=team_identity_maps(state['pages'],legacy)
    state['team_aliases']=event_aliases
    for title,row in state['pages'].items():
        eid=row['event_id']; info=row.get('info',{}); e=events.setdefault(eid,{'id':eid,'name':eid.replace(PREFIX,''),'year':int(eid.split('/')[1]),'region':row['region'],'source':p.source_url(eid.replace(' ','_')),'fetched_at':'','start':'','end':'','pages':[],'warnings':[]})
        if title==eid or not e['start']:
            e['name']=info.get('name') or e['name']; e['start']=info.get('sdate') or e['start']; e['end']=info.get('edate') or e['end']
        e['pages'].append(title); e['fetched_at']=max(e['fetched_at'],row.get('fetched_at','')); e['warnings']+=row.get('warnings',[])
        for original in row.get('matches',[]):
            m=normalize_match(original,eid,row['region'],event_aliases.get(eid,{}))
            if not m: continue
            prior=matches.get(m['id']); score=(bool(m.get('finished')),len(m.get('maps',[])),row.get('fetched_at',''))
            if prior is None or score >= prior[0]: matches[m['id']]=(score,m)
        for r in row.get('rosters',[]):
            name=alias_name(r['team'],row['region'],event_aliases.get(eid,{})); rk=(eid,key(name),key(r['player']),r.get('role','')); rosters[rk]={**r,'team':name,'event_id':eid}
    for title in state['discovered']:
        if absent_page(title,state): continue
        eid=event_for(title)
        if eid not in events: events[eid]={'id':eid,'name':eid.replace(PREFIX,''),'year':int(eid.split('/')[1]),'region':region_for(eid),'source':p.source_url(eid.replace(' ','_')),'fetched_at':'','start':'','end':'','pages':[],'warnings':['未取得']}
    mm=[v[1] for v in matches.values()]; rr=list(rosters.values()); by_m=defaultdict(list); by_r=defaultdict(list)
    for m in mm: by_m[m['event_id']].append(m)
    for r in rr: by_r[r['event_id']].append(r)
    for eid,e in events.items():
        rows=by_m[eid]; finished=[m for m in rows if m.get('finished')]; teams={m[x] for m in rows for x in ('team_a','team_b')}; roster_teams={r['team'] for r in by_r[eid]}; missing=sorted(teams-roster_teams)
        if missing: e['warnings'].append('Roster未収録: '+', '.join(missing))
        e['coverage']={'series':len(finished),'detail_maps':sum(len(m.get('maps',[])) for m in finished),'score_maps':sum(m['score_a']+m['score_b'] for m in finished),'roster_teams':len(roster_teams),'match_teams':len(teams)}
        e['warnings'] += [err for err in state.get('errors',[]) if err.startswith(eid+':') or err.startswith(eid+'/')]
        e['warnings']=list(dict.fromkeys(e['warnings']))
    state['events']=sorted(events.values(),key=lambda e:e['id']); state['matches']=mm; state['rosters']=rr

def dt_age(value, dt):
    parsed=p.parse_iso_dt(value)
    return dt-parsed if parsed else timedelta(days=10000)

def date_only(value):
    try: return datetime.strptime(str(value), '%Y-%m-%d').date()
    except (ValueError,TypeError): return None

def known_pages(state):
    # Actual discovered pages plus seeds not yet checked. Don't invent child pages.
    return sorted(set(state.get('discovered',{})) | set(state.get('pages',{})))

def absent_page(t,state):
    # A guessed seed not listed in a successful allpages scan may be absent. Re-discovery can reopen it.
    return t in state.get('absent_pages',{}) and not state.get('discovered',{}).get(t) and t not in state.get('pages',{})

def schedule_hint(title, wiki, revision, dt):
    """Read only calendar and result markers; no MAP/roster extraction or storage."""
    info=parse_info(wiki); region=region_for(title); dates=[]; completed=[]
    for raw in p.extract_templates_by_name(wiki,'match'):
        q=(p.parse_template(raw) or {}).get('params',{})
        try: stamp=p.parse_liquipedia_date(q.get('date',''),region)
        except (ValueError,TypeError): continue
        if not stamp: continue
        dates.append(stamp.isoformat()); flag=p.clean_wiki_value(q.get('finished','')).lower()
        if stamp<=dt and flag in {'true','1','yes'}: completed.append(stamp.isoformat())
    return {'checked_at':dt.isoformat(),'revision':revision,'start':info.get('sdate',''),'end':info.get('edate',''),
            'dates':sorted(set(dates)), 'completed':sorted(set(completed))}

def schedule_scan(client,state,dt):
    """Bounded metadata requests, using same rate-limited session as parse."""
    hints=state.setdefault('schedule_hints',{}); attempts=state.setdefault('schedule_attempts',{}); due=[]; today=(dt+timedelta(hours=9)).date()
    for t in known_pages(state):
        if absent_page(t,state): continue
        eid=event_for(t); meta=state.get('lifecycle',{}).get(eid,{})
        if meta.get('collection')=='archived': continue
        h=hints.get(t,{}); start=date_only(h.get('start') or hints.get(eid,{}).get('start') or meta.get('start')); end=date_only(h.get('end') or meta.get('end'))
        interval=timedelta(hours=10)
        if start and start>today: interval=timedelta(days=1 if (start-today).days<=2 else 7)
        elif end and end<today: interval=timedelta(days=7)
        elif int(eid.split('/')[1])>today.year: interval=timedelta(days=7)
        if dt_age(h.get('checked_at'),dt)>=interval and dt_age(attempts.get(t),dt)>=timedelta(hours=10): due.append(t)
    # Never let a permanent stream of recent pages prevent initial discovery of older dates.
    due.sort(key=lambda t:(max(hints.get(t,{}).get('checked_at',''),attempts.get(t,'')),t not in SEEDS,t!=event_for(t),t))
    for i in range(0,min(len(due),40),10):
        batch=due[i:i+10]
        for t in batch: attempts[t]=dt.isoformat()
        # Unchanged revisions need no repeated raw content download.
        cached=[t for t in batch if hints.get(t,{}).get('revision') is not None]
        if cached:
            current=client.query_revisions(cached)
            for t in cached:
                if current.get(t) is not None and str(current[t])==str(hints[t]['revision']):
                    hints[t]['checked_at']=dt.isoformat()
            batch=[t for t in batch if t not in cached or current.get(t) is None or str(current[t])!=str(hints[t]['revision'])]
        if not batch: continue
        data=client._request({'action':'query','prop':'revisions','rvprop':'ids|content','rvslots':'main','titles':'|'.join(batch),'format':'json','formatversion':2},is_parse=False).json()
        if 'error' in data: raise RuntimeError('Schedule metadata: '+str(data['error']))
        for item in data.get('query',{}).get('pages',[]):
            t=norm(item.get('title','')); revs=item.get('revisions') or []
            if t not in batch or not revs: continue
            rev=revs[0]; slot=rev.get('slots',{}).get('main',{}); wiki=slot.get('content',slot.get('*',rev.get('*')))
            if not isinstance(wiki,str): continue
            try: hints[t]=schedule_hint(t,wiki,rev.get('revid'),dt)
            except Exception as e:
                state['errors'].append('Schedule hint '+t+': '+str(e)[:200]); continue
            row=state.get('pages',{}).get(t,{})
            if row.get('revision') is not None and rev.get('revid') is not None and str(row['revision'])!=str(rev['revid']): state.setdefault('revision_dirty',{})[t]=True
    print('SCHEDULE_SCAN',min(len(due),40),'of',len(due),'due')

def schedule_index(state,dt):
    """Aggregate signals by event, not by arbitrary page or region."""
    out=defaultdict(lambda:{'completed':None,'past':None,'future':None,'start':None,'end':None,'has_dates':False})
    def push(e,k,value,choose=max):
        if value is not None: e[k]=choose(e[k],value) if e[k] is not None else value
    for t,h in state.get('schedule_hints',{}).items():
        eid=event_for(t); e=out[eid]
        if t==eid: push(e,'start',date_only(h.get('start')),min); push(e,'end',date_only(h.get('end')))
        for raw in h.get('dates',[]):
            stamp=p.parse_iso_dt(raw)
            if stamp: e['has_dates']=True; push(e,'past' if stamp<=dt else 'future',stamp,min if stamp>dt else max)
        for raw in h.get('completed',[]):
            stamp=p.parse_iso_dt(raw)
            if stamp and stamp<=dt: push(e,'completed',stamp)
    for m in state.get('matches',[]):
        stamp=p.parse_iso_dt(m.get('date_jst')); e=out[m['event_id']]
        if not stamp: continue
        e['has_dates']=True; push(e,'past' if stamp<=dt else 'future',stamp,min if stamp>dt else max)
        if m.get('finished') and stamp<=dt: push(e,'completed',stamp)
    for eid,m in state.get('lifecycle',{}).items():
        e=out[eid]; push(e,'start',date_only(m.get('start')),min); push(e,'end',date_only(m.get('end')))
    return out

def lifecycle(state,dt):
    """Two independent axes: source calendar and collection quality. Never infer finish from a score alone."""
    grouped=defaultdict(list)
    for t in known_pages(state):
        if not absent_page(t,state): grouped[event_for(t)].append(t)
    result={}
    for eid, titles in grouped.items():
        root=state.get('pages',{}).get(eid,{})
        hint=state.get('schedule_hints',{}).get(eid,{})
        start=date_only(hint.get('start') or root.get('info',{}).get('sdate')); end=date_only(hint.get('end') or root.get('info',{}).get('edate'))
        today=(dt+timedelta(hours=9)).date()
        # Allow a 7-day settling window after scheduled end (timezone/late updates).
        final_day=end+timedelta(days=7) if end else None
        status='date_unknown'
        if end and today> end: status='ended'
        elif start and today<start: status='upcoming'
        elif start and today>=start: status='active'
        elif end and today<=end: status='active_or_upcoming'
        pages=state.get('pages',{}); fetched=[t for t in titles if pages.get(t,{}).get('fetched_at')]
        pending=[t for t in titles if t not in fetched or pages[t].get('parser_rev')!=PARSER_REV]
        # Rebuild from normalized/deduplicated archive, not duplicated raw HTML rows.
        ms=[m for m in state.get('matches',[]) if m.get('event_id')==eid]
        rs=[r for r in state.get('rosters',[]) if r.get('event_id')==eid and r.get('role') in {'Tank','DPS','Support','Flex'}]
        unfinished=sum(not m.get('finished') for m in ms)
        finished=[m for m in ms if m.get('finished')]
        map_missing=0; ban_missing=0; map_total=0
        for m in finished:
            maps=m.get('maps',[]); a,b=m['team_a'],m['team_b']
            if sum(x.get('winner')==a for x in maps)!=m.get('score_a') or sum(x.get('winner')==b for x in maps)!=m.get('score_b') or len({x.get('map_no',i) for i,x in enumerate(maps)})!=len(maps):
                map_missing+=1
            map_total+=len(maps); ban_missing+=sum(not x.get('ban_a') or not x.get('ban_b') for x in maps)
        teams={key(m[x]) for m in ms for x in ('team_a','team_b')}
        rteams={key(r['team']) for r in rs}
        root_hints={key(alias_name(n,region_for(eid),state.get('team_aliases',{}).get(eid,{}))) for n in root.get('team_hints',[])}
        roster_missing=len((teams|root_hints)-rteams)
        team_count=str(root.get('info',{}).get('team_number',''))
        if team_count.isdigit(): roster_missing=max(roster_missing,int(team_count)-len(rteams))
        source_issues=sum(int(pages[t].get('audit',{}).get('unresolved_templates',0))+int(pages[t].get('audit',{}).get('undated',0)) for t in fetched)
        source_issues+=sum(any('Match templates present but no dated' in w or 'Prior matches retained' in w or 'Roster retained from prior' in w or 'No dated matches on phase page' in w for w in pages[t].get('warnings',[])) for t in fetched)
        # Only freeze pages successfully fetched after the settling window. Missing date or missing source coverage prevents freeze.
        verify_pending=[t for t in fetched if final_day and (p.parse_iso_dt(pages[t].get('fetched_at')) is None or (p.parse_iso_dt(pages[t]['fetched_at'])+timedelta(hours=9)).date()<final_day)]
        discovery_ok=bool(state.get('discovery_dates',{}).get(eid.split('/')[1]))
        issues=[]
        if pending: issues.append(f'未取得・要再解析 {len(pending)}ページ')
        if not discovery_ok: issues.append('年度の大会探索未完了')
        if not root: issues.append('大会概要未取得')
        if not end: issues.append('終了日未確認')
        if not finished: issues.append('終了試合未収録')
        if unfinished: issues.append(f'結果未確定 {unfinished}試合')
        if map_missing: issues.append(f'MAP詳細不足 {map_missing}試合')
        if ban_missing: issues.append(f'BAN未記録 {ban_missing}MAP')
        if roster_missing: issues.append(f'ロスター未収録 {roster_missing}チーム')
        if source_issues: issues.append(f'解析要確認 {source_issues}件')
        if verify_pending: issues.append(f'終了後の確認待ち {len(verify_pending)}ページ')
        complete=(status=='ended' and today>=final_day and not issues)
        collection='archived' if complete else 'unfetched' if not fetched else 'partial'
        result[eid]={'calendar':status,'collection':collection,'total_pages':len(titles),'fetched_pages':len(fetched),'pending_pages':pending,'verify_pending':verify_pending,'issues':issues,'finished_series':len(finished),'map_missing_series':map_missing,'ban_missing_maps':ban_missing,'roster_missing_teams':roster_missing,'source_issues':source_issues,'end':str(end or ''),'start':str(start or '')}
    return result

def update_lifecycle(state,dt):
    state['lifecycle']=lifecycle(state,dt); index=schedule_index(state,dt)
    for eid,meta in state['lifecycle'].items():
        sig=index[eid]; meta['last_finished_match_at']=sig['completed'].isoformat() if sig['completed'] else ''
        meta['next_scheduled_match_at']=sig['future'].isoformat() if sig['future'] else ''
        meta['priority_reason']=priority_reason(sig,meta,dt)
    for e in state.get('events',[]):
        if e['id'] in state['lifecycle']:
            e['collection_status']=state['lifecycle'][e['id']]; e['start']=e['collection_status']['start'] or e.get('start',''); e['end']=e['collection_status']['end'] or e.get('end','')
    queue=sorted(known_pages(state),key=lambda t:target_priority(t,state,dt,index),reverse=True)
    state['next_queue']=[t for t in queue if target_priority(t,state,dt,index)[0]>=0][:12]
    return index

def priority_reason(sig,meta,dt):
    today=(dt+timedelta(hours=9)).date()
    if meta.get('collection')=='archived': return 'archived'
    if sig['start'] and sig['start']>today and not sig['completed']: return 'future'
    if sig['completed']: return 'finished'
    if (sig['past'] and (sig['past']+timedelta(hours=9)).date()<today) or (sig['end'] and sig['end']<today): return 'past_unverified'
    if sig['past']: return 'today_unconfirmed'
    if sig['future']: return 'future'
    return 'unknown'

def target_priority(t,state,dt,index=None):
    skip=(-1,0,0,'')
    if absent_page(t,state): return skip
    eid=event_for(t); meta=state.get('lifecycle',{}).get(eid,{}); row=state.get('pages',{}).get(t,{})
    if meta.get('collection')=='archived': return skip
    sig=(index if index is not None else schedule_index(state,dt))[eid]; reason=priority_reason(sig,meta,dt)
    today=(dt+timedelta(hours=9)).date()
    if reason in {'future','unknown','today_unconfirmed'}: return skip
    # Never parse a future-only phase even when another phase of its event has finished.
    h=state.get('schedule_hints',{}).get(t,{})
    page_dates=[p.parse_iso_dt(x) for x in h.get('dates',[])]; page_dates=[x for x in page_dates if x]
    page_start=date_only(h.get('start'))
    if (page_start and page_start>today) or (t!=eid and page_dates and min(page_dates)>dt): return skip
    if t!=eid and not h and not row and meta.get('calendar')!='ended': return skip
    last=row.get('fetched_at'); attempt=state.get('page_attempts',{}).get(t); dirty=state.get('revision_dirty',{}).get(t)
    fetched=p.parse_iso_dt(last); latest=sig['completed']; pending=t in meta.get('pending_pages',[])
    recent=bool(latest and (latest+timedelta(hours=9)).date()==today)
    # Changed revision can be processed on the next scheduled run; never repeatedly retry a failed page.
    if dt_age(attempt,dt)<timedelta(hours=10): return skip
    if not last or row.get('parser_rev')!=PARSER_REV or pending or dirty: pass
    elif t in meta.get('verify_pending',[]) and sig['end'] and today>=sig['end']+timedelta(days=7): pass
    elif latest and fetched and latest>fetched: pass
    elif recent and dt_age(last,dt)>=timedelta(hours=10): pass
    elif meta.get('calendar')=='ended' and dt_age(last,dt)>=timedelta(days=30): pass
    else: return skip
    # Verified results first, ordered by actual source match timestamp. Past schedule-only probes are lower.
    if latest: tier=2; stamp=latest.timestamp()
    else:
        tier=1; stamp=sig['past'].timestamp() if sig['past'] else datetime.combine(sig['end'],datetime.min.time(),tzinfo=timezone(timedelta(hours=9))).timestamp()
    # Avoid repeatedly fetching the root before uncollected phase pages of the same event.
    page_rank=3 if not last else 2 if dirty or pending else 1
    if t==eid: page_rank+=0.1
    return (tier,stamp,page_rank,t)

def revision_audit(client,state,dt):
    # Only already collected historical incomplete pages; completed archives get no parse or revision checks.
    due=[]
    for t,row in state.get('pages',{}).items():
        meta=state.get('lifecycle',{}).get(event_for(t),{})
        if meta.get('calendar')!='ended' or meta.get('collection')=='archived' or row.get('parser_rev')!=PARSER_REV: continue
        if dt_age(state.get('revision_checked',{}).get(t),dt)>=timedelta(days=7) and row.get('revision') is not None: due.append(t)
    due.sort(key=lambda t:state.get('revision_checked',{}).get(t,''))
    if not due: return
    due=due[:40]; versions=client.query_revisions(due)
    for t in due:
        rev=versions.get(t)
        if rev is None: continue
        state.setdefault('revision_checked',{})[t]=now()
        if str(rev)!=str(state['pages'][t].get('revision')): state.setdefault('revision_dirty',{})[t]=True
    print('REVISION_AUDIT',len(due),'changed',sum(bool(state.get('revision_dirty',{}).get(t)) for t in due))

def page_fingerprint(row):
    # Stable signature of the exact v1.7 cached content; exclude mutable fetch timestamps.
    payload={k:row.get(k) for k in ('title','revision','parser_rev','matches','rosters')}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def restore_debug(state, bundle):
    """Apply the six verified offline repairs only to the exact old cache snapshot."""
    if bundle.get('parser_rev')!=PARSER_REV or not isinstance(bundle.get('pages'),dict):
        raise ValueError('Invalid recovery bundle')
    count=0
    for title,item in bundle['pages'].items():
        old=state['pages'].get(title); new=copy.deepcopy(item['row'])
        if not old or new.get('title')!=title or new.get('parser_rev')!=PARSER_REV or page_fingerprint(old)!=item['old_fingerprint']:
            print('RESTORE_SKIP',title,'cache differs / already repaired / missing'); continue
        if str(old.get('revision'))!=str(new.get('revision')): raise ValueError('Revision mismatch: '+title)
        new['fetched_at']=old['fetched_at']
        new['reparsed_at']=now()
        state['pages'][title]=new
        hint=state.setdefault('schedule_hints',{}).get(title,{})
        if not hint or str(hint.get('revision'))==str(new['revision']):
            fresh=copy.deepcopy(item['schedule_hint']); fresh['checked_at']=hint.get('checked_at') or old['fetched_at']; state['schedule_hints'][title]=fresh
        count+=1
        print('RESTORED',title,len(new['matches']),sum(len(m.get('maps',[])) for m in new['matches'] if m.get('finished')),len(new['rosters']))
    return count

def main():
    state=read(OUT) or {'schema_version':4,'pages':{},'discovered':{},'discovery_dates':{},'errors':[]}
    if '--restore-debug' in sys.argv:
        if not OUT.exists() or not state.get('pages'): raise SystemExit('No existing archive to restore')
        bundle=read(Path('v18_recovery.json'))
        count=restore_debug(state,bundle)
        if count:
            merge_archive(state,read(OLD)); update_lifecycle(state,datetime.now(timezone.utc))
            state['last_operation']='restore_debug'; state['debug_restored_at']=now(); save(OUT,state)
        print('DEBUG_RESTORE_OK',count,'pages; zero API calls')
        return
    if '--repair-only' in sys.argv:
        if not OUT.exists() or not state.get('pages'):
            raise SystemExit('No collected archive pages to repair. Run normal collection first.')
        old_count=len(state.get('matches',[])); old_teams={m[k] for m in state.get('matches',[]) for k in ('team_a','team_b')}
        merge_archive(state,read(OLD))
        new_teams={m[k] for m in state['matches'] for k in ('team_a','team_b')}
        state['name_repair_at']=now(); state['last_operation']='repair_only'; update_lifecycle(state,datetime.now(timezone.utc)); save(OUT,state)
        print('NAME_REPAIR_OK: no API calls; matches',old_count,'->',len(state['matches']),'teams',len(old_teams),'->',len(new_teams))
        print('RETIRED LABELS:',sorted(old_teams-new_teams))
        return
    # Store provenance and cooldown; don't hammer on failure.
    state['errors']=[]; state['scheduler_rev']=SCHEDULER_REV; state['last_operation']='collect'; state['attempted_at']=now(); state.setdefault('page_attempts',{}); dt=datetime.now(timezone.utc)
    if state.get('collector_rev')!=PARSER_REV:
        state['collector_rev']=PARSER_REV
    for seed in SEEDS: state['discovered'].setdefault(seed,None)
    prior=read(OLD); until=max([x for x in [state.get('rate_limit_until'),prior.get('rate_limit_until')] if x] or [''])
    if until and p.parse_iso_dt(until) and p.parse_iso_dt(until)>dt:
        state['rate_limit_until']=until; state['errors']=['COOLDOWN_SKIP: '+until]; print(state['errors'][0]); state['run_stats']={'query_calls':0,'parse_calls':0,'attempted_pages':0}; merge_archive(state,prior); update_lifecycle(state,dt); save(OUT,state); return
    contact=os.environ.get('LIQUIPEDIA_CONTACT','').strip()
    if not contact: raise SystemExit('LIQUIPEDIA_CONTACT secret required')
    for seed in SEEDS: state['discovered'].setdefault(seed,None)
    client=p.ApiClient(contact); budget=max(1,min(8,int(os.getenv('OWCS_PARSE_BUDGET','6')))); attempted=0
    try:
        # Discover before planning so new phase pages reopen a previously archived event.
        years=list(range(2026,max(2026,dt.year)+2))
        due=[y for y in years if dt_age(state['discovery_dates'].get(str(y)),dt)>=timedelta(days=30 if y<dt.year else 1)]
        if due:
            y=min(due,key=lambda y:state['discovery_dates'].get(str(y),''))
            try:
                found=discover(client,y); state['discovered'].update(found); state['discovery_dates'][str(y)]=now()
                absent=state.setdefault('absent_pages',{})
                for t in found: absent.pop(t,None)
                for t in SEEDS:
                    if t.split('/')[1]==str(y) and t not in found and t not in state['pages'] and not state['discovered'].get(t): absent[t]=now()
                print('DISCOVERY',y,len(found))
            except p.RateLimitError: raise
            except Exception as e: state['errors'].append('Discovery '+str(y)+': '+str(e)[:300])
        merge_archive(state,prior); update_lifecycle(state,dt)
        try: schedule_scan(client,state,dt)
        except p.RateLimitError: raise
        except Exception as e: state['errors'].append('Schedule scan: '+str(e)[:300])
        index=update_lifecycle(state,dt)
        titles=known_pages(state)
        def priority(t): return target_priority(t,state,dt,index)
        targets=sorted(titles,key=priority,reverse=True)
        eligible=[t for t in targets if priority(t)[0]>=0]
        print('QUEUE',len(eligible),'budget',budget,'archived',sum(x['collection']=='archived' for x in state['lifecycle'].values()),'next',eligible[:budget])
        for t in targets:
            if attempted>=budget: break
            if priority(t)[0]<0: continue
            attempted+=1
            state['page_attempts'][t]=now()
            try:
                h,w,rev=client.parse(t.replace(' ','_')); new=parse_page(t,h,w,rev); diagnostic(t,h,w,new); old=state['pages'].get(t)
                if old:
                    if old.get('matches'):
                        # preserve verified historical rows on a partial response; never silently wipe.
                        mm={m['id']:m for m in old['matches'] if m.get('finished')}
                        for m in new['matches']:
                            prior_m=mm.get(m['id'])
                            if prior_m and prior_m.get('finished') and not m.get('finished'):
                                new['warnings'].append('Prior matches retained; verify removed/rescheduled entries'); continue
                            if prior_m and len(prior_m.get('maps',[]))>len(m.get('maps',[])) and (prior_m['score_a'],prior_m['score_b'])==((m['score_a'],m['score_b']) if prior_m['team_a']==m['team_a'] else (m['score_b'],m['score_a'])):
                                m['maps']=copy.deepcopy(prior_m['maps'])
                                if prior_m['team_a']!=m['team_a']:
                                    for mp in m['maps']:
                                        for x,y in [('score_a','score_b'),('ban_a','ban_b'),('received_a','received_b')]: mp[x],mp[y]=mp.get(y,''),mp.get(x,'')
                                new['warnings'].append('Prior map detail retained')
                            mm[m['id']]=m
                        if len(mm)>len(new['matches']): new['warnings'].append('Prior matches retained; verify removed/rescheduled entries')
                        new['matches']=list(mm.values())
                    if old.get('rosters') and not new['rosters']: new['rosters']=old['rosters']; new['warnings'].append('Roster retained from prior fetch; not verified current')
                state['pages'][t]=new
                try: state.setdefault('schedule_hints',{})[t]=schedule_hint(t,w,rev,dt)
                except Exception as e: state['errors'].append('Schedule hint '+t+': '+str(e)[:200])
                state.setdefault('revision_dirty',{}).pop(t,None)
                state.setdefault('revision_checked',{})[t]=now()
                print('FETCH',t,len(new['matches']),len(new['rosters']),new['warnings'])
            except p.RateLimitError: raise
            except Exception as e: state['errors'].append(t+': '+str(e)[:300])
        state.pop('rate_limit_until',None)
        if attempted: state['rate_limit_streak']=0
    except p.RateLimitError as e:
        streak=min(3,int(state.get('rate_limit_streak',0))+1); state['rate_limit_streak']=streak
        seconds=max(21600*2**(streak-1),e.retry_after or 0)
        state['rate_limit_until']=(datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat(); state['errors'].append(str(e))
        print('RATE_LIMITED: stop; until',state['rate_limit_until'],'streak',streak)
    except Exception as e: state['errors'].append('Discovery: '+str(e)[:500])
    state['run_stats']={'query_calls':client.query_calls,'parse_calls':client.parse_calls,'attempted_pages':attempted}; merge_archive(state,prior); update_lifecycle(state,datetime.now(timezone.utc)); save(OUT,state); print(state['run_stats'],len(state['events']),len(state['matches']),state['errors'])
    for region in REGION_ORDER:
        es=[e for e in state['events'] if e['region']==region]; ms=[m for m in state['matches'] if m['event_id'] in {e['id'] for e in es}]; rs=[r for r in state['rosters'] if r['event_id'] in {e['id'] for e in es}]
        print('COVERAGE',region,'events',len(es),'fetched',sum(bool(e['fetched_at']) for e in es),'series',sum(bool(m.get('finished')) for m in ms),'maps',sum(len(m.get('maps',[])) for m in ms if m.get('finished')),'roster_rows',len(rs))

if __name__=='__main__': main()
