// Copyright 2026 Daytona Platforms Inc.
// SPDX-License-Identifier: AGPL-3.0
package admission

import (
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

var capacityGauge = promauto.NewGaugeVec(prometheus.GaugeOpts{
	Name: "daytona_runner_admission_capacity", Help: "Configured node operation capacity by class.",
}, []string{"class"})
var activeGauge = promauto.NewGaugeVec(prometheus.GaugeOpts{
	Name: "daytona_runner_admission_reserved", Help: "Reserved node operation permits, including in-flight polls.",
}, []string{"class"})
var deniedCounter = promauto.NewCounterVec(prometheus.CounterOpts{
	Name: "daytona_runner_admission_denied_total", Help: "Admission attempts denied by capacity or node pressure.",
}, []string{"class", "reason"})
var pressureGauge = promauto.NewGaugeVec(prometheus.GaugeOpts{
	Name: "daytona_runner_admission_pressure", Help: "Latest enabled pressure source measurements, in percent.",
}, []string{"source"})
var pressureBlocked = promauto.NewGauge(prometheus.GaugeOpts{
	Name: "daytona_runner_admission_pressure_blocked", Help: "Whether the enabled pressure guard blocks new heavy work.",
})
