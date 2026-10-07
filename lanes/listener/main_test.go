package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/signal"
	"path/filepath"
	"reflect"
	"strings"
	"syscall"
	"testing"
	"time"
)

// readmeConfig is the example config block of README.md.
func readmeConfig(t *testing.T) string {
	t.Helper()
	readme, err := os.ReadFile("README.md")
	if err != nil {
		t.Fatal(err)
	}
	_, rest, ok := strings.Cut(string(readme), "```json\n")
	block, _, closed := strings.Cut(rest, "```")
	if !ok || !closed {
		t.Fatal("README.md has no json block")
	}
	return block
}

func writeConfig(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "lane.json")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestReadmeExampleConfigLoads(t *testing.T) {
	cfg, err := loadConfig(writeConfig(t, readmeConfig(t)))
	if err != nil {
		t.Fatalf("the README's example config is refused: %v", err)
	}
	if cfg.owner() != "example" || cfg.minRunners(SetKey{"example", kindCI}) != 2 || cfg.minRunners(SetKey{"example", kindQAE}) != 0 {
		t.Errorf("config %+v", cfg)
	}
	if got := unitName(cfg.Name, kindQAE, 3); got != "example-ci-qae@3.service" {
		t.Errorf("unit name %q", got)
	}
	// The wait kind has a budget of its own; every other kind draws on budget.slots.
	if cfg.kindSlots(kindWait) != 8 || cfg.kindSlots(kindCI) != 5 || cfg.kindSlots(kindQAE) != 5 ||
		!reflect.DeepEqual(cfg.ownSlots(), map[string]int{kindWait: 8}) ||
		!reflect.DeepEqual(cfg.kindContainerBytes(), map[string]uint64{kindWait: gib}) {
		t.Errorf("per-kind budgets: wait %d, ci %d, own %v, sizes %v", cfg.kindSlots(kindWait), cfg.kindSlots(kindCI), cfg.ownSlots(), cfg.kindContainerBytes())
	}
}

func TestUserScopeConfigAsTheReadmeDescribes(t *testing.T) {
	var doc map[string]any
	if err := json.Unmarshal([]byte(readmeConfig(t)), &doc); err != nil {
		t.Fatal(err)
	}
	doc["scope"], doc["runner_group"] = "user", "default"
	doc["budget"].(map[string]any)["min_runners"] = 0
	doc["repos"] = map[string]any{"exclude": []string{"some-repo"}, "refresh_sec": 600}
	body, _ := json.Marshal(doc)
	cfg, err := loadConfig(writeConfig(t, string(body)))
	if err != nil {
		t.Fatalf("user scope: %v", err)
	}
	if cfg.targetURL("example/repo-a") != "https://github.com/example/repo-a" || cfg.minRunners(SetKey{"example/repo-a", kindCI}) != 0 {
		t.Errorf("config %+v", cfg)
	}
}

func TestLoadConfigRefusesUnknownKeys(t *testing.T) {
	body := strings.Replace(readmeConfig(t), `"idle_stop_sec": 300`, `"idle_stop_sec": 300, "idle_stop_seconds": 60`, 1)
	if _, err := loadConfig(writeConfig(t, body)); err == nil || !strings.Contains(err.Error(), "idle_stop_seconds") {
		t.Errorf("a misspelt key: %v", err)
	}
}

// TestWarmMaxAgeDefaultsWhenAbsent: a config without warm_max_age_sec recycles warm slots after
// 1200 s; one that sets it must set a positive number.
func TestWarmMaxAgeDefaultsWhenAbsent(t *testing.T) {
	var doc map[string]any
	if err := json.Unmarshal([]byte(readmeConfig(t)), &doc); err != nil {
		t.Fatal(err)
	}
	delete(doc, "warm_max_age_sec")
	body, _ := json.Marshal(doc)
	cfg, err := loadConfig(writeConfig(t, string(body)))
	if err != nil || cfg.WarmMaxAgeSec != 1200 {
		t.Fatalf("absent: %v, %+v", err, cfg)
	}
	for _, bad := range []int{0, -1} {
		doc["warm_max_age_sec"] = bad
		body, _ = json.Marshal(doc)
		if _, err := loadConfig(writeConfig(t, string(body))); err == nil || !strings.Contains(err.Error(), "warm_max_age_sec must be positive") {
			t.Errorf("warm_max_age_sec %d: %v", bad, err)
		}
	}
}

func TestLoggerWritesTheFleetLineShape(t *testing.T) {
	var buf bytes.Buffer
	l := &logger{w: &buf, now: func() time.Time { return time.Date(2026, 9, 28, 19, 20, 3, 0, time.FixedZone("x", 3600)) }}
	l.printf("started %s", "example-ci-ci@1.service")
	if got := buf.String(); got != "2026-09-28T18:20:03Z started example-ci-ci@1.service\n" {
		t.Errorf("log line %q", got)
	}
}

// TestExitStatus: a stop signal exits 0 (signal.NotifyContext cancels with a cause of its own,
// which must not read as a failure); the lane's own error exits 1.
func TestExitStatus(t *testing.T) {
	signals, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM)
	defer stop()
	ctx, cancel := context.WithCancelCause(signals)
	defer cancel(nil)
	if err := syscall.Kill(os.Getpid(), syscall.SIGTERM); err != nil {
		t.Fatal(err)
	}
	<-ctx.Done()
	if code, cause := exitStatus(signals, ctx); code != 0 {
		t.Errorf("SIGTERM: exit %d (%v), want 0", code, cause)
	}

	quiet, stopQuiet := signal.NotifyContext(context.Background(), syscall.SIGUSR1)
	defer stopQuiet()
	failed, fail := context.WithCancelCause(quiet)
	fail(errors.New("no message session within 5m0s"))
	if code, cause := exitStatus(quiet, failed); code != 1 || cause == nil || !strings.Contains(cause.Error(), "no message session") {
		t.Errorf("a lane error: exit %d (%v), want 1", code, cause)
	}
}

func TestRunRefusesBadArguments(t *testing.T) {
	for _, args := range [][]string{nil, {"-delete-sets"}, {"-bogus", "lane.json"}, {"a.json", "b.json"}} {
		if code := run(args); code != 2 {
			t.Errorf("run(%q) = %d, want 2", args, code)
		}
	}
}

func TestHeartbeatNamesTheLanesLoad(t *testing.T) {
	h := newHarness(t, orgConfig())
	h.lane.addSet(SetKey{"example", kindCI}, 1, h.api(t, "example"))
	if got := h.lane.heartbeat(); got != "heartbeat: 1 scale sets served, 0 of 3 slots in use" {
		t.Errorf("heartbeat %q", got)
	}
}
