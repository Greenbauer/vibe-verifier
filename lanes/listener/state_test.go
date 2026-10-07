package main

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

func TestSlotFilesRoundTrip(t *testing.T) {
	run := t.TempDir()
	job := formatJobLine("example/repo-a", kindCI, 7, 4242, "example-ci-ci-2-1700000000")
	if err := writeSlotFiles(run, kindCI, 2, "ENCODED", job); err != nil {
		t.Fatal(err)
	}
	for name, mode := range map[string]os.FileMode{jitFile: 0o400, jobFile: 0o600} {
		info, err := os.Stat(filepath.Join(run, kindCI, "2", name))
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode().Perm() != mode {
			t.Errorf("%s mode %o, want %o", name, info.Mode().Perm(), mode)
		}
	}
	if info, _ := os.Stat(filepath.Join(run, kindCI, "2")); info.Mode().Perm() != 0o700 {
		t.Errorf("slot dir mode %o, want 700", info.Mode().Perm())
	}
	if got, _ := os.ReadFile(filepath.Join(run, kindCI, "2", jitFile)); string(got) != "ENCODED" {
		t.Errorf("jit = %q", got)
	}
	if got, _ := os.ReadFile(jobPath(run, kindCI, 2)); string(got) != "example/repo-a ci 7 4242 example-ci-ci-2-1700000000\n" {
		t.Errorf("job line = %q", got)
	}

	// A second write over a 0400 jit (a reused instance) replaces it.
	if err := writeSlotFiles(run, kindCI, 2, "SECOND", job); err != nil {
		t.Fatalf("rewrite over a read-only jit: %v", err)
	}

	recs := readJobFiles(run, []string{kindCI, kindQAE})
	if len(recs) != 1 {
		t.Fatalf("records = %+v", recs)
	}
	r := recs[0]
	if r.Err != nil || r.Kind != kindCI || r.Instance != 2 || r.Target != "example/repo-a" || r.SetID != 7 ||
		r.RunnerID != 4242 || r.RunnerName != "example-ci-ci-2-1700000000" || r.ModTime.IsZero() {
		t.Errorf("record = %+v", r)
	}

	if err := writeIdleStop(run, kindCI, 2); err != nil {
		t.Fatal(err)
	}
	if err := removeSlotFiles(run, kindCI, 2); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(run, kindCI, "2")); !os.IsNotExist(err) {
		t.Errorf("slot dir survived removal: %v", err)
	}
	if recs := readJobFiles(run, []string{kindCI}); len(recs) != 0 {
		t.Errorf("records after removal = %+v", recs)
	}
	if err := removeSlotFiles(run, kindCI, 2); err != nil {
		t.Errorf("removing an absent slot is not an error: %v", err)
	}
}

func TestReadJobFilesRebuildsTheRunDir(t *testing.T) {
	run := t.TempDir()
	write := func(rel, body string) {
		path := filepath.Join(run, rel)
		if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	write("qae/1/job", "example/repo-b qae 9 11 example-ci-qae-1-1700000001\n")
	write("ci/3/job", "example ci 1 12 example-ci-ci-3-1700000002\n")
	write("ci/1/job", "example ci 1 13 example-ci-ci-1-1700000003\n")
	write("ci/2/jit", "a jit with no job file is not a slot")
	write("ci/4/job", "not a job line\n")
	write("ci/5/job", "example qae 1 14 wrong-kind\n")
	write("ci/extra/job", "example ci 1 15 not-an-instance\n")
	write("other/1/job", "example other 1 16 not-a-configured-kind\n")

	recs := readJobFiles(run, []string{kindCI, kindQAE})
	var got []string
	for _, r := range recs {
		if r.Err != nil {
			got = append(got, r.Kind+"/"+strconv.Itoa(r.Instance)+" error")
			continue
		}
		got = append(got, r.Kind+"/"+strconv.Itoa(r.Instance)+" "+r.RunnerName)
	}
	want := []string{
		"ci/1 example-ci-ci-1-1700000003",
		"ci/3 example-ci-ci-3-1700000002",
		"ci/4 error",
		"ci/5 error",
		"qae/1 example-ci-qae-1-1700000001",
	}
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Errorf("records\n got %v\nwant %v", got, want)
	}
	if recs := readJobFiles(filepath.Join(run, "absent"), []string{kindCI}); len(recs) != 0 {
		t.Errorf("an absent run dir yields %v", recs)
	}
}

func TestParseJobLine(t *testing.T) {
	for _, bad := range []string{"", "a b c d", "t ci 0 1 n", "t ci 1 0 n", "t ci x 1 n", "t ci 1 1 n extra"} {
		if _, _, _, _, _, err := parseJobLine(bad); err == nil {
			t.Errorf("parseJobLine(%q) accepted", bad)
		}
	}
}

func TestParseMemAvailable(t *testing.T) {
	got, err := parseMemAvailable(strings.NewReader("MemTotal:       24030000 kB\nMemFree:  100 kB\nMemAvailable:   20110000 kB\n"))
	if err != nil || got != 20110000*1024 {
		t.Errorf("got %d, %v", got, err)
	}
	if _, err := parseMemAvailable(strings.NewReader("MemTotal: 1 kB\n")); err == nil {
		t.Error("a meminfo without MemAvailable was accepted")
	}
}

func TestHostFilesReadTheRealFiles(t *testing.T) {
	dir := t.TempDir()
	meminfo := filepath.Join(dir, "meminfo")
	if err := os.WriteFile(meminfo, []byte("MemTotal: 8 kB\nMemAvailable:       2 kB\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	h := hostFiles{meminfo: meminfo}
	if got, err := h.MemAvailable(); err != nil || got != 2048 {
		t.Errorf("MemAvailable = %d, %v", got, err)
	}
	if !h.Exists(meminfo) || h.Exists(filepath.Join(dir, "absent")) {
		t.Error("Exists misreports")
	}
	if _, err := (hostFiles{meminfo: filepath.Join(dir, "absent")}).MemAvailable(); err == nil {
		t.Error("an absent meminfo read as a value")
	}
}
