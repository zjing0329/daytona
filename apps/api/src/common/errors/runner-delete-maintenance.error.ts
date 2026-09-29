/*
 * Copyright 2026 Daytona Platforms Inc.
 * SPDX-License-Identifier: AGPL-3.0
 */
import { ServiceUnavailableException } from '@nestjs/common'
import type { Redis } from 'ioredis'

export const runnerDeleteMaintenanceKey = (runnerId: string): string => `runner:maintenance:delete:${runnerId}`

export class RunnerDeleteMaintenanceError extends ServiceUnavailableException {
  constructor(public readonly retryAfterSeconds: number, unavailable = false) {
    super(unavailable ? 'Runner maintenance status is unavailable; retry deletion later.' : 'Runner is under maintenance; retry deletion later.')
  }
}

/** Only an explicit missing key permits deletion. Persistent/malformed leases and Redis errors fail closed. */
export async function assertRunnerDeleteAllowed(redis: Pick<Redis, 'pttl'>, runnerId?: string | null): Promise<void> {
  if (!runnerId) return
  let remaining: number
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    // The second check holds a DB row lock. Never wait indefinitely for a disconnected Redis client.
    remaining = await Promise.race([
      redis.pttl(runnerDeleteMaintenanceKey(runnerId)),
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error('Maintenance lookup timed out')), 2000)
      }),
    ])
  } catch {
    throw new RunnerDeleteMaintenanceError(30, true)
  } finally {
    if (timer !== undefined) clearTimeout(timer)
  }
  if (remaining === -2) return
  const retryAfter = Number.isFinite(remaining) && remaining >= 0 ? Math.max(1, Math.ceil(remaining / 1000)) : 30
  throw new RunnerDeleteMaintenanceError(retryAfter)
}
