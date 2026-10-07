package main

import (
	"context"
	"fmt"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

// Systemd is the listener's whole use of systemd; tests replace it.
type Systemd interface {
	// Start runs `systemctl start` and returns once the unit is up or its start failed.
	Start(ctx context.Context, unit string) error
	// Stop runs `systemctl stop`, which also runs the unit's cleanup.
	Stop(ctx context.Context, unit string) error
	// States returns each unit's `systemctl is-active` state.
	States(ctx context.Context, units []string) (map[string]string, error)
	// ActiveSince returns when each unit last entered the active state (its ActiveEnterTimestamp),
	// the moment its RuntimeMaxSec counts from. A unit never active has no entry.
	ActiveSince(ctx context.Context, units []string) (map[string]time.Time, error)
}

type systemctl struct{ bin string }

func (s systemctl) Start(ctx context.Context, unit string) error { return s.run(ctx, "start", unit) }

func (s systemctl) Stop(ctx context.Context, unit string) error { return s.run(ctx, "stop", unit) }

func (s systemctl) run(ctx context.Context, verb, unit string) error {
	out, err := exec.CommandContext(ctx, s.bin, verb, "--", unit).CombinedOutput()
	if err != nil {
		return fmt.Errorf("systemctl %s %s: %w: %s", verb, unit, err, strings.TrimSpace(string(out)))
	}
	return nil
}

// States asks for every unit in one `systemctl is-active` call. It exits non-zero whenever any
// unit is not active and prints one state per unit either way, so the output decides, and a
// count that does not match the units asked for is an error.
func (s systemctl) States(ctx context.Context, units []string) (map[string]string, error) {
	states := make(map[string]string, len(units))
	if len(units) == 0 {
		return states, nil
	}
	out, err := exec.CommandContext(ctx, s.bin, append([]string{"is-active", "--"}, units...)...).Output()
	lines := strings.Fields(string(out))
	if len(lines) != len(units) {
		return nil, fmt.Errorf("systemctl is-active printed %d states for %d units (%v)", len(lines), len(units), err)
	}
	for i, unit := range units {
		states[unit] = lines[i]
	}
	return states, nil
}

// ActiveSince asks for every unit's ActiveEnterTimestamp in one `systemctl show` call, printed as
// @<unix seconds> (--timestamp=unix, systemd 251 and later). -p prints a property even when it is
// empty (a unit never active), and systemctl prints one block per unit, in the order asked,
// separated by a blank line; a block that does not name the unit asked for, or a timestamp in any
// other form, is an error.
func (s systemctl) ActiveSince(ctx context.Context, units []string) (map[string]time.Time, error) {
	since := make(map[string]time.Time, len(units))
	if len(units) == 0 {
		return since, nil
	}
	args := append([]string{"show", "--timestamp=unix", "-p", "Id,ActiveEnterTimestamp", "--"}, units...)
	out, err := exec.CommandContext(ctx, s.bin, args...).Output()
	if err != nil {
		return nil, fmt.Errorf("systemctl show: %w", err)
	}
	blocks := strings.Split(strings.TrimSuffix(string(out), "\n"), "\n\n")
	if len(blocks) != len(units) {
		return nil, fmt.Errorf("systemctl show printed %d units for %d", len(blocks), len(units))
	}
	for i, block := range blocks {
		props := map[string]string{}
		for _, line := range strings.Split(block, "\n") {
			key, value, _ := strings.Cut(line, "=")
			props[key] = value
		}
		if props["Id"] != units[i] {
			return nil, fmt.Errorf("systemctl show answered for %q where %s was asked", props["Id"], units[i])
		}
		stamp := props["ActiveEnterTimestamp"]
		if stamp == "" {
			continue // never active
		}
		secs, err := strconv.ParseInt(strings.TrimPrefix(stamp, "@"), 10, 64)
		if err != nil || !strings.HasPrefix(stamp, "@") {
			return nil, fmt.Errorf("%s: ActiveEnterTimestamp %q is not @<unix seconds>", units[i], stamp)
		}
		since[units[i]] = time.Unix(secs, 0)
	}
	return since, nil
}

// unitRunning reports whether a unit holds its instance: anything but inactive or failed
// (activating covers the slot's prepare, deactivating its cleanup).
func unitRunning(state string) bool {
	switch state {
	case "active", "activating", "deactivating", "reloading", "refreshing":
		return true
	}
	return false
}
