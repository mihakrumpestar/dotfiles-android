package commands

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/mihakrumpestar/dotfiles-android/internal/debloat"
	"github.com/mihakrumpestar/dotfiles-android/internal/profile"
)

// ApplyCmd applies the declarative profiles. Every store is covered by one
// command because the section decides which one owns an entry, and every file
// is applied by one command because a run snapshots all of them together.
type ApplyCmd struct {
	Files []string `help:"Profiles to apply, in order." default:"policy/profile.conf,policy/apps.conf" type:"path"`
	State string   `help:"Directory for the pre-apply snapshot." default:".state" type:"path"`
	Yes   bool     `help:"Actually write. Without this, only reports."`
}

// Run implements apply.
func (c *ApplyCmd) Run(global *Globals) error {
	p, err := loadProfiles(c.Files)
	if err != nil {
		return err
	}
	device, err := global.Device()
	if err != nil {
		return err
	}
	installed, err := device.Installed()
	if err != nil {
		return err
	}

	// One profile serves both phones: an entry about an app this one does not
	// carry is skipped, not written and not counted as a failure.
	applicable := make(profile.Profile, 0, len(p))
	skipped := map[string]string{}
	for _, entry := range p {
		missing, err := missingPackage(entry, installed)
		if err != nil {
			return fmt.Errorf("%s: %w", profile.Key(entry), err)
		}
		if missing != "" {
			skipped[profile.Key(entry)] = missing
			continue
		}
		applicable = append(applicable, entry)
	}

	before, err := profile.ReadAll(device, applicable)
	if err != nil {
		return err
	}

	changed := map[string]profile.SnapshotEntry{}
	for _, entry := range p {
		key := profile.Key(entry)
		if missing, skip := skipped[key]; skip {
			fmt.Fprintf(stdout, "  %-6s %s  (%s not installed)\n", "skip", key, missing)
			continue
		}
		current := before[key]
		// A derived value that already matches is left alone: the platform
		// reasserts it anyway. An unset entry is always attempted, so a write
		// that cannot work shows up as IGNORED instead of a silent skip.
		same := current.State != profile.Unset && profile.Satisfied(entry, current.Value, current.State)
		marker := "would"
		switch {
		case same:
			marker = "same"
		case c.Yes:
			marker = "write"
			changed[key] = current
		}
		fmt.Fprintf(stdout, "  %-6s %s = %s  (%s)\n", marker, key, entry.Value, entry.Reason)
	}

	if !c.Yes {
		fmt.Fprintf(stdout, "\ndry run; re-run with --yes to write%s\n", skipNote(len(skipped)))
		return nil
	}
	if len(changed) == 0 {
		fmt.Fprintf(stdout, "\nnothing to do; every applicable entry already matches%s\n", skipNote(len(skipped)))
		return nil
	}

	path, err := writeSnapshot(c.State, device.Serial, changed)
	if err != nil {
		return err
	}
	fmt.Fprintf(stdout, "\nsnapshot: %s\n", path)

	failed := 0
	for _, entry := range p {
		if _, dirty := changed[profile.Key(entry)]; !dirty {
			continue
		}
		ok, message := profile.StoreFor(entry).Write(device, entry)
		if !ok {
			failed++
			fmt.Fprintf(os.Stderr, "  FAILED  %s  %s\n", profile.Key(entry), message)
			continue
		}
		if ok, message := readback(device, entry); !ok {
			failed++
			fmt.Fprintf(os.Stderr, "  IGNORED %s  %s\n", profile.Key(entry), message)
		}
	}
	if failed > 0 {
		return fmt.Errorf("%d of %d writes did not take effect", failed, len(changed))
	}
	fmt.Fprintf(stdout, "applied %d entries%s\n", len(changed), skipNote(len(skipped)))
	return nil
}

// VerifyCmd reports drift without writing.
type VerifyCmd struct {
	Files []string `help:"Profiles to check, in order." default:"policy/profile.conf,policy/apps.conf" type:"path"`
}

// Run implements verify.
func (c *VerifyCmd) Run(global *Globals) error {
	p, err := loadProfiles(c.Files)
	if err != nil {
		return err
	}
	device, err := global.Device()
	if err != nil {
		return err
	}
	installed, err := device.Installed()
	if err != nil {
		return err
	}

	drift := 0
	skipped := 0
	for _, entry := range p {
		key := profile.Key(entry)
		missing, err := missingPackage(entry, installed)
		if err != nil {
			return fmt.Errorf("%s: %w", key, err)
		}
		if missing != "" {
			skipped++
			fmt.Fprintf(stdout, "  %-6s %s  (%s not installed)\n", "skip", key, missing)
			continue
		}
		current, state, err := profile.StoreFor(entry).Read(device, entry)
		if err != nil {
			return err
		}
		switch {
		case state == profile.Unset:
			fmt.Fprintf(stdout, "  unset   %s (want %q)\n", key, entry.Value)
			drift++
		case profile.Satisfied(entry, current, state):
			fmt.Fprintf(stdout, "  ok      %s = %s\n", key, current)
		default:
			fmt.Fprintf(stdout, "  drift   %s is %q (%s), want %q\n", key, current, state, entry.Value)
			drift++
		}
	}
	if drift > 0 {
		return fmt.Errorf("%d entr(ies) differ from the profile", drift)
	}
	fmt.Fprintf(stdout, "\nall entries match%s\n", skipNote(skipped))
	return nil
}

// RevertCmd restores the values captured before the last apply.
type RevertCmd struct {
	From  string `help:"Snapshot path; defaults to the newest." type:"path"`
	State string `help:"Directory holding snapshots." default:".state" type:"path"`
}

// Run implements revert.
func (c *RevertCmd) Run(global *Globals) error {
	device, err := global.Device()
	if err != nil {
		return err
	}
	path := c.From
	if path == "" {
		path, err = debloat.NewestFile(filepath.Join(c.State, "profile"), device.Serial)
		if err != nil {
			return err
		}
	}
	snapshot, err := readSnapshot(path)
	if err != nil {
		return err
	}
	before, err := snapshot.before()
	if err != nil {
		return fmt.Errorf("%s: %w", path, err)
	}
	if snapshot.Serial != device.Serial {
		return fmt.Errorf("snapshot is for %s, device is %s", snapshot.Serial, device.Serial)
	}

	fmt.Fprintf(stdout, "snapshot %s (%s)\n", filepath.Base(path), snapshot.Created)
	failed := 0
	skipped := 0
	restored := 0
	for _, key := range sortedKeys(before) {
		namespace, name, ok := strings.Cut(key, "/")
		if !ok {
			return fmt.Errorf("malformed snapshot key %q", key)
		}
		store, ok := profile.SectionStore(namespace)
		if !ok {
			failed++
			fmt.Fprintf(os.Stderr, "  FAILED  %s  unknown section [%s]\n", key, namespace)
			continue
		}
		entry := profile.Entry{Namespace: namespace, Key: name}
		previous := before[key]
		switch previous.State {
		case profile.Implicit:
			// The platform derived this value, but apply may have written over
			// it, which turns the derived value into an explicit row. Skipping
			// would leave that row behind, so reset it; a store with no reset
			// (standby, app links) genuinely has nothing to undo.
			reset, message := store.Reset(device, entry)
			if !reset {
				skipped++
				fmt.Fprintf(stdout, "  skip    %s (%s)\n", key, message)
				continue
			}
			if ok, message := resetReadback(device, store, entry); !ok {
				failed++
				fmt.Fprintf(os.Stderr, "  FAILED  %s  %s\n", key, message)
				continue
			}
			restored++
			fmt.Fprintf(stdout, "  reset   %s (was %s)\n", key, previous.Value)
		case profile.Explicit:
			entry.Value = previous.Value
			ok, message := store.Write(device, entry)
			if ok {
				ok, message = readback(device, entry)
			}
			if !ok {
				failed++
				fmt.Fprintf(os.Stderr, "  FAILED  %s  %s\n", key, message)
				continue
			}
			restored++
			fmt.Fprintf(stdout, "  restore %s = %s\n", key, previous.Value)
		default:
			reset, message := store.Reset(device, entry)
			if !reset {
				skipped++
				fmt.Fprintf(stdout, "  skip    %s (%s)\n", key, message)
				continue
			}
			// A reset is a write too, so it gets the same evidence: the store
			// must read back the state the reset aims for. This catches a delete
			// or revoke the platform accepted and ignored while leaving the value
			// in place.
			if ok, message := resetReadback(device, store, entry); !ok {
				failed++
				fmt.Fprintf(os.Stderr, "  FAILED  %s  %s\n", key, message)
				continue
			}
			restored++
			fmt.Fprintf(stdout, "  unset   %s\n", key)
		}
	}
	if failed > 0 {
		return fmt.Errorf("%d of %d could not be restored", failed, len(before))
	}
	fmt.Fprintf(stdout, "reverted %d entr(ies)%s\n", restored, skipNote(skipped))
	return nil
}

// readback reports whether the device now holds the entry's wanted value. A
// command can be accepted and ignored, which is the failure this tool exists to
// catch: only the stored value, read back, is evidence. Shared by apply and
// revert so the two cannot drift apart.
func readback(d *debloat.Device, entry profile.Entry) (bool, string) {
	got, state, err := profile.StoreFor(entry).Read(d, entry)
	if err != nil {
		return false, err.Error()
	}
	if !profile.Satisfied(entry, got, state) {
		return false, fmt.Sprintf("device holds %q (%s), want %q", got, state, entry.Value)
	}
	return true, ""
}

// resetReadback reports whether the device no longer holds the value a reset
// aims to remove. It is the same evidence rule as readback, applied to Reset.
func resetReadback(d *debloat.Device, store profile.Store, entry profile.Entry) (bool, string) {
	got, state, err := store.Read(d, entry)
	if err != nil {
		return false, err.Error()
	}
	if !profile.ResetOK(entry, got, state) {
		return false, fmt.Sprintf("device holds %q (%s) after the reset", got, state)
	}
	return true, ""
}

// missingPackage returns the package an entry is about that the device does not
// install for user 0, if any. A malformed key is a profile error, not a silent
// exemption from the skip rule.
func missingPackage(entry profile.Entry, installed map[string]bool) (string, error) {
	packages, err := profile.Packages(entry)
	if err != nil {
		return "", err
	}
	for _, pkg := range packages {
		if !installed[pkg] {
			return pkg, nil
		}
	}
	return "", nil
}

func skipNote(count int) string {
	if count == 0 {
		return ""
	}
	return fmt.Sprintf(" (%d skipped)", count)
}

// loadProfiles reads the given profiles in order and merges them into one, so a
// run treats them as a single declaration: one snapshot, one report, and a key
// declared twice is an error rather than a silent last-one-wins.
func loadProfiles(paths []string) (profile.Profile, error) {
	var merged profile.Profile
	seen := map[string]string{}
	for _, path := range paths {
		body, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("reading %s: %w", path, err)
		}
		p, err := profile.Parse(string(body))
		if err != nil {
			return nil, fmt.Errorf("%s: %w", path, err)
		}
		for _, entry := range p {
			key := profile.Key(entry)
			if first, dup := seen[key]; dup {
				return nil, fmt.Errorf("%s: %s is also declared in %s", path, key, first)
			}
			seen[key] = path
			merged = append(merged, entry)
		}
	}
	if len(merged) == 0 {
		return nil, fmt.Errorf("no entries across %s", strings.Join(paths, ", "))
	}
	return merged, nil
}

// snapshotFile records each touched entry's state before the run. `values` is
// the shape older snapshots use and stays readable; `entries` adds the state
// that tells a revert what to write, what to leave and what to reset.
type snapshotFile struct {
	Created string                           `json:"created"`
	Serial  string                           `json:"serial"`
	Values  map[string]*string               `json:"values,omitempty"`
	Entries map[string]profile.SnapshotEntry `json:"entries,omitempty"`
}

// before returns the snapshot's entries, reading the older values-only shape.
func (s snapshotFile) before() (map[string]profile.SnapshotEntry, error) {
	if s.Entries != nil {
		return s.Entries, nil
	}
	if s.Values == nil {
		return nil, fmt.Errorf("not a profile snapshot")
	}
	entries := make(map[string]profile.SnapshotEntry, len(s.Values))
	for key, value := range s.Values {
		entry := profile.SnapshotEntry{State: profile.Unset}
		if value != nil {
			entry = profile.SnapshotEntry{State: profile.Explicit, Value: *value}
		}
		entries[key] = entry
	}
	return entries, nil
}

func writeSnapshot(dir, serial string, entries map[string]profile.SnapshotEntry) (string, error) {
	now := time.Now().UTC().Truncate(time.Second)
	path := filepath.Join(dir, "profile", now.Format("20060102T150405Z")+"-"+serial+".json")
	err := debloat.WriteJSON(path, snapshotFile{
		Created: now.Format(time.RFC3339), Serial: serial, Entries: entries,
	})
	return path, err
}

func readSnapshot(path string) (snapshotFile, error) {
	var snapshot snapshotFile
	err := debloat.ReadJSON(path, &snapshot)
	return snapshot, err
}
