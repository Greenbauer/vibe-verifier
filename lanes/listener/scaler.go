package main

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"

	"github.com/actions/scaleset"
	"github.com/actions/scaleset/listener"
)

// refusalQuiet is how often one set's refusal of one kind is repeated in the log while it lasts.
const refusalQuiet = time.Minute

// Lane is the listener's state and the only thing that starts or stops slots. Every set's
// listener reports into it, and every reconcile plans the whole lane at once, because the budget
// is shared by every set (the wait kind's own budget by every wait set). Starts and idle stops run
// in their own goroutines; the slot they touch is reserved (starting, stopping) so a concurrent
// plan counts it and leaves it alone.
type Lane struct {
	cfg    *Config
	runDir string
	sys    Systemd
	host   hostProbe
	logf   func(string, ...any)
	now    func() time.Time
	newAPI func(target string) (api, error)
	// opsCtx is what starts and stops run under: a stop signal lets them finish (see waitInflight),
	// so a slot is never left half-started by a shutdown.
	opsCtx context.Context

	apiMu sync.Mutex
	apis  map[string]api

	mu       sync.Mutex
	sets     map[SetKey]*setEntry
	slots    map[slotRef]*Slot
	foreign  map[slotRef]bool
	quiet    map[string]time.Time
	inflight sync.WaitGroup
}

type setEntry struct {
	id       int
	api      api
	known    bool
	assigned int
}

func newLane(cfg *Config, runDir string, sys Systemd, host hostProbe, newAPI func(string) (api, error), logf func(string, ...any)) *Lane {
	return &Lane{
		cfg: cfg, runDir: runDir, sys: sys, host: host, logf: logf, now: time.Now, newAPI: newAPI,
		opsCtx: context.Background(), apis: map[string]api{},
		sets: map[SetKey]*setEntry{}, slots: map[slotRef]*Slot{}, foreign: map[slotRef]bool{}, quiet: map[string]time.Time{},
	}
}

// apiFor returns the (cached) scale set client of one scope target.
func (l *Lane) apiFor(target string) (api, error) {
	l.apiMu.Lock()
	defer l.apiMu.Unlock()
	if a, ok := l.apis[target]; ok {
		return a, nil
	}
	a, err := l.newAPI(target)
	if err != nil {
		return nil, fmt.Errorf("scale set client for %s: %w", target, err)
	}
	l.apis[target] = a
	return a, nil
}

func (l *Lane) addSet(key SetKey, id int, a api) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.sets[key] = &setEntry{id: id, api: a}
}

func (l *Lane) removeSet(key SetKey) {
	l.mu.Lock()
	defer l.mu.Unlock()
	delete(l.sets, key)
}

func (l *Lane) unit(ref slotRef) string { return unitName(l.cfg.Name, ref.Kind, ref.N) }

// allUnits is every instance of every configured kind, in a fixed order.
func (l *Lane) allUnits() ([]slotRef, []string) {
	var refs []slotRef
	var units []string
	for _, kind := range l.cfg.kindNames() {
		for n := 1; n <= l.cfg.kindSlots(kind); n++ {
			refs = append(refs, slotRef{kind, n})
			units = append(units, unitName(l.cfg.Name, kind, n))
		}
	}
	return refs, units
}

// rebuild adopts the slots a previous listener started: a job file whose unit is still running
// is tracked again (as idle: a busy runner refuses the removal an idle stop begins with), and one
// whose unit is not running is a leftover whose runner and files are removed.
func (l *Lane) rebuild() {
	l.mu.Lock()
	defer l.mu.Unlock()
	records := readJobFiles(l.runDir, l.cfg.kindNames())
	if len(records) == 0 {
		l.logf("run dir %s holds no slots to adopt", l.runDir)
		return
	}
	_, units := l.allUnits()
	states, err := l.sys.States(l.opsCtx, units)
	if err != nil {
		// Adopt every job file as running: the next reconcile that reads the states forgets the
		// ones that are not, and nothing is started or stopped until then.
		l.logf("unit states unreadable at start (%v); every job file is adopted until they read", err)
	}
	for _, rec := range records {
		ref := slotRef{rec.Kind, rec.Instance}
		unit := l.unit(ref)
		running := err != nil || unitRunning(states[unit])
		switch {
		case rec.Err != nil && running:
			l.logf("%s is running with an unreadable job file (%v); its instance counts as occupied", unit, rec.Err)
		case rec.Err != nil:
			l.clearStale(ref, JobRecord{})
		case running:
			l.slots[ref] = &Slot{
				Kind: rec.Kind, Instance: rec.Instance, Set: SetKey{rec.Target, rec.Kind},
				RunnerID: rec.RunnerID, RunnerName: rec.RunnerName, Phase: PhaseIdle, Since: rec.ModTime,
			}
			l.logf("adopted %s: runner %s (id %d) of %s set %d", unit, rec.RunnerName, rec.RunnerID, rec.Target, rec.SetID)
		default:
			l.clearStale(ref, rec)
		}
	}
}

// clearStale removes a job file whose unit is no longer running, and its runner registration.
// Called with l.mu held; the removal from GitHub runs in the background.
func (l *Lane) clearStale(ref slotRef, rec JobRecord) {
	if err := removeSlotFiles(l.runDir, ref.Kind, ref.N); err != nil {
		l.logf("leftover files of %s could not all be removed: %v", l.unit(ref), err)
	}
	l.logf("cleared the leftover job file of %s (unit not running)", l.unit(ref))
	l.removeRunnerLater(rec.Target, rec.RunnerID)
}

// removeRunnerLater removes a runner that no slot will use, in the background; one GitHub already
// removed (it ran its job) is fine. Called with l.mu held.
func (l *Lane) removeRunnerLater(target string, runnerID int64) {
	if runnerID == 0 || target == "" {
		return
	}
	a, err := l.apiFor(target)
	if err != nil {
		l.logf("runner %d of %s left registered: %v", runnerID, target, err)
		return
	}
	l.inflight.Add(1)
	go func() {
		defer l.inflight.Done()
		if err := a.RemoveRunner(l.opsCtx, runnerID); err != nil && !errors.Is(err, scaleset.RunnerNotFoundError) {
			l.logf("runner %d of %s could not be removed: %v", runnerID, target, err)
		}
	}()
}

// reconcile refreshes the lane from systemd and the run dir, plans, and begins the plan's starts
// and idle stops. Everything it does itself is local (systemctl, the run dir, /proc/meminfo), so it
// takes no context: a stop signal must not cut a state query short and read as a systemd failure.
func (l *Lane) reconcile() {
	l.mu.Lock()
	defer l.mu.Unlock()
	occupied, states, ok := l.refresh()
	if !ok {
		return
	}
	mem, err := l.host.MemAvailable()
	if err != nil {
		l.quietly("meminfo", "MemAvailable unreadable (%v); every start is refused until it reads", err)
		mem = 0
	}
	instances := map[string]int{}
	missing := map[slotRef]string{}
	for kind, k := range l.cfg.Kinds {
		instances[kind] = l.cfg.instances(kind)
		for i, path := range k.RequiresFiles {
			if !l.host.Exists(path) {
				missing[slotRef{kind, i + 1}] = path
			}
		}
	}
	var sets []SetStatus
	for key, e := range l.sets {
		sets = append(sets, SetStatus{Key: key, Known: e.known, Assigned: e.assigned, MinRunners: l.cfg.minRunners(key)})
	}
	// A slot whose set is no longer served (its repository was dropped, or it was adopted at start
	// for a set not served now) gets no job from this listener: its set counts as having none, so
	// its idle slot is stopped like any other instead of holding the budget until RuntimeMaxSec.
	orphaned := map[SetKey]bool{}
	for _, s := range l.slots {
		if _, served := l.sets[s.Set]; !served && s.Set != (SetKey{}) && !orphaned[s.Set] {
			orphaned[s.Set] = true
			sets = append(sets, SetStatus{Key: s.Set, Known: true})
		}
	}
	unitStarts := l.warmUnitStarts(occupied, states)
	now := l.now()
	plan := makePlan(PlanInput{
		Now: now, Slots: l.cfg.Budget.Slots, QAEConcurrency: l.cfg.Budget.QAEConcurrency,
		IdleStop:       time.Duration(l.cfg.IdleStopSec) * time.Second,
		ContainerBytes: l.cfg.Admission.ContainerMemoryBytes, ReserveBytes: l.cfg.Admission.ReserveBytes,
		OwnSlots: l.cfg.ownSlots(), KindContainerBytes: l.cfg.kindContainerBytes(),
		MemAvailable: mem, Instances: instances, RequiresMissing: missing, Sets: sets, Occupied: occupied,
		WarmMaxAge: time.Duration(l.cfg.WarmMaxAgeSec) * time.Second, UnitStarts: unitStarts,
	})
	for _, s := range plan.IdleStops {
		l.beginIdleStop(s, idleStopKind)
	}
	for _, s := range plan.Recycles {
		l.logf("%s has been up %s without a job, past warm_max_age_sec (%ds): recycling it",
			l.unit(s.ref()), now.Sub(unitStarts[s.ref()]).Round(time.Second), l.cfg.WarmMaxAgeSec)
		l.beginIdleStop(s, warmRecycleKind)
	}
	for _, st := range plan.Starts {
		l.beginStart(st)
	}
	for _, r := range plan.Refusals {
		l.quietly(r.Set.String()+"|"+r.Code, "no start for %s: %s: %s", r.Set, r.Code, r.Detail)
	}
}

// refresh forgets slots whose units stopped, clears leftovers, and returns every occupied
// instance with every unit's state. ok is false when unit states are unreadable: nothing is
// planned then.
func (l *Lane) refresh() ([]Slot, map[string]string, bool) {
	refs, units := l.allUnits()
	states, err := l.sys.States(l.opsCtx, units)
	if err != nil {
		l.quietly("states", "unit states unreadable (%v); no starts or stops until they read", err)
		return nil, nil, false
	}
	var occupied []Slot
	for _, ref := range refs {
		unit := l.unit(ref)
		running := unitRunning(states[unit])
		hasJob := l.host.Exists(jobPath(l.runDir, ref.Kind, ref.N))
		if s, tracked := l.slots[ref]; tracked {
			// A start or an idle stop in flight owns its instance until its goroutine returns,
			// whatever the unit's state: a stop must never land on the next slot's unit.
			if running || s.Phase == PhaseStarting || s.Phase == PhaseStopping {
				occupied = append(occupied, *s)
				continue
			}
			switch {
			case hasJob:
				l.clearStale(ref, JobRecord{Target: s.Set.Target, RunnerID: s.RunnerID})
			case s.Phase == PhaseIdle:
				// It exited without a job (a crash, or its RuntimeMaxSec): GitHub still lists its runner.
				l.logf("%s exited without taking a job (runner %s); removing its runner", unit, s.RunnerName)
				l.removeRunnerLater(s.Set.Target, s.RunnerID)
			default:
				l.logf("%s finished (runner %s, %s); the instance is free", unit, s.RunnerName, s.Phase)
			}
			delete(l.slots, ref)
			continue
		}
		switch {
		case !running && hasJob:
			rec := JobRecord{Kind: ref.Kind, Instance: ref.N}
			if parseJobFile(jobPath(l.runDir, ref.Kind, ref.N), &rec) != nil {
				rec = JobRecord{}
			}
			l.clearStale(ref, rec)
			delete(l.foreign, ref)
		case running || hasJob:
			if !l.foreign[ref] {
				l.logf("%s is %s without a slot this listener started; it counts against the budget", unit, states[unit])
				l.foreign[ref] = true
			}
			occupied = append(occupied, Slot{Kind: ref.Kind, Instance: ref.N, Phase: PhaseDone})
		default:
			delete(l.foreign, ref)
		}
	}
	return occupied, states, true
}

// warmUnitStarts reads when the unit of each idle slot of a warm pool entered the active state
// (systemd's ActiveEnterTimestamp): the clock RuntimeMaxSec runs on, and so a warm slot's age for
// a recycle. Only an active unit is asked: one still in its prepare (activating) carries the
// previous run's timestamp. An unreadable answer recycles nothing this round. Called with l.mu held.
func (l *Lane) warmUnitStarts(occupied []Slot, states map[string]string) map[slotRef]time.Time {
	var refs []slotRef
	var units []string
	for _, s := range occupied {
		if s.Phase == PhaseIdle && l.cfg.minRunners(s.Set) > 0 && states[l.unit(s.ref())] == "active" {
			refs = append(refs, s.ref())
			units = append(units, l.unit(s.ref()))
		}
	}
	if len(units) == 0 {
		return nil
	}
	since, err := l.sys.ActiveSince(l.opsCtx, units)
	if err != nil {
		l.quietly("active-since", "unit start times unreadable (%v); no warm slot is recycled until they read", err)
		return nil
	}
	starts := make(map[slotRef]time.Time, len(refs))
	for i, ref := range refs {
		if t, ok := since[units[i]]; ok {
			starts[ref] = t
		}
	}
	return starts
}

// beginStart reserves the instance and starts the slot in the background.
func (l *Lane) beginStart(st Start) {
	e := l.sets[st.Set]
	ref := slotRef{st.Set.Kind, st.Instance}
	s := &Slot{
		Kind: st.Set.Kind, Instance: st.Instance, Set: st.Set, Phase: PhaseStarting, Since: l.now(),
		RunnerName: fmt.Sprintf("%s-%s-%d-%d", l.cfg.Runner.NamePrefix, st.Set.Kind, st.Instance, l.now().Unix()),
	}
	l.slots[ref] = s
	l.inflight.Add(1)
	go l.start(s, e.id, e.api)
}

// start mints the runner, writes the slot's files and starts its unit; any failure undoes all
// three so neither a registration nor a job file outlives it.
func (l *Lane) start(s *Slot, setID int, a api) {
	defer l.inflight.Done()
	ctx := l.opsCtx
	unit := l.unit(s.ref())
	jit, err := a.GenerateJitRunnerConfig(ctx,
		&scaleset.RunnerScaleSetJitRunnerSetting{Name: s.RunnerName, WorkFolder: l.cfg.Runner.WorkFolder}, setID)
	if err == nil && (jit == nil || jit.Runner == nil || jit.EncodedJITConfig == "") {
		err = errors.New("the response carried no runner or no config")
	}
	if err != nil {
		l.logf("start of %s for %s failed: JIT config for %s: %v", unit, s.Set, s.RunnerName, err)
		l.forget(s)
		return
	}
	runnerID := int64(jit.Runner.ID)
	l.mu.Lock()
	s.RunnerID = runnerID
	l.mu.Unlock()
	job := formatJobLine(s.Set.Target, s.Kind, setID, runnerID, s.RunnerName)
	if err := writeSlotFiles(l.runDir, s.Kind, s.Instance, jit.EncodedJITConfig, job); err != nil {
		l.logf("start of %s for %s failed: run-dir files: %v", unit, s.Set, err)
		l.undoStart(ctx, a, s)
		return
	}
	if err := l.sys.Start(ctx, unit); err != nil {
		l.logf("start of %s for %s failed: %v", unit, s.Set, err)
		l.undoStart(ctx, a, s)
		return
	}
	l.mu.Lock()
	if l.slots[s.ref()] == s && s.Phase == PhaseStarting {
		s.Phase, s.Since = PhaseIdle, l.now()
	}
	l.mu.Unlock()
	l.logf("started %s for %s set %d: runner %s (id %d)", unit, s.Set, setID, s.RunnerName, runnerID)
}

func (l *Lane) undoStart(ctx context.Context, a api, s *Slot) {
	if err := a.RemoveRunner(ctx, s.RunnerID); err != nil && !errors.Is(err, scaleset.RunnerNotFoundError) {
		l.logf("runner %s (id %d) could not be removed: %v", s.RunnerName, s.RunnerID, err)
	} else {
		l.logf("runner %s (id %d) removed", s.RunnerName, s.RunnerID)
	}
	if err := removeSlotFiles(l.runDir, s.Kind, s.Instance); err != nil {
		l.logf("files of %s could not all be removed: %v", l.unit(s.ref()), err)
	}
	l.forget(s)
}

func (l *Lane) forget(s *Slot) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.slots[s.ref()] == s {
		delete(l.slots, s.ref())
	}
}

// stopKind names, for the log, why an idle slot is stopped: an idle stop, or a warm recycle. Both
// stop the slot the same way.
type stopKind struct{ name, done string }

var (
	idleStopKind    = stopKind{"idle stop", "idle-stopped"}
	warmRecycleKind = stopKind{"warm recycle", "warm-recycled"}
)

// beginIdleStop marks the slot stopping and stops it in the background.
func (l *Lane) beginIdleStop(planned Slot, kind stopKind) {
	s := l.slots[planned.ref()]
	if s == nil || s.RunnerName != planned.RunnerName {
		return
	}
	a, err := l.apiFor(s.Set.Target)
	if err != nil {
		l.logf("%s of %s skipped: %v", kind.name, l.unit(s.ref()), err)
		return
	}
	s.Phase = PhaseStopping
	l.inflight.Add(1)
	go l.idleStop(s, a, kind)
}

// idleStop writes the marker (the slot's cleanup then counts no failure), removes the runner so
// no job can reach it, and only then stops the unit. A runner GitHub will not remove has just
// taken a job: the marker goes and the slot is left alone.
func (l *Lane) idleStop(s *Slot, a api, kind stopKind) {
	defer l.inflight.Done()
	ctx := l.opsCtx
	unit := l.unit(s.ref())
	if err := writeIdleStop(l.runDir, s.Kind, s.Instance); err != nil {
		l.logf("%s of %s skipped: marker: %v", kind.name, unit, err)
		l.abortStop(s)
		return
	}
	if err := a.RemoveRunner(ctx, s.RunnerID); err != nil && !errors.Is(err, scaleset.RunnerNotFoundError) {
		if rerr := removeIdleStop(l.runDir, s.Kind, s.Instance); rerr != nil {
			l.logf("idle-stop marker of %s could not be removed: %v", unit, rerr)
		}
		l.logf("%s of %s skipped: runner %s was not removed (it may have just taken a job): %v", kind.name, unit, s.RunnerName, err)
		l.abortStop(s)
		return
	}
	if err := l.sys.Stop(ctx, unit); err != nil {
		l.logf("%s of %s: runner %s removed, but the unit did not stop: %v", kind.name, unit, s.RunnerName, err)
	} else {
		l.logf("%s %s: runner %s removed, unit stopped", kind.done, unit, s.RunnerName)
	}
	// Done: the instance frees once the unit is inactive and its job file is gone.
	l.mu.Lock()
	if l.slots[s.ref()] == s && s.Phase == PhaseStopping {
		s.Phase = PhaseDone
	}
	l.mu.Unlock()
}

// abortStop returns a slot whose idle stop or warm recycle did not happen to idle, aged afresh so
// the next idle stop waits a full idle_stop_sec. A warm recycle ages a slot by its unit, which the
// abort leaves as old as it was, so the slot also gets a NoRecycleBefore idle_stop_sec ahead:
// otherwise the next plan, about 5 s later, would try again a runner that has just taken a job.
func (l *Lane) abortStop(s *Slot) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.slots[s.ref()] == s && s.Phase == PhaseStopping {
		now := l.now()
		s.Phase, s.Since = PhaseIdle, now
		s.NoRecycleBefore = now.Add(time.Duration(l.cfg.IdleStopSec) * time.Second)
	}
}

// waitInflight waits up to d for starts, stops and runner removals in flight.
func (l *Lane) waitInflight(d time.Duration) bool {
	done := make(chan struct{})
	go func() {
		l.inflight.Wait()
		close(done)
	}()
	select {
	case <-done:
		return true
	case <-time.After(d):
		return false
	}
}

// quietly logs a message at most once per refusalQuiet for the same key. Called with l.mu held.
func (l *Lane) quietly(key, format string, args ...any) {
	if last, ok := l.quiet[key]; ok && l.now().Sub(last) < refusalQuiet {
		return
	}
	l.quiet[key] = l.now()
	l.logf(format, args...)
}

func (l *Lane) slotByRunner(name string) *Slot {
	for _, s := range l.slots {
		if s.RunnerName == name {
			return s
		}
	}
	return nil
}

func (l *Lane) jobStarted(key SetKey, j *scaleset.JobStarted) {
	l.mu.Lock()
	defer l.mu.Unlock()
	s := l.slotByRunner(j.RunnerName)
	if s == nil {
		l.logf("job %q started on runner %s, which this listener does not track (%s)", j.JobDisplayName, j.RunnerName, key)
		return
	}
	if s.Phase != PhaseDone {
		s.Phase = PhaseBusy
	}
	l.logf("job %q of %s/%s started on %s (runner %s)", j.JobDisplayName, j.OwnerName, j.RepositoryName, l.unit(s.ref()), j.RunnerName)
}

func (l *Lane) jobCompleted(key SetKey, j *scaleset.JobCompleted) {
	l.mu.Lock()
	defer l.mu.Unlock()
	s := l.slotByRunner(j.RunnerName)
	if s == nil {
		l.logf("job %q completed (%s) on runner %s, which this listener does not track (%s)", j.JobDisplayName, j.Result, j.RunnerName, key)
		return
	}
	s.Phase = PhaseDone
	l.logf("job %q of %s/%s completed (%s) on %s; the unit exits by itself", j.JobDisplayName, j.OwnerName, j.RepositoryName, j.Result, l.unit(s.ref()))
}

func (l *Lane) recordStatistics(key SetKey, st *scaleset.RunnerScaleSetStatistic) {
	if st == nil {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	e := l.sets[key]
	if e == nil {
		return
	}
	e.known, e.assigned = true, st.TotalAssignedJobs
	counts := map[Phase]int{}
	for _, s := range l.slots {
		if s.Set == key {
			counts[s.Phase]++
		}
	}
	l.logf("set %s (id %d): assigned %d, running %d, registered %d, busy %d, idle %d; its slots: %d starting, %d idle, %d busy; %s",
		key, e.id, st.TotalAssignedJobs, st.TotalRunningJobs, st.TotalRegisteredRunners, st.TotalBusyRunners,
		st.TotalIdleRunners, counts[PhaseStarting], counts[PhaseIdle], counts[PhaseBusy], l.budgetUse(key.Kind))
}

func (l *Lane) setAssigned(key SetKey, n int) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if e := l.sets[key]; e != nil {
		e.known, e.assigned = true, n
	}
}

func (l *Lane) activeCount(key SetKey) int {
	l.mu.Lock()
	defer l.mu.Unlock()
	n := 0
	for _, s := range l.slots {
		if s.Set == key && s.Phase.active() {
			n++
		}
	}
	return n
}

// heartbeat is the line an otherwise quiet listener logs every heartbeatEvery.
func (l *Lane) heartbeat() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	line := fmt.Sprintf("heartbeat: %d scale sets served, %d of %d slots in use", len(l.sets), l.held(""), l.cfg.Budget.Slots)
	for _, kind := range l.cfg.kindNames() {
		if n := l.cfg.Kinds[kind].Slots; n > 0 {
			line += fmt.Sprintf(", %d of %d %s slots", l.held(kind), n, kind)
		}
	}
	return line
}

// budgetUse says how full the budget a kind draws from is: the lane's shared one, or its own.
// Called with l.mu held.
func (l *Lane) budgetUse(kind string) string {
	if n := l.cfg.Kinds[kind].Slots; n > 0 {
		return fmt.Sprintf("%s %d of %d slots in use", kind, l.held(kind), n)
	}
	return fmt.Sprintf("lane %d of %d slots in use", l.held(""), l.cfg.Budget.Slots)
}

// held counts the instances, tracked or foreign, held against a budget: a kind's own (by its
// name), or with "" the shared one every kind without its own draws from. Called with l.mu held.
func (l *Lane) held(budget string) int {
	n := 0
	count := func(kind string) {
		own := l.cfg.Kinds[kind].Slots > 0
		if (budget == "" && !own) || (own && kind == budget) {
			n++
		}
	}
	for ref := range l.slots {
		count(ref.Kind)
	}
	for ref := range l.foreign {
		count(ref.Kind)
	}
	return n
}

// trackedSlots is a sorted snapshot, for tests and logs.
func (l *Lane) trackedSlots() []Slot {
	l.mu.Lock()
	defer l.mu.Unlock()
	var out []Slot
	for _, s := range l.slots {
		out = append(out, *s)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Kind != out[j].Kind {
			return out[i].Kind < out[j].Kind
		}
		return out[i].Instance < out[j].Instance
	})
	return out
}

func (l *Lane) scaler(key SetKey) *setScaler { return &setScaler{lane: l, key: key} }

// setScaler is one set's listener.Scaler and listener.MetricsRecorder: it reports the set's
// messages to the lane, and every desired-count call (each message, and each empty long poll)
// reconciles the whole lane. It never returns an error: a failed start is logged and retried by
// a later plan, and an error here would stop the set's listener.
type setScaler struct {
	lane *Lane
	key  SetKey
}

var (
	_ listener.Scaler          = (*setScaler)(nil)
	_ listener.MetricsRecorder = (*setScaler)(nil)
)

func (s *setScaler) HandleJobStarted(_ context.Context, j *scaleset.JobStarted) error {
	s.lane.jobStarted(s.key, j)
	return nil
}

func (s *setScaler) HandleJobCompleted(_ context.Context, j *scaleset.JobCompleted) error {
	s.lane.jobCompleted(s.key, j)
	return nil
}

func (s *setScaler) HandleDesiredRunnerCount(_ context.Context, assigned int) (int, error) {
	s.lane.setAssigned(s.key, assigned)
	s.lane.reconcile()
	return s.lane.activeCount(s.key), nil
}

func (s *setScaler) RecordStatistics(st *scaleset.RunnerScaleSetStatistic) {
	s.lane.recordStatistics(s.key, st)
}

func (s *setScaler) RecordJobStarted(*scaleset.JobStarted)     {}
func (s *setScaler) RecordJobCompleted(*scaleset.JobCompleted) {}
func (s *setScaler) RecordDesiredRunners(int)                  {}
