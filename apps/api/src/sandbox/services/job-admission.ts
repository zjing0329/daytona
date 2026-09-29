/*
 * Copyright 2026 Daytona Platforms Inc.
 * SPDX-License-Identifier: AGPL-3.0
 */
import { In, Not, Repository } from 'typeorm'
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
