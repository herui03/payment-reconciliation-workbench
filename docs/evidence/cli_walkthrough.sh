#!/usr/bin/env bash
# Reproducible CLI walkthrough used to produce docs/evidence/cli_walkthrough.txt (run from repo root, fresh DB).
set -euo pipefail
DB=work/ev/walk.db
R="python -m recon --db $DB"
echo "== 1. import sample"; for s in ledger psp bank; do $R import $s data/sample/$s.csv | python -c "import json,sys;d=json.load(sys.stdin);print(d['source'],d['status'],d['message'])"; done
echo "== 2. duplicate / renamed / bad-schema / overlap files"
$R import ledger data/sample/ledger.csv | python -c "import json,sys;d=json.load(sys.stdin);print(d['status'],d['message'])"
$R import ledger data/sample/extra/ledger_renamed_copy.csv | python -c "import json,sys;d=json.load(sys.stdin);print(d['status'],d['message'])"
$R import bank data/sample/extra/bank_missing_amount_column.csv | python -c "import json,sys;d=json.load(sys.stdin);print(d['status'],d['message'])" || true
$R import ledger data/sample/extra/ledger_resend_overlap.csv | python -c "import json,sys;d=json.load(sys.stdin);print(d['status'],d['message'])"
echo "== 3. reconcile as-of 2026-07-10"
$R reconcile --as-of 2026-07-10 | python -c "import json,sys;d=json.load(sys.stdin);s=d['summary'];print('run',d['run_id'],'groups',s['counts']['groups'],'exceptions',s['counts']['exceptions'],'changes',s['case_changes'])"
$R cases --status Open
echo "== 4. analyst works cases"
PARTIAL=$($R cases | awk '/PARTIAL_RECEIPT/{print $1}')
MISSING=$($R cases | awk '/MISSING_IN_BANK/{print $1}')
UNEXP=$($R cases | awk '/UNEXPECTED_BANK_CREDIT/{print $1}')
$R --actor analyst.a case-assign $PARTIAL ops.analyst
$R --actor analyst.a case-note $PARTIAL "PSP confirms remainder 800.00 EUR sent in second transfer"
$R --actor analyst.a case-status $PARTIAL "In review"
$R --actor analyst.a case-status $UNEXP Resolved --reason "x" || echo "(expected refusal above: disposition + reason required)"
$R --actor analyst.a case-status $UNEXP Resolved --disposition NO_ACTION_REQUIRED --reason "Bank interest, booked by treasury"
$R report --out docs/evidence/sample_reports > /dev/null
echo "== 5. late bank file + re-run as-of 2026-07-11"
$R import bank data/sample/late/bank_late.csv | python -c "import json,sys;d=json.load(sys.stdin);print(d['status'],d['message'])"
$R reconcile --as-of 2026-07-11 | python -c "import json,sys;d=json.load(sys.stdin);s=d['summary'];print('run',d['run_id'],'groups',s['counts']['groups'],'exceptions',s['counts']['exceptions'],'changes',s['case_changes'])"
$R cases | grep -E "PARTIAL_RECEIPT|MISSING_IN_BANK|UNEXPECTED_BANK_CREDIT"
echo "== 6. re-run same data (idempotency)"
$R reconcile --as-of 2026-07-11 | python -c "import json,sys;d=json.load(sys.stdin);print('run',d['run_id'],'changes',d['summary']['case_changes'])"
$R report --out docs/evidence/sample_reports
echo "== 7. audit trail for the partial-receipt case"
python - "$DB" "$PARTIAL" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
for r in c.execute("SELECT ts, actor, action FROM audit_events WHERE entity_type='case' AND entity_id=? ORDER BY event_id", (sys.argv[2],)):
    print(*r)
try:
    c.execute("UPDATE audit_events SET actor='someone-else'")
except sqlite3.IntegrityError as e:
    print("UPDATE audit_events ->", e)
PY
