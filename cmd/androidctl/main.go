// Command androidctl applies declarative Android device state over adb.
// This file is kong wiring only.
package main

import (
	"fmt"
	"os"

	"github.com/alecthomas/kong"
	"github.com/mihakrumpestar/dotfiles-android/internal/commands"
)

// CLI is the kong command tree.
type CLI struct {
	Globals   commands.Globals      `embed:""`
	Report    commands.ReportCmd    `cmd:"" help:"Every package with the facts that decide a keep/kill call."`
	Disable   commands.DisableCmd   `cmd:"" help:"Disable packages from a file, or stdin when omitted."`
	Restore   commands.RestoreCmd   `cmd:"" help:"Re-enable everything the newest snapshot recorded as enabled."`
	Health    commands.HealthCmd    `cmd:"" help:"Check boot, launcher, keyboard, roles, crash buffer."`
	Obtainium commands.ObtainiumCmd `cmd:"" help:"Install Obtainium from its GitHub release, checksum verified."`
	Apply     commands.ApplyCmd     `cmd:"" help:"Apply policy/profile.conf and policy/apps.conf. Dry run unless --yes."`
	Verify    commands.VerifyCmd    `cmd:"" help:"Report drift between the device and both profiles."`
	Revert    commands.RevertCmd    `cmd:"" help:"Restore the values captured before the last apply."`
	UI        commands.UICmd        `cmd:"" help:"Replay a declarative UI recipe: list, backup, restore, verify."`
}

func main() {
	var cli CLI

	parser, err := kong.New(&cli,
		kong.Name("androidctl"),
		kong.Description("Apply declarative Android device state over adb."),
	)
	if err != nil {
		fmt.Fprintln(os.Stderr, "androidctl:", err)
		os.Exit(2)
	}

	ctx, err := parser.Parse(os.Args[1:])
	if err != nil {
		fmt.Fprintln(os.Stderr, "androidctl:", err)
		os.Exit(2)
	}

	if err := ctx.Run(&cli.Globals); err != nil {
		fmt.Fprintln(os.Stderr, "androidctl:", err)
		os.Exit(1)
	}
}
