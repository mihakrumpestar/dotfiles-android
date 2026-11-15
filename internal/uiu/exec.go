package uiu

import (
	"bytes"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/mihakrumpestar/dotfiles-android/internal/debloat"
)

// dumpPath is where uiautomator writes on the device. One fixed name keeps the
// pull cheap and leaves no per-run files behind.
const dumpPath = "/sdcard/uiu-dump.xml"

// Run executes steps in order. Every relative pull `to` or push `from` resolves
// inside the recipe's own directory, so a step names a blob by bare file name and
// never repeats a path. Verify points the recipe at a temp directory instead.
//
// Every step that looks at the screen goes through snapshot, so the "dump
// immediately before acting" rule cannot be skipped by a new step kind.
func Run(d *debloat.Device, r Recipe, steps []Step) error {
	run := &runner{device: d, recipe: r}
	dir, err := os.MkdirTemp("", "uiu-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(dir)
	run.tmp = dir

	// The dumps are a scratch file on the device's shared storage; a failed run
	// must not leave one behind for a person to wonder about.
	defer d.Shell("rm -f " + dumpPath)

	// A resumed app lands on whatever page the last run left open, so the app is
	// stopped before the first step and every run starts from its root screen.
	d.Shell("am force-stop " + r.Package)
	// A file picker the app opened and the run never closed would otherwise own
	// the screen the first selector is resolved against.
	closeForeign(d, r.Package)
	time.Sleep(300 * time.Millisecond)

	if err := run.clearExports(steps); err != nil {
		return err
	}
	// The export is on the host now; leaving it on the device only makes the
	// next run's SAF picker ambiguous and the Download folder grow.
	defer run.clearExports(steps)

	for i, step := range steps {
		step.expand(r)
		if err := run.step(step); err != nil {
			return fmt.Errorf("step %d (%s): %w", i+1, step.Kind, err)
		}
	}
	return nil
}

// exportPaths lists the device-side paths a run reads or writes: a pull's
// source and a push's destination. A newest pull is skipped: its source is the
// app's own backup folder, which must not be deleted.
func exportPaths(steps []Step, r Recipe) []string {
	var paths []string
	for _, step := range steps {
		step.expand(r)
		switch step.Kind {
		case "pull":
			if !step.Newest {
				paths = append(paths, step.From)
			}
		case "push":
			paths = append(paths, step.To)
		}
	}
	return paths
}

// clearExports removes the device-side files a run is about to read or write.
// A stale export from an earlier run would otherwise be pulled and hashed as if
// it were fresh, and SAF appends " (1)" instead of overwriting, so the old file
// has to go for the recipe's own name to be the one that appears.
func (r *runner) clearExports(steps []Step) error {
	for _, path := range exportPaths(steps, r.recipe) {
		if !strings.HasPrefix(path, "/sdcard/") || strings.Contains(path, "..") {
			return fmt.Errorf("export path %q is not under /sdcard", path)
		}
		code, _, errOut := r.device.Shell("rm -f " + quote(path))
		if code != 0 {
			return fmt.Errorf("removing %s: %s", path, firstLine(errOut))
		}
	}
	return nil
}

type runner struct {
	device *debloat.Device
	recipe Recipe
	tmp    string
	// screen is read once, for scroll geometry, and only when needed.
	screen struct {
		width, height int
		known         bool
	}
}

func (r *runner) step(s Step) error {
	switch s.Kind {
	case "launch":
		return r.launch(s)
	case "tap":
		return r.tap(s)
	case "type":
		return r.typing(s)
	case "clear":
		return r.clear(s)
	case "wait":
		return r.wait(s)
	case "key":
		return r.key(s)
	case "scroll":
		return r.scroll(s)
	case "sleep":
		time.Sleep(seconds(s.Seconds))
		return nil
	case "pull":
		return r.pull(s)
	case "push":
		return r.push(s)
	case "capture":
		return r.capture(s)
	case "assert":
		return r.assert(s)
	default:
		return fmt.Errorf("unknown step %q", s.Kind)
	}
}

func (r *runner) launch(s Step) error {
	return Launch(r.device, s.Component, s.Clear)
}

func (r *runner) tap(s Step) error {
	nodes, _, err := r.snapshot()
	if err != nil {
		return err
	}
	node, err := Resolve(nodes, *s.Selector)
	if err != nil {
		if s.Optional && errors.Is(err, ErrNoMatch) {
			return nil
		}
		return err
	}
	target := HitTarget(nodes, node)
	x, y := target.Bounds.Centre()
	return tapPoint(r.device, x, y, s.Duration)
}

// tapPoint sends a tap, or a long press when duration is non-zero, to a point.
func tapPoint(d *debloat.Device, x, y, duration int) error {
	command := fmt.Sprintf("input tap %d %d", x, y)
	if duration > 0 {
		command = fmt.Sprintf("input swipe %d %d %d %d %d", x, y, x, y, duration)
	}
	code, _, errOut := d.Shell(command)
	if code != 0 {
		return fmt.Errorf("%s: %s", command, firstLine(errOut))
	}
	return nil
}

func (r *runner) typing(s Step) error {
	nodes, _, err := r.snapshot()
	if err != nil {
		return err
	}
	if s.Selector != nil {
		node, err := Resolve(nodes, *s.Selector)
		if err != nil {
			return err
		}
		x, y := HitTarget(nodes, node).Bounds.Centre()
		if err := tapPoint(r.device, x, y, 0); err != nil {
			return err
		}
	}
	// input text reads %s as a literal space; sh quoting keeps every other
	// character from becoming shell syntax.
	text := strings.ReplaceAll(s.Text, " ", "%s")
	code, _, errOut := r.device.Shell("input text " + quote(text))
	if code != 0 {
		return fmt.Errorf("input text: %s", firstLine(errOut))
	}
	return nil
}

func (r *runner) clear(s Step) error {
	nodes, _, err := r.snapshot()
	if err != nil {
		return err
	}
	node, err := Resolve(nodes, *s.Selector)
	if err != nil {
		return err
	}
	x, y := HitTarget(nodes, node).Bounds.Centre()
	if err := tapPoint(r.device, x, y, 0); err != nil {
		return err
	}

	// The tap may have opened the IME, which moves every coordinate on the
	// screen, so the text length comes from a dump taken after it.
	nodes, _, err = r.snapshot()
	if err != nil {
		return err
	}
	node, err = Resolve(nodes, *s.Selector)
	if err != nil {
		return err
	}
	if code, _, errOut := r.device.Shell("input keyevent KEYCODE_MOVE_END"); code != 0 {
		return fmt.Errorf("input keyevent: %s", firstLine(errOut))
	}

	// Deletes run in chunks because each `input keyevent` is a separate adb
	// round trip and the argument list has a length limit.
	remaining := len([]rune(node.Text))
	for remaining > 0 {
		batch := min(remaining, 32)
		codes := make([]string, batch)
		for i := range codes {
			codes[i] = "KEYCODE_DEL"
		}
		if code, _, errOut := r.device.Shell("input keyevent " + strings.Join(codes, " ")); code != 0 {
			return fmt.Errorf("input keyevent: %s", firstLine(errOut))
		}
		remaining -= batch
	}

	// The keyboard moves the field too, so re-dump before deciding it is empty.
	nodes, _, err = r.snapshot()
	if err != nil {
		return err
	}
	node, err = Resolve(nodes, *s.Selector)
	if err != nil {
		return err
	}
	if node.Text == "" {
		return nil
	}
	left := len([]rune(node.Text))
	codes := make([]string, left)
	for i := range codes {
		codes[i] = "KEYCODE_DEL"
	}
	if code, _, errOut := r.device.Shell("input keyevent " + strings.Join(codes, " ")); code != 0 {
		return fmt.Errorf("input keyevent: %s", firstLine(errOut))
	}
	nodes, _, err = r.snapshot()
	if err != nil {
		return err
	}
	node, err = Resolve(nodes, *s.Selector)
	if err != nil {
		return err
	}
	if node.Text != "" {
		return fmt.Errorf("field still holds %q after deleting %d characters", node.Text, left)
	}
	return nil
}

func (r *runner) wait(s Step) error {
	deadline := time.Now().Add(seconds(s.Timeout))
	var last error = errors.New("no dump yet")
	for {
		nodes, _, err := r.snapshot()
		if err == nil {
			if _, err := Resolve(nodes, *s.Selector); err == nil {
				return nil
			} else {
				last = err
			}
		} else {
			last = err
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("waited %.0fs: %w", s.Timeout, last)
		}
		time.Sleep(500 * time.Millisecond)
	}
}

func (r *runner) key(s Step) error {
	code, _, errOut := r.device.Shell("input keyevent " + s.Code)
	if code != 0 {
		return fmt.Errorf("input keyevent %s: %s", s.Code, firstLine(errOut))
	}
	return nil
}

func (r *runner) scroll(s Step) error {
	width, height, err := r.screenSize()
	if err != nil {
		return err
	}
	x := width / 2
	near, far := height*3/4, height/4
	start, end := near, far
	if s.Direction == "up" {
		start, end = far, near
	}
	for range s.Times {
		command := fmt.Sprintf("input swipe %d %d %d %d 300", x, start, x, end)
		if code, _, errOut := r.device.Shell(command); code != 0 {
			return fmt.Errorf("%s: %s", command, firstLine(errOut))
		}
		time.Sleep(time.Second)
	}
	return nil
}

// redactFile rewrites a pulled export in place with the recipe's redact fields
// zeroed. It runs on every pull, so a personal value cannot reach the repository
// through a later backup even after the artifact was cleaned by hand.
func redactFile(path string, fields []string) error {
	if len(fields) == 0 {
		return nil
	}
	body, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	redacted := Redact(body, fields)
	if bytes.Equal(redacted, body) {
		return nil
	}
	return os.WriteFile(path, redacted, 0o644)
}

func (r *runner) pull(s Step) error {
	local, err := r.localPath(s.To)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(local), 0o755); err != nil {
		return err
	}
	from := s.From
	if s.Newest {
		from, err = r.newest(s.From)
		if err != nil {
			return err
		}
	}
	if _, err := r.device.Run("pull", from, local); err != nil {
		return err
	}
	if err := redactFile(local, r.recipe.Redact); err != nil {
		return err
	}
	return splitFile(local, r.recipe)
}

// splitFile lifts the recipe's private values out of a pulled export. The
// artifact keeps the public half, which is the same bytes a redacted pull
// produces, and the overlay beside it keeps only the values that may not be
// committed, so a restore still has them.
func splitFile(path string, r Recipe) error {
	if len(r.Private) == 0 {
		return nil
	}
	body, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	public, private, err := splitExport(body, r.Private)
	if err != nil {
		return fmt.Errorf("%s: %w", path, err)
	}
	if err := os.WriteFile(path, public, 0o644); err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(r.PrivatePath()), 0o755); err != nil {
		return err
	}
	return os.WriteFile(r.PrivatePath(), private, 0o600)
}

// MergeFile returns an artifact with its private overlay put back. A restore's
// undo copy has to be this file: the artifact alone is missing whatever the
// recipe keeps out of the repository, so undoing with it would drop those entries
// from the phone.
func MergeFile(r Recipe) ([]byte, error) {
	public, err := os.ReadFile(r.ArtifactPath())
	if err != nil {
		return nil, err
	}
	if !r.IsPrivate() {
		return public, nil
	}
	overlay, err := os.ReadFile(r.PrivatePath())
	if err != nil {
		return nil, fmt.Errorf("reading %s: %w; the undo copy would be missing every private entry", r.PrivatePath(), err)
	}
	return mergeExport(public, overlay, r.Private)
}

// pushable is the file a push should send: the artifact with the private overlay
// merged back in, written to the run's temporary directory so the committed
// artifact is never the thing that carries the values. A recipe that names
// private fields must have the overlay, because pushing the zeroed half would
// reset the phone to blanks instead of restoring it.
func (r *runner) pushable(local string) (string, error) {
	if !r.recipe.IsPrivate() {
		return local, nil
	}
	overlay, err := os.ReadFile(r.recipe.PrivatePath())
	if err != nil {
		return "", fmt.Errorf("reading %s: %w; a recipe with private fields needs it beside the artifact", r.recipe.PrivatePath(), err)
	}
	public, err := os.ReadFile(local)
	if err != nil {
		return "", err
	}
	merged, err := mergeExport(public, overlay, r.recipe.Private)
	if err != nil {
		return "", fmt.Errorf("%s: %w", r.recipe.PrivatePath(), err)
	}
	out := filepath.Join(r.tmp, "push-"+filepath.Base(local))
	if err := os.WriteFile(out, merged, 0o600); err != nil {
		return "", err
	}
	return out, nil
}

// newest expands a device-side glob and returns its most recently modified
// match. An app that stamps a timestamp into every export name leaves a folder
// of equally valid exports, and the only useful one is the newest; the others
// are the app's own backups and are left alone.
func (r *runner) newest(pattern string) (string, error) {
	// The pattern reaches the device shell unquoted so it can expand, which is
	// only safe because validation restricts it to a glob over /sdcard.
	code, out, errOut := r.device.Shell("ls -t " + pattern + " | head -n 1")
	if code != 0 {
		return "", fmt.Errorf("listing %s: %s", pattern, firstLine(errOut))
	}
	path := strings.TrimSpace(out)
	if path == "" {
		return "", fmt.Errorf("no file matches %s", pattern)
	}
	if !strings.HasPrefix(path, "/sdcard/") {
		return "", fmt.Errorf("%s matched %q, which is not an absolute path under /sdcard", pattern, path)
	}
	return path, nil
}

func (r *runner) push(s Step) error {
	local, err := r.localPath(s.From)
	if err != nil {
		return err
	}
	local, err = r.pushable(local)
	if err != nil {
		return err
	}
	if _, err := r.device.Run("push", local, s.To); err != nil {
		return err
	}
	return nil
}

func (r *runner) capture(s Step) error {
	_, body, err := r.snapshot()
	if err != nil {
		return err
	}
	pretty, err := indentXML(body)
	if err != nil {
		return err
	}
	dir := filepath.Join(r.recipe.Dir, r.recipe.CaptureFolder())
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	path := filepath.Join(dir, s.Name+".xml")
	return os.WriteFile(path, pretty, 0o644)
}

func (r *runner) assert(s Step) error {
	nodes, _, err := r.snapshot()
	if err != nil {
		return err
	}
	got, err := Checked(nodes, *s.Selector)
	if err != nil {
		return err
	}
	if got != *s.Checked {
		return fmt.Errorf("checked is %t, want %t", got, *s.Checked)
	}
	return nil
}

// snapshot dumps the current screen and parses it. Every step that needs to
// know where something is calls this first: the screen has no stable moment, so
// coordinates from an older tap are stale by construction.
func (r *runner) snapshot() ([]Node, []byte, error) {
	return dumpInto(r.device, filepath.Join(r.tmp, "dump.xml"))
}

// Dump reads the device's current accessibility tree into memory. It is the same
// read every step performs, exposed so a recipe can be written against the tree
// the app actually renders instead of a guess at one.
func Dump(d *debloat.Device) ([]Node, error) {
	// The dump is scratch on shared storage; a probe must not leave one behind
	// for a person to wonder about.
	defer d.Shell("rm -f " + dumpPath)
	file, err := os.CreateTemp("", "uiu-dump-*.xml")
	if err != nil {
		return nil, err
	}
	path := file.Name()
	file.Close()
	defer os.Remove(path)
	nodes, _, err := dumpInto(d, path)
	return nodes, err
}

// Tap resolves one selector and taps the node a person's finger would hit. It
// waits for the selector, because a probe chain taps a screen the previous tap
// has only just opened and a cold app paints its tree several seconds after its
// window takes focus.
func Tap(d *debloat.Device, sel Selector) error {
	deadline := time.Now().Add(15 * time.Second)
	for {
		nodes, err := Dump(d)
		if err != nil {
			return err
		}
		node, err := Resolve(nodes, sel)
		if err == nil {
			x, y := HitTarget(nodes, node).Bounds.Centre()
			return tapPoint(d, x, y, 0)
		}
		if !errors.Is(err, ErrNoMatch) || time.Now().After(deadline) {
			return err
		}
		time.Sleep(400 * time.Millisecond)
	}
}

// Launch starts a component, stopping the app first when clear is set, and gives
// its window a moment to take focus. A probe chain uses it to reach the screen a
// recipe will start from.
func Launch(d *debloat.Device, component string, clear bool) error {
	pkg := component[:strings.Index(component, "/")]
	if clear {
		d.Shell("am force-stop " + pkg)
	}
	closeForeign(d, pkg)
	flag := ""
	if clear {
		flag = "-S "
	}
	code, out, errOut := d.Shell("am start " + flag + "-n " + component)
	if code != 0 {
		return fmt.Errorf("am start %s: %s", component, firstLine(out+errOut))
	}
	// Focus is one cheap dumpsys instead of a 2.5s UI dump. The wait is best
	// effort: an app that opens another window (a file picker) still counts as
	// launched, and the next read reports what is really there.
	for deadline := time.Now().Add(6 * time.Second); time.Now().Before(deadline); {
		if _, focused, _ := d.Shell("dumpsys window | grep -m1 mCurrentFocus"); strings.Contains(focused, pkg+"/") {
			break
		}
		time.Sleep(300 * time.Millisecond)
	}
	return nil
}

// Focused names the focused window's component, pkg/activity.
func Focused(d *debloat.Device) (string, error) {
	_, out, _ := d.Shell("dumpsys window | grep -m1 mCurrentFocus")
	line := strings.TrimSuffix(firstLine(out), "}")
	open := strings.LastIndex(line, "Window{")
	if open < 0 {
		return "", errors.New("no focused window")
	}
	fields := strings.Fields(line[open+len("Window{"):])
	if len(fields) == 0 {
		return "", errors.New("unreadable focused window")
	}
	return fields[len(fields)-1], nil
}

// FocusedPackage is Focused without the activity name.
func FocusedPackage(d *debloat.Device) (string, bool) {
	focus, err := Focused(d)
	if err != nil {
		return "", false
	}
	if index := strings.Index(focus, "/"); index >= 0 {
		focus = focus[:index]
	}
	return focus, focus != ""
}

// closeForeign backs out of a window another app left on top, which is how a file
// picker abandoned by an earlier run keeps the app being scripted from coming
// forward. It touches nothing else: a few Back presses are what a person does,
// and an app that ignores them loses nothing.
func closeForeign(d *debloat.Device, pkg string) {
	for range 3 {
		focus, ok := FocusedPackage(d)
		if !ok || focus == pkg {
			return
		}
		if code, _, _ := d.Shell("input keyevent KEYCODE_BACK"); code != 0 {
			return
		}
		time.Sleep(500 * time.Millisecond)
	}
}

// dumpInto pulls the hierarchy to local and returns the parsed nodes plus the raw
// bytes, which a capture keeps.
func dumpInto(d *debloat.Device, local string) ([]Node, []byte, error) {
	code, out, errOut := d.Shell("uiautomator dump " + dumpPath)
	if code != 0 {
		return nil, nil, fmt.Errorf("uiautomator dump: %s", firstLine(out+errOut))
	}
	if _, err := d.Run("pull", dumpPath, local); err != nil {
		return nil, nil, err
	}
	body, err := os.ReadFile(local)
	if err != nil {
		return nil, nil, err
	}
	nodes, err := ParseDump(body)
	if err != nil {
		return nil, nil, err
	}
	return nodes, body, nil
}

func (r *runner) localPath(path string) (string, error) {
	// The boundary is lexical: a committed symlink inside the data folder is
	// trusted, because recipes are repository policy, not untrusted input.
	if filepath.IsAbs(path) {
		return "", fmt.Errorf("%s is absolute; a recipe only names files in the app's own directory", path)
	}
	if r.recipe.BlobDir == "" {
		return "", fmt.Errorf("%s needs the app's blob directory", path)
	}
	return filepath.Join(r.recipe.BlobDir, path), nil
}

func (r *runner) screenSize() (int, int, error) {
	if r.screen.known {
		return r.screen.width, r.screen.height, nil
	}
	code, out, errOut := r.device.Shell("wm size")
	if code != 0 {
		return 0, 0, fmt.Errorf("wm size: %s", firstLine(errOut))
	}
	for _, line := range strings.Split(out, "\n") {
		_, size, ok := strings.Cut(strings.TrimSpace(line), ":")
		if !ok {
			continue
		}
		width, height, ok := strings.Cut(strings.TrimSpace(size), "x")
		if !ok {
			continue
		}
		w, errW := strconv.Atoi(width)
		h, errH := strconv.Atoi(height)
		if errW != nil || errH != nil {
			continue
		}
		r.screen.width, r.screen.height, r.screen.known = w, h, true
		return w, h, nil
	}
	return 0, 0, fmt.Errorf("cannot parse wm size output %q", strings.TrimSpace(out))
}

func seconds(value float64) time.Duration {
	return time.Duration(value * float64(time.Second))
}

// quote makes a value survive `adb shell`, which joins its arguments into one
// string the device's sh parses.
func quote(value string) string {
	if value != "" && !strings.ContainsAny(value, " \t\n\"'$`\\|&;<>()*?[]{}~!#%") {
		return value
	}
	return "'" + strings.ReplaceAll(value, "'", `'\''`) + "'"
}

func firstLine(text string) string {
	for _, line := range strings.Split(text, "\n") {
		if line = strings.TrimSpace(line); line != "" {
			return line
		}
	}
	return ""
}

// indentXML re-indents a dump so a capture is readable and diffable. The device
// writes the whole hierarchy on one line.
func indentXML(body []byte) ([]byte, error) {
	decoder := xml.NewDecoder(bytes.NewReader(body))
	var out bytes.Buffer
	encoder := xml.NewEncoder(&out)
	encoder.Indent("", "  ")
	for {
		token, err := decoder.Token()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("re-indenting dump: %w", err)
		}
		if err := encoder.EncodeToken(token); err != nil {
			return nil, err
		}
	}
	if err := encoder.Flush(); err != nil {
		return nil, err
	}
	return out.Bytes(), nil
}
