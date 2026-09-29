#!/usr/bin/env python3
"""Root-only DELETE lease and verified Runner runtime reporting.
Lease actions never change sandbox rows or consume lifecycle jobs.
report-verified-running uses the existing authenticated Runner state-report endpoint.
Enter is a 300-second preparation lease; latch before stopping any inner container.
A latched lease never expires: release only after candidate/rollback health and ID checks.
"""
import argparse, datetime, hashlib, json, os, subprocess, uuid, urllib.request, urllib.error
from pathlib import Path
TARGETS = {"a2":"http://192.168.0.149:3003", "a3":"http://192.168.0.62:3003", "a4":"http://192.168.0.231:3003", "a5":"http://192.168.0.162:3003"}
PREFIX = 'runner:maintenance:delete:'
def lit(value):
    return "'"+str(value).replace("'","''")+"'"
def sql(query):
    p=subprocess.run(['docker','exec','daytona-db-1','sh','-c','exec psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-daytona}" -At -c "$1"','_',query],capture_output=True,text=True,timeout=20)
    if p.returncode: raise RuntimeError('Maintenance SQL failed (details withheld)')
    return p.stdout.strip()
def runner(name):
    value=sql('SELECT row_to_json(x) FROM (SELECT id,name,"apiUrl",unschedulable,draining FROM runner WHERE name='+lit(name)+' AND "apiUrl"='+lit(TARGETS[name])+') x')
    if not value or '\n' in value: raise RuntimeError('Runner identity missing or ambiguous')
    return json.loads(value)
def write(path, record):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tmp')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    os.fchmod(fd,0o600)
    with os.fdopen(fd,'w') as out:
        json.dump(record,out,indent=2);out.write('\n');out.flush();os.fsync(out.fileno())
    os.replace(tmp,path)
def read_record(path,name):
    if path.stat().st_mode & 0o077: raise RuntimeError('Lease state must have mode 0600')
    record=json.loads(path.read_text())
    current=runner(name)
    if record['name']!=name or record['runnerId']!=current['id'] or record['apiUrl']!=current['apiUrl']: raise RuntimeError('Runner identity changed')
    return record
SCRIPTS={
 'enter': "if redis.call('SET',KEYS[1],ARGV[1],'NX','PX',ARGV[2]) then return 1 else return 0 end",
 'status': "return {redis.call('PTTL',KEYS[1]),redis.call('GET',KEYS[1])==ARGV[1] and 1 or 0}",
 'renew': "if redis.call('GET',KEYS[1])~=ARGV[1] then return 0 end; if redis.call('PTTL',KEYS[1])==-1 then return 1 end; return redis.call('PEXPIRE',KEYS[1],ARGV[2])",
 'latch': "if redis.call('GET',KEYS[1])~=ARGV[1] then return 0 end; redis.call('PERSIST',KEYS[1]); return 1",
 'release': "if redis.call('GET',KEYS[1])~=ARGV[1] then return 0 end; return redis.call('DEL',KEYS[1])",
}
NODE=r"""
const fs=require('node:fs'),Redis=require('ioredis');
const p=JSON.parse(fs.readFileSync(0,'utf8'));
const redis=new Redis({host:process.env.REDIS_HOST,port:Number(process.env.REDIS_PORT||6379),username:process.env.REDIS_USERNAME,password:process.env.REDIS_PASSWORD,tls:process.env.REDIS_TLS==='true'?{}:undefined,connectTimeout:5000,maxRetriesPerRequest:1});
redis.on('error',()=>{});
(async()=>{try{const result=await redis.eval(p.script,1,p.key,p.owner,String(p.ttl*1000));process.stdout.write(JSON.stringify(result));}catch{process.exitCode=1;}finally{redis.disconnect();}})();
"""
def redis(action,record):
    payload={'script':SCRIPTS[action],'key':PREFIX+record['runnerId'],'owner':record['owner'],'ttl':record['ttl']}
    p=subprocess.run(['docker','exec','-i','daytona-api-1','node','-e',NODE],input=json.dumps(payload),capture_output=True,text=True,timeout=15)
    if p.returncode: raise RuntimeError('Maintenance Redis operation failed (details withheld)')
    return json.loads(p.stdout)
def barrier(record):
    ids=record['protectedIds']
    if not ids:return
    # Row locks wait for pre-lease DELETE transactions. A later DELETE rechecks its lease under the same lock.
    query='BEGIN; SET LOCAL lock_timeout=\'10s\'; SELECT COALESCE(json_agg(x),\'[]\'::json) FROM (SELECT id,"runnerId","desiredState" FROM sandbox WHERE id IN ('+','.join(lit(i) for i in ids)+') ORDER BY id FOR UPDATE) x; COMMIT;'
    output=sql(query)
    start=output.find('[')
    if start<0: raise RuntimeError('Maintenance barrier returned no row snapshot')
    rows,end=json.JSONDecoder().raw_decode(output[start:])
    if output[start+end:].strip() not in ('', 'COMMIT'): raise RuntimeError('Unexpected barrier transaction output')
    if {r['id'] for r in rows}!=set(ids) or any(r['runnerId']!=record['runnerId'] or r['desiredState']!='started' for r in rows):
        raise RuntimeError('A protected sandbox changed runner/desired state; abort maintenance before stopping')
def summary(record):
    remaining,owned=redis('status',record)
    return {'name':record['name'],'runnerId':record['runnerId'],'owned':bool(owned),'remainingMs':remaining,'latched':remaining==-1,'protectedIds':record['protectedIds'],'ownerHash':hashlib.sha256(record['owner'].encode()).hexdigest()[:16]}

def validate_running_proof(record, proof):
    ids=set(record['protectedIds'])
    if proof.get('name')!=record['name'] or proof.get('runnerId')!=record['runnerId'] or set(proof.get('protectedIds',[]))!=ids:
        raise RuntimeError('Physical proof identity mismatch')
    node,before=proof['node'],proof['before']
    age=(datetime.datetime.now(datetime.timezone.utc)-datetime.datetime.fromisoformat(node['at'])).total_seconds()
    if not 0<=age<=180: raise RuntimeError('Physical daemon proof must be no older than 180 seconds')
    for field in ('all_ids','running_ids','running_sandbox_ids'):
        if len(node[field])!=len(set(node[field])) or set(node[field])!=set(before[field]):
            raise RuntimeError('Physical container identity set changed')
    if set(node['running_sandbox_ids'])!=ids or node['sandbox_docker_ids']!=before['sandbox_docker_ids'] or set(node['sandbox_docker_ids'])!=ids:
        raise RuntimeError('Protected sandbox/container mapping changed')
    if set(node['sandbox_docker_ids'].values())!=set(node['running_ids']) or len(node['sandbox_docker_ids'])!=len(node['running_ids']):
        raise RuntimeError('Physical container mapping is not one-to-one')
    if sorted(node['volumes'],key=repr)!=sorted(before['volumes'],key=repr):
        raise RuntimeError('Runner Docker data mounts changed')
    if node.get('paused') or node.get('health')!='healthy' or node.get('http_status')!=200 or set(node['daemon_versions'])!=set(node['running_ids']) or any(not v for v in node['daemon_versions'].values()):
        raise RuntimeError('Runner and every protected daemon must be healthy')

def report_rows(record):
    ids=','.join(lit(i) for i in record['protectedIds']) or 'NULL'
    return json.loads(sql('SELECT COALESCE(json_agg(x),\'[]\'::json) FROM (SELECT id,"runnerId",state,"desiredState",pending FROM sandbox WHERE id IN ('+ids+') ORDER BY id) x'))

def assert_report_preconditions(record, rows):
    if redis('status',record)!=[-1,1]: raise RuntimeError('Verified report requires the owned persistent DELETE lease')
    if {r['id'] for r in rows}!=set(record['protectedIds']) or any(r['runnerId']!=record['runnerId'] or r['pending'] or (r['state'],r['desiredState']) not in (('started','started'),('stopped','stopped')) for r in rows):
        raise RuntimeError('Sandbox has a pending or conflicting control-plane intent')
    ids=','.join(lit(i) for i in record['protectedIds']) or 'NULL'
    since=lit(record['createdAt'])
    audit=int(sql('SELECT count(*) FROM audit_log WHERE "targetType"=\'sandbox\' AND "targetId" IN ('+ids+') AND "createdAt">='+since+' AND "statusCode">=200 AND "statusCode"<300 AND action IN (\'stop\',\'archive\',\'delete\',\'start\',\'recover\')'))
    jobs=int(sql('SELECT count(*) FROM job WHERE "resourceId" IN ('+ids+') AND "createdAt">='+since))
    if audit or jobs: raise RuntimeError('Lifecycle request/job appeared during maintenance; do not overwrite its intent')

def send_running_report(token, sandbox_id):
    request=urllib.request.Request('http://127.0.0.1:3000/api/sandbox/'+sandbox_id+'/state',data=b'{"state":"started"}',method='PUT',headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(request,timeout=10) as response:
            if response.status!=200: raise RuntimeError('Runner state report did not return HTTP 200')
    except urllib.error.URLError:
        raise RuntimeError('Runner state report failed; retain maintenance protection') from None

def report_verified_running(record, proof, result_path):
    # This is the existing Runner observation endpoint, never POST/start (which can migrate an unschedulable node).
    validate_running_proof(record,proof)
    rows=report_rows(record);assert_report_preconditions(record,rows)
    token=sql('SELECT "apiKey" FROM runner WHERE id='+lit(record['runnerId']))
    if not token: raise RuntimeError('Runner reporting credential unavailable')
    result={'name':record['name'],'runnerId':record['runnerId'],'source':'verified physical runtime via official Runner PUT state','startedAt':datetime.datetime.now(datetime.timezone.utc).isoformat(),'before':rows,'reportedIds':[],'proofAt':proof['node']['at']}
    write(result_path,result)
    for original in rows:
        current=report_rows(record);assert_report_preconditions(record,current)
        validate_running_proof(record,proof)
        row=next(r for r in current if r['id']==original['id'])
        if row['state']=='started': continue
        send_running_report(token,row['id'])
        after=report_rows(record);assert_report_preconditions(record,after)
        updated=next(r for r in after if r['id']==row['id'])
        if (updated['state'],updated['desiredState'])!=('started','started'):
            raise RuntimeError('Runner report was not reflected; retain maintenance protection')
        if [r for r in current if r['id']!=row['id']]!=[r for r in after if r['id']!=row['id']]:
            raise RuntimeError('Another sandbox changed during reporting; retain maintenance protection')
        result['reportedIds'].append(row['id']);write(result_path,result)
    after=report_rows(record);assert_report_preconditions(record,after)
    if any(r['state']!='started' for r in after): raise RuntimeError('Not every protected sandbox is started')
    result.update(after=after,completedAt=datetime.datetime.now(datetime.timezone.utc).isoformat());write(result_path,result)
    return {'name':record['name'],'runnerId':record['runnerId'],'protected':len(rows),'reported':len(result['reportedIds']),'sameRunner':True,'lease':'owned+latched','evidence':str(result_path)}

def main():
    if os.geteuid()!=0: raise RuntimeError('Run as root on API host')
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['snapshot','enter','status','renew','latch','release','report-verified-running','check-rollback-intent']);p.add_argument('name',choices=TARGETS)
    p.add_argument('--proof-file',type=Path);p.add_argument('--state-file',type=Path);p.add_argument('--ids-file',type=Path);p.add_argument('--ttl',type=int,default=300)
    a=p.parse_args()
    if a.action=='snapshot':
        row=runner(a.name)
        sandbox_rows=json.loads(sql("SELECT COALESCE(json_agg(x),'[]'::json) FROM (SELECT id,state,\"desiredState\" FROM sandbox WHERE \"runnerId\"="+lit(row['id'])+" AND state::text NOT IN ('destroyed','DESTROYED') ORDER BY id) x"))
        pending=json.loads(sql("SELECT COALESCE(json_agg(x),'[]'::json) FROM (SELECT type,status,count(*) AS count FROM job WHERE \"runnerId\"="+lit(row['id'])+" AND status::text IN ('PENDING','IN_PROGRESS','pending','in_progress') GROUP BY type,status) x"))
        print(json.dumps({'name':a.name,'runnerId':row['id'],'sandboxes':sandbox_rows,'protectedIds':[x['id'] for x in sandbox_rows if x['state']=='started' and x['desiredState']=='started'],'activeJobs':pending}));return
    if not a.state_file or not a.state_file.is_absolute(): raise RuntimeError('Use an absolute private state path')
    if a.action=='check-rollback-intent':
        record=read_record(a.state_file,a.name)
        rows=report_rows(record);assert_report_preconditions(record,rows)
        print(json.dumps({'name':a.name,'runnerId':record['runnerId'],'allowed':True,'protected':len(rows),'lease':'owned+latched'}));return
    if a.action=='report-verified-running':
        if not a.proof_file or not a.proof_file.is_absolute() or a.proof_file.stat().st_mode & 0o077 or a.proof_file.stat().st_uid!=0: raise RuntimeError('Use a root-owned private physical proof file')
        record=read_record(a.state_file,a.name)
        result_path=a.proof_file.with_name(a.proof_file.stem+'-report.json')
        print(json.dumps(report_verified_running(record,json.loads(a.proof_file.read_text()),result_path)));return
    if a.action=='enter':
        if not a.ids_file or a.ttl<30 or a.ttl>300: raise RuntimeError('Enter requires ids-file and TTL 30..300 seconds')
        if a.state_file.exists(): raise RuntimeError('State path already exists; do not overwrite an owner')
        row=runner(a.name)
        if not row['unschedulable'] or row['draining']: raise RuntimeError('Runner must be unschedulable and not draining')
        ids=json.loads(a.ids_file.read_text())
        if not isinstance(ids,list) or any(not isinstance(i,str) or str(uuid.UUID(i))!=i for i in ids) or len(ids)!=len(set(ids)): raise RuntimeError('IDs must be a unique UUID JSON array')
        record={'name':a.name,'runnerId':row['id'],'apiUrl':row['apiUrl'],'owner':str(uuid.uuid4()),'ttl':a.ttl,'protectedIds':ids,'createdAt':datetime.datetime.now(datetime.timezone.utc).isoformat(),'phase':'intent'}
        write(a.state_file,record)
        if redis('enter',record)!=1: raise RuntimeError('Another DELETE maintenance owner exists')
        try:
            barrier(record)
            result=summary(record)
            if not result['owned'] or result['remainingMs']<=0: raise RuntimeError('Preparation lease expired; do not stop containers')
        except Exception:
            redis('release',record)
            record['phase']='aborted';write(a.state_file,record)
            raise
        record['phase']='entered';write(a.state_file,record)
    else:
        record=read_record(a.state_file,a.name)
        if a.action!='status':
            if redis(a.action,record)!=1: raise RuntimeError('Lease owner mismatch or expired; operation rejected')
            record['phase']=a.action;write(a.state_file,record)
        result=summary(record)
    print(json.dumps(result))
if __name__=='__main__':main()
