// Copyright 2026 Daytona Platforms Inc.
// SPDX-License-Identifier: AGPL-3.0
package admission

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func TestSharedBudgetAndNestedLease(t *testing.T) {
	m := New(1, 1, nil)
	ctx, release, err := m.Try(context.Background(), Heavy)
	if err != nil {
		t.Fatal(err)
	}
	_, _, err = m.Try(context.Background(), Heavy)
	if !errors.Is(err, ErrBusy) {
		t.Fatalf("unbounded heavy: %v", err)
	}
	nested, done, err := m.Try(ctx, Cleanup)
	if err != nil {
		t.Fatal(err)
	}
	done()
	if m.Used(Heavy) != 1 || m.Used(Cleanup) != 0 {
		t.Fatal("nested operation took a second permit")
	}
	_, cleanup, err := m.Try(context.Background(), Cleanup)
	if err != nil {
		t.Fatal("cleanup blocked by heavy")
	}
	cleanup()
	release()
	release()
	_, again, err := m.Try(nested, Heavy)
	if err != nil {
		t.Fatal(err)
	}
	defer again()
	if m.Used(Heavy) != 1 {
		t.Fatal("released context lease incorrectly reused")
	}
}
func TestPressureBlocksHeavyButNotCleanup(t *testing.T) {
	m := New(2, 1, func() error { return errors.New("io pressure") })
	if _, _, err := m.Try(context.Background(), Heavy); err == nil {
		t.Fatal("pressure ignored")
	}
	_, done, err := m.Try(context.Background(), Cleanup)
	if err != nil {
		t.Fatal(err)
	}
	done()
}
func TestCancelledAcquisitionDoesNotLeak(t *testing.T) {
	m := New(1, 1, nil)
	_, release, _ := m.Try(context.Background(), Heavy)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, _, err := m.Acquire(ctx, Heavy); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	release()
	if m.Used(Heavy) != 0 {
		t.Fatal("permit leaked")
	}
}
func TestConcurrentLimit(t *testing.T) {
	m := New(3, 1, nil)
	var wg sync.WaitGroup
	for i := 0; i < 100; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, done, err := m.Acquire(context.Background(), Heavy)
			if err != nil {
				t.Error(err)
				return
			}
			if m.Used(Heavy) > 3 {
				t.Error("over budget")
			}
			done()
		}()
	}
	wg.Wait()
	if m.Used(Heavy) != 0 {
		t.Fatal("leaked permit")
	}
}
func TestPressureHysteresisAndReadFailure(t *testing.T) {
	g := NewPressureGuard(PressureConfig{MinMemoryPercent: 10, MinDiskPercent: 10, MaxIOPSI: 20})
	s := PressureSample{MemoryAvailablePercent: 50, DiskAvailablePercent: 50, IOPSI: 25}
	if g.evaluate(s) == nil {
		t.Fatal("io pressure accepted")
	}
	g.blocked = true
	s.IOPSI = 18
	if g.evaluate(s) == nil {
		t.Fatal("reopened without hysteresis")
	}
	s.IOPSI = 15
	if err := g.evaluate(s); err != nil {
		t.Fatal(err)
	}
	g.sample = func() (PressureSample, error) { return s, errors.New("missing host proc") }
	if g.Check() == nil {
		t.Fatal("read error fails open")
	}
	g.last = time.Time{}
	g.sample = func() (PressureSample, error) { return s, nil }
	if err := g.Check(); err != nil {
		t.Fatal(err)
	}
}
func TestReadPressure(t *testing.T) {
	root := t.TempDir()
	if err := os.Mkdir(filepath.Join(root, "pressure"), 0700); err != nil {
		t.Fatal(err)
	}
	os.WriteFile(filepath.Join(root, "meminfo"), []byte("MemTotal: 1000 kB\nMemAvailable: 250 kB\n"), 0600)
	os.WriteFile(filepath.Join(root, "pressure", "io"), []byte("some avg10=12.50 avg60=1 total=55\nfull avg10=1.0\n"), 0600)
	s, err := ReadPressure(PressureConfig{ProcRoot: root, MinMemoryPercent: 10, MaxIOPSI: 20})
	if err != nil || s.MemoryAvailablePercent != 25 || s.IOPSI != 12.5 {
		t.Fatalf("%+v %v", s, err)
	}
	if _, err := parsePSI("full avg10=1"); err == nil {
		t.Fatal("missing some accepted")
	}
}
func TestJobClasses(t *testing.T) {
	for _, v := range []string{"CREATE_SANDBOX", "START_SANDBOX", "CREATE_BACKUP", "BUILD_SNAPSHOT", "PULL_SNAPSHOT", "RECOVER_SANDBOX", "SNAPSHOT_SANDBOX", "future"} {
		if ClassForJob(v) != Heavy {
			t.Fatal(v)
		}
	}
	for _, v := range []string{"STOP_SANDBOX", "DESTROY_SANDBOX", "REMOVE_SNAPSHOT", "PAUSE_SANDBOX"} {
		if ClassForJob(v) != Cleanup {
			t.Fatal(v)
		}
	}
}

func TestCleanupCannotBypassHeavyPressure(t *testing.T) {
	m := New(1, 1, func() error { return errors.New("pressure") })
	ctx, release, err := m.Try(context.Background(), Cleanup)
	if err != nil {
		t.Fatal(err)
	}
	defer release()
	if _, _, err := m.Try(ctx, Heavy); err == nil {
		t.Fatal("cleanup lease bypassed heavy guard")
	}
}
func TestMalformedPSIRejected(t *testing.T) {
	for _, v := range []string{"NaN", "Inf", "-1", "101", "n/a"} {
		if _, err := parsePSI("some avg10=" + v); err == nil {
			t.Fatal(v)
		}
	}
}
