#!/usr/bin/env python3
"""Offline checks for the rehearsal's refusal boundaries."""
import ast,pathlib,unittest
PATH=pathlib.Path(__file__).with_name('rehearse.py')
def load_function(name,globals_):
 tree=ast.parse(PATH.read_text())
 node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
 exec(compile(ast.Module(body=[node],type_ignores=[]),str(PATH),'exec'),globals_)
 return globals_[name]
class RollbackGateTests(unittest.TestCase):
 def check(self,states,domains):
  values=iter([str(states),str(domains)])
  return load_function('rollback_safety',{'sql':lambda query:next(values)})()
 def test_expanded_schema_without_new_semantics_allows_image_rollback(self):
  self.assertTrue(self.check(0,0)['new_schema_preserved'])
 def test_paused_state_refuses_old_image(self):
  for count in [1,100]:
   with self.assertRaisesRegex(AssertionError,'pause states'):self.check(count,0)
 def test_domain_policy_refuses_old_image(self):
  for count in [1,100]:
   with self.assertRaisesRegex(AssertionError,'domain allowlist'):self.check(0,count)
 def test_query_error_is_not_a_safe_zero(self):
  def fail(query):raise RuntimeError('DB unavailable')
  with self.assertRaisesRegex(RuntimeError,'DB unavailable'):
   load_function('rollback_safety',{'sql':fail})()
if __name__=='__main__':unittest.main()
