// Isolated regressions for the v0.190 lifecycle concurrency fixes.
// Run from a locked dependency workspace: node --test <this-file>.
// DSEC_CONCURRENCY_SOURCE_ROOT may point to an isolated older checkout for
// differential verification. No Nest application, DB, Redis or Runner is started.
const path = require('node:path')
const {test} = require('node:test')
const assert = require('node:assert/strict')

const sourceRoot = path.resolve(process.env.DSEC_CONCURRENCY_SOURCE_ROOT || process.cwd())
require('reflect-metadata')
require('ts-node').register({
  transpileOnly: true,
  compilerOptions: {module: 'commonjs', moduleResolution: 'node', esModuleInterop: true, target: 'ES2022', experimentalDecorators: true, emitDecoratorMetadata: true},
})
require('tsconfig-paths').register({
  baseUrl: sourceRoot,
  paths: require(path.join(sourceRoot, 'tsconfig.base.json')).compilerOptions.paths,
})
const load = relative => require(path.join(sourceRoot, 'apps/api/src', relative))
const {JobStateHandlerService} = load('sandbox/services/job-state-handler.service.ts')
const {SandboxStartAction} = load('sandbox/managers/sandbox-actions/sandbox-start.action.ts')
const {SandboxManager} = load('sandbox/managers/sandbox.manager.ts')
const {SnapshotManager} = load('sandbox/managers/snapshot.manager.ts')
const {SandboxConflictError} = load('sandbox/errors/sandbox-conflict.error.ts')
const {JobStatus} = load('sandbox/enums/job-status.enum.ts')
const {JobType} = load('sandbox/enums/job-type.enum.ts')
const {ResourceType} = load('sandbox/enums/resource-type.enum.ts')
const {SandboxState} = load('sandbox/enums/sandbox-state.enum.ts')
const {SandboxDesiredState} = load('sandbox/enums/sandbox-desired-state.enum.ts')
const {BackupState} = load('sandbox/enums/backup-state.enum.ts')
const {RunnerState} = load('sandbox/enums/runner-state.enum.ts')
const {getStateChangeLockKey} = load('sandbox/utils/lock-key.util.ts')

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  // A baseline that fails to await child work must not leak an unhandled rejection.
  promise.catch(() => {})
  return {promise, resolve, reject}
}
const nextTurn = () => new Promise(resolve => setImmediate(resolve))
function logger() {
  const errors = []
  return {errors, error: (...args) => errors.push(args), warn() {}, debug() {}, log() {}, verbose() {}}
}
function manager(Manager) {
  return Object.assign(Object.create(Manager.prototype), {logger: logger(), activeJobs: new Set()})
}

test('completing CREATE cannot delete the lock held by a concurrent state sync', {timeout: 5000}, async () => {
  const service = Object.create(JobStateHandlerService.prototype)
  const entered = deferred(), finishWrite = deferred()
  const key = getStateChangeLockKey('sandbox-completion')
  const locks = new Map([[key, 'original-sync-owner']])
  const unlocks = [], writes = []
  service.logger = logger()
  service.redisLockProvider = {unlock: async lockKey => { unlocks.push(lockKey); locks.delete(lockKey) }}
  service.sandboxRepository = {
    findOne: async () => ({
      id: 'sandbox-completion', desiredState: SandboxDesiredState.STARTED,
      state: SandboxState.CREATING, backupState: BackupState.NONE,
    }),
    update: async (id, data) => { writes.push({id, data}); entered.resolve(); await finishWrite.promise },
  }
  const running = service.handleJobCompletion({
    id: 'completed-create', resourceId: 'sandbox-completion', resourceType: ResourceType.SANDBOX,
    type: JobType.CREATE_SANDBOX, status: JobStatus.COMPLETED, getResultMetadata: () => ({}),
  })
  try {
    await entered.promise
    // The previous owner's lease expired while completion was updating the row.
    locks.set(key, 'new-sync-owner')
  } finally {
    finishWrite.resolve()
    await running
  }
  assert.equal(writes.length, 1)
  assert.equal(writes[0].data.updateData.state, SandboxState.STARTED)
  assert.equal(locks.get(key), 'new-sync-owner', 'completion must not release another execution owner')
  assert.deepEqual(unlocks, [])
})

function restoreFixture(currentToken) {
  const action = Object.create(SandboxStartAction.prototype)
  const writes = [], dispatched = []
  const sandbox = {
    id: 'sandbox-restore', name: 'isolated-restore', pending: true,
    state: SandboxState.ARCHIVED, desiredState: SandboxDesiredState.STARTED,
    runnerId: null, region: 'isolated-region', sandboxClass: 'container', gpu: 0,
    backupRegistryId: 'isolated-registry', backupSnapshot: 'isolated-backup',
    existingBackupSnapshots: [{snapshotName: 'isolated-backup', createdAt: '2026-01-01T00:00:00Z'}],
  }
  const runner = {id: 'new-runner', state: RunnerState.READY, unschedulable: false}
  const adapter = {
    inspectSnapshotInRegistry: async () => {},
    createSandbox: async (...args) => dispatched.push(args),
  }
  action.logger = logger()
  action.redisLockProvider = {getCode: async () => currentToken === null ? null : {getCode: () => currentToken}}
  action.sandboxRepository = {update: async (id, data) => { writes.push({id, data}); Object.assign(sandbox, data.updateData) }}
  action.dockerRegistryService = {findOne: async () => ({id: 'isolated-registry'})}
  action.runnerService = {findAvailableRunners: async () => [runner]}
  action.runnerAdapterFactory = {create: async () => adapter}
  action.redis = {del: async () => 1}
  action.configService = {get: () => undefined}
  const run = () => action.restoreSandboxOnNewRunner(
    sandbox, {getCode: () => 'expected-owner'}, {sandboxMetadata: {}}, 'previous-runner',
  )
  return {run, writes, dispatched}
}

for (const currentToken of [null, 'replacement-owner']) {
  test('restore rejects ' + (currentToken === null ? 'expired' : 'replaced') + ' sync ownership before dispatch',
    {timeout: 5000}, async () => {
      const fixture = restoreFixture(currentToken)
      await assert.rejects(fixture.run(), SandboxConflictError)
      assert.deepEqual(fixture.writes, [])
      assert.deepEqual(fixture.dispatched, [], 'no Runner CREATE after state update loses ownership')
    })
}

test('owned restore persists both runner IDs before exactly one CREATE dispatch', {timeout: 5000}, async () => {
  const fixture = restoreFixture('expected-owner')
  await fixture.run()
  assert.equal(fixture.writes.length, 1)
  assert.deepEqual(fixture.writes[0].data.updateData, {
    state: SandboxState.RESTORING, runnerId: 'new-runner', prevRunnerId: 'previous-runner',
  })
  assert.equal(fixture.dispatched.length, 1)
  assert.equal(fixture.dispatched[0][0].runnerId, 'new-runner')
  assert.equal(fixture.dispatched[0][0].prevRunnerId, 'previous-runner')
})

test('archived-state cron holds its batch lock and active-job entry until every sync settles',
  {timeout: 5000}, async () => {
    const instance = manager(SandboxManager)
    const ready = deferred(), gates = [deferred(), deferred()]
    let starts = 0, settled = false, held = false
    const unlocks = []
    instance.redisLockProvider = {
      lock: async () => { held = true; return true },
      unlock: async key => { held = false; unlocks.push(key) },
    }
    instance.sandboxRepository = {find: async () => [{id: 'a'}, {id: 'b'}]}
    instance.syncInstanceState = id => {
      if (++starts === 2) ready.resolve()
      return gates[id === 'a' ? 0 : 1].promise
    }
    const running = instance.syncArchivedDesiredStates().finally(() => { settled = true })
    try {
      await ready.promise
      await nextTurn()
      assert.equal(settled, false, 'cron returned while child syncs are active')
      assert.equal(held, true)
      assert.equal(instance.activeJobs.has('syncArchivedDesiredStates'), true)
      gates[0].reject(new Error('isolated child failure'))
      await nextTurn()
      assert.equal(settled, false, 'one rejected child must not release the batch lock')
      assert.equal(held, true)
    } finally {
      gates.forEach(gate => gate.resolve())
      await running
    }
    assert.equal(held, false)
    assert.deepEqual(unlocks, ['sync-archived-desired-states'])
    assert.equal(instance.activeJobs.size, 0)
  })

test('snapshot-state cron remains active until all snapshot syncs settle', {timeout: 5000}, async () => {
  const instance = manager(SnapshotManager)
  const ready = deferred(), gates = [deferred(), deferred()]
  let starts = 0, settled = false
  instance.snapshotRepository = {find: async () => [{id: 'a'}, {id: 'b'}]}
  instance.syncSnapshotState = id => {
    if (++starts === 2) ready.resolve()
    return gates[id === 'a' ? 0 : 1].promise
  }
  const running = instance.checkSnapshotState().finally(() => { settled = true })
  try {
    await ready.promise
    await nextTurn()
    assert.equal(settled, false, 'snapshot cron returned while child syncs are active')
    assert.equal(instance.activeJobs.has('checkSnapshotState'), true)
    gates[0].reject(new Error('isolated snapshot failure'))
    await nextTurn()
    assert.equal(settled, false)
  } finally {
    gates.forEach(gate => gate.resolve())
    await running
  }
  assert.equal(instance.activeJobs.size, 0)
  assert.equal(instance.logger.errors.length, 1)
})

test('snapshot cleanup releases its batch lock even when the initial query fails', {timeout: 5000}, async () => {
  const instance = manager(SnapshotManager)
  const unlocks = []
  instance.redisLockProvider = {lock: async () => true, unlock: async key => unlocks.push(key)}
  instance.snapshotRepository = {find: async () => { throw new Error('isolated query failure') }}
  await assert.rejects(instance.checkSnapshotCleanup(), /isolated query failure/)
  assert.deepEqual(unlocks, ['check-snapshot-cleanup-lock'])
  assert.equal(instance.activeJobs.size, 0)
})

test('one snapshot cleanup failure waits for other removals before releasing the lock', {timeout: 5000}, async () => {
  const instance = manager(SnapshotManager)
  const removing = deferred(), finishRemoval = deferred()
  const unlocks = [], removed = []
  let settled = false
  instance.redisLockProvider = {lock: async () => true, unlock: async key => unlocks.push(key)}
  instance.snapshotRunnerRepository = {update: async () => {}}
  instance.snapshotRepository = {
    find: async () => [{id: 'bad', ref: 'bad-ref'}, {id: 'good', ref: 'good-ref'}],
    count: async query => {
      if (query.where.ref === 'bad-ref') throw new Error('isolated count failure')
      return 0
    },
    remove: async snapshot => { removing.resolve(); await finishRemoval.promise; removed.push(snapshot.id) },
  }
  const running = instance.checkSnapshotCleanup().finally(() => { settled = true })
  running.catch(() => {})
  try {
    await removing.promise
    await nextTurn()
    assert.equal(settled, false, 'batch finished before another cleanup task completed')
    assert.deepEqual(unlocks, [])
  } finally {
    finishRemoval.resolve()
    await running
  }
  assert.deepEqual(removed, ['good'])
  assert.deepEqual(unlocks, ['check-snapshot-cleanup-lock'])
  assert.equal(instance.logger.errors.length, 1)
})
