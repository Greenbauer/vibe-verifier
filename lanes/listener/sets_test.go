package main

import (
	"context"
	"errors"
	"reflect"
	"slices"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/actions/scaleset"
)

// stubSupervisors replaces the session supervisor with one that records which sets run and
// holds until its set is dropped or the manager stops.
type stubSupervisors struct {
	mu      sync.Mutex
	running map[SetKey]int
}

func (s *stubSupervisors) supervise(ctx context.Context, key SetKey, _ api, _ int) {
	s.mu.Lock()
	s.running[key]++
	s.mu.Unlock()
	<-ctx.Done()
	s.mu.Lock()
	s.running[key]--
	if s.running[key] == 0 {
		delete(s.running, key)
	}
	s.mu.Unlock()
}

func (s *stubSupervisors) keys() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []string
	for k := range s.running {
		out = append(out, k.String())
	}
	sort.Strings(out)
	return out
}

func managed(t *testing.T, cfg *Config) (*harness, *setManager, *stubSupervisors) {
	t.Helper()
	h := newHarness(t, cfg)
	m := newSetManager(cfg, h.lane, "box", h.log.logf, func(err error) { t.Errorf("fatal: %v", err) })
	stub := &stubSupervisors{running: map[SetKey]int{}}
	m.supervise = stub.supervise
	t.Cleanup(m.stopAll)
	return h, m, stub
}

func laneSets(h *harness) []string {
	h.lane.mu.Lock()
	defer h.lane.mu.Unlock()
	var out []string
	for k := range h.lane.sets {
		out = append(out, k.String())
	}
	sort.Strings(out)
	return out
}

func TestUserScopeSetsFollowTheRepositoryList(t *testing.T) {
	h, m, stub := managed(t, userConfig())
	ctx := context.Background()
	a, b, c := "example/repo-a", "example/repo-b", "example/repo-c"

	m.syncTargets(ctx, []string{a, b}, nil)
	want := []string{a + " ci", a + " qae", b + " ci", b + " qae"}
	if got := laneSets(h); !reflect.DeepEqual(got, want) {
		t.Fatalf("lane sets %v, want %v", got, want)
	}
	waitFor(t, func() bool { return reflect.DeepEqual(stub.keys(), want) }, "supervisors for a and b")
	for _, target := range []string{a, b} {
		snap := h.api(t, target).snapshot()
		if !reflect.DeepEqual(sorted(snap.created), []string{"example-ci", "example-qae"}) {
			t.Errorf("%s created %v", target, snap.created)
		}
		set, _ := h.api(t, target).GetRunnerScaleSet(ctx, 1, "example-ci")
		if set == nil || set.RunnerGroupID != 1 || !set.RunnerSetting.DisableUpdate ||
			!reflect.DeepEqual(set.Labels, []scaleset.Label{{Name: "self-hosted"}, {Name: "example-ci"}}) {
			t.Errorf("%s ci set %+v", target, set)
		}
	}

	// repo-a leaves the list (archived, excluded, made public or gone); repo-c arrives.
	m.syncTargets(ctx, []string{b, c}, nil)
	want = []string{b + " ci", b + " qae", c + " ci", c + " qae"}
	if got := laneSets(h); !reflect.DeepEqual(got, want) {
		t.Fatalf("lane sets %v, want %v", got, want)
	}
	waitFor(t, func() bool { return reflect.DeepEqual(stub.keys(), want) }, "supervisors for b and c only")
	if snap := h.api(t, a).snapshot(); len(snap.deleted) != 2 {
		t.Errorf("repo-a's sets deleted %v, want both", snap.deleted)
	}
	if left, _ := h.api(t, a).GetRunnerScaleSet(ctx, 1, "example-ci"); left != nil {
		t.Errorf("repo-a still has %+v", left)
	}
	if snap := h.api(t, b).snapshot(); len(snap.created) != 2 || len(snap.deleted) != 0 {
		t.Errorf("repo-b was touched again: created %v deleted %v", snap.created, snap.deleted)
	}

	// Stopping closes every session and deletes no set.
	m.stopAll()
	waitFor(t, func() bool { return len(stub.keys()) == 0 }, "every supervisor stopped")
	if snap := h.api(t, b).snapshot(); len(snap.deleted) != 0 {
		t.Errorf("stopping deleted sets: %v", snap.deleted)
	}
}

func TestSweepDeletesThisLanesSetsOnUnservedRepositories(t *testing.T) {
	h, m, _ := managed(t, userConfig())
	ctx := context.Background()
	site := h.api(t, "example/site")
	if _, err := site.CreateRunnerScaleSet(ctx, &scaleset.RunnerScaleSet{Name: "example-ci", RunnerGroupID: 1}); err != nil {
		t.Fatal(err)
	}
	if _, err := site.CreateRunnerScaleSet(ctx, &scaleset.RunnerScaleSet{Name: "someone-elses", RunnerGroupID: 1}); err != nil {
		t.Fatal(err)
	}
	m.syncTargets(ctx, nil, []string{"example/site"})
	if left, _ := site.GetRunnerScaleSet(ctx, 1, "example-ci"); left != nil {
		t.Errorf("the lane's set on an unserved repository survived the sweep")
	}
	if other, _ := site.GetRunnerScaleSet(ctx, 1, "someone-elses"); other == nil {
		t.Errorf("the sweep deleted a set that is not the lane's")
	}
}

func TestEnsureSetUpdatesDifferingLabelsOnly(t *testing.T) {
	h := newHarness(t, orgConfig())
	a := h.api(t, "example")
	ctx := context.Background()
	if _, err := a.CreateRunnerScaleSet(ctx, &scaleset.RunnerScaleSet{
		Name: "example-ci", RunnerGroupID: 4, Labels: []scaleset.Label{{Name: "Self-Hosted"}, {Name: "EXAMPLE-CI"}},
	}); err != nil {
		t.Fatal(err)
	}
	if _, did, err := ensureSet(ctx, a, 4, "example-ci", []string{"self-hosted", "example-ci"}); err != nil || did != "" {
		t.Errorf("labels equal but for case: did %q, %v", did, err)
	}
	set, did, err := ensureSet(ctx, a, 4, "example-ci", []string{"self-hosted", "example-ci", "extra"})
	if err != nil || did != "labels updated" || len(set.Labels) != 3 || set.ID != 1 {
		t.Errorf("differing labels: %+v %q %v", set, did, err)
	}
}

func TestOrgScopeUsesTheNamedRunnerGroup(t *testing.T) {
	h, m, _ := managed(t, orgConfig())
	if err := m.ensureTarget(context.Background(), "example"); err != nil {
		t.Fatal(err)
	}
	set, _ := h.api(t, "example").GetRunnerScaleSet(context.Background(), 4, "example-ci")
	if set == nil {
		t.Fatal("no set in the named group (id 4)")
	}
	if !h.log.has(`scale set "example-ci" (id`) || !h.log.has("created for example ci: runner group 4") {
		t.Errorf("log:\n%s", h.log.all())
	}
	cfg := orgConfig()
	cfg.RunnerGroup = "missing-group"
	_, m2, _ := managed(t, cfg)
	if err := m2.ensureTarget(context.Background(), "example"); err == nil || !strings.Contains(err.Error(), "missing-group") {
		t.Errorf("an absent runner group: %v", err)
	}
}

// TestOrgScopeServesTheQAEKindInTheSameGroup: an org lane may declare a qae kind; its set lives in
// the lane's runner group beside the ci set, with its own labels, and the warm pool is the ci
// set's alone.
func TestOrgScopeServesTheQAEKindInTheSameGroup(t *testing.T) {
	cfg := orgConfig()
	cfg.Budget.MinRunners = 1
	cfg.Kinds[kindQAE] = KindConfig{SetName: "example-qae", Labels: []string{"example-qae"}, RequiresFiles: []string{"/var/lib/example-ci/codex/auth.json"}}
	if err := cfg.validate(); err != nil {
		t.Fatalf("an org lane with a qae kind is refused: %v", err)
	}
	h, m, stub := managed(t, cfg)
	if err := m.ensureTarget(context.Background(), "example"); err != nil {
		t.Fatal(err)
	}
	want := []string{"example ci", "example qae"}
	if got := laneSets(h); !reflect.DeepEqual(got, want) {
		t.Fatalf("lane sets %v, want %v", got, want)
	}
	waitFor(t, func() bool { return reflect.DeepEqual(stub.keys(), want) }, "a supervisor per kind")
	set, _ := h.api(t, "example").GetRunnerScaleSet(context.Background(), 4, "example-qae")
	if set == nil || !reflect.DeepEqual(set.Labels, []scaleset.Label{{Name: "example-qae"}}) {
		t.Fatalf("qae set in group 4: %+v", set)
	}
	if ci, qae := cfg.minRunners(SetKey{"example", kindCI}), cfg.minRunners(SetKey{"example", kindQAE}); ci != 1 || qae != 0 {
		t.Errorf("warm pool: ci %d, qae %d; want 1 and 0", ci, qae)
	}
}

func TestOpenSessionWaitsOutAStaleSessionThenGivesUp(t *testing.T) {
	h, m, _ := managed(t, orgConfig())
	m.retryFirst, m.retryMax, m.sessionWindow = time.Millisecond, 2*time.Millisecond, time.Second
	a := h.api(t, "example")
	conflict := errors.New(`request POST sessions failed(status="409 Conflict"): TaskAgentSessionConflictException`)
	a.openErrs = []error{conflict, conflict}
	sess, err := m.openSession(context.Background(), SetKey{"example", kindCI}, a, 41)
	if err != nil || sess == nil {
		t.Fatalf("after two conflicts: %v", err)
	}
	if !h.log.has("retrying in 1ms, until 1s after the first try") || !h.log.has(`status="409 Conflict"`) {
		t.Errorf("log:\n%s", h.log.all())
	}

	m.sessionWindow = 20 * time.Millisecond
	a.openErrs = slices.Repeat([]error{conflict}, 1000)
	if _, err := m.openSession(context.Background(), SetKey{"example", kindCI}, a, 41); err == nil ||
		!strings.Contains(err.Error(), "no message session within 20ms") {
		t.Errorf("a set that never gets a session: %v", err)
	}
}

func waitConfig() *Config {
	c := orgConfig()
	c.Kinds[kindWait] = KindConfig{SetName: "example-wait", Labels: []string{"self-hosted", "example-wait"}, Slots: 8, ContainerMemoryBytes: gib}
	return c
}

func TestConfigValidation(t *testing.T) {
	good := []*Config{orgConfig(), userConfig(), waitConfig()}
	for _, c := range good {
		if err := c.validate(); err != nil {
			t.Errorf("%s config refused: %v", c.Scope, err)
		}
	}
	bad := map[string]func(c *Config){
		"user scope in a named group":  func(c *Config) { c.Scope, c.Repos = "user", &ReposConfig{RefreshSec: 600} },
		"a warm pool for a user lane":  func(c *Config) { *c = *userConfig(); c.Budget.MinRunners = 1 },
		"repos on an org lane":         func(c *Config) { c.Repos = &ReposConfig{RefreshSec: 600} },
		"no ci kind":                   func(c *Config) { delete(c.Kinds, kindCI) },
		"an unknown kind":              func(c *Config) { c.Kinds["gpu"] = KindConfig{SetName: "g", Labels: []string{"g"}} },
		"a shared set name":            func(c *Config) { c.Kinds[kindQAE] = KindConfig{SetName: "EXAMPLE-CI", Labels: []string{"x"}} },
		"qae with no concurrency":      func(c *Config) { c.Budget.QAEConcurrency = 0 },
		"min_runners above the budget": func(c *Config) { c.Budget.MinRunners = 4 },
		"a deeper github_url":          func(c *Config) { c.GitHubURL = "https://github.com/example/repo" },
		"a relative key file":          func(c *Config) { c.App.KeyFile = "app.pem" },
		"no idle stop":                 func(c *Config) { c.IdleStopSec = 0 },
		"a wait kind with no slots": func(c *Config) {
			*c = *waitConfig()
			c.Kinds[kindWait] = KindConfig{SetName: "example-wait", Labels: []string{"example-wait"}, ContainerMemoryBytes: gib}
		},
		"a wait kind with no container size": func(c *Config) {
			*c = *waitConfig()
			c.Kinds[kindWait] = KindConfig{SetName: "example-wait", Labels: []string{"example-wait"}, Slots: 8}
		},
		"a ci kind with a budget of its own": func(c *Config) {
			c.Kinds[kindCI] = KindConfig{SetName: "example-ci", Labels: []string{"example-ci"}, Slots: 2, ContainerMemoryBytes: gib}
		},
		"a qae container size": func(c *Config) {
			c.Kinds[kindQAE] = KindConfig{SetName: "example-qae", Labels: []string{"example-qae"}, ContainerMemoryBytes: gib}
		},
		"a relative required file": func(c *Config) {
			c.Kinds[kindQAE] = KindConfig{SetName: "example-qae", Labels: []string{"example-qae"}, RequiresFiles: []string{"codex/auth.json"}}
		},
		"qae required files not one per qae instance": func(c *Config) {
			c.Kinds[kindQAE] = KindConfig{SetName: "example-qae", Labels: []string{"example-qae"}, RequiresFiles: []string{"/a/auth.json", "/b/auth.json"}}
		},
		"more required files than slots": func(c *Config) {
			c.Kinds[kindCI] = KindConfig{SetName: "example-ci", Labels: []string{"example-ci"}, RequiresFiles: []string{"/a", "/b", "/c", "/d"}}
		},
		"more wait required files than its own slots": func(c *Config) {
			*c = *waitConfig()
			c.Kinds[kindWait] = KindConfig{SetName: "example-wait", Labels: []string{"example-wait"}, Slots: 1, ContainerMemoryBytes: gib, RequiresFiles: []string{"/a", "/b"}}
		},
	}
	for name, mutate := range bad {
		c := orgConfig()
		mutate(c)
		if err := c.validate(); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func waitFor(t *testing.T, cond func() bool, what string) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(2 * time.Millisecond)
	}
}

func sorted(in []string) []string {
	out := append([]string(nil), in...)
	sort.Strings(out)
	return out
}

// TestUserLoopServesTheInstallationsPrivateRepositories runs the user scope's loop against the
// test GitHub server: the first listing creates sets for the private, non-archived, non-excluded
// repositories only, and sweeps this lane's leftover set off a public one.
func TestUserLoopServesTheInstallationsPrivateRepositories(t *testing.T) {
	h, m, stub := managed(t, userConfig())
	app, _ := testApp(t, []installationRepo{
		repo("app", true, false), repo("site", false, false), repo("old", true, true), repo("fleet", true, false),
	})
	site := h.api(t, "example/site")
	if _, err := site.CreateRunnerScaleSet(context.Background(), &scaleset.RunnerScaleSet{Name: "example-ci", RunnerGroupID: 1}); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		m.userLoop(ctx, app, time.Hour)
		close(done)
	}()
	want := []string{"example/app ci", "example/app qae"}
	waitFor(t, func() bool { return reflect.DeepEqual(stub.keys(), want) }, "supervisors for the one served repository")
	waitFor(t, func() bool { left, _ := site.GetRunnerScaleSet(ctx, 1, "example-ci"); return left == nil }, "the public repository's set swept")
	cancel()
	<-done
	for _, target := range []string{"example/old", "example/fleet"} {
		if created := h.api(t, target).snapshot().created; len(created) != 0 {
			t.Errorf("%s got sets %v", target, created)
		}
	}
}

func TestAFailedSetDeletionIsRetriedByTheNextListing(t *testing.T) {
	h, m, _ := managed(t, userConfig())
	ctx := context.Background()
	a := "example/repo-a"
	m.syncTargets(ctx, []string{a}, nil)
	fa := h.api(t, a)
	fa.mu.Lock()
	fa.deleteErr = errBoom
	fa.mu.Unlock()
	m.syncTargets(ctx, nil, []string{a}) // made public: its sets must go, but GitHub fails the delete
	if left, _ := fa.GetRunnerScaleSet(ctx, 1, "example-ci"); left == nil {
		t.Fatal("the fake did not fail the delete")
	}
	fa.mu.Lock()
	fa.deleteErr = nil
	fa.mu.Unlock()
	m.syncTargets(ctx, nil, []string{a})
	for _, name := range []string{"example-ci", "example-qae"} {
		if left, _ := fa.GetRunnerScaleSet(ctx, 1, name); left != nil {
			t.Errorf("set %q survived the retry", name)
		}
	}
}

func TestDeleteLaneSetsRemovesEveryKindOnEveryTarget(t *testing.T) {
	ctx := context.Background()
	t.Run("org", func(t *testing.T) {
		h := newHarness(t, orgConfig())
		org := h.api(t, "example")
		for _, name := range []string{"example-ci", "example-qae", "someone-elses"} {
			if _, err := org.CreateRunnerScaleSet(ctx, &scaleset.RunnerScaleSet{Name: name, RunnerGroupID: 4}); err != nil {
				t.Fatal(err)
			}
		}
		if err := deleteLaneSets(ctx, h.lane.cfg, h.lane.apiFor, []string{"example"}, h.log.logf); err != nil {
			t.Fatalf("delete: %v", err)
		}
		for _, name := range []string{"example-ci", "example-qae"} {
			if left, _ := org.GetRunnerScaleSet(ctx, 4, name); left != nil {
				t.Errorf("%s survived", name)
			}
		}
		if other, _ := org.GetRunnerScaleSet(ctx, 4, "someone-elses"); other == nil {
			t.Errorf("a set that is not the lane's was deleted")
		}
		// Again, with nothing left: not an error.
		if err := deleteLaneSets(ctx, h.lane.cfg, h.lane.apiFor, []string{"example"}, h.log.logf); err != nil {
			t.Errorf("a second delete: %v", err)
		}
		if !h.log.has(`scale set "example-ci" of example ci: none to delete`) {
			t.Errorf("log:\n%s", h.log.all())
		}
	})
	t.Run("user", func(t *testing.T) {
		h := newHarness(t, userConfig())
		a, site := h.api(t, "example/repo-a"), h.api(t, "example/site")
		for _, api := range []*fakeAPI{a, site} {
			if _, err := api.CreateRunnerScaleSet(ctx, &scaleset.RunnerScaleSet{Name: "example-ci", RunnerGroupID: 1}); err != nil {
				t.Fatal(err)
			}
		}
		site.deleteErr = errBoom
		err := deleteLaneSets(ctx, h.lane.cfg, h.lane.apiFor, []string{"example/site", "example/repo-a"}, h.log.logf)
		if err == nil || !strings.Contains(err.Error(), "example/site ci") {
			t.Fatalf("a failed deletion must be returned, got %v", err)
		}
		if left, _ := a.GetRunnerScaleSet(ctx, 1, "example-ci"); left != nil {
			t.Errorf("one target's failure stopped the next target's deletion")
		}
		if !h.log.has("not deleted: boom") {
			t.Errorf("the failure was not logged:\n%s", h.log.all())
		}
	})
}
