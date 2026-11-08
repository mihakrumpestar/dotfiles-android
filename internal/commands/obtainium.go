package commands

import (
	"fmt"
	"strings"

	"github.com/mihakrumpestar/dotfiles-android/internal/debloat"
	"github.com/mihakrumpestar/dotfiles-android/internal/obtainium"
)

// ObtainiumCmd installs Obtainium, which then owns every other app. There is no
// app manifest here: Obtainium's export is the declarative artifact.
type ObtainiumCmd struct {
	Asset  string `help:"Override the release asset name." default:""`
	DryRun bool   `help:"Resolve and verify the asset, install nothing."`
	State  string `help:"Directory for downloads." default:".state" type:"path"`
}

// Run installs Obtainium.
func (c *ObtainiumCmd) Run(global *Globals) error {
	device, err := global.Device()
	if err != nil {
		return err
	}

	release, err := obtainium.LatestRelease(obtainium.Client, obtainium.ReleaseURL)
	if err != nil {
		return err
	}

	assetName := c.Asset
	if assetName == "" {
		// Pick by the device's own ABI list rather than guessing the host's.
		code, out, _ := device.Shell("getprop ro.product.cpu.abilist")
		if code != 0 || strings.TrimSpace(out) == "" {
			return fmt.Errorf("could not read ro.product.cpu.abilist from the device")
		}
		assetName = obtainium.AssetFor(strings.Split(strings.TrimSpace(out), ","))
	}

	apk, err := release.Find(assetName)
	if err != nil {
		return err
	}
	checksumAsset, err := release.FindChecksum(apk)
	if err != nil {
		return err
	}
	want, err := obtainium.ExpectedHash(obtainium.Client, checksumAsset)
	if err != nil {
		return err
	}

	fmt.Fprintln(stdout, obtainium.Describe(release, apk, want))

	if c.DryRun {
		fmt.Fprintln(stdout, "\ndry run: nothing downloaded, nothing installed")
		return nil
	}

	path, err := obtainium.Download(obtainium.Client, apk, want, c.State+"/obtainium")
	if err != nil {
		return err
	}
	fmt.Fprintf(stdout, "verified  %s\n", path)

	if _, err := device.Run("install", "-r", path); err != nil {
		return err
	}

	states, err := device.States()
	if err != nil {
		return err
	}
	if debloat.StateOf(states, obtainium.PackageID) != debloat.Enabled {
		return fmt.Errorf("install reported success but %s is not enabled", obtainium.PackageID)
	}

	fmt.Fprintf(stdout, "installed %s\n", obtainium.PackageID)
	fmt.Fprintln(stdout, "\nNext: open Obtainium and import your export (Settings > Import/Export).")
	fmt.Fprintln(stdout, "That export is the app list; this repo does not keep a second copy.")
	return nil
}
