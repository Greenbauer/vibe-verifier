package main

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/actions/scaleset"
)

// startedLane is a harness whose org ci set has `assigned` jobs, reconciled once.
func startedLane(t *testing.T, cfg *Config, assigned int) (*harness, *fakeAPI) {
	t.Helper()
	h := newHarness(t, cfg)
	a := h.api(t, "example")
	h.lane.addSet(SetKey{"example", kindCI}, 41, a)
	h.lane.setAssigned(SetKey{"example", kindCI}, assigned)
	h.settle(t)
	return h, a
}

func TestLaneStartWritesTheRunDirThenStartsTheUnit(t *testing.T) {
	h, a := startedLane(t, orgConfig(), 2)
	started, _ := h.sys.calls()
	if !reflect.DeepEqual(started, []string{"example-ci-ci@1.service", "example-ci-ci@2.service"}) &&
		!reflect.DeepEqual(started, []string{"example-ci-ci@2.service", "example-ci-ci@1.service"}) {
		t.Fatalf("started %v", started)
	}
	for n := 1; n <= 2; n++ {
		name := fmt.Sprintf("box-ci-ci-%d-%d", n, t0.Unix())
		job, err := os.ReadFile(jobPath(h.run, kindCI, n))
		if err != nil {
			t.Fatal(err)
		}
		if !strings.HasPrefix(string(job), "example ci 41 ") || !strings.HasSuffix(string(job), " "+name+"\n") {
			t.Errorf("job file %d = %q", n, job)
		}
		jit, _ := os.ReadFile(filepath.Join(h.run, kindCI, fmt.Sprint(n), jitFile))
		if string(jit) != "JIT-FOR-"+name {
			t.Errorf("jit %d = %q", n, jit)
		}
	}
	for _, s := range a.snapshot().jits {
		if s.WorkFolder != "/home/runner/_work" || !strings.HasPrefix(s.Name, "box-ci-ci-") {
			t.Errorf("JIT setting %+v", s)
		}
	}
	for _, s := range h.lane.trackedSlots() {
		if s.Phase != PhaseIdle || s.RunnerID == 0 {
			t.Errorf("slot %+v", s)
		}
	}
	if strings.Contains(h.log.all(), "JIT-FOR-") {
		t.Error("the JIT config reached the log")
	}
}

func TestLaneFailedStartRemovesTheRunnerAndItsFiles(t *testing.T) {
	cfg := orgConfig()
	h := newHarness(t, cfg)
	h.sys.startErr["example-ci-ci@1.service"] = errBoom
	a := h.api(t, "example")
	h.lane.addSet(SetKey{"example", kindCI}, 41, a)
	h.lane.setAssigned(SetKey{"example", kindCI}, 1)
	h.settle(t)
	snap := a.snapshot()
	if len(snap.removed) != 1 || snap.removed[0] != 1001 {
		t.Fatalf("removed runners %v, want the minted 1001", snap.removed)
	}
	if _, err := os.Stat(slotDir(h.run, kindCI, 1)); !os.IsNotExist(err) {
		t.Errorf("slot files survived a failed start: %v", err)
	}
	if got := h.lane.trackedSlots(); len(got) != 0 {
		t.Errorf("a failed start is still tracked: %+v", got)
	}
	// The next round tries again.
	delete(h.sys.startErr, "example-ci-ci@1.service")
	h.settle(t)
	if got := h.lane.trackedSlots(); len(got) != 1 || got[0].Phase != PhaseIdle {
		t.Errorf("retry: %+v", got)
	}
}

func TestLaneFailedMintLeavesNothingBehind(t *testing.T) {
	h := newHarness(t, orgConfig())
	a := h.api(t, "example")
	a.jitErr = errBoom
	h.lane.addSet(SetKey{"example", kindCI}, 41, a)
	h.lane.setAssigned(SetKey{"example", kindCI}, 1)
	h.settle(t)
	if started, _ := h.sys.calls(); len(started) != 0 || len(h.lane.trackedSlots()) != 0 {
		t.Errorf("started %v, tracked %v", started, h.lane.trackedSlots())
	}
	if _, err := os.Stat(slotDir(h.run, kindCI, 1)); !os.IsNotExist(err) {
		t.Errorf("files written without a runner: %v", err)
	}
}

func TestLaneFollowsAJobToItsEnd(t *testing.T) {
	h, _ := startedLane(t, orgConfig(), 1)
	s := h.lane.trackedSlots()[0]
	h.lane.jobStarted(SetKey{"example", kindCI}, &scaleset.JobStarted{RunnerName: s.RunnerName})
	if got := h.lane.trackedSlots()[0].Phase; got != PhaseBusy {
		t.Fatalf("after JobStarted: %s", got)
	}
	h.lane.jobCompleted(SetKey{"example", kindCI}, &scaleset.JobCompleted{RunnerName: s.RunnerName, Result: "succeeded"})
	h.lane.setAssigned(SetKey{"example", kindCI}, 0)
	h.settle(t)
	if got := h.lane.trackedSlots(); len(got) != 1 || got[0].Phase != PhaseDone {
		t.Fatalf("a completed slot whose unit still runs its cleanup: %+v", got)
	}
	// The slot's cleanup removes the job file last, then the unit is inactive.
	if err := removeSlotFiles(h.run, kindCI, 1); err != nil {
		t.Fatal(err)
	}
	h.sys.set("example-ci-ci@1.service", "inactive")
	h.settle(t)
	if got := h.lane.trackedSlots(); len(got) != 0 {
		t.Errorf("a finished slot is still tracked: %+v", got)
	}
	if started, stopped := h.sys.calls(); len(started) != 1 || len(stopped) != 0 {
		t.Errorf("started %v, stopped %v: the listener never stops a slot that ran a job", started, stopped)
	}
}

func TestLaneIdleStopRemovesTheRunnerBeforeStoppingTheUnit(t *testing.T) {
	cfg := userConfig()
	h := newHarness(t, cfg)
	key := SetKey{"example/repo-a", kindCI}
	a := h.api(t, key.Target)
	h.lane.addSet(key, 8, a)
	h.lane.setAssigned(key, 1)
	h.settle(t)
	runner := h.lane.trackedSlots()[0].RunnerID

	var markerAtStop bool
	var removedAtStop []int64
	h.sys.onStop = func(string) {
		_, err := os.Stat(filepath.Join(slotDir(h.run, kindCI, 1), idleStopFile))
		markerAtStop = err == nil
		removedAtStop = a.snapshot().removed
	}
	h.lane.setAssigned(key, 0) // the job was cancelled before the runner took it
	h.advance(4 * time.Minute)
	h.settle(t)
	if _, stopped := h.sys.calls(); len(stopped) != 0 {
		t.Fatalf("stopped %v before idle_stop_sec", stopped)
	}
	h.advance(2 * time.Minute)
	h.settle(t)
	if _, stopped := h.sys.calls(); !reflect.DeepEqual(stopped, []string{"example-ci-ci@1.service"}) {
		t.Fatalf("stopped %v", stopped)
	}
	if !markerAtStop || !reflect.DeepEqual(removedAtStop, []int64{runner}) {
		t.Errorf("at stop: marker %v, removed %v; want the marker and runner %d removed first", markerAtStop, removedAtStop, runner)
	}
	// The unit is gone; the slot's cleanup would remove the files. Here they are left, so the
	// next round finds a job file with no running unit and clears it.
	h.settle(t)
	if len(h.lane.trackedSlots()) != 0 {
		t.Errorf("tracked after the stop: %+v", h.lane.trackedSlots())
	}
	if _, err := os.Stat(slotDir(h.run, kindCI, 1)); !os.IsNotExist(err) {
		t.Errorf("leftover files not cleared: %v", err)
	}
}

func TestLaneIdleStopLeavesARunnerThatJustTookAJob(t *testing.T) {
	h := newHarness(t, userConfig())
	key := SetKey{"example/repo-a", kindCI}
	a := h.api(t, key.Target)
	h.lane.addSet(key, 8, a)
	h.lane.setAssigned(key, 1)
	h.settle(t)
	a.mu.Lock()
	a.removeErr = fmt.Errorf("request failed: %w", scaleset.JobStillRunningError)
	a.mu.Unlock()
	h.lane.setAssigned(key, 0)
	h.advance(10 * time.Minute)
	h.settle(t)
	if _, stopped := h.sys.calls(); len(stopped) != 0 {
		t.Fatalf("a runner GitHub would not remove was stopped: %v", stopped)
	}
	if _, err := os.Stat(filepath.Join(slotDir(h.run, kindCI, 1), idleStopFile)); !os.IsNotExist(err) {
		t.Errorf("the idle-stop marker was left behind: %v", err)
	}
	s := h.lane.trackedSlots()[0]
	if s.Phase != PhaseIdle || !s.Since.Equal(h.now) {
		t.Errorf("slot %+v, want idle and aged afresh", s)
	}
	h.settle(t) // aged afresh: no second attempt straight away
	if n := len(a.snapshot().removed); n != 1 {
		t.Errorf("%d removal attempts, want 1", n)
	}
}

func TestLaneRebuildAdoptsRunningSlotsAndClearsLeftovers(t *testing.T) {
	h := newHarness(t, userConfig())
	if err := writeSlotFiles(h.run, kindCI, 1, "J1", formatJobLine("example/repo-a", kindCI, 8, 501, "box-ci-ci-1-1")); err != nil {
		t.Fatal(err)
	}
	if err := writeSlotFiles(h.run, kindCI, 2, "J2", formatJobLine("example/repo-b", kindCI, 9, 502, "box-ci-ci-2-1")); err != nil {
		t.Fatal(err)
	}
	h.sys.set("example-ci-ci@1.service", "active")
	h.lane.rebuild()
	if !h.lane.waitInflight(5 * time.Second) {
		t.Fatal("rebuild removals did not finish")
	}
	got := h.lane.trackedSlots()
	if len(got) != 1 || got[0].Instance != 1 || got[0].RunnerID != 501 || got[0].Set != (SetKey{"example/repo-a", kindCI}) {
		t.Fatalf("adopted %+v", got)
	}
	if _, err := os.Stat(slotDir(h.run, kindCI, 2)); !os.IsNotExist(err) {
		t.Errorf("a leftover job file of an inactive unit survived: %v", err)
	}
	if removed := h.api(t, "example/repo-b").snapshot().removed; !reflect.DeepEqual(removed, []int64{502}) {
		t.Errorf("leftover runner removals %v", removed)
	}
	// The adopted slot counts against its set: one assigned job starts nothing more.
	key := SetKey{"example/repo-a", kindCI}
	h.lane.addSet(key, 8, h.api(t, key.Target))
	h.lane.setAssigned(key, 1)
	h.settle(t)
	if started, _ := h.sys.calls(); len(started) != 0 {
		t.Errorf("started %v next to an adopted slot", started)
	}
}

func TestLaneCountsAUnitItDidNotStart(t *testing.T) {
	cfg := orgConfig()
	cfg.Budget.Slots = 1
	h := newHarness(t, cfg)
	h.sys.set("example-ci-ci@1.service", "active")
	a := h.api(t, "example")
	h.lane.addSet(SetKey{"example", kindCI}, 41, a)
	h.lane.setAssigned(SetKey{"example", kindCI}, 1)
	h.settle(t)
	if started, _ := h.sys.calls(); len(started) != 0 {
		t.Errorf("started %v while the only instance runs something else", started)
	}
	if !h.log.has("without a slot this listener started") || !h.log.has("budget") {
		t.Errorf("log:\n%s", h.log.all())
	}
}

func TestLaneAdmissionRefusalIsLoggedWithMemAvailable(t *testing.T) {
	h := newHarness(t, orgConfig())
	h.host.mem = 3 * gib
	a := h.api(t, "example")
	h.lane.addSet(SetKey{"example", kindCI}, 41, a)
	h.lane.setAssigned(SetKey{"example", kindCI}, 2)
	h.settle(t)
	h.settle(t) // a repeat within a minute is not logged again
	if started, _ := h.sys.calls(); len(started) != 0 {
		t.Fatalf("started %v", started)
	}
	if n := strings.Count(h.log.all(), "admission"); n != 1 || !h.log.has("MemAvailable 3221225472 bytes") {
		t.Errorf("admission logged %d times:\n%s", n, h.log.all())
	}
}

func TestLaneQAEWaitsForItsRequiredFile(t *testing.T) {
	h := newHarness(t, orgConfig())
	h.host.files["/var/lib/example-ci/store/auth.json"] = false
	a := h.api(t, "example")
	key := SetKey{"example", kindQAE}
	h.lane.addSet(key, 42, a)
	h.lane.setAssigned(key, 1)
	h.settle(t)
	if started, _ := h.sys.calls(); len(started) != 0 || !h.log.has("store/auth.json missing for every free instance") {
		t.Fatalf("started %v; log:\n%s", started, h.log.all())
	}
	h.host.files["/var/lib/example-ci/store/auth.json"] = true
	h.settle(t)
	if started, _ := h.sys.calls(); !reflect.DeepEqual(started, []string{"example-ci-qae@1.service"}) {
		t.Errorf("started %v", started)
	}
}

// TestLaneWaitKindHasItsOwnBudget: with the shared budget held (here by a unit the listener did not
// start), waiters still start on their own instances, and the heartbeat reports both budgets.
func TestLaneWaitKindHasItsOwnBudget(t *testing.T) {
	cfg := waitConfig()
	cfg.Budget.Slots = 1
	cfg.Kinds[kindWait] = KindConfig{SetName: "example-wait", Labels: []string{"self-hosted", "example-wait"}, Slots: 2, ContainerMemoryBytes: gib}
	h := newHarness(t, cfg)
	h.sys.set("example-ci-ci@1.service", "active")
	a := h.api(t, "example")
	h.lane.addSet(SetKey{"example", kindCI}, 41, a)
	h.lane.addSet(SetKey{"example", kindWait}, 43, a)
	h.lane.setAssigned(SetKey{"example", kindCI}, 1)
	h.lane.setAssigned(SetKey{"example", kindWait}, 3)
	h.settle(t)
	started, _ := h.sys.calls()
	if want := []string{"example-ci-wait@1.service", "example-ci-wait@2.service"}; !reflect.DeepEqual(sorted(started), want) {
		t.Fatalf("started %v, want %v; log:\n%s", started, want, h.log.all())
	}
	if !h.log.has("no start for example ci: budget: 1 of 1 slots in use") {
		t.Errorf("log:\n%s", h.log.all())
	}
	if got := h.lane.heartbeat(); got != "heartbeat: 2 scale sets served, 1 of 1 slots in use, 2 of 2 wait slots" {
		t.Errorf("heartbeat %q", got)
	}
}

// TestLaneWaitSlotsStartWhileAQAEStoreIsMissing: the wait kind's own budget and qae's per-instance
// Codex stores compose. With qae's only store not logged in, a qae job is refused while the waiters
// start on their own instances, and the qae job starts once the store is seeded.
func TestLaneWaitSlotsStartWhileAQAEStoreIsMissing(t *testing.T) {
	cfg := waitConfig()
	store := "/var/lib/example-ci/store/auth.json"
	h := newHarness(t, cfg)
	h.host.files[store] = false
	a := h.api(t, "example")
	qae, wait := SetKey{"example", kindQAE}, SetKey{"example", kindWait}
	h.lane.addSet(qae, 42, a)
	h.lane.addSet(wait, 43, a)
	h.lane.setAssigned(qae, 1)
	h.lane.setAssigned(wait, 2)
	h.settle(t)
	started, _ := h.sys.calls()
	if want := []string{"example-ci-wait@1.service", "example-ci-wait@2.service"}; !reflect.DeepEqual(sorted(started), want) || !h.log.has(store+" missing for every free instance") {
		t.Fatalf("started %v, want %v; log:\n%s", started, want, h.log.all())
	}
	h.host.files[store] = true
	h.settle(t)
	if started, _ := h.sys.calls(); !reflect.DeepEqual(sorted(started), []string{"example-ci-qae@1.service", "example-ci-wait@1.service", "example-ci-wait@2.service"}) {
		t.Errorf("started %v", started)
	}
}

// TestLaneQAEInstanceWaitsForItsOwnStore: with qae_concurrency 2 each qae instance has its own Codex
// login store. Instance 1's store is not logged in yet, so a job starts on instance 2, whose store is,
// and a second job waits rather than taking instance 1; once store 1 holds a login it starts there.
func TestLaneQAEInstanceWaitsForItsOwnStore(t *testing.T) {
	cfg := orgConfig()
	cfg.Budget.Slots, cfg.Budget.QAEConcurrency = 4, 2
	store1, store2 := "/var/lib/example-ci/codex/auth.json", "/var/lib/example-ci/codex-2/auth.json"
	cfg.Kinds[kindQAE] = KindConfig{SetName: "example-qae", Labels: []string{"example-qae"}, RequiresFiles: []string{store1, store2}}
	if err := cfg.validate(); err != nil {
		t.Fatalf("qae_concurrency 2 with a store per instance is refused: %v", err)
	}
	h := newHarness(t, cfg)
	h.host.files[store1], h.host.files[store2] = false, true
	a := h.api(t, "example")
	key := SetKey{"example", kindQAE}
	h.lane.addSet(key, 42, a)
	h.lane.setAssigned(key, 2)
	h.settle(t)
	if started, _ := h.sys.calls(); !reflect.DeepEqual(started, []string{"example-ci-qae@2.service"}) || !h.log.has(store1+" missing for every free instance") {
		t.Fatalf("started %v; log:\n%s", started, h.log.all())
	}
	h.host.files[store1] = true
	h.settle(t)
	if started, _ := h.sys.calls(); !reflect.DeepEqual(started, []string{"example-ci-qae@2.service", "example-ci-qae@1.service"}) {
		t.Errorf("started %v", started)
	}
}

// TestSupervisedSetStartsASlotThroughTheListenerPackage runs the real listener package over a
// fake session: the session's initial statistics (one assigned job) reach the lane and start one
// slot; a failed long poll closes the session and a new one is opened; cancelling closes it.
func TestSupervisedSetStartsASlotThroughTheListenerPackage(t *testing.T) {
	cfg := orgConfig()
	h := newHarness(t, cfg)
	a := h.api(t, "example")
	first := true
	a.newSession = func() *fakeSession {
		s := &fakeSession{assigned: 1}
		if first {
			s.getErr, first = errBoom, false
		}
		return s
	}
	m := newSetManager(cfg, h.lane, "box", h.log.logf, func(error) {})
	m.reopenDelay = time.Millisecond
	key := SetKey{"example", kindCI}
	h.lane.addSet(key, 41, a)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		m.superviseSet(ctx, key, a, 41)
		close(done)
	}()
	deadline := time.Now().Add(5 * time.Second)
	for {
		a.mu.Lock()
		opened := len(a.sessions)
		a.mu.Unlock()
		started, _ := h.sys.calls()
		if opened == 2 && len(started) == 1 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("sessions %d, started %v; log:\n%s", opened, started, h.log.all())
		}
		time.Sleep(5 * time.Millisecond)
	}
	cancel()
	<-done
	h.lane.waitInflight(5 * time.Second)
	a.mu.Lock()
	defer a.mu.Unlock()
	if !a.sessions[0].isClosed() || !a.sessions[1].isClosed() {
		t.Errorf("sessions closed: %v %v", a.sessions[0].isClosed(), a.sessions[1].isClosed())
	}
	if started, _ := h.sys.calls(); len(started) != 1 {
		t.Errorf("started %v, want one slot for one assigned job across both sessions", started)
	}
}

// TestLaneStopInFlightKeepsItsInstance: while an idle stop is in flight its instance stays
// reserved, even if the unit went inactive and its files are gone, so the stop cannot land on a
// slot started on the same instance.
func TestLaneStopInFlightKeepsItsInstance(t *testing.T) {
	cfg := userConfig()
	cfg.Budget.Slots = 1
	h := newHarness(t, cfg)
	keyA, keyB := SetKey{"example/repo-a", kindCI}, SetKey{"example/repo-b", kindCI}
	a := h.api(t, keyA.Target)
	h.lane.addSet(keyA, 8, a)
	h.lane.setAssigned(keyA, 1)
	h.settle(t)
	h.lane.setAssigned(keyA, 0)
	h.advance(10 * time.Minute)
	gate := make(chan struct{})
	a.mu.Lock()
	a.removeGate = gate
	a.mu.Unlock()
	h.lane.reconcile() // the idle stop begins: marker written, then it waits on GitHub
	marker := filepath.Join(slotDir(h.run, kindCI, 1), idleStopFile)
	waitFor(t, func() bool { _, err := os.Stat(marker); return err == nil }, "the idle-stop marker")

	// Meanwhile the runner exits, the slot's cleanup runs, and another set has a job.
	if err := removeSlotFiles(h.run, kindCI, 1); err != nil {
		t.Fatal(err)
	}
	h.sys.set("example-ci-ci@1.service", "inactive")
	h.lane.addSet(keyB, 9, h.api(t, keyB.Target))
	h.lane.setAssigned(keyB, 1)
	h.lane.reconcile()
	// A start reserves its instance synchronously, so the tracked slots show it at once.
	if s := h.lane.trackedSlots(); len(s) != 1 || s[0].Set != keyA || s[0].Phase != PhaseStopping {
		t.Fatalf("while the stop is in flight the lane tracks %+v; log:\n%s", s, h.log.all())
	}
	close(gate)
	if !h.lane.waitInflight(5 * time.Second) {
		t.Fatal("the stop did not finish")
	}
	h.settle(t) // now the instance frees, and repo-b's job gets it
	started, stopped := h.sys.calls()
	if len(started) != 2 || !reflect.DeepEqual(stopped, []string{"example-ci-ci@1.service"}) {
		t.Errorf("started %v, stopped %v", started, stopped)
	}
	if s := h.lane.trackedSlots(); len(s) != 1 || s[0].Set != keyB {
		t.Errorf("tracked %+v", s)
	}
}

func TestLaneRemovesTheRunnerOfASlotThatExitedWithoutAJob(t *testing.T) {
	h, a := startedLane(t, orgConfig(), 1)
	runner := h.lane.trackedSlots()[0].RunnerID
	if err := removeSlotFiles(h.run, kindCI, 1); err != nil { // the unit ended (RuntimeMaxSec) and cleaned up
		t.Fatal(err)
	}
	h.sys.set("example-ci-ci@1.service", "inactive")
	h.lane.setAssigned(SetKey{"example", kindCI}, 0)
	h.settle(t)
	if removed := a.snapshot().removed; !reflect.DeepEqual(removed, []int64{runner}) {
		t.Errorf("removed %v, want the idle slot's runner %d", removed, runner)
	}
}

func TestLaneIdleStopsASlotWhoseSetIsNoLongerServed(t *testing.T) {
	h := newHarness(t, userConfig())
	key := SetKey{"example/repo-a", kindCI}
	h.lane.addSet(key, 8, h.api(t, key.Target))
	h.lane.setAssigned(key, 1)
	h.settle(t)
	h.lane.removeSet(key) // the repository was dropped
	h.advance(10 * time.Minute)
	h.settle(t)
	if _, stopped := h.sys.calls(); !reflect.DeepEqual(stopped, []string{"example-ci-ci@1.service"}) {
		t.Errorf("stopped %v, want the dropped set's idle slot", stopped)
	}
}

// warmLane is an org lane with a warm pool of two, both started at t0 (their units' start time).
func warmLane(t *testing.T) (*harness, *fakeAPI) {
	t.Helper()
	cfg := orgConfig()
	cfg.Budget.MinRunners = 2
	h, a := startedLane(t, cfg, 0)
	if got := h.lane.trackedSlots(); len(got) != 2 {
		t.Fatalf("warm pool %+v", got)
	}
	return h, a
}

// TestLaneRecyclesWarmSlotsOneAtATime: a warm slot whose unit has been up past warm_max_age_sec
// is recycled the way an idle stop stops a slot (marker, runner removed, then the unit stopped), a
// fresh one starts in its place, and the other old slot goes only once the fresh one is up.
func TestLaneRecyclesWarmSlotsOneAtATime(t *testing.T) {
	h, a := warmLane(t)
	h.advance(19 * time.Minute)
	h.settle(t)
	if _, stopped := h.sys.calls(); len(stopped) != 0 {
		t.Fatalf("recycled %v before warm_max_age_sec", stopped)
	}
	first := h.lane.trackedSlots()[0]
	var markerAtStop bool
	var removedAtStop []int64
	h.sys.onStop = func(unit string) {
		if unit == "example-ci-ci@1.service" {
			_, err := os.Stat(filepath.Join(slotDir(h.run, kindCI, 1), idleStopFile))
			markerAtStop, removedAtStop = err == nil, a.snapshot().removed
		}
	}
	h.advance(2 * time.Minute)
	h.settle(t)
	if _, stopped := h.sys.calls(); !reflect.DeepEqual(stopped, []string{"example-ci-ci@1.service"}) {
		t.Fatalf("stopped %v, want one warm slot per plan", stopped)
	}
	if !markerAtStop || !reflect.DeepEqual(removedAtStop, []int64{first.RunnerID}) {
		t.Errorf("at stop: marker %v, removed %v; want the marker and runner %d removed first", markerAtStop, removedAtStop, first.RunnerID)
	}
	if !h.log.has("example-ci-ci@1.service has been up 21m0s without a job") || !h.log.has("warm-recycled example-ci-ci@1.service") {
		t.Errorf("log:\n%s", h.log.all())
	}
	h.settle(t) // the recycled slot is gone: a fresh one starts, and the other old one waits for it
	if started, stopped := h.sys.calls(); len(started) != 3 || len(stopped) != 1 {
		t.Fatalf("started %v, stopped %v", started, stopped)
	}
	h.settle(t) // the fresh one is up: now the other old one goes
	if _, stopped := h.sys.calls(); !reflect.DeepEqual(stopped, []string{"example-ci-ci@1.service", "example-ci-ci@2.service"}) {
		t.Fatalf("stopped %v", stopped)
	}
	h.settle(t)
	slots := h.lane.trackedSlots()
	if started, _ := h.sys.calls(); len(started) != 4 || len(slots) != 2 {
		t.Fatalf("started %v, tracked %+v", started, slots)
	}
	for _, s := range slots {
		if s.Phase != PhaseIdle || s.RunnerID == first.RunnerID {
			t.Errorf("after both recycles: %+v", s)
		}
	}
}

// TestLaneWarmRecycleLeavesARunnerThatJustTookAJob: GitHub refusing the removal (422) leaves the
// slot running with its marker gone; once its job is reported it is busy and never tried again.
func TestLaneWarmRecycleLeavesARunnerThatJustTookAJob(t *testing.T) {
	h, a := warmLane(t)
	a.mu.Lock()
	a.removeErr = fmt.Errorf("request failed: %w", scaleset.JobStillRunningError)
	a.mu.Unlock()
	first := h.lane.trackedSlots()[0]
	h.advance(21 * time.Minute)
	h.settle(t)
	if _, stopped := h.sys.calls(); len(stopped) != 0 {
		t.Fatalf("a runner GitHub would not remove was stopped: %v", stopped)
	}
	if _, err := os.Stat(filepath.Join(slotDir(h.run, kindCI, 1), idleStopFile)); !os.IsNotExist(err) {
		t.Errorf("the idle-stop marker was left behind: %v", err)
	}
	if s := h.lane.trackedSlots()[0]; s.Phase != PhaseIdle || s.RunnerID != first.RunnerID {
		t.Errorf("slot %+v, want it left idle", s)
	}
	key := SetKey{"example", kindCI}
	h.lane.jobStarted(key, &scaleset.JobStarted{RunnerName: first.RunnerName})
	h.lane.setAssigned(key, 1)
	h.settle(t)
	h.settle(t)
	attempts := 0
	for _, id := range a.snapshot().removed {
		if id == first.RunnerID {
			attempts++
		}
	}
	if attempts != 1 {
		t.Errorf("%d removals of the runner that took a job, want the one refused", attempts)
	}
}

// TestLaneWarmRecycleWaitsIdleStopSecAfterARefusal: a slot whose recycle GitHub refused (422) is
// not tried again until idle_stop_sec after the refusal, however often the lane plans in between,
// although its unit stays past warm_max_age_sec the whole time.
func TestLaneWarmRecycleWaitsIdleStopSecAfterARefusal(t *testing.T) {
	idleStop := time.Duration(orgConfig().IdleStopSec) * time.Second
	cases := []struct {
		name  string
		after time.Duration // from the refusal to the next plan
		tries int           // removals of the slot's runner asked for, the refused one included
	}{
		{"not at the next plan, 5 s later", 5 * time.Second, 1},
		{"not a second before idle_stop_sec", idleStop - time.Second, 1},
		{"again at idle_stop_sec", idleStop, 2},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := orgConfig()
			cfg.Budget.MinRunners = 1
			h, a := startedLane(t, cfg, 0)
			a.mu.Lock()
			a.removeErr = fmt.Errorf("request failed: %w", scaleset.JobStillRunningError)
			a.mu.Unlock()
			warm := h.lane.trackedSlots()[0]
			h.advance(21 * time.Minute)
			h.settle(t) // the recycle is refused at t
			h.advance(tc.after)
			h.settle(t)
			tries := 0
			for _, id := range a.snapshot().removed {
				if id == warm.RunnerID {
					tries++
				}
			}
			if tries != tc.tries {
				t.Errorf("%d removals of runner %d asked, want %d", tries, warm.RunnerID, tc.tries)
			}
			if _, stopped := h.sys.calls(); len(stopped) != 0 {
				t.Errorf("a runner GitHub would not remove was stopped: %v", stopped)
			}
		})
	}
}

// TestLaneWarmRecycleNeedsAnActiveUnitAndItsStartTime: an unreadable start time recycles nothing
// (and says so), and a unit still in its prepare is not even asked about, since its timestamp is
// the previous run's.
func TestLaneWarmRecycleNeedsAnActiveUnitAndItsStartTime(t *testing.T) {
	h, _ := warmLane(t)
	h.advance(21 * time.Minute)
	h.sys.mu.Lock()
	h.sys.sinceErr = errBoom
	h.sys.mu.Unlock()
	h.settle(t)
	if _, stopped := h.sys.calls(); len(stopped) != 0 || !h.log.has("unit start times unreadable") {
		t.Fatalf("stopped %v; log:\n%s", stopped, h.log.all())
	}
	h.sys.mu.Lock()
	h.sys.sinceErr, h.sys.sinceAsked = nil, nil
	h.sys.mu.Unlock()
	h.sys.set("example-ci-ci@1.service", "activating")
	h.sys.set("example-ci-ci@2.service", "activating")
	h.settle(t)
	h.sys.mu.Lock()
	asked := h.sys.sinceAsked
	h.sys.mu.Unlock()
	if _, stopped := h.sys.calls(); len(stopped) != 0 || len(asked) != 0 {
		t.Errorf("stopped %v, asked the start time of %v", stopped, asked)
	}
}
