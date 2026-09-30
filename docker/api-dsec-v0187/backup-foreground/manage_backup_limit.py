#!/usr/bin/env python3
"""Prepare/apply/rollback an API-only backup concurrency override. No Runner mutation."""
import argparse,datetime,hashlib,importlib.util,json,os,pathlib,subprocess,time,urllib.request,uuid
HERE=pathlib.Path(__file__).resolve().parent
OVERRIDE=HERE/'docker-compose.backup-foreground.yaml'
PLAN=HERE/'plan-v2.private.json'
RESULT=HERE/'apply-result.json'
SERVICE='daytona-api-1'
WORK=pathlib.Path('/opt/daytona/docker')
CLEAN_RUN='dd-app200-20260930-attempt3-formal-71b16897'
def now():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
def save(path,value):
 tmp=path.with_suffix(path.suffix+'.tmp');fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
 with os.fdopen(fd,'w')as f:json.dump(value,f,indent=2)
 os.replace(tmp,path)
def run(argv,timeout=120):
 r=subprocess.run(argv,cwd=WORK,capture_output=True,text=True,timeout=timeout)
 if r.returncode:
  p=HERE/('command-error-'+str(time.time_ns())+'.private.json');save(p,dict(at=now(),returncode=r.returncode,stdout=r.stdout[-4000:],stderr=r.stderr[-4000:]))
  raise RuntimeError('Command failed; private diagnostic '+str(p))
 return r.stdout
def inspect():return json.loads(run(['docker','inspect',SERVICE]))[0]
def env(c):return dict(x.split('=',1)for x in c['Config']['Env']if'='in x)
def mounts(c):return sorted([{k:m.get(k)for k in ['Type','Name','Source','Destination','RW']}for m in c['Mounts']],key=lambda x:x['Destination'])
def compose(files,*tail):
 args=['docker','compose','--project-name','daytona']
 for f in files:args+=['-f',str(f)]
 return args+list(tail)
def config(files):return json.loads(run(compose(files,'config','--format','json')))
def gmodule():
 p='/opt/codex-dsec-20260929/daytona-api-v0187/docker/api-dsec-v0187/runner_gate.py';sp=importlib.util.spec_from_file_location('g',p);m=importlib.util.module_from_spec(sp);sp.loader.exec_module(m);return m
def counts():
 g=gmodule()
 return dict(activeJobs=int(g.sql("SELECT count(*) FROM job WHERE status::text IN ('PENDING','IN_PROGRESS','pending','in_progress')")),readyRunners=int(g.sql("SELECT count(*) FROM runner WHERE name IN ('a2','a3','a4','a5') AND state::text='ready' AND draining=false AND unschedulable=false")))
def stable(plan,c):
 assert c['Image']==plan['image'],'API image drift'
 assert mounts(c)==plan['mounts'],'API data/config mounts drift'
def rendered_env(rendered,image):
 effective=dict(x.split('=',1)for x in image['Config'].get('Env',[])if'='in x)
 configured=rendered['services']['api'].get('environment',{})
 assert isinstance(configured,dict)and all(isinstance(k,str)and isinstance(v,str)for k,v in configured.items()),'Unresolved or unsupported rendered API environment'
 effective.update(configured)
 return effective
def validate_candidate(plan):
 for p,sha in plan['baseHashes'].items():assert hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()==sha,'Base Compose changed'
 assert hashlib.sha256(OVERRIDE.read_bytes()).hexdigest()==plan['overrideHash'],'Override changed'
 base=config(plan['baseFiles']);assert digest(base)==plan['renderedBaseHash'],'Rendered base Compose drift (including env_file/interpolation)'
 image=json.loads(run(['docker','image','inspect',plan['image']]))[0]
 assert rendered_env(base,image)==plan['oldEnv'],'Rendered base API environment differs from saved running environment'
 candidate=config(plan['baseFiles']+[str(OVERRIDE)])
 a=base['services']['api'];b=candidate['services']['api'];ea=dict(a.get('environment',{}));eb=dict(b.get('environment',{}))
 assert {k for k in set(ea)|set(eb)if ea.get(k)!=eb.get(k)}=={'MAX_CONCURRENT_BACKUPS_PER_RUNNER'}
 assert str(eb['MAX_CONCURRENT_BACKUPS_PER_RUNNER'])=='1'
 ac=dict(a);bc=dict(b);ac.pop('environment',None);bc.pop('environment',None);assert ac==bc,'Non-environment API configuration changed'
 ba=dict(base);ca=dict(candidate);ba['services']=dict(base['services']);ca['services']=dict(candidate['services']);ba['services'].pop('api');ca['services'].pop('api');assert ba==ca,'Other service configuration changed'
 assert json.loads(run(['docker','image','inspect',b['image']]))[0]['Id']==plan['image'],'Image tag changed'
def prepare():
 assert not PLAN.exists(),'Preparation already exists; do not overwrite'
 c=inspect();labels=c['Config'].get('Labels',{});files=labels['com.docker.compose.project.config_files'].split(',')
 assert labels['com.docker.compose.service']=='api'and labels['com.docker.compose.project']=='daytona'
 assert len(files)==6 and str(OVERRIDE)not in files
 e=env(c);assert e.get('RATE_LIMIT_SANDBOX_CREATE_LIMIT')==''and e.get('RATE_LIMIT_SANDBOX_CREATE_TTL')==''
 assert 'MAX_CONCURRENT_BACKUPS_PER_RUNNER'not in e,'Old value differs from reviewed absent/default6 state'
 rendered=config(files);image=json.loads(run(['docker','image','inspect',c['Image']]))[0]
 assert rendered_env(rendered,image)==e,'Rendered base API environment differs from current running environment'
 save(HERE/'rendered-base-v2.private.json',rendered)
 plan=dict(version=2,renderedBaseHash=digest(rendered),at=now(),expectedOldId=c['Id'],image=c['Image'],oldEnv=e,mounts=mounts(c),baseFiles=files,baseHashes={p:hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()for p in files},overrideHash=hashlib.sha256(OVERRIDE.read_bytes()).hexdigest())
 validate_candidate(plan);save(HERE/'old-container-v2.private.json',c);save(PLAN,plan)
 safe=dict(planVersion=2,renderedBaseHash=plan['renderedBaseHash'],renderedApiEnvironmentMatchesCurrentContainer=True,preparedAt=now(),oldContainerId=c['Id'],image=c['Image'],change={'MAX_CONCURRENT_BACKUPS_PER_RUNNER':{'before':'absent; existing source default6','after':'1'}},otherEnvironmentUnchanged=True,imageUnchanged=True,mountsUnchanged=True,otherServicesUnchanged=True,createLimiterRemainsDisabled=True,productionApplied=False)
 save(HERE/'preparation-v2.json',safe);print(json.dumps(safe))
def health(plan,expected_env):
 deadline=time.monotonic()+150;last='not checked'
 while time.monotonic()<deadline:
  try:
   c=inspect();stable(plan,c);assert env(c)==expected_env,'Unexpected effective API environment'
   assert c['State'].get('Health',{}).get('Status')=='healthy'
   with urllib.request.urlopen('http://127.0.0.1:3000/api/config',timeout=5)as r:assert r.status==200
   n=counts();assert n['readyRunners']==4
   return dict(at=now(),id=c['Id'],image=c['Image'],startedAt=c['State']['StartedAt'],health='healthy',configHttpStatus=200,counts=n,backupConcurrency=expected_env.get('MAX_CONCURRENT_BACKUPS_PER_RUNNER'),createLimit=expected_env.get('RATE_LIMIT_SANDBOX_CREATE_LIMIT'),createTTL=expected_env.get('RATE_LIMIT_SANDBOX_CREATE_TTL'))
  except Exception as e:last=type(e).__name__
  time.sleep(2)
 raise RuntimeError('Health deadline exceeded; last error type '+last)
def reconciliation_module():
 p=HERE/'maintenance_reconciliation.py';sp=importlib.util.spec_from_file_location('reconciliation',p);m=importlib.util.module_from_spec(sp);sp.loader.exec_module(m);return m
def cleanup_gate(path,reconciliation=None):
 content=path.read_bytes();proof=json.loads(content);assert proof['runId']==CLEAN_RUN and proof['phase']=='final'and not proof['activeJobs']
 verified=None
 if reconciliation:
  module=reconciliation_module();verified=module.validate_reconciliation(reconciliation,expected_run_id=CLEAN_RUN)
  assert hashlib.sha256(content).hexdigest()==verified['original_final_sha256'],'Original final audit changed'
 else:assert proof['cleanupPassed']and proof['protectedWarm80Passed']
 ids=[str(uuid.UUID(s))for s in proof['auditedSandboxIds']];assert ids
 g=gmodule();left=int(g.sql('SELECT count(*) FROM sandbox WHERE id IN ('+', '.join(g.lit(x)for x in ids)+") AND state::text!='destroyed'"));assert left==0,'Test runtime remains live'
 if verified:
  cols='id,state::text,"desiredState"::text,"runnerId",snapshot,labels->>\'pool_profile\'AS pool_profile,labels->>\'pool_state\'AS pool_state,labels->>\'pool_use_count\'AS pool_use_count,labels->>\'app_id\'AS app_id,labels->>\'dd_app_id\'AS dd_app_id,labels->>\'session_id\'AS session_id,labels->>\'closed_app_id\'AS closed_app_id,labels->>\'skill_bundle_version\'AS skill_bundle_version,labels->>\'warm_baseline_sha256\'AS warm_baseline_sha256'
  rows=json.loads(g.sql('SELECT coalesce(json_agg(x),\'[]\'::json)FROM(SELECT '+cols+' FROM sandbox WHERE state::text!=\'destroyed\')x'))
  module.validate_current_warm_rows(rows,verified['current_warm_rows'])
 n=counts();assert n['activeJobs']==0 and n['readyRunners']==4,'Jobs active or Runner not ready'
def recreate(files):
 run(compose(files,'up','-d','--no-deps','--no-build','--pull','never','--force-recreate','api'),timeout=240)
def apply(cleanup_proof,reconciliation=None):
 assert not RESULT.exists(),'Apply result exists; do not repeat'
 plan=json.loads(PLAN.read_text());validate_candidate(plan);c=inspect();assert c['Id']==plan['expectedOldId'];stable(plan,c);assert env(c)==plan['oldEnv']
 cleanup_gate(cleanup_proof,reconciliation)
 started=time.monotonic();save(HERE/'apply-started.json',dict(at=now(),oldId=c['Id']))
 try:
  recreate(plan['baseFiles']+[str(OVERRIDE)])
  expected=dict(plan['oldEnv'],MAX_CONCURRENT_BACKUPS_PER_RUNNER='1');h=health(plan,expected)
  result=dict(status='applied',at=now(),seconds=time.monotonic()-started,oldId=c['Id'],newId=h['id'],health=h);save(RESULT,result);print(json.dumps(result))
 except Exception as e:
  save(HERE/'apply-failure.json',dict(at=now(),error_type=type(e).__name__))
  validate_candidate(plan)
  recreate(plan['baseFiles']);h=health(plan,plan['oldEnv']);save(HERE/'automatic-rollback.json',dict(at=now(),health=h))
  raise
def rollback():
 plan=json.loads(PLAN.read_text());validate_candidate(plan);c=inspect();stable(plan,c)
 result=json.loads(RESULT.read_text());assert c['Id']==result['newId'],'A later deployment exists'
 recreate(plan['baseFiles']);h=health(plan,plan['oldEnv']);save(HERE/'rollback-result.json',dict(at=now(),health=h));print(json.dumps(h))
def main():
 os.umask(0o077);p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','apply','rollback']);p.add_argument('--confirm-production-change',action='store_true');p.add_argument('--cleanup-proof',type=pathlib.Path);p.add_argument('--warm-reconciliation',type=pathlib.Path);a=p.parse_args()
 if a.action=='prepare':prepare();return
 assert a.confirm_production_change,'Explicit production authorization flag required'
 if a.action=='apply':assert a.cleanup_proof;apply(a.cleanup_proof,a.warm_reconciliation)
 else:rollback()
if __name__=='__main__':main()
