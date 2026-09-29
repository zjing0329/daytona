// Copyright 2026 Daytona Platforms Inc.
// SPDX-License-Identifier: AGPL-3.0
package admission

import (
	"fmt"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

type PressureConfig struct {
	ProcRoot         string
	DiskPath         string
	MinMemoryPercent float64
	MinDiskPercent   float64
	MinInodePercent  float64
	MaxIOPSI         float64
	MaxCPUPSI        float64
}
type PressureSample struct {
	MemoryAvailablePercent float64
	DiskAvailablePercent   float64
	InodesAvailablePercent float64
	IOPSI                  float64
	CPUPSI                 float64
}
type PressureGuard struct {
	config    PressureConfig
	sample    func() (PressureSample, error)
	mu        sync.Mutex
	last      time.Time
	blocked   bool
	lastError error
}

func NewPressureGuard(c PressureConfig) *PressureGuard {
	g := &PressureGuard{config: c}
	g.sample = func() (PressureSample, error) { return ReadPressure(c) }
	return g
}

// Check fails closed if an enabled source cannot be measured. Hysteresis
// requires 20% headroom before reopening; cleanup never calls this guard.
func (g *PressureGuard) Check() error {
	g.mu.Lock()
	defer g.mu.Unlock()
	if time.Since(g.last) < time.Second {
		return g.lastError
	}
	g.last = time.Now()
	s, err := g.sample()
	sampleOK := err == nil
	if sampleOK {
		err = g.evaluate(s)
	}
	g.blocked = err != nil
	if g.blocked {
		pressureBlocked.Set(1)
	} else {
		pressureBlocked.Set(0)
	}
	if sampleOK {
		if g.config.MinMemoryPercent > 0 {
			pressureGauge.WithLabelValues("memory_available").Set(s.MemoryAvailablePercent)
		}
		if g.config.MinDiskPercent > 0 {
			pressureGauge.WithLabelValues("disk_available").Set(s.DiskAvailablePercent)
		}
		if g.config.MinInodePercent > 0 {
			pressureGauge.WithLabelValues("inodes_available").Set(s.InodesAvailablePercent)
		}
		if g.config.MaxIOPSI > 0 {
			pressureGauge.WithLabelValues("io_psi_some_avg10").Set(s.IOPSI)
		}
		if g.config.MaxCPUPSI > 0 {
			pressureGauge.WithLabelValues("cpu_psi_some_avg10").Set(s.CPUPSI)
		}
	}
	g.lastError = err
	return err
}
func (g *PressureGuard) evaluate(s PressureSample) error {
	lowFactor, highFactor := 1.0, 1.0
	if g.blocked {
		lowFactor = 1.2
		highFactor = 0.8
	}
	for _, v := range []struct {
		name             string
		value, threshold float64
	}{
		{"memory available", s.MemoryAvailablePercent, g.config.MinMemoryPercent},
		{"disk available", s.DiskAvailablePercent, g.config.MinDiskPercent},
		{"inodes available", s.InodesAvailablePercent, g.config.MinInodePercent},
	} {
		if v.threshold > 0 && v.value < v.threshold*lowFactor {
			return fmt.Errorf("runner pressure: %s %.2f%% below %.2f%%", v.name, v.value, v.threshold*lowFactor)
		}
	}
	for _, v := range []struct {
		name             string
		value, threshold float64
	}{
		{"io PSI some avg10", s.IOPSI, g.config.MaxIOPSI},
		{"cpu PSI some avg10", s.CPUPSI, g.config.MaxCPUPSI},
	} {
		if v.threshold > 0 && v.value >= v.threshold*highFactor {
			return fmt.Errorf("runner pressure: %s %.2f above %.2f", v.name, v.value, v.threshold*highFactor)
		}
	}
	return nil
}

func ReadPressure(c PressureConfig) (PressureSample, error) {
	var s PressureSample
	if c.MinMemoryPercent > 0 {
		data, err := os.ReadFile(filepath.Join(c.ProcRoot, "meminfo"))
		if err != nil {
			return s, err
		}
		total, available := 0.0, -1.0
		for _, line := range strings.Split(string(data), "\n") {
			f := strings.Fields(line)
			if len(f) < 2 {
				continue
			}
			n, err := strconv.ParseFloat(f[1], 64)
			if err != nil {
				continue
			}
			if f[0] == "MemTotal:" {
				total = n
			}
			if f[0] == "MemAvailable:" {
				available = n
			}
		}
		if total <= 0 || available < 0 || available > total || math.IsNaN(total) || math.IsNaN(available) || math.IsInf(total, 0) || math.IsInf(available, 0) {
			return s, fmt.Errorf("runner pressure: missing MemTotal or MemAvailable")
		}
		s.MemoryAvailablePercent = available / total * 100
	}
	if c.MinDiskPercent > 0 || c.MinInodePercent > 0 {
		var st syscall.Statfs_t
		if err := syscall.Statfs(c.DiskPath, &st); err != nil {
			return s, err
		}
		if st.Blocks == 0 {
			return s, fmt.Errorf("runner pressure: filesystem has zero blocks")
		}
		s.DiskAvailablePercent = float64(st.Bavail) / float64(st.Blocks) * 100
		if st.Files > 0 {
			s.InodesAvailablePercent = float64(st.Ffree) / float64(st.Files) * 100
		} else {
			s.InodesAvailablePercent = 100
		}
	}
	for _, v := range []struct {
		name    string
		enabled bool
		target  *float64
	}{
		{"io", c.MaxIOPSI > 0, &s.IOPSI}, {"cpu", c.MaxCPUPSI > 0, &s.CPUPSI},
	} {
		if !v.enabled {
			continue
		}
		data, err := os.ReadFile(filepath.Join(c.ProcRoot, "pressure", v.name))
		if err != nil {
			return s, err
		}
		n, err := parsePSI(string(data))
		if err != nil {
			return s, err
		}
		*v.target = n
	}
	return s, nil
}
func parsePSI(data string) (float64, error) {
	for _, line := range strings.Split(data, "\n") {
		f := strings.Fields(line)
		if len(f) == 0 || f[0] != "some" {
			continue
		}
		for _, field := range f[1:] {
			if strings.HasPrefix(field, "avg10=") {
				value, err := strconv.ParseFloat(strings.TrimPrefix(field, "avg10="), 64)
				if err != nil || math.IsNaN(value) || math.IsInf(value, 0) || value < 0 || value > 100 {
					return 0, fmt.Errorf("runner pressure: invalid PSI avg10 %q", field)
				}
				return value, nil
			}
		}
	}
	return 0, fmt.Errorf("runner pressure: missing PSI some avg10")
}
