// Command lane-listener starts a lane's disposable CI slots on demand.
//
// It serves one lane from one JSON config (the one argument; README.md documents every key). It
// keeps one GitHub Actions runner scale set per kind (per repository for a user-scope lane),
// long-polls each set's message session, and when GitHub assigns a set more jobs than it has
// runners it mints a just-in-time runner, writes the slot's run-dir files and starts the kind's
// systemd slot unit, within one budget shared by every set (and the wait kind's own, when it is
// configured: jobs that only wait for another job's result). The slot unit runs the container and
// exits after its one job; this program never stops a busy slot. Linux-only by design: it drives
// systemd and reads /proc/meminfo, and runs as root on the box. With -delete-sets it instead
// deletes every scale set the config would serve and exits (the lane provisioner's --remove).
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/actions/scaleset"
)

// Config is the lane's JSON configuration. Unknown keys are refused, so a typo fails at start.
type Config struct {
	Name        string                `json:"name"`
	Scope       string                `json:"scope"`
	GitHubURL   string                `json:"github_url"`
	RunnerGroup string                `json:"runner_group"`
	App         AppConfig             `json:"app"`
	Kinds       map[string]KindConfig `json:"kinds"`
	Repos       *ReposConfig          `json:"repos"`
	Budget      BudgetConfig          `json:"budget"`
	Admission   AdmissionConfig       `json:"admission"`
	Runner      RunnerConfig          `json:"runner"`
	IdleStopSec int                   `json:"idle_stop_sec"`
	// WarmMaxAgeSec is how long a warm slot's unit may stay active before the slot is recycled
	// (defaultWarmMaxAgeSec when the key is absent). The slot unit's RuntimeMaxSec is this plus the
	// lane's job budget, so a job taken by a slot of any warm age keeps that whole budget.
	WarmMaxAgeSec int `json:"warm_max_age_sec"`
}

// AppConfig names the GitHub App the listener authenticates as. The key file is read once at
// start and never logged.
type AppConfig struct {
	ClientID       string `json:"client_id"`
	InstallationID int64  `json:"installation_id"`
	KeyFile        string `json:"key_file"`
}

// KindConfig is one kind of slot: the scale set its jobs come through and, optionally, one file per
// instance that must exist before that instance may start: instance n waits for RequiresFiles[n-1],
// and a kind that lists them uses no instance beyond the list. The qae kind lists one Codex login
// per instance, so each QAE job has a login of its own. The wait kind (and only it) instead carries
// a budget of its own: at most Slots of its slots at once, none counted against budget.slots, each
// container ContainerMemoryBytes for admission. Its jobs only wait for another job's result, so a
// queue of them can never hold the shared slots the awaited job needs.
type KindConfig struct {
	SetName              string   `json:"set_name"`
	Labels               []string `json:"labels"`
	RequiresFiles        []string `json:"requires_files"`
	Slots                int      `json:"slots"`
	ContainerMemoryBytes uint64   `json:"container_memory_bytes"`
}

// ReposConfig is the user scope's repository discovery.
type ReposConfig struct {
	Exclude    []string `json:"exclude"`
	RefreshSec int      `json:"refresh_sec"`
}

// BudgetConfig bounds the lane: slots is the most slot units running at once across every set
// and kind, qae_concurrency the most of the qae kind, min_runners the organisation ci set's warm
// pool.
type BudgetConfig struct {
	Slots          int `json:"slots"`
	MinRunners     int `json:"min_runners"`
	QAEConcurrency int `json:"qae_concurrency"`
}

// AdmissionConfig is the memory a start needs: MemAvailable must cover one container plus the
// reserve, per start.
type AdmissionConfig struct {
	ContainerMemoryBytes uint64 `json:"container_memory_bytes"`
	ReserveBytes         uint64 `json:"reserve_bytes"`
}

// RunnerConfig shapes the JIT runners: <name_prefix>-<kind>-<n>-<unix time>, and their work folder.
type RunnerConfig struct {
	NamePrefix string `json:"name_prefix"`
	WorkFolder string `json:"work_folder"`
}

const (
	kindCI   = "ci"
	kindQAE  = "qae"
	kindWait = "wait"
	// reconcileEvery re-plans the whole lane between messages. The budget is shared, so a slot
	// freed in one set must be able to start a job queued in another set whose listener is parked
	// in a long poll (about 50 s); the tick is also what ages idle slots and notices finished units.
	reconcileEvery = 5 * time.Second
	// shutdownWait bounds how long a stop signal waits for starts and stops already in flight.
	shutdownWait = 20 * time.Second
	// heartbeatEvery is how often an otherwise quiet listener logs a line: an idle lane logs
	// nothing else for hours, and the lane provisioner's --check reads the newest line's age.
	heartbeatEvery = time.Minute
	// defaultWarmMaxAgeSec is warm_max_age_sec when the config does not set it.
	defaultWarmMaxAgeSec = 1200
)

var (
	laneNamePattern   = regexp.MustCompile(`^[a-z][a-z0-9-]*$`)
	ownerURLPattern   = regexp.MustCompile(`^https://github\.com/([A-Za-z0-9][A-Za-z0-9-]*)$`)
	setNamePattern    = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*$`)
	labelPattern      = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*$`)
	namePrefixPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9-]*$`)
)

func loadConfig(path string) (*Config, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	dec := json.NewDecoder(f)
	dec.DisallowUnknownFields()
	c := Config{WarmMaxAgeSec: defaultWarmMaxAgeSec} // decoding keeps the default of an absent key
	if err := dec.Decode(&c); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	if err := c.validate(); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return &c, nil
}

func (c *Config) validate() error {
	var problems []string
	bad := func(format string, args ...any) { problems = append(problems, fmt.Sprintf(format, args...)) }

	if !laneNamePattern.MatchString(c.Name) {
		bad("name must match %s", laneNamePattern)
	}
	if c.Scope != "org" && c.Scope != "user" {
		bad("scope must be org or user")
	}
	if !ownerURLPattern.MatchString(c.GitHubURL) {
		bad("github_url must be https://github.com/<owner>")
	}
	if c.RunnerGroup == "" {
		bad("runner_group is required")
	}
	if c.Scope == "user" && c.RunnerGroup != scaleset.DefaultRunnerGroup {
		bad("a user-scope lane registers repository-level sets, which live in runner group %q", scaleset.DefaultRunnerGroup)
	}
	if c.App.ClientID == "" || c.App.InstallationID <= 0 || !filepath.IsAbs(c.App.KeyFile) {
		bad("app needs client_id, a positive installation_id and an absolute key_file")
	}
	c.validateKinds(bad)
	switch {
	case c.Scope == "user" && (c.Repos == nil || c.Repos.RefreshSec <= 0):
		bad("a user-scope lane needs repos.refresh_sec > 0")
	case c.Scope == "org" && c.Repos != nil:
		bad("repos is for a user-scope lane only")
	}
	b := c.Budget
	if b.Slots < 1 || b.MinRunners < 0 || b.MinRunners > b.Slots || b.QAEConcurrency < 0 || b.QAEConcurrency > b.Slots {
		bad("budget needs slots >= 1 and 0 <= min_runners, qae_concurrency <= slots")
	}
	if c.Scope == "user" && b.MinRunners != 0 {
		bad("budget.min_runners applies to the organisation ci set only; a user-scope lane must set 0")
	}
	if _, ok := c.Kinds[kindQAE]; ok && b.QAEConcurrency < 1 {
		bad("budget.qae_concurrency must be at least 1 when the qae kind is configured")
	}
	if c.Admission.ContainerMemoryBytes == 0 {
		bad("admission.container_memory_bytes is required")
	}
	if !namePrefixPattern.MatchString(c.Runner.NamePrefix) || c.Runner.WorkFolder == "" {
		bad("runner needs a name_prefix matching %s and a work_folder", namePrefixPattern)
	}
	if c.IdleStopSec <= 0 {
		bad("idle_stop_sec must be positive")
	}
	if c.WarmMaxAgeSec <= 0 {
		bad("warm_max_age_sec must be positive")
	}
	if len(problems) > 0 {
		return errors.New(strings.Join(problems, "; "))
	}
	return nil
}

func (c *Config) validateKinds(bad func(string, ...any)) {
	if _, ok := c.Kinds[kindCI]; !ok {
		bad("kinds.ci is required")
	}
	names := map[string]string{}
	for kind, k := range c.Kinds {
		if kind != kindCI && kind != kindQAE && kind != kindWait {
			bad("kinds may only be ci, qae and wait, not %q", kind)
			continue
		}
		if own := k.Slots != 0 || k.ContainerMemoryBytes != 0; own != (kind == kindWait) {
			bad("kinds.%s: slots and container_memory_bytes belong to the wait kind, which needs both", kind)
		} else if own && (k.Slots < 1 || k.ContainerMemoryBytes == 0) {
			bad("kinds.wait needs slots >= 1 and container_memory_bytes > 0")
		}
		if !setNamePattern.MatchString(k.SetName) {
			bad("kinds.%s.set_name must match %s", kind, setNamePattern)
		} else if other, dup := names[strings.ToLower(k.SetName)]; dup {
			bad("kinds.%s and kinds.%s share the set name %q", other, kind, k.SetName)
		}
		names[strings.ToLower(k.SetName)] = kind
		if len(k.Labels) == 0 {
			bad("kinds.%s.labels must not be empty", kind)
		}
		for _, label := range k.Labels {
			if !labelPattern.MatchString(label) {
				bad("kinds.%s.labels: %q must match %s", kind, label, labelPattern)
			}
		}
		for _, path := range k.RequiresFiles {
			if !filepath.IsAbs(path) {
				bad("kinds.%s.requires_files: %q must be absolute", kind, path)
			}
		}
		switch {
		case kind == kindQAE && len(k.RequiresFiles) > 0 && len(k.RequiresFiles) != c.Budget.QAEConcurrency:
			bad("kinds.qae.requires_files must list one file per qae instance: budget.qae_concurrency %d, not %d", c.Budget.QAEConcurrency, len(k.RequiresFiles))
		case len(k.RequiresFiles) > c.kindSlots(kind):
			bad("kinds.%s.requires_files lists more files than the kind has instances (%d)", kind, c.kindSlots(kind))
		}
	}
}

// instances is the instance range 1..n of a kind: one per required file when it lists them,
// otherwise every instance its budget allows.
func (c *Config) instances(kind string) int {
	if n := len(c.Kinds[kind].RequiresFiles); n > 0 {
		return n
	}
	return c.kindSlots(kind)
}

// owner is the organisation or user the lane serves, from github_url.
func (c *Config) owner() string { return ownerURLPattern.FindStringSubmatch(c.GitHubURL)[1] }

// kindNames lists the configured kinds in a fixed order.
func (c *Config) kindNames() []string {
	kinds := make([]string, 0, len(c.Kinds))
	for kind := range c.Kinds {
		kinds = append(kinds, kind)
	}
	sort.Strings(kinds)
	return kinds
}

// kindSlots is how many slots of a kind may hold an instance at once, so also its instance range
// 1..n and its sets' MaxRunners: the wait kind's own budget, budget.slots for every other kind.
func (c *Config) kindSlots(kind string) int {
	if n := c.Kinds[kind].Slots; n > 0 {
		return n
	}
	return c.Budget.Slots
}

// ownSlots maps each kind with a budget of its own (the wait kind) to that budget.
func (c *Config) ownSlots() map[string]int {
	own := map[string]int{}
	for kind, k := range c.Kinds {
		if k.Slots > 0 {
			own[kind] = k.Slots
		}
	}
	return own
}

// kindContainerBytes maps each kind whose container differs from admission's to its size.
func (c *Config) kindContainerBytes() map[string]uint64 {
	sizes := map[string]uint64{}
	for kind, k := range c.Kinds {
		if k.ContainerMemoryBytes > 0 {
			sizes[kind] = k.ContainerMemoryBytes
		}
	}
	return sizes
}

// minRunners is the warm pool of one set: the organisation lane's ci set only.
func (c *Config) minRunners(key SetKey) int {
	if c.Scope == "org" && key.Kind == kindCI {
		return c.Budget.MinRunners
	}
	return 0
}

// targetURL is the GitHub configuration URL of a scope target: the organisation itself, or
// owner/repo for a user-scope lane.
func (c *Config) targetURL(target string) string {
	if c.Scope == "org" {
		return c.GitHubURL
	}
	return "https://github.com/" + target
}

// unitName is the systemd slot unit of one kind's instance.
func unitName(lane, kind string, n int) string { return fmt.Sprintf("%s-%s@%d.service", lane, kind, n) }

// logger writes the lane's plain `<UTC stamp> <message>` lines to stdout.
type logger struct {
	mu  sync.Mutex
	w   io.Writer
	now func() time.Time
}

func (l *logger) printf(format string, args ...any) {
	l.mu.Lock()
	defer l.mu.Unlock()
	fmt.Fprintf(l.w, "%s %s\n", l.now().UTC().Format("2006-01-02T15:04:05Z"), fmt.Sprintf(format, args...))
}

func main() { os.Exit(run(os.Args[1:])) }

func run(args []string) int {
	log := &logger{w: os.Stdout, now: time.Now}
	flags := flag.NewFlagSet("lane-listener", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	deleteSets := flags.Bool("delete-sets", false, "delete every scale set the config would serve, then exit")
	if err := flags.Parse(args); err != nil || flags.NArg() != 1 {
		fmt.Fprintln(os.Stderr, "usage: lane-listener [-delete-sets] <config.json>")
		return 2
	}
	cfg, err := loadConfig(flags.Arg(0))
	if err != nil {
		log.printf("config refused: %v", err)
		return 2
	}
	key, err := os.ReadFile(cfg.App.KeyFile)
	if err != nil {
		log.printf("the App key could not be read: %v", err)
		return 2
	}
	host, err := os.Hostname()
	if err != nil || host == "" {
		host = cfg.Name + "-listener"
	}

	signals, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	if *deleteSets {
		return runDeleteSets(signals, cfg, key, log)
	}
	ctx, fail := context.WithCancelCause(signals)

	newAPI := scalesetClients(cfg, key)
	lane := newLane(cfg, filepath.Join("/run", cfg.Name), systemctl{bin: "systemctl"},
		hostFiles{meminfo: "/proc/meminfo"}, newAPI, log.printf)
	log.printf("lane %s (%s scope, %s): budget %d slots, qae %d, warm pool %d, wait %d of its own; idle stop after %ds, warm recycle after %ds",
		cfg.Name, cfg.Scope, cfg.GitHubURL, cfg.Budget.Slots, cfg.Budget.QAEConcurrency,
		cfg.Budget.MinRunners, cfg.Kinds[kindWait].Slots, cfg.IdleStopSec, cfg.WarmMaxAgeSec)
	lane.rebuild()

	mgr := newSetManager(cfg, lane, host, log.printf, fail)
	switch cfg.Scope {
	case "org":
		if err := mgr.ensureTarget(ctx, cfg.owner()); err != nil {
			fail(fmt.Errorf("the organisation's scale sets could not be ensured: %w", err))
		}
	case "user":
		app, err := newAppClient(cfg.App.ClientID, cfg.App.InstallationID, key)
		if err != nil {
			fail(err)
		} else {
			go mgr.userLoop(ctx, app, time.Duration(cfg.Repos.RefreshSec)*time.Second)
		}
	}

	ticker := time.NewTicker(reconcileEvery)
	heartbeat := time.NewTicker(heartbeatEvery)
	for ctx.Err() == nil {
		select {
		case <-ctx.Done():
		case <-ticker.C:
			lane.reconcile()
		case <-heartbeat.C:
			log.printf("%s", lane.heartbeat())
		}
	}
	ticker.Stop()
	heartbeat.Stop()
	mgr.stopAll()
	if !lane.waitInflight(shutdownWait) {
		log.printf("starts or stops still in flight after %s; exiting anyway (their files and units stay as they are)", shutdownWait)
	}
	code, cause := exitStatus(signals, ctx)
	if code != 0 {
		log.printf("stopping on error: %v", cause)
		return code
	}
	log.printf("stopped: sessions closed, scale sets and running slots left as they are")
	return 0
}

// scalesetClients builds the scale set client of one scope target, authenticated as the App.
func scalesetClients(cfg *Config, key []byte) func(target string) (api, error) {
	return func(target string) (api, error) {
		c, err := scaleset.NewClientWithGitHubApp(scaleset.ClientWithGitHubAppConfig{
			GitHubConfigURL: cfg.targetURL(target),
			GitHubAppAuth: scaleset.GitHubAppAuth{
				ClientID: cfg.App.ClientID, InstallationID: cfg.App.InstallationID, PrivateKey: string(key),
			},
		})
		if err != nil {
			return nil, err
		}
		return scalesetAPI{c}, nil
	}
}

// runDeleteSets is -delete-sets: it deletes every scale set the config would serve and exits 0,
// or 1 when any deletion failed. The lane provisioner's --remove runs it after stopping the
// listener, so no scale set outlives its lane.
func runDeleteSets(ctx context.Context, cfg *Config, key []byte, log *logger) int {
	targets := []string{cfg.owner()}
	if cfg.Scope == "user" {
		app, err := newAppClient(cfg.App.ClientID, cfg.App.InstallationID, key)
		if err != nil {
			log.printf("scale sets not deleted: %v", err)
			return 1
		}
		repos, err := app.repositories(ctx)
		if err != nil {
			log.printf("scale sets not deleted: the repository list is unreadable: %v", err)
			return 1
		}
		served, unserved := servedTargets(repos, cfg.owner(), cfg.Repos.Exclude)
		targets = append(served, unserved...)
	}
	if err := deleteLaneSets(ctx, cfg, scalesetClients(cfg, key), targets, log.printf); err != nil {
		log.printf("lane %s: scale sets not all deleted (each failure is logged above)", cfg.Name)
		return 1
	}
	log.printf("lane %s: every scale set it would serve is gone", cfg.Name)
	return 0
}

// exitStatus is 0 for a stop signal and 1, with the cause, when the lane stopped on its own
// error. The signal is checked first: signal.NotifyContext cancels with a cause of its own
// ("terminated signal received"), which is not context.Canceled.
func exitStatus(signals, ctx context.Context) (int, error) {
	if signals.Err() != nil {
		return 0, nil
	}
	if cause := context.Cause(ctx); cause != nil && !errors.Is(cause, context.Canceled) {
		return 1, cause
	}
	return 0, nil
}
