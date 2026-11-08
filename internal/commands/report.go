package commands

import (
	"fmt"
	"strings"
	"text/tabwriter"

	"github.com/mihakrumpestar/dotfiles-android/internal/debloat"
)

// ReportCmd prints one line per package with the facts that decide a keep/kill
// call. Read-only, and every package by default: hiding disabled ones would hide
// exactly the decisions worth reviewing.
type ReportCmd struct {
	JSON    bool   `help:"Emit JSON for an agent to read."`
	Enabled bool   `help:"Only packages that are currently enabled."`
	State   string `help:"Directory for cached ratings." default:".state" type:"path"`
}

// reportJSON is the row shape agents consume. Field names match the Python
// version so existing prompts keep working.
type reportJSON struct {
	Pkg         string `json:"pkg"`
	State       string `json:"state"`
	Tier        string `json:"tier"`
	SharedUser  string `json:"shared_user,omitempty"`
	TargetSDK   int    `json:"target_sdk,omitempty"`
	Rating      string `json:"rating,omitempty"`
	RatingList  string `json:"rating_list,omitempty"`
	Description string `json:"description,omitempty"`
	Role        string `json:"role,omitempty"`
}

// Run implements the report subcommand.
func (c *ReportCmd) Run(global *Globals) error {
	device, err := global.Device()
	if err != nil {
		return err
	}

	states, err := device.States()
	if err != nil {
		return err
	}
	uids, sdks, err := device.PackageFacts()
	if err != nil {
		return err
	}
	tiers, err := device.Tiers()
	if err != nil {
		return err
	}
	ratings, err := debloat.LoadRatings(c.State)
	if err != nil {
		return err
	}
	holders, err := device.RoleHolders()
	if err != nil {
		return err
	}

	roleOf := map[string]string{}
	for _, role := range sortedKeys(holders) {
		if holder := holders[role]; holder != "" {
			roleOf[holder] = role
		}
	}

	names := sortedKeys(states)
	rows := make([]reportJSON, 0, len(names))
	enabled := 0
	for _, name := range names {
		if states[name] == debloat.Enabled {
			enabled++
		}
		if c.Enabled && states[name] != debloat.Enabled {
			continue
		}
		rating := ratings.ByPackage[name]
		description := rating.Description
		if first, _, found := strings.Cut(description, "\n"); found {
			description = first
		}
		rows = append(rows, reportJSON{
			Pkg:         name,
			State:       states[name],
			Tier:        tiers[name],
			SharedUser:  uids[name],
			TargetSDK:   sdks[name],
			Rating:      rating.Removal,
			RatingList:  rating.List,
			Description: truncate(description, 120),
			Role:        roleOf[name],
		})
	}

	if c.JSON {
		return printJSON(rows)
	}

	fmt.Fprintf(stdout, "# %s: %d shown, %d enabled\n", device.Serial, len(rows), enabled)
	// Name the branch, so a stale list is visible rather than assumed.
	fmt.Fprintf(stdout, "# ratings: %s (%s)\n", ratings, debloat.UADBranch)

	table := tabwriter.NewWriter(stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(table, "state\ttier\tshared uid\tsdk\trating\trole\tpackage")
	for _, row := range rows {
		fmt.Fprintf(
			table, "%s\t%s\t%s\t%s\t%s\t%s\t%s\n",
			row.State, row.Tier, orDash(row.SharedUser), orDashInt(row.TargetSDK),
			orDash(row.Rating), orDash(row.Role), row.Pkg,
		)
	}
	return table.Flush()
}

// truncate cuts s to n runes, so a multi-byte description is never split.
func truncate(s string, n int) string {
	runes := []rune(s)
	if len(runes) <= n {
		return s
	}
	return string(runes[:n])
}

func orDash(value string) string {
	if value == "" {
		return "-"
	}
	return value
}

func orDashInt(value int) string {
	if value == 0 {
		return "-"
	}
	return fmt.Sprint(value)
}
