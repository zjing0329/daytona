// Copyright 2026 Daytona Platforms Inc.
// SPDX-License-Identifier: AGPL-3.0
package poller

import (
	"context"
	"encoding/json"
	"fmt"
	apiclient "github.com/daytonaio/daytona/libs/api-client-go"
	"github.com/daytonaio/runner/pkg/admission"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type fakeExecutor struct {
	execute func(context.Context, *apiclient.Job)
}

func (f fakeExecutor) Execute(c context.Context, j *apiclient.Job) { f.execute(c, j) }
func testService(t *testing.T, h http.HandlerFunc, m *admission.Manager) *Service {
	server := httptest.NewServer(h)
	t.Cleanup(server.Close)
	cfg := apiclient.NewConfiguration()
	cfg.Servers = apiclient.ServerConfigurations{{URL: server.URL}}
	cfg.HTTPClient = server.Client()
	return &Service{log: slog.New(slog.NewTextHandler(io.Discard, nil)), client: apiclient.NewAPIClient(cfg), admission: m, pollLimit: 10, pollTimeout: 30 * time.Second}
}
func TestCapabilityFallbackIsOnly404(t *testing.T) {
	for _, code := range []int{404, 401, 500, 200} {
		t.Run(http.StatusText(code), func(t *testing.T) {
			s := testService(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(code); w.Write([]byte("{\"version\":1}")) }, admission.New(1, 1, nil))
			yes, err := s.supportsAdmission(context.Background())
			if code == 404 && (yes || err != nil) {
				t.Fatal("404 must fallback")
			}
			if code == 200 && (!yes || err != nil) {
				t.Fatal("v1 missing", err)
			}
			if code != 200 && code != 404 && err == nil {
				t.Fatal("auth/transient error silently downgraded")
			}
		})
	}
}
func TestReservationsBoundClaimAndKeepCleanupIndependent(t *testing.T) {
	m := admission.New(2, 1, nil)
	s := &Service{admission: m, pollLimit: 10}
	rs := s.reserve(context.Background(), admission.Heavy, false)
	if len(rs) != 2 {
		t.Fatalf("reserved %d", len(rs))
	}
	if len(s.reserve(context.Background(), admission.Heavy, false)) != 0 {
		t.Fatal("claimed without capacity")
	}
	cleanup := s.reserve(context.Background(), admission.Cleanup, false)
	if len(cleanup) != 1 {
		t.Fatal("cleanup starved")
	}
	for _, r := range append(rs, cleanup...) {
		r.cancel()
	}
	legacy := s.reserve(context.Background(), admission.Heavy, true)
	if len(legacy) != 1 {
		t.Fatal("legacy failed dual bound")
	}
	for _, r := range legacy {
		r.cancel()
	}
	if m.Used(admission.Heavy) != 0 || m.Used(admission.Cleanup) != 0 {
		t.Fatal("leaked dual reservation")
	}
}
func TestLoopDoesNotClaimBeyondCapacity(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	m := admission.New(2, 1, nil)
	var claims atomic.Int32
	s := testService(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("limit") != "2" || r.URL.Query().Get("class") != "heavy" {
			t.Error(r.URL.String())
		}
		claims.Add(1)
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{"version": 1, "jobs": []apiclient.Job{{Id: "a", Type: apiclient.JOBTYPE_CREATE_SANDBOX}, {Id: "b", Type: apiclient.JOBTYPE_PULL_SNAPSHOT}}})
	}, m)
	started := make(chan struct{}, 2)
	s.executor = fakeExecutor{func(ctx context.Context, j *apiclient.Job) { started <- struct{}{}; <-ctx.Done() }}
	done := make(chan struct{})
	go func() { s.loop(ctx, admission.Heavy, false); close(done) }()
	for i := 0; i < 2; i++ {
		select {
		case <-started:
		case <-time.After(time.Second):
			t.Fatal("not dispatched")
		}
	}
	time.Sleep(150 * time.Millisecond)
	if claims.Load() != 1 {
		t.Fatal("polled again with no capacity")
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("cancellation stuck")
	}
	if m.Used(admission.Heavy) != 0 {
		t.Fatal("lease leaked")
	}
}
func TestPollFailureReleasesReservation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	m := admission.New(1, 1, nil)
	s := testService(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(500); cancel() }, m)
	s.loop(ctx, admission.Heavy, false)
	if m.Used(admission.Heavy) != 0 {
		t.Fatal("failed request leaked reservation")
	}
}
func TestLegacyClaimUsesBoundedLimit(t *testing.T) {
	s := testService(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/jobs/poll" || r.URL.Query().Get("limit") != "1" || r.URL.Query().Get("timeout") != "1" {
			t.Error(r.URL.String())
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{"jobs": []any{}})
	}, admission.New(1, 1, nil))
	if _, err := s.pollJobs(context.Background(), admission.Heavy, 1, true); err != nil {
		t.Fatal(err)
	}
}
func TestRecoveredJobsShareBudget(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	m := admission.New(1, 1, nil)
	s := testService(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(apiclient.Job{Id: "recover", Type: apiclient.JOBTYPE_CREATE_SANDBOX, Status: apiclient.JOBSTATUS_IN_PROGRESS})
	}, m)
	started := make(chan struct{}, 1)
	s.executor = fakeExecutor{func(ctx context.Context, j *apiclient.Job) { started <- struct{}{}; <-ctx.Done() }}
	jobs := []apiclient.Job{{Type: apiclient.JOBTYPE_CREATE_SANDBOX}, {Type: apiclient.JOBTYPE_CREATE_SANDBOX}}
	done := make(chan struct{})
	go func() { s.recoverClass(ctx, jobs, admission.Heavy); close(done) }()
	<-started
	time.Sleep(150 * time.Millisecond)
	if len(started) != 0 || m.Used(admission.Heavy) != 1 {
		t.Fatal("recovery bypassed limit")
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("recovery cancellation stuck")
	}
	if m.Used(admission.Heavy) != 0 {
		t.Fatal("recovery leaked permit")
	}
}

func TestInvalidClaimResponsesReleasePermits(t *testing.T) {
	for _, kind := range []string{"too-many", "wrong-class"} {
		t.Run(kind, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			m := admission.New(1, 1, nil)
			s := testService(t, func(w http.ResponseWriter, r *http.Request) {
				jobs := []apiclient.Job{{Id: "a", Type: apiclient.JOBTYPE_STOP_SANDBOX}}
				if kind == "too-many" {
					jobs = append(jobs, jobs[0])
				}
				w.Header().Set("Content-Type", "application/json")
				json.NewEncoder(w).Encode(map[string]any{"version": 1, "jobs": jobs})
				cancel()
			}, m)
			s.executor = fakeExecutor{func(context.Context, *apiclient.Job) { t.Error("invalid response executed") }}
			s.loop(ctx, admission.Heavy, false)
			if m.Used(admission.Heavy) != 0 || m.Used(admission.Cleanup) != 0 {
				t.Fatal("bad response leaked permit")
			}
		})
	}
}
func TestCleanupRecoveryRunsWhileHeavyRecoveryBlocked(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	m := admission.New(1, 1, nil)
	_, release, _ := m.Try(ctx, admission.Heavy)
	defer release()
	started := make(chan string, 1)
	s := testService(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(apiclient.Job{Id: "cleanup", Type: apiclient.JOBTYPE_DESTROY_SANDBOX, Status: apiclient.JOBSTATUS_IN_PROGRESS})
	}, m)
	s.executor = fakeExecutor{func(ctx context.Context, j *apiclient.Job) { started <- j.Id }}
	jobs := []apiclient.Job{{Id: "heavy", Type: apiclient.JOBTYPE_CREATE_SANDBOX}, {Id: "cleanup", Type: apiclient.JOBTYPE_DESTROY_SANDBOX}}
	done := make(chan struct{})
	go func() { s.recoverClass(ctx, jobs, admission.Heavy); close(done) }()
	s.recoverClass(ctx, jobs, admission.Cleanup)
	select {
	case id := <-started:
		if id != "cleanup" {
			t.Fatal(id)
		}
	case <-time.After(time.Second):
		t.Fatal("cleanup blocked")
	}
	cancel()
	<-done
}

func TestRecoveryRevalidatesAfterWaitingForCapacity(t *testing.T) {
	for _, terminal := range []bool{false, true} {
		t.Run(map[bool]string{false: "eligible", true: "became-failed"}[terminal], func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			m := admission.New(1, 1, nil)
			_, release, _ := m.Try(ctx, admission.Heavy)
			var reads, executed atomic.Int32
			var nowFailed atomic.Bool
			s := testService(t, func(w http.ResponseWriter, r *http.Request) {
				reads.Add(1)
				if m.Used(admission.Heavy) != 1 {
					t.Error("revalidated before reserving")
				}
				status := apiclient.JOBSTATUS_IN_PROGRESS
				if nowFailed.Load() {
					status = apiclient.JOBSTATUS_FAILED
				}
				w.Header().Set("Content-Type", "application/json")
				payload := "fresh"
				json.NewEncoder(w).Encode(apiclient.Job{Id: "recover", Type: apiclient.JOBTYPE_CREATE_SANDBOX, Status: status, Payload: &payload})
			}, m)
			s.executor = fakeExecutor{func(ctx context.Context, j *apiclient.Job) {
				executed.Add(1)
				if j.GetPayload() != "fresh" {
					t.Error("executed stale startup payload")
				}
			}}
			done := make(chan struct{})
			go func() {
				s.recoverClass(ctx, []apiclient.Job{{Id: "recover", Type: apiclient.JOBTYPE_CREATE_SANDBOX, Status: apiclient.JOBSTATUS_IN_PROGRESS}}, admission.Heavy)
				close(done)
			}()
			time.Sleep(20 * time.Millisecond)
			if reads.Load() != 0 {
				t.Fatal("read before waiting for capacity")
			}
			nowFailed.Store(terminal)
			release()
			select {
			case <-done:
			case <-time.After(time.Second):
				t.Fatal("recovery stuck")
			}
			want := int32(1)
			if terminal {
				want = 0
			}
			if executed.Load() != want || reads.Load() != 1 {
				t.Fatalf("executed=%d reads=%d", executed.Load(), reads.Load())
			}
			if m.Used(admission.Heavy) != 0 {
				t.Fatal("revalidation leaked permit")
			}
		})
	}
}

func TestRecoveryRenewalNegotiationAndFailClosed(t *testing.T) {
	for _, status := range []int{200, 401, 403, 500} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			s := testService(t, func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				if r.URL.Path == "/jobs/admission/capabilities" {
					w.Write([]byte("{\"version\":1,\"recoveryRenewal\":true}"))
					return
				}
				if r.Method != http.MethodPost || r.URL.Path != "/jobs/admission/recover/recover" {
					t.Error("unexpected recovery request", r.Method, r.URL.Path)
				}
				w.WriteHeader(status)
				if status == 200 {
					w.Write([]byte("{\"version\":1,\"job\":null}"))
				}
			}, admission.New(1, 1, nil))
			supported, err := s.supportsAdmission(context.Background())
			if err != nil || !supported || !s.recoveryRenewal {
				t.Fatal("renewal not negotiated", err)
			}
			job, err := s.revalidateRecovery(context.Background(), "recover")
			if status == 200 && (job != nil || err != nil) {
				t.Fatal("terminal null must be skipped")
			}
			if status != 200 && err == nil {
				t.Fatal("renewal failure must not downgrade")
			}
		})
	}
}

// The real API ListJobsQueryDto uses PageLimit with a maximum of 200.
// Do not silently accept arbitrary limits in recovery HTTP fixtures.
func productionRecoveryQuery(w http.ResponseWriter, r *http.Request) (int, int, bool) {
	page, pageErr := strconv.Atoi(r.URL.Query().Get("page"))
	limit, limitErr := strconv.Atoi(r.URL.Query().Get("limit"))
	if r.URL.Path != "/jobs" || r.Method != http.MethodGet || pageErr != nil || page < 1 || limitErr != nil || limit < 1 || limit > 200 || r.URL.Query().Get("status") != "IN_PROGRESS" {
		http.Error(w, `{"message":"page must be >=1; limit must be <=200; status must be IN_PROGRESS"}`, http.StatusBadRequest)
		return 0, 0, false
	}
	w.Header().Set("Content-Type", "application/json")
	return page, limit, true
}

func TestRecoveryPaginationMatchesProductionHTTPValidation(t *testing.T) {
	for _, total := range []int{0, 99, 100, 101, 200, 201} {
		t.Run(fmt.Sprint(total), func(t *testing.T) {
			var pages []int
			var pagesMu sync.Mutex
			s := testService(t, func(w http.ResponseWriter, r *http.Request) {
				page, limit, ok := productionRecoveryQuery(w, r)
				if !ok {
					return
				}
				if limit != 100 {
					t.Errorf("recovery page size=%d; want production-compatible 100", limit)
				}
				pagesMu.Lock()
				pages = append(pages, page)
				pagesMu.Unlock()
				items := []apiclient.Job{}
				for i := (page - 1) * limit; i < page*limit && i < total; i++ {
					items = append(items, apiclient.Job{Id: fmt.Sprint(i), Type: apiclient.JOBTYPE_CREATE_SANDBOX, Status: apiclient.JOBSTATUS_IN_PROGRESS})
				}
				json.NewEncoder(w).Encode(map[string]any{"items": items, "total": total, "page": page, "totalPages": (total + limit - 1) / limit})
			}, admission.New(1, 1, nil))
			// Prove this fixture reproduces production's rejected old request.
			_, response, err := s.client.JobsAPI.ListJobs(context.Background()).Status(apiclient.JOBSTATUS_IN_PROGRESS).Page(1).Limit(500).Execute()
			if err == nil || response.StatusCode != http.StatusBadRequest {
				t.Fatal("fixture accepted over-limit recovery request")
			}
			jobs, err := s.recoveryJobs(context.Background())
			if err != nil || len(jobs) != total {
				t.Fatalf("recovered=%d want=%d err=%v", len(jobs), total, err)
			}
			for i, job := range jobs {
				if job.Id != fmt.Sprint(i) || job.Status != apiclient.JOBSTATUS_IN_PROGRESS {
					t.Fatalf("missing, duplicated or wrong-status job at %d", i)
				}
			}
			pagesMu.Lock()
			defer pagesMu.Unlock()
			if len(pages) != total/100+1 {
				t.Fatalf("pages=%v truncated recovery or failed to stop", pages)
			}
			for i, page := range pages {
				if page != i+1 {
					t.Fatalf("nonsequential pages=%v", pages)
				}
			}
		})
	}
}

func TestRecoveryPaginationFailureDoesNotReturnPartialWork(t *testing.T) {
	s := testService(t, func(w http.ResponseWriter, r *http.Request) {
		page, limit, ok := productionRecoveryQuery(w, r)
		if !ok {
			return
		}
		if page == 2 {
			http.Error(w, "unavailable", http.StatusServiceUnavailable)
			return
		}
		items := make([]apiclient.Job, limit)
		for i := range items {
			items[i] = apiclient.Job{Id: fmt.Sprint(i), Status: apiclient.JOBSTATUS_IN_PROGRESS}
		}
		json.NewEncoder(w).Encode(map[string]any{"items": items, "total": 101, "page": page, "totalPages": 2})
	}, admission.New(1, 1, nil))
	jobs, err := s.recoveryJobs(context.Background())
	if err == nil || jobs != nil {
		t.Fatalf("partial startup recovery escaped: jobs=%d error=%v", len(jobs), err)
	}
}

func TestStartupWithProductionPaginationReachesCleanupClaim(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	executed := make(chan string, 1)
	var recoverySeen, claimed atomic.Bool
	s := testService(t, func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/jobs":
			_, limit, ok := productionRecoveryQuery(w, r)
			if !ok {
				return
			}
			if limit != 100 {
				t.Errorf("unexpected recovery limit %d", limit)
			}
			recoverySeen.Store(true)
			json.NewEncoder(w).Encode(map[string]any{"items": []any{}, "total": 0, "page": 1, "totalPages": 0})
		case "/jobs/admission/capabilities":
			json.NewEncoder(w).Encode(map[string]any{"version": 1, "recoveryRenewal": true})
		case "/jobs/admission/poll":
			jobs := []apiclient.Job{}
			if r.URL.Query().Get("class") == "cleanup" && claimed.CompareAndSwap(false, true) {
				jobs = append(jobs, apiclient.Job{Id: "pending-destroy", Type: apiclient.JOBTYPE_DESTROY_SANDBOX, Status: apiclient.JOBSTATUS_IN_PROGRESS})
			}
			json.NewEncoder(w).Encode(map[string]any{"version": 1, "jobs": jobs})
		default:
			http.NotFound(w, r)
		}
	}, admission.New(1, 1, nil))
	s.executor = fakeExecutor{execute: func(_ context.Context, j *apiclient.Job) { executed <- j.Id; cancel() }}
	done := make(chan struct{})
	go func() { defer close(done); s.Start(ctx) }()
	select {
	case id := <-executed:
		if id != "pending-destroy" || !recoverySeen.Load() {
			t.Fatal("cleanup bypassed startup recovery")
		}
	case <-time.After(3 * time.Second):
		cancel()
		t.Fatal("startup recovery blocked all new claims")
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("poller did not stop")
	}
}
