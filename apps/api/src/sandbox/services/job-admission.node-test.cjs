// Run with node --test after registering the installed ts-node transpiler.
// No database, Redis, runner or production credentials are used.
require('reflect-metadata')
require('ts-node').register({
  transpileOnly: true,
  compilerOptions: {module:'commonjs',target:'ES2022',experimentalDecorators:true,emitDecoratorMetadata:true},
})
const {test}=require('node:test')
const assert=require('node:assert/strict')
const {claimPendingJobs,parseAdmissionRequest}=require('./job-admission.ts')
const {JobStatus}=require('../enums/job-status.enum.ts')

test('admission query validation rejects absent, fractional, negative and excessive capacity',()=>{
 for(const v of [undefined,'','0','-1','1.5','101','NaN','Infinity'])assert.throws(()=>parseAdmissionRequest('heavy',v))
 assert.throws(()=>parseAdmissionRequest('all','1'))
 assert.deepEqual(parseAdmissionRequest('cleanup','2'),{jobClass:'cleanup',limit:2})
})
test('filter class and remaining limit are applied before selecting rows',async()=>{
 for(const mode of ['heavy','cleanup',undefined]){
  let query
  const repo={find:async q=>{query=q;return []}}
  assert.deepEqual(await claimPendingJobs(repo,'runner-a',2,mode),[])
  assert.equal(query.take,2);assert.equal(query.where.runnerId,'runner-a')
  if(mode==='heavy'){assert.equal(query.where.type.type,'not');assert.equal(query.where.type.child.type,'in')}
  if(mode==='cleanup'){assert.equal(query.where.type.type,'in');assert.deepEqual(query.where.type.value,['STOP_SANDBOX','DESTROY_SANDBOX','REMOVE_SNAPSHOT','PAUSE_SANDBOX'])}
  if(!mode)assert.equal(query.where.type,undefined)
 }
})
test('concurrent claims use pending-state compare-and-swap and never return a job twice',async()=>{
 let status=JobStatus.PENDING
 const repo={
  find:async()=>[{id:'j1',runnerId:'runner-a',status:JobStatus.PENDING,version:1}],
  update:async(criteria,patch)=>{
   assert.deepEqual(criteria,{id:'j1',runnerId:'runner-a',status:JobStatus.PENDING})
   assert.equal(patch.status,JobStatus.IN_PROGRESS)
   if(status!==criteria.status)return {affected:0}
   status=patch.status
   return {affected:1}
  },
 }
 const results=await Promise.all([claimPendingJobs(repo,'runner-a',1),claimPendingJobs(repo,'runner-a',1,'heavy')])
 assert.equal(results.flat().length,1);assert.equal(results.flat()[0].version,2)
})
test('database errors propagate instead of pretending there are no jobs',async()=>{
 const repo={find:async()=>[{id:'j1'}],update:async()=>{throw new Error('database unavailable')}}
 await assert.rejects(claimPendingJobs(repo,'runner-a',1),/database unavailable/)
})

test('PostgreSQL concurrent claim and cleanup selection integration', {skip:!process.env.DSEC_TEST_DATABASE}, async()=>{
 const {DataSource,EntitySchema}=require('typeorm')
 const db=new DataSource({type:'postgres',url:process.env.DSEC_TEST_DATABASE,entities:[new EntitySchema({
  name:'AdmissionJobTest',tableName:'admission_job_test',columns:{
   id:{type:String,primary:true},runnerId:{type:String},status:{type:String},type:{type:String},
   version:{type:Number,version:true},createdAt:{type:Date},updatedAt:{type:Date},startedAt:{type:Date,nullable:true},
  },
 })],synchronize:true})
 await db.initialize()
 try{
  const repo=db.getRepository('AdmissionJobTest')
  const now=new Date()
  await repo.save([
   {id:'heavy',runnerId:'runner-a',status:JobStatus.PENDING,type:'CREATE_SANDBOX',version:1,createdAt:now,updatedAt:now},
   {id:'cleanup',runnerId:'runner-a',status:JobStatus.PENDING,type:'DESTROY_SANDBOX',version:1,createdAt:now,updatedAt:now},
   {id:'other',runnerId:'runner-b',status:JobStatus.PENDING,type:'DESTROY_SANDBOX',version:1,createdAt:now,updatedAt:now},
  ])
  const cleanup=await claimPendingJobs(repo,'runner-a',1,'cleanup')
  assert.deepEqual(cleanup.map(j=>j.id),['cleanup'])
  const results=await Promise.all(Array.from({length:10},()=>claimPendingJobs(repo,'runner-a',1,'heavy')))
  assert.deepEqual(results.flat().map(j=>j.id),['heavy'])
  assert.equal((await repo.findOneByOrFail({id:'other'})).status,JobStatus.PENDING)
 }finally{await db.destroy()}
})
