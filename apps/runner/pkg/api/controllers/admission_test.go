// Copyright 2026 Daytona Platforms Inc.
// SPDX-License-Identifier: AGPL-3.0
package controllers

import (
	"context"
	"github.com/daytonaio/runner/pkg/admission"
	runnerdocker "github.com/daytonaio/runner/pkg/docker"
	"github.com/daytonaio/runner/pkg/runner"
	"github.com/docker/docker/api/types/system"
	"github.com/docker/docker/client"
	"github.com/gin-gonic/gin"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

type infoClient struct{ client.APIClient }

func (infoClient) Info(context.Context) (system.Info, error) { return system.Info{}, nil }
func TestAsyncSnapshotHTTPRejectsBeforeBackgroundWork(t *testing.T) {
	gin.SetMode(gin.TestMode)
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	m := admission.New(1, 1, nil)
	d, err := runnerdocker.NewDockerClient(context.Background(), runnerdocker.DockerClientConfig{ApiClient: infoClient{}, Admission: m, Logger: log, InterSandboxNetworkEnabled: true})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = runner.GetInstance(&runner.RunnerInstanceConfig{Docker: d, Logger: log}); err != nil {
		t.Fatal(err)
	}
	_, release, _ := m.Try(context.Background(), admission.Heavy)
	defer release()
	for _, handler := range []gin.HandlerFunc{PullSnapshot(context.Background(), log), BuildSnapshot(context.Background(), log)} {
		w := httptest.NewRecorder()
		ctx, _ := gin.CreateTestContext(w)
		ctx.Request = httptest.NewRequest(http.MethodPost, "/", strings.NewReader("{\"snapshot\":\"test:latest\"}"))
		ctx.Request.Header.Set("Content-Type", "application/json")
		handler(ctx)
		if len(ctx.Errors) != 1 || !strings.Contains(ctx.Errors[0].Error(), "capacity exhausted") {
			t.Fatalf("expected busy, got %v", ctx.Errors)
		}
		if w.Header().Get("Retry-After") != "1" {
			t.Fatal("missing retry hint")
		}
		// The fake Docker and nil SnapshotErrorCache would panic if any background
		// work or cache mutation happened before admission.
		if m.Used(admission.Heavy) != 1 {
			t.Fatal("rejection changed another operation's permit")
		}
	}
}
