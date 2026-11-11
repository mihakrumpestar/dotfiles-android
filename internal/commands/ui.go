package commands

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/mihakrumpestar/dotfiles-android/internal/debloat"
	"github.com/mihakrumpestar/dotfiles-android/internal/uiu"
)

// unlockable wakes the device and dismisses the keyguard. Every ui command
// needs the screen unlocked before its first step, and none of them may fail
// when the screen happens to be unlocked already.
type unlockable struct {
	device *debloat.Device
}

// unlock wakes the screen and dismisses the keyguard, and does nothing more
// when the device is already unlocked. `wm dismiss-keyguard` is not used: this
// ROM ignores it.
func (u unlockable) unlock() error {
	// The panel is woken first, unconditionally: a dozing device reports
	// isKeyguardShowing=true and returns the last frame from every dump, so the
	// keyguard flag alone cannot say whether the screen is usable.
	if err := u.wake(); err != nil {
		return err
	}
	showing, err := u.keyguardShowing()
	if err != nil {
		return err
	}
	if !showing {
		return nil
	}
	// The swipe is dropped while the panel is still ramping up, so each attempt
	// wakes, swipes, then re-reads the flag instead of assuming either landed.
	// The power key is deliberately not used: it would blank a screen that is
	// merely slow, and this layer must never lock a device.
	for range 4 {
		if code, _, errOut := u.device.Shell("input keyevent 224"); code != 0 {
			return fmt.Errorf("waking screen: %s", firstLine(errOut))
		}
		time.Sleep(800 * time.Millisecond)
		if code, _, errOut := u.device.Shell("input swipe 540 1900 540 600 200"); code != 0 {
			return fmt.Errorf("dismissing keyguard: %s", firstLine(errOut))
		}
		time.Sleep(1500 * time.Millisecond)
		showing, err = u.keyguardShowing()
		if err != nil {
			return err
		}
		if !showing {
			return nil
		}
	}
	// A credential prompt cannot be swept away; saying so is more useful than
	// repeating a swipe that will never work.
	if locked, _ := u.secureLocked(); locked {
		return errors.New("the device has a secure lock screen; unlock it by hand and re-run")
	}
	return errors.New("keyguard is still showing after 4 attempts")
}

// wake presses the wake key until the power service reports Awake. Wakefulness
// is the signal that answers while the panel is dark: `dumpsys display` can
// report ON for an always-on display that shows no app, and prints nothing at
// all when the panel is truly off.
func (u unlockable) wake() error {
	for range 15 {
		if u.awake() {
			return nil
		}
		if code, _, errOut := u.device.Shell("input keyevent 224"); code != 0 {
			return fmt.Errorf("waking screen: %s", firstLine(errOut))
		}
		time.Sleep(700 * time.Millisecond)
	}
	return errors.New("the screen did not turn on; wake the device and re-run")
}

// awake reports whether the power service considers the device awake.
func (u unlockable) awake() bool {
	_, out, _ := u.device.Shell("dumpsys power | grep -m1 mWakefulness=")
	return strings.Contains(out, "mWakefulness=Awake")
}

// secureLocked reports whether the keyguard is showing its credential prompt,
// which a swipe cannot dismiss.
func (u unlockable) secureLocked() (bool, error) {
	const path = "/sdcard/uiu-lock.xml"
	defer func() { u.device.Shell("rm -f " + path) }()
	code, _, errOut := u.device.Shell("uiautomator dump " + path + " >/dev/null && grep -q passwordEntry " + path)
	if code != 0 && firstLine(errOut) != "" {
		return false, fmt.Errorf("reading lock screen: %s", firstLine(errOut))
	}
	return code == 0, nil
}

// keyguardShowing reads the window manager's own flag. `grep -m1` stops
// dumpsys from writing 100k lines into a closed pipe.
func (u unlockable) keyguardShowing() (bool, error) {
	code, out, errOut := u.device.Shell("dumpsys window | grep -m1 isKeyguardShowing")
	if code != 0 {
		return false, fmt.Errorf("reading keyguard state: %s", firstLine(errOut))
	}
	return strings.Contains(out, "isKeyguardShowing=true"), nil
}

func firstLine(text string) string {
	for _, line := range strings.Split(text, "\n") {
		if line = strings.TrimSpace(line); line != "" {
			return line
		}
	}
	return ""
}

// UICmd groups the declarative UI recipe commands.
type UICmd struct {
	List    UIListCmd    `cmd:"" help:"List app recipes and whether each one has a stored export."`
	Backup  UIBackupCmd  `cmd:"" help:"Run a recipe's backup steps and report the artifact's hash."`
	Restore UIRestoreCmd `cmd:"" help:"Restore a stored artifact, guarded against overwriting unexported state."`
	Verify  UIVerifyCmd  `cmd:"" help:"Export to a temporary file and compare it with the stored artifact."`
}

// appsFlag names the tree every ui subcommand reads.
type appsFlag struct {
	Apps string `help:"Directory holding one folder per app, each with steps.json." default:"policy/data" type:"path"`
}

// recipeFlags add the undo location, which only restore writes.
type recipeFlags struct {
	appsFlag
	State string `help:"Directory for undo copies." default:".state" type:"path"`
}

// load finds the app's folder by package id and reads its steps.json. The id
// lives inside the file rather than in its name, so every folder is scanned;
// the folder name is the app's display name and carries no identity of its own.
//
// A recipe that does not parse is fatal here: the duplicate check below is only
// sound when every file was read.
func (f appsFlag) load(app string) (uiu.Recipe, error) {
	if !debloat.ValidPackage(app) {
		return uiu.Recipe{}, fmt.Errorf("not a package name: %q", app)
	}
	recipes, errs := f.all()
	if len(errs) > 0 {
		return uiu.Recipe{}, errs[0]
	}
	var found []uiu.Recipe
	for _, recipe := range recipes {
		if recipe.Package == app {
			found = append(found, recipe)
		}
	}
	switch len(found) {
	case 0:
		return uiu.Recipe{}, fmt.Errorf("no recipe declares package %q under %s", app, f.Apps)
	case 1:
		return found[0], nil
	default:
		var names []string
		for _, recipe := range found {
			names = append(names, recipe.Dir)
		}
		return uiu.Recipe{}, fmt.Errorf("package %q is declared by more than one recipe: %s", app, strings.Join(names, ", "))
	}
}

// all parses every recipe in the tree. A broken recipe is reported rather than
// hidden, and the rest are still returned, so listing the tree stays useful
// while one file is being edited.
func (f appsFlag) all() ([]uiu.Recipe, []error) {
	paths, err := filepath.Glob(filepath.Join(f.Apps, "*", uiu.StepsFile))
	if err != nil {
		return nil, []error{err}
	}
	if len(paths) == 0 {
		return nil, []error{fmt.Errorf("no recipes under %s", filepath.Join(f.Apps, "*", uiu.StepsFile))}
	}
	slices.Sort(paths)
	var recipes []uiu.Recipe
	var errs []error
	for _, path := range paths {
		recipe, err := uiu.Parse(path)
		if err != nil {
			errs = append(errs, err)
			continue
		}
		recipes = append(recipes, recipe)
	}
	return recipes, errs
}

// UIListCmd lists every recipe.
type UIListCmd struct {
	appsFlag
}

// Run implements ui list. It reads only the tree, so it answers without a
// device: what a person wants to know here is what is declared, not what is
// plugged in.
func (c *UIListCmd) Run(_ *Globals) error {
	recipes, errs := c.all()
	for _, recipe := range recipes {
		state := "missing"
		if recipe.Artifact != "" {
			if info, err := os.Stat(recipe.ArtifactPath()); err == nil && !info.IsDir() {
				state = "stored"
			}
			if hasCapture(recipe) {
				state += " +captures"
			}
		} else if hasCapture(recipe) {
			state = "captured"
		} else {
			state = "no captures"
		}
		fmt.Fprintf(stdout, "%-24s %-32s %-20s %s\n", recipe.Name(), recipe.Package, orArtifact(recipe.Artifact), state)
	}
	for _, err := range errs {
		fmt.Fprintf(stdout, "error    %v\n", err)
	}
	if len(errs) > 0 {
		return fmt.Errorf("%d recipe(s) did not parse", len(errs))
	}
	return nil
}

// hasCapture reports whether the recipe's capture folder holds a screen dump.
func hasCapture(recipe uiu.Recipe) bool {
	entries, err := os.ReadDir(filepath.Join(recipe.Dir, recipe.CaptureFolder()))
	if err != nil {
		return false
	}
	for _, entry := range entries {
		if entry.Type().IsRegular() {
			return true
		}
	}
	return false
}

// orArtifact prints "-" for a capture-only recipe, which has no artifact file.
func orArtifact(artifact string) string {
	if artifact == "" {
		return "-"
	}
	return artifact
}

// UIBackupCmd exports the device's current state.
type UIBackupCmd struct {
	appsFlag
	App string `help:"Package id of the recipe to run." required:""`
}

// Run implements ui backup.
func (c *UIBackupCmd) Run(global *Globals) error {
	recipe, err := c.load(c.App)
	if err != nil {
		return err
	}
	device, err := global.Device()
	if err != nil {
		return err
	}
	if err := (unlockable{device}).unlock(); err != nil {
		return err
	}

	if recipe.Artifact == "" {
		// A capture-only recipe has no artifact to hash; the captures are the
		// output, and they are what a later review diffs.
		if err := uiu.Run(device, recipe, recipe.Backup); err != nil {
			return err
		}
		fmt.Fprintf(stdout, "captured %s\n", filepath.Join(recipe.Dir, recipe.CaptureFolder()))
		return nil
	}

	stored := recipe.ArtifactPath()
	started := time.Now()
	if err := uiu.Run(device, recipe, recipe.Backup); err != nil {
		return err
	}
	info, err := os.Stat(stored)
	if err != nil {
		return fmt.Errorf("the recipe finished but %s did not appear: %w", stored, err)
	}
	// A pull that never ran leaves the previous artifact in place, so the
	// modification time is what proves this run produced the file. This holds
	// because `adb pull` does not use -a, which would preserve the device mtime.
	if info.ModTime().Before(started.Add(-time.Second)) {
		return fmt.Errorf("%s was not written by this run (modtime %s)", stored, info.ModTime().Format(time.RFC3339))
	}

	// The digest is reported, not stored: it is a function of the artifact, so
	// writing it down would only create a second copy that can go stale.
	digest, err := hashFile(recipe, stored)
	if err != nil {
		return err
	}
	fmt.Fprintf(stdout, "artifact %s\nsha256   %s\n", stored, digest)
	return nil
}

// UIVerifyCmd compares a fresh export with the stored artifact.
type UIVerifyCmd struct {
	appsFlag
	App string `help:"Package id of the recipe to run." required:""`
}

// Run implements ui verify.
func (c *UIVerifyCmd) Run(global *Globals) error {
	recipe, err := c.load(c.App)
	if err != nil {
		return err
	}
	if recipe.Artifact == "" {
		return fmt.Errorf("%s is capture-only: there is no artifact to verify", recipe.Package)
	}
	device, err := global.Device()
	if err != nil {
		return err
	}
	if err := (unlockable{device}).unlock(); err != nil {
		return err
	}

	want, err := storedHash(recipe)
	if err != nil {
		return err
	}
	got, path, cleanup, err := exportTemp(device, recipe)
	if err != nil {
		return err
	}
	defer cleanup()

	if got == want {
		fmt.Fprintf(stdout, "same     %s %s\n", recipe.Package, want)
		return nil
	}
	fmt.Fprintf(stdout, "drift    %s stored %s, device %s\n", recipe.Package, want, got)
	fmt.Fprint(stdout, diffArtifacts(recipe, path))
	return fmt.Errorf("%s differs from the stored artifact", recipe.Package)
}

// UIRestoreCmd writes a stored artifact back through the app's import UI.
type UIRestoreCmd struct {
	recipeFlags
	App   string `help:"Package id of the recipe to run." required:""`
	Force bool   `help:"Restore even when the device state differs from the stored artifact."`
}

// Run implements ui restore.
func (c *UIRestoreCmd) Run(global *Globals) error {
	recipe, err := c.load(c.App)
	if err != nil {
		return err
	}
	if recipe.Artifact == "" || len(recipe.Restore) == 0 {
		return fmt.Errorf("%s has no restore flow", recipe.Package)
	}
	device, err := global.Device()
	if err != nil {
		return err
	}
	if err := (unlockable{device}).unlock(); err != nil {
		return err
	}

	want, err := storedHash(recipe)
	if err != nil {
		return err
	}
	stored := recipe.ArtifactPath()
	if _, err := os.Stat(stored); err != nil {
		return fmt.Errorf("stored artifact: %w", err)
	}
	got, path, cleanup, err := exportTemp(device, recipe)
	if err != nil {
		return err
	}
	defer cleanup()

	if got == want {
		fmt.Fprintf(stdout, "already matches, nothing to restore (%s %s)\n", recipe.Package, want)
		return nil
	}

	fmt.Fprintf(stdout, "drift    %s stored %s, device %s\n", recipe.Package, want, got)
	fmt.Fprint(stdout, diffArtifacts(recipe, path))

	// The undo tree is keyed on the package id, not the folder name: a folder
	// can be renamed or reused, and a restore copy has to stay attributable.
	undo := filepath.Join(c.State, "ui", recipe.Package,
		time.Now().UTC().Format("20060102T150405Z")+"-pre-restore"+filepath.Ext(stored))
	if err := copyFile(path, undo); err != nil {
		return err
	}
	fmt.Fprintf(stdout, "undo copy: %s\n", undo)

	if !c.Force {
		return fmt.Errorf("%s differs from the stored artifact; re-run with --force to overwrite it (undo copy: %s)", recipe.Package, undo)
	}

	if err := uiu.Run(device, recipe, recipe.Restore); err != nil {
		return err
	}
	fmt.Fprintf(stdout, "restored %s from %s\n", recipe.Package, stored)
	return nil
}

// exportTemp runs the backup steps against a temporary artifact name in an empty
// temporary directory, and returns the hash of that export plus its local path.
// The name keeps the original extension so the app's own file filter still
// shows it, and the directory is empty so nothing from the app's stored folder
// can be mistaken for this run's output.
func exportTemp(d *debloat.Device, recipe uiu.Recipe) (digest, path string, cleanup func(), err error) {
	dir, err := os.MkdirTemp("", "uiu-verify-")
	if err != nil {
		return "", "", nil, err
	}
	cleanup = func() { os.RemoveAll(dir) }

	temp := recipe.WithDir(dir).WithArtifact(tempArtifact(recipe.Artifact))
	if err := uiu.Run(d, temp, temp.Backup); err != nil {
		cleanup()
		return "", "", nil, err
	}
	path = temp.ArtifactPath()
	digest, err = hashFile(temp, path)
	if err != nil {
		cleanup()
		return "", "", nil, err
	}
	return digest, path, cleanup, nil
}

// tempArtifact keeps the extension and adds a marked stem, e.g.
// yamlauncher.verify-20261110T173000.json.
func tempArtifact(artifact string) string {
	ext := filepath.Ext(artifact)
	stem := strings.TrimSuffix(artifact, ext)
	stamp := time.Now().UTC().Format("20060102T150405")
	return fmt.Sprintf("%s.verify-%s%s", stem, stamp, ext)
}

// storedHash hashes the stored artifact now. Nothing is cached: the artifact is
// the only truth, so a hand-edited artifact is simply the new baseline and a
// stale digest file cannot disagree with it.
func storedHash(recipe uiu.Recipe) (string, error) {
	path := recipe.ArtifactPath()
	if _, err := os.Stat(path); err != nil {
		return "", fmt.Errorf("stored artifact (run `androidctl ui backup --app %s` first): %w", recipe.Package, err)
	}
	return hashFile(recipe, path)
}

// hashFile hashes the artifact's canonical form: a hash of the raw bytes would
// call every export drift, because apps stamp an export time into the file.
func hashFile(recipe uiu.Recipe, path string) (string, error) {
	body, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(uiu.Canonical(body, recipe.Volatile))
	return hex.EncodeToString(sum[:]), nil
}

// diffArtifacts renders the meaningful difference between the stored artifact
// and a fresh export, with volatile fields masked so the timestamp noise is not
// what the reader sees. A binary artifact gets no diff: a line diff of bytes
// would be noise, and the hashes already say they differ.
func diffArtifacts(recipe uiu.Recipe, fresh string) string {
	stored, err := os.ReadFile(recipe.ArtifactPath())
	if err != nil {
		return ""
	}
	body, err := os.ReadFile(fresh)
	if err != nil {
		return ""
	}
	if !isText(stored) || !isText(body) {
		return ""
	}
	old := uiu.Canonical(stored, recipe.Volatile)
	new := uiu.Canonical(body, recipe.Volatile)
	return uiu.UnifiedDiff(recipe.Artifact, old, new)
}

// isText keeps a diff of a zip or an image from reaching the terminal. UTF-8
// validity is enough here: every artifact this tool exports is JSON or a text
// format, and neither contains a NUL byte.
func isText(body []byte) bool {
	return utf8.Valid(body) && !bytes.ContainsRune(body, 0)
}

// copyFile writes a new undo copy and refuses to replace an existing one: an
// undo copy is the only record of what the device held before a forced restore.
func copyFile(from, to string) error {
	body, err := os.ReadFile(from)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(to), 0o755); err != nil {
		return err
	}
	file, err := os.OpenFile(to, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return fmt.Errorf("writing %s: %w", to, err)
	}
	if _, err := file.Write(body); err != nil {
		file.Close()
		return fmt.Errorf("writing %s: %w", to, err)
	}
	return file.Close()
}
