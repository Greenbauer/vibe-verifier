package main

import (
	"reflect"
	"strings"
	"testing"
	"time"
)

const gib = uint64(1) << 30

var t0 = time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)

func set(target, kind string, assigned, minRunners int) SetStatus {
	return SetStatus{Key: SetKey{target, kind}, Known: true, Assigned: assigned, MinRunners: minRunners}
}

func slot(target, kind string, n int, phase Phase, age time.Duration) Slot {
	return Slot{Kind: kind, Instance: n, Set: SetKey{target, kind}, RunnerName: target + "-" + kind, Phase: phase, Since: t0.Add(-age)}
}

// input is a lane with room for everything unless a case says otherwise.
func input(slots int, sets []SetStatus, occupied ...Slot) PlanInput {
	return PlanInput{
		Now: t0, Slots: slots, QAEConcurrency: 1, IdleStop: 5 * time.Minute,
		ContainerBytes: 4 * gib, ReserveBytes: 2 * gib, MemAvailable: 64 * gib,
		Sets: sets, Occupied: occupied,
	}
}

// withWait gives the input a wait kind with a budget of its own and its own container size.
func withWait(in PlanInput, slots int, container uint64) PlanInput {
	in.OwnSlots = map[string]int{kindWait: slots}
	in.KindContainerBytes = map[string]uint64{kindWait: container}
	return in
}

func starts(p Plan) []Start { return p.Starts }

func codes(p Plan) []string {
	var out []string
	for _, r := range p.Refusals {
		out = append(out, r.Set.String()+":"+r.Code)
	}
	return out
}

func TestPlanStarts(t *testing.T) {
	org := "example"
	repoA, repoB := "example/repo-a", "example/repo-b"
	lowMem := input(4, []SetStatus{set(repoA, kindCI, 3, 0)})
	lowMem.MemAvailable = 5 * gib // below one container (4) + reserve (2)
	twoFit := input(4, []SetStatus{set(repoA, kindCI, 3, 0)})
	twoFit.MemAvailable = 10 * gib // 10-4=6 fits a second start (4+2), 6-4=2 does not fit a third
	noStore := input(4, []SetStatus{set(repoA, kindQAE, 1, 0)})
	noStore.Instances = map[string]int{kindQAE: 1}
	noStore.RequiresMissing = map[slotRef]string{{kindQAE, 1}: "/var/lib/example/store/auth.json"}
	// qae_concurrency 2: one Codex login store per qae instance, the n-th for instance n.
	twoStores := func(assigned int, missing map[slotRef]string, occupied ...Slot) PlanInput {
		in := input(4, []SetStatus{set(repoA, kindQAE, assigned, 0)}, occupied...)
		in.QAEConcurrency, in.Instances, in.RequiresMissing = 2, map[string]int{kindQAE: 2}, missing
		return in
	}
	store1 := map[slotRef]string{{kindQAE, 1}: "/var/lib/example/codex/auth.json"}
	store2 := map[slotRef]string{{kindQAE, 2}: "/var/lib/example/codex-2/auth.json"}
	// 7 GiB: a ci start (4 + reserve 2) then a wait start (1 + 2 on the 3 left) fit, nothing after.
	mixedFit := withWait(input(4, []SetStatus{set(org, kindCI, 2, 0), set(org, kindWait, 2, 0)}), 4, 1*gib)
	mixedFit.MemAvailable = 7 * gib

	cases := []struct {
		name      string
		in        PlanInput
		want      []Start
		refusals  []string
		detailHas string
	}{
		{
			name: "a six-job run under a budget of four starts four",
			in:   input(4, []SetStatus{set(repoA, kindCI, 6, 0)}),
			// desired = min(slots, assigned): the set wants four, so nothing is refused.
			want: []Start{{SetKey{repoA, kindCI}, 1}, {SetKey{repoA, kindCI}, 2}, {SetKey{repoA, kindCI}, 3}, {SetKey{repoA, kindCI}, 4}},
		},
		{
			name: "while all four run, nothing more starts",
			in: input(4, []SetStatus{set(repoA, kindCI, 6, 0)},
				slot(repoA, kindCI, 1, PhaseBusy, time.Minute), slot(repoA, kindCI, 2, PhaseBusy, time.Minute),
				slot(repoA, kindCI, 3, PhaseBusy, time.Minute), slot(repoA, kindCI, 4, PhaseBusy, time.Minute)),
		},
		{
			name: "a completed job still holds its instance until its unit is gone",
			in: input(4, []SetStatus{set(repoA, kindCI, 5, 0)},
				slot(repoA, kindCI, 1, PhaseDone, time.Minute), slot(repoA, kindCI, 2, PhaseBusy, time.Minute),
				slot(repoA, kindCI, 3, PhaseBusy, time.Minute), slot(repoA, kindCI, 4, PhaseBusy, time.Minute)),
			refusals: []string{repoA + " ci:budget"},
		},
		{
			name: "then the last two start as jobs complete and their instances free",
			in: input(4, []SetStatus{set(repoA, kindCI, 4, 0)},
				slot(repoA, kindCI, 3, PhaseBusy, time.Minute), slot(repoA, kindCI, 4, PhaseBusy, time.Minute)),
			want: []Start{{SetKey{repoA, kindCI}, 1}, {SetKey{repoA, kindCI}, 2}},
		},
		{
			name: "the global budget is shared in turn by two repositories' sets",
			in:   input(4, []SetStatus{set(repoB, kindCI, 3, 0), set(repoA, kindCI, 3, 0)}),
			want: []Start{
				{SetKey{repoA, kindCI}, 1}, {SetKey{repoB, kindCI}, 2},
				{SetKey{repoA, kindCI}, 3}, {SetKey{repoB, kindCI}, 4},
			},
			refusals: []string{repoA + " ci:budget", repoB + " ci:budget"},
		},
		{
			name: "one repository's running slots leave the other only the rest of the budget",
			in: input(4, []SetStatus{set(repoA, kindCI, 3, 0), set(repoB, kindCI, 2, 0)},
				slot(repoA, kindCI, 1, PhaseBusy, time.Minute), slot(repoA, kindCI, 2, PhaseBusy, time.Minute),
				slot(repoA, kindCI, 3, PhaseBusy, time.Minute)),
			want:     []Start{{SetKey{repoB, kindCI}, 4}},
			refusals: []string{repoB + " ci:budget"},
		},
		{
			name:     "qae is capped at one lane-wide, across repositories",
			in:       input(4, []SetStatus{set(repoA, kindQAE, 2, 0), set(repoB, kindQAE, 1, 0)}),
			want:     []Start{{SetKey{repoA, kindQAE}, 1}},
			refusals: []string{repoB + " qae:qae_concurrency", repoA + " qae:qae_concurrency"},
		},
		{
			name: "a running qae slot blocks another qae start but not a ci start",
			in: input(4, []SetStatus{set(repoA, kindCI, 1, 0), set(repoB, kindQAE, 1, 0)},
				slot(repoA, kindQAE, 1, PhaseBusy, time.Minute)),
			want:     []Start{{SetKey{repoA, kindCI}, 1}},
			refusals: []string{repoB + " qae:qae_concurrency"},
		},
		{
			name:      "qae is skipped without its requires_file",
			in:        noStore,
			refusals:  []string{repoA + " qae:requires_file"},
			detailHas: "/var/lib/example/store/auth.json missing for every free instance",
		},
		{
			name: "qae_concurrency 2 with both stores' logins starts two qae slots, each on its own instance",
			in:   twoStores(2, nil),
			want: []Start{{SetKey{repoA, kindQAE}, 1}, {SetKey{repoA, kindQAE}, 2}},
		},
		{
			name: "an instance whose store has no login is never started while a seeded one is free",
			in:   twoStores(1, store1),
			want: []Start{{SetKey{repoA, kindQAE}, 2}},
		},
		{
			name:      "a second job waits for the unseeded store rather than borrowing the seeded instance's",
			in:        twoStores(2, store1),
			want:      []Start{{SetKey{repoA, kindQAE}, 2}},
			refusals:  []string{repoA + " qae:requires_file"},
			detailHas: "/var/lib/example/codex/auth.json missing for every free instance",
		},
		{
			name:      "with instance 1 busy and store 2 unseeded, no qae start goes past the stores to instance 3",
			in:        twoStores(2, store2, slot(repoA, kindQAE, 1, PhaseBusy, time.Minute)),
			refusals:  []string{repoA + " qae:requires_file"},
			detailHas: "/var/lib/example/codex-2/auth.json missing",
		},
		{
			name:     "both qae instances busy: the third job waits on qae_concurrency",
			in:       twoStores(3, nil, slot(repoA, kindQAE, 1, PhaseBusy, time.Minute), slot(repoA, kindQAE, 2, PhaseBusy, time.Minute)),
			refusals: []string{repoA + " qae:qae_concurrency"},
		},
		{
			name:      "the admission check refuses every start below one container plus the reserve",
			in:        lowMem,
			refusals:  []string{repoA + " ci:admission"},
			detailHas: "MemAvailable 5368709120 bytes",
		},
		{
			name:     "admission counts the starts already planned this round",
			in:       twoFit,
			want:     []Start{{SetKey{repoA, kindCI}, 1}, {SetKey{repoA, kindCI}, 2}},
			refusals: []string{repoA + " ci:admission"},
		},
		{
			name: "min_runners keeps two warm for the organisation set with nothing assigned",
			in:   input(5, []SetStatus{set(org, kindCI, 0, 2)}),
			want: []Start{{SetKey{org, kindCI}, 1}, {SetKey{org, kindCI}, 2}},
		},
		{
			name: "a user set with nothing assigned starts nothing",
			in:   input(5, []SetStatus{set(repoA, kindCI, 0, 0)}),
		},
		{
			name: "a warm runner taking a job is replaced",
			in: input(5, []SetStatus{set(org, kindCI, 1, 2)},
				slot(org, kindCI, 1, PhaseBusy, time.Minute), slot(org, kindCI, 2, PhaseIdle, time.Minute)),
			want: []Start{{SetKey{org, kindCI}, 3}},
		},
		{
			// One machine's log of 2026-10-06 18:49Z: ci, five busy and wanting its warm runner,
			// took every freed slot by sorting first while qae's assigned job waited 42 minutes.
			name: "the last free slot goes to an assigned qae job before ci's warm runner",
			in: input(6, []SetStatus{set(org, kindCI, 5, 1), set(org, kindQAE, 1, 0)},
				slot(org, kindCI, 1, PhaseBusy, time.Minute), slot(org, kindCI, 2, PhaseBusy, time.Minute),
				slot(org, kindCI, 3, PhaseBusy, time.Minute), slot(org, kindCI, 4, PhaseBusy, time.Minute),
				slot(org, kindCI, 5, PhaseBusy, time.Minute)),
			want:     []Start{{SetKey{org, kindQAE}, 1}},
			refusals: []string{org + " ci:budget"},
		},
		{
			name: "assigned demand goes first even when the warm set has fewer active slots",
			in: func() PlanInput {
				in := input(2, []SetStatus{set(org, kindCI, 0, 1), set(org, kindQAE, 2, 0)},
					slot(org, kindQAE, 1, PhaseBusy, time.Minute))
				in.QAEConcurrency = 2
				return in
			}(),
			want:     []Start{{SetKey{org, kindQAE}, 2}},
			refusals: []string{org + " ci:budget"},
		},
		{
			// The log of 2026-10-07 04:15Z: both sets had assigned jobs; ci sorted first and won.
			name: "with assigned demand on both sets, the one free slot goes to the set with fewer active slots",
			in: func() PlanInput {
				in := input(6, []SetStatus{set(org, kindCI, 6, 1), set(org, kindQAE, 3, 0)},
					slot(org, kindCI, 1, PhaseBusy, time.Minute), slot(org, kindCI, 2, PhaseBusy, time.Minute),
					slot(org, kindCI, 3, PhaseBusy, time.Minute), slot(org, kindCI, 4, PhaseBusy, time.Minute),
					slot(org, kindCI, 5, PhaseBusy, time.Minute))
				in.QAEConcurrency = 2
				return in
			}(),
			want:      []Start{{SetKey{org, kindQAE}, 1}},
			refusals:  []string{org + " qae:budget", org + " ci:budget"},
			detailHas: "6 of 6 slots in use; 2 more wanted",
		},
		{
			name: "a single set meets its jobs and then its warm pool, as before",
			in:   input(4, []SetStatus{set(org, kindCI, 2, 1)}, slot(org, kindCI, 1, PhaseBusy, time.Minute)),
			want: []Start{{SetKey{org, kindCI}, 2}, {SetKey{org, kindCI}, 3}},
		},
		{
			name: "a set whose statistics have not arrived is left alone",
			in:   input(4, []SetStatus{{Key: SetKey{repoA, kindCI}, Assigned: 3}}),
		},
		{
			name: "waiters do not draw on the shared budget: a full lane still starts a wait slot",
			in: withWait(input(2, []SetStatus{set(org, kindCI, 2, 0), set(org, kindWait, 1, 0)},
				slot(org, kindCI, 1, PhaseDone, time.Minute), slot(org, kindCI, 2, PhaseBusy, time.Minute)), 3, 1*gib),
			want:     []Start{{SetKey{org, kindWait}, 1}},
			refusals: []string{org + " ci:budget"},
		},
		{
			name: "and waiters filling their own budget leave every shared slot to ci",
			in: withWait(input(2, []SetStatus{set(org, kindCI, 2, 0), set(org, kindWait, 2, 0)},
				slot(org, kindWait, 1, PhaseDone, time.Minute), slot(org, kindWait, 2, PhaseBusy, time.Minute)), 2, 1*gib),
			want:      []Start{{SetKey{org, kindCI}, 1}, {SetKey{org, kindCI}, 2}},
			refusals:  []string{org + " wait:budget"},
			detailHas: "2 of 2 wait slots in use",
		},
		{
			name: "a wait set wants at most its own slots, on its own instances 1..n",
			in:   withWait(input(2, []SetStatus{set(org, kindWait, 5, 0)}), 3, 1*gib),
			want: []Start{{SetKey{org, kindWait}, 1}, {SetKey{org, kindWait}, 2}, {SetKey{org, kindWait}, 3}},
		},
		{
			name:     "admission sizes each start by its kind's container",
			in:       mixedFit,
			want:     []Start{{SetKey{org, kindCI}, 1}, {SetKey{org, kindWait}, 1}},
			refusals: []string{org + " ci:admission", org + " wait:admission"},
		},
		{
			name: "a qae instance's missing store refuses qae and leaves the wait kind its own free slots",
			in: func() PlanInput {
				in := withWait(twoStores(1, store1), 4, 1*gib)
				in.Instances = map[string]int{kindQAE: 1}
				in.Sets = append(in.Sets, set(org, kindWait, 2, 0))
				return in
			}(),
			want:      []Start{{SetKey{org, kindWait}, 1}, {SetKey{org, kindWait}, 2}},
			refusals:  []string{repoA + " qae:requires_file"},
			detailHas: "/var/lib/example/codex/auth.json missing for every free instance",
		},
		{
			name: "an instance held by a unit the listener did not start counts against the budget",
			in: input(2, []SetStatus{set(repoA, kindCI, 2, 0)},
				Slot{Kind: kindCI, Instance: 1, Phase: PhaseDone}),
			want:     []Start{{SetKey{repoA, kindCI}, 2}},
			refusals: []string{repoA + " ci:budget"},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			p := makePlan(tc.in)
			if !reflect.DeepEqual(starts(p), tc.want) {
				t.Errorf("starts = %v, want %v", starts(p), tc.want)
			}
			if !reflect.DeepEqual(codes(p), tc.refusals) {
				t.Errorf("refusals = %v, want %v", codes(p), tc.refusals)
			}
			if tc.detailHas != "" && (len(p.Refusals) == 0 || !strings.Contains(p.Refusals[0].Detail, tc.detailHas)) {
				t.Errorf("refusal detail %v does not mention %q", p.Refusals, tc.detailHas)
			}
			if len(p.IdleStops) != 0 || len(p.Recycles) != 0 {
				t.Errorf("unexpected idle stops %v or recycles %v", p.IdleStops, p.Recycles)
			}
		})
	}
}

func TestPlanIdleStops(t *testing.T) {
	org, repo := "example", "example/repo-a"
	old, young := 10*time.Minute, time.Minute
	cases := []struct {
		name string
		in   PlanInput
		want []int // instances stopped
	}{
		{
			name: "above min_runners, nothing assigned: the oldest extra idle slot stops",
			in: input(5, []SetStatus{set(org, kindCI, 0, 2)},
				slot(org, kindCI, 1, PhaseIdle, old+time.Minute), slot(org, kindCI, 2, PhaseIdle, old),
				slot(org, kindCI, 3, PhaseIdle, old)),
			want: []int{1},
		},
		{
			name: "at min_runners nothing stops, however old",
			in: input(5, []SetStatus{set(org, kindCI, 0, 2)},
				slot(org, kindCI, 1, PhaseIdle, old), slot(org, kindCI, 2, PhaseIdle, old)),
		},
		{
			name: "a set with an assigned job keeps its idle slots",
			in: input(5, []SetStatus{set(org, kindCI, 1, 2)},
				slot(org, kindCI, 1, PhaseIdle, old), slot(org, kindCI, 2, PhaseIdle, old),
				slot(org, kindCI, 3, PhaseIdle, old)),
		},
		{
			name: "a user set keeps no warm pool: an old idle slot stops, a young one waits",
			in: input(5, []SetStatus{set(repo, kindCI, 0, 0)},
				slot(repo, kindCI, 1, PhaseIdle, old), slot(repo, kindCI, 2, PhaseIdle, young)),
			want: []int{1},
		},
		{
			name: "busy, starting and stopping slots are never idle-stopped",
			in: input(5, []SetStatus{set(repo, kindCI, 0, 0)},
				slot(repo, kindCI, 1, PhaseBusy, old), slot(repo, kindCI, 2, PhaseStarting, old),
				slot(repo, kindCI, 3, PhaseStopping, old)),
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var got []int
			for _, s := range makePlan(tc.in).IdleStops {
				got = append(got, s.Instance)
			}
			if !reflect.DeepEqual(got, tc.want) {
				t.Errorf("idle stops = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestPlanWarmRecycles(t *testing.T) {
	org, repo := "example", "example/repo-a"
	old, young := 25*time.Minute, 10*time.Minute // against a warm_max_age of 20 minutes
	cases := []struct {
		name  string
		sets  []SetStatus
		slots []Slot
		ages  map[int]time.Duration // instance: how long its unit has been active
		want  []int                 // instances recycled
		stops []int                 // instances idle-stopped
	}{
		{
			name:  "a warm slot whose unit is older than warm_max_age is recycled",
			sets:  []SetStatus{set(org, kindCI, 0, 2)},
			slots: []Slot{slot(org, kindCI, 1, PhaseIdle, old), slot(org, kindCI, 2, PhaseIdle, young)},
			ages:  map[int]time.Duration{1: old, 2: young},
			want:  []int{1},
		},
		{
			name:  "one per set per plan: of two old warm slots, the older unit goes first",
			sets:  []SetStatus{set(org, kindCI, 0, 2)},
			slots: []Slot{slot(org, kindCI, 1, PhaseIdle, old), slot(org, kindCI, 2, PhaseIdle, old)},
			ages:  map[int]time.Duration{1: old, 2: old + time.Minute},
			want:  []int{2},
		},
		{
			name:  "warm slots younger than warm_max_age are kept",
			sets:  []SetStatus{set(org, kindCI, 0, 2)},
			slots: []Slot{slot(org, kindCI, 1, PhaseIdle, old), slot(org, kindCI, 2, PhaseIdle, old)},
			ages:  map[int]time.Duration{1: young, 2: young},
		},
		{
			name:  "a busy slot is never recycled, however old its unit",
			sets:  []SetStatus{set(org, kindCI, 1, 2)},
			slots: []Slot{slot(org, kindCI, 1, PhaseBusy, old), slot(org, kindCI, 2, PhaseIdle, young), slot(org, kindCI, 3, PhaseIdle, young)},
			ages:  map[int]time.Duration{1: old, 2: young, 3: young},
		},
		{
			name:  "beside a busy slot, an old idle one is recycled",
			sets:  []SetStatus{set(org, kindCI, 1, 2)},
			slots: []Slot{slot(org, kindCI, 1, PhaseBusy, old), slot(org, kindCI, 2, PhaseIdle, old), slot(org, kindCI, 3, PhaseIdle, young)},
			ages:  map[int]time.Duration{1: old, 2: old, 3: young},
			want:  []int{2},
		},
		{
			name: "the age is the unit's, not Since: a slot an aborted idle stop aged afresh is still recycled",
			sets: []SetStatus{set(org, kindCI, 0, 2)},
			// Since was reset a moment ago; the unit has been up for 25 minutes.
			slots: []Slot{slot(org, kindCI, 1, PhaseIdle, 0), slot(org, kindCI, 2, PhaseIdle, young)},
			ages:  map[int]time.Duration{1: old, 2: young},
			want:  []int{1},
		},
		{
			name:  "and an old Since with a young unit is kept",
			sets:  []SetStatus{set(org, kindCI, 0, 2)},
			slots: []Slot{slot(org, kindCI, 1, PhaseIdle, time.Hour), slot(org, kindCI, 2, PhaseIdle, time.Hour)},
			ages:  map[int]time.Duration{1: young, 2: young},
		},
		{
			name: "a slot held after a refused recycle is passed over; another old one may go",
			sets: []SetStatus{set(org, kindCI, 0, 2)},
			slots: []Slot{
				{Kind: kindCI, Instance: 1, Set: SetKey{org, kindCI}, Phase: PhaseIdle, Since: t0, NoRecycleBefore: t0.Add(time.Second)},
				slot(org, kindCI, 2, PhaseIdle, old),
			},
			ages: map[int]time.Duration{1: old + time.Minute, 2: old},
			want: []int{2},
		},
		{
			name:  "a unit without a start time (still in its prepare) is kept",
			sets:  []SetStatus{set(org, kindCI, 0, 2)},
			slots: []Slot{slot(org, kindCI, 1, PhaseIdle, time.Hour), slot(org, kindCI, 2, PhaseIdle, young)},
			ages:  map[int]time.Duration{2: young},
		},
		{
			name:  "no recycle while a job waits for a runner: an idle runner may be about to take it",
			sets:  []SetStatus{set(org, kindCI, 1, 2)},
			slots: []Slot{slot(org, kindCI, 1, PhaseIdle, old), slot(org, kindCI, 2, PhaseIdle, old), slot(org, kindCI, 3, PhaseIdle, old)},
			ages:  map[int]time.Duration{1: old, 2: old, 3: old},
		},
		{
			name: "no recycle while one of the set's slots is still starting, even with the pool full",
			sets: []SetStatus{set(org, kindCI, 1, 2)},
			slots: []Slot{slot(org, kindCI, 1, PhaseBusy, old), slot(org, kindCI, 2, PhaseIdle, old),
				slot(org, kindCI, 3, PhaseIdle, young), slot(org, kindCI, 4, PhaseStarting, 0)},
			ages: map[int]time.Duration{1: old, 2: old, 3: young},
		},
		{
			name:  "no recycle while another is stopping, nor while the set is short of its pool",
			sets:  []SetStatus{set(org, kindCI, 0, 2)},
			slots: []Slot{slot(org, kindCI, 1, PhaseStopping, old), slot(org, kindCI, 2, PhaseIdle, old)},
			ages:  map[int]time.Duration{1: old, 2: old},
		},
		{
			name: "no recycle in a round that idle-stops one of the set's slots",
			sets: []SetStatus{set(org, kindCI, 0, 2)},
			slots: []Slot{slot(org, kindCI, 1, PhaseIdle, old+time.Minute), slot(org, kindCI, 2, PhaseIdle, old),
				slot(org, kindCI, 3, PhaseIdle, old)},
			ages:  map[int]time.Duration{1: old, 2: old, 3: old},
			stops: []int{1},
		},
		{
			name:  "a set without a warm pool recycles nothing (its idle slots are idle-stopped)",
			sets:  []SetStatus{set(repo, kindCI, 1, 0)},
			slots: []Slot{slot(repo, kindCI, 1, PhaseBusy, old), slot(repo, kindCI, 2, PhaseIdle, old)},
			ages:  map[int]time.Duration{1: old, 2: old},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			in := input(5, tc.sets, tc.slots...)
			in.WarmMaxAge = 20 * time.Minute
			in.UnitStarts = map[slotRef]time.Time{}
			for n, age := range tc.ages {
				in.UnitStarts[slotRef{kindCI, n}] = t0.Add(-age)
			}
			p := makePlan(in)
			var got, stops []int
			for _, s := range p.Recycles {
				got = append(got, s.Instance)
			}
			for _, s := range p.IdleStops {
				stops = append(stops, s.Instance)
			}
			if !reflect.DeepEqual(got, tc.want) || !reflect.DeepEqual(stops, tc.stops) {
				t.Errorf("recycles = %v, idle stops = %v; want %v and %v", got, stops, tc.want, tc.stops)
			}
		})
	}
}

// TestPlanSixJobsEndToEnd walks the six-job run through the plan as jobs complete: four start,
// then the last two as instances free, and six starts in all.
func TestPlanSixJobsEndToEnd(t *testing.T) {
	key := SetKey{"example/repo-a", kindCI}
	var occupied []Slot
	assigned, total := 6, 0
	plan := func() []Start {
		p := makePlan(input(4, []SetStatus{{Key: key, Known: true, Assigned: assigned}}, occupied...))
		for _, st := range p.Starts {
			occupied = append(occupied, Slot{Kind: kindCI, Instance: st.Instance, Set: key, Phase: PhaseBusy, Since: t0})
		}
		total += len(p.Starts)
		return p.Starts
	}
	if got := len(plan()); got != 4 {
		t.Fatalf("first round started %d, want 4", got)
	}
	for completed := 1; completed <= 6; completed++ {
		assigned--
		occupied = occupied[1:] // the oldest job completes and its unit is gone
		plan()
		if len(occupied) > 4 {
			t.Fatalf("after %d completions %d slots are occupied, over the budget of 4", completed, len(occupied))
		}
	}
	if total != 6 {
		t.Fatalf("started %d slots for six jobs, want 6", total)
	}
}

func TestPlanAdmissionCountsStartsStillInFlight(t *testing.T) {
	key := SetKey{"example/repo-a", kindCI}
	in := input(4, []SetStatus{set(key.Target, kindCI, 3, 0)}, slot(key.Target, kindCI, 1, PhaseStarting, 0))
	in.MemAvailable = 10 * gib // one container is already on its way: 10-4=6 fits one more (4+2), 2 does not
	p := makePlan(in)
	if !reflect.DeepEqual(p.Starts, []Start{{key, 2}}) || !reflect.DeepEqual(codes(p), []string{key.String() + ":admission"}) {
		t.Errorf("starts %v, refusals %v", p.Starts, codes(p))
	}
}
