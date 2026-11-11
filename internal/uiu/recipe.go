// Package uiu replays a declarative UI recipe over adb: dump the screen,
// resolve a node, act on it. It exists because an app's own export/import UI is
// the only way to reach its private state without root.
package uiu

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"

	"github.com/mihakrumpestar/dotfiles-android/internal/debloat"
)

// ErrNoMatch is returned when a selector matches no node in the dump.
var ErrNoMatch = errors.New("no matching node")

var (
	// artifactRE is narrow because the artifact name becomes a file name on the
	// host and on the device.
	artifactRE = regexp.MustCompile(`^[A-Za-z0-9._-]+$`)
	// keyRE keeps `input keyevent` arguments from becoming shell syntax.
	keyRE = regexp.MustCompile(`^[A-Za-z0-9_]+$`)
	// nameRE is the app folder: lowercase, no spaces, so the name needs no
	// quoting in a shell or a URL.
	nameRE = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]*$`)
)

// Selector picks one node out of a dump. Every field is optional, but at least
// one must be set; index is a tie-breaker, not a selector on its own.
//
// Class exists because a SAF dialog reuses android:id/title for its file rows,
// so the id alone cannot name the file-name field.
type Selector struct {
	Text         string `json:"text,omitempty"`
	ID           string `json:"id,omitempty"`
	Desc         string `json:"desc,omitempty"`
	TextContains string `json:"textContains,omitempty"`
	Class        string `json:"class,omitempty"`
	Index        int    `json:"index,omitempty"`
}

// Empty reports whether the selector names nothing to match on.
func (s Selector) Empty() bool {
	return s.Text == "" && s.ID == "" && s.Desc == "" && s.TextContains == "" && s.Class == ""
}

// String renders the selector for error messages.
func (s Selector) String() string {
	var parts []string
	if s.Text != "" {
		parts = append(parts, "text="+s.Text)
	}
	if s.ID != "" {
		parts = append(parts, "id="+s.ID)
	}
	if s.Desc != "" {
		parts = append(parts, "desc="+s.Desc)
	}
	if s.TextContains != "" {
		parts = append(parts, "textContains="+s.TextContains)
	}
	if s.Class != "" {
		parts = append(parts, "class="+s.Class)
	}
	if s.Index != 0 {
		parts = append(parts, fmt.Sprintf("index=%d", s.Index))
	}
	return "{" + strings.Join(parts, " ") + "}"
}

// Step is one recipe step. Exactly one action is set, which is what Kind
// names; the remaining fields are that action's arguments.
type Step struct {
	Kind string `json:"-"`

	// launch
	Component string `json:"component,omitempty"`

	// tap, type, clear, wait, assert
	Selector *Selector `json:"selector,omitempty"`
	Optional bool      `json:"optional,omitempty"`
	// Duration turns a tap into a long press when it is non-zero, in ms.
	Duration int `json:"duration,omitempty"`

	// type
	Text string `json:"text,omitempty"`

	// wait
	Timeout float64 `json:"timeout,omitempty"`

	// key
	Code string `json:"code,omitempty"`

	// scroll
	Direction string `json:"direction,omitempty"`
	Times     int    `json:"times,omitempty"`

	// sleep
	Seconds float64 `json:"seconds,omitempty"`

	// pull, push
	From string `json:"from,omitempty"`
	To   string `json:"to,omitempty"`

	// capture
	Name string `json:"name,omitempty"`

	// assert
	Checked *bool `json:"checked,omitempty"`
}

// Recipe is one app's backup and restore flow, plus the artifact they agree on.
//
// The directory an app lives in is its normalized name, lowercase and path
// friendly: policy/data/yamlauncher/. The package id appears once, in the recipe
// itself, and never as a path component. The folder is the name, so renaming it
// renames the app in every report without touching the JSON.
type Recipe struct {
	Package string `json:"package"`
	// Artifact is the file name inside the app's own directory.
	Artifact string `json:"artifact"`
	// Volatile lists JSON field paths that legitimately change on every export
	// (a timestamp), so comparison ignores them without hiding real drift.
	Volatile []string `json:"volatile,omitempty"`
	// Redact lists JSON field paths whose values never reach the repository: a
	// place name, a coordinate. They are zeroed on the way in, so a re-export
	// cannot put them back.
	Redact  []string `json:"redact,omitempty"`
	Backup  []Step   `json:"backup"`
	Restore []Step   `json:"restore,omitempty"`

	// Dir is the directory this recipe was loaded from, relative to the repo
	// root, e.g. policy/data/yamlauncher. It is filled by the loader and never
	// parsed from the file, so a recipe cannot claim someone else's data.
	Dir string `json:"-"`
}

// Name is the app's normalized name: the name of the directory holding it, which
// is lowercase and path friendly so it reads the same in a shell and in a tree.
func (r Recipe) Name() string {
	return filepath.Base(r.Dir)
}

// actionFields are the step keys that name an action. Every other key is
// either an argument of that action or an error.
var actionFields = []string{
	"launch", "tap", "type", "clear", "wait", "key",
	"scroll", "sleep", "pull", "push", "capture", "assert",
}

// StepsFile is the one file in an app's directory that holds the recipe; the
// blobs beside it are the app's own exports.
const StepsFile = "steps.json"

// Parse reads one app's recipe and validates it. The directory it sits in is
// taken from the path, so the tree itself decides where the data lives and what
// the app is called.
func Parse(path string) (Recipe, error) {
	if base := filepath.Base(path); base != StepsFile {
		return Recipe{}, fmt.Errorf("%s: a recipe must be named %s", path, StepsFile)
	}
	body, err := os.ReadFile(path)
	if err != nil {
		return Recipe{}, fmt.Errorf("reading %s: %w", path, err)
	}
	recipe, err := ParseBytes(body)
	if err != nil {
		return Recipe{}, fmt.Errorf("%s: %w", path, err)
	}
	recipe.Dir = filepath.Dir(path)
	if name := recipe.Name(); name == "." || name == string(filepath.Separator) {
		return Recipe{}, fmt.Errorf("%s: a recipe must sit in its own directory", path)
	}
	if !nameRE.MatchString(recipe.Name()) {
		return Recipe{}, fmt.Errorf("%s: folder %q is not a normalized app name; use lowercase letters, digits, dot, dash or underscore, e.g. yamlauncher", path, recipe.Name())
	}
	return recipe, nil
}

// ParseBytes parses and validates a recipe held in memory. It has no directory,
// so it also has no name: the tree supplies both.
func ParseBytes(body []byte) (Recipe, error) {
	var keys map[string]json.RawMessage
	if err := json.Unmarshal(body, &keys); err == nil {
		if _, ok := keys["name"]; ok {
			return Recipe{}, errors.New(`"name" is the directory name now; delete this key`)
		}
	}

	var raw struct {
		Package  string            `json:"package"`
		Artifact string            `json:"artifact"`
		Volatile []string          `json:"volatile"`
		Redact   []string          `json:"redact"`
		Backup   []json.RawMessage `json:"backup"`
		Restore  []json.RawMessage `json:"restore"`
	}
	decoder := json.NewDecoder(strings.NewReader(string(body)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&raw); err != nil {
		return Recipe{}, fmt.Errorf("parsing recipe: %w", err)
	}
	// A second value in the same file is a paste accident, not a second recipe.
	if err := decoder.Decode(new(json.RawMessage)); !errors.Is(err, io.EOF) {
		return Recipe{}, errors.New("parsing recipe: trailing data after the recipe")
	}

	recipe := Recipe{
		Package: raw.Package, Artifact: raw.Artifact,
		Volatile: raw.Volatile, Redact: raw.Redact,
	}
	var err error
	if recipe.Backup, err = parseSteps(raw.Backup); err != nil {
		return Recipe{}, fmt.Errorf("backup: %w", err)
	}
	if recipe.Restore, err = parseSteps(raw.Restore); err != nil {
		return Recipe{}, fmt.Errorf("restore: %w", err)
	}
	// Steps keep their placeholders so verify and restore can retarget the
	// artifact name; Validate expands them before judging paths.
	if err := Validate(recipe); err != nil {
		return Recipe{}, err
	}
	return recipe, nil
}

func parseSteps(raw []json.RawMessage) ([]Step, error) {
	steps := make([]Step, 0, len(raw))
	for i, body := range raw {
		step, err := parseStep(body)
		if err != nil {
			return nil, fmt.Errorf("step %d: %w", i+1, err)
		}
		steps = append(steps, step)
	}
	return steps, nil
}

func parseStep(body json.RawMessage) (Step, error) {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(body, &fields); err != nil {
		return Step{}, fmt.Errorf("not an object: %w", err)
	}
	var kind string
	keys := make([]string, 0, len(fields))
	for key := range fields {
		keys = append(keys, key)
	}
	slices.Sort(keys)
	for _, key := range keys {
		if !slices.Contains(actionFields, key) {
			return Step{}, fmt.Errorf("unknown key %q", key)
		}
		if kind != "" {
			return Step{}, fmt.Errorf("two actions: %q and %q", kind, key)
		}
		kind = key
	}
	if kind == "" {
		return Step{}, errors.New("no action key")
	}

	step := Step{Kind: kind}
	if err := decodeAction(kind, fields[kind], &step); err != nil {
		return Step{}, err
	}
	switch kind {
	case "wait":
		if step.Timeout == 0 {
			step.Timeout = 10
		}
	case "scroll":
		if step.Times == 0 {
			step.Times = 1
		}
	}
	return step, nil
}

// actionArgs is the union of the per-kind argument shapes. Decoding through one
// struct keeps the parser table-free; the unknown-field check is what keeps a
// misspelled key from being silently ignored.
type actionArgs struct {
	Component string    `json:"component"`
	Selector  *Selector `json:"selector"`
	// Optional is an argument of tap, not an action of its own.
	Optional  bool    `json:"optional"`
	Duration  int     `json:"duration"`
	Text      string  `json:"text"`
	Timeout   float64 `json:"timeout"`
	Code      string  `json:"code"`
	Direction string  `json:"direction"`
	Times     int     `json:"times"`
	Seconds   float64 `json:"seconds"`
	From      string  `json:"from"`
	To        string  `json:"to"`
	Name      string  `json:"name"`
	Checked   *bool   `json:"checked"`
}

func decodeAction(kind string, body json.RawMessage, step *Step) error {
	var args actionArgs
	decoder := json.NewDecoder(strings.NewReader(string(body)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&args); err != nil {
		return fmt.Errorf("%s: %w", kind, err)
	}
	step.Component = args.Component
	step.Selector = args.Selector
	step.Optional = args.Optional
	step.Duration = args.Duration
	step.Text = args.Text
	step.Timeout = args.Timeout
	step.Code = args.Code
	step.Direction = args.Direction
	step.Times = args.Times
	step.Seconds = args.Seconds
	step.From = args.From
	step.To = args.To
	step.Name = args.Name
	step.Checked = args.Checked
	return nil
}

func (s *Step) expand(r Recipe) {
	replace := func(value string) string {
		value = strings.ReplaceAll(value, "{{artifact}}", r.Artifact)
		return strings.ReplaceAll(value, "{{package}}", r.Package)
	}
	s.Component = replace(s.Component)
	s.Text = replace(s.Text)
	s.Code = replace(s.Code)
	s.Direction = replace(s.Direction)
	s.From = replace(s.From)
	s.To = replace(s.To)
	s.Name = replace(s.Name)
	if s.Selector != nil {
		// The selector is shared between a step and every copy of it, so it is
		// cloned before being rewritten: expanding a step's copy must not change
		// the recipe the caller still holds.
		sel := *s.Selector
		sel.Text = replace(sel.Text)
		sel.ID = replace(sel.ID)
		sel.Desc = replace(sel.Desc)
		sel.TextContains = replace(sel.TextContains)
		sel.Class = replace(sel.Class)
		s.Selector = &sel
	}
}

// Validate rejects a recipe that cannot run or cannot be compared. It works on
// the file's content alone, so it says nothing about the directory the recipe
// will end up in; Parse checks that.
//
// Steps are judged after expansion, on what will actually run. That is sound
// only because neither placeholder can introduce path syntax: the artifact is a
// bare file name and the package id contains no separator.
func Validate(r Recipe) error {
	if !debloat.ValidPackage(r.Package) {
		return fmt.Errorf("invalid package %q", r.Package)
	}
	if r.Artifact != "" && (!artifactRE.MatchString(r.Artifact) || r.Artifact == "." || r.Artifact == "..") {
		return fmt.Errorf("invalid artifact %q: use only letters, digits, dot, dash, underscore", r.Artifact)
	}
	for _, field := range r.Volatile {
		if field == "" || strings.ContainsAny(field, " \t/") {
			return fmt.Errorf("invalid volatile field %q", field)
		}
	}
	for _, field := range r.Redact {
		if field == "" || strings.ContainsAny(field, " \t/") {
			return fmt.Errorf("invalid redact field %q", field)
		}
	}
	if len(r.Backup) == 0 {
		return errors.New("backup is empty")
	}

	expanded := r.Expand()
	hasPull := false
	hasCapture := false
	pullsArtifact := false
	for i, step := range expanded.Backup {
		if err := validateStep(step); err != nil {
			return fmt.Errorf("backup step %d: %w", i+1, err)
		}
		switch step.Kind {
		case "pull":
			hasPull = true
			if step.To == r.Artifact {
				pullsArtifact = true
			}
		case "capture":
			hasCapture = true
		case "push":
			return fmt.Errorf("backup step %d: push belongs in restore, not backup", i+1)
		}
	}
	pushesArtifact := false
	for i, step := range expanded.Restore {
		if err := validateStep(step); err != nil {
			return fmt.Errorf("restore step %d: %w", i+1, err)
		}
		switch step.Kind {
		case "pull":
			return fmt.Errorf("restore step %d: pull belongs in backup, not restore", i+1)
		case "push":
			if step.From == r.Artifact {
				pushesArtifact = true
			}
		}
	}

	if len(r.Restore) == 0 {
		// A recipe with no restore list is a capture-only record: it must
		// capture something, and it must not export a file nothing reads back.
		if r.Artifact != "" {
			return errors.New("artifact is set but there is no restore flow")
		}
		if hasPull {
			return errors.New("restore is empty but backup exports an artifact")
		}
		if !hasCapture {
			return errors.New("backup neither exports nor captures anything")
		}
		return nil
	}
	if r.Artifact == "" {
		return errors.New("restore is set but artifact is empty")
	}
	if !hasPull || !pullsArtifact {
		return fmt.Errorf("backup never pulls the artifact %q", r.Artifact)
	}
	if !pushesArtifact {
		return fmt.Errorf("restore never pushes the artifact %q", r.Artifact)
	}
	return nil
}

func validateStep(s Step) error {
	if s.Optional && s.Kind != "tap" {
		return errors.New("optional is only valid on tap")
	}
	switch s.Kind {
	case "launch":
		if s.Component == "" {
			return errors.New("launch needs component")
		}
		if !strings.Contains(s.Component, "/") {
			return fmt.Errorf("component %q is not package/activity", s.Component)
		}
	case "tap":
		if s.Selector == nil {
			return errors.New("tap needs selector")
		}
		if s.Duration < 0 {
			return errors.New("tap duration is negative")
		}
	case "type":
		if s.Text == "" {
			return errors.New("type needs text")
		}
	case "clear", "wait", "assert":
		if s.Selector == nil {
			return fmt.Errorf("%s needs selector", s.Kind)
		}
		if s.Kind == "assert" && s.Checked == nil {
			return errors.New("assert needs checked")
		}
	case "key":
		if !keyRE.MatchString(s.Code) {
			return fmt.Errorf("invalid key code %q", s.Code)
		}
	case "scroll":
		if s.Direction != "up" && s.Direction != "down" {
			return fmt.Errorf("scroll direction %q is not up or down", s.Direction)
		}
		if s.Times < 1 {
			return errors.New("scroll times must be at least 1")
		}
	case "sleep":
		if s.Seconds <= 0 {
			return errors.New("sleep seconds must be positive")
		}
	case "pull", "push":
		if s.From == "" || s.To == "" {
			return fmt.Errorf("%s needs from and to", s.Kind)
		}
		// The local side always names a file in the app's own directory: a
		// recipe may not read or write anywhere else on the host.
		local := s.To
		if s.Kind == "push" {
			local = s.From
		}
		if filepath.IsAbs(local) {
			return fmt.Errorf("%s local path %q must be relative to the app directory", s.Kind, local)
		}
		parts := strings.Split(filepath.ToSlash(local), "/")
		for _, part := range parts {
			if part == ".." {
				return fmt.Errorf("%s local path %q leaves the app directory", s.Kind, local)
			}
		}
		if parts[len(parts)-1] == StepsFile {
			return fmt.Errorf("%s local path %q would overwrite the recipe", s.Kind, local)
		}
	case "capture":
		if !artifactRE.MatchString(s.Name) || s.Name == "." || s.Name == ".." {
			return fmt.Errorf("invalid capture name %q", s.Name)
		}
	default:
		return fmt.Errorf("unknown step %q", s.Kind)
	}
	if s.Selector != nil {
		if s.Selector.Empty() {
			return errors.New("selector names no field")
		}
		if s.Selector.Index < 0 {
			return errors.New("selector index is negative")
		}
	}
	return nil
}

// ArtifactPath is the artifact's location, relative to the repo root.
func (r Recipe) ArtifactPath() string {
	return filepath.Join(r.Dir, r.Artifact)
}

// CaptureFolder is where `capture` steps write, relative to the app's own
// directory. A recipe with an artifact names it after that artifact; a
// capture-only recipe has no artifact, so its captures share one folder.
func (r Recipe) CaptureFolder() string {
	if r.Artifact != "" {
		return strings.TrimSuffix(r.Artifact, filepath.Ext(r.Artifact)) + ".d"
	}
	return "captures"
}

// Expand returns a copy of the recipe with every placeholder replaced.
func (r Recipe) Expand() Recipe {
	copy := r
	copy.Backup = make([]Step, len(r.Backup))
	copy.Restore = make([]Step, len(r.Restore))
	for i := range r.Backup {
		copy.Backup[i] = r.Backup[i]
		copy.Backup[i].expand(r)
	}
	for i := range r.Restore {
		copy.Restore[i] = r.Restore[i]
		copy.Restore[i].expand(r)
	}
	return copy
}

// WithDir returns a copy that resolves its artifact under a different directory,
// keeping the placeholder values a recipe was written with. Verify uses it to
// export into a temporary tree without retargeting the recipe by hand.
func (r Recipe) WithDir(dir string) Recipe {
	copy := r
	copy.Dir = dir
	return copy
}

// WithArtifact returns a copy whose steps target a different artifact file name.
// Verify exports into a temporary name so a run never mistakes the stored
// artifact for the one it just produced.
func (r Recipe) WithArtifact(name string) Recipe {
	copy := r
	copy.Artifact = name
	return copy
}

// Canonical returns the bytes that two exports are compared on. A JSON artifact
// keeps only meaningful content: volatile paths (a timestamp the app writes on
// every export) are replaced by a placeholder, and the result is re-encoded with
// sorted keys so formatting differences do not read as drift. Anything that is
// not JSON is returned unchanged.
func Canonical(body []byte, volatile []string) []byte {
	var value any
	if err := json.Unmarshal(body, &value); err != nil {
		return body
	}
	for _, path := range volatile {
		mask(value, strings.Split(path, "."))
	}
	pretty, err := json.MarshalIndent(value, "", " ")
	if err != nil {
		return body
	}
	return append(pretty, '\n')
}

// Redact returns the bytes an export may be stored as: every redact path is
// replaced by the zero of its own type, so a place name or a coordinate leaves
// nothing behind and the app's own import still finds the key it expects. A path
// the export does not carry is not an error, and a non-JSON body is returned
// unchanged.
//
// The zeroing is a value substitution, not a deletion: the app restores by
// clearing preferences and putting back what the file names, so dropping a key
// would delete it from the device instead of resetting it. The replacement keeps
// the original type, because that app reads the value back with a typed getter
// that throws on a mismatch, which would fail the whole restore.
func Redact(body []byte, fields []string) []byte {
	var value any
	if err := json.Unmarshal(body, &value); err != nil {
		return body
	}
	for _, path := range fields {
		zero(value, strings.Split(path, "."))
	}
	pretty, err := json.MarshalIndent(value, "", " ")
	if err != nil {
		return body
	}
	return append(pretty, '\n')
}

// zero replaces the value at path with the zero of its own type, creating
// nothing.
func zero(value any, path []string) {
	if len(path) == 0 {
		return
	}
	object, ok := value.(map[string]any)
	if !ok {
		return
	}
	if len(path) == 1 {
		switch object[path[0]].(type) {
		case string:
			object[path[0]] = ""
		case bool:
			object[path[0]] = false
		case float64:
			object[path[0]] = 0
		}
		return
	}
	zero(object[path[0]], path[1:])
}

// mask replaces the value at path with a fixed placeholder, creating nothing:
// a volatile path the app stopped writing is not an error.
func mask(value any, path []string) {
	if len(path) == 0 {
		return
	}
	object, ok := value.(map[string]any)
	if !ok {
		return
	}
	if len(path) == 1 {
		if _, present := object[path[0]]; present {
			object[path[0]] = "<volatile>"
		}
		return
	}
	mask(object[path[0]], path[1:])
}
