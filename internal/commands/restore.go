package commands

import (
	"fmt"

	"github.com/mihakrumpestar/dotfiles-android/internal/debloat"
)

// RestoreCmd re-enables every package a snapshot recorded as enabled.
//
// Only what the snapshot saw as enabled comes back, so packages the human
// disabled themselves stay disabled. Restoring must not resurrect them.
type RestoreCmd struct {
	From   string `help:"Snapshot path; defaults to the newest for this device." type:"path"`
	State  string `help:"Directory holding snapshots." default:".state" type:"path"`
	DryRun bool   `help:"Print the commands, change nothing."`
}

// Run implements the restore subcommand.
func (c *RestoreCmd) Run(global *Globals) error {
	device, err := global.Device()
	if err != nil {
		return err
	}

	path := c.From
	if path == "" {
		path, err = debloat.NewestSnapshot(c.State, device.Serial)
		if err != nil {
			return err
		}
	}

	snapshot, err := debloat.LoadSnapshot(path)
	if err != nil {
		return err
	}
	if snapshot.Packages == nil {
		return fmt.Errorf("%s is not a package snapshot", path)
	}
	if snapshot.Serial != device.Serial {
		return fmt.Errorf("snapshot is for %s, device is %s", snapshot.Serial, device.Serial)
	}
	states, err := device.States()
	if err != nil {
		return err
	}

	fmt.Fprintln(stdout, snapshot.Summary(path))

	var toEnable []string
	for _, pkg := range snapshot.EnabledIn() {
		if states[pkg] == debloat.Disabled {
			toEnable = append(toEnable, pkg)
		}
	}
	if len(toEnable) == 0 {
		fmt.Fprintln(stdout, "nothing to restore")
		return nil
	}

	failed := 0
	for _, pkg := range toEnable {
		if c.DryRun {
			fmt.Fprintf(stdout, "would     pm enable --user 0 %s\n", pkg)
			continue
		}
		ok, message, err := device.Enable(pkg)
		if err != nil {
			return err
		}
		verdict := "ok"
		if !ok {
			verdict = "FAILED"
			failed++
		}
		fmt.Fprintf(stdout, "%-8s  %s  %s\n", verdict, pkg, message)
	}

	if failed > 0 {
		return fmt.Errorf("%d of %d did not re-enable", failed, len(toEnable))
	}
	return nil
}
