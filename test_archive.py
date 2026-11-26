"""Offline fixtures only. These are synthetic examples, not real match statistics."""
import copy, tempfile
from datetime import datetime, timezone, timedelta
import collect_all as c

def game(a,b,sa,sb,date):
    return ('{{Match|opponent1={{TeamOpponent|'+a+'|score='+str(sa)+'}}|opponent2={{TeamOpponent|'+b+'|score='+str(sb)+'}}|date='+date+' {{Abbr/CST}}|finished=true|map1={{Map|map=Busan|winner=1|t1b=ana|t2b=kiriko}}|map2={{Map|map=Suravasa|winner=2|t1b=bastion|t2b=tracer}}}}')
def card(t, names):
    roles=['tank','dps','dps','support','support']
    return '{{Opponent|'+t+'|players={{Persons|'+ '|'.join('{{Person|'+x+'|role='+r+'}}' for x,r in zip(names,roles))+'}}}}'
base=c.ROOT+'China/Stage 3'; overview='== Participants ==\n{{TeamParticipants|'+card('Weibo Gaming',['A','B','C','D','E'])+'|'+card('All Gamers',['F','G','H','I','J'])+'|'+card('Black Flag',['K'])+'}}\n== Regional Playoffs ==\n'+game('WBG','AG',3,1,'2026-10-18 18:00')
regular='== Swiss Stage ==\n'+game('WBG','AG',3,2,'2026-10-03 18:00')+'\n== Round Robin ==\n'+game('Weibo Gaming','All Gamers',3,0,'2026-10-11 18:00')+'\n'+game('WBG','AG',3,1,'2026-10-18 18:00')
h='<span class="team-template-text"><a title="Weibo Gaming" href="/overwatch/Weibo_Gaming">WBG</a></span>'
r1=c.parse_page(base,h,overview,123); r2=c.parse_page(base+'/Regular Season',h,regular,124)
assert len(r1['rosters'])==10 and {x['team'] for x in r1['rosters']}=={'Weibo Gaming','All Gamers'}
assert any('Black Flag' in w for w in r1['warnings'])
state={'pages':{base:r1,base+'/Regular Season':r2},'discovered':{},'errors':[]}
c.merge_archive(state)
assert len(state['matches'])==3 and len(state['rosters'])==10
assert all(x['team_a']=='Weibo Gaming' and x['team_b']=='All Gamers' for x in state['matches'])
assert all(x['maps'][0]['ban_a']=='ana' and x['maps'][1]['picker']=='' for x in state['matches'])
assert state['events'][0]['coverage']['series']==3
assert c.event_for(base+'/Regular Season/Swiss Stage')==base
for region,path in [('NA','NA/Stage 3'),('EMEA','EMEA/Stage 3'),('PACIFIC','Asia/Stage 3/Pacific')]:
    title=c.ROOT+path
    assert c.region_for(title)==region
    r=c.parse_page(title,'',game('Test A','Test B',3,2,'2026-10-12 19:00'),1)
    assert len(r['matches'])==1 and r['matches'][0]['maps'][1]['picker']==''
assert c.candidate(c.PREFIX+'2027/Asia/Stage 1/Japan')
assert not c.candidate(c.PREFIX+'2025/Asia/Stage 3/Japan')
assert c.alias_name('AG','EMEA',{})=='AG' # no cross-region abbreviation leak
# v1.7: no region priority, no date guess from title, explicit result order.
now=datetime(2026,11,26,12,tzinfo=timezone.utc)
a=c.ROOT+'Asia/Stage 3/Japan'; b=c.ROOT+'World Finals'; z=c.ROOT+'EMEA/Stage 3'; future=c.PREFIX+'2027/NA/Stage 1'; unknown=c.ROOT+'NA/Stage 1'; phase=a+'/Playoffs'
state={'pages':{},'page_attempts':{},'discovered':{x:1 for x in (a,b,z,future,unknown,phase)},'schedule_hints':{}}
for t,d in [(a,'2026-11-26 18:00'),(b,'2026-11-25 19:00'),(z,'2026-10-01 19:00'),(future,'2027-02-01 19:00'),(phase,'2026-11-28 19:00')]:
    state['schedule_hints'][t]=c.schedule_hint(t,game('Team A','Team B',3,1,d),1,now)
idx=c.update_lifecycle(state,now)
assert [t for t in sorted(state['discovered'],key=lambda t:c.target_priority(t,state,now,idx),reverse=True) if c.target_priority(t,state,now,idx)[0]>0]==[a,b,z]
assert c.target_priority(future,state,now,idx)[0]<0 and c.target_priority(phase,state,now,idx)[0]<0 and c.target_priority(unknown,state,now,idx)[0]<0
state['page_attempts'][a]=now.isoformat(); assert c.target_priority(a,state,now,idx)[0]<0
# Same-day start with explicit unfinished score is not a verified finished result.
assert not c.schedule_hint(a,game('A','B',1,0,'2026-11-26 18:00').replace('finished=true','finished=false'),1,now)['completed']
assert not c.p.parse_matches_wikitext(game('A','B',1,0,'2026-11-26 18:00').replace('|finished=true',''),'JP','fixture')[0]['finished']
print('ARCHIVE_SELF_TEST_OK: roster boundaries, incomplete rosters, alias isolation, repeated opponents, phase dedup, map BAN, region detection, future discovery, scheduling')

# Regression: screenshot-style duplicate DOM labels, one record with detailed maps
# and one series-only record. Both refer to the same match, not two series.
na=c.ROOT+'NA/Stage 3'
raw=game('Dallas Fuel','Disguised',2,1,'2026-10-03 18:00')
clean=c.parse_page(na,'',raw,1)
assert len(clean['matches'])==1
m=copy.deepcopy(clean['matches'][0]); m['team_a']='DAL Dallas Fuel Dallas Fuel'; m['team_b']='DSG Disguised Disguised'; m['maps']=[]
# Old cache uses different IDs; archive rebuild must normalize BEFORE computing IDs.
old=copy.deepcopy(clean); old['matches']=[m]; old['fetched_at']='2026-11-27T02:00:00+00:00'
# A true rematch at a different time must survive.
rematch=copy.deepcopy(m); rematch['date_jst']='2026-10-04T19:00:00+09:00'
old['matches'].append(rematch)
# Unknown composite of a different team, without a hard-coded alias.
kafe=c.parse_page(na,'',game('The Kafe','NTMR',3,1,'2026-10-05 19:00'),2)
km=copy.deepcopy(kafe['matches'][0]); km['team_a']='KAFE The Kafe The Kafe'; km['team_b']='NTMR NTMR NTMR'; km['maps']=[]
old['matches'].append(km); clean['matches']+=kafe['matches']
# Do not combine a genuinely distinct name with an arbitrary substring.
unrelated=copy.deepcopy(m); unrelated['team_a']='Dallas Fuel Academy'; unrelated['date_jst']='2026-10-06T19:00:00+09:00'; old['matches'].append(unrelated)
clean['rosters']=[{'team':'Dallas Fuel','player':'Kellan','role':'Tank'}]
china=c.parse_page(base,'',game('HUNENG Gaming','SHENGSHI Esports',2,1,'2026-10-07 19:00'),3)
cm=copy.deepcopy(china['matches'][0]); cm['team_a']='HNG HUNENG HUNENG Gaming'; cm['team_b']='SSE SHENGSHI SHENGSHI Esports'; cm['maps']=[]
cm2=copy.deepcopy(cm);cm2['team_a']='huneng Gaming';cm2['team_b']='shengshi esports'
china['matches'] += [cm,cm2]
state={'schema_version':4,'pages':{na:clean,na+'/Regular Season':old,base:china},'discovered':{},'errors':[],'rate_limit_until':'2099-01-01T00:00:00+00:00'}
legacy={'regions':{'CHINA':{'finished_matches':[{'team_a':'Huneng Gaming','team_b':'SHENGSHI Esports'}]},'NA':{'finished_matches':[{'team_a':'DAL Dallas Fuel Dallas Fuel','team_b':'Disguised'}]}}}
c.merge_archive(state,legacy)
assert len(state['matches'])==5, [(x['team_a'],x['team_b'],x['date_jst']) for x in state['matches']]
assert len([m for m in state['matches'] if m['team_a']=='Dallas Fuel'])==2
assert len([m for m in state['matches'] if m['team_a']=='Dallas Fuel' and m['maps']])==1
assert any(x['team_a']=='Dallas Fuel Academy' for x in state['matches'])
assert any(x['team_a']=='The Kafe' and x['team_b']=='NTMR' for x in state['matches'])
assert len([m for m in state['matches'] if m['event_id']==base])==1
assert state['team_aliases'][base]['hunenggaming']=='HUNENG Gaming'
assert state['rosters'][0]['team']=='Dallas Fuel'
# HTML parsing uses a verified link target rather than concatenated visible text.
from bs4 import BeautifulSoup
node=BeautifulSoup('<div class="match-info-header-opponent"><span class="name">DAL <a title="Dallas Fuel" href="/overwatch/Dallas_Fuel">Dallas Fuel</a> Dallas Fuel</span></div>','html.parser').div
assert c.p.popup_team_name(node,'NA')=='Dallas Fuel'
# No API or contact secret needed for repair, and timestamps/cooldown are unchanged.
import os,sys,json
from pathlib import Path
with tempfile.TemporaryDirectory() as d:
    orig=(c.OUT,c.OLD,sys.argv,c.p.ApiClient)
    try:
        c.OUT=Path(d)/'owcs_all.json';c.OLD=Path(d)/'owcs.json';before=copy.deepcopy(state); before['matches']+=old['matches'][:1]+[cm,cm2]; c.save(c.OUT,before);c.save(c.OLD,legacy)
        sys.argv=['collect_all.py','--repair-only']
        def fail(*a,**kw): raise AssertionError('Offline repair made an API client')
        c.p.ApiClient=fail
        c.main(); repaired=c.read(c.OUT)
        assert repaired['pages']==state['pages'] and repaired['rate_limit_until']==state['rate_limit_until']
        assert repaired['matches']==state['matches']
    finally: c.OUT,c.OLD,sys.argv,c.p.ApiClient=orig
print('NAME_REPAIR_SELF_TEST_OK: compound labels, case variants, rich match preservation, distinct teams, rematch, legacy aliases, offline repair')

# Lifecycle: independently verify elapsed calendar, known pages and normalized content.
enddt=datetime(2026,11,27,12,tzinfo=timezone.utc)
eid=c.ROOT+'NA/Stage 3'
def full_event():
    match={'event_id':eid,'team_a':'Dallas Fuel','team_b':'Disguised','score_a':1,'score_b':1,'finished':True,'date_jst':'2026-11-01T19:00:00+09:00','maps':[{'map':'Busan','winner':'Dallas Fuel','ban_a':'ana','ban_b':'kiriko'},{'map':'Ilios','winner':'Disguised','ban_a':'tracer','ban_b':'bastion'}]}
    row={'event_id':eid,'title':eid,'region':'NA','info':{'sdate':'2026-10-01','edate':'2026-11-01','team_number':'2'},'fetched_at':'2026-11-12T00:00:00+00:00','parser_rev':c.PARSER_REV,'revision':10,'audit':{'unresolved_templates':0,'undated':0},'warnings':[]}
    return {'pages':{eid:row},'discovered':{eid:1},'discovery_dates':{'2026':'2026-11-20T00:00:00+00:00'},'page_attempts':{},'matches':[match],'rosters':[{'event_id':eid,'team':t,'role':'DPS','player':t+str(i)} for t in ('Dallas Fuel','Disguised') for i in range(5)]}
def check(s):
    c.update_lifecycle(s,enddt); return s['lifecycle'][eid]
s=full_event(); assert check(s)['collection']=='archived'; assert c.target_priority(eid,s,enddt)[0]<0
s['discovered'][eid+'/Playoffs']=2; assert check(s)['collection']=='partial'; assert c.target_priority(eid+'/Playoffs',s,enddt)[0]>0
s=full_event(); s['pages'][eid]['info']['edate']='2026-12-04'; assert check(s)['calendar']=='active' and check(s)['collection']=='partial'; assert c.target_priority(eid,s,enddt)[0]<0
s=full_event(); s['pages'][eid]['info'].pop('edate'); assert check(s)['collection']=='partial'
s=full_event(); s['pages'][eid]['fetched_at']='2026-11-02T00:00:00+00:00'; assert eid in check(s)['verify_pending']; assert c.target_priority(eid,s,enddt)[0]>0
s=full_event(); s['matches'][0]['maps'].pop(); assert check(s)['map_missing_series']==1; assert check(s)['collection']=='partial'
s=full_event(); s['matches'][0]['maps'][0]['ban_b']=''; assert check(s)['ban_missing_maps']==1 and check(s)['collection']=='partial'
s=full_event(); s['pages'][eid]['audit']['unresolved_templates']=1; assert check(s)['source_issues']==1 and check(s)['collection']=='partial'
s=full_event(); s['matches'][0]['finished']=False; assert check(s)['collection']=='partial'
s=full_event(); s['rosters']=[]; assert check(s)['roster_missing_teams']==2
s=full_event(); s['pages'][eid]['parser_rev']=2; assert check(s)['pending_pages']==[eid]
s=full_event(); s['discovery_dates']={}; assert check(s)['collection']=='partial'
s=full_event(); s['matches'][0]['maps']=[]; check(s)
class MockRevisions:
    def __init__(self): self.calls=[]
    def query_revisions(self,titles): self.calls+=titles; return {t:11 for t in titles}
client=MockRevisions(); c.revision_audit(client,s,enddt); assert client.calls==[eid] and s['revision_dirty'][eid]
assert c.target_priority(eid,s,enddt)[0]>0
s=full_event(); check(s); client=MockRevisions(); c.revision_audit(client,s,enddt); assert not client.calls
s=full_event(); s['discovered'][eid+'/Regular Season']=None; s['absent_pages']={eid+'/Regular Season':'2026-11-20'}; assert check(s)['collection']=='archived'
assert c.canonical('SSG Spacestation Spacestation Gaming','NA')=='Spacestation Gaming'
assert c.canonical('SSG Spacestation Spacestation Gaming','WORLD')=='Spacestation Gaming'
partial=game('Dallas Fuel','Disguised',1,0,'2026-10-03 18:00').replace('finished=true','finished=false')
assert not c.p.parse_matches_wikitext(partial,'NA','test')[0]['finished']
print('LIFECYCLE_SELF_TEST_OK: archived skip, incomplete backfill, active refresh, new pages reopen, date unknown, final validation, map/BAN/roster gaps, revision audit, absent seeds, SSG, explicit unfinished')

# A cooldown run may not construct a client; a rate limit in normal collection must stop after one call.
from unittest.mock import patch
with tempfile.TemporaryDirectory() as d:
    before={'schema_version':4,'pages':{},'discovered':{},'discovery_dates':{},'errors':[],'rate_limit_until':'2099-01-01T00:00:00+00:00'}
    out=Path(d)/'all.json'; old_path=Path(d)/'old.json'; c.save(out,before)
    with patch.object(c,'OUT',out),patch.object(c,'OLD',old_path),patch.object(c.p,'ApiClient',side_effect=AssertionError('cooldown accessed API')),patch.object(sys,'argv',['collect_all.py']):
        c.main(); after=c.read(out); assert after['rate_limit_until']==before['rate_limit_until'] and after['run_stats']['parse_calls']==0
    before.pop('rate_limit_until'); before['rate_limit_streak']=1; c.save(out,before)
    class Limited:
        def __init__(self,contact): self.query_calls=0; self.parse_calls=0
        def _request(self,*a,**kw): self.query_calls+=1; raise c.p.RateLimitError('HTTP 429', 1)
    with patch.object(c,'OUT',out),patch.object(c,'OLD',old_path),patch.object(c.p,'ApiClient',Limited),patch.object(sys,'argv',['collect_all.py']),patch.dict(os.environ,{'LIQUIPEDIA_CONTACT':'test@example.invalid'}):
        c.main(); after=c.read(out); assert after['run_stats']['query_calls']==1 and after['run_stats']['parse_calls']==0 and after['rate_limit_streak']==2
        assert c.p.parse_iso_dt(after['rate_limit_until'])-datetime.now(timezone.utc)>timedelta(hours=11,minutes=59)
print('RATE_LIMIT_SELF_TEST_OK: zero API in cooldown, immediate stop, persisted progressive backoff')

# Real scheduling interface exercised through a fake MediaWiki query response.
scan_dt=datetime(2026,11,27,2,tzinfo=timezone.utc)
recent=c.ROOT+'Asia/Stage 3/Japan'; prior=c.ROOT+'World Finals'; upcoming=c.PREFIX+'2027/NA/Stage 1'; ongoing=c.ROOT+'EMEA/Stage 3'
fixtures={recent:game('A','B',3,1,'2026-11-27 09:30'),prior:game('A','B',3,1,'2026-11-26 22:00'),upcoming:'{{Infobox league|sdate=2027-01-01|edate=2027-01-31}}'+game('A','B',0,0,'2027-01-01 10:00').replace('finished=true','finished=false'),ongoing:game('A','B',1,0,'2026-11-27 09:00').replace('finished=true','finished=false')}
class ScanMock:
    def __init__(self): self.content_calls=0; self.revision_calls=0
    def _request(self,params,is_parse):
        assert not is_parse; assert params['rvprop']=='ids|content'; self.content_calls+=1
        class Response:
            def json(self): return {'query':{'pages':[{'title':t,'revisions':[{'revid':42,'slots':{'main':{'content':fixtures[t]}}}]} for t in params['titles'].split('|')]}}
        return Response()
    def query_revisions(self,pages): self.revision_calls+=1; return {t:42 for t in pages}
s={'pages':{},'discovered':{t:1 for t in fixtures},'errors':[]}; client=ScanMock(); c.update_lifecycle(s,scan_dt); c.schedule_scan(client,s,scan_dt); index=c.update_lifecycle(s,scan_dt)
assert s['next_queue']==[recent,prior],s['next_queue']
assert s['lifecycle'][upcoming]['priority_reason']=='future'
assert s['lifecycle'][ongoing]['priority_reason']=='today_unconfirmed'
assert client.content_calls==1 and client.revision_calls==0
c.schedule_scan(client,s,scan_dt+timedelta(hours=12)); assert client.content_calls==1 and client.revision_calls==1
assert not any('content' in h or 'wiki' in h for h in s['schedule_hints'].values())
print('V17_SCHEDULE_SCAN_SELF_TEST_OK: cached revisions, recent JST dates, future and ongoing excluded, metadata only')

# v1.8: rendered final flag + Wikitext maps, reversed sides and ICT.
from unittest.mock import patch
pac=c.ROOT+'Asia/Stage 3/Pacific'
wiki=game('Test A','Test B',1,1,'2026-10-03 18:00').replace('Abbr/CST','Abbr/ICT').replace('|finished=true','')
assert c.p.parse_liquipedia_date('2026-10-03 18:00 {{Abbr/ICT}}','PACIFIC').isoformat()=='2026-10-03T20:00:00+09:00'
hrow=dict(team_a='Test B',team_b='Test A',score_a=1,score_b=1,finished=True,date_jst='2026-10-03T20:00:00+09:00')
with patch.object(c.p,'parse_matches',return_value=[hrow]):
    rr=c.parse_page(pac,'',wiki,1); assert len(rr['matches'])==1
    m=rr['matches'][0]; assert m['finished'] and m['team_a']=='Test A' and len(m['maps'])==2 and m['maps'][0]['ban_a']=='ana'
    rr=c.parse_page(pac,'',wiki.replace('|map1=','|finished=false|map1='),1)
    assert not rr['matches'][0]['finished'] and rr['audit']['unresolved_templates']>0
with patch.object(c.p,'parse_matches',return_value=[{**hrow,'finished':False}]):
    assert not c.parse_page(pac,'',wiki,1)['matches'][0]['finished']
with patch.object(c.p,'parse_matches',return_value=[{**hrow,'score_a':3}]):
    rr=c.parse_page(pac,'',wiki,1); assert rr['audit']['unresolved_templates']>0 and not rr['matches'][0].get('maps')
# Unique roster boundary; other team's adjacent card must not contribute.
def html_card(team,names):
    return '<div class="team-participant-card"><div class="team-participant-card__header"><div class="block-team"><span class="name"><a>'+team+'</a></span></div></div>'+''.join('<div class="team-participant-card__member"><div class="team-participant-card__member-role-left"><img alt="DPS"/></div><div class="team-participant-card__member-name"><span class="name">'+n+'</span></div></div>' for n in names)+'</div>'
html=html_card('Test A',['A1','A2','A3','A4','A5'])+html_card('Test B',['B1','B2','B3','B4','B5'])
rr=c.html_roster_card(html,'Test A','PACIFIC','fixture',{}); assert {r['player'] for r in rr}=={'A1','A2','A3','A4','A5'}
assert not c.html_roster_card(html+html,'Test A','PACIFIC','fixture',{})
# Cache repair respects prior data, exact revision, and cooldown; repeated repair is harmless.
old=c.parse_page(pac,'',wiki,1); old['parser_rev']=3
with patch.object(c.p,'parse_matches',return_value=[hrow]): new=c.parse_page(pac,'',wiki,1)
state={'pages':{pac:copy.deepcopy(old)},'rate_limit_until':'2099-01-01T00:00:00+00:00'}
bundle={'parser_rev':c.PARSER_REV,'pages':{pac:{'row':new,'old_fingerprint':c.page_fingerprint(old),'schedule_hint':{}}}}
assert c.restore_debug(state,bundle)==1 and state['pages'][pac]['fetched_at']==old['fetched_at']
assert c.restore_debug(state,bundle)==0 and state['rate_limit_until']=='2099-01-01T00:00:00+00:00'
state['pages'][pac]={**old,'revision':2}; assert c.restore_debug(state,bundle)==0
print('V18_MATCH_ROSTER_RESTORE_SELF_TEST_OK')
