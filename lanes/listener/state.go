package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

// The run-dir contract (README.md): the listener writes, per started slot,
//
//	<run>/<kind>/<n>/jit         0400  the encoded JIT runner config; the slot mounts it into the container
//	<run>/<kind>/<n>/job         0600  "<scope-target> <kind> <set-id> <runner-id> <runner-name>\n"
//	<run>/<kind>/<n>/assignment  0600  the job GitHub reported started, written by JobStarted
//	<run>/<kind>/<n>/idle-stop         written before an idle stop removes the runner
//
// An instance is free when it has no job file and its unit is inactive. The assignment file does
// not hold the instance: the slot script never reads it, and removes only jit, idle-stop and job
// before rmdir. The listener removes the assignment with those files, and again once the unit is
// inactive and the job file is already gone, so the directory does not stay behind.
const (
	jitFile        = "jit"
	jobFile        = "job"
	assignmentFile = "assignment"
	idleStopFile   = "idle-stop"
	maxJobName     = 240
	maxAssignment  = 4096
)

var (
	assignmentOwner = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9-]{0,38}$`)
	assignmentRepo  = regexp.MustCompile(`^[A-Za-z0-9._-]{1,100}$`)
	assignmentJobID = regexp.MustCompile(`^[1-9][0-9]{0,19}$`)
)

// jobAssignment is what JobStarted records for the dashboard. run_id and job_id are included only
// when both can build a GitHub job link; the five-field job line is a different file.
type jobAssignment struct {
	Repository string `json:"repository"`
	Name       string `json:"name"`
	RunID      int64  `json:"run_id,omitempty"`
	JobID      string `json:"job_id,omitempty"`
}

func slotDir(runDir, kind string, n int) string {
	return filepath.Join(runDir, kind, strconv.Itoa(n))
}

func jobPath(runDir, kind string, n int) string {
	return filepath.Join(slotDir(runDir, kind, n), jobFile)
}

// JobRecord is one job file found under the run dir. Err is set when the file is unreadable or
// malformed; the instance is still occupied until its unit is inactive.
type JobRecord struct {
	Kind       string
	Instance   int
	Target     string
	SetID      int
	RunnerID   int64
	RunnerName string
	ModTime    time.Time
	Err        error
}

func formatJobLine(target, kind string, setID int, runnerID int64, runnerName string) string {
	return fmt.Sprintf("%s %s %d %d %s\n", target, kind, setID, runnerID, runnerName)
}

func parseJobLine(line string) (target, kind string, setID int, runnerID int64, runnerName string, err error) {
	fields := strings.Fields(line)
	if len(fields) != 5 {
		return "", "", 0, 0, "", fmt.Errorf("want 5 fields, got %d", len(fields))
	}
	if setID, err = strconv.Atoi(fields[2]); err != nil || setID <= 0 {
		return "", "", 0, 0, "", fmt.Errorf("bad set id %q", fields[2])
	}
	if runnerID, err = strconv.ParseInt(fields[3], 10, 64); err != nil || runnerID <= 0 {
		return "", "", 0, 0, "", fmt.Errorf("bad runner id %q", fields[3])
	}
	return fields[0], fields[1], setID, runnerID, fields[4], nil
}

// readJobFiles lists the job files of the configured kinds, ordered by kind and instance. Only
// numeric instance directories count; anything else under the run dir is not the listener's.
func readJobFiles(runDir string, kinds []string) []JobRecord {
	var records []JobRecord
	for _, kind := range kinds {
		entries, err := os.ReadDir(filepath.Join(runDir, kind))
		if err != nil {
			continue // no slot of this kind was ever started (or the run dir is gone at reboot)
		}
		for _, entry := range entries {
			n, err := strconv.Atoi(entry.Name())
			if !entry.IsDir() || err != nil || n < 1 {
				continue
			}
			path := jobPath(runDir, kind, n)
			info, err := os.Stat(path)
			if errors.Is(err, os.ErrNotExist) {
				continue
			}
			rec := JobRecord{Kind: kind, Instance: n}
			if err == nil {
				rec.ModTime = info.ModTime()
				rec.Err = parseJobFile(path, &rec)
			} else {
				rec.Err = err
			}
			records = append(records, rec)
		}
	}
	sort.Slice(records, func(i, j int) bool {
		if records[i].Kind != records[j].Kind {
			return records[i].Kind < records[j].Kind
		}
		return records[i].Instance < records[j].Instance
	})
	return records
}

func parseJobFile(path string, rec *JobRecord) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	target, kind, setID, runnerID, name, err := parseJobLine(string(data))
	if err != nil {
		return fmt.Errorf("%s: %w", path, err)
	}
	if kind != rec.Kind {
		return fmt.Errorf("%s: names kind %q under the %s directory", path, kind, rec.Kind)
	}
	rec.Target, rec.SetID, rec.RunnerID, rec.RunnerName = target, setID, runnerID, name
	return nil
}

// writeSlotFiles writes the jit (0400) then the job file (0600) of one instance, each through a
// temporary file renamed into place, so the slot never reads half a file.
func writeSlotFiles(runDir, kind string, n int, jit, job string) error {
	dir := slotDir(runDir, kind, n)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	if err := writeFileAtomic(filepath.Join(dir, jitFile), []byte(jit), 0o400); err != nil {
		return err
	}
	return writeFileAtomic(filepath.Join(dir, jobFile), []byte(job), 0o600)
}

func writeFileAtomic(path string, data []byte, mode os.FileMode) error {
	tmp := path + ".tmp"
	_ = os.Remove(tmp)
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_EXCL, mode)
	if err != nil {
		return err
	}
	_, err = f.Write(data)
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	if err == nil {
		err = os.Chmod(tmp, mode) // exact mode whatever the umask
	}
	if err == nil {
		err = os.Rename(tmp, path)
	}
	if err != nil {
		_ = os.Remove(tmp)
	}
	return err
}

// assignmentRecord is the assignment file body, or ok false when the name or repository cannot be
// shown. A refused record is not written; the page then uses its GitHub match.
func assignmentRecord(owner, repository, name string, runID int64, jobID string) ([]byte, bool) {
	if !assignmentOwner.MatchString(owner) || !assignmentRepo.MatchString(repository) {
		return nil, false
	}
	if name == "" || utf8.RuneCountInString(name) > maxJobName || len(name) > maxAssignment {
		return nil, false
	}
	for _, r := range name {
		if r < 0x20 || r == 0x7f {
			return nil, false
		}
	}
	rec := jobAssignment{Repository: owner + "/" + repository, Name: name}
	if runID > 0 && assignmentJobID.MatchString(jobID) {
		rec.RunID, rec.JobID = runID, jobID
	}
	data, err := json.Marshal(rec)
	if err != nil || len(data)+1 > maxAssignment {
		return nil, false
	}
	return append(data, '\n'), true
}

func writeAssignment(runDir, kind string, n int, body []byte) error {
	return writeFileAtomic(filepath.Join(slotDir(runDir, kind, n), assignmentFile), body, 0o600)
}

// removeSlotFiles removes one instance's files, the job file last because its absence is what
// frees the instance, then the directory if nothing else is in it.
func removeSlotFiles(runDir, kind string, n int) error {
	dir := slotDir(runDir, kind, n)
	var errs []error
	names := []string{
		jitFile, jitFile + ".tmp", idleStopFile,
		assignmentFile, assignmentFile + ".tmp",
		jobFile + ".tmp", jobFile,
	}
	for _, name := range names {
		if err := os.Remove(filepath.Join(dir, name)); err != nil && !errors.Is(err, os.ErrNotExist) {
			errs = append(errs, err)
		}
	}
	_ = os.Remove(dir)
	return errors.Join(errs...)
}

// removeAssignment removes a job record the slot script does not know about, then the directory
// when that was the last file. Absent files are not an error.
func removeAssignment(runDir, kind string, n int) error {
	dir := slotDir(runDir, kind, n)
	var errs []error
	for _, name := range []string{assignmentFile, assignmentFile + ".tmp"} {
		if err := os.Remove(filepath.Join(dir, name)); err != nil && !errors.Is(err, os.ErrNotExist) {
			errs = append(errs, err)
		}
	}
	_ = os.Remove(dir)
	return errors.Join(errs...)
}

func writeIdleStop(runDir, kind string, n int) error {
	return os.WriteFile(filepath.Join(slotDir(runDir, kind, n), idleStopFile), nil, 0o600)
}

func removeIdleStop(runDir, kind string, n int) error {
	err := os.Remove(filepath.Join(slotDir(runDir, kind, n), idleStopFile))
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}

// hostProbe is what the lane reads from the host besides systemd; tests replace it.
type hostProbe interface {
	MemAvailable() (uint64, error)
	Exists(path string) bool
}

type hostFiles struct{ meminfo string }

func (h hostFiles) MemAvailable() (uint64, error) {
	f, err := os.Open(h.meminfo)
	if err != nil {
		return 0, err
	}
	defer f.Close()
	return parseMemAvailable(f)
}

func (hostFiles) Exists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

// parseMemAvailable reads MemAvailable from /proc/meminfo (reported in kB) as bytes.
func parseMemAvailable(r io.Reader) (uint64, error) {
	scanner := bufio.NewScanner(r)
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) == 3 && fields[0] == "MemAvailable:" && fields[2] == "kB" {
			kb, err := strconv.ParseUint(fields[1], 10, 64)
			if err != nil {
				return 0, fmt.Errorf("MemAvailable %q: %w", fields[1], err)
			}
			return kb * 1024, nil
		}
	}
	if err := scanner.Err(); err != nil {
		return 0, err
	}
	return 0, errors.New("no MemAvailable line")
}
