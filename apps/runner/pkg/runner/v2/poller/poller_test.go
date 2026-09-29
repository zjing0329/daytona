// Copyright 2026 Daytona Platforms Inc.
// SPDX-License-Identifier: AGPL-3.0
package poller

import (
	"context"
	"encoding/json"
	apiclient "github.com/daytonaio/daytona/libs/api-client-go"
	"github.com/daytonaio/runner/pkg/admission"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
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
