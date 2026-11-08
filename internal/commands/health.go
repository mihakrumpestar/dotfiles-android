package commands

import (
	"fmt"

	"github.com/mihakrumpestar/dotfiles-android/internal/debloat"
)

// HealthCmd checks a device still works. Read-only.
//
// This proves the package manager is self-consistent. It does not prove the
// camera still previews or that a call connects, so say what you actually tested.
type HealthCmd struct{}

// Run implements the health subcommand.
func (c *HealthCmd) Run(global *Globals) error {
	device, err := global.Device()
	if err != nil {
		return err
	}

	states, err := device.States()
	if err != nil {
		return err
	}
	holders, err := device.RoleHolders()
	if err != nil {
		return err
	}
	launchers, err := device.Launchers()
	if err != nil {
		return err
	}
	keyboard, err := device.Keyboard()
	if err != nil {
		return err
	}
	crashes, err := device.Crashes()
	if err != nil {
		return err
	}
	booted, err := device.BootCompleted()
	if err != nil {
		return err
	}

	var active []string
	for _, pkg := range launchers {
		if states[pkg] != debloat.Disabled {
			active = append(active, pkg)
		}
	}

	var problems []string
	if !booted {
		problems = append(problems, "boot not completed")
	}
	if len(active) == 0 {
		problems = append(problems, "no enabled launcher: no home screen")
	}
	if keyboard != "" && states[keyboard] == debloat.Disabled {
		problems = append(problems, "default keyboard "+keyboard+" is disabled")
	}
	for _, role := range sortedKeys(holders) {
		if holder := holders[role]; holder != "" && states[holder] == debloat.Disabled {
			problems = append(problems, role+" role is held by disabled "+holder)
		}
	}

	fmt.Fprintf(stdout, "launcher   %s\n", joinOr(active, "none"))
	fmt.Fprintf(stdout, "keyboard   %s\n", orUnset(keyboard))
	for _, role := range sortedKeys(holders) {
		fmt.Fprintf(stdout, "%-10s %s\n", lower(role), orUnset(holders[role]))
	}
	// The crash buffer is historical: one old crash would fail every run until
	// reboot, so it is reported and not judged.
	fmt.Fprintf(stdout, "crashes    %d\n", crashes)

	if len(problems) > 0 {
		fmt.Fprintf(stdout, "\n%d problem(s):\n", len(problems))
		for _, problem := range problems {
			fmt.Fprintf(stdout, "  %s\n", problem)
		}
		return fmt.Errorf("health check failed")
	}
	fmt.Fprintln(stdout, "\nok")
	return nil
}
