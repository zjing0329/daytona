#!/usr/bin/env python3
import hashlib,json,os,pathlib,subprocess,tempfile,unittest
SCRIPT=pathlib.Path(__file__).with_name('prepare_context.py').resolve()
class ContextTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.tmp.name)
  self.repo=self.root/'repo';self.repo.mkdir();self.out=self.root/'out';self.out.mkdir()
  subprocess.run(['git','init','-q',str(self.repo)],check=True)
  files={'apps/api/Dockerfile':'FROM node:24-slim AS builder\nENV CI=true\nFROM node:24-slim AS daytona\n','apps/api/src/example.ts':'export const value = 1\n','apps/api/.env':'DO_NOT_COPY=private\n','apps/api/.env.local':'DO_NOT_COPY=private-local\n','apps/api/.env.production':'DO_NOT_COPY=private-production\n','apps/api/.env.example':'SYNTHETIC=example\n','apps/dashboard/main.ts':'export const dashboard = 1\n','apps/unrelated/private':'DO_NOT_COPY=unrelated\n','package.json':'{}\n'}
  for name,data in files.items():
   p=self.repo/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(data)
  subprocess.run(['git','add','-A'],cwd=self.repo,check=True)
  subprocess.run(['git','-c','user.name=test','-c','user.email=test@example.com','commit','-qm','fixture'],cwd=self.repo,check=True)
  bindir=self.root/'bin';bindir.mkdir();d=bindir/'docker';d.write_text('#!/bin/sh\nexit 1\n');d.chmod(0o700)
  self.env=dict(os.environ,PATH=str(bindir)+':'+os.environ['PATH'],DSEC_V0190_NODE_BASE='node@sha256:'+'a'*64,DSEC_V0190_IMAGE_TAG='isolated:test')
 def tearDown(self):self.tmp.cleanup()
 def run_script(self):return subprocess.run(['python3',str(SCRIPT),str(self.repo),str(self.out)],env=self.env,capture_output=True,text=True)
 def test_allowlisted_context_excludes_runtime_secrets(self):
  result=self.run_script();self.assertEqual(result.returncode,0,result.stderr)
  m=json.loads((self.out/'source-manifest.json').read_text())
  self.assertEqual(set(m['excluded_runtime_environment_files']),{'apps/api/.env','apps/api/.env.local','apps/api/.env.production'})
  self.assertIn('apps/api/.env.example',m['source_sha256'])
  self.assertFalse((self.out/'context/apps/unrelated/private').exists())
  self.assertEqual(m['source_sha256']['apps/api/src/example.ts'],hashlib.sha256((self.repo/'apps/api/src/example.ts').read_bytes()).hexdigest())
 def test_existing_context_is_not_overwritten(self):
  self.assertEqual(self.run_script().returncode,0)
  sentinel=self.out/'context/sentinel';sentinel.write_text('retain')
  self.assertNotEqual(self.run_script().returncode,0)
  self.assertEqual(sentinel.read_text(),'retain')
 def test_unpinned_base_is_refused(self):
  self.env['DSEC_V0190_NODE_BASE']='node:24-slim'
  self.assertNotEqual(self.run_script().returncode,0)
  self.assertFalse((self.out/'context').exists())
 def test_tracked_symlink_is_refused(self):
  p=self.repo/'apps/api/src/example.ts';p.unlink();p.symlink_to('/etc/passwd')
  self.assertNotEqual(self.run_script().returncode,0)
if __name__=='__main__':unittest.main()
