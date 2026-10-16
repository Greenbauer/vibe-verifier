package main

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"reflect"
	"sort"
	"sync"
	"time"

	"github.com/actions/scaleset"
	"github.com/actions/scaleset/listener"
)

// refusalQuiet is how often one set's refusal of one kind is repeated in the log while it lasts.
const refusalQuiet = time.Minute

// retiredKeep is how long a runner whose slot reconcile forgot still has its late job events
// recorded, and maxRetired bounds how many such runners are kept. GitHub can deliver a short job's
// JobStarted and JobCompleted after its unit has already exited and the slot is gone.
const (
	retiredKeep = 15 * time.Minute
	maxRetired  = 256
)

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

	// recMu orders the writes of queue.json and jobs.json, so the last file written holds the
	// latest state, and guards jobs, the history jobs.json is written from, and queueWritten. It is
	// taken before mu when both are held, never while mu is.
	recMu sync.Mutex
	jobs  []jobTiming
	// queueWritten is queue.json's content as last written, nil until it first is; reconcile
	// rewrites the file only when the content (updated_at aside) changed.
	queueWritten *queueRecord

	mu       sync.Mutex
	sets     map[SetKey]*setEntry
	slots    map[slotRef]*Slot
	foreign  map[slotRef]bool
	quiet    map[string]time.Time
	inflight sync.WaitGroup
	// retired is every runner whose slot reconcile forgot within retiredKeep, by runner name, so a
	// job event GitHub delivers late still reaches jobs.json.
	retired map[string]retiredRunner
}

// retiredRunner is where a forgotten slot's runner ran, and when reconcile forgot it.
type retiredRunner struct {
	ref slotRef
	set SetKey
	at  time.Time
}

type setEntry struct {
	id       int
	api      api
	known    bool
	assigned int
	// stats is the set's latest statistics for queue.json; nil until the first arrives.
	stats *queueSet
}

func newLane(cfg *Config, runDir string, sys Systemd, host hostProbe, newAPI func(string) (api, error), logf func(string, ...any)) *Lane {
	return &Lane{
		cfg: cfg, runDir: runDir, sys: sys, host: host, logf: logf, now: time.Now, newAPI: newAPI,
		opsCtx: context.Background(), apis: map[string]api{},
		sets: map[SetKey]*setEntry{}, slots: map[slotRef]*Slot{}, foreign: map[slotRef]bool{}, quiet: map[string]time.Time{},
		retired: map[string]retiredRunner{},
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
	l.loadJobHistory()
	l.mu.Lock()
	defer l.mu.Unlock()
	records := readJobFiles(l.runDir, l.cfg.kindNames())
	if len(records) == 0 {
		l.logf("run dir %s holds no slots to adopt", l.runDir)
		l.sweepAssignments()
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
	l.sweepAssignments()
}

// loadJobHistory takes up the jobs.json a previous listener left, so a restart keeps the history.
func (l *Lane) loadJobHistory() {
	jobs, err := readJobHistory(l.runDir)
	l.recMu.Lock()
	l.jobs = jobs
	l.recMu.Unlock()
	if err != nil {
		l.logf("job history %s ignored: %v", filepath.Join(l.runDir, jobsFile), err)
	}
}

// sweepAssignments removes a job record left in a slot the listener is not tracking and whose job
// file is already gone. The slot script's cleanup cannot remove it, so a restart would otherwise
// keep the previous job's name until the next start. Called with l.mu held.
func (l *Lane) sweepAssignments() {
	for _, kind := range l.cfg.kindNames() {
		for n := 1; n <= l.cfg.kindSlots(kind); n++ {
			ref := slotRef{kind, n}
			if _, tracked := l.slots[ref]; tracked || l.host.Exists(jobPath(l.runDir, kind, n)) {
				continue
			}
			l.dropAssignment(ref)
		}
	}
}

func (l *Lane) dropAssignment(ref slotRef) {
	if err := removeAssignment(l.runDir, ref.Kind, ref.N); err != nil {
		l.logf("job record of %s could not be removed: %v", l.unit(ref), err)
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

// reconcile refreshes the lane from systemd and the run dir, plans, begins the plan's starts and
// idle stops, and then rewrites queue.json if its content changed, so the warm pool it names is at
// most one reconcile behind the slots. Everything it does itself is local (systemctl, the run dir,
// /proc/meminfo), so it takes no context: a stop signal must not cut a state query short and read
// as a systemd failure.
func (l *Lane) reconcile() {
	l.replan()
	l.refreshQueue()
}

// replan is reconcile's work under l.mu: refresh, plan, and begin the plan's starts and stops.
func (l *Lane) replan() {
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

// refreshQueue rewrites queue.json when its content, updated_at aside, differs from what was last
// written. Statistics arrive only with GitHub's messages, but the warm pool changes with every start,
// idle stop, recycle and taken job. Nothing is written before the first statistics. Takes l.recMu,
// then l.mu (the lock order), so it must be called with neither held.
func (l *Lane) refreshQueue() {
	l.recMu.Lock()
	defer l.recMu.Unlock()
	l.mu.Lock()
	rec := l.queueRecord()
	l.mu.Unlock()
	last := l.queueWritten
	if last == nil && len(rec.Sets) == 0 {
		return // no set has had statistics yet
	}
	if last != nil && last.Version == rec.Version && reflect.DeepEqual(last.Sets, rec.Sets) {
		return
	}
	l.writeQueue(rec, "the lane")
}

// writeQueue writes queue.json and remembers what it wrote. Called with l.recMu held.
func (l *Lane) writeQueue(rec queueRecord, what string) {
	if err := writeLaneRecord(l.runDir, queueFile, rec); err != nil {
		l.logf("queue record could not be written for %s: %v", what, err)
		return
	}
	l.queueWritten = &rec
}

// retire remembers the runner of a slot reconcile is about to forget, for retiredKeep, so a late
// JobStarted or JobCompleted for it still reaches jobs.json. Expired entries are dropped first, then
// the oldest while maxRetired are kept. Called with l.mu held.
func (l *Lane) retire(s *Slot) {
	if s.RunnerName == "" {
		return
	}
	now := l.now()
	for name, r := range l.retired {
		if now.Sub(r.at) > retiredKeep {
			delete(l.retired, name)
		}
	}
	for len(l.retired) >= maxRetired {
		oldest := ""
		for name, r := range l.retired {
			if oldest == "" || r.at.Before(l.retired[oldest].at) {
				oldest = name
			}
		}
		delete(l.retired, oldest)
	}
	l.retired[s.RunnerName] = retiredRunner{ref: s.ref(), set: s.Set, at: now}
}

// retiredRunner is the runner's entry when reconcile forgot its slot within retiredKeep. Called
// with l.mu held.
func (l *Lane) retiredRunner(name string) (retiredRunner, bool) {
	r, ok := l.retired[name]
	if !ok || l.now().Sub(r.at) > retiredKeep {
		return retiredRunner{}, false
	}
	return r, true
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
				l.dropAssignment(ref)
			default:
				l.logf("%s finished (runner %s, %s); the instance is free", unit, s.RunnerName, s.Phase)
				l.dropAssignment(ref)
			}
			l.retire(s)
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
			l.dropAssignment(ref)
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
	s := l.slotByRunner(j.RunnerName)
	if s == nil {
		r, retired := l.retiredRunner(j.RunnerName)
		l.mu.Unlock()
		if !retired {
			l.logf("job %q started on runner %s, which this listener does not track (%s)", j.JobDisplayName, j.RunnerName, key)
			return
		}
		l.lateStart(r, j)
		return
	}
	if s.Phase != PhaseDone {
		s.Phase = PhaseBusy
	}
	ref := s.ref()
	l.logf("job %q of %s/%s started on %s (runner %s)", j.JobDisplayName, j.OwnerName, j.RepositoryName, l.unit(ref), j.RunnerName)
	timing := jobTiming{Kind: ref.Kind, Instance: ref.N, RunnerName: s.RunnerName, StartedAt: stamp(l.now())}
	l.mu.Unlock()
	l.recordAssignment(ref, j.OwnerName, j.RepositoryName, j.JobDisplayName, j.WorkflowRunID, j.JobID)
	timing.setJob(&j.JobMessageBase)
	l.recordJob(ref, timing)
}

// lateStart records, in jobs.json only, a JobStarted heard after reconcile forgot the runner's slot:
// no assignment file, slot or phase is touched. The job is over by then, so its start is GitHub's
// runner-assigned time, the listener's clock only when GitHub sent none.
func (l *Lane) lateStart(r retiredRunner, j *scaleset.JobStarted) {
	l.logf("late start of job %q of %s/%s on %s (runner %s of %s, retired %s ago): recorded in the job history only",
		j.JobDisplayName, j.OwnerName, j.RepositoryName, l.unit(r.ref), j.RunnerName, r.set, l.now().Sub(r.at).Round(time.Second))
	timing := jobTiming{Kind: r.ref.Kind, Instance: r.ref.N, RunnerName: j.RunnerName}
	timing.setJob(&j.JobMessageBase)
	timing.StartedAt = timing.RunnerAssignedAt
	if timing.StartedAt == "" {
		timing.StartedAt = stamp(l.now())
	}
	l.recordJob(r.ref, timing)
}

// setJob takes the job's GitHub times and, when usable, its assignment fields from a job message.
func (t *jobTiming) setJob(j *scaleset.JobMessageBase) {
	t.QueuedAt, t.ScaleSetAssignedAt, t.RunnerAssignedAt = stamp(j.QueueTime), stamp(j.ScaleSetAssignTime), stamp(j.RunnerAssignTime)
	if a, ok := assignmentFields(j.OwnerName, j.RepositoryName, j.JobDisplayName, j.WorkflowRunID, j.JobID); ok {
		t.Repository, t.Name, t.RunID, t.JobID = a.Repository, a.Name, a.RunID, a.JobID
	}
}

// recordJob adds a started job to the history and rewrites jobs.json. A JobStarted heard after the
// runner's job already finished fills that entry in (fillFrom) instead of adding a second one: a JIT
// runner runs one job, so a start for a finished runner is that job's late start. Keying this on the
// saved finish time, not a flag in memory, keeps it right across a listener restart.
func (l *Lane) recordJob(ref slotRef, timing jobTiming) {
	l.recMu.Lock()
	defer l.recMu.Unlock()
	if e := l.lastJob(timing.RunnerName); e != nil && e.FinishedAt != "" {
		e.fillFrom(timing)
	} else {
		l.jobs = append(l.jobs, timing)
	}
	l.writeJobs(ref)
}

// recordFinish sets when a job finished on the runner's newest history entry, or, when the listener
// recorded no start for that runner, adds the entry from the completion, and rewrites jobs.json.
func (l *Lane) recordFinish(ref slotRef, timing jobTiming) {
	l.recMu.Lock()
	defer l.recMu.Unlock()
	if e := l.lastJob(timing.RunnerName); e != nil {
		e.FinishedAt = timing.FinishedAt
	} else {
		l.jobs = append(l.jobs, timing)
	}
	l.writeJobs(ref)
}

// fillFrom takes what a late JobStarted knows into the entry a JobCompleted made, keeping the
// entry's value wherever the start message has none. The start becomes GitHub's runner-assigned
// time when there is one; the listener's clock is never used, being by then past the finish.
func (t *jobTiming) fillFrom(start jobTiming) {
	take := func(dst *string, v string) {
		if v != "" {
			*dst = v
		}
	}
	if start.Name != "" {
		t.Repository, t.Name, t.RunID, t.JobID = start.Repository, start.Name, start.RunID, start.JobID
	}
	take(&t.QueuedAt, start.QueuedAt)
	take(&t.ScaleSetAssignedAt, start.ScaleSetAssignedAt)
	take(&t.RunnerAssignedAt, start.RunnerAssignedAt)
	take(&t.StartedAt, start.RunnerAssignedAt)
}

// lastJob is the newest history entry for a runner, or nil. Called with l.recMu held.
func (l *Lane) lastJob(runner string) *jobTiming {
	for i := len(l.jobs) - 1; i >= 0; i-- {
		if l.jobs[i].RunnerName == runner {
			return &l.jobs[i]
		}
	}
	return nil
}

// writeJobs drops the oldest jobs past maxJobHistory and rewrites jobs.json. Called with l.recMu
// held.
func (l *Lane) writeJobs(ref slotRef) {
	if n := len(l.jobs); n > maxJobHistory {
		l.jobs = append([]jobTiming(nil), l.jobs[n-maxJobHistory:]...)
	}
	if err := writeLaneRecord(l.runDir, jobsFile, jobsRecord{Version: 1, Jobs: l.jobs}); err != nil {
		l.logf("job history could not be written for %s: %v", l.unit(ref), err)
	}
}

func (l *Lane) recordAssignment(ref slotRef, owner, repository, name string, runID int64, jobID string) {
	body, ok := assignmentRecord(owner, repository, name, runID, jobID)
	if !ok {
		l.logf("job record of %s not written: the job name or repository is not usable", l.unit(ref))
		return
	}
	if err := writeAssignment(l.runDir, ref.Kind, ref.N, body); err != nil {
		l.logf("job record of %s could not be written: %v", l.unit(ref), err)
	}
}

func (l *Lane) jobCompleted(key SetKey, j *scaleset.JobCompleted) {
	l.mu.Lock()
	var ref slotRef
	if s := l.slotByRunner(j.RunnerName); s != nil {
		s.Phase = PhaseDone
		ref = s.ref()
		l.logf("job %q of %s/%s completed (%s) on %s; the unit exits by itself", j.JobDisplayName, j.OwnerName, j.RepositoryName, j.Result, l.unit(ref))
	} else if r, retired := l.retiredRunner(j.RunnerName); retired {
		// Reconcile already forgot the slot: only the job history is written.
		ref = r.ref
		l.logf("late completion of job %q of %s/%s (%s) on %s (runner %s of %s, retired %s ago): recorded in the job history only",
			j.JobDisplayName, j.OwnerName, j.RepositoryName, j.Result, l.unit(ref), j.RunnerName, r.set, l.now().Sub(r.at).Round(time.Second))
	} else {
		l.logf("job %q completed (%s) on runner %s, which this listener does not track (%s)", j.JobDisplayName, j.Result, j.RunnerName, key)
		l.mu.Unlock()
		return
	}
	now := l.now()
	timing := jobTiming{Kind: ref.Kind, Instance: ref.N, RunnerName: j.RunnerName, StartedAt: stamp(now), FinishedAt: stamp(now)}
	l.mu.Unlock()
	timing.setJob(&j.JobMessageBase)
	if timing.RunnerAssignedAt != "" {
		timing.StartedAt = timing.RunnerAssignedAt
	}
	if !j.FinishTime.IsZero() {
		timing.FinishedAt = stamp(j.FinishTime)
		if timing.RunnerAssignedAt == "" {
			timing.StartedAt = timing.FinishedAt // never a start after the finish GitHub reported
		}
	}
	l.recordFinish(ref, timing)
}

// recordStatistics logs a set's statistics, keeps them for the plan and queue.json, and rewrites
// queue.json.
func (l *Lane) recordStatistics(key SetKey, st *scaleset.RunnerScaleSetStatistic) {
	if st == nil {
		return
	}
	l.recMu.Lock()
	defer l.recMu.Unlock()
	queue, ok := l.noteStatistics(key, st)
	if !ok {
		return
	}
	l.writeQueue(queue, key.String())
}

// noteStatistics records one set's statistics and returns queue.json's content, or ok false for a
// set the lane does not track. Called with l.recMu held.
func (l *Lane) noteStatistics(key SetKey, st *scaleset.RunnerScaleSetStatistic) (queueRecord, bool) {
	l.mu.Lock()
	defer l.mu.Unlock()
	e := l.sets[key]
	if e == nil {
		return queueRecord{}, false
	}
	e.known, e.assigned = true, st.TotalAssignedJobs
	e.stats = &queueSet{Kind: key.Kind, Assigned: st.TotalAssignedJobs, Running: st.TotalRunningJobs}
	counts := map[Phase]int{}
	for _, s := range l.slots {
		if s.Set == key {
			counts[s.Phase]++
		}
	}
	l.logf("set %s (id %d): assigned %d, running %d, registered %d, busy %d, idle %d; its slots: %d starting, %d idle, %d busy; %s",
		key, e.id, st.TotalAssignedJobs, st.TotalRunningJobs, st.TotalRegisteredRunners, st.TotalBusyRunners,
		st.TotalIdleRunners, counts[PhaseStarting], counts[PhaseIdle], counts[PhaseBusy], l.budgetUse(key.Kind))
	return l.queueRecord(), true
}

// queueRecord is queue.json's content: each tracked set with statistics, by set key. Called with
// l.mu held.
func (l *Lane) queueRecord() queueRecord {
	keys := make([]SetKey, 0, len(l.sets))
	for key, e := range l.sets {
		if e.stats != nil {
			keys = append(keys, key)
		}
	}
	sort.Slice(keys, func(i, j int) bool { return keys[i].String() < keys[j].String() })
	rec := queueRecord{Version: 1, UpdatedAt: stamp(l.now()), Sets: make([]queueSet, 0, len(keys))}
	for _, key := range keys {
		set := *l.sets[key].stats
		set.MinRunners = l.cfg.minRunners(key)
		set.WarmInstances = l.warmInstances(key, set.MinRunners)
		rec.Sets = append(rec.Sets, set)
	}
	return rec
}

// warmInstances are the idle slots of a set its warm pool keeps: the newest minRunners in
// planIdleStops' order (sortIdle). Called with l.mu held.
func (l *Lane) warmInstances(key SetKey, minRunners int) []int {
	var idle []Slot
	for _, s := range l.slots {
		if s.Set == key && s.Phase == PhaseIdle {
			idle = append(idle, *s)
		}
	}
	sortIdle(idle)
	kept := []int{}
	for _, s := range idle[max(0, len(idle)-minRunners):] {
		kept = append(kept, s.Instance)
	}
	sort.Ints(kept)
	return kept
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
