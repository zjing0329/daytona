#!/usr/bin/env python3
"""Only isolated v187-clone -> v190 -> old-image/new-schema validation. Never production DDL."""
import argparse,datetime,hashlib,json,os,pathlib,re,subprocess,time,traceback,urllib.parse
os.umask(0o077)
parser=argparse.ArgumentParser()
parser.add_argument('--image',required=True)
parser.add_argument('--output',required=True)
parser.add_argument('--postgres',default='dsec-v190-db-rehearsal-20260930')
parser.add_argument('--old-image',default='deepdiver/daytona-api:v0.187.0-dsec-3cb5e97')
parser.add_argument('--fixture-env',required=True)
a=parser.parse_args();out=pathlib.Path(a.output).resolve()
assert not out.exists(),'use a new evidence directory'
out.mkdir(mode=0o700)
pg=json.loads(subprocess.check_output(['docker','inspect',a.postgres],text=True))[0]
assert a.postgres.startswith('dsec-v190-') and pg['HostConfig']['NetworkMode']=='none'
assert not pg['HostConfig'].get('PortBindings'),'test database must not publish host ports'
assert all(m['Type']=='tmpfs' for m in pg['Mounts']),'test database must have no durable production volumes'
prefix=a.postgres.replace('-db-','-api-')
created=[];launched=[];result={'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'new_image':a.image,'old_image':a.old_image,'database_container':a.postgres,'database':'upgrade_test','production_modified':False,'steps':{}}
def save(): (out/'result.json').write_text(json.dumps(result,indent=2))
def cmd(args,log=None,timeout=180):
 r=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
 if log:(out/log).write_text(r.stdout+r.stderr)
 if r.returncode:raise RuntimeError((log or args[0])+': command failed with exit '+str(r.returncode))
 return r.stdout.strip()
def sql(s):
 return cmd(['docker','exec',a.postgres,'psql','-X','-v','ON_ERROR_STOP=1','-U','postgres','-d','upgrade_test','-At','-c',s])
def start(name,args):
 assert subprocess.run(['docker','inspect',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode!=0,'test container exists'
 created.append(name);launched.append(name)
 cmd(['docker','run','-d','--name',name,'--network','container:'+a.postgres,*args])
def stop(name):
 if subprocess.run(['docker','inspect',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode!=0:
  if name in created:created.remove(name)
  return
 r=subprocess.run(['docker','logs',name],capture_output=True,text=True);(out/(name+'.private.log')).write_text(r.stdout+r.stderr)
 cmd(['docker','rm','-f',name])
 if name in created:created.remove(name)
def step(name,fn):
 begin=time.monotonic()
 try:
  detail=fn();result['steps'][name]={'status':'pass','seconds':round(time.monotonic()-begin,3),'detail':detail};save();print(name,'PASS',flush=True);return detail
 except BaseException as e:
  result['steps'][name]={'status':'fail','error':str(e)};save();raise
def migrations():
 return json.loads(sql('SELECT json_agg(t) FROM (SELECT timestamp,name FROM migrations ORDER BY timestamp) t'))
def columns(table):
 return json.loads(sql("SELECT json_agg(column_name ORDER BY ordinal_position) FROM information_schema.columns WHERE table_schema='public' AND table_name='"+table+"'"))
tables=['sandbox','job','sandbox_usage_periods','sandbox_usage_periods_archive']
old_columns={t:columns(t) for t in tables}
def data_digest():
 answer={}
 for table,cols in old_columns.items():
  quoted=','.join('"'+x.replace('"','""')+'"' for x in cols)
  q='SELECT json_build_object(\'count\',count(*),\'digest\',md5(coalesce(string_agg(md5(row_to_json(t)::text),\'\' ORDER BY md5(row_to_json(t)::text)),\'\'))) FROM (SELECT '+quoted+' FROM "'+table+'") t'
  answer[table]=json.loads(sql(q))
 return answer
def request(api,path,method='GET',headers=None):
 args=['docker','exec',api,'curl','-sS','--max-time','10','-w','\n%{http_code}','-X',method]
 for h in headers or []:args+=['-H',h]
 text=cmd(args+['http://127.0.0.1:3000'+path]);body,status=text.rsplit('\n',1)
 return int(status),body
fixture=json.loads(pathlib.Path(a.fixture_env).read_text())
fixture.update(DB_USERNAME='postgres',DB_PASSWORD='isolated-upgrade-test',DB_DATABASE='upgrade_test',RUN_MIGRATIONS='true',DISABLE_CRON_JOBS='true',DONT_SERVE_DASHBOARD='false',APP_ROLE='api')
envargs=[]
for k,v in fixture.items():envargs+=['-e',k+'='+str(v)]
def start_api(image,name):
 start(name,['--cpus','4','--memory','8g',*envargs,image])
 for _ in range(60):
  r=subprocess.run(['docker','exec',name,'curl','-fsS','--max-time','2','http://127.0.0.1:3000/api/config'],capture_output=True)
  if r.returncode==0:return
  state=json.loads(cmd(['docker','inspect',name]))[0]['State']
  if not state['Running']:raise RuntimeError('test API exited before ready')
  time.sleep(2)
 raise RuntimeError('test API not ready after 120s')
def http_contract(api,is_new):
 auth=['Authorization: Bearer isolated-runner-key']
 assert request(api,'/api/config')[0]==200
 status,body=request(api,'/api/jobs/admission/capabilities',headers=auth)
 assert status==200 and json.loads(body)=={'version':1,'recoveryRenewal':True}
 status,body=request(api,'/api/jobs/admission/poll?class=cleanup&limit=1',headers=auth)
 assert status==200 and json.loads(body)=={'version':1,'jobs':[]}
 assert request(api,'/api/jobs/admission/poll?class=invalid&limit=1',headers=auth)[0]==400
 jid='00000000-0000-4000-8000-000000000091'
 sql('INSERT INTO job (id,version,type,status,"runnerId","resourceType","resourceId",payload,"startedAt","createdAt","updatedAt") SELECT \''+jid+'\',1,\'CREATE_SANDBOX\',\'IN_PROGRESS\',id::text,\'SANDBOX\',\'isolated-upgrade-recovery\',\'{}\',NOW(),NOW(),NOW() FROM runner WHERE name=\'isolated-test\'')
 try:
  status,body=request(api,'/api/jobs/admission/recover/'+jid,'POST',auth);payload=json.loads(body)
  assert status==200 and payload['job']['id']==jid
  sql('UPDATE job SET status=\'FAILED\',"completedAt"=NOW() WHERE id=\''+jid+'\'')
  assert request(api,'/api/jobs/admission/recover/'+jid,'POST',auth)==(200,'{"version":1,"job":null}')
 finally:sql("DELETE FROM job WHERE id='"+jid+"'")
 detail={'health':200,'admission_http_checks':6,'migrations':len(migrations()),'run_migrations':'true'}
 if is_new:
  headers=cmd(['docker','exec',api,'curl','-sS','-D','-','-o','/dev/null','-X','OPTIONS','-H','Origin: http://dashboard.example.test','-H','Access-Control-Request-Method: GET','-H','Access-Control-Request-Headers: Authorization,Content-Type','http://127.0.0.1:3000/api/config']).lower()
  assert 'access-control-allow-origin: http://dashboard.example.test' in headers
  assert 'authorization' in headers and 'content-type' in headers
  assert 'access-control-allow-credentials: true' not in headers
  detail['cors']={'bearer_preflight':True,'credentialed_cookies_enabled':False}
  status,html=request(api,'/dashboard/')
  assert status==200 and '<html' in html.lower(),'dashboard index unavailable'
  scripts=re.findall(r'<script[^>]*src=["\']([^"\']+)',html)
  assert scripts,'dashboard missing script'
  path=urllib.parse.urlsplit(scripts[0]).path
  assert path.startswith('/dashboard/') and '..' not in path
  code,js=request(api,path);assert code==200 and len(js)>100
  detail['dashboard']={'index_status':status,'javascript_status':code,'javascript_bytes':len(js),'javascript_sha256':hashlib.sha256(js.encode()).hexdigest()}
  dex=cmd(['docker','exec',api,'curl','-fsS','http://127.0.0.1:5556/dex/.well-known/openid-configuration'])
  assert json.loads(dex)['issuer']=='http://127.0.0.1:5556/dex'
  detail['dex_discovery']=True
 return detail
def rollback_safety():
 state_count=int(sql("SELECT count(*) FROM sandbox WHERE state::text IN ('pausing','paused','resuming') OR \"desiredState\"::text='paused'"))
 domain_count=int(sql('SELECT count(*) FROM sandbox WHERE "domainAllowList" IS NOT NULL'))
 assert state_count==0,'new pause states in use: old-image rollback denied'
 assert domain_count==0,'domain allowlist in use: old-image rollback denied'
 return {'new_states_used':state_count,'domain_allowlist_used':domain_count,'new_schema_preserved':True,'no_down_or_restore_on_production':True}
def migration_lock_timeout():
 assert len(migrations())==109
 lock_sql="SET application_name='dsec-upgrade-lock-proof'; BEGIN; LOCK TABLE sandbox IN ACCESS EXCLUSIVE MODE;\n"
 locker=subprocess.Popen(['docker','exec','-i',a.postgres,'psql','-X','-v','ON_ERROR_STOP=1','-U','postgres','-d','upgrade_test','-At'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 locker.stdin.write(lock_sql.encode());locker.stdin.flush()
 try:
  for _ in range(30):
   if sql("SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid WHERE a.application_name='dsec-upgrade-lock-proof' AND l.relation='sandbox'::regclass AND l.mode='AccessExclusiveLock' AND l.granted")=='1':break
   time.sleep(.1)
  else:raise RuntimeError('could not establish isolated DDL blocker')
  args=['docker','run','--rm','--network','container:'+a.postgres,*envargs,'-e','PGOPTIONS=-c lock_timeout=2s -c statement_timeout=120s','--entrypoint','sh',a.image,'-c','cd /daytona && yarn migration:run:pre-deploy']
  r=subprocess.run(args,capture_output=True,text=True,timeout=90)
  (out/'expected-lock-timeout.private.log').write_text(r.stdout+r.stderr)
  assert r.returncode!=0 and 'lock timeout' in (r.stdout+r.stderr).lower(),'migration did not honor configured DDL lock timeout'
 finally:
  stdout,stderr=locker.communicate(input=b'ROLLBACK;\n',timeout=15)
  (out/'isolated-lock-holder.log').write_bytes(stdout+stderr)
 assert len(migrations())==109,'failed transaction must leave original migration ledger'
 assert 'domainAllowList' not in columns('sandbox')
 return {'expected_failure':'lock timeout','migration_count_after':109,'domain_column_absent':True}
def migration_up():
 before=migrations();assert len(before)==109,'clone must start at v187 migrations'
 digest_before=data_digest()
 args=['docker','run','--rm','--network','container:'+a.postgres,*envargs,'-e','PGOPTIONS=-c lock_timeout=2s -c statement_timeout=120s','--entrypoint','sh',a.image,'-c','cd /daytona && yarn migration:run:pre-deploy']
 cmd(args,log='migration-up.private.log',timeout=180)
 after=migrations()
 expected=[1781022372014,1781267138889,1781597992117,1781740800000]
 assert len(after)==113 and [int(x['timestamp']) for x in after if x not in before]==expected
 assert data_digest()==digest_before,'existing lifecycle/usage row values changed'
 assert sql("SELECT count(*) FROM sandbox WHERE \"domainAllowList\" IS NOT NULL")=='0'
 assert sql("SELECT count(*) FROM sandbox_usage_periods WHERE \"regionType\" != 'shared'")=='0'
 assert sql("SELECT count(*) FROM sandbox_usage_periods_archive WHERE \"regionType\" != 'shared'")=='0'
 assert sql("SELECT cardinality(permissions) FROM organization_role WHERE id='00000000-0000-0000-0000-000000000005'")=='15'
 for key in ['pausing','paused','resuming']:
  assert sql("SELECT count(*) FROM pg_enum WHERE enumtypid='sandbox_state_enum'::regtype AND enumlabel='"+key+"'")=='1'
 return {'before':109,'after':113,'new_timestamps':expected,'existing_data_digests':digest_before,'role_permissions':15,'defaults_verified':True,'lock_timeout':'2s','statement_timeout':'120s','down_executed':False}
try:
 save()
 dex=out/'dex-isolated.yaml'
 dex.write_text('issuer: http://127.0.0.1:5556/dex\nstorage:\n  type: sqlite3\n  config:\n    file: /tmp/dex.db\nweb:\n  http: 127.0.0.1:5556\nstaticClients:\n- id: dsec\n  name: isolated validation\n  secret: isolated-test\n  redirectURIs:\n  - http://127.0.0.1:3000/callback\nenablePasswordDB: true\n')
 start(prefix+'-redis',['redis:latest','redis-server','--save','','--appendonly','no'])
 start(prefix+'-dex',['--user','0','-v',str(dex)+':/etc/dex/config.yaml:ro','dexidp/dex:v2.42.0','dex','serve','/etc/dex/config.yaml'])
 start(prefix+'-minio',['--tmpfs','/data','-e','MINIO_ROOT_USER=dsec','-e','MINIO_ROOT_PASSWORD=isolated-test','minio/minio:latest','server','/data','--address','127.0.0.1:9000'])
 old=prefix+'-old-before'
 step('old_api_v187_schema_start',lambda:start_api(a.old_image,old))
 step('old_api_v187_schema_http',lambda:http_contract(old,False));stop(old)
 step('migration_lock_timeout',migration_lock_timeout)
 step('v190_predeploy_migrations',migration_up)
 new=prefix+'-new'
 step('new_api_expanded_schema_start',lambda:start_api(a.image,new))
 step('new_api_http_dashboard_oidc_cors',lambda:http_contract(new,True))
 assert len(migrations())==113,'startup unexpectedly changed migrations'
 stop(new)
 old=prefix+'-old-rollback'
 step('old_api_expanded_schema_start',lambda:start_api(a.old_image,old))
 step('old_api_expanded_schema_http',lambda:http_contract(old,False))
 assert len(migrations())==113,'rollback must not remove new migrations'
 step('rollback_safety_checks',rollback_safety)
 stop(old);result['status']='pass'
except BaseException as e:
 result['status']='fail';result['error']=str(e);(out/'failure.private.log').write_text(traceback.format_exc())
finally:
 cleanup_errors=[]
 for n in list(reversed(created)):
  try:stop(n)
  except Exception as exc:cleanup_errors.append({'container':n,'error':str(exc)})
 # PG was created only for this rehearsal; its logical dump/evidence remains private on disk.
 try:cmd(['docker','rm','-f',a.postgres])
 except Exception as exc:cleanup_errors.append({'container':a.postgres,'error':str(exc)})
 remaining=[n for n in launched+[a.postgres] if subprocess.run(['docker','inspect',n],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0]
 result['cleanup']={'verified':not cleanup_errors and not remaining,'errors':cleanup_errors,'remaining':remaining,'postgres_removed':a.postgres not in remaining}
 if not result['cleanup']['verified']:
  result['status']='fail';result['error']='isolated fixture cleanup did not verify'
 result['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();save()
 print(json.dumps({'status':result['status'],'error':result.get('error'),'result':str(out/'result.json')}),flush=True)
 if result['status']!='pass':raise SystemExit(1)
