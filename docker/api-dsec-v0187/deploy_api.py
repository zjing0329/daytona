#!/usr/bin/env python3
"""Image-only API rollout with exact compose backup and automatic image rollback."""
import argparse, datetime, json, os, subprocess, sys, time, urllib.request
from pathlib import Path
BASE='sha256:8de6315a378430a58a44ce6c20b41050c2f602446e75f3ff559edbaa0b3758a7'
ROOT=Path('/opt/codex-dsec-20260929')
HERE=Path(__file__).resolve().parent
OVERLAY=Path('/opt/daytona/docker/docker-compose.dsec-api.yaml')
def run(args):
    result=subprocess.run(args,capture_output=True,text=True)
    if result.returncode: raise RuntimeError('Command failed: '+args[0]+' '+args[1]+'; output omitted to protect configuration')
    return result.stdout

def save(path,value):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f: f.write(value);f.flush();os.fsync(f.fileno())
def inspect(name): return json.loads(run(['docker','inspect',name]))[0]
def health(seconds=120):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        try:
            with urllib.request.urlopen('http://127.0.0.1:3000/api/config',timeout=4) as r:
                if r.status==200:return
        except Exception:pass
        time.sleep(2)
    raise RuntimeError('API health did not reach HTTP 200')
def main():
    parser=argparse.ArgumentParser();parser.add_argument('image');a=parser.parse_args()
    candidate=json.loads(run(['docker','image','inspect',a.image]))[0]
    revision=run(['git','rev-parse','HEAD']).strip()
    if candidate['Config'].get('Labels',{}).get('org.opencontainers.image.revision')!=revision:raise RuntimeError('Candidate label is not the committed release revision')
    old=inspect('daytona-api-1')
    if old['Image']!=BASE:raise RuntimeError('Live API is no longer the reviewed base; stop for a fresh review')
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup=ROOT/('api-rollout-'+stamp);backup.mkdir(mode=0o700)
    save(backup/'container-before.json',json.dumps(old,indent=2))
    files=old['Config']['Labels']['com.docker.compose.project.config_files'].split(',')
    files=[f for f in files if f!=str(OVERLAY)]
    base=['docker','compose','-p','daytona','--project-directory','/opt/daytona/docker']
    for n,f in enumerate(files):
        base.extend(['-f',f]);save(backup/('compose-'+str(n)+'.yaml'),Path(f).read_text())
    resolved=json.loads(run(base+['config','--format','json']))
    save(backup/'compose-resolved-before.json',json.dumps(resolved,indent=2))
    actual=dict(x.split('=',1) for x in old['Config']['Env'] if '=' in x)
    desired=resolved['services']['api'].get('environment',{})
    if any(str(v or '')!=actual.get(k,'') for k,v in desired.items()):raise RuntimeError('Live API and compose environment differ; refusing unrelated changes')
    if OVERLAY.exists():save(backup/'previous-overlay.yaml',OVERLAY.read_text())
    original_ref='daytonaio/daytona-api@'+BASE
    original_yaml='services:\n  api:\n    image: '+original_ref+'\n'
    save(backup/'rollback-overlay.yaml',original_yaml)
    rollback_cmd=base+['-f',str(OVERLAY),'up','-d','--no-deps','--pull','never','api']
    save(backup/'rollback.json',json.dumps({'overlay':str(OVERLAY),'content':original_yaml,'command':rollback_cmd,'old_image':BASE},indent=2))
    save(backup/'rollback.py','import json,subprocess\nfrom pathlib import Path\nx=json.loads((Path(__file__).parent/"rollback.json").read_text())\nPath(x["overlay"]).write_text(x["content"])\nsubprocess.run(x["command"],check=True)\n')
    save(OVERLAY,'services:\n  api:\n    image: '+a.image+'\n')
    try:
        save(backup/'compose-up.log',run(rollback_cmd))
        health()
        new=inspect('daytona-api-1')
        if new['Image']!=candidate['Id']:raise RuntimeError('Running API image does not match reviewed candidate')
        capability=[]
        for name in ['a2','a3','a4','a5']:
            capability.append(json.loads(run([sys.executable,str(HERE/'runner_gate.py'),'capabilities',name])))
        state=[]
        for name in ['a2','a3','a4','a5']:
            state.append(json.loads(run([sys.executable,str(HERE/'runner_gate.py'),'ready',name,'--after',new['State']['StartedAt']])))
        save(backup/'container-after.json',json.dumps(new,indent=2))
        report={'status':'healthy','at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'source':revision,'before_container':old['Id'],'after_container':new['Id'],'old_image':BASE,'new_image':candidate['Id'],'image_ref':a.image,'capabilities':capability,'runners':state,'backup':str(backup),'rollback':str(backup/'rollback.py')}
        save(backup/'report.json',json.dumps(report,indent=2));print(json.dumps(report))
    except Exception as error:
        save(OVERLAY,original_yaml)
        save(backup/'automatic-rollback.log',run(rollback_cmd))
        health()
        if inspect('daytona-api-1')['Image']!=BASE:raise RuntimeError('Rollback image verification failed') from error
        print(json.dumps({'status':'rolled_back','reason':str(error),'backup':str(backup)}));raise
if __name__=='__main__':main()
