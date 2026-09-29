// Copyright 2026 Daytona Platforms Inc.
// SPDX-License-Identifier: AGPL-3.0
package docker

import (
	"context"
	"errors"
	"github.com/daytonaio/runner/pkg/admission"
	"github.com/daytonaio/runner/pkg/api/dto"
	"github.com/daytonaio/runner/pkg/cache"
	"github.com/docker/docker/api/types"
	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/client"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"
)

func TestLifecycleBoundariesRejectBeforeDocker(t *testing.T) {
	m := admission.New(1, 1, nil)
	_, heavy, _ := m.Try(context.Background(), admission.Heavy)
	defer heavy()
	_, cleanup, _ := m.Try(context.Background(), admission.Cleanup)
	defer cleanup()
	d := &DockerClient{admission: m}
	ctx := context.Background()
	tests := map[string]func() error{
		"create":       func() error { _, _, e := d.Create(ctx, dto.CreateSandboxDTO{}); return e },
		"start":        func() error { _, _, e := d.Start(ctx, "x", nil, nil); return e },
		"stop":         func() error { return d.Stop(ctx, "x", false) },
		"destroy":      func() error { return d.Destroy(ctx, "x") },
		"backup":       func() error { return d.CreateBackup(ctx, "x", dto.CreateBackupDTO{}) },
		"async backup": func() error { return d.CreateBackupAsync(ctx, "x", dto.CreateBackupDTO{}) },
		"pull":         func() error { return d.PullSnapshot(ctx, dto.PullSnapshotRequestDTO{}) },
		"build":        func() error { return d.BuildSnapshot(ctx, dto.BuildSnapshotRequestDTO{}) },
		"recover":      func() error { return d.RecoverSandbox(ctx, "x", dto.RecoverSandboxDTO{}) },
		"resize":       func() error { return d.Resize(ctx, "x", dto.ResizeSandboxDTO{}) },
		"remove":       func() error { return d.RemoveImage(ctx, "x", true) },
		"snapshot":     func() error { _, e := d.CreateSnapshotFromSandbox(ctx, "x", nil); return e },
	}
	for name, run := range tests {
		t.Run(name, func(t *testing.T) {
			if run() == nil {
				t.Fatal("operation bypassed admission")
			}
		})
	}
}

type blockedCommitClient struct {
	client.APIClient
	entered chan struct{}
	unblock chan struct{}
	once    sync.Once
}

func (f *blockedCommitClient) ContainerCommit(ctx context.Context, id string, o container.CommitOptions) (types.IDResponse, error) {
	f.once.Do(func() { close(f.entered) })
	select {
	case <-f.unblock:
		return types.IDResponse{}, errors.New("test commit failed")
	case <-ctx.Done():
		return types.IDResponse{}, ctx.Err()
	}
}
func TestAsyncBackupHoldsPermitUntilActualCompletion(t *testing.T) {
	root, cancelRoot := context.WithCancel(context.Background())
	defer cancelRoot()
	m := admission.New(1, 1, nil)
	fake := &blockedCommitClient{entered: make(chan struct{}), unblock: make(chan struct{})}
	d := &DockerClient{admission: m, apiClient: fake, logger: slog.New(slog.NewTextHandler(io.Discard, nil)), backupInfoCache: cache.NewBackupInfoCache(root, time.Minute), backupTimeoutMin: 1}
	request, cancelRequest := context.WithCancel(root)
	if err := d.CreateBackupAsync(request, "lease-test", dto.CreateBackupDTO{}); err != nil {
		t.Fatal(err)
	}
	cancelRequest()
	select {
	case <-fake.entered:
	case <-time.After(time.Second):
		t.Fatal("background backup did not start")
	}
	if m.Used(admission.Heavy) != 1 {
		t.Fatal("HTTP return released active backup permit")
	}
	if _, _, err := m.Try(root, admission.Heavy); err == nil {
		t.Fatal("second heavy operation admitted")
	}
	close(fake.unblock)
	deadline := time.Now().Add(time.Second)
	for m.Used(admission.Heavy) != 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if m.Used(admission.Heavy) != 0 {
		t.Fatal("failed backup leaked permit")
	}
}
