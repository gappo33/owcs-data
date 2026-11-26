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
state={'pages':{},'page_attempts':{}}
now=datetime(2026,11,26,tzinfo=timezone.utc)
targets=sorted(c.SEEDS,key=lambda x:c.target_priority(x,state,now),reverse=True)
assert {c.region_for(x) for x in targets[:4]}=={'CHINA','NA'}
for title in targets[:4]:
    state['pages'][title]={'parser_rev':c.PARSER_REV,'fetched_at':now.isoformat(),'info':{'edate':'2026-11-08'}}
    state['page_attempts'][title]=now.isoformat()
next_targets=sorted(c.SEEDS,key=lambda x:c.target_priority(x,state,now+timedelta(days=1)),reverse=True)
assert 'EMEA' in {c.region_for(x) for x in next_targets[:4]}
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
