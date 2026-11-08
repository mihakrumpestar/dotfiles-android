// Package profile applies declarative device state to Android's two key/value
// stores. They differ only in the CLI that reaches them, so one set of
// operations covers both.
package profile

import (
	"errors"
	"fmt"
	"regexp"
	"strings"

	"github.com/mihakrumpestar/dotfiles-android/internal/debloat"
)

// nameRE is narrow on purpose: namespaces and keys reach `adb shell`, so
// anything wider would be syntax there, not a name.
var nameRE = regexp.MustCompile(`^[A-Za-z0-9_.]+$`)

// Entry is one namespace/key/value triple and why it is wanted.
type Entry struct {
	Namespace string
	Key       string
	Value     string
	Reason    string
}

// Profile is an ordered set of entries.
type Profile []Entry

// Store is a device key/value store.
type Store struct {
	Name string
	// prefix is the shell command that reaches the store.
	prefix []string
	// namespaces is the set the Settings provider accepts; every other
	// namespace belongs to DeviceConfig. The namespace alone decides the store,
	// so a value cannot be written to the one that cannot see it.
	namespaces []string
}

var (
	Settings     = Store{Name: "settings", prefix: []string{"settings"}, namespaces: []string{"system", "secure", "global"}}
	DeviceConfig = Store{Name: "device_config", prefix: []string{"cmd", "device_config"}}
)

// StoreFor returns the store that owns namespace.
func StoreFor(namespace string) Store {
	for _, known := range Settings.namespaces {
		if namespace == known {
			return Settings
		}
	}
	return DeviceConfig
}

func (s Store) run(d *debloat.Device, parts ...string) (string, error) {
	return d.Run("shell", s.command(parts...))
}

// command renders one store invocation with every argument quoted. adb joins
// its arguments into a single string the device's sh parses, so an unquoted
// argument is syntax, not a value.
func (s Store) command(parts ...string) string {
	quoted := make([]string, len(parts))
	for i, part := range parts {
		quoted[i] = quote(part)
	}
	return strings.Join(append(append([]string{}, s.prefix...), quoted...), " ")
}

// Read returns the current value and whether the key is set at all. The stores
// print `null` for a key that was never written, which is not the same as an
// explicit empty value when it is time to undo a run.
func (s Store) Read(d *debloat.Device, namespace, key string) (string, bool, error) {
	out, err := s.run(d, "get", namespace, key)
	if err != nil {
		return "", false, err
	}
	value := strings.TrimSpace(out)
	if value == "null" {
		return "", false, nil
	}
	return value, true, nil
}

// Write applies one entry and returns the device's own message, if any. Success
// here means the command was accepted, not that the value took effect.
func (s Store) Write(d *debloat.Device, entry Entry) (bool, string) {
	return s.mutate(d, "put", entry.Namespace, entry.Key, entry.Value)
}

// Delete unsets a key, which is how a run that created a value is undone.
func (s Store) Delete(d *debloat.Device, namespace, key string) (bool, string) {
	return s.mutate(d, "delete", namespace, key)
}

func (s Store) mutate(d *debloat.Device, verb, namespace, key string, value ...string) (bool, string) {
	parts := append([]string{verb, namespace, key}, value...)
	code, out, errOut := d.Shell(s.command(parts...))
	message := strings.TrimSpace(out)
	if message == "" {
		message = strings.TrimSpace(errOut)
	}
	return code == 0, message
}

// ReadAll captures every value a profile touches, so a run can be undone. A nil
// value means the key was unset.
func ReadAll(d *debloat.Device, p Profile) (map[string]*string, error) {
	values := make(map[string]*string, len(p))
	for _, entry := range p {
		value, set, err := StoreFor(entry.Namespace).Read(d, entry.Namespace, entry.Key)
		if err != nil {
			return nil, fmt.Errorf("reading %s/%s: %w", entry.Namespace, entry.Key, err)
		}
		if set {
			values[entry.Namespace+"/"+entry.Key] = &value
		} else {
			values[entry.Namespace+"/"+entry.Key] = nil
		}
	}
	return values, nil
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
			continue
		case namespace == "":
			return nil, fmt.Errorf("line %d: entry before any [namespace]", line)
		}
		key, rest, ok := strings.Cut(raw, "=")
		key = strings.TrimSpace(key)
		if !ok || !nameRE.MatchString(key) {
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
