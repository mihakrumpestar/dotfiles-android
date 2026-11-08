package debloat

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"time"
)

// SnapshotFile is the on-disk shape of a package snapshot. It records only what
// is needed to undo a run: which packages were enabled at that moment.
type SnapshotFile struct {
	Created  string            `json:"created"`
	Serial   string            `json:"serial"`
	Packages map[string]string `json:"packages"`
}

// WriteSnapshot records the states the caller already read from the device.
func WriteSnapshot(dir, serial string, states map[string]string) (string, error) {
	now := time.Now().UTC().Truncate(time.Second)
	path := filepath.Join(dir, "snapshots", now.Format("20060102T150405Z")+"-"+serial+".json")
	err := WriteJSON(path, SnapshotFile{
		Created: now.Format(time.RFC3339), Serial: serial, Packages: states,
	})
	return path, err
}

// WriteJSON writes v as indented JSON with a trailing newline.
func WriteJSON(path string, v any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("creating %s: %w", filepath.Dir(path), err)
	}
	body, err := json.MarshalIndent(v, "", " ")
	if err != nil {
		return fmt.Errorf("encoding %s: %w", path, err)
	}
	if err := os.WriteFile(path, append(body, '\n'), 0o644); err != nil {
		return fmt.Errorf("writing %s: %w", path, err)
	}
	return nil
}

// ReadJSON decodes a JSON file into v.
func ReadJSON(path string, v any) error {
	body, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("reading %s: %w", path, err)
	}
	if err := json.Unmarshal(body, v); err != nil {
		return fmt.Errorf("parsing %s: %w", path, err)
	}
	return nil
}

// LoadSnapshot reads a package snapshot.
func LoadSnapshot(path string) (SnapshotFile, error) {
	var snapshot SnapshotFile
	err := ReadJSON(path, &snapshot)
	return snapshot, err
}

// NewestSnapshot returns the most recent package snapshot for this device.
func NewestSnapshot(dir, serial string) (string, error) {
	return NewestFile(filepath.Join(dir, "snapshots"), serial)
}

// NewestFile returns the lexically last <timestamp>-<serial>.json in dir. Names
// are UTC timestamps, so lexical order is chronological.
func NewestFile(dir, serial string) (string, error) {
	matches, err := filepath.Glob(filepath.Join(dir, "*-"+serial+".json"))
	if err != nil {
		return "", fmt.Errorf("looking for snapshots: %w", err)
	}
	if len(matches) == 0 {
		return "", fmt.Errorf("no snapshots for %s under %s", serial, dir)
	}
	slices.Sort(matches)
	return matches[len(matches)-1], nil
}

// EnabledIn returns the packages the snapshot recorded as enabled, sorted.
func (s SnapshotFile) EnabledIn() []string {
	var names []string
	for name, state := range s.Packages {
		if state == Enabled {
			names = append(names, name)
		}
	}
	slices.Sort(names)
	return names
}

// Summary is one line describing a snapshot, for the restore output.
func (s SnapshotFile) Summary(path string) string {
	return fmt.Sprintf("snapshot %s (%s)", filepath.Base(path), s.Created)
}
