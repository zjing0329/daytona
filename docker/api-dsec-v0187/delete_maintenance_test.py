#!/usr/bin/env python3
"""Run without production access; optional Lua integration only on the named isolated test container."""
import importlib.util,json,os,subprocess,tempfile,unittest,copy,datetime
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('maintenance',Path(__file__).with_name('delete_maintenance.py'))
h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
ID='00000000-0000-4000-8000-000000000021';RUNNER='00000000-0000-4000-8000-000000000031'
class HelperTests(unittest.TestCase):
 def test_barrier_is_ordered_locked_and_rejects_changed_rows(self):
  record={'protectedIds':[ID],'runnerId':RUNNER}
  for rows in [[],[{'id':ID,'runnerId':RUNNER,'desiredState':'destroyed'}],[{'id':ID,'runnerId':'wrong','desiredState':'started'}]]:
   with patch.object(h,'sql',return_value='BEGIN\nSET\n'+json.dumps(rows)+'\nCOMMIT') as sql:
    with self.assertRaises(RuntimeError):h.barrier(record)
    self.assertIn('ORDER BY id FOR UPDATE',sql.call_args[0][0])
  with patch.object(h,'sql',return_value=json.dumps([{'id':ID,'runnerId':RUNNER,'desiredState':'started'}])):h.barrier(record)
  with patch.object(h,'sql',side_effect=AssertionError('empty IDs must not lock arbitrary rows')):h.barrier({'protectedIds':[]})
 def test_barrier_rejects_unexpected_transaction_tail(self):
  rows=[{'id':ID,'runnerId':RUNNER,'desiredState':'started'}]
  with patch.object(h,'sql',return_value=json.dumps(rows)+'\nUNEXPECTED'):
   with self.assertRaises(RuntimeError):h.barrier({'protectedIds':[ID],'runnerId':RUNNER})
 def test_private_state_and_owner_not_in_status(self):
  record={'name':'a2','runnerId':RUNNER,'protectedIds':[ID],'owner':'secret-owner'}
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'lease.json';h.write(p,record);self.assertEqual(p.stat().st_mode&0o777,0o600)
  with patch.object(h,'redis',return_value=[-1,1]):
   result=h.summary(record);self.assertTrue(result['latched']);self.assertTrue(result['owned']);self.assertNotIn('secret-owner',json.dumps(result))
  with patch.object(h,'redis',return_value=[-2,0]):self.assertFalse(h.summary(record)['owned'])
 @unittest.skipUnless(os.environ.get('DSEC_TEST_POSTGRES_CONTAINER'),'explicit isolated PostgreSQL container required')
 def test_real_varchar_id_barrier_matches_production_schema(self):
  container=os.environ['DSEC_TEST_POSTGRES_CONTAINER']
  self.assertEqual(container,'daytona-delete-guard-test-db')
  def query(q):
   return subprocess.check_output(['docker','exec',container,'psql','-v','ON_ERROR_STOP=1','-U','postgres','-d','delete_test','-At','-c',q],text=True).strip()
  query('CREATE TABLE sandbox(id character varying PRIMARY KEY,"runnerId" uuid,"desiredState" text)')
  try:
   ids=[ID,'00000000-0000-4000-8000-000000000022']
   for sandbox_id in ids:query('INSERT INTO sandbox VALUES('+h.lit(sandbox_id)+','+h.lit(RUNNER)+",'started')")
   with patch.object(h,'sql',side_effect=query):h.barrier({'protectedIds':ids,'runnerId':RUNNER})
   query("UPDATE sandbox SET \"desiredState\"='destroyed'")
   with patch.object(h,'sql',side_effect=query):
    with self.assertRaises(RuntimeError):h.barrier({'protectedIds':[ID],'runnerId':RUNNER})
  finally:query('DROP TABLE sandbox')
 @unittest.skipUnless(os.environ.get('DSEC_TEST_REDIS_CONTAINER'),'explicit isolated Redis container required')
 def test_real_lua_owner_latch_renew_clear(self):
  container=os.environ['DSEC_TEST_REDIS_CONTAINER']
  self.assertEqual(container,'daytona-delete-guard-test-redis')
  key='isolated-test-delete-maintenance';owner='isolated-owner'
  def command(*args):return subprocess.check_output(['docker','exec',container,'redis-cli','--raw',*args],text=True).strip()
  def action(name,who=owner,ttl=300000):
   values=[int(x) for x in command('EVAL',h.SCRIPTS[name],'1',key,who,str(ttl)).splitlines()]
   return values if name=='status' else values[0]
  try:
   self.assertEqual(action('enter'),1);self.assertEqual(action('enter','other'),0)
   self.assertEqual(action('latch','other'),0);self.assertEqual(action('latch'),1)
   self.assertEqual(action('status'),[-1,1]);self.assertEqual(action('renew'),1);self.assertEqual(action('status'),[-1,1])
   self.assertEqual(action('release','other'),0);self.assertEqual(action('release'),1);self.assertEqual(action('status'),[-2,0])
  finally:command('DEL',key)

class VerifiedRunningTests(unittest.TestCase):
 def setUp(self):
  self.record={'name':'a3','runnerId':RUNNER,'protectedIds':[ID],'createdAt':'2026-09-29T10:00:00+00:00'}
  self.node={'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'all_ids':['docker-one','stopped-two'],'running_ids':['docker-one'],'running_sandbox_ids':[ID],'sandbox_docker_ids':{ID:'docker-one'},'volumes':[['volume','docker-data','/source','/var/lib/docker',True]],'paused':False,'health':'healthy','http_status':200,'daemon_versions':{'docker-one':'version-1'}}
  self.proof={'name':'a3','runnerId':RUNNER,'protectedIds':[ID],'node':self.node,'before':copy.deepcopy(self.node)}
  self.stopped={'id':ID,'runnerId':RUNNER,'state':'stopped','desiredState':'stopped','pending':False}
 def test_proof_rejects_identity_freshness_mounts_and_daemon_failures(self):
  h.validate_running_proof(self.record,self.proof)
  changes=[('runnerId','wrong'),('protectedIds',[])]
  for key,value in changes:
   p=copy.deepcopy(self.proof);p[key]=value
   with self.assertRaises(RuntimeError):h.validate_running_proof(self.record,p)
  changes=[('at','2020-01-01T00:00:00+00:00'),('volumes',[]),('daemon_versions',{}),('health','unhealthy'),('http_status',503),('paused',True),('all_ids',['other']),('running_ids',['docker-one','docker-one']),('sandbox_docker_ids',{ID:'changed'})]
  for key,value in changes:
   p=copy.deepcopy(self.proof);p['node'][key]=value
   with self.assertRaises(RuntimeError):h.validate_running_proof(self.record,p)
 def test_preconditions_reject_missing_expiring_or_other_lease(self):
  for status in ([-2,0],[300000,1],[-1,0]):
   with patch.object(h,'redis',return_value=status),patch.object(h,'sql') as sql:
    with self.assertRaises(RuntimeError):h.assert_report_preconditions(self.record,[self.stopped])
    sql.assert_not_called()
 def test_preconditions_reject_conflicting_intent_and_jobs(self):
  for values in ((1,0),(0,1)):
   with patch.object(h,'redis',return_value=[-1,1]),patch.object(h,'sql',side_effect=map(str,values)):
    with self.assertRaises(RuntimeError):h.assert_report_preconditions(self.record,[self.stopped])
  for field,value in [('runnerId','wrong'),('state','error'),('desiredState','started'),('pending',True)]:
   row={**self.stopped,field:value}
   with patch.object(h,'redis',return_value=[-1,1]),patch.object(h,'sql') as sql:
    with self.assertRaises(RuntimeError):h.assert_report_preconditions(self.record,[row])
    sql.assert_not_called()
 def test_success_only_reports_original_and_keeps_lease(self):
  rows=[copy.deepcopy(self.stopped)]
  def send(token,sandbox_id):
   self.assertEqual(sandbox_id,ID);self.assertEqual(token,'private-token');rows[0].update(state='started',desiredState='started')
  with tempfile.TemporaryDirectory() as tmp,patch.object(h,'report_rows',side_effect=lambda record:copy.deepcopy(rows)),patch.object(h,'redis',return_value=[-1,1]) as redis,patch.object(h,'sql',side_effect=lambda q:'private-token' if 'apiKey' in q else '0'),patch.object(h,'send_running_report',side_effect=send) as put:
   p=Path(tmp)/'result.json';result=h.report_verified_running(self.record,self.proof,p)
   self.assertEqual(result['reported'],1);put.assert_called_once();self.assertNotIn('private-token',p.read_text());self.assertEqual(p.stat().st_mode&0o777,0o600)
   self.assertTrue(all(c.args[0]=='status' for c in redis.call_args_list))
 def test_already_started_is_idempotent(self):
  row={**self.stopped,'state':'started','desiredState':'started'}
  with tempfile.TemporaryDirectory() as tmp,patch.object(h,'report_rows',return_value=[row]),patch.object(h,'redis',return_value=[-1,1]),patch.object(h,'sql',return_value='0'),patch.object(h,'send_running_report') as put:
   self.assertEqual(h.report_verified_running(self.record,self.proof,Path(tmp)/'result.json')['reported'],0);put.assert_not_called()
 def test_failed_http_or_postflight_conflict_never_releases(self):
  with tempfile.TemporaryDirectory() as tmp,patch.object(h,'report_rows',return_value=[self.stopped]),patch.object(h,'redis',return_value=[-1,1]) as redis,patch.object(h,'sql',return_value='0'),patch.object(h,'send_running_report',side_effect=RuntimeError('failed')):
   with self.assertRaises(RuntimeError):h.report_verified_running(self.record,self.proof,Path(tmp)/'result.json')
   self.assertTrue(all(c.args[0]=='status' for c in redis.call_args_list))
  calls=0
  def preconditions(record,rows):
   nonlocal calls
   calls+=1
   if calls==3:raise RuntimeError('external STOP appeared after PUT')
  with tempfile.TemporaryDirectory() as tmp,patch.object(h,'report_rows',return_value=[self.stopped]),patch.object(h,'assert_report_preconditions',side_effect=preconditions),patch.object(h,'sql',return_value='private-token'),patch.object(h,'send_running_report') as put,patch.object(h,'redis') as redis:
   with self.assertRaises(RuntimeError):h.report_verified_running(self.record,self.proof,Path(tmp)/'result.json')
   put.assert_called_once();redis.assert_not_called()
 def test_rollback_check_is_readonly_and_does_not_require_daemon_proof(self):
  with tempfile.TemporaryDirectory() as tmp:
   args=['helper','check-rollback-intent','a3','--state-file',str(Path(tmp)/'lease.json')]
   with patch('sys.argv',args),patch.object(h.os,'geteuid',return_value=0),patch.object(h,'read_record',return_value=self.record),patch.object(h,'report_rows',return_value=[self.stopped]),patch.object(h,'assert_report_preconditions') as check,patch.object(h,'send_running_report') as put,patch.object(h,'write') as write,patch('builtins.print') as output:
    h.main();check.assert_called_once_with(self.record,[self.stopped]);put.assert_not_called();write.assert_not_called();self.assertTrue(json.loads(output.call_args.args[0])['allowed'])
 def test_http_uses_existing_runner_endpoint_and_body(self):
  from unittest.mock import MagicMock
  response=MagicMock();response.__enter__.return_value.status=200
  with patch.object(h.urllib.request,'urlopen',return_value=response) as request:
   h.send_running_report('private-token',ID)
   actual=request.call_args.args[0]
   self.assertEqual(actual.method,'PUT');self.assertEqual(actual.full_url,'http://127.0.0.1:3000/api/sandbox/'+ID+'/state');self.assertEqual(json.loads(actual.data),{'state':'started'})

if __name__=='__main__':unittest.main()
