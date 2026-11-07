package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"

	"github.com/actions/scaleset"
)

// TestPlanQAEWithItsOwnBudget: QAE with slots of its own neither waits behind CI for a shared slot
// nor takes one from it.
func TestPlanQAEWithItsOwnBudget(t *testing.T) {
	repo := "example/repo-a"
	busy := func(kind string, n int) Slot { return slot(repo, kind, n, PhaseBusy, time.Minute) }
	own := func(in PlanInput) PlanInput {
		in.QAEConcurrency, in.Instances, in.OwnSlots = 2, map[string]int{kindQAE: 2}, map[string]int{kindQAE: 2}
		return in
	}
	// CI holds all four shared slots; QAE still starts both of its own.
	full := own(input(4, []SetStatus{set(repo, kindCI, 5, 0), set(repo, kindQAE, 2, 0)},
		busy(kindCI, 1), busy(kindCI, 2), busy(kindCI, 3), busy(kindCI, 4)))
	if got, want := makePlan(full).Starts, []Start{{SetKey{repo, kindQAE}, 1}, {SetKey{repo, kindQAE}, 2}}; !reflect.DeepEqual(got, want) {
		t.Errorf("a full lane: starts %v, want %v", got, want)
	}
	// Two busy QAE slots leave CI every shared one; sharing the budget of four they would leave it two.
	qaeBusy := own(input(4, []SetStatus{set(repo, kindCI, 4, 0), set(repo, kindQAE, 3, 0)}, busy(kindQAE, 1), busy(kindQAE, 2)))
	want := []Start{{SetKey{repo, kindCI}, 1}, {SetKey{repo, kindCI}, 2}, {SetKey{repo, kindCI}, 3}, {SetKey{repo, kindCI}, 4}}
	if got := makePlan(qaeBusy).Starts; !reflect.DeepEqual(got, want) {
		t.Errorf("QAE full: starts %v, want %v", got, want)
	}
}

// TestLaneQAEKindWithItsOwnBudget: on a lane whose QAE has slots of its own, QAE starts while CI
// holds every shared slot, the heartbeat reports both budgets and queue.json names QAE's.
func TestLaneQAEKindWithItsOwnBudget(t *testing.T) {
	cfg := qaeOwnConfig()
	cfg.Budget.Slots = 3
	h := newHarness(t, cfg)
	for _, store := range cfg.Kinds[kindQAE].RequiresFiles {
		h.host.files[store] = true
	}
	for n := 1; n <= 3; n++ {
		h.sys.set(fmt.Sprintf("example-ci-ci@%d.service", n), "active")
	}
	a := h.api(t, "example")
	h.lane.addSet(SetKey{"example", kindCI}, 41, a)
	h.lane.addSet(SetKey{"example", kindQAE}, 42, a)
	h.lane.setAssigned(SetKey{"example", kindCI}, 1)
	h.lane.setAssigned(SetKey{"example", kindQAE}, 5)
	h.lane.recordStatistics(SetKey{"example", kindQAE}, &scaleset.RunnerScaleSetStatistic{TotalAssignedJobs: 5})
	h.settle(t)
	started, _ := h.sys.calls()
	if want := []string{"example-ci-qae@1.service", "example-ci-qae@2.service", "example-ci-qae@3.service"}; !reflect.DeepEqual(sorted(started), want) {
		t.Fatalf("started %v, want %v; log:\n%s", started, want, h.log.all())
	}
	if got := h.lane.heartbeat(); got != "heartbeat: 2 scale sets served, 3 of 3 slots in use, 3 of 3 qae slots" {
		t.Errorf("heartbeat %q", got)
	}
	var queue struct {
		QAESlots int `json:"qae_slots"`
	}
	raw, err := os.ReadFile(filepath.Join(h.run, queueFile))
	if err != nil || json.Unmarshal(raw, &queue) != nil || queue.QAESlots != 3 {
		t.Errorf("queue.json %s (%v): want qae_slots 3", raw, err)
	}
}
