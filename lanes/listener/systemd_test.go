package main

import (
	"context"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

// fakeSystemctl writes a stand-in for systemctl that logs its arguments and answers is-active
// the way systemctl does: one state per unit, exit 3 when any unit is not active; show prints
// the given text.
func fakeSystemctl(t *testing.T, states, show string) (systemctl, string) {
	t.Helper()
	dir := t.TempDir()
	calls := filepath.Join(dir, "calls")
	script := "#!/bin/sh\n" +
		"echo \"$*\" >> '" + calls + "'\n" +
		"case \"$1\" in\n" +
		"  is-active) printf '" + states + "'; exit 3 ;;\n" +
		"  show) printf '" + show + "' ;;\n" +
		"  start) case \"$3\" in *@9.service) echo 'Job failed. See journalctl.' >&2; exit 1 ;; esac ;;\n" +
		"esac\n"
	bin := filepath.Join(dir, "systemctl")
	if err := os.WriteFile(bin, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	return systemctl{bin: bin}, calls
}

func TestSystemctlStates(t *testing.T) {
	s, calls := fakeSystemctl(t, `active\ninactive\nactivating\n`, ``)
	units := []string{"lane-ci@1.service", "lane-ci@2.service", "lane-qae@1.service"}
	got, err := s.States(context.Background(), units)
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]string{"lane-ci@1.service": "active", "lane-ci@2.service": "inactive", "lane-qae@1.service": "activating"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("states %v", got)
	}
	if log, _ := os.ReadFile(calls); strings.TrimSpace(string(log)) != "is-active -- "+strings.Join(units, " ") {
		t.Errorf("called with %q", log)
	}
	if !unitRunning("activating") || !unitRunning("deactivating") || unitRunning("inactive") || unitRunning("failed") {
		t.Error("unitRunning misreads a state")
	}
}

func TestSystemctlStatesRefusesAShortAnswer(t *testing.T) {
	s, _ := fakeSystemctl(t, `active\n`, ``)
	if _, err := s.States(context.Background(), []string{"a.service", "b.service"}); err == nil {
		t.Error("one state for two units was accepted")
	}
}

func TestSystemctlStartAndStop(t *testing.T) {
	s, calls := fakeSystemctl(t, ``, ``)
	if err := s.Start(context.Background(), "lane-ci@1.service"); err != nil {
		t.Fatal(err)
	}
	if err := s.Stop(context.Background(), "lane-ci@1.service"); err != nil {
		t.Fatal(err)
	}
	err := s.Start(context.Background(), "lane-ci@9.service")
	if err == nil || !strings.Contains(err.Error(), "Job failed") {
		t.Errorf("a failed start: %v", err)
	}
	log, _ := os.ReadFile(calls)
	want := "start -- lane-ci@1.service\nstop -- lane-ci@1.service\nstart -- lane-ci@9.service\n"
	if string(log) != want {
		t.Errorf("calls %q", log)
	}
}

// TestSystemctlActiveSince: one `systemctl show` for every unit, each block's ActiveEnterTimestamp
// read as @<unix seconds> whatever the order of its lines, and a unit never active left out.
func TestSystemctlActiveSince(t *testing.T) {
	s, calls := fakeSystemctl(t, ``,
		`Id=lane-ci@1.service\nActiveEnterTimestamp=@1790000000\n\nActiveEnterTimestamp=\nId=lane-ci@2.service\n`)
	units := []string{"lane-ci@1.service", "lane-ci@2.service"}
	got, err := s.ActiveSince(context.Background(), units)
	if err != nil {
		t.Fatal(err)
	}
	if want := map[string]time.Time{"lane-ci@1.service": time.Unix(1790000000, 0)}; !reflect.DeepEqual(got, want) {
		t.Errorf("since %v, want %v", got, want)
	}
	if log, _ := os.ReadFile(calls); strings.TrimSpace(string(log)) != "show --timestamp=unix -p Id,ActiveEnterTimestamp -- "+strings.Join(units, " ") {
		t.Errorf("called with %q", log)
	}
}

func TestSystemctlActiveSinceRefusesAnAnswerItCannotRead(t *testing.T) {
	for name, out := range map[string]string{
		"one block for two units": `Id=a.service\nActiveEnterTimestamp=@1\n`,
		"blocks out of order":     `Id=b.service\nActiveEnterTimestamp=@1\n\nId=a.service\nActiveEnterTimestamp=@2\n`,
		"a pretty timestamp":      `Id=a.service\nActiveEnterTimestamp=Mon 2026-09-28 12:00:00 UTC\n\nId=b.service\nActiveEnterTimestamp=\n`,
	} {
		s, _ := fakeSystemctl(t, ``, out)
		if got, err := s.ActiveSince(context.Background(), []string{"a.service", "b.service"}); err == nil {
			t.Errorf("%s: accepted as %v", name, got)
		}
	}
}
