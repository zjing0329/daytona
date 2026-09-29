// Isolated actual-service/repository tests. PG and Redis integration require explicit test URLs.
require('reflect-metadata')
require('ts-node').register({transpileOnly:true,compilerOptions:{module:'commonjs',target:'ES2022',experimentalDecorators:true,emitDecoratorMetadata:true}})
require('tsconfig-paths').register({baseUrl:process.cwd(),paths:require(process.cwd()+'/tsconfig.base.json').compilerOptions.paths})
const {test}=require('node:test'),assert=require('node:assert/strict')
const {assertRunnerDeleteAllowed,RunnerDeleteMaintenanceError,runnerDeleteMaintenanceKey}=require('../../common/errors/runner-delete-maintenance.error.ts')
const {SandboxService}=require('./sandbox.service.ts')
const {SandboxRepository}=require('../repositories/sandbox.repository.ts')
const {SandboxController}=require('../controllers/sandbox.controller.ts')
const {AllExceptionsFilter}=require('../../filters/all-exceptions.filter.ts')
const {SandboxEvents}=require('../constants/sandbox-events.constants.ts')
const START={id:'00000000-0000-4000-8000-000000000021',name:'isolated',runnerId:'00000000-0000-4000-8000-000000000031',state:'started',desiredState:'started',pending:false,backupState:'Completed',organizationId:'test-org'}
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve}}
function fixture(redis){
 const stats={writes:0,events:[],committed:false,validations:0}
 let row={...START}
 const repo=Object.create(SandboxRepository.prototype)
 repo.emitUpdateEvents=()=>stats.events.push('repository-event');repo.invalidateLookupCacheOnUpdate=()=>{}
 Object.defineProperty(repo,'manager',{value:{transaction:async work=>{
  const entity={...row,assertValid:()=>stats.validations++,enforceInvariants:()=>({})}
  const result=await work({findOne:async(_target,q)=>{assert.equal(q.lock.mode,'pessimistic_write');return entity},update:async(_target,_id,patch)=>{stats.writes++;row={...row,...patch}}})
  stats.committed=true;return result
 }}})
 const service=Object.create(SandboxService.prototype)
 service.redis=redis;service.findOneByIdOrName=async()=>({...row});service.sandboxForkRepository={find:async()=>[]};service.sandboxRepository=repo
 service.eventEmitter={emit:name=>{if(name===SandboxEvents.DESTROYED)assert.equal(stats.committed,true,'DESTROYED must follow transaction commit');stats.events.push(name)}}
 return {service,repo,stats,row:()=>row}
}
test('only Redis explicit missing key allows DELETE; persistent/invalid/error leases fail closed',async()=>{
 for(const ttl of [-1,0,1,300000,-3,NaN,Infinity,undefined])await assert.rejects(assertRunnerDeleteAllowed({pttl:async()=>ttl},START.runnerId),RunnerDeleteMaintenanceError)
 await assertRunnerDeleteAllowed({pttl:async()=>-2},START.runnerId)
 await assert.rejects(assertRunnerDeleteAllowed({pttl:async()=>{throw Error('redis unavailable')}},START.runnerId),e=>e.getStatus()===503)
 await assertRunnerDeleteAllowed({pttl:async()=>{throw Error('must not be called')}},null)
})
test('unresponsive Redis has a bounded wait and no mutation or event',async()=>{
 const f=fixture({pttl:()=>new Promise(()=>{})});const start=Date.now()
 await assert.rejects(f.service.destroy(START.id),RunnerDeleteMaintenanceError)
 assert.ok(Date.now()-start<3500);assert.equal(f.stats.writes,0);assert.deepEqual(f.stats.events,[])
})
test('maintenance denial never mutates, validates or emits destruction',async()=>{
 for(const redis of [{pttl:async()=>300000},{pttl:async()=>-1},{pttl:async()=>{throw Error('offline')}}]){
  const f=fixture(redis);await assert.rejects(f.service.destroy(START.id),RunnerDeleteMaintenanceError)
  assert.deepEqual(f.row(),START);assert.deepEqual(f.stats,{writes:0,events:[],committed:false,validations:0})
 }
})
test('DELETE rechecks after row lock using fresh runner; rejected callback precedes all mutation/events',async()=>{
 let calls=0;const f=fixture({pttl:async()=>++calls===1?-2:300000})
 await assert.rejects(f.service.destroy(START.id),RunnerDeleteMaintenanceError)
 assert.equal(calls,2);assert.equal(f.stats.writes,0);assert.equal(f.stats.validations,0);assert.deepEqual(f.stats.events,[]);assert.deepEqual(f.row(),START)
})
test('normal deletion preserves soft-delete fields and emits DESTROYED only after commit',async()=>{
 const f=fixture({pttl:async()=>-2});const result=await f.service.destroy(START.id)
 assert.equal(result.desiredState,'destroyed');assert.equal(result.pending,true);assert.equal(result.backupState,'None');assert.match(result.name,/^DESTROYED_/)
 assert.equal(f.stats.writes,1);assert.ok(f.stats.events.includes(SandboxEvents.DESTROYED))
})
test('optional repository callback leaves existing callers unchanged',async()=>{
 const f=fixture({pttl:async()=>-2});await f.repo.updateWhere(START.id,{updateData:{name:'renamed'},whereCondition:{state:'started'}})
 assert.equal(f.row().name,'renamed');assert.equal(f.stats.writes,1)
})
test('actual PostgreSQL row barrier and real Redis lease reject in-flight DELETE; HTTP returns 503/Retry-After then release/expiry restore deletion', {skip:!process.env.DSEC_TEST_DATABASE||!process.env.DSEC_TEST_REDIS},async()=>{
 const {Pool}=require('pg'),Redis=require('ioredis'),{Controller,Delete,Module}=require('@nestjs/common'),{NestFactory}=require('@nestjs/core')
 const pool=new Pool({connectionString:process.env.DSEC_TEST_DATABASE}),redis=new Redis(process.env.DSEC_TEST_REDIS)
 const key=runnerDeleteMaintenanceKey(START.runnerId),owner='isolated-maintenance-owner',other='wrong-owner'
 const table='delete_maintenance_test'
 let app
 try{
  await pool.query(`CREATE TABLE IF NOT EXISTS ${table}(id uuid PRIMARY KEY,name text,"runnerId" uuid,state text,"desiredState" text,pending boolean,"backupState" text,"organizationId" text)`)
  const reset=async()=>{await pool.query(`DELETE FROM ${table}`);await pool.query(`INSERT INTO ${table} VALUES($1,$2,$3,$4,$5,$6,$7,$8)`,Object.values(START));await redis.del(key)}
  await reset()
  const stats={writes:0,events:[],committed:false};let beforeSelect,beforeWrite
  const repo=Object.create(SandboxRepository.prototype)
  repo.emitUpdateEvents=()=>stats.events.push('repository-event');repo.invalidateLookupCacheOnUpdate=()=>{}
  Object.defineProperty(repo,'manager',{value:{transaction:async work=>{
   const client=await pool.connect();await client.query('BEGIN')
   try{
    const result=await work({findOne:async()=>{
     beforeSelect?.resolve()
     const {rows}=await client.query(`SELECT * FROM ${table} WHERE id=$1 FOR UPDATE`,[START.id])
     return Object.assign(rows[0],{assertValid(){},enforceInvariants(){return {}}})
    },update:async(_target,_id,patch)=>{
     if(beforeWrite)await beforeWrite()
     await client.query(`UPDATE ${table} SET name=$1,"desiredState"=$2,pending=$3,"backupState"=$4 WHERE id=$5`,[patch.name,patch.desiredState,patch.pending,patch.backupState,START.id]);stats.writes++
    }})
    await client.query('COMMIT');stats.committed=true;return result
   }catch(e){await client.query('ROLLBACK');throw e}finally{client.release()}
  }}})
  const service=Object.create(SandboxService.prototype)
  service.redis=redis;service.findOneByIdOrName=async()=> (await pool.query(`SELECT * FROM ${table} WHERE id=$1`,[START.id])).rows[0]
  service.sandboxForkRepository={find:async()=>[]};service.sandboxRepository=repo;service.toSandboxDto=async value=>value
  service.eventEmitter={emit:name=>{assert.equal(stats.committed,true);stats.events.push(name)}}
  // Delayed DELETE passed the entry check, then waits for the maintenance row barrier.
  const gate=await pool.connect();await gate.query('BEGIN');await gate.query(`SELECT id FROM ${table} WHERE id=$1 FOR UPDATE`,[START.id])
  beforeSelect=deferred();const inFlight=service.destroy(START.id);const rejected=assert.rejects(inFlight,RunnerDeleteMaintenanceError)
  await beforeSelect.promise;await redis.set(key,owner,'PX',300000,'NX')
  const {rows}=await gate.query(`SELECT "desiredState" FROM ${table} WHERE id=$1`,[START.id]);assert.equal(rows[0].desiredState,'started')
  await gate.query('COMMIT');gate.release();await rejected
  assert.equal(stats.writes,0);assert.deepEqual(stats.events,[])
  // Actual HTTP Nest route delegates to the production controller method and production exception filter.
  class HttpController{async remove(){return SandboxController.prototype.deleteSandbox.call({sandboxService:service},{organizationId:'test-org'},START.id)}}
  Controller('sandbox')(HttpController);Delete(':id')(HttpController.prototype,'remove',Object.getOwnPropertyDescriptor(HttpController.prototype,'remove'))
  class TestModule{};Module({controllers:[HttpController]})(TestModule)
  app=await NestFactory.create(TestModule,{logger:false});app.useGlobalFilters(new AllExceptionsFilter({}));await app.listen(0,'127.0.0.1')
  const url=(await app.getUrl())+'/sandbox/'+START.id
  let response=await fetch(url,{method:'DELETE'});assert.equal(response.status,503);assert.ok(Number(response.headers.get('retry-after'))>0);assert.equal(stats.writes,0);assert.deepEqual(stats.events,[])
  await redis.persist(key);response=await fetch(url,{method:'DELETE'});assert.equal(response.status,503);assert.equal(response.headers.get('retry-after'),'30');assert.equal(stats.writes,0)
  const release="if redis.call('GET',KEYS[1])~=ARGV[1] then return 0 end; return redis.call('DEL',KEYS[1])"
  assert.equal(await redis.eval(release,1,key,other),0);assert.equal(await redis.pttl(key),-1)
  assert.equal(await redis.eval(release,1,key,owner),1)
  response=await fetch(url,{method:'DELETE'});assert.equal(response.status,200);assert.equal((await response.json()).desiredState,'destroyed');assert.equal(stats.writes,1);assert.ok(stats.events.includes(SandboxEvents.DESTROYED))
  await reset();stats.committed=false;stats.writes=0;stats.events=[]
  await redis.set(key,owner,'PX',20);await new Promise(r=>setTimeout(r,40));await service.destroy(START.id);assert.equal(stats.writes,1)
  // If a DELETE already passed the locked check, entry's row barrier must wait and detect its committed destruction.
  await reset();stats.committed=false;stats.writes=0;stats.events=[]
  const writing=deferred(),finishWrite=deferred();beforeWrite=async()=>{writing.resolve();await finishWrite.promise}
  const earlyDelete=service.destroy(START.id);await writing.promise;await redis.set(key,owner,'PX',300000,'NX')
  const lateGate=await pool.connect();await lateGate.query('BEGIN');let barrierFinished=false
  const observed=lateGate.query(`SELECT "desiredState" FROM ${table} WHERE id=$1 FOR UPDATE`,[START.id]).then(x=>{barrierFinished=true;return x})
  await new Promise(r=>setTimeout(r,20));assert.equal(barrierFinished,false,'entry barrier must wait for the in-flight writer')
  finishWrite.resolve();await earlyDelete
  assert.equal((await observed).rows[0].desiredState,'destroyed','entry must abort when earlier DELETE won')
  await lateGate.query('COMMIT');lateGate.release()

 }finally{if(app)await app.close();await redis.del(key);redis.disconnect();await pool.query(`DROP TABLE IF EXISTS ${table}`);await pool.end()}
})
