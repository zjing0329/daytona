//go:build runner_embedded_assets

// Copyright 2026 Daytona Platforms Inc.
// SPDX-License-Identifier: AGPL-3.0
package daemon

import (
	"bytes"
	"debug/elf"
	"testing"
)

func TestRequiredStaticBinariesAreAMD64Executables(t *testing.T) {
	for _, name := range []string{"daemon-amd64", "daytona-computer-use"} {
		t.Run(name, func(t *testing.T) {
			data, err := static.ReadFile("static/" + name)
			if err != nil {
				t.Fatal(err)
			}
			binary, err := elf.NewFile(bytes.NewReader(data))
			if err != nil {
				t.Fatalf("required embedded asset must be a real ELF executable: %v", err)
			}
			defer binary.Close()
			if binary.Machine != elf.EM_X86_64 || (binary.Type != elf.ET_EXEC && binary.Type != elf.ET_DYN) {
				t.Fatalf("unexpected embedded executable: machine=%v type=%v", binary.Machine, binary.Type)
			}
		})
	}
}
