package main

import (
	"fmt"
	"sort"
	"strings"
	"time"
)

// SetKey names one scale set: its scope target (the organisation, or owner/repo) and its kind.
type SetKey struct {
	Target string
	Kind   string
}

func (k SetKey) String() string { return k.Target + " " + k.Kind }

// Phase is where a slot is in its life, as far as the listener knows.
type Phase int

const (
	// PhaseStarting: reserved; the JIT mint, the run-dir files and `systemctl start` are in flight.
	PhaseStarting Phase = iota
	// PhaseIdle: the unit is up and its runner has not taken a job.
	PhaseIdle
	// PhaseBusy: GitHub reported JobStarted on its runner.
	PhaseBusy
	// PhaseStopping: an idle stop is in flight.
	PhaseStopping
	// PhaseDone: its job completed, or it was found running without a set; it holds its instance
	// until the unit is inactive and its job file is gone.
	PhaseDone
)

var phaseNames = [...]string{"starting", "idle", "busy", "stopping", "done"}

func (p Phase) String() string { return phaseNames[p] }

// active reports whether the slot serves its set's demand: a runner that is coming, waiting or working.
func (p Phase) active() bool { return p == PhaseStarting || p == PhaseIdle || p == PhaseBusy }

// slotRef is one instance of one kind: the unit <name>-<kind>@<n>.service and /run/<name>/<kind>/<n>/.
type slotRef struct {
	Kind string
	N    int
}

// Slot is one occupied instance. Set is the zero SetKey for an instance occupied by a unit or a
// job file this listener did not start; it still counts against the budget.
type Slot struct {
	Kind       string
	Instance   int
	Set        SetKey
	RunnerID   int64
	RunnerName string
	Phase      Phase
	Since      time.Time // started, adopted at boot, or last found busy by an idle stop
	// NoRecycleBefore holds off a warm recycle of the slot until then: a stop that did not happen
	// (GitHub refused the removal, its runner having just taken a job) sets it idle_stop_sec ahead.
	NoRecycleBefore time.Time
}

func (s Slot) ref() slotRef { return slotRef{s.Kind, s.Instance} }

// SetStatus is one set's demand as GitHub last reported it.
type SetStatus struct {
	Key        SetKey
	Known      bool // statistics received since its session opened
	Assigned   int  // TotalAssignedJobs: jobs GitHub assigned to the set and not yet completed
	MinRunners int
}

// PlanInput is everything a plan depends on; the caller does all the I/O.
type PlanInput struct {
	Now            time.Time
	Slots          int // budget.slots: every set of every kind without a budget of its own
	QAEConcurrency int
	IdleStop       time.Duration
	ContainerBytes uint64
	ReserveBytes   uint64
	MemAvailable   uint64
	// OwnSlots maps a kind with a budget of its own (the wait kind) to it: at most that many of its
	// slots at once, on instances 1..n, none of them counted against Slots.
	OwnSlots map[string]int
	// KindContainerBytes maps a kind whose container is not ContainerBytes to its size.
	KindContainerBytes map[string]uint64
	// Instances maps a kind whose instance range is fixed by its requires_files (qae: one Codex
	// login store per instance) to its range 1..n. A kind not listed uses 1..OwnSlots if it has a
	// budget of its own, else 1..Slots.
	Instances map[string]int
	// RequiresMissing maps an instance to its required file when that file is absent.
	RequiresMissing map[slotRef]string
	Sets            []SetStatus
	// Occupied is every instance in use: the listener's slots plus instances held by a unit or job
	// file it did not start.
	Occupied []Slot
	// WarmMaxAge is warm_max_age_sec: how long a warm slot's unit may stay active before a recycle.
	WarmMaxAge time.Duration
	// UnitStarts is when the unit of each idle slot of a warm pool entered the active state
	// (systemd's ActiveEnterTimestamp, the moment RuntimeMaxSec counts from). A slot without an
	// entry (its unit still in its prepare, or the time unreadable) is not recycled.
	UnitStarts map[slotRef]time.Time
}

// Start is one slot to start: the lowest free instance of the set's kind.
type Start struct {
	Set      SetKey
	Instance int
}

// Refusal is demand the plan could not meet this round, and why.
type Refusal struct {
	Set    SetKey
	Code   string // requires_file, budget, qae_concurrency, instance, admission
	Detail string
}

// Plan is what the lane does this round.
type Plan struct {
	Starts    []Start
	IdleStops []Slot
	Recycles  []Slot // warm slots stopped the way an idle stop stops them, to be started afresh
	Refusals  []Refusal
}

// slotsOf is a kind's budget: its own, or the shared Slots.
func (in PlanInput) slotsOf(kind string) int {
	if n, own := in.OwnSlots[kind]; own {
		return n
	}
	return in.Slots
}

// bytesOf is one container of a kind, for admission.
func (in PlanInput) bytesOf(kind string) uint64 {
	if b, ok := in.KindContainerBytes[kind]; ok {
		return b
	}
	return in.ContainerBytes
}

// makePlan decides this round's starts and idle stops. It is pure: same input, same plan.
//
// Per set, desired = min(slots of its kind, min_runners + assigned) and the shortfall against its
// active slots is its demand. Demand for assigned jobs (min(slots, assigned) less its active slots)
// is met across all sets before any warm-pool demand (the rest), and within each, the next start
// goes to the set with the fewest active slots (ties in key order), so neither a warm pool nor the
// set whose key sorts first takes every freed slot. A set refused once gets no more starts this
// round. Every start keeps: the occupied instances of every kind without a budget of
// its own <= slots, and a kind with one (wait) within it; qae slots <= qae_concurrency; a free
// instance of the kind in its range whose required file is present (the lowest such); and
// MemAvailable, less one container of its kind per slot still starting and per start already
// planned this round, at least one container of the kind plus the reserve.
func makePlan(in PlanInput) Plan {
	starts, refusals := planStarts(in)
	stops := planIdleStops(in)
	return Plan{Starts: starts, IdleStops: stops, Recycles: planRecycles(in, stops), Refusals: refusals}
}

// demand is one set's shortfall: want[0] for jobs assigned to it, want[1] for its warm pool.
type demand struct {
	key    SetKey
	active int
	want   [2]int
}

func planStarts(in PlanInput) ([]Start, []Refusal) {
	var demands []*demand
	for _, set := range in.Sets {
		if !set.Known {
			continue
		}
		slots, active := in.slotsOf(set.Key.Kind), activeSlots(in.Occupied, set.Key)
		jobs := max(0, min(slots, set.Assigned)-active)
		if total := min(slots, set.MinRunners+set.Assigned) - active; total > 0 {
			demands = append(demands, &demand{set.Key, active, [2]int{jobs, total - jobs}})
		}
	}
	if len(demands) == 0 {
		return nil, nil
	}

	taken := map[slotRef]bool{}
	usedQAE := 0
	// used counts the instances held against each budget: "" is the shared one, a kind with a
	// budget of its own counts under its name.
	used := map[string]int{}
	budgetOf := func(kind string) string {
		if _, own := in.OwnSlots[kind]; own {
			return kind
		}
		return ""
	}
	// A slot still starting has not taken its memory yet, so MemAvailable does not show it: it
	// counts as one container of its kind, like a start planned this round.
	var planned uint64
	for _, s := range in.Occupied {
		taken[s.ref()] = true
		used[budgetOf(s.Kind)]++
		if s.Kind == kindQAE {
			usedQAE++
		}
		if s.Phase == PhaseStarting {
			planned += in.bytesOf(s.Kind)
		}
	}
	var starts []Start
	var refusals []Refusal
	for tier := range 2 {
		for {
			var d *demand
			for _, c := range demands {
				if c.want[tier] > 0 && (d == nil || c.active < d.active || c.active == d.active && c.key.String() < d.key.String()) {
					d = c
				}
			}
			if d == nil {
				break
			}
			kind, budget := d.key.Kind, budgetOf(d.key.Kind)
			slots, container := in.slotsOf(kind), in.bytesOf(kind)
			instances := in.instances(kind)
			n, missing := lowestFree(kind, instances, taken, in.RequiresMissing)
			code, detail := "", ""
			switch {
			case n == 0 && len(missing) > 0:
				code, detail = "requires_file", strings.Join(missing, ", ")+" missing for every free instance"
			case used[budget] >= slots:
				code, detail = "budget", fmt.Sprintf("%d of %d %sslots in use", used[budget], slots, strings.TrimLeft(budget+" ", " "))
			case kind == kindQAE && usedQAE >= in.QAEConcurrency:
				code, detail = "qae_concurrency", fmt.Sprintf("%d of %d qae slots in use", usedQAE, in.QAEConcurrency)
			case n == 0:
				code, detail = "instance", fmt.Sprintf("every %s instance 1-%d is still occupied", kind, instances)
			case in.MemAvailable < planned || in.MemAvailable-planned < container+in.ReserveBytes:
				code, detail = "admission", fmt.Sprintf("MemAvailable %d bytes, less %d for starts in flight or planned, is below container %d + reserve %d",
					in.MemAvailable, planned, container, in.ReserveBytes)
			}
			if code != "" {
				// Every rule only tightens as the round's starts are planned, so a set refused here
				// would be refused again: its whole remaining demand is reported once.
				refusals = append(refusals, Refusal{d.key, code, fmt.Sprintf("%s; %d more wanted", detail, d.want[0]+d.want[1])})
				d.want = [2]int{}
				continue
			}
			starts = append(starts, Start{d.key, n})
			taken[slotRef{kind, n}] = true
			used[budget]++
			if kind == kindQAE {
				usedQAE++
			}
			planned += container
			d.active++
			d.want[tier]--
		}
	}
	return starts, refusals
}

// planIdleStops picks, per set with no assigned job, its idle slots beyond min_runners that are
// older than idle_stop, oldest first. A busy slot is never picked.
func planIdleStops(in PlanInput) []Slot {
	var stops []Slot
	for _, set := range in.Sets {
		if !set.Known || set.Assigned > 0 {
			continue
		}
		var idle []Slot
		for _, s := range in.Occupied {
			if s.Set == set.Key && s.Phase == PhaseIdle {
				idle = append(idle, s)
			}
		}
		sort.Slice(idle, func(i, j int) bool {
			if !idle[i].Since.Equal(idle[j].Since) {
				return idle[i].Since.Before(idle[j].Since)
			}
			return idle[i].Instance < idle[j].Instance
		})
		for _, s := range idle[:max(0, len(idle)-set.MinRunners)] {
			if in.Now.Sub(s.Since) < in.IdleStop {
				break // sorted oldest first: every later one is younger
			}
			stops = append(stops, s)
		}
	}
	return stops
}

// planRecycles picks, per set with a warm pool, at most one idle slot whose unit has been active
// longer than warm_max_age, the oldest unit first. A warm slot otherwise lives until its unit's
// RuntimeMaxSec, and a job it takes late in that time would be killed with it. The age is the
// unit's, never the slot's Since, which an aborted idle stop resets. A set recycles only while it
// holds every slot it wants with none starting or stopping (an idle stop this round included), so
// its warm pool is renewed one slot at a time: the next goes once the fresh one is up. And only
// while every job assigned to it is on a busy slot, since an idle runner may be about to take a
// waiting job. A busy slot is never picked, nor one before its NoRecycleBefore.
func planRecycles(in PlanInput, idleStops []Slot) []Slot {
	stopping := map[slotRef]bool{}
	for _, s := range idleStops {
		stopping[s.ref()] = true
	}
	var recycles []Slot
	for _, set := range in.Sets {
		if !set.Known || set.MinRunners == 0 {
			continue
		}
		active, busy, settling := 0, 0, false
		var pick *Slot
		for i, s := range in.Occupied {
			if s.Set != set.Key {
				continue
			}
			if stopping[s.ref()] || s.Phase == PhaseStarting || s.Phase == PhaseStopping {
				settling = true
				continue
			}
			if s.Phase.active() {
				active++
			}
			if s.Phase == PhaseBusy {
				busy++
			}
			start, known := in.UnitStarts[s.ref()]
			if s.Phase != PhaseIdle || !known || in.Now.Sub(start) < in.WarmMaxAge || in.Now.Before(s.NoRecycleBefore) {
				continue
			}
			if pick == nil || start.Before(in.UnitStarts[pick.ref()]) ||
				(start.Equal(in.UnitStarts[pick.ref()]) && s.Instance < pick.Instance) {
				pick = &in.Occupied[i]
			}
		}
		if pick != nil && !settling && active >= min(in.slotsOf(set.Key.Kind), set.MinRunners+set.Assigned) && set.Assigned <= busy {
			recycles = append(recycles, *pick)
		}
	}
	return recycles
}

func activeSlots(occupied []Slot, key SetKey) int {
	n := 0
	for _, s := range occupied {
		if s.Set == key && s.Phase.active() {
			n++
		}
	}
	return n
}

// instances is a kind's instance range 1..n: fixed by its requires_files, else its own budget,
// else the shared one.
func (in PlanInput) instances(kind string) int {
	if n, ok := in.Instances[kind]; ok {
		return n
	}
	return in.slotsOf(kind)
}

// lowestFree is the lowest free instance of the kind in 1..instances whose required file is
// present, or 0. missing lists the required files of the free instances it passed over: a job goes
// to an instance whose file is present and never waits on one whose file is not.
func lowestFree(kind string, instances int, taken map[slotRef]bool, requiresMissing map[slotRef]string) (n int, missing []string) {
	for i := 1; i <= instances; i++ {
		ref := slotRef{kind, i}
		switch {
		case taken[ref]:
		case requiresMissing[ref] != "":
			missing = append(missing, requiresMissing[ref])
		default:
			return i, nil
		}
	}
	return 0, missing
}
