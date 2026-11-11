package uiu

import (
	"fmt"
	"strings"
)

// UnifiedDiff renders a line diff between two texts with three lines of context.
// It exists so `verify` can show what actually changed instead of only a hash
// mismatch; the artifacts are small, so an O(n*m) table is cheap.
func UnifiedDiff(name string, from, to []byte) string {
	oldLines := splitLines(string(from))
	newLines := splitLines(string(to))
	ops := diffOps(oldLines, newLines)
	return fmt.Sprintf("--- a/%s\n+++ b/%s\n%s", name, name, formatHunks(ops))
}

func splitLines(text string) []string {
	text = strings.TrimSuffix(text, "\n")
	if text == "" {
		return nil
	}
	return strings.Split(text, "\n")
}

type diffOp struct {
	// kind is ' ', '-' or '+'.
	kind byte
	line string
}

func diffOps(a, b []string) []diffOp {
	table := make([][]int, len(a)+1)
	for i := range table {
		table[i] = make([]int, len(b)+1)
	}
	for i := len(a) - 1; i >= 0; i-- {
		for j := len(b) - 1; j >= 0; j-- {
			if a[i] == b[j] {
				table[i][j] = table[i+1][j+1] + 1
				continue
			}
			table[i][j] = max(table[i+1][j], table[i][j+1])
		}
	}

	ops := make([]diffOp, 0, len(a)+len(b))
	i, j := 0, 0
	for i < len(a) && j < len(b) {
		switch {
		case a[i] == b[j]:
			ops = append(ops, diffOp{' ', a[i]})
			i++
			j++
		case table[i+1][j] >= table[i][j+1]:
			ops = append(ops, diffOp{'-', a[i]})
			i++
		default:
			ops = append(ops, diffOp{'+', b[j]})
			j++
		}
	}
	for ; i < len(a); i++ {
		ops = append(ops, diffOp{'-', a[i]})
	}
	for ; j < len(b); j++ {
		ops = append(ops, diffOp{'+', b[j]})
	}
	return ops
}

// context is how many unchanged lines surround a hunk.
const context = 3

func formatHunks(ops []diffOp) string {
	var out strings.Builder
	for start := 0; start < len(ops); {
		if ops[start].kind == ' ' {
			start++
			continue
		}
		// Grow the hunk to cover every change reachable within 2*context
		// unchanged lines, which merges changes the reader would see as one edit.
		end := start
		for end < len(ops) {
			if ops[end].kind != ' ' {
				end++
				continue
			}
			run := 0
			for end+run < len(ops) && ops[end+run].kind == ' ' {
				run++
			}
			if run > 2*context || end+run == len(ops) {
				break
			}
			end += run
		}

		first := max(start-context, 0)
		last := min(end+context, len(ops))
		// Recount the change lines to size the header.
		oldStart, newStart := 1, 1
		for _, op := range ops[:first] {
			if op.kind != '+' {
				oldStart++
			}
			if op.kind != '-' {
				newStart++
			}
		}
		oldCount, newCount := 0, 0
		for _, op := range ops[first:last] {
			if op.kind != '+' {
				oldCount++
			}
			if op.kind != '-' {
				newCount++
			}
		}
		fmt.Fprintf(&out, "@@ -%d,%d +%d,%d @@\n", oldStart, oldCount, newStart, newCount)
		for _, op := range ops[first:last] {
			out.WriteByte(op.kind)
			out.WriteString(op.line)
			out.WriteByte('\n')
		}
		start = last
	}
	return out.String()
}
