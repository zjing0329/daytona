#!/usr/bin/env python3
"""HTTP regression against the isolated v0.187 test stack only."""
import json, subprocess
API='daytona-dsec-v0187-health'
DB='daytona-dsec-v0187-test-db'
ID='00000000-0000-4000-8000-000000000001'
def sql(q):
    return subprocess.run(['docker','exec',DB,'psql','-v','ON_ERROR_STOP=1','-U','postgres','-d','admission_test','-At','-c',q],capture_output=True,text=True,check=True).stdout.strip()
def request(path,method='GET'):
    r=subprocess.run(['docker','exec',API,'curl','-sS','--max-time','10','-w','\n%{http_code}','-X',method,'-H','Authorization: Bearer isolated-runner-key','http://127.0.0.1:3000'+path],capture_output=True,text=True,check=True)
    body,status=r.stdout.rsplit('\n',1)
    return int(status),json.loads(body)
assert request('/api/config')[0]==200
assert request('/api/jobs/admission/capabilities')==(200,{'version':1,'recoveryRenewal':True})
assert request('/api/jobs/admission/poll?class=cleanup&limit=1')==(200,{'version':1,'jobs':[]})
assert request('/api/jobs/admission/poll?class=invalid&limit=1')[0]==400
sql(f"""INSERT INTO job (id,version,type,status,"runnerId","resourceType","resourceId",payload,"startedAt","createdAt","updatedAt") SELECT '{ID}',1,'CREATE_SANDBOX','IN_PROGRESS',id::text,'SANDBOX','isolated-dsec-recovery','{{}}',NOW(),NOW(),NOW() FROM runner WHERE name='isolated-test' ON CONFLICT(id) DO UPDATE SET status='IN_PROGRESS',"completedAt"=NULL,"updatedAt"=NOW()""")
try:
    status,result=request('/api/jobs/admission/recover/'+ID,'POST')
    assert status==200,(status,result)
    assert result['version']==1 and result['job']['id']==ID and result['job']['status']=='IN_PROGRESS'
    sql(f"UPDATE job SET status='FAILED',\"completedAt\"=NOW() WHERE id='{ID}'")
    assert request('/api/jobs/admission/recover/'+ID,'POST')==(200,{'version':1,'job':None})
    assert sql(f"SELECT status::text FROM job WHERE id='{ID}'")=='FAILED'
finally:
    sql(f"DELETE FROM job WHERE id='{ID}'")
print('PASS: health, capability, class poll, invalid query, recovery HTTP 200, terminal no-revive (6 checks)')
