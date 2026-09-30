const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const ts=require('/source/node_modules/typescript');
const root='/source/apps/api/src';
function source(p){return ts.createSourceFile(p,fs.readFileSync(root+'/'+p,'utf8'),ts.ScriptTarget.Latest,true)}
function method(p,name){const sf=source(p);let found;function walk(n){if(ts.isMethodDeclaration(n)&&n.name.getText(sf)===name)found=n.getText(sf);ts.forEachChild(n,walk)}walk(sf);assert.ok(found,name);return found}
function klass(name,methods,context){const code=ts.transpile('class '+name+' {'+methods.join('\n')+'}\nthis.Result='+name,{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS});vm.runInNewContext(code,context);return context.Result}
const BackupState={PENDING:'Pending',IN_PROGRESS:'InProgress',COMPLETED:'Completed',ERROR:'Error'};
const config=source('config/configuration.ts');let expression;
function walk(n){if(ts.isPropertyAssignment(n)&&n.name.getText(config)==='maxConcurrentBackupsPerRunner')expression=n.initializer.getText(config);ts.forEachChild(n,walk)}walk(config);assert.ok(expression);
function configured(value){return vm.runInNewContext(expression,{process:{env:value===undefined?{}:{MAX_CONCURRENT_BACKUPS_PER_RUNNER:value}},parseInt})}
assert.equal(configured(undefined),6);assert.equal(configured('1'),1);
const C=klass('BackupManager',[method('sandbox/managers/backup.manager.ts','handlePendingBackup'),method('sandbox/managers/backup.manager.ts','markBackupInProgressIfPending')],{BackupState,SandboxConflictError:class extends Error{},sanitizeSandboxError:e=>({errorReason:String(e),recoverable:false}),SandboxDesiredState:{ARCHIVED:'archived'}});
const A=klass('Adapter',[method('sandbox/runner-adapter/runnerAdapter.v2.ts','createBackup')],{JobType:{CREATE_BACKUP:'CREATE_BACKUP'},ResourceType:{SANDBOX:'sandbox'}});
const results=[];
async function scenario(limit,n,existing=0){
 const rows=Array.from({length:n+existing},(_,i)=>({id:'s'+i,runnerId:'runner',backupState:i<existing?BackupState.IN_PROGRESS:BackupState.PENDING,backupRegistryId:'registry',backupSnapshot:'fixture',desiredState:'started'}));
 let locked=false;const waiters=[],jobs=[];const c=new C();
 c.redisLockProvider={waitForLock:async()=>{if(locked)await new Promise(r=>waiters.push(r));locked=true},unlock:async()=>{const next=waiters.shift();if(next)next();else locked=false}};
 c.sandboxRepository={count:async()=>rows.filter(x=>x.backupState===BackupState.IN_PROGRESS).length,updateWhere:async(id,{updateData,whereCondition})=>{const r=rows.find(x=>x.id===id);assert.equal(r.backupState,whereCondition.backupState);Object.assign(r,updateData)}};
 c.configService={getOrThrow:key=>{assert.equal(key,'maxConcurrentBackupsPerRunner');return limit}};
 c.dockerRegistryService={findOne:async()=>({id:'registry',project:'test',url:'http://invalid.test',username:'synthetic',password:'synthetic'})};
 c.runnerService={findOneOrFail:async()=>({id:'runner'})};
 const a=new A();a.runner={id:'runner'};a.logger={debug:()=>{}};a.jobService={createJob:async(_,type,runnerId,resourceType,id)=>{assert.equal(type,'CREATE_BACKUP');assert.equal(runnerId,'runner');jobs.push({id,status:'PENDING'});await Promise.resolve()}};
 a.sandboxInfo=async()=>({backupState:'None'});c.runnerAdapterFactory={create:async()=>a};c.logger={debug:()=>{}};c.runnerIsDraining=async()=>false;c.markErroredIfDraining=async()=>{};
 await Promise.all(rows.slice(existing).map(r=>c.handlePendingBackup(r)));
 return{c,rows,jobs};
}
(async()=>{
 let s=await scenario(configured('1'),8);assert.equal(s.jobs.length,1);assert.equal(s.jobs[0].status,'PENDING');assert.equal(s.rows.filter(x=>x.backupState===BackupState.IN_PROGRESS).length,1);results.push('one pending v2 job already occupies the configured slot');
 await s.c.handlePendingBackup(s.rows[1]);assert.equal(s.jobs.length,1);results.push('in-progress backup prevents a second submission');
 s.rows[0].backupState=BackupState.COMPLETED;await s.c.handlePendingBackup(s.rows[1]);assert.equal(s.jobs.length,2);results.push('normal completion admits the next backup');
 s=await scenario(1,4,6);assert.equal(s.jobs.length,0);results.push('existing backlog above lowered limit is preserved and blocks new submissions');
 s=await scenario(6,8);assert.equal(s.jobs.length,6);results.push('original default six is unchanged');
 console.log(JSON.stringify({passed:true,checks:results,defaultLimit:configured(undefined),candidateLimit:configured('1'),source:'Exact existing TypeScript methods transpiled without API bootstrap',boundary:'Simulates successful lock ownership only; existing 10-second Redis TTL expiry and direct Runner HTTP bypass are not converted into a hard execution guarantee.'}));
})().catch(e=>{console.error(e);process.exitCode=1});
