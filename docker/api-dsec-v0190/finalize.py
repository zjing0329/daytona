#!/usr/bin/env python3
"""Bind the validated full v190 image to a final clean source commit, without rebuilding code."""
import argparse,datetime,hashlib,json,os,pathlib,re,subprocess,uuid
os.umask(0o077)
p=argparse.ArgumentParser()
p.add_argument('--build-dir',required=True);p.add_argument('--source-revision',required=True)
p.add_argument('--test-result',required=True);p.add_argument('--rehearsal-result',required=True)
p.add_argument('--image-ref',required=True)
a=p.parse_args();build=pathlib.Path(a.build_dir).resolve()
source=pathlib.Path(subprocess.check_output(['git','rev-parse','--show-toplevel'],text=True).strip())
rev=a.source_revision
assert re.fullmatch('[0-9a-f]{40}',rev)
assert re.fullmatch('[a-z0-9][a-z0-9/_.:-]+',a.image_ref)
def run(args,**kw):return subprocess.check_output(args,text=True,timeout=180,**kw).strip()
def sha(data):return hashlib.sha256(data).hexdigest()
assert run(['git','rev-parse','HEAD'])==rev
assert not run(['git','status','--porcelain']),'final source must be clean'
manifest=json.loads((build/'source-manifest.json').read_text())
tests=json.loads(pathlib.Path(a.test_result).read_text());rehearsal=json.loads(pathlib.Path(a.rehearsal_result).read_text())
assert tests['status']=='pass' and rehearsal['status']=='pass'
assert rehearsal['cleanup']['verified'],'rehearsal resources not cleaned'
assert tests['source_manifest_sha256']==sha((build/'source-manifest.json').read_bytes())
assert rehearsal['new_image']==manifest['image_tag'],'rehearsal used another candidate'
prefixes=('apps/api/','apps/dashboard/','libs/runner-api-client/','libs/api-client/','libs/analytics-api-client/','libs/toolbox-api-client/','libs/sdk-typescript/','libs/runner-proto/','libs/billing-api-client/')
rootfiles={'package.json','yarn.lock','.yarnrc.yml','nx.json','tsconfig.base.json','.nxignore'}
paths=[]
for name in run(['git','ls-files']).splitlines():
 if not(name in rootfiles or name.startswith(prefixes)):continue
 f=pathlib.Path(name)
 if f.name in {'.env','.env.local'} or f.name.startswith('.env.') and not f.name.endswith('.example'):continue
 paths.append(name)
assert set(paths)==set(manifest['source_sha256']),'final build input path set changed'
for name,digest in manifest['source_sha256'].items():
 assert sha((source/name).read_bytes())==digest,name
 assert sha(subprocess.check_output(['git','show',rev+':'+name]))==digest,name
candidate=json.loads(run(['docker','image','inspect',manifest['image_tag']]))[0]
assert candidate['Id']==tests['builder_runtime_candidate_id'],'candidate tag was replaced'
assert any(e.startswith('VERSION=v0.190.0-') for e in candidate['Config']['Env']),'not a full v190 runtime candidate'
assert subprocess.run(['docker','image','inspect',a.image_ref],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode!=0,'final image tag already exists'
out=build/('final-'+rev[:12]);assert not out.exists(),'final receipt already exists';out.mkdir(mode=0o700)
version='v0.190.0-dsec-'+rev[:12]
temporary='deepdiver/daytona-api-final-base:'+rev[:12]+'-'+uuid.uuid4().hex[:8]
# A validated temporary tag is needed: BuildKit interprets FROM sha256:<id> as a remote repo.
run(['docker','tag',candidate['Id'],temporary])
try:
 assert json.loads(run(['docker','image','inspect',temporary]))[0]['Id']==candidate['Id']
 dockerfile='FROM '+temporary+'\nENV VERSION='+version+'\nLABEL org.opencontainers.image.revision="'+rev+'" org.opencontainers.image.version="'+version+'" io.deepdiver.api.full-build-base="'+candidate['Id']+'"\n'
 (out/'Dockerfile').write_text(dockerfile)
 result=subprocess.run(['docker','build','--network=none','--pull=false','--progress=plain','-f',str(out/'Dockerfile'),'-t',a.image_ref,str(out)],capture_output=True,text=True,timeout=180)
 (out/'package.log').write_text(result.stdout+result.stderr);assert result.returncode==0,'metadata image packaging failed'
 final=json.loads(run(['docker','image','inspect',a.image_ref]))[0]
 assert final['RootFS']['Layers']==candidate['RootFS']['Layers'],'final image filesystem changed'
 assert final['Config']['Labels']['org.opencontainers.image.revision']==rev
 assert 'VERSION='+version in final['Config']['Env']
finally:
 subprocess.run(['docker','image','rm',temporary],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
inspect_code=r"""const fs=require('fs'),p=require('path'),crypto=require('crypto'),r={};function visit(x){if(!fs.existsSync(x))return;const s=fs.statSync(x);if(s.isDirectory()){for(const n of fs.readdirSync(x).sort())visit(p.join(x,n));}else r[x]=crypto.createHash('sha256').update(fs.readFileSync(x)).digest('hex');}for(const x of ['/daytona/package.json','/daytona/yarn.lock','/daytona/apps/api/src','/daytona/dist/apps/api','/daytona/dist/apps/dashboard'])visit(x);process.stdout.write(JSON.stringify(r));"""
before=json.loads(run(['docker','run','--rm','--network','none','--entrypoint','node',candidate['Id'],'-e',inspect_code]))
after=json.loads(run(['docker','run','--rm','--network','none','--entrypoint','node',final['Id'],'-e',inspect_code]))
assert before==after and before,'full runtime files differ'
assert any('1781740800000-migration' in x for x in after),'new migrations absent'
assert any('/dist/apps/dashboard/' in x for x in after),'new dashboard absent'
receipt={'status':'success','sourceRevision':rev,'imageId':final['Id'],'imageRef':a.image_ref,'candidateImageId':candidate['Id'],'version':version,'finishedAt':datetime.datetime.now(datetime.timezone.utc).isoformat(),'productionDeployed':False,'sourceClean':True,'buildInputCount':len(paths),'fullUpstreamRecipe':True,'nodeBaseDigest':manifest['node_base_digest'],'runtimeFileCount':len(after),'runtimeFilesSha256':after,'evidenceSha256':{'sourceManifest':sha((build/'source-manifest.json').read_bytes()),'tests':sha(pathlib.Path(a.test_result).read_bytes()),'rehearsal':sha(pathlib.Path(a.rehearsal_result).read_bytes())}}
(out/'image-receipt.json').write_text(json.dumps(receipt,indent=2))
print(json.dumps({k:v for k,v in receipt.items() if k!='runtimeFilesSha256'},indent=2))
