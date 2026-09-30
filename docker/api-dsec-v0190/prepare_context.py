#!/usr/bin/env python3
"""Copy tracked build inputs without runtime secrets; pin both upstream Node stages."""
import datetime,hashlib,json,os,pathlib,re,subprocess,sys
os.umask(0o077)
source=pathlib.Path(sys.argv[1]).resolve();out=pathlib.Path(sys.argv[2]).resolve()
context=out/'context'
assert not context.exists(),'context already exists; use a new output path'
tag=os.environ['DSEC_V0190_IMAGE_TAG'];base=os.environ['DSEC_V0190_NODE_BASE']
assert re.fullmatch(r'node@sha256:[0-9a-f]{64}',base),base
probe=subprocess.run(['docker','image','inspect',tag],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
assert probe.returncode!=0,'image tag already exists'
context.mkdir(mode=0o700)
paths=subprocess.check_output(['git','ls-files','-z'],cwd=source).decode().split('\0')
prefixes=('apps/api/','apps/dashboard/','libs/runner-api-client/','libs/api-client/','libs/analytics-api-client/','libs/toolbox-api-client/','libs/sdk-typescript/','libs/runner-proto/','libs/billing-api-client/')
rootfiles={'package.json','yarn.lock','.yarnrc.yml','nx.json','tsconfig.base.json','.nxignore'}
hashes={};excluded=[]
for name in paths:
 if not name or not(name in rootfiles or name.startswith(prefixes)):continue
 p=pathlib.Path(name)
 if p.name in {'.env','.env.local'} or p.name.startswith('.env.') and not p.name.endswith('.example'):
  excluded.append(name);continue
 src=source/name;dest=context/name
 assert not src.is_symlink(),'unexpected build source symlink: '+name
 data=src.read_bytes();dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(data)
 hashes[name]=hashlib.sha256(data).hexdigest()
dockerfile=(source/'apps/api/Dockerfile').read_text()
assert dockerfile.count('FROM node:24-slim AS ')==2,'upstream Dockerfile stages changed'
pinned=dockerfile.replace('FROM node:24-slim AS ','FROM '+base+' AS ')
pinned=pinned.replace('ENV CI=true','ENV CI=true\nENV NX_NO_CLOUD=true',1)
(out/'Dockerfile.pinned').write_text(pinned)
receipt={'created_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'source_head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True).strip(),'candidate_working_tree':True,'image_tag':tag,'node_base_digest':base,'upstream_dockerfile_sha256':hashlib.sha256(dockerfile.encode()).hexdigest(),'pinned_dockerfile_sha256':hashlib.sha256(pinned.encode()).hexdigest(),'recipe_adjustments':['pin identical Node24 digest for both FROM stages','set NX_NO_CLOUD=true to prevent offline cloud client failure'],'excluded_runtime_environment_files':excluded,'source_sha256':hashes}
(out/'source-manifest.json').write_text(json.dumps(receipt,indent=2))
