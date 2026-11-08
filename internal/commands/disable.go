package commands

import (
	"fmt"
	"os"

	"github.com/mihakrumpestar/dotfiles-android/internal/debloat"
)

// DisableCmd disables packages named in a file, or on stdin when no file is
// given, one at a time.
//
// Every line printed is the device's own message, verbatim. No summaries, no
// retries: a run that reports success while the device said otherwise is worse
// than no tool at all.
type DisableCmd struct {
	List       string `arg:"" optional:"" help:"File of package ids, one per line. Reads stdin when omitted." type:"path"`
	DryRun     bool   `help:"Print the command, change nothing."`
	NoSnapshot bool   `help:"Skip the pre-run snapshot because you already have one."`
	State      string `help:"Directory for snapshots." default:".state" type:"path"`
}

// Run implements the disable subcommand.
func (c *DisableCmd) Run(global *Globals) error {
	source := stdin
	if c.List != "" {
		file, err := os.Open(c.List)
		if err != nil {
			return fmt.Errorf("reading %s: %w", c.List, err)
		}
		defer file.Close()
		source = file
	}
	requested, err := readPackages(source)
	if err != nil {
		return err
	}
	if len(requested) == 0 {
		return fmt.Errorf("no packages to disable")
	}
	for _, pkg := range requested {
		if !debloat.ValidPackage(pkg) {
			return fmt.Errorf("not a package name: %q", pkg)
		}
	}

	device, err := global.Device()
	if err != nil {
		return err
	}
	states, err := device.States()
	if err != nil {
		return err
	}
	uids, _, err := device.PackageFacts()
	if err != nil {
		return err
	}

	if !c.DryRun && !c.NoSnapshot {
		path, err := debloat.WriteSnapshot(c.State, device.Serial, states)
		if err != nil {
			return err
		}
		fmt.Fprintf(stdout, "snapshot: %s\n", path)
	}

	failed := 0
	absent := 0
	for _, pkg := range requested {
		// Naming the shared uid is the one piece of context that turns a wrong
		// disable into an obvious one, so it goes on every line.
		note := ""
		if uid, ok := uids[pkg]; ok {
			note = "  [shares " + uid + "]"
		}

		// Absence is decided before the dry-run branch so a dry run predicts the
		// real run exactly. A preview that claims it would disable a package the
		// device does not have is worse than no preview.
		switch debloat.StateOf(states, pkg) {
		case debloat.Absent:
			// Not a failure: a policy list is written for a family of devices,
			// so a package that this firmware never shipped is expected. It is
			// reported because it means the list no longer matches this device,
			// but it must not make an otherwise clean run exit nonzero.
			fmt.Fprintf(stdout, "absent    %s  not installed\n", pkg)
			absent++
			continue
		case debloat.Disabled:
			fmt.Fprintf(stdout, "skipped   %s  already disabled\n", pkg)
			continue
		}

		if c.DryRun {
			fmt.Fprintf(stdout, "would     pm disable-user --user 0 %s%s\n", pkg, note)
			continue
		}

		ok, message, err := device.Disable(pkg)
		if err != nil {
			return err
		}
		verdict := "ok"
		if !ok {
			verdict = "FAILED"
			failed++
		}
		fmt.Fprintf(stdout, "%-8s  %s  %s%s\n", verdict, pkg, message, note)
	}

	if failed > 0 {
		return fmt.Errorf("%d of %d did not disable; read the messages above", failed, len(requested))
	}
	if absent > 0 {
		fmt.Fprintf(stdout, "\n%d package(s) are not on this device; trim the list if they are gone for good\n", absent)
	}
	return nil
}
