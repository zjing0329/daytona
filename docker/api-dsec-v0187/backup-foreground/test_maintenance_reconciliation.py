import copy,datetime,hashlib,importlib.util,json,pathlib,tempfile,unittest,uuid
p=pathlib.Path(__file__).with_name('maintenance_reconciliation.py');sp=importlib.util.spec_from_file_location('v',p);v=importlib.util.module_from_spec(sp);sp.loader.exec_module(v)
class Evidence(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.tmp.name);self.addCleanup(self.tmp.cleanup)
  now=datetime.datetime.now(datetime.timezone.utc);t=now.isoformat();birth=(now-datetime.timedelta(seconds=21601)).isoformat();sid=lambda n:str(uuid.UUID(int=n));old=[sid(i)for i in range(1,81)];current=[sid(i)for i in range(81,161)];tests=[sid(i)for i in range(201,601)];apps=[sid(i)for i in range(1001,1201)]
  def row(i,state):return dict(id=i,state=state,desiredState=state,runnerId=sid(9999),snapshot='template',pool_profile='default',pool_state='idle',pool_use_count='0',app_id=None,dd_app_id=None,session_id=None,closed_app_id=None,skill_bundle_version='same',warm_baseline_sha256='same')
  jobs=[dict(id=kind+i,type=kind,resourceId=i,status='COMPLETED',createdAt=t,completedAt=t)for kind in ['CREATE_SANDBOX','DESTROY_SANDBOX']for i in tests]
  self.docs={'manifest':dict(run_id=v.RUN,status='pass',finished_at=t,identity_cleanup=True,apps=[dict(app_id=i,cleanup={'passed':True},restore={'passed':True})for i in apps]),'baseline':{'samples':{'a1':{'rows':[{'id':i}for i in old]}}},'original_final':dict(runId=v.RUN,phase='final',at=t,testAppCount=200,protectedWarm80Passed=False,cleanupPassed=False,manifestCoveragePassed=True,testTerminalStatesPassed=True,claimUniquenessPassed=True,activeJobs=[],duplicateExecutions=[],missingRequiredLifecycleJobs=[],missingLifecycleExecutionCompletionEvidence=[],auditedSandboxIds=tests,runtimeCountPerAppDistribution={'2':200},jobs=jobs,failedJobs=[],protectedWarm=[{'id':i}for i in old]),'current_pool':dict(at=t,current=[row(i,'started')for i in current],original=[row(i,'destroyed')for i in old],failedBackupCategories=[]),'delete_audit':dict(poolMetadata=[dict(id=i,pool_profile='default',pool_use_count='0',pool_created_at=birth,createdAt=birth)for i in old],destroyAudits=[dict(targetId=i,actorFingerprint='same',action='delete',statusCode=200,ipAddress='192.168.0.225',createdAt=t)for i in old]),'live_evidence':dict(ttlSeconds=21600,liveRuntimeMatchesSource=True,sourceRetiresMaxAge=True,replacementCreatedIds=current,sourceEvidence=[{'path':'source.py','sha256':'0'*64}],retirementLogEvidence={'exact_old_ids_with_direct_reason':0})}
  self.proof=dict(schemaVersion=1,kind='natural-live-warm-pool-rotation',runId=v.RUN,at=t,original80Unchanged=False,ttlSeconds=21600,currentWarmIds=sorted(current),currentWarmIdentitySha256=v.identity_hash(current),originalWarmIdentitySha256=v.identity_hash(old))
 def write(self):
  self.proof['evidence']={}
  for name,d in self.docs.items():
   data=json.dumps(d).encode();(self.root/(name+'.json')).write_bytes(data);self.proof['evidence'][name]={'path':name+'.json','sha256':v.sha(data)}
  p=self.root/'proof.json';p.write_text(json.dumps(self.proof));return p
 def verify(self):return v.validate_reconciliation(self.write(),expected_run_id=v.RUN)
 def test_valid_explicit_reconciliation(self):
  result=self.verify();self.assertTrue(result['test_cleanup_passed']);self.assertFalse(result['original80_unchanged']);self.assertEqual(len(result['current_warm_ids']),80)
 def test_evidence_hash_mismatch(self):
  p=self.write();(self.root/'manifest.json').write_text('{}')
  with self.assertRaises(AssertionError):v.validate_reconciliation(p,expected_run_id=v.RUN)
 def test_wrong_run(self):
  self.proof['runId']='other'
  with self.assertRaises(AssertionError):self.verify()
 def test_original_false_cannot_be_rewritten_true(self):
  self.docs['original_final']['protectedWarm80Passed']=True
  with self.assertRaises(AssertionError):self.verify()
 def test_early_retirement_rejected(self):
  self.docs['delete_audit']['poolMetadata'][0]['pool_created_at']=self.proof['at']
  with self.assertRaises(AssertionError):self.verify()
 def test_missing_replacement_creation_provenance(self):
  self.docs['live_evidence']['replacementCreatedIds'].pop()
  with self.assertRaises(AssertionError):self.verify()
 def test_bound_pool_runtime_rejected(self):
  self.docs['current_pool']['current'][0]['app_id']='bound'
  with self.assertRaises(AssertionError):self.verify()
 def test_extra_live_runtime_rejected(self):
  extra=dict(self.docs['current_pool']['current'][0],id=str(uuid.uuid4()));self.docs['current_pool']['current'].append(extra)
  with self.assertRaises(AssertionError):self.verify()
 def test_snapshot_change_rejected(self):
  self.docs['current_pool']['current'][0]['snapshot']='different'
  with self.assertRaises(AssertionError):self.verify()
 def test_incomplete_test_cleanup_rejected(self):
  self.docs['original_final']['jobs'].pop()
  with self.assertRaises(AssertionError):self.verify()
 def test_stale_proof_rejected(self):
  self.proof['at']=(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(seconds=901)).isoformat()
  with self.assertRaises(AssertionError):self.verify()
 def test_evidence_path_escape_rejected(self):
  p=self.write();self.proof['evidence']['manifest']['path']='../outside';p.write_text(json.dumps(self.proof))
  with self.assertRaises(AssertionError):v.validate_reconciliation(p,expected_run_id=v.RUN)
 def test_unknown_backup_error_rejected(self):
  self.docs['original_final']['failedJobs']=[dict(id='backup',type='CREATE_BACKUP',resourceId=self.docs['original_final']['auditedSandboxIds'][0],completedAt=self.proof['at'])];self.docs['current_pool']['failedBackupCategories']=[dict(id='backup',resourceId=self.docs['original_final']['auditedSandboxIds'][0],category='storage_failure')]
  with self.assertRaises(AssertionError):self.verify()
 def test_failed_cleanup_dict_rejected(self):
  self.docs['manifest']['apps'][0]['cleanup']={'passed':False}
  with self.assertRaises(AssertionError):self.verify()
 def test_owner_cleanup_missing_rejected(self):
  self.docs['manifest']['identity_cleanup']=False
  with self.assertRaises(AssertionError):self.verify()
 def test_postgres_short_fraction_python310_compatible(self):
  self.assertEqual(v.stamp('2026-09-30T06:31:19.09+00:00').microsecond,90000)
 def test_naive_timestamp_rejected(self):
  with self.assertRaises(AssertionError):v.stamp('2026-09-30T06:31:19')
 def test_manifest_run_identity_mismatch_rejected(self):
  self.docs['manifest']['run_id']='another-run'
  with self.assertRaises(AssertionError):self.verify()
if __name__=='__main__':unittest.main()
