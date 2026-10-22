#!/bin/bash
# Live demo against the real backend. Usage: ./demo.sh delay | rule | ask
API=${API:-http://localhost:8000}
PY=.venv/bin/python
CID=$(curl -s $API/cases | $PY -c "import sys,json;print([c for c in json.load(sys.stdin)['cases'] if c['employee_name']=='Ana Torres'][0]['id'])")

show_plan() {
  $PY -c "
import sys,json;d=json.load(sys.stdin)
for r in d.get('replans') or d.get('results') or []:
    print('\n\033[1mPlanner (live)\033[0m  risk:', r.get('risk'))
    n=r.get('next_best_action') or {}
    print('\033[1mNext action:\033[0m', n.get('title'), '- unblocks', n.get('unblocks'))
    print('\033[1mWhy:\033[0m', r.get('explanation'))
    for a in r.get('this_week') or []: print('  this week:', a)
    print('\033[1mReminder to Ana:\033[0m', r.get('reminder'))
if 'ingest' in d:
    i=d['ingest']; print('\n\033[1mPipeline (live)\033[0m hash changed:', i.get('changed'), '| rules:', i.get('rules'),
      '| verifier passed', i.get('passed'), '/', i.get('checked'), '| outcomes:', i.get('outcomes'))
"
}

case "$1" in
  delay)
    echo "▶ Event: Ana's prefecture appointment delayed 14 days"
    curl -s -X POST $API/cases/$CID/delay -H 'content-type: application/json' \
      -d '{"rule_key":"residence_permit","days":14}' | show_plan ;;
  rule)
    echo "▶ Official page changed: service-public.fr F33453 (Visale), age limit edited in the fetched snapshot"
    echo "  fetch → hash → Extractor → Verifier → Resolver → rule.changed → Watcher → Planner"
    curl -s -X POST $API/simulate/rule-change -H 'content-type: application/json' \
      -d '{"source_url":"https://www.service-public.fr/particuliers/vosdroits/F33453","find":"moins de 30 ans","replace":"moins de 26 ans"}' | show_plan ;;
  restore)
    echo "▶ Restoring the real official text (under 30)"
    curl -s -X POST $API/simulate/rule-change -H 'content-type: application/json' \
      -d '{"source_url":"https://www.service-public.fr/particuliers/vosdroits/F33453","find":"moins de 26 ans","replace":"moins de 30 ans"}' | show_plan ;;
  ask)
    Q=${2:-"I'm 28 on a permanent contract, can I use Visale for a 1,500 € flat in Paris?"}
    echo "▶ Ana asks: $Q"
    curl -s -X POST $API/cases/$CID/ask -H 'content-type: application/json' -d "{\"question\":\"$Q\"}" | $PY -c "
import sys,json;d=json.load(sys.stdin);print('\n'+d['answer']);print()
for c in d['citations']: print('  source:', c['source_url'], '| verified', c.get('last_verified','')[:10])" ;;
  *) echo "usage: ./demo.sh delay | rule | restore | ask [question]" ;;
esac
