#!/usr/bin/env python3
"""Run without production access; optional Lua integration only on the named isolated test container."""
import importlib.util,json,os,subprocess,tempfile,unittest
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
 def test_private_state_and_owner_not_in_status(self):
  record={'name':'a2','runnerId':RUNNER,'protectedIds':[ID],'owner':'secret-owner'}
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'lease.json';h.write(p,record);self.assertEqual(p.stat().st_mode&0o777,0o600)
  with patch.object(h,'redis',return_value=[-1,1]):
   result=h.summary(record);self.assertTrue(result['latched']);self.assertTrue(result['owned']);self.assertNotIn('secret-owner',json.dumps(result))
  with patch.object(h,'redis',return_value=[-2,0]):self.assertFalse(h.summary(record)['owned'])
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
if __name__=='__main__':unittest.main()
