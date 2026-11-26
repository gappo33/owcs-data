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
