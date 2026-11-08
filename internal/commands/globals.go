// Package commands holds one type per CLI subcommand. Each Run does its own work
// and returns an error; main only wires and exits non-zero.
package commands

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"slices"
	"strings"

	"github.com/mihakrumpestar/dotfiles-android/internal/debloat"
)

// Globals are the flags every subcommand shares.
type Globals struct {
	Serial string `help:"adb serial; defaults to the only attached device."`
}

// Device resolves the handle, honouring --serial.
func (g *Globals) Device() (*debloat.Device, error) {
	return debloat.NewDevice(g.Serial)
}

// stdout exists so tests can swap it without touching the real stream.
var stdout io.Writer = os.Stdout

// stdin exists for the same reason.
var stdin io.Reader = os.Stdin

func printJSON(value any) error {
	body, err := json.MarshalIndent(value, "", " ")
	if err != nil {
		return err
	}
	_, err = fmt.Fprintln(stdout, string(body))
	return err
}

// sortedKeys returns the map's keys in order, so output is stable.
func sortedKeys[V any](values map[string]V) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	slices.Sort(keys)
	return keys
}

// readPackages reads package ids from r, one per line, skipping blanks and
// comments.
func readPackages(r io.Reader) ([]string, error) {
	var packages []string
	scanner := bufio.NewScanner(r)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		packages = append(packages, line)
	}
	return packages, scanner.Err()
}

func orUnset(value string) string {
	if value == "" {
		return "unset"
	}
	return value
}

func joinOr(values []string, fallback string) string {
	if len(values) == 0 {
		return fallback
	}
	return strings.Join(values, ", ")
}

func lower(value string) string {
	return strings.ToLower(value)
}
