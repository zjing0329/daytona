#!/usr/bin/env python3
"""Run the actual Runner entrypoint using fake API, network=none and a fresh volume."""
import argparse, datetime, hashlib, json, os, pathlib, subprocess, time, uuid

API_BASE = "daytonaio/daytona-api@sha256:8de6315a378430a58a44ce6c20b41050c2f602446e75f3ff559edbaa0b3758a7"
FAKE_API = r"""
const http = require('http');
const counts = {__recovery_limits:[], __recovery_pages:[], __invalid_recovery_limits:0};
http.createServer((req,res) => {
  const path = req.url.split('?')[0], key = req.method+' '+path;
  req.resume(); req.on('end', () => {
    if(path==='/__smoke_stats'){res.end(JSON.stringify(counts));return;}
    counts[key]=(counts[key]||0)+1;
    let body={};
    if(path==='/api/jobs/admission/capabilities') body={version:1,recoveryRenewal:true};
    else if(path==='/api/jobs/admission/poll') body={version:1,jobs:[]};
    else if(path==='/api/jobs') {
      const query=new URL(req.url,'http://127.0.0.1').searchParams;
      const limit=Number(query.get('limit')), page=Number(query.get('page')||1);
      counts.__recovery_limits.push(limit);counts.__recovery_pages.push(page);
      if(!Number.isInteger(limit)||limit<1||limit>200){
        counts.__invalid_recovery_limits++;res.statusCode=400;
        body={error:'limit must be between 1 and 200'};
      } else body={items:[],total:0,page,limit,totalPages:0};
    }
    else if(path==='/api/runners/healthcheck') body={};
    else {res.statusCode=404;body={error:'unsupported isolated test path'};}
    res.setHeader('Content-Type','application/json');res.end(JSON.stringify(body));
  });
}).listen(3000,'127.0.0.1');
"""
def run(*args, check=True, timeout=40):
    cp=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if check and cp.returncode:
        raise RuntimeError("Command failed: "+repr(args[:3])+" "+cp.stderr[-1200:])
    return cp
def inspect(name): return json.loads(run("docker","inspect",name).stdout)[0]
def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--image",required=True)
    parser.add_argument("--manifest",required=True)
    parser.add_argument("--output",required=True)
    parser.add_argument("--observe-seconds",type=int,default=45)
    args=parser.parse_args()
    if args.observe_seconds < 30: raise RuntimeError("Observe at least one normal healthcheck interval")
    output=pathlib.Path(args.output);output.mkdir(parents=True,exist_ok=False);os.chmod(output,0o700)
    manifest={}
    for line in pathlib.Path(args.manifest).read_text().splitlines():
        digest,name=line.split(None,1);manifest[name.strip()]=digest
    suffix=uuid.uuid4().hex[:12]
    api="dsec-smoke-api-"+suffix;runner="dsec-smoke-runner-"+suffix;volume="dsec-smoke-data-"+suffix
    created=[];volume_created=False
    result={"image":args.image,"started_at":datetime.datetime.now(datetime.timezone.utc).isoformat(),"network":"none via fake API namespace","production_credentials":False,"real_jobs":0}
    try:
        run("docker","volume","create","--label","deepdiver.purpose=isolated-runner-smoke",volume);volume_created=True
        run("docker","run","-d","--name",api,"--network","none","--cpus","0.5","--memory","512m","--entrypoint","node",API_BASE,"-e",FAKE_API);created.append(api)
        fake_request="fetch('http://127.0.0.1:3000/__smoke_stats').then(r=>r.text()).then(t=>process.stdout.write(t))"
        for _ in range(20):
            if run("docker","exec",api,"node","-e",fake_request,check=False).returncode==0: break
            time.sleep(1)
        else: raise RuntimeError("Fake API did not start")
        env={
            "DAYTONA_API_URL":"http://127.0.0.1:3000/api","DAYTONA_RUNNER_TOKEN":"isolated-smoke-token",
            "API_PORT":"3003","API_VERSION":"2","RUNNER_DOMAIN":"127.0.0.1","ENVIRONMENT":"production",
            "CONTAINER_RUNTIME":"runc","CONTAINER_NETWORK":"bridge",
            "AWS_REGION":"us-east-1","AWS_ENDPOINT_URL":"http://127.0.0.1:9000",
            "AWS_ACCESS_KEY_ID":"isolated-smoke","AWS_SECRET_ACCESS_KEY":"isolated-smoke","AWS_DEFAULT_BUCKET":"isolated-smoke",
            "OTEL_LOGGING_ENABLED":"false","OTEL_TRACING_ENABLED":"false","INITIALIZE_DAEMON_TELEMETRY":"false",
            "HEALTHCHECK_INTERVAL":"10s","POLL_TIMEOUT":"1s","VOLUME_CLEANUP_DRY_RUN":"true",
            "NODE_HEAVY_CONCURRENCY":"3","NODE_CLEANUP_CONCURRENCY":"2","NODE_PRESSURE_ENABLED":"true",
        }
        command=["docker","run","-d","--name",runner,"--privileged","--network","container:"+api,
                 "--cpus","2","--memory","2g","--restart=no","--mount","source="+volume+",target=/var/lib/docker"]
        for key,value in env.items(): command.extend(["-e",key+"="+value])
        command.append(args.image)
        run(*command);created.append(runner)
        image=json.loads(run("docker","image","inspect",args.image).stdout)[0]
        live=inspect(runner)
        assert live["Config"].get("Entrypoint")==image["Config"].get("Entrypoint")
        assert live["Config"].get("Cmd")==image["Config"].get("Cmd")
        result["real_image_entrypoint_preserved"]=True
        result["runner_id"]=live["Id"];result["temporary_volume"]=volume
        for _ in range(60):
            live=inspect(runner)
            if not live["State"]["Running"]: raise RuntimeError("Runner exited "+str(live["State"]["ExitCode"]))
            daemon=run("docker","exec",runner,"docker","info","--format","{{.ID}}",check=False,timeout=10)
            health=run("docker","exec",runner,"curl","-fsS","--max-time","4","http://127.0.0.1:3003/",check=False,timeout=10)
            if daemon.returncode==0 and daemon.stdout.strip() and health.returncode==0:
                if json.loads(health.stdout).get("status")=="ok": break
            time.sleep(2)
        else: raise RuntimeError("Inner Docker/Runner HTTP readiness timed out")
        expected={
            "/usr/local/bin/daytona-runner":manifest["dist/apps/runner-amd64"],
            "/usr/local/bin/.tmp/binaries/daemon-amd64":manifest["dist/apps/daemon-amd64"],
            "/usr/local/bin/.tmp/binaries/daytona-computer-use":manifest["dist/libs/computer-use-amd64"],
        }
        observed={}
        for path,digest in expected.items():
            actual=run("docker","exec",runner,"sha256sum",path).stdout.split()[0]
            assert actual==digest,"Runtime extracted resource hash mismatch: "+path
            observed[path]=actual
        result["runtime_binary_sha256"]=observed
        result["inner_docker_id"]=daemon.stdout.strip()
        result["runner_health"]=json.loads(health.stdout)
        end=time.monotonic()+args.observe_seconds;checks=0
        while time.monotonic()<end:
            time.sleep(5)
            live=inspect(runner)
            assert live["State"]["Running"] and live["RestartCount"]==0
            health=run("docker","exec",runner,"curl","-fsS","--max-time","4","http://127.0.0.1:3003/")
            assert json.loads(health.stdout)["status"]=="ok"
            assert not run("docker","exec",runner,"docker","ps","-aq").stdout.strip()
            checks+=1
        assert inspect(runner)["State"].get("Health",{}).get("Status")=="healthy"
        stats=json.loads(run("docker","exec",api,"node","-e",fake_request).stdout)
        assert stats.get("__recovery_limits"),"Real Runner made no startup recovery request"
        assert stats["__invalid_recovery_limits"]==0,"Runner recovery exceeded production DTO limit"
        assert all(limit==100 for limit in stats["__recovery_limits"]),"Runner recovery did not use reviewed page size 100"
        assert stats.get("GET /api/jobs/admission/capabilities",0)>0
        assert stats.get("GET /api/jobs/admission/poll",0)>0
        assert stats.get("POST /api/runners/healthcheck",0)>=3
        result.update(success=True,observe_seconds=args.observe_seconds,stable_checks=checks,restart_count=0,container_health="healthy",inner_containers=0,fake_api_counts=stats)
    except Exception as e:
        result.update(success=False,error=str(e))
    finally:
        if runner in created:
            logs=run("docker","logs",runner,check=False)
            (output/"runner.log").write_text(logs.stdout+logs.stderr);os.chmod(output/"runner.log",0o600)
        for name in reversed(created):
            run("docker","stop","--timeout","20",name,check=False,timeout=45)
            run("docker","rm",name,check=False)
        if volume_created:run("docker","volume","rm",volume,check=False)
        result["cleanup_verified"]=all(run("docker","inspect",name,check=False).returncode!=0 for name in created) and run("docker","volume","inspect",volume,check=False).returncode!=0
        result["finished_at"]=datetime.datetime.now(datetime.timezone.utc).isoformat()
        (output/"result.json").write_text(json.dumps(result,indent=2));os.chmod(output/"result.json",0o600)
        print(json.dumps(result),flush=True)
    if not result.get("success") or not result["cleanup_verified"]: raise SystemExit(1)
if __name__=="__main__":main()
