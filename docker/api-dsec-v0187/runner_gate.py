#!/usr/bin/env python3
"""Exact infrastructure-only maintenance gate. Run on the Daytona API host."""
import argparse, datetime, json, os, re, subprocess, time, urllib.request
from pathlib import Path
TARGETS = {"a2":"http://192.168.0.149:3003", "a3":"http://192.168.0.62:3003", "a4":"http://192.168.0.231:3003", "a5":"http://192.168.0.162:3003"}
STATE = Path("/opt/codex-dsec-20260929/runner-gates")
def sql(query):
    r = subprocess.run(["docker","exec","daytona-db-1","sh","-c",'exec psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-daytona}" -At -c "$1"',"_",query], capture_output=True, text=True)
    if r.returncode: raise RuntimeError("Infrastructure SQL failed; no credential or query result printed")
    return r.stdout.strip()
def lit(value):
    if value is None: return "NULL"
    if isinstance(value,bool): return "true" if value else "false"
    return "'" + str(value).replace("'", "''") + "'"
def get(name):
    value = sql('SELECT row_to_json(x) FROM (SELECT id, name, "apiUrl", unschedulable, draining, state, "lastChecked" FROM runner WHERE name='+lit(name)+' AND "apiUrl"='+lit(TARGETS[name])+') x')
    if not value or "\n" in value: raise RuntimeError("Runner identity is missing or ambiguous")
    return json.loads(value)
def write(path,value):
    STATE.mkdir(mode=0o700,exist_ok=True)
    temp=path.with_suffix(".tmp")
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,"w") as f:
        json.dump(value,f,indent=2);f.write("\n");f.flush();os.fsync(f.fileno())
    os.replace(temp,path)
def jobs(row):
    value=sql('SELECT json_agg(x) FROM (SELECT status::text, type::text, count(*) AS count FROM job WHERE "runnerId"='+lit(row["id"])+" AND status::text IN ('pending','in_progress','PENDING','IN_PROGRESS') GROUP BY status,type) x")
    return json.loads(value or "[]")
def cas(row,before,after):
    value=sql('WITH previous AS (SELECT id,unschedulable FROM runner WHERE id='+lit(row['id'])+'::uuid AND name='+lit(row['name'])+' AND "apiUrl"='+lit(row['apiUrl'])+' AND unschedulable IS NOT DISTINCT FROM '+lit(before)+' AND draining IS NOT DISTINCT FROM '+lit(row['draining'])+' FOR UPDATE), changed AS (UPDATE runner r SET unschedulable='+lit(after)+',"updatedAt"=NOW() FROM previous p WHERE r.id=p.id RETURNING r.id) SELECT count(*) FROM changed')
    if value!='1': raise RuntimeError("Maintenance CAS failed; gate state was not changed")
def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['status','gate','restore','wait-idle','ready','capabilities']);parser.add_argument('name',choices=TARGETS);parser.add_argument('--timeout',type=int,default=120);parser.add_argument('--after');a=parser.parse_args()
    row=get(a.name);path=STATE/(a.name+'.json')
    if a.action=='gate':
        if path.exists() and json.loads(path.read_text()).get('phase')!='restored': raise RuntimeError('Unfinished maintenance gate already exists; inspect or restore it first')
        if row['draining']: raise RuntimeError('Runner is already draining; refusing overlapping maintenance')
        record={'before':row,'phase':'intent','at':datetime.datetime.now(datetime.timezone.utc).isoformat()};write(path,record)
        cas(row,row['unschedulable'],True);record['phase']='gated';write(path,record)
        assert get(a.name)['unschedulable'] is True
        print(json.dumps({'name':a.name,'gated':True,'backup':str(path),'original':row['unschedulable']}));return
    if a.action=='restore':
        record=json.loads(path.read_text());before=record['before']
        if before['id']!=row['id'] or before['apiUrl']!=row['apiUrl']: raise RuntimeError('Runner identity changed; refusing restoration')
        if record['phase']=='restored': print(json.dumps({'name':a.name,'already_restored':True}));return
        cas(before,True,before['unschedulable']);record['phase']='restored';record['restored_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();write(path,record)
        print(json.dumps({'name':a.name,'restored':get(a.name)['unschedulable']}));return
    if a.action=='capabilities':
        token=sql('SELECT "apiKey" FROM runner WHERE id='+lit(row['id'])+'::uuid')
        req=urllib.request.Request('http://127.0.0.1:3000/api/jobs/admission/capabilities',headers={'Authorization':'Bearer '+token})
        with urllib.request.urlopen(req,timeout=10) as response: result=json.load(response)
        if result.get('version')!=1 or result.get('recoveryRenewal') is not True: raise RuntimeError('Unexpected admission capability response')
        print(json.dumps({'name':a.name,'capabilities':result}));return
    deadline=time.monotonic()+a.timeout
    while True:
        row=get(a.name);pending=jobs(row)
        ready=row['state']=='ready'
        if a.action=='ready' and path.exists():
            record=json.loads(path.read_text());checked=datetime.datetime.fromisoformat(row['lastChecked'].replace('Z','+00:00'))
            if checked.tzinfo is None: checked=checked.replace(tzinfo=datetime.timezone.utc)
            ready=ready and checked>=datetime.datetime.fromisoformat(record['at'])
        if a.action=='ready' and a.after:
            threshold=datetime.datetime.fromisoformat(re.sub(r'(\.\d{6})\d+',r'\1',a.after).replace('Z','+00:00'))
            if threshold.tzinfo is None: threshold=threshold.replace(tzinfo=datetime.timezone.utc)
            checked=datetime.datetime.fromisoformat(row['lastChecked'].replace('Z','+00:00'))
            if checked.tzinfo is None: checked=checked.replace(tzinfo=datetime.timezone.utc)
            ready=ready and checked>=threshold
        if a.action=='status' or (a.action=='wait-idle' and not pending) or (a.action=='ready' and ready):
            print(json.dumps({'runner':row,'active_jobs':pending}));return
        if time.monotonic()>=deadline: raise RuntimeError('Timed out waiting for '+a.action)
        time.sleep(2)
if __name__=='__main__':main()
