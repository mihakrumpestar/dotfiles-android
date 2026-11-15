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
	"sort"
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
	// Clear drops the app's task before starting it, so a launch lands on the
	// root activity instead of resuming whatever page an earlier run left open.
	Clear bool `json:"clear,omitempty"`

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
	// Newest turns a pull's `from` into a device-side glob and takes the most
	// recently modified match, which is how an app that stamps a timestamp into
	// every export name is read. Nothing is deleted: those files are the app's
	// own backups, not a scratch file.
	Newest bool `json:"newest,omitempty"`

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
	// Artifact is the file name inside the app's export directory.
	Artifact string `json:"artifact"`
	// Volatile lists JSON field paths that legitimately change on every export
	// (a timestamp), so comparison ignores them without hiding real drift.
	Volatile []string `json:"volatile,omitempty"`
	// Redact lists JSON field paths whose values never reach the repository: a
	// place name, a coordinate. They are zeroed on the way in, so a re-export
	// cannot put them back.
	Redact []string `json:"redact,omitempty"`
	// Private lists JSON field paths whose values live only in the export tree
	// and never in the committed artifact. The artifact carries the zero of each
	// path, so it still commits as a readable diff of everything else, and a
	// restore merges the values back before pushing the file to the phone.
	Private []string `json:"private,omitempty"`
	// PrivateFile names a file in the export tree that holds the `private` list
	// instead of the recipe. A selector can name what must not be published, so a
	// recipe that spelled one out would publish the very entry it hides.
	PrivateFile string `json:"-"`
	// Sort lists JSON field paths that hold a list whose order carries no
	// meaning. The app writes them in whatever order its own map yields, so two
	// exports of identical state differ only by permutation; sorting by each
	// element's own JSON makes the comparison order-insensitive.
	Sort    []string `json:"sort,omitempty"`
	Backup  []Step   `json:"backup"`
	Restore []Step   `json:"restore,omitempty"`

	// Dir is the directory this recipe was loaded from, relative to the repo
	// root, e.g. policy/data/yamlauncher. It is filled by the loader and never
	// parsed from the file, so a recipe cannot claim someone else's data.
	Dir string `json:"-"`

	// DataDir is where the values that may not be published live: the same
	// normalized name under the private data root, whose location the loader
	// supplies. A recipe that splits its artifact writes the overlay here.
	DataDir string `json:"-"`
	// BlobDir is where this recipe's artifact and its steps' bare file names
	// resolve. An export is a snapshot of one phone, so it defaults to DataDir;
	// a recipe that names `private` fields makes its artifact safe by
	// construction, and only then does the artifact live beside the recipe.
	BlobDir string `json:"-"`
}

// Name is the app's normalized name: the name of the directory holding it, which
// is lowercase and path friendly so it reads the same in a shell and in a tree.
// parsePrivate reads the `private` key, which is either the list itself or the
// name of a file in the export tree that holds it.
func (r *Recipe) parsePrivate(body json.RawMessage) error {
	if len(body) == 0 {
		return nil
	}
	var list []string
	if err := json.Unmarshal(body, &list); err == nil {
		r.Private = list
		return nil
	}
	var name string
	if err := json.Unmarshal(body, &name); err != nil {
		return errors.New(`"private" is a list of selectors, or the name of a file holding one`)
	}
	if name == "" || filepath.IsAbs(name) || strings.ContainsAny(name, " \t") || strings.Contains(name, "..") {
		return fmt.Errorf("invalid private file %q: a bare name inside the export directory", name)
	}
	if !strings.HasSuffix(name, ".json") {
		return fmt.Errorf("invalid private file %q: a JSON file", name)
	}
	r.PrivateFile = name
	return nil
}

// LoadPrivate reads the selector list a recipe keeps out of the repository. It is
// called once the loader knows the export directory, and it is an error for the
// file to be missing: silently treating the recipe as having no private fields
// would publish them.
func (r *Recipe) LoadPrivate() error {
	if r.PrivateFile == "" {
		return nil
	}
	path := filepath.Join(r.DataDir, r.PrivateFile)
	body, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("reading %s: %w", path, err)
	}
	var list []string
	if err := json.Unmarshal(body, &list); err != nil {
		return fmt.Errorf("%s: a list of selectors in the private tree: %w", path, err)
	}
	if len(list) == 0 {
		// An empty file would quietly publish everything it was meant to hold.
		return fmt.Errorf("%s: no selectors, so the recipe would publish every field", path)
	}
	r.Private = list
	return nil
}

// IsPrivate reports whether this recipe keeps part of its export out of the
// repository, which is what decides where its artifact belongs.
func (r Recipe) IsPrivate() bool {
	return len(r.Private) > 0 || r.PrivateFile != ""
}

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
// blobs it produces live in the private export tree, under the folder name.
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
		Private  json.RawMessage   `json:"private"`
		Sort     []string          `json:"sort"`
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
		Volatile: raw.Volatile, Redact: raw.Redact, Sort: raw.Sort,
	}
	if err := recipe.parsePrivate(raw.Private); err != nil {
		return Recipe{}, err
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
	Newest    bool    `json:"newest"`
	Clear     bool    `json:"clear"`
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
	step.Newest = args.Newest
	step.Clear = args.Clear
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
	if r.IsPrivate() && !strings.HasSuffix(r.Artifact, ".json") {
		// Splitting is a JSON operation, and a binary artifact would land in the
		// recipe tree whole instead of with its private half removed.
		return errors.New("private fields need a JSON artifact")
	}
	if r.PrivateFile != "" && len(r.Private) > 0 {
		return errors.New(`"private" is either a list or a file name, not both`)
	}
	for _, field := range r.Private {
		if field == "" || strings.ContainsAny(field, " \t/") {
			return fmt.Errorf("invalid private field %q", field)
		}
		if _, err := parseSelection(field); err != nil {
			return err
		}
		if slices.Contains(r.Redact, field) {
			return fmt.Errorf("%q is both redacted and private: a value is thrown away or kept, not both", field)
		}
	}
	for _, field := range r.Sort {
		if field == "" || strings.ContainsAny(field, " \t/") {
			return fmt.Errorf("invalid sort field %q", field)
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
		if s.Newest && s.Kind != "pull" {
			return errors.New("newest is only valid on pull")
		}
		// The local side always names a file in the app's export directory: a
		// recipe may not read or write anywhere else on the host.
		local := s.To
		if s.Kind == "push" {
			local = s.From
		}
		if filepath.IsAbs(local) {
			return fmt.Errorf("%s local path %q must be relative to the export directory", s.Kind, local)
		}
		parts := strings.Split(filepath.ToSlash(local), "/")
		for _, part := range parts {
			if part == ".." {
				return fmt.Errorf("%s local path %q leaves the export directory", s.Kind, local)
			}
		}
		if parts[len(parts)-1] == StepsFile {
			return fmt.Errorf("%s local path %q would overwrite the recipe", s.Kind, local)
		}
		if s.Newest && !strings.HasPrefix(s.From, "/sdcard/") {
			return fmt.Errorf("newest needs a device path under /sdcard, got %q", s.From)
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
	return filepath.Join(r.BlobDir, r.Artifact)
}

// PrivatePath is the overlay holding the values `private` lifted out of the
// artifact: the part of this app's export that stays in the private tree.
func (r Recipe) PrivatePath() string {
	base := strings.TrimSuffix(r.Artifact, filepath.Ext(r.Artifact))
	return filepath.Join(r.DataDir, base+".private.json")
}

// CapturePath is where `capture` steps write, inside the private data root: a
// screen dump is the phone's state, not policy.
func (r Recipe) CapturePath() string {
	return filepath.Join(r.DataDir, r.CaptureFolder())
}

// CaptureFolder is the folder `capture` steps write into. A recipe with an
// artifact names it after that artifact; a capture-only recipe has no artifact,
// so its captures share one folder.
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

// WithDataDir returns a copy that resolves its artifact and its overlay under a
// different directory, keeping the placeholder values a recipe was written with.
// Verify uses it to export into a temporary tree without touching the stored
// files.
func (r Recipe) WithDataDir(dir string) Recipe {
	copy := r
	copy.DataDir = dir
	copy.BlobDir = dir
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
// every export) are replaced by a placeholder, lists named in sort are put in a
// stable order, and the result is re-encoded with sorted keys so formatting
// differences do not read as drift. Anything that is not JSON is returned
// unchanged.
func Canonical(body []byte, volatile, sortLists []string) []byte {
	var value any
	if err := json.Unmarshal(body, &value); err != nil {
		return body
	}
	for _, path := range volatile {
		mask(value, strings.Split(path, "."))
	}
	for _, path := range sortLists {
		sortList(value, strings.Split(path, "."))
	}
	pretty, err := json.MarshalIndent(value, "", " ")
	if err != nil {
		return body
	}
	return append(pretty, '\n')
}

// sortList orders the list at path by each element's own encoding. The app
// writes some lists in map iteration order, so without this an unchanged phone
// still reads as drift.
func sortList(value any, path []string) {
	if len(path) == 0 {
		return
	}
	if list, ok := value.([]any); ok {
		for _, item := range list {
			sortList(item, path)
		}
		return
	}
	object, ok := value.(map[string]any)
	if !ok {
		return
	}
	if len(path) == 1 {
		list, ok := object[path[0]].([]any)
		if !ok {
			return
		}
		type keyed struct {
			key  string
			item any
		}
		entries := make([]keyed, len(list))
		for i, item := range list {
			encoded, err := json.Marshal(item)
			if err != nil {
				return
			}
			entries[i] = keyed{key: string(encoded), item: item}
		}
		sort.Slice(entries, func(i, j int) bool { return entries[i].key < entries[j].key })
		for i, entry := range entries {
			list[i] = entry.item
		}
		return
	}
	sortList(object[path[0]], path[1:])
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
	out, err := marshalCanonical(value)
	if err != nil {
		return body
	}
	return out
}

// zero replaces the value at path with the zero of its own type, creating
// nothing. A list is walked in place, so a path reaches every element.
func zero(value any, path []string) {
	if len(path) == 0 {
		return
	}
	if list, ok := value.([]any); ok {
		for _, item := range list {
			zero(item, path)
		}
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
		case []any:
			// A path can name a whole list, and zeroing it has to empty it: a
			// private list left in place would publish everything it holds.
			object[path[0]] = []any{}
		case map[string]any:
			object[path[0]] = map[string]any{}
		}
		return
	}
	zero(object[path[0]], path[1:])
}

// mask replaces the value at path with a fixed placeholder, creating nothing:
// a volatile path the app stopped writing is not an error.
//
// A list is walked in place, so the same path applies to every element: an
// export shaped as an app list has the field once per app, not once per file.
func mask(value any, path []string) {
	if len(path) == 0 {
		return
	}
	if list, ok := value.([]any); ok {
		for _, item := range list {
			mask(item, path)
		}
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
