/*
 * Copyright 2026 Daytona Platforms Inc.
 * SPDX-License-Identifier: AGPL-3.0
 */
import { In, Not, LessThan, Repository } from 'typeorm'
import { Job } from '../entities/job.entity'
import { JobStatus } from '../enums/job-status.enum'
import { JobType } from '../enums/job-type.enum'

export type JobClass = 'heavy' | 'cleanup'
export function parseAdmissionRequest(jobClass: string, limit: string): { jobClass: JobClass; limit: number } {
  const count = Number(limit)
  if ((jobClass !== 'heavy' && jobClass !== 'cleanup') || !Number.isInteger(count) || count < 1 || count > 100) {
    throw new Error('class must be heavy or cleanup and limit must be an integer from 1 to 100')
  }
  return { jobClass, limit: count }
}

export async function claimPendingJobs(
  repository: Repository<Job>, runnerId: string, limit: number, jobClass?: JobClass,
): Promise<Job[]> {
  const cleanupTypes = [JobType.STOP_SANDBOX, JobType.DESTROY_SANDBOX, JobType.REMOVE_SNAPSHOT, JobType.PAUSE_SANDBOX]
  const typeFilter = jobClass === 'cleanup' ? In(cleanupTypes) : jobClass === 'heavy' ? Not(In(cleanupTypes)) : undefined
  const jobs = await repository.find({
    where: { runnerId, status: JobStatus.PENDING, ...(typeFilter ? { type: typeFilter } : {}) },
    order: { createdAt: 'ASC', id: 'ASC' },
    take: limit,
  })
  const claimed: Job[] = []
  for (const job of jobs) {
    const now = new Date()
    // VersionColumn alone on save() is not a compare-and-swap. The status
    // predicate arbitrates concurrent legacy and class-filtered poll requests.
    const result = await repository.update(
      { id: job.id, runnerId, status: JobStatus.PENDING },
      { status: JobStatus.IN_PROGRESS, startedAt: now, updatedAt: now },
    )
    if (result.affected === 1) {
      job.status = JobStatus.IN_PROGRESS
      job.startedAt = now
      job.updatedAt = now
      job.version += 1
      claimed.push(job)
    }
  }
  return claimed
}


// Renew only an existing IN_PROGRESS row. A terminal job must never be revived.
export async function renewRecoveryJob(repository: Repository<Job>, runnerId: string, id: string): Promise<Job | null> {
  const job = await repository.findOneBy({ id, runnerId, status: JobStatus.IN_PROGRESS })
  if (!job) return null
  const updatedAt = new Date()
  const result = await repository.update(
    { id, runnerId, status: JobStatus.IN_PROGRESS, version: job.version },
    { updatedAt },
  )
  if (result.affected !== 1) return null
  job.updatedAt = updatedAt
  job.version += 1
  return job
}

// The stale scanner uses its observed version as well as the timeout predicate,
// so it cannot fail a job after a recovery renewal or concurrent completion.
export async function failStaleJob(repository: Repository<Job>, job: Job, threshold: Date, errorMessage: string): Promise<Job | null> {
  const now = new Date()
  const result = await repository.update(
    { id: job.id, status: JobStatus.IN_PROGRESS, version: job.version, updatedAt: LessThan(threshold) },
    { status: JobStatus.FAILED, errorMessage, updatedAt: now, completedAt: now },
  )
  if (result.affected !== 1) return null
  job.status = JobStatus.FAILED
  job.errorMessage = errorMessage
  job.updatedAt = now
  job.completedAt = now
  job.version += 1
  return job
}
