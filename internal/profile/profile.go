// Package profile applies declarative device state: Android's two key/value
// stores plus the app-scoped state that only its own command can change.
package profile

import (
	"errors"
	"fmt"
	"regexp"
	"slices"
	"strings"

	"github.com/mihakrumpestar/dotfiles-android/internal/debloat"
)

// nameRE is narrow on purpose: namespaces reach `adb shell`, so anything wider
// would be syntax there, not a name.
var nameRE = regexp.MustCompile(`^[A-Za-z0-9_.]+$`)

// keyRE allows the separator, hyphens and wildcards the app-scoped stores
// need. Each store validates the parts it splits out, so the looser grammar
// never reaches the shell unvalidated.
var keyRE = regexp.MustCompile(`^[A-Za-z0-9_.:*-]+$`)

// Entry is one namespace/key/value triple and why it is wanted.
type Entry struct {
	Namespace string
	Key       string
	Value     string
	Reason    string
}

// Profile is an ordered set of entries.
type Profile []Entry

// State says where a value came from: the device holds it because somebody set
// it (Explicit), the platform derives it and a write would not stick (Implicit),
// or the store has nothing to say for this key at all (Unset).
type State int

const (
	Unset State = iota
	Implicit
	Explicit
)

func (s State) String() string {
	switch s {
	case Implicit:
		return "implicit"
	case Explicit:
		return "explicit"
	default:
		return "unset"
	}
}

// MarshalText keeps snapshots readable and stable across runs.
func (s State) MarshalText() ([]byte, error) { return []byte(s.String()), nil }

func (s *State) UnmarshalText(text []byte) error {
	switch string(text) {
	case "unset":
		*s = Unset
	case "implicit":
		*s = Implicit
	case "explicit":
		*s = Explicit
	default:
		return fmt.Errorf("unknown state %q", text)
	}
	return nil
}

// SnapshotEntry is one key's state before a run. A revert writes Explicit back,
// leaves Implicit alone because the platform derives it, and resets Unset.
type SnapshotEntry struct {
	State State  `json:"state"`
	Value string `json:"value"`
}

// Store is one declarative device store: the three operations apply, verify and
// revert need. A store with no unset state returns false from Reset.
type Store interface {
	Read(d *debloat.Device, e Entry) (value string, state State, err error)
	Write(d *debloat.Device, e Entry) (ok bool, message string)
	Reset(d *debloat.Device, e Entry) (ok bool, message string)
}

// KV is a device key/value store reached by one command prefix.
type KV struct {
	// prefix is the shell command that reaches the store.
	prefix []string
	// namespaces is the set the Settings provider accepts; every other
	// namespace belongs to DeviceConfig. The namespace alone decides the store,
	// so a value cannot be written to the one that cannot see it.
	namespaces []string
}

var (
	Settings     = &KV{prefix: []string{"settings"}, namespaces: []string{"system", "secure", "global"}}
	DeviceConfig = &KV{prefix: []string{"cmd", "device_config"}}
)

// appStores are the stores selected by their own section name.
var appStores = map[string]Store{
	"permissions":        permissionsStore{},
	"appops":             appOpsStore{},
	"role":               roleStore{},
	"deviceidle":         deviceIdleStore{},
	"standby":            standbyStore{},
	"locale":             localeStore{},
	"applinks":           appLinksStore{},
	"applinks_domain":    appLinksDomainStore{},
	"applinks_selection": appLinksSelectionStore{},
}

// deviceConfigNamespaces are the DeviceConfig namespaces a profile may name.
// The platform ships hundreds; listing only the ones the profiles use keeps a
// typo a parse error without copying the device's namespace table into a file.
// Adding one is this line, because a namespace the code never lists cannot be
// distinguished from a mistyped section.
var deviceConfigNamespaces = map[string]bool{
	"telephony": true,
}

// SectionStore returns the store that owns namespace, and whether a profile
// may name it at all. A section outside this set is a typo, not a store:
// routing it to DeviceConfig would write a row nothing reads.
func SectionStore(namespace string) (Store, bool) {
	for _, known := range Settings.namespaces {
		if namespace == known {
			return Settings, true
		}
	}
	if store, ok := appStores[namespace]; ok {
		return store, true
	}
	if deviceConfigNamespaces[namespace] {
		return DeviceConfig, true
	}
	return nil, false
}

// StoreFor returns the store that owns the entry's section. Parse rejects an
// unknown section, so every entry from a profile has one.
func StoreFor(e Entry) Store {
	store, _ := SectionStore(e.Namespace)
	return store
}

// SectionNames lists every section a profile may use, for parse errors.
func SectionNames() []string {
	names := append([]string{}, Settings.namespaces...)
	for name := range appStores {
		names = append(names, name)
	}
	for name := range deviceConfigNamespaces {
		names = append(names, name)
	}
	slices.Sort(names)
	return names
}

func (s KV) run(d *debloat.Device, parts ...string) (string, error) {
	return d.Read(s.command(parts...))
}

// command renders one store invocation with every argument quoted. adb joins
// its arguments into a single string the device's sh parses, so an unquoted
// argument is syntax, not a value.
func (s KV) command(parts ...string) string {
	return render(s.prefix, parts...)
}

// Read returns the current value and where it came from. Both key/value stores
// print `null` for a key that was never written, which is Unset, not an explicit
// empty value when it is time to undo a run.
func (s KV) Read(d *debloat.Device, e Entry) (string, State, error) {
	out, err := s.run(d, "get", e.Namespace, e.Key)
	if err != nil {
		return "", Unset, err
	}
	value := strings.TrimSpace(out)
	if value == "null" {
		return "", Unset, nil
	}
	return value, Explicit, nil
}

// Write applies one entry and returns the device's own message, if any. Success
// here means the command was accepted, not that the value took effect.
func (s KV) Write(d *debloat.Device, e Entry) (bool, string) {
	return s.mutate(d, "put", e.Namespace, e.Key, e.Value)
}

// Reset unsets a key, which is how a run that created a value is undone.
func (s KV) Reset(d *debloat.Device, e Entry) (bool, string) {
	return s.mutate(d, "delete", e.Namespace, e.Key)
}

func (s KV) mutate(d *debloat.Device, verb, namespace, key string, value ...string) (bool, string) {
	parts := append([]string{verb, namespace, key}, value...)
	code, out, errOut := d.Shell(s.command(parts...))
	message := strings.TrimSpace(out)
	if message == "" {
		message = strings.TrimSpace(errOut)
	}
	return code == 0, message
}

// ReadAll captures every entry's state before a run, so the run can be undone.
func ReadAll(d *debloat.Device, p Profile) (map[string]SnapshotEntry, error) {
	before := make(map[string]SnapshotEntry, len(p))
	for _, entry := range p {
		value, state, err := StoreFor(entry).Read(d, entry)
		if err != nil {
			return nil, fmt.Errorf("reading %s/%s: %w", entry.Namespace, entry.Key, err)
		}
		before[Key(entry)] = SnapshotEntry{State: state, Value: value}
	}
	return before, nil
}

// Key names an entry in output and in snapshots.
func Key(e Entry) string { return e.Namespace + "/" + e.Key }

// ResetOK reports whether a readback after Reset is what the reset aims for.
//
// A key/value store deletes the row, so Unset is the goal there and nothing
// else will do. Every app-scoped store resets to a value the platform then
// reports explicitly (a revoked permission, a dropped app op, no role holder,
// `ignore`, `none`), so Unset after one of those resets means the parser found
// nothing where the device should have answered, which is not evidence of
// success. deviceidle also accepts a derived allow, because a system-excidle row
// cannot be removed.
func ResetOK(e Entry, value string, state State) bool {
	if state == Implicit && e.Namespace == "deviceidle" {
		return true
	}
	if state == Unset {
		// Only a store that deletes the row aims for Unset. An app-scoped store
		// answers every read, so Unset there means the parser found nothing.
		return isSettings(e.Namespace)
	}
	neutral, ok := neutral(e)
	if !ok {
		return false
	}
	return Satisfied(neutral, value, state)
}

func isSettings(namespace string) bool {
	return slices.Contains(Settings.namespaces, namespace)
}

// neutral is the entry a store resets to when it has no delete. A store whose
// reset deletes the key has no neutral value: only Unset is accepted for it.
func neutral(e Entry) (Entry, bool) {
	switch e.Namespace {
	case "permissions":
		e.Value = "revoked"
	case "appops":
		e.Value = "default"
	case "role", "locale":
		e.Value = ""
	case "deviceidle":
		e.Value = "ignore"
	case "applinks_domain":
		e.Value = "none"
	default:
		return Entry{}, false
	}
	return e, true
}

// domainCodes resolve the two spellings `applinks_domain` accepts for one state
// (`verified` and `1`), so both sides of a comparison collapse to the code.
var domainCodes = map[string]string{
	"none": "0", "0": "0",
	"verified": "1", "1": "1",
	"approved": "2", "2": "2",
	"denied": "3", "3": "3",
}

// equivalent reports whether the value the device reports satisfies the entry.
func equivalent(e Entry, got string) bool {
	if e.Namespace != "applinks_domain" {
		return got == e.Value
	}
	code, ok := domainCodes[got]
	return ok && code == domainCodes[e.Value]
}

// Satisfied reports whether a value and state read from the device satisfy the
// entry. Unset never satisfies anything: the store has nothing to say, so a
// write that left it unset did not stick. A derived value satisfies the entry
// when it already equals the wanted value, because the platform reasserts it
// either way. An app op asked to be `default` satisfies too: the row is gone by
// design and the platform reports the derived mode, not the word `default`.
func Satisfied(e Entry, value string, state State) bool {
	if state == Unset {
		return false
	}
	if e.Namespace == "appops" && e.Value == "default" && state == Implicit {
		return true
	}
	return equivalent(e, value)
}

// render joins a command prefix and its arguments, quoting every argument.
func render(prefix []string, parts ...string) string {
	quoted := make([]string, len(parts))
	for i, part := range parts {
		quoted[i] = quote(part)
	}
	return strings.Join(append(append([]string{}, prefix...), quoted...), " ")
}

// quote makes a value survive `adb shell`, which joins its arguments into one
// string the device's sh parses. An empty value must still reach the command as
// an empty argument, or `settings put key` has one argument too few.
func quote(value string) string {
	if value != "" && !strings.ContainsAny(value, " \t\n\"'$`\\|&;<>()*?[]{}~!#") {
		return value
	}
	return "'" + strings.ReplaceAll(value, "'", `'\''`) + "'"
}

// Parse reads a profile:
//
//	[namespace]
//	key = "value"   # reason
//
// The reason is required. A change nobody can justify later is indistinguishable
// from a mistake, and the file exists so the reasoning outlives the session.
func Parse(text string) (Profile, error) {
	var p Profile
	namespace := ""
	seen := map[string]bool{}
	for i, raw := range strings.Split(text, "\n") {
		line := i + 1
		raw = strings.TrimSpace(raw)
		switch {
		case raw == "", strings.HasPrefix(raw, "#"):
			continue
		case strings.HasPrefix(raw, "[") && strings.HasSuffix(raw, "]"):
			namespace = raw[1 : len(raw)-1]
			if !nameRE.MatchString(namespace) {
				return nil, fmt.Errorf("line %d: invalid namespace %q", line, namespace)
			}
			if _, known := SectionStore(namespace); !known {
				return nil, fmt.Errorf("line %d: unknown section [%s]; known sections: %s", line, namespace, strings.Join(SectionNames(), ", "))
			}
			continue
		case namespace == "":
			return nil, fmt.Errorf("line %d: entry before any [namespace]", line)
		}
		key, rest, ok := strings.Cut(raw, "=")
		key = strings.TrimSpace(key)
		if !ok || !keyRE.MatchString(key) {
			return nil, fmt.Errorf("line %d: expected key = \"value\"", line)
		}
		value, reason, err := splitValueAndReason(rest)
		if err != nil {
			return nil, fmt.Errorf("line %d: %w", line, err)
		}
		if reason == "" {
			return nil, fmt.Errorf("line %d: %s has no trailing # reason", line, key)
		}
		name := namespace + "/" + key
		if seen[name] {
			return nil, fmt.Errorf("line %d: duplicate %s", line, name)
		}
		seen[name] = true
		p = append(p, Entry{
			Namespace: namespace,
			Key:       key,
			Value:     value,
			Reason:    reason,
		})
	}
	if len(p) == 0 {
		return nil, errors.New("profile is empty")
	}
	return p, nil
}

func splitValueAndReason(rest string) (value, reason string, err error) {
	rest = strings.TrimSpace(rest)
	if !strings.HasPrefix(rest, `"`) {
		return "", "", errors.New(`value must be quoted, e.g. "90"`)
	}
	end := strings.Index(rest[1:], `"`)
	if end < 0 {
		return "", "", errors.New("unterminated value")
	}
	value = rest[1 : end+1]
	tail := strings.TrimSpace(rest[end+2:])
	if tail == "" {
		return value, "", nil
	}
	note, ok := strings.CutPrefix(tail, "#")
	if !ok {
		return "", "", errors.New("unexpected text after the value")
	}
	note = strings.TrimSpace(note)
	for _, prefix := range []string{"why:", "why "} {
		if _, found := strings.CutPrefix(strings.ToLower(note), prefix); found {
			return value, strings.TrimSpace(note[len(prefix):]), nil
		}
	}
	return value, note, nil
}
