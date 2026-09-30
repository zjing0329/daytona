// Copyright 2026 Daytona Platforms Inc.
// SPDX-License-Identifier: AGPL-3.0

// Package admission bounds node-wide lifecycle work before accepting it.
// A lease is carried in context so nested Docker operations share one budget.
package admission

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"time"
)

type Class string

const (
	Heavy   Class = "heavy"
	Cleanup Class = "cleanup"
)

var ErrBusy = errors.New("runner operation capacity exhausted; retry later")

type Manager struct {
	mu       sync.Mutex
	limits   map[Class]int
	used     map[Class]int
	pressure func() error
}
type leaseKey struct{}
type lease struct {
	owner    *Manager
	class    Class
	released atomic.Bool
}

func New(heavy, cleanup int, pressure func() error) *Manager {
	if heavy < 1 {
		heavy = 1
	}
	if cleanup < 1 {
		cleanup = 1
	}
	capacityGauge.WithLabelValues(string(Heavy)).Set(float64(heavy))
	capacityGauge.WithLabelValues(string(Cleanup)).Set(float64(cleanup))
	return &Manager{limits: map[Class]int{Heavy: heavy, Cleanup: cleanup}, used: make(map[Class]int), pressure: pressure}
}

// Try never queues. Callers must reserve before claiming a job or returning 202.
// Only internal contexts can carry leases; clients cannot forge a lease over HTTP.
func (m *Manager) Try(ctx context.Context, class Class) (context.Context, func(), error) {
	if err := ctx.Err(); err != nil {
		return ctx, nil, err
	}
	if m == nil {
		return ctx, func() {}, nil
	}
	if l, ok := ctx.Value(leaseKey{}).(*lease); ok && l.owner == m && !l.released.Load() && (l.class == Heavy || class == Cleanup) {
		return ctx, func() {}, nil
	}
	if class != Cleanup {
		class = Heavy
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.used[class] >= m.limits[class] {
		deniedCounter.WithLabelValues(string(class), "capacity").Inc()
		return ctx, nil, ErrBusy
	}
	if class == Heavy && m.pressure != nil {
		if err := m.pressure(); err != nil {
			deniedCounter.WithLabelValues(string(class), "pressure").Inc()
			return ctx, nil, err
		}
	}
	m.used[class]++
	activeGauge.WithLabelValues(string(class)).Inc()
	l := &lease{owner: m, class: class}
	release := func() {
		if l.released.CompareAndSwap(false, true) {
			m.mu.Lock()
			m.used[class]--
			activeGauge.WithLabelValues(string(class)).Dec()
			m.mu.Unlock()
		}
	}
	return context.WithValue(ctx, leaseKey{}, l), release, nil
}

// Acquire is for bounded dispatchers only. HTTP handlers use Try and return 429.
func (m *Manager) Acquire(ctx context.Context, class Class) (context.Context, func(), error) {
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()
	for {
		leased, release, err := m.Try(ctx, class)
		if err == nil {
			return leased, release, nil
		}
		select {
		case <-ctx.Done():
			return ctx, nil, ctx.Err()
		case <-ticker.C:
		}
	}
}

func (m *Manager) Used(class Class) int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.used[class]
}

// ClassForJob deliberately sends unknown and future job types through Heavy.
func ClassForJob(jobType string) Class {
	switch jobType {
	case "STOP_SANDBOX", "DESTROY_SANDBOX", "REMOVE_SNAPSHOT", "PAUSE_SANDBOX":
		return Cleanup
	default:
		return Heavy
	}
}
