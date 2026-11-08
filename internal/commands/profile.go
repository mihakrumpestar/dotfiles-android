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

// ApplyCmd applies policy/profile.conf. Both stores are covered by one command
// because the namespace decides which one owns an entry.
type ApplyCmd struct {
	File  string `help:"Profile to apply." default:"policy/profile.conf" type:"path"`
	State string `help:"Directory for the pre-apply snapshot." default:".state" type:"path"`
	Yes   bool   `help:"Actually write. Without this, only reports."`
}

// Run implements apply.
func (c *ApplyCmd) Run(global *Globals) error {
	p, err := loadProfile(c.File)
	if err != nil {
		return err
	}
	device, err := global.Device()
	if err != nil {
		return err
	}

	before, err := profile.ReadAll(device, p)
	if err != nil {
		return err
	}

	changed := map[string]*string{}
	for _, entry := range p {
		key := entry.Namespace + "/" + entry.Key
		same := before[key] != nil && *before[key] == entry.Value
		marker := "would"
		switch {
		case same:
			marker = "same"
		case c.Yes:
			marker = "write"
			changed[key] = before[key]
		}
		fmt.Fprintf(stdout, "  %-6s %s = %s  (%s)\n", marker, key, entry.Value, entry.Reason)
	}

	if !c.Yes {
		fmt.Fprintln(stdout, "\ndry run; re-run with --yes to write")
		return nil
	}
	if len(changed) == 0 {
		fmt.Fprintln(stdout, "\nnothing to do; every entry already matches")
		return nil
	}

	path, err := writeSnapshot(c.State, device.Serial, changed)
	if err != nil {
		return err
	}
	fmt.Fprintf(stdout, "\nsnapshot: %s\n", path)

	failed := 0
	for _, entry := range p {
		key := entry.Namespace + "/" + entry.Key
		if _, dirty := changed[key]; !dirty {
			continue
		}
		store := profile.StoreFor(entry.Namespace)
		ok, message := store.Write(device, entry)
		if !ok {
			failed++
			fmt.Fprintf(os.Stderr, "  FAILED  %s  %s\n", key, message)
			continue
		}
		// The command can be accepted and ignored, which is the failure this
		// tool exists to catch. Only the stored value is evidence.
		got, set, err := store.Read(device, entry.Namespace, entry.Key)
		if err != nil {
			return err
		}
		if !set || got != entry.Value {
			failed++
			fmt.Fprintf(os.Stderr, "  IGNORED %s  device holds %q, want %q\n", key, got, entry.Value)
		}
	}
	if failed > 0 {
		return fmt.Errorf("%d of %d writes did not take effect", failed, len(changed))
	}
	fmt.Fprintf(stdout, "applied %d entries\n", len(changed))
	return nil
}

// VerifyCmd reports drift without writing.
type VerifyCmd struct {
	File string `help:"Profile to check." default:"policy/profile.conf" type:"path"`
}

// Run implements verify.
func (c *VerifyCmd) Run(global *Globals) error {
	p, err := loadProfile(c.File)
	if err != nil {
		return err
	}
	device, err := global.Device()
	if err != nil {
		return err
	}

	drift := 0
	for _, entry := range p {
		key := entry.Namespace + "/" + entry.Key
		current, set, err := profile.StoreFor(entry.Namespace).Read(device, entry.Namespace, entry.Key)
		if err != nil {
			return err
		}
		if !set {
			fmt.Fprintf(stdout, "  unset   %s (want %q)\n", key, entry.Value)
			drift++
			continue
		}
		if current == entry.Value {
			fmt.Fprintf(stdout, "  ok      %s = %s\n", key, current)
			continue
		}
		fmt.Fprintf(stdout, "  drift   %s is %q, want %q\n", key, current, entry.Value)
		drift++
	}
	if drift > 0 {
		return fmt.Errorf("%d entr(ies) differ from the profile", drift)
	}
	fmt.Fprintln(stdout, "\nall entries match")
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
	if snapshot.Values == nil {
		return fmt.Errorf("%s is not a profile snapshot", path)
	}
	if snapshot.Serial != device.Serial {
		return fmt.Errorf("snapshot is for %s, device is %s", snapshot.Serial, device.Serial)
	}

	fmt.Fprintf(stdout, "snapshot %s (%s)\n", filepath.Base(path), snapshot.Created)
	failed := 0
	for _, key := range sortedKeys(snapshot.Values) {
		namespace, name, ok := strings.Cut(key, "/")
		if !ok {
			return fmt.Errorf("malformed snapshot key %q", key)
		}
		store := profile.StoreFor(namespace)
		previous := snapshot.Values[key]
		if previous == nil {
			ok, message := store.Delete(device, namespace, name)
			if !ok {
				failed++
				fmt.Fprintf(os.Stderr, "  FAILED  %s  %s\n", key, message)
				continue
			}
			fmt.Fprintf(stdout, "  unset   %s\n", key)
			continue
		}
		ok, message := store.Write(device, profile.Entry{
			Namespace: namespace, Key: name, Value: *previous,
		})
		if !ok {
			failed++
			fmt.Fprintf(os.Stderr, "  FAILED  %s  %s\n", key, message)
			continue
		}
		fmt.Fprintf(stdout, "  restore %s = %s\n", key, *previous)
	}
	if failed > 0 {
		return fmt.Errorf("%d of %d could not be restored", failed, len(snapshot.Values))
	}
	fmt.Fprintf(stdout, "reverted %d entr(ies)\n", len(snapshot.Values))
	return nil
}

func loadProfile(path string) (profile.Profile, error) {
	body, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("reading %s: %w", path, err)
	}
	p, err := profile.Parse(string(body))
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return p, nil
}

// snapshotFile records each touched entry's pre-apply value. A nil value means
// the key was unset, which is undone with `delete`, not `put ""`.
type snapshotFile struct {
	Created string             `json:"created"`
	Serial  string             `json:"serial"`
	Values  map[string]*string `json:"values"`
}

func writeSnapshot(dir, serial string, values map[string]*string) (string, error) {
	now := time.Now().UTC().Truncate(time.Second)
	path := filepath.Join(dir, "profile", now.Format("20060102T150405Z")+"-"+serial+".json")
	err := debloat.WriteJSON(path, snapshotFile{
		Created: now.Format(time.RFC3339), Serial: serial, Values: values,
	})
	return path, err
}

func readSnapshot(path string) (snapshotFile, error) {
	var snapshot snapshotFile
	err := debloat.ReadJSON(path, &snapshot)
	return snapshot, err
}
