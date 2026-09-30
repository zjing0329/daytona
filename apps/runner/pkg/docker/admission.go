// Copyright 2026 Daytona Platforms Inc.
// SPDX-License-Identifier: AGPL-3.0
package docker

import (
	"context"
	common_errors "github.com/daytonaio/common-go/pkg/errors"
	"github.com/daytonaio/runner/pkg/admission"
	"net/http"
)

// ReserveOperation is also used before starting detached HTTP work. The
// caller owns release until the asynchronous operation has actually completed.
func (d *DockerClient) ReserveOperation(ctx context.Context, class admission.Class) (context.Context, func(), error) {
	leased, release, err := d.admission.Try(ctx, class)
	if err != nil {
		return ctx, nil, common_errors.NewCustomError(http.StatusTooManyRequests, err.Error(), "RUNNER_BUSY")
	}
	return leased, release, nil
}
