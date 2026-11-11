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
// source and a push's destination.
func exportPaths(steps []Step, r Recipe) []string {
	var paths []string
	for _, step := range steps {
		step.expand(r)
		switch step.Kind {
		case "pull":
			paths = append(paths, step.From)
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
	code, out, errOut := r.device.Shell("am start -n " + s.Component)
	if code != 0 {
		return fmt.Errorf("am start %s: %s", s.Component, firstLine(out+errOut))
	}
	return nil
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
	return r.tapAt(x, y, s.Duration)
}

func (r *runner) tapAt(x, y, duration int) error {
	command := fmt.Sprintf("input tap %d %d", x, y)
	if duration > 0 {
		command = fmt.Sprintf("input swipe %d %d %d %d %d", x, y, x, y, duration)
	}
	code, _, errOut := r.device.Shell(command)
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
		if err := r.tapAt(x, y, 0); err != nil {
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
	if err := r.tapAt(x, y, 0); err != nil {
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
	if _, err := r.device.Run("pull", s.From, local); err != nil {
		return err
	}
	return redactFile(local, r.recipe.Redact)
}

func (r *runner) push(s Step) error {
	local, err := r.localPath(s.From)
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
	code, out, errOut := r.device.Shell("uiautomator dump " + dumpPath)
	if code != 0 {
		return nil, nil, fmt.Errorf("uiautomator dump: %s", firstLine(out+errOut))
	}
	local := filepath.Join(r.tmp, "dump.xml")
	if _, err := r.device.Run("pull", dumpPath, local); err != nil {
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
	// The boundary is lexical: a committed symlink inside the app folder is
	// trusted, because recipes are repository policy, not untrusted input.
	if filepath.IsAbs(path) {
		return "", fmt.Errorf("%s is absolute; a recipe only names files in its own directory", path)
	}
	if r.recipe.Dir == "" {
		return "", fmt.Errorf("%s needs the recipe's directory", path)
	}
	return filepath.Join(r.recipe.Dir, path), nil
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
