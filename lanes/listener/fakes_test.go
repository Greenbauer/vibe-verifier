package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/actions/scaleset"
)

// fakeAPI is one scope target's scale set service.
type fakeAPI struct {
	mu         sync.Mutex
	target     string
	nextID     *int // shared across targets, as set ids are unique per service
	sets       map[string]*scaleset.RunnerScaleSet
	groups     map[string]int
	created    []string
	updated    []string
	deleted    []int
	jits       []scaleset.RunnerScaleSetJitRunnerSetting
	jitErr     error
	nextRunner int64
	removed    []int64
	removeErr  error
	removeGate chan struct{} // when set, RemoveRunner waits for it to close
	deleteErr  error
	openErrs   []error // returned by OpenSession, in order, before sessions succeed
	sessions   []*fakeSession
	newSession func() *fakeSession
}

func (f *fakeAPI) GetRunnerGroupByName(_ context.Context, name string) (*scaleset.RunnerGroup, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	id, ok := f.groups[name]
	if !ok {
		return nil, fmt.Errorf("no runner group %q", name)
	}
	return &scaleset.RunnerGroup{ID: id, Name: name}, nil
}

func (f *fakeAPI) GetRunnerScaleSet(_ context.Context, groupID int, name string) (*scaleset.RunnerScaleSet, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if s, ok := f.sets[name]; ok && s.RunnerGroupID == groupID {
		c := *s
		return &c, nil
	}
	return nil, nil
}

func (f *fakeAPI) CreateRunnerScaleSet(_ context.Context, s *scaleset.RunnerScaleSet) (*scaleset.RunnerScaleSet, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	*f.nextID++
	c := *s
	c.ID = *f.nextID
	f.sets[s.Name] = &c
	f.created = append(f.created, s.Name)
	out := c
	return &out, nil
}

func (f *fakeAPI) UpdateRunnerScaleSet(_ context.Context, id int, s *scaleset.RunnerScaleSet) (*scaleset.RunnerScaleSet, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	c := *s
	c.ID = id
	f.sets[s.Name] = &c
	f.updated = append(f.updated, s.Name)
	out := c
	return &out, nil
}

func (f *fakeAPI) DeleteRunnerScaleSet(_ context.Context, id int) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.deleteErr != nil {
		return f.deleteErr
	}
	for name, s := range f.sets {
		if s.ID == id {
			delete(f.sets, name)
		}
	}
	f.deleted = append(f.deleted, id)
	return nil
}

func (f *fakeAPI) GenerateJitRunnerConfig(_ context.Context, s *scaleset.RunnerScaleSetJitRunnerSetting, _ int) (*scaleset.RunnerScaleSetJitRunnerConfig, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.jits = append(f.jits, *s)
	if f.jitErr != nil {
		return nil, f.jitErr
	}
	f.nextRunner++
	return &scaleset.RunnerScaleSetJitRunnerConfig{
		Runner:           &scaleset.RunnerReference{ID: int(f.nextRunner), Name: s.Name},
		EncodedJITConfig: "JIT-FOR-" + s.Name,
	}, nil
}

func (f *fakeAPI) RemoveRunner(_ context.Context, id int64) error {
	f.mu.Lock()
	gate := f.removeGate
	f.mu.Unlock()
	if gate != nil {
		<-gate
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.removed = append(f.removed, id)
	return f.removeErr
}

func (f *fakeAPI) OpenSession(context.Context, int, string) (session, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.openErrs) > 0 {
		err := f.openErrs[0]
		f.openErrs = f.openErrs[1:]
		return nil, err
	}
	s := &fakeSession{}
	if f.newSession != nil {
		s = f.newSession()
	}
	f.sessions = append(f.sessions, s)
	return s, nil
}

func (f *fakeAPI) snapshot() fakeAPI {
	f.mu.Lock()
	defer f.mu.Unlock()
	return fakeAPI{
		created: append([]string(nil), f.created...), updated: append([]string(nil), f.updated...),
		deleted: append([]int(nil), f.deleted...), removed: append([]int64(nil), f.removed...),
		jits: append([]scaleset.RunnerScaleSetJitRunnerSetting(nil), f.jits...),
	}
}

// fakeSession is a message session whose first message can fail, and which otherwise blocks in
// GetMessage until its context ends, as a long poll does.
type fakeSession struct {
	mu       sync.Mutex
	assigned int
	getErr   error
	closed   bool
}

func (s *fakeSession) GetMessage(ctx context.Context, _, _ int) (*scaleset.RunnerScaleSetMessage, error) {
	s.mu.Lock()
	err := s.getErr
	s.mu.Unlock()
	if err != nil {
		return nil, err
	}
	<-ctx.Done()
	return nil, ctx.Err()
}

func (s *fakeSession) DeleteMessage(context.Context, int) error { return nil }

func (s *fakeSession) AcquireJobs(_ context.Context, ids []int64) ([]int64, error) { return ids, nil }

func (s *fakeSession) Session() scaleset.RunnerScaleSetSession {
	var sess scaleset.RunnerScaleSetSession
	sess.SessionID[0] = 1 // any non-nil id
	sess.Statistics = &scaleset.RunnerScaleSetStatistic{TotalAssignedJobs: s.assigned}
	return sess
}

func (s *fakeSession) Close(context.Context) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.closed = true
	return nil
}

func (s *fakeSession) isClosed() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.closed
}

// fakeSystemd keeps unit states in memory: start makes a unit active (at clock's time, its
// ActiveEnterTimestamp), stop inactive.
type fakeSystemd struct {
	mu         sync.Mutex
	states     map[string]string
	since      map[string]time.Time
	sinceErr   error
	sinceAsked []string
	clock      func() time.Time
	startErr   map[string]error
	started    []string
	stopped    []string
	onStop     func(unit string)
}

func newFakeSystemd() *fakeSystemd {
	return &fakeSystemd{states: map[string]string{}, since: map[string]time.Time{}, startErr: map[string]error{}}
}

func (s *fakeSystemd) Start(_ context.Context, unit string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.started = append(s.started, unit)
	if err := s.startErr[unit]; err != nil {
		s.states[unit] = "failed"
		return err
	}
	s.states[unit] = "active"
	if s.clock != nil {
		s.since[unit] = s.clock()
	}
	return nil
}

func (s *fakeSystemd) Stop(_ context.Context, unit string) error {
	s.mu.Lock()
	if s.onStop != nil {
		s.onStop(unit)
	}
	s.stopped = append(s.stopped, unit)
	s.states[unit] = "inactive"
	s.mu.Unlock()
	return nil
}

func (s *fakeSystemd) States(_ context.Context, units []string) (map[string]string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := map[string]string{}
	for _, u := range units {
		if st, ok := s.states[u]; ok {
			out[u] = st
		} else {
			out[u] = "inactive"
		}
	}
	return out, nil
}

func (s *fakeSystemd) ActiveSince(_ context.Context, units []string) (map[string]time.Time, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.sinceAsked = append(s.sinceAsked, units...)
	if s.sinceErr != nil {
		return nil, s.sinceErr
	}
	out := map[string]time.Time{}
	for _, u := range units {
		if t, ok := s.since[u]; ok {
			out[u] = t
		}
	}
	return out, nil
}

func (s *fakeSystemd) set(unit, state string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.states[unit] = state
}

func (s *fakeSystemd) calls() (started, stopped []string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.started...), append([]string(nil), s.stopped...)
}

// fakeHost reports a settable MemAvailable; Exists answers from overrides, else the filesystem.
type fakeHost struct {
	mu    sync.Mutex
	mem   uint64
	files map[string]bool
}

func (h *fakeHost) MemAvailable() (uint64, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.mem, nil
}

func (h *fakeHost) Exists(path string) bool {
	h.mu.Lock()
	v, ok := h.files[path]
	h.mu.Unlock()
	if ok {
		return v
	}
	_, err := os.Stat(path)
	return err == nil
}

// capture collects log lines.
type capture struct {
	mu    sync.Mutex
	lines []string
}

func (c *capture) logf(format string, args ...any) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.lines = append(c.lines, fmt.Sprintf(format, args...))
}

func (c *capture) has(sub string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, l := range c.lines {
		if strings.Contains(l, sub) {
			return true
		}
	}
	return false
}

func (c *capture) all() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return strings.Join(c.lines, "\n")
}

func orgConfig() *Config {
	return &Config{
		Name: "example-ci", Scope: "org", GitHubURL: "https://github.com/example", RunnerGroup: "example-group",
		App: AppConfig{ClientID: "123456", InstallationID: 77, KeyFile: "/etc/example-ci/app.pem"},
		Kinds: map[string]KindConfig{
			kindCI:  {SetName: "example-ci", Labels: []string{"self-hosted", "example-ci"}},
			kindQAE: {SetName: "example-qae", Labels: []string{"example-qae"}, RequiresFiles: []string{"/var/lib/example-ci/store/auth.json"}},
		},
		Budget:        BudgetConfig{Slots: 3, MinRunners: 0, QAEConcurrency: 1},
		Admission:     AdmissionConfig{ContainerMemoryBytes: 4 * gib, ReserveBytes: 2 * gib},
		Runner:        RunnerConfig{NamePrefix: "box-ci", WorkFolder: "/home/runner/_work"},
		IdleStopSec:   300,
		WarmMaxAgeSec: 1200,
	}
}

func userConfig() *Config {
	c := orgConfig()
	c.Scope, c.RunnerGroup = "user", "default"
	c.Repos = &ReposConfig{Exclude: []string{"fleet"}, RefreshSec: 600}
	return c
}

// harness is a lane over fakes, with a run dir in a temp directory and a settable clock.
type harness struct {
	lane *Lane
	sys  *fakeSystemd
	host *fakeHost
	apis map[string]*fakeAPI
	log  *capture
	run  string
	now  time.Time
	mu   sync.Mutex
}

func newHarness(t *testing.T, cfg *Config) *harness {
	t.Helper()
	h := &harness{
		sys: newFakeSystemd(), host: &fakeHost{mem: 64 * gib, files: map[string]bool{}},
		apis: map[string]*fakeAPI{}, log: &capture{}, run: t.TempDir(), now: t0,
	}
	nextID := 0
	newAPI := func(target string) (api, error) {
		h.mu.Lock()
		defer h.mu.Unlock()
		if a, ok := h.apis[target]; ok {
			return a, nil
		}
		a := &fakeAPI{target: target, nextID: &nextID, sets: map[string]*scaleset.RunnerScaleSet{}, groups: map[string]int{"example-group": 4}, nextRunner: 1000}
		h.apis[target] = a
		return a, nil
	}
	h.lane = newLane(cfg, h.run, h.sys, h.host, newAPI, h.log.logf)
	h.lane.now = func() time.Time {
		h.mu.Lock()
		defer h.mu.Unlock()
		return h.now
	}
	h.sys.clock = h.lane.now
	return h
}

func (h *harness) api(t *testing.T, target string) *fakeAPI {
	t.Helper()
	a, err := h.lane.apiFor(target)
	if err != nil {
		t.Fatal(err)
	}
	return a.(*fakeAPI)
}

func (h *harness) advance(d time.Duration) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.now = h.now.Add(d)
}

// settle reconciles and waits for the starts and stops it began.
func (h *harness) settle(t *testing.T) {
	t.Helper()
	h.lane.reconcile()
	if !h.lane.waitInflight(5 * time.Second) {
		t.Fatal("starts or stops did not finish")
	}
}

var errBoom = errors.New("boom")
