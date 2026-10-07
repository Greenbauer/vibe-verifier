package main

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/actions/scaleset"
	"github.com/actions/scaleset/listener"
)

// api is the part of the scale set client the listener uses, bound to one scope target (the
// organisation, or one repository). scalesetAPI adapts *scaleset.Client; tests use a fake.
type api interface {
	GetRunnerGroupByName(ctx context.Context, name string) (*scaleset.RunnerGroup, error)
	GetRunnerScaleSet(ctx context.Context, groupID int, name string) (*scaleset.RunnerScaleSet, error)
	CreateRunnerScaleSet(ctx context.Context, set *scaleset.RunnerScaleSet) (*scaleset.RunnerScaleSet, error)
	UpdateRunnerScaleSet(ctx context.Context, id int, set *scaleset.RunnerScaleSet) (*scaleset.RunnerScaleSet, error)
	DeleteRunnerScaleSet(ctx context.Context, id int) error
	GenerateJitRunnerConfig(ctx context.Context, setting *scaleset.RunnerScaleSetJitRunnerSetting, setID int) (*scaleset.RunnerScaleSetJitRunnerConfig, error)
	RemoveRunner(ctx context.Context, runnerID int64) error
	OpenSession(ctx context.Context, setID int, owner string) (session, error)
}

// session is one set's message session: what the listener package polls, plus Close.
type session interface {
	listener.Client
	Close(ctx context.Context) error
}

type scalesetAPI struct{ *scaleset.Client }

func (s scalesetAPI) OpenSession(ctx context.Context, setID int, owner string) (session, error) {
	c, err := s.MessageSessionClient(ctx, setID, owner)
	if err != nil {
		return nil, err
	}
	return c, nil
}

// ensureSet makes the named set exist in the group with exactly these labels (compared without
// case, as GitHub matches them): create it when absent, update it when its labels differ. It
// returns what it did ("created", "labels updated" or "").
func ensureSet(ctx context.Context, a api, groupID int, name string, labels []string) (*scaleset.RunnerScaleSet, string, error) {
	want := &scaleset.RunnerScaleSet{
		Name: name, RunnerGroupID: groupID, RunnerSetting: scaleset.RunnerSetting{DisableUpdate: true},
	}
	for _, label := range labels {
		want.Labels = append(want.Labels, scaleset.Label{Name: label})
	}
	existing, err := a.GetRunnerScaleSet(ctx, groupID, name)
	switch {
	case err != nil:
		return nil, "", err
	case existing == nil:
		created, err := a.CreateRunnerScaleSet(ctx, want)
		return created, "created", err
	case sameLabels(existing.Labels, labels):
		return existing, "", nil
	}
	updated, err := a.UpdateRunnerScaleSet(ctx, existing.ID, want)
	return updated, "labels updated", err
}

func sameLabels(have []scaleset.Label, want []string) bool {
	norm := func(names []string) []string {
		out := make([]string, len(names))
		for i, n := range names {
			out[i] = strings.ToLower(n)
		}
		slices.Sort(out)
		return slices.Compact(out)
	}
	var names []string
	for _, l := range have {
		names = append(names, l.Name)
	}
	return slices.Equal(norm(names), norm(want))
}

// setManager owns the lane's scale sets: one per kind per scope target, a supervisor per set that
// keeps its message session and listener running, and (user scope) the repository sync.
type setManager struct {
	cfg   *Config
	lane  *Lane
	owner string // the session owner: this host's name
	logf  func(string, ...any)
	fail  context.CancelCauseFunc
	// supervise runs one set until its context ends; tests replace it.
	supervise func(ctx context.Context, key SetKey, a api, setID int)

	sessionWindow time.Duration // how long a set may fail to open a session before the process exits
	retryFirst    time.Duration
	retryMax      time.Duration
	reopenDelay   time.Duration

	mu      sync.Mutex
	targets map[string]*target
}

type target struct {
	api    api
	sets   map[string]int // kind -> scale set id
	cancel context.CancelFunc
	wg     sync.WaitGroup
}

func newSetManager(cfg *Config, lane *Lane, owner string, logf func(string, ...any), fail context.CancelCauseFunc) *setManager {
	m := &setManager{
		cfg: cfg, lane: lane, owner: owner, logf: logf, fail: fail,
		sessionWindow: 5 * time.Minute, retryFirst: 5 * time.Second, retryMax: time.Minute, reopenDelay: 10 * time.Second,
		targets: map[string]*target{},
	}
	m.supervise = m.superviseSet
	return m
}

// groupID is the runner group the target's sets live in.
func (m *setManager) groupID(ctx context.Context, a api) (int, error) {
	return runnerGroupID(ctx, a, m.cfg.RunnerGroup)
}

// runnerGroupID resolves a runner group on one scope target: `default` is id 1 (every
// repository's only group); a named organisation group is looked up.
func runnerGroupID(ctx context.Context, a api, group string) (int, error) {
	if strings.EqualFold(group, scaleset.DefaultRunnerGroup) {
		return 1, nil
	}
	g, err := a.GetRunnerGroupByName(ctx, group)
	if err != nil {
		return 0, fmt.Errorf("runner group %q: %w", group, err)
	}
	return g.ID, nil
}

// deleteLaneSets deletes the lane's scale sets, every kind's, on every given target. A set that is
// already gone is fine; a failure is logged and returned, and the other targets and kinds are still
// tried.
func deleteLaneSets(ctx context.Context, cfg *Config, newAPI func(string) (api, error), targets []string, logf func(string, ...any)) error {
	var errs []error
	for _, target := range targets {
		a, err := newAPI(target)
		if err != nil {
			errs = append(errs, fmt.Errorf("%s: %w", target, err))
			continue
		}
		groupID, err := runnerGroupID(ctx, a, cfg.RunnerGroup)
		if err != nil {
			errs = append(errs, fmt.Errorf("%s: %w", target, err))
			continue
		}
		for _, kind := range cfg.kindNames() {
			name := cfg.Kinds[kind].SetName
			set, err := a.GetRunnerScaleSet(ctx, groupID, name)
			switch {
			case err != nil:
				errs = append(errs, fmt.Errorf("%s %s: scale set %q unreadable: %w", target, kind, name, err))
			case set == nil:
				logf("scale set %q of %s %s: none to delete", name, target, kind)
			default:
				if err := a.DeleteRunnerScaleSet(ctx, set.ID); err != nil {
					errs = append(errs, fmt.Errorf("%s %s: scale set %d (%q) not deleted: %w", target, kind, set.ID, name, err))
					continue
				}
				logf("scale set %d (%q) of %s %s deleted", set.ID, name, target, kind)
			}
		}
	}
	for _, err := range errs {
		logf("%v", err)
	}
	return errors.Join(errs...)
}

// ensureTarget makes one scope target's sets exist and starts their supervisors. A target already
// served is left alone.
func (m *setManager) ensureTarget(ctx context.Context, name string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, ok := m.targets[name]; ok {
		return nil
	}
	if err := ctx.Err(); err != nil {
		return err // stopping: no new supervisor may start after stopAll
	}
	a, err := m.lane.apiFor(name)
	if err != nil {
		return err
	}
	groupID, err := m.groupID(ctx, a)
	if err != nil {
		return err
	}
	t := &target{api: a, sets: map[string]int{}}
	for _, kind := range m.cfg.kindNames() {
		k := m.cfg.Kinds[kind]
		set, did, err := ensureSet(ctx, a, groupID, k.SetName, k.Labels)
		if err != nil {
			return fmt.Errorf("%s %s: scale set %q: %w", name, kind, k.SetName, err)
		}
		if did != "" {
			m.logf("scale set %q (id %d) %s for %s %s: runner group %d, labels %s",
				k.SetName, set.ID, did, name, kind, groupID, strings.Join(k.Labels, ","))
		}
		t.sets[kind] = set.ID
	}
	var tctx context.Context
	tctx, t.cancel = context.WithCancel(ctx)
	for kind, id := range t.sets {
		key := SetKey{name, kind}
		m.lane.addSet(key, id, a)
		t.wg.Add(1)
		go func() {
			defer t.wg.Done()
			m.supervise(tctx, key, a, id)
		}()
	}
	m.targets[name] = t
	return nil
}

// dropTarget stops a target's supervisors (closing their sessions) and deletes its sets. Its
// running slots are left to finish their jobs.
func (m *setManager) dropTarget(ctx context.Context, name string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	t, ok := m.targets[name]
	if !ok {
		return
	}
	delete(m.targets, name)
	t.cancel()
	t.wg.Wait()
	for kind, id := range t.sets {
		m.lane.removeSet(SetKey{name, kind})
		if err := t.api.DeleteRunnerScaleSet(ctx, id); err != nil {
			m.logf("scale set %d of %s %s could not be deleted: %v", id, name, kind, err)
			continue
		}
		m.logf("scale set %d of %s %s deleted: the repository is no longer served", id, name, kind)
	}
}

// stopAll ends every supervisor, which closes every session. Scale sets are never deleted here.
func (m *setManager) stopAll() {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, t := range m.targets {
		t.cancel()
	}
	for _, t := range m.targets {
		t.wg.Wait()
	}
}

// syncTargets makes the served targets exactly the given list: new ones get their sets, gone ones
// lose them. It then deletes this lane's sets found on any repository it does not serve, which
// covers a repository archived, excluded or made public while the listener was down, and retries
// a deletion that failed.
func (m *setManager) syncTargets(ctx context.Context, served, unserved []string) {
	wanted := map[string]bool{}
	for _, name := range served {
		wanted[name] = true
		if err := m.ensureTarget(ctx, name); err != nil {
			m.logf("%s not served yet: %v (retried at the next refresh)", name, err)
		}
	}
	m.mu.Lock()
	var gone []string
	for name := range m.targets {
		if !wanted[name] {
			gone = append(gone, name)
		}
	}
	m.mu.Unlock()
	slices.Sort(gone)
	for _, name := range gone {
		m.dropTarget(ctx, name)
	}
	m.sweep(ctx, unserved)
}

func (m *setManager) sweep(ctx context.Context, unserved []string) {
	for _, name := range unserved {
		a, err := m.lane.apiFor(name)
		if err != nil {
			m.logf("sweep of %s skipped: %v", name, err)
			continue
		}
		for _, kind := range m.cfg.kindNames() {
			setName := m.cfg.Kinds[kind].SetName
			set, err := a.GetRunnerScaleSet(ctx, 1, setName)
			if err != nil {
				m.logf("sweep of %s: scale set %q unreadable: %v", name, setName, err)
				continue
			}
			if set == nil {
				continue
			}
			if err := a.DeleteRunnerScaleSet(ctx, set.ID); err != nil {
				m.logf("sweep of %s: scale set %d could not be deleted: %v", name, set.ID, err)
				continue
			}
			m.logf("scale set %d (%q) of %s deleted: the repository is not served", set.ID, setName, name)
		}
	}
}

// userLoop keeps a user-scope lane's sets matching the installation's repositories.
func (m *setManager) userLoop(ctx context.Context, app *appClient, every time.Duration) {
	for {
		repos, err := app.repositories(ctx)
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			m.logf("repository list unreadable: %v; the current sets stay", err)
		} else {
			served, unserved := servedTargets(repos, m.cfg.owner(), m.cfg.Repos.Exclude)
			m.syncTargets(ctx, served, unserved)
		}
		if !sleepCtx(ctx, every) {
			return
		}
	}
}

// superviseSet keeps one set's session open and its listener running until ctx ends.
func (m *setManager) superviseSet(ctx context.Context, key SetKey, a api, setID int) {
	scaler := m.lane.scaler(key)
	for ctx.Err() == nil {
		sess, err := m.openSession(ctx, key, a, setID)
		if err != nil {
			if ctx.Err() == nil {
				m.fail(fmt.Errorf("set %s (id %d): %w", key, setID, err))
			}
			return
		}
		m.logf("set %s (id %d): message session open", key, setID)
		l, err := listener.New(sess, listener.Config{ScaleSetID: setID, MaxRunners: m.cfg.kindSlots(key.Kind)},
			listener.WithMetricsRecorder(scaler))
		if err == nil {
			err = l.Run(ctx, scaler)
		}
		m.closeSession(key, setID, sess)
		if ctx.Err() != nil {
			return
		}
		m.logf("set %s (id %d): listener stopped (%v); reopening its session in %s", key, setID, err, m.reopenDelay)
		if !sleepCtx(ctx, m.reopenDelay) {
			return
		}
	}
}

// openSession retries a session GitHub refuses, typically because an earlier listener's session
// on the set has not expired yet, with a doubling wait, for at most sessionWindow.
func (m *setManager) openSession(ctx context.Context, key SetKey, a api, setID int) (session, error) {
	deadline := time.Now().Add(m.sessionWindow)
	delay := m.retryFirst
	for {
		sess, err := a.OpenSession(ctx, setID, m.owner)
		if err == nil {
			return sess, nil
		}
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		if !time.Now().Add(delay).Before(deadline) {
			return nil, fmt.Errorf("no message session within %s: %w", m.sessionWindow, err)
		}
		m.logf("set %s (id %d): message session not opened (%v); retrying in %s, until %s after the first try (a 409 Conflict is a previous listener's session that has not expired yet)",
			key, setID, err, delay, m.sessionWindow)
		if !sleepCtx(ctx, delay) {
			return nil, ctx.Err()
		}
		delay = min(2*delay, m.retryMax)
	}
}

func (m *setManager) closeSession(key SetKey, setID int, sess session) {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if err := sess.Close(ctx); err != nil {
		m.logf("set %s (id %d): session close failed: %v", key, setID, err)
		return
	}
	m.logf("set %s (id %d): session closed", key, setID)
}

// sleepCtx waits d, or less if ctx ends; it reports whether the full wait passed.
func sleepCtx(ctx context.Context, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}
