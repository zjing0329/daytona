import importlib.util,json,pathlib,tempfile,unittest
from unittest.mock import patch,Mock
p=pathlib.Path(__file__).with_name('manage_backup_limit.py');s=importlib.util.spec_from_file_location('m',p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
class Guards(unittest.TestCase):
 def proof(self,tmp,**updates):
  d=dict(runId=m.CLEAN_RUN,phase='final',cleanupPassed=True,protectedWarm80Passed=True,activeJobs=[],auditedSandboxIds=['11111111-1111-4111-8111-111111111111']);d.update(updates);p=pathlib.Path(tmp)/'proof.json';p.write_text(json.dumps(d));return p
 def test_refuses_unfinished_or_wrong_scope(self):
  for changes in [dict(phase='ready'),dict(cleanupPassed=False),dict(runId='other'),dict(protectedWarm80Passed=False),dict(activeJobs=[{}])]:
   with self.subTest(changes=changes),tempfile.TemporaryDirectory()as t,patch.object(m,'gmodule')as g:
    with self.assertRaises(AssertionError):m.cleanup_gate(self.proof(t,**changes))
    g.assert_not_called()
 def test_fresh_live_runtime_refuses(self):
  with tempfile.TemporaryDirectory()as t,patch.object(m,'gmodule',return_value=Mock(sql=Mock(return_value='1'),lit=lambda x:"'"+x+"'")):
   with self.assertRaises(AssertionError):m.cleanup_gate(self.proof(t))
 def test_fresh_active_jobs_refuse(self):
  with tempfile.TemporaryDirectory()as t,patch.object(m,'gmodule',return_value=Mock(sql=Mock(return_value='0'),lit=lambda x:"'"+x+"'")),patch.object(m,'counts',return_value={'activeJobs':1,'readyRunners':4}):
   with self.assertRaises(AssertionError):m.cleanup_gate(self.proof(t))
 def test_successful_fresh_cleanup_gate(self):
  with tempfile.TemporaryDirectory()as t,patch.object(m,'gmodule',return_value=Mock(sql=Mock(return_value='0'),lit=lambda x:"'"+x+"'")),patch.object(m,'counts',return_value={'activeJobs':0,'readyRunners':4}):
   m.cleanup_gate(self.proof(t))
 def test_repeated_apply_does_not_call_docker(self):
  with tempfile.TemporaryDirectory()as t:
   p=pathlib.Path(t)/'result';p.write_text('{}')
   with patch.object(m,'RESULT',p),patch.object(m,'inspect')as inspect:
    with self.assertRaises(AssertionError):m.apply(pathlib.Path('unused'))
    inspect.assert_not_called()
 def test_stable_refuses_image_and_mount_changes(self):
  plan={'image':'expected','mounts':[]}
  with self.assertRaises(AssertionError):m.stable(plan,{'Image':'other','Mounts':[]})
  with self.assertRaises(AssertionError):m.stable(plan,{'Image':'expected','Mounts':[{'Destination':'/data','Source':'wrong'}]})
 def test_rendered_environment_merges_image_defaults(self):
  image={'Config':{'Env':['DEFAULT=keep','OVERRIDE=old']}}
  rendered={'services':{'api':{'environment':{'OVERRIDE':'new','EXTRA':'value'}}}}
  self.assertEqual(m.rendered_env(rendered,image),{'DEFAULT':'keep','OVERRIDE':'new','EXTRA':'value'})
 def test_unresolved_environment_refuses(self):
  with self.assertRaises(AssertionError):m.rendered_env({'services':{'api':{'environment':{'UNRESOLVED':None}}}},{'Config':{'Env':[]}})
 def test_env_file_or_interpolation_drift_rejected_before_mutation(self):
  original={'services':{'api':{'environment':{'VARIABLE':'old'}}}}
  changed={'services':{'api':{'environment':{'VARIABLE':'new'}}}}
  with tempfile.TemporaryDirectory()as t:
   override=pathlib.Path(t)/'override';override.write_text('only intended change')
   plan={'baseHashes':{},'baseFiles':['base'],'overrideHash':m.hashlib.sha256(override.read_bytes()).hexdigest(),'renderedBaseHash':m.digest(original)}
   with patch.object(m,'OVERRIDE',override),patch.object(m,'config',return_value=changed),patch.object(m,'run')as shell:
    with self.assertRaisesRegex(AssertionError,'Rendered base Compose drift'):m.validate_candidate(plan)
    shell.assert_not_called()
 def test_saved_runtime_environment_must_match_rendered_base(self):
  original={'services':{'api':{'environment':{'VARIABLE':'old'}}}}
  with tempfile.TemporaryDirectory()as t:
   override=pathlib.Path(t)/'override';override.write_text('only intended change')
   plan={'baseHashes':{},'baseFiles':['base'],'overrideHash':m.hashlib.sha256(override.read_bytes()).hexdigest(),'renderedBaseHash':m.digest(original),'image':'immutable','oldEnv':{'VARIABLE':'unexpected-runtime-value'}}
   with patch.object(m,'OVERRIDE',override),patch.object(m,'config',return_value=original),patch.object(m,'run',return_value='[{"Config":{"Env":[]}}]'):
    with self.assertRaisesRegex(AssertionError,'Rendered base API environment'):m.validate_candidate(plan)
 def test_explicit_reconciliation_accepts_bound_original_and_fresh_pool(self):
  with tempfile.TemporaryDirectory()as t:
   proof=self.proof(t,cleanupPassed=False,protectedWarm80Passed=False);data={'original_final_sha256':m.hashlib.sha256(proof.read_bytes()).hexdigest(),'current_warm_rows':[{'id':'expected'}]};module=Mock(validate_reconciliation=Mock(return_value=data));g=Mock(sql=Mock(side_effect=['0','[]']),lit=lambda x:"'"+x+"'")
   with patch.object(m,'reconciliation_module',return_value=module),patch.object(m,'gmodule',return_value=g),patch.object(m,'counts',return_value={'activeJobs':0,'readyRunners':4}):m.cleanup_gate(proof,pathlib.Path('explicit-proof'))
   module.validate_current_warm_rows.assert_called_once_with([],data['current_warm_rows'])
 def test_reconciliation_original_hash_mismatch_refused(self):
  with tempfile.TemporaryDirectory()as t:
   proof=self.proof(t,cleanupPassed=False,protectedWarm80Passed=False);module=Mock(validate_reconciliation=Mock(return_value={'original_final_sha256':'wrong'}))
   with patch.object(m,'reconciliation_module',return_value=module),patch.object(m,'gmodule')as g:
    with self.assertRaises(AssertionError):m.cleanup_gate(proof,pathlib.Path('explicit-proof'))
    g.assert_not_called()
 def test_reconciliation_fresh_pool_drift_refused(self):
  with tempfile.TemporaryDirectory()as t:
   proof=self.proof(t,cleanupPassed=False,protectedWarm80Passed=False);module=Mock(validate_reconciliation=Mock(return_value={'original_final_sha256':m.hashlib.sha256(proof.read_bytes()).hexdigest(),'current_warm_rows':[]}),validate_current_warm_rows=Mock(side_effect=AssertionError('drift')))
   with patch.object(m,'reconciliation_module',return_value=module),patch.object(m,'gmodule',return_value=Mock(sql=Mock(side_effect=['0','[]']),lit=lambda x:"'"+x+"'")),patch.object(m,'counts')as counts:
    with self.assertRaises(AssertionError):m.cleanup_gate(proof,pathlib.Path('explicit-proof'))
    counts.assert_not_called()
if __name__=='__main__':unittest.main()
