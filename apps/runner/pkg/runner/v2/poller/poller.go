// Copyright 2026 Daytona Platforms Inc.
// SPDX-License-Identifier: AGPL-3.0
package poller

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	apiclient "github.com/daytonaio/daytona/libs/api-client-go"
	"github.com/daytonaio/runner/pkg/admission"
	runnerapiclient "github.com/daytonaio/runner/pkg/apiclient"
)

type jobExecutor interface {
	Execute(context.Context, *apiclient.Job)
}
type PollerServiceConfig struct {
	PollTimeout time.Duration
	PollLimit   int
	Logger      *slog.Logger
	Executor    jobExecutor
	Admission   *admission.Manager
}
type Service struct {
	log             *slog.Logger
	pollTimeout     time.Duration
	pollLimit       int
	executor        jobExecutor
	client          *apiclient.APIClient
	admission       *admission.Manager
	recoveryRenewal bool
}
type reservation struct {
	ctx            context.Context
	release        func()
	cleanupCtx     context.Context
	cleanupRelease func()
}

func (r reservation) cancel() {
	r.release()
	if r.cleanupRelease != nil {
		r.cleanupRelease()
	}
}
func NewService(cfg *PollerServiceConfig) (*Service, error) {
	c, err := runnerapiclient.GetApiClient()
	if err != nil {
		return nil, err
	}
	if cfg.Admission == nil {
		return nil, fmt.Errorf("node admission is required")
	}
	return &Service{log: cfg.Logger.With("component", "poller"), pollTimeout: cfg.PollTimeout, pollLimit: cfg.PollLimit, executor: cfg.Executor, client: c, admission: cfg.Admission}, nil
}
func wait(ctx context.Context, d time.Duration) bool {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}
func (s *Service) Start(ctx context.Context) {
	// Fetch all pages before executing: completions mutate status-filtered pages.
	// Recovery dispatch has one bounded producer per class, never one waiter per job.
	jobs, err := s.recoveryJobs(ctx)
	for err != nil {
		s.log.ErrorContext(ctx, "Cannot recover in-progress jobs; refusing new claims", "error", err)
		if !wait(ctx, 5*time.Second) {
			return
		}
		jobs, err = s.recoveryJobs(ctx)
	}
	supported, err := s.supportsAdmission(ctx)
	for err != nil {
		s.log.ErrorContext(ctx, "Failed to detect admission API", "error", err)
		if !wait(ctx, 5*time.Second) {
			return
		}
		supported, err = s.supportsAdmission(ctx)
	}
	var workers sync.WaitGroup
	for _, class := range []admission.Class{admission.Heavy, admission.Cleanup} {
		workers.Add(1)
		go func(class admission.Class) {
			defer workers.Done()
			s.recoverClass(ctx, jobs, class)
			if supported && ctx.Err() == nil {
				s.loop(ctx, class, false)
			}
		}(class)
	}
	workers.Wait()
	if !supported && ctx.Err() == nil {
		s.log.WarnContext(ctx, "Legacy API: conservative dual reservation; cleanup queue priority requires admission API")
		s.loop(ctx, admission.Heavy, true)
	}
}

func (s *Service) recoverClass(ctx context.Context, jobs []apiclient.Job, class admission.Class) {
	var active sync.WaitGroup
	defer active.Wait()
	for i := range jobs {
		if admission.ClassForJob(string(jobs[i].GetType())) != class {
			continue
		}
		leased, release, err := s.admission.Acquire(ctx, class)
		if err != nil {
			return
		}
		job, err := s.revalidateRecovery(leased, jobs[i].GetId())
		if err != nil {
			release()
			s.log.ErrorContext(ctx, "Cannot revalidate recovery job; skipping execution", "job_id", jobs[i].GetId(), "error", err)
			continue
		}
		if job == nil || job.GetStatus() != apiclient.JOBSTATUS_IN_PROGRESS || admission.ClassForJob(string(job.GetType())) != class {
			release()
			s.log.InfoContext(ctx, "Recovery job is no longer eligible", "job_id", jobs[i].GetId())
			continue
		}
		active.Add(1)
		go func() { defer active.Done(); defer release(); s.executor.Execute(leased, job) }()
	}
}

// Revalidate only after acquiring capacity: the startup list may be older than
// the API's stale-job deadline by the time a queued recovery can actually run.
func (s *Service) revalidateRecovery(ctx context.Context, id string) (*apiclient.Job, error) {
	if s.recoveryRenewal {
		var result struct {
			Version int
			Job     *apiclient.Job
		}
		status, err := s.admissionRequestMethod(ctx, http.MethodPost, "/jobs/admission/recover/"+url.PathEscape(id), nil, &result)
		if status == http.StatusNotFound {
			return nil, nil
		}
		if err != nil {
			return nil, err
		}
		if result.Version != 1 {
			return nil, fmt.Errorf("recovery renewal response missing version 1")
		}
		return result.Job, nil
	}
	freshCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	job, resp, err := s.client.JobsAPI.GetJob(freshCtx, id).Execute()
	if resp != nil && resp.StatusCode == http.StatusNotFound {
		return nil, nil
	}
	return job, err
}

// Keep startup recovery within the production API pagination DTO maximum (200).
// A shared size also prevents truncating recovery when changing the request limit.
const recoveryPageSize = 100

func (s *Service) recoveryJobs(ctx context.Context) ([]apiclient.Job, error) {
	var result []apiclient.Job
	for page := 1; ; page++ {
		resp, _, err := s.client.JobsAPI.ListJobs(ctx).Status(apiclient.JOBSTATUS_IN_PROGRESS).Page(float32(page)).Limit(recoveryPageSize).Execute()
		if err != nil {
			return nil, err
		}
		if resp == nil || len(resp.Items) == 0 {
			return result, nil
		}
		result = append(result, resp.Items...)
		if len(resp.Items) < recoveryPageSize {
			return result, nil
		}
	}
}
func (s *Service) reserve(ctx context.Context, class admission.Class, legacy bool) []reservation {
	var result []reservation
	for i := 0; i < s.pollLimit; i++ {
		leased, release, err := s.admission.Try(ctx, class)
		if err != nil {
			break
		}
		r := reservation{ctx: leased, release: release}
		if legacy {
			cleanupCtx, cleanupRelease, err := s.admission.Try(ctx, admission.Cleanup)
			if err != nil {
				release()
				break
			}
			r.cleanupCtx = cleanupCtx
			r.cleanupRelease = cleanupRelease
		}
		result = append(result, r)
	}
	return result
}
func (s *Service) loop(ctx context.Context, class admission.Class, legacy bool) {
	var active sync.WaitGroup
	defer active.Wait()
	for ctx.Err() == nil {
		reserved := s.reserve(ctx, class, legacy)
		if len(reserved) == 0 {
			if !wait(ctx, 100*time.Millisecond) {
				return
			}
			continue
		}
		jobs, err := s.pollJobs(ctx, class, len(reserved), legacy)
		if err != nil {
			for _, r := range reserved {
				r.cancel()
			}
			s.log.ErrorContext(ctx, "Failed to claim admitted jobs", "error", err, "class", class)
			if !wait(ctx, 5*time.Second) {
				return
			}
			continue
		}
		if len(jobs) > len(reserved) {
			// Never execute an over-delivering API outside the node budget.
			s.log.ErrorContext(ctx, "API returned more jobs than reserved capacity", "jobs", len(jobs), "reserved", len(reserved))
			for _, r := range reserved {
				r.cancel()
			}
			return
		}
		for i, r := range reserved {
			if i >= len(jobs) {
				r.cancel()
				continue
			}
			job := jobs[i]
			actual := admission.ClassForJob(string(job.GetType()))
			if !legacy && actual != class {
				r.cancel()
				s.log.ErrorContext(ctx, "Admission API returned wrong job class", "job_id", job.GetId(), "class", class)
				// Leave unexecuted IN_PROGRESS work for reconciliation; do not run it in
				// cleanup capacity, which would bypass the heavy pressure guard.
				continue
			}
			leased, release := r.ctx, r.release
			if legacy {
				if actual == admission.Cleanup {
					r.release()
					leased, release = r.cleanupCtx, r.cleanupRelease
				} else {
					r.cleanupRelease()
				}
			}
			active.Add(1)
			go func() { defer active.Done(); defer release(); s.executor.Execute(leased, &job) }()
		}
		if len(jobs) == 0 && !wait(ctx, 500*time.Millisecond) {
			return
		}
	}
}
func (s *Service) supportsAdmission(ctx context.Context) (bool, error) {
	var result struct {
		Version         int
		RecoveryRenewal bool
	}
	status, err := s.admissionRequest(ctx, "/jobs/admission/capabilities", nil, &result)
	if status == http.StatusNotFound {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	if result.Version != 1 {
		return false, fmt.Errorf("unsupported admission API version %d", result.Version)
	}
	s.recoveryRenewal = result.RecoveryRenewal
	return true, nil
}
func (s *Service) pollJobs(ctx context.Context, class admission.Class, limit int, legacy bool) ([]apiclient.Job, error) {
	if legacy {
		// Reservations must not monopolize direct HTTP capacity for a 30s idle poll.
		timeout := float32(min(s.pollTimeout.Seconds(), 1))
		resp, httpResp, err := s.client.JobsAPI.PollJobs(ctx).Timeout(timeout).Limit(float32(limit)).Execute()
		if httpResp != nil && httpResp.StatusCode == 408 {
			return nil, nil
		}
		if err != nil {
			return nil, err
		}
		if resp == nil {
			return nil, nil
		}
		return resp.Jobs, nil
	}
	var result struct {
		Version int
		Jobs    []apiclient.Job
	}
	_, err := s.admissionRequest(ctx, "/jobs/admission/poll", url.Values{"class": {string(class)}, "limit": {strconv.Itoa(limit)}}, &result)
	if err != nil {
		return nil, err
	}
	if result.Version != 1 {
		return nil, fmt.Errorf("admission poll response missing version 1")
	}
	return result.Jobs, nil
}
func (s *Service) admissionRequest(ctx context.Context, path string, query url.Values, result any) (int, error) {
	return s.admissionRequestMethod(ctx, http.MethodGet, path, query, result)
}
func (s *Service) admissionRequestMethod(ctx context.Context, method string, path string, query url.Values, result any) (int, error) {
	cfg := s.client.GetConfig()
	base, err := cfg.ServerURLWithContext(ctx, "JobsAPIService.PollJobs")
	if err != nil {
		return 0, err
	}
	reqCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(reqCtx, method, strings.TrimRight(base, "/")+path+"?"+query.Encode(), nil)
	if err != nil {
		return 0, err
	}
	for k, v := range cfg.DefaultHeader {
		req.Header.Set(k, v)
	}
	req.Header.Set("Accept", "application/json")
	resp, err := cfg.HTTPClient.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return resp.StatusCode, fmt.Errorf("admission API HTTP %d", resp.StatusCode)
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 8<<20)).Decode(result); err != nil {
		return resp.StatusCode, err
	}
	return resp.StatusCode, nil
}
