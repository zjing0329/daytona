/*
 * Copyright 2025 Daytona Platforms Inc.
 * SPDX-License-Identifier: AGPL-3.0
 */

package poller

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	apiclient "github.com/daytonaio/daytona/libs/api-client-go"
	runnerapiclient "github.com/daytonaio/runner/pkg/apiclient"
	"github.com/daytonaio/runner/pkg/runner/v2/executor"
)

type PollerServiceConfig struct {
	PollTimeout time.Duration
	PollLimit   int
	Logger      *slog.Logger
	Executor    *executor.Executor
	SandboxCreateConcurrency  int
	SandboxDestroyConcurrency int
}

// Service handles job polling from the API
type Service struct {
	log         *slog.Logger
	pollTimeout time.Duration
	pollLimit   int
	executor    *executor.Executor
	client      *apiclient.APIClient
	createGate  chan struct{}
	destroyGate chan struct{}
}

// NewService creates a new poller service
func NewService(cfg *PollerServiceConfig) (*Service, error) {
	apiClient, err := runnerapiclient.GetApiClient()
	if err != nil {
		return nil, fmt.Errorf("failed to create API client: %w", err)
	}

	createConcurrency := cfg.SandboxCreateConcurrency
	if createConcurrency < 1 {
		createConcurrency = 1
	}
	destroyConcurrency := cfg.SandboxDestroyConcurrency
	if destroyConcurrency < 1 {
		destroyConcurrency = 1
	}

	return &Service{
		log:         cfg.Logger.With(slog.String("component", "poller")),
		pollTimeout: cfg.PollTimeout,
		pollLimit:   cfg.PollLimit,
		executor:    cfg.Executor,
		client:      apiClient,
		createGate:  make(chan struct{}, createConcurrency),
		destroyGate: make(chan struct{}, destroyConcurrency),
	}, nil
}

// Start begins the job polling loop
func (s *Service) Start(ctx context.Context) {
	inProgressJobs, _, err := s.client.JobsAPI.ListJobs(ctx).Status(apiclient.JOBSTATUS_IN_PROGRESS).Execute()
	if err != nil {
		// Only log error
		s.log.WarnContext(ctx, "Failed to fetch IN_PROGRESS jobs", "error", err)
	} else {
		if inProgressJobs != nil && len(inProgressJobs.Items) > 0 {
			s.log.InfoContext(ctx, "Found IN_PROGRESS jobs", "count", len(inProgressJobs.Items))
			for _, job := range inProgressJobs.Items {
				job := job
				go s.execute(ctx, &job)
			}
		} else {
			s.log.InfoContext(ctx, "No IN_PROGRESS jobs found")
		}
	}

	s.log.InfoContext(ctx, "Starting job poller")

	for {
		select {
		case <-ctx.Done():
			s.log.InfoContext(ctx, "Job poller stopped")
			return
		default:
			// Poll for jobs
			jobs, err := s.pollJobs(ctx)
			if err != nil {
				s.log.ErrorContext(ctx, "Failed to poll jobs", "error", err)
				// Wait a bit before retrying on error
				time.Sleep(5 * time.Second)
				continue
			}

			// Process jobs
			if len(jobs) > 0 {
				s.log.DebugContext(ctx, "Received jobs", "count", len(jobs))
				for _, job := range jobs {
					job := job
					go s.execute(ctx, &job)
				}
			}
		}
	}
}

// execute bounds the Docker-heavy lifecycle operations independently. Polling
// may continue so the API can expose an accurate pending-job queue, but a
// runner never starts an unbounded create/destroy storm against its daemon.
func (s *Service) execute(ctx context.Context, job *apiclient.Job) {
	var gate chan struct{}
	switch job.GetType() {
	case apiclient.JOBTYPE_CREATE_SANDBOX:
		gate = s.createGate
	case apiclient.JOBTYPE_DESTROY_SANDBOX:
		gate = s.destroyGate
	}

	if gate != nil {
		select {
		case gate <- struct{}{}:
			defer func() { <-gate }()
		case <-ctx.Done():
			return
		}
	}

	s.executor.Execute(ctx, job)
}

// pollJobs polls the API for pending jobs
func (s *Service) pollJobs(ctx context.Context) ([]apiclient.Job, error) {
	// Build poll request
	timeout := float32(s.pollTimeout.Seconds())
	limit := float32(s.pollLimit)

	req := s.client.JobsAPI.PollJobs(ctx).
		Timeout(timeout).
		Limit(limit)

	// Execute poll request
	resp, httpResp, err := req.Execute()
	if err != nil {
		// Check if it's a timeout (expected for long polling)
		if httpResp != nil && httpResp.StatusCode == 408 {
			// Timeout is normal for long polling, just return empty
			return []apiclient.Job{}, nil
		}
		return nil, err
	}

	if resp == nil {
		return []apiclient.Job{}, nil
	}

	return resp.GetJobs(), nil
}
