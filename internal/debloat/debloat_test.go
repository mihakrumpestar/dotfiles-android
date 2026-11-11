package debloat

import (
	"go/ast"
	"go/parser"
	"go/token"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"testing"
)

func writeFile(t *testing.T, path, body string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatalf("writing %s: %v", path, err)
	}
}

func readFile(t *testing.T, path string) string {
	t.Helper()
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("reading %s: %v", path, err)
	}
	return string(body)
}

func fileExists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

func TestTier(t *testing.T) {
	cases := map[string]string{
		"/system/priv-app/Foo/Foo.apk":         "system/priv-app",
		"/system/app/EasterEgg/EasterEgg.apk":  "system/app",
		"/data/app/~~abc==/pkg-xyz==/base.apk": "data/app",
		"/product/overlay/X.apk":               "product/overlay",
		"/apex/com.android.wifi@1":             "apex",
		"null":                                 "unknown",
		"":                                     "unknown",
		"/":                                    "unknown",
	}
	for path, want := range cases {
		if got := Tier(path); got != want {
			t.Errorf("Tier(%q) = %q, want %q", path, got, want)
		}
	}
}

func TestParseTiers(t *testing.T) {
	// Real device line: the code path contains `==`, so splitting at the first
	// `=` truncates the path and drops the package.
	const line = "package:/data/app/~~abc123def456==/" +
		"android.autoinstalls.config.samsung-001122334455==/base.apk=" +
		"android.autoinstalls.config.samsung"
	tests := []struct {
		name string
		in   string
		want map[string]string
	}{
		{"data app with ==", line, map[string]string{"android.autoinstalls.config.samsung": "data/app"}},
		{"system app", "package:/system/priv-app/Foo/Foo.apk=com.foo",
			map[string]string{"com.foo": "system/priv-app"}},
		{"apex", "package:/apex/com.android.runtime@1/apex.apk=com.android.runtime",
			map[string]string{"com.android.runtime": "apex"}},
		{"junk lines", "random\npackage:no-name\n", map[string]string{}},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := parseTiers(tc.in)
			if len(got) != len(tc.want) {
				t.Fatalf("got %v, want %v", got, tc.want)
			}
			for name, tier := range tc.want {
				if got[name] != tier {
					t.Errorf("tier[%q] = %q, want %q", name, got[name], tier)
				}
			}
		})
	}
}

func TestPackageRE(t *testing.T) {
	valid := []string{"com.android.egg", "android", "a_b.c"}
	for _, name := range valid {
		if !packageRE.MatchString(name) {
			t.Errorf("%q should be a valid package name", name)
		}
	}
	// Names reach `adb shell`, so anything with metacharacters must fail.
	invalid := []string{"com.a;reboot", "com.evil$(id)", "com.a b", "com.a|b", "com.a\nb"}
	for _, name := range invalid {
		if packageRE.MatchString(name) {
			t.Errorf("%q must be rejected", name)
		}
	}
}

func TestMutateRejectsDangerousName(t *testing.T) {
	device := &Device{Serial: "TEST", ADB: "true"}
	if _, _, err := device.Mutate("disable-user", "com.a; reboot"); err == nil {
		t.Fatal("expected an error for a shell-injection attempt")
	}
}

func TestSnapshotRoundTrip(t *testing.T) {
	dir := t.TempDir()
	states := map[string]string{
		"com.a": Enabled,
		"com.b": Disabled,
	}
	path, err := WriteSnapshot(dir, "SERIAL", states)
	if err != nil {
		t.Fatalf("writeSnapshot: %v", err)
	}

	snapshot, err := LoadSnapshot(path)
	if err != nil {
		t.Fatalf("LoadSnapshot: %v", err)
	}
	if snapshot.Serial != "SERIAL" {
		t.Errorf("serial = %q, want SERIAL", snapshot.Serial)
	}
	if snapshot.Packages["com.b"] != Disabled {
		t.Errorf("com.b state = %q, want disabled", snapshot.Packages["com.b"])
	}
	if got := snapshot.EnabledIn(); len(got) != 1 || got[0] != "com.a" {
		t.Errorf("EnabledIn = %v, want [com.a]", got)
	}
}

func TestNewestSnapshotPrefersNewest(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "snapshots"), 0o755); err != nil {
		t.Fatal(err)
	}
	older := filepath.Join(dir, "snapshots", "20200101T000000Z-S.json")
	newer := filepath.Join(dir, "snapshots", "20200102T000000Z-S.json")
	for _, path := range []string{older, newer} {
		if err := os.WriteFile(path, []byte("{}"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	got, err := NewestSnapshot(dir, "S")
	if err != nil {
		t.Fatalf("NewestSnapshot: %v", err)
	}
	if got != newer {
		t.Errorf("got %q, want %q", got, newer)
	}
}

func TestNewestSnapshotWithoutHistoryFailsLoudly(t *testing.T) {
	if _, err := NewestSnapshot(t.TempDir(), "S"); err == nil {
		t.Fatal("expected an error when no snapshots exist")
	}
}

func TestSnapshotDoesNotResurrectUserDisabledPackages(t *testing.T) {
	// A package disabled before the snapshot must stay disabled: restoring must
	// only undo what a debloat run did.
	snapshot := SnapshotFile{Packages: map[string]string{
		"com.enabled":  Enabled,
		"com.user-off": Disabled,
	}}
	enabled := snapshot.EnabledIn()
	if len(enabled) != 1 || enabled[0] != "com.enabled" {
		t.Fatalf("EnabledIn = %v, want [com.enabled]", enabled)
	}
}

func TestStateOfTreatsMissingKeyAsAbsent(t *testing.T) {
	// Go returns "" for a missing map key; Python's dict.get(pkg, ABSENT)
	// returned ABSENT. Preview output must not claim it would disable a package
	// the device does not have.
	states := map[string]string{"com.on": Enabled, "com.off": Disabled}
	if got := StateOf(states, "com.on"); got != Enabled {
		t.Errorf("StateOf(com.on) = %q", got)
	}
	if got := StateOf(states, "com.off"); got != Disabled {
		t.Errorf("StateOf(com.off) = %q", got)
	}
	if got := StateOf(states, "com.missing"); got != Absent {
		t.Errorf("StateOf(com.missing) = %q, want absent", got)
	}
	if got := StateOf(nil, "com.missing"); got != Absent {
		t.Errorf("StateOf(nil, com.missing) = %q, want absent", got)
	}
}

func TestExplainFindsTheReasonNotTheStackTail(t *testing.T) {
	// A refused disable prints a stack trace whose last line is always
	// "at android.os.Binder.execTransact(...)". Showing that as "the device's
	// message" tells nobody anything.
	refused := `Exception occurred while executing 'disable-user':
java.lang.SecurityException: [Theme] Cannot disable a protected package: com.samsung.android.themestore
	at com.android.server.wm.Task.m(Task.java:33)
	at android.os.Binder.execTransact(Binder.java:1359)`
	got := explain(refused)
	want := "java.lang.SecurityException: [Theme] Cannot disable a protected package: com.samsung.android.themestore"
	if got != want {
		t.Errorf("explain() = %q, want %q", got, want)
	}
}

func TestExplainFindsTheRootOnlyFailure(t *testing.T) {
	got := explain("Failure [only root can delete system app for a particular user]")
	if !strings.Contains(got, "only root can delete system app") {
		t.Errorf("explain() = %q", got)
	}
}

func TestExplainHandlesSuccessOutput(t *testing.T) {
	got := explain("Package com.android.egg new state: disabled-user\n")
	if got != "Package com.android.egg new state: disabled-user" {
		t.Errorf("explain() = %q", got)
	}
}

func TestExplainHandlesEmpty(t *testing.T) {
	if got := explain(""); got != "" {
		t.Errorf("explain(\"\") = %q, want empty", got)
	}
}

// sourceFiles returns every non-test .go file in the module.
func sourceFiles(t *testing.T) []string {
	t.Helper()
	root := filepath.Join("..", "..")
	var files []string
	err := filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() {
			if name := info.Name(); name == ".git" || name == ".devenv" || name == ".state" {
				return filepath.SkipDir
			}
			return nil
		}
		if strings.HasSuffix(path, ".go") && !strings.HasSuffix(path, "_test.go") {
			files = append(files, path)
		}
		return nil
	})
	if err != nil {
		t.Fatalf("walking module: %v", err)
	}
	if len(files) == 0 {
		t.Fatal("found no Go sources")
	}
	return files
}

// stringLiterals returns every string constant in a file.
func stringLiterals(t *testing.T, path string) []string {
	t.Helper()
	file, err := parser.ParseFile(token.NewFileSet(), path, nil, 0)
	if err != nil {
		t.Fatalf("parsing %s: %v", path, err)
	}
	var literals []string
	ast.Inspect(file, func(node ast.Node) bool {
		if literal, ok := node.(*ast.BasicLit); ok && literal.Kind == token.STRING {
			literals = append(literals, strings.Trim(literal.Value, "`\""))
		}
		return true
	})
	return literals
}

func TestNeverEmitsUninstallOrClear(t *testing.T) {
	// The exact incident class this toolchain exists to prevent. On Android 17
	// `pm uninstall --user 0` deletes /data/app APKs beyond the reach of
	// `cmd package install-existing`, and `pm clear` wipes data irreversibly.
	banned := regexp.MustCompile(`\buninstall\b|\bclear\b`)
	sawCommand := false
	for _, path := range sourceFiles(t) {
		for _, literal := range stringLiterals(t, path) {
			if strings.HasPrefix(literal, "pm ") {
				sawCommand = true
			}
			if banned.MatchString(literal) && strings.Contains(literal, "pm") {
				t.Errorf("%s emits a banned command: %q", path, literal)
			}
		}
	}
	if !sawCommand {
		t.Fatal("expected the toolchain to build pm commands")
	}
}

func TestOnlyDisableAndEnableVerbsExist(t *testing.T) {
	// Checked per file: a file that builds `pm` commands anywhere may not hold a
	// banned verb at all, which also catches a verb assembled into the command
	// later. Files like the UI recipes name steps "clear" and never touch pm, so
	// they are correctly out of scope.
	banned := map[string]bool{
		"uninstall": true, "clear": true, "hide": true, "block": true, "suspend": true,
	}
	for _, path := range sourceFiles(t) {
		literals := stringLiterals(t, path)
		buildsPM := false
		for _, literal := range literals {
			if slices.Contains(strings.Fields(literal), "pm") {
				buildsPM = true
				break
			}
		}
		if !buildsPM {
			continue
		}
		for _, literal := range literals {
			if banned[literal] {
				t.Errorf("%s references banned verb %q but builds pm commands", path, literal)
			}
		}
	}
}

func TestNoHardcodedVendorPackageIDs(t *testing.T) {
	// Selection is per device and per human. A baked-in list is the thing this
	// replaced, so a vendor package id in the source is a regression.
	vendor := regexp.MustCompile(`^com\.(samsung|sec|google|microsoft|android)\.[\w.]+$`)
	for _, path := range sourceFiles(t) {
		for _, literal := range stringLiterals(t, path) {
			if vendor.MatchString(literal) {
				t.Errorf("%s hardcodes %q", path, literal)
			}
		}
	}
}

func TestRatingsURLTracksABranchNotACommit(t *testing.T) {
	// A pinned commit silently ages. The ratings are an input to a human's
	// decision, so they must be current.
	if !strings.Contains(UADURL, "/"+UADBranch+"/") {
		t.Fatalf("UADURL = %q, want the %q branch", UADURL, UADBranch)
	}
	if len(UADBranch) == 40 {
		t.Fatal("UADBranch looks like a commit SHA; it must be a branch")
	}
	if !strings.HasSuffix(UADURL, "/resources/assets/uad_lists.json") {
		t.Fatalf("UADURL = %q, want the uad_lists.json asset", UADURL)
	}
}

func TestRatingsRevalidatesWithETag(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, dir+"/uad_lists.json", `{"com.a":{"removal":"Recommended"}}`)
	writeFile(t, dir+"/uad_lists.etag", "\"abc123\"\n")

	var gotIfNoneMatch string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotIfNoneMatch = r.Header.Get("If-None-Match")
		w.WriteHeader(http.StatusNotModified)
	}))
	defer server.Close()

	ratings, err := loadRatingsFrom(dir, server.URL)
	if err != nil {
		t.Fatalf("loadRatingsFrom: %v", err)
	}
	if gotIfNoneMatch != "\"abc123\"" {
		t.Errorf("If-None-Match = %q, want the stored etag", gotIfNoneMatch)
	}
	if ratings.Source != FreshnessRevalidated {
		t.Errorf("source = %q, want revalidated", ratings.Source)
	}
	if ratings.ByPackage["com.a"].Removal != "Recommended" {
		t.Error("cache was not used on a 304")
	}
}

func TestRatingsFallsBackToCacheWhenOffline(t *testing.T) {
	// Offline is not a reason to refuse to describe the device.
	dir := t.TempDir()
	writeFile(t, dir+"/uad_lists.json", `{"com.a":{"removal":"Oem"}}`)

	ratings, err := loadRatingsFrom(dir, "http://127.0.0.1:0/uad_lists.json")
	if err != nil {
		t.Fatalf("expected the cache to be used, got %v", err)
	}
	if ratings.Source != FreshnessCache {
		t.Errorf("source = %q, want cache", ratings.Source)
	}
	if ratings.ByPackage["com.a"].Removal != "Oem" {
		t.Error("cache was not used when offline")
	}
}

func TestRatingsFailsWithoutCacheOrNetwork(t *testing.T) {
	dir := t.TempDir()
	if _, err := loadRatingsFrom(dir, "http://127.0.0.1:0/uad_lists.json"); err == nil {
		t.Fatal("expected an error with no cache and no network")
	}
}

func TestRatingsFetchesAndCaches(t *testing.T) {
	dir := t.TempDir()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("ETag", `"fresh"`)
		_, _ = w.Write([]byte(`{"com.b":{"removal":"Advanced"}}`))
	}))
	defer server.Close()

	ratings, err := loadRatingsFrom(dir, server.URL)
	if err != nil {
		t.Fatalf("loadRatingsFrom: %v", err)
	}
	if ratings.Source != FreshnessFetched {
		t.Errorf("source = %q, want fetched", ratings.Source)
	}
	if ratings.ByPackage["com.b"].Removal != "Advanced" {
		t.Error("fetched body was not parsed")
	}
	if !fileExists(dir + "/uad_lists.json") {
		t.Error("response was not cached")
	}
	if got := readFile(t, dir+"/uad_lists.etag"); strings.TrimSpace(got) != `"fresh"` {
		t.Errorf("etag = %q, want %q", got, `"fresh"`)
	}
}

// The toolchain must never ship or expect a prebuilt binary. Go is not installed
// on the host; it only exists inside devenv, and `task` comes from that shell.
// A `go build` step would either fail outside devenv or leave a stale binary that
// silently diverges from the source.
func TestTaskfileUsesGoRunOnly(t *testing.T) {
	path := filepath.Join("..", "..", "Taskfile.yml")
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("reading Taskfile: %v", err)
	}
	text := string(body)

	if strings.Contains(text, "go build") {
		t.Error("Taskfile builds a binary; use `go run` so the toolchain stays inside devenv")
	}
	if strings.Contains(text, "BIN:") || strings.Contains(text, "./androidctl") {
		t.Error("Taskfile references a built binary; invoke `go run ./cmd/androidctl` instead")
	}
	// The app path lives in a `vars.APP` default, so assert the pieces rather
	// than a literal command line that templating would hide.
	if !strings.Contains(text, "go run {{.APP}}") {
		t.Error("tasks must invoke `go run` on the app path")
	}
	if !strings.Contains(text, "APP: ./cmd/androidctl") {
		t.Error("the APP var must still point at ./cmd/androidctl")
	}
}

func TestNoBuiltBinaryIsTracked(t *testing.T) {
	// A committed or lingering binary is the failure this prevents.
	root := filepath.Join("..", "..")
	for _, name := range []string{"androidctl", "bin"} {
		if _, err := os.Stat(filepath.Join(root, name)); err == nil {
			t.Errorf("%s exists; nothing should be built ahead of time", name)
		}
	}
}
