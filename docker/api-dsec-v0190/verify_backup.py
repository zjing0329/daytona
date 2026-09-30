#!/usr/bin/env python3
"""Restore a prepared production dump only into a new isolated disposable PostgreSQL."""
import argparse,datetime,hashlib,json,os,pathlib,subprocess,time,uuid
os.umask(0o077)
p=argparse.ArgumentParser()
p.add_argument('--backup',required=True);p.add_argument('--source-container-id',required=True)
p.add_argument('--expected-counts',required=True,help='JSON file with migrations/sandbox_count/job_count fields')
p.add_argument('--output',required=True)
a=p.parse_args();backup=pathlib.Path(a.backup);output=pathlib.Path(a.output)
assert backup.is_file() and not backup.is_symlink() and backup.stat().st_mode&0o077==0,'backup must be a private regular file'
assert not output.exists(),'do not overwrite restore proof'
expected=json.loads(pathlib.Path(a.expected_counts).read_text())
assert set(expected)=={'migrations','sandbox_count','job_count'} and all(isinstance(v,int) and v>=0 for v in expected.values())
assert expected['migrations']==109,'this upgrade requires v187 baseline'
assert len(a.source_container_id)==64 and all(c in '0123456789abcdef' for c in a.source_container_id)
output.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
name='dsec-v190-backup-verify-'+uuid.uuid4().hex[:12]
digest=hashlib.sha256(backup.read_bytes()).hexdigest()
r={'startedAt':datetime.datetime.now(datetime.timezone.utc).isoformat(),'backupSha256':digest,'sourceContainerId':a.source_container_id,'productionModified':False,'fixtureContainer':name}
created=False
try:
 subprocess.run(['docker','run','-d','--name',name,'--network','none','--cpus','2','--memory','4g','--tmpfs','/var/lib/postgresql:size=2g','-e','POSTGRES_PASSWORD=isolated-restore-test','-e','POSTGRES_DB=verify_test','postgres:18'],stdout=subprocess.DEVNULL,check=True,timeout=60)
 created=True
 for _ in range(30):
  if subprocess.run(['docker','exec',name,'pg_isready','-U','postgres','-d','verify_test'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:break
  time.sleep(1)
 else:raise RuntimeError('restore verification postgres not ready')
 with backup.open('rb') as f:
  result=subprocess.run(['docker','exec','-i',name,'pg_restore','--exit-on-error','--no-owner','--no-privileges','-U','postgres','-d','verify_test'],stdin=f,capture_output=True,timeout=300)
 output.with_suffix('.restore.private.log').write_bytes(result.stdout+result.stderr)
 assert result.returncode==0,'isolated pg_restore failed'
 def scalar(sql):
  return int(subprocess.check_output(['docker','exec',name,'psql','-X','-v','ON_ERROR_STOP=1','-U','postgres','-d','verify_test','-At','-c',sql],text=True,timeout=30).strip())
 restored={'migrations':scalar('SELECT count(*) FROM migrations'),'sandbox_count':scalar('SELECT count(*) FROM sandbox'),'job_count':scalar('SELECT count(*) FROM job')}
 assert restored==expected,'restored row counts differ from prepared snapshot'
 assert hashlib.sha256(backup.read_bytes()).hexdigest()==digest,'backup mutated during restore'
 r.update(status='success',restored=restored)
except BaseException as e:
 r.update(status='failed',error=type(e).__name__+': '+str(e))
finally:
 if created:subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=60)
 r['cleanupVerified']=subprocess.run(['docker','inspect',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode!=0
 if not r['cleanupVerified']:r.update(status='failed',error='isolated postgres remained')
 r['finishedAt']=datetime.datetime.now(datetime.timezone.utc).isoformat();output.write_text(json.dumps(r,indent=2))
 print(json.dumps({k:v for k,v in r.items() if k!='restored'},indent=2))
 if r['status']!='success':raise SystemExit(1)
