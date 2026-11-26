"""Incremental OWCS archive, independent of legacy data/owcs.json."""
import os, re, json, hashlib, copy
from pathlib import Path
from datetime import datetime, timezone, timedelta
import owcs_parsers as p
from bs4 import BeautifulSoup
from collections import defaultdict
from urllib.parse import unquote

PARSER_REV = 2
REGION_ORDER = ["CHINA", "NA", "EMEA", "WORLD", "ASIA", "PACIFIC", "JP", "KR"]
# Explicit aliases confirmed on the China event pages. No ambiguous global abbreviations.
CHINA_ALIASES = {"wbg":"Weibo Gaming", "weibogamingwbg":"Weibo Gaming", "ag":"All Gamers", "allgamersag":"All Gamers", "jdg":"JD Gaming", "jdgamingjdg":"JD Gaming", "oug":"OU Gaming", "ougamingoug":"OU Gaming", "sv":"Solus Victorem", "solusvictoremsv":"Solus Victorem", "hng":"HUNENG Gaming", "hunenggaminghng":"HUNENG Gaming", "bf":"Black Flag", "ssf":"SHENGSHI Esports"}
p.TEAM_ALIASES.setdefault("CHINA", {}).update(CHINA_ALIASES)

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
    return p.canonical_team(region,name)

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
    for raw in raw_html+raw_wiki:
        if not raw.get('date_jst'): undated+=1; continue
        m=normalize_match(raw,event,region,aliases)
        if not m: continue
        prev=rows.get(m['id'])
        if not prev or (bool(m.get('finished')),len(m.get('maps',[]))) >= (bool(prev.get('finished')),len(prev.get('maps',[]))): rows[m['id']]=m
    # Never use standings (especially cumulative Swiss snapshots) as independent matches.
    if undated: warnings.append(f'{undated} undated match candidates excluded; verify source')
    if ('{{match' in wiki.lower() or 'brkts-match-info-popup' in html) and not rows: warnings.append('Match templates present but no dated matches parsed')
    if title==event and not rosters: warnings.append('No validated event roster; check Participants source')
    finished=[m for m in rows.values() if m.get('finished')]
    no_maps=sum(not m.get('maps') for m in finished)
    incomplete=sum(sum(x.get('winner') in (m['team_a'],m['team_b']) for x in m.get('maps',[])) != m['score_a']+m['score_b'] for m in finished)
    if incomplete: warnings.append(f'{incomplete}/{len(finished)} finished series have incomplete map detail (score comparison)')
    if no_maps: warnings.append(f'{no_maps}/{len(finished)} finished series have no detailed maps')
    if not rows and title!=event: warnings.append('No dated matches on phase page')
    return {'title':title,'event_id':event,'region':region,'source':src,'revision':rev,'fetched_at':now(),'parser_rev':PARSER_REV,'info':info,'matches':list(rows.values()),'rosters':rosters,'team_hints':hints,'aliases':aliases,'warnings':list(dict.fromkeys(warnings))}

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

def merge_archive(state):
    events={}; matches={}; rosters={}; alias_sets=defaultdict(lambda:defaultdict(set))
    for title,row in state['pages'].items():
        for k,v in row.get('aliases',{}).items(): alias_sets[row['event_id']][k].add(v)
    event_aliases={e:{k:next(iter(v)) for k,v in a.items() if len(v)==1} for e,a in alias_sets.items()}
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

def target_priority(t,state,dt):
    row=state['pages'].get(t,{}); last=p.parse_iso_dt(row.get('fetched_at')); prev=p.parse_iso_dt(state['page_attempts'].get(t)); age=(dt-last).total_seconds()/86400 if last else 10000
    if prev and dt-prev<timedelta(hours=20): return (-1,0,0,'')
    rev=row.get('parser_rev')!=PARSER_REV
    if t in SEEDS and (not last or rev) and not prev: tier=100
    elif t in SEEDS and (not last or rev): tier=90
    elif not last: tier=70 if not prev else 40
    elif rev: tier=65
    else:
        end=row.get('info',{}).get('edate','') or state['pages'].get(event_for(t),{}).get('info',{}).get('edate',''); ended=bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}',end) and end<(dt-timedelta(days=7)).date().isoformat())
        if age < (30 if ended else 1): return (-1,0,0,'')
        # Avoid the old fixed bonus for Asia/World that could starve other regions.
        tier=80 if t in SEEDS[:2] and not ended else 30
    region=region_for(t); order=REGION_ORDER.index(region) if region in REGION_ORDER else 99
    # Within stage 3, fetch the two phase/overview pages of each overseas region together.
    if tier>=90: return (tier,-order,int(t==event_for(t)),t)
    year=int(t.split('/')[1]); return (tier,year,age,t)

def main():
    state=read(OUT) or {'schema_version':4,'pages':{},'discovered':{},'discovery_dates':{},'errors':[]}
    # Store provenance and cooldown; don't hammer on failure.
    state['errors']=[]; state['attempted_at']=now(); state.setdefault('page_attempts',{}); dt=datetime.now(timezone.utc)
    if state.get('collector_rev')!=PARSER_REV:
        state['page_attempts']={}; state['collector_rev']=PARSER_REV
    for seed in SEEDS: state['discovered'].setdefault(seed,None)
    prior=read(OLD); until=max([x for x in [state.get('rate_limit_until'),prior.get('rate_limit_until')] if x] or [''])
    if until and p.parse_iso_dt(until) and p.parse_iso_dt(until)>dt:
        state['rate_limit_until']=until; state['errors']=['COOLDOWN_SKIP: '+until]; merge_archive(state); save(OUT,state); return
    contact=os.environ.get('LIQUIPEDIA_CONTACT','').strip()
    if not contact: raise SystemExit('LIQUIPEDIA_CONTACT secret required')
    for seed in SEEDS: state['discovered'].setdefault(seed,None)
    client=p.ApiClient(contact); budget=max(1,min(8,int(os.getenv('OWCS_PARSE_BUDGET','4')))); attempted=0
    try:
        titles=list(state['discovered'])
        def priority(t): return target_priority(t,state,dt)
        targets=sorted(titles,key=priority,reverse=True)
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
                            if prior_m and prior_m.get('finished') and not m.get('finished'): continue
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
                print('FETCH',t,len(new['matches']),len(new['rosters']),new['warnings'])
            except p.RateLimitError: raise
            except Exception as e: state['errors'].append(t+': '+str(e)[:300])
        # At most one year discovery per run, rotate oldest discovery; next year proactively discover.
        years=list(range(2026,max(2026,dt.year)+2)); due=sorted(years,key=lambda y:state['discovery_dates'].get(str(y),''))
        y=due[0]; last=p.parse_iso_dt(state['discovery_dates'].get(str(y)))
        if not last or dt-last>=timedelta(days=1):
            try:
                state['discovered'].update(discover(client,y)); state['discovery_dates'][str(y)]=now()
            except p.RateLimitError: raise
            except Exception as e: state['errors'].append('Discovery '+str(y)+': '+str(e)[:300])
        for seed in SEEDS: state['discovered'].setdefault(seed,None)
        state.pop('rate_limit_until',None)
    except p.RateLimitError as e:
        state['rate_limit_until']=(datetime.now(timezone.utc)+timedelta(seconds=max(21600,e.retry_after or 0))).isoformat(); state['errors'].append(str(e))
    except Exception as e: state['errors'].append('Discovery: '+str(e)[:500])
    state['run_stats']={'query_calls':client.query_calls,'parse_calls':client.parse_calls,'attempted_pages':attempted}; merge_archive(state); save(OUT,state); print(state['run_stats'],len(state['events']),len(state['matches']),state['errors'])
    for region in REGION_ORDER:
        es=[e for e in state['events'] if e['region']==region]; ms=[m for m in state['matches'] if m['event_id'] in {e['id'] for e in es}]; rs=[r for r in state['rosters'] if r['event_id'] in {e['id'] for e in es}]
        print('COVERAGE',region,'events',len(es),'fetched',sum(bool(e['fetched_at']) for e in es),'series',sum(bool(m.get('finished')) for m in ms),'maps',sum(len(m.get('maps',[])) for m in ms if m.get('finished')),'roster_rows',len(rs))

if __name__=='__main__': main()
