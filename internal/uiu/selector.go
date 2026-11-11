package uiu

import (
	"encoding/xml"
	"errors"
	"fmt"
	"strconv"
	"strings"
)

// Rect is a node's screen bounds in pixels.
type Rect struct {
	X1, Y1, X2, Y2 int
}

// Centre is the point a tap on the bounds should use.
func (r Rect) Centre() (x, y int) {
	return (r.X1 + r.X2) / 2, (r.Y1 + r.Y2) / 2
}

// Contains reports whether the point is inside the rectangle.
func (r Rect) Contains(x, y int) bool {
	return x >= r.X1 && x <= r.X2 && y >= r.Y1 && y <= r.Y2
}

// Empty reports whether the bounds carry no area, which a malformed dump can
// produce and which must never win a "smallest" comparison.
func (r Rect) Empty() bool {
	return r.X2 <= r.X1 || r.Y2 <= r.Y1
}

// Area is the tap-target size; HitTarget prefers the smallest one.
func (r Rect) Area() int { return (r.X2 - r.X1) * (r.Y2 - r.Y1) }

// Node is one accessibility node from a uiautomator dump.
type Node struct {
	// Index is the node's position in document order, so a selector with an
	// index resolves the same way every time.
	Index   int
	Text    string
	ID      string
	Desc    string
	Class   string
	Checked bool
	// Checkable marks a settings row or a switch: the node that owns the state.
	Checkable bool
	Clickable bool
	Bounds    Rect
}

// xmlNode mirrors the dump element. Every attribute is a string because
// uiautomator writes booleans as text and a missing attribute must stay
// distinguishable from false.
type xmlNode struct {
	Text        string    `xml:"text,attr"`
	ResourceID  string    `xml:"resource-id,attr"`
	Class       string    `xml:"class,attr"`
	ContentDesc string    `xml:"content-desc,attr"`
	Checkable   string    `xml:"checkable,attr"`
	Checked     string    `xml:"checked,attr"`
	Clickable   string    `xml:"clickable,attr"`
	Bounds      string    `xml:"bounds,attr"`
	Children    []xmlNode `xml:"node"`
}

type hierarchy struct {
	Nodes []xmlNode `xml:"node"`
}

// ParseDump decodes a uiautomator dump. The device emits the whole hierarchy on
// one line, which encoding/xml accepts; the traversal is depth first and the
// resulting order is the document order selectors index into.
func ParseDump(body []byte) ([]Node, error) {
	var root hierarchy
	if err := xml.Unmarshal(body, &root); err != nil {
		return nil, fmt.Errorf("parsing dump: %w", err)
	}
	var nodes []Node
	var walk func(list []xmlNode)
	walk = func(list []xmlNode) {
		for _, item := range list {
			nodes = append(nodes, Node{
				Index:     len(nodes),
				Text:      item.Text,
				ID:        item.ResourceID,
				Desc:      item.ContentDesc,
				Class:     item.Class,
				Checkable: item.Checkable == "true",
				Checked:   item.Checked == "true",
				Clickable: item.Clickable == "true",
				Bounds:    parseBounds(item.Bounds),
			})
			walk(item.Children)
		}
	}
	walk(root.Nodes)
	if len(nodes) == 0 {
		return nil, errors.New("dump has no nodes")
	}
	return nodes, nil
}

// parseBounds reads the `[x1,y1][x2,y2]` form. A malformed value yields empty
// bounds rather than an error: one odd node must not fail a whole dump.
func parseBounds(value string) Rect {
	parts := strings.FieldsFunc(value, func(r rune) bool {
		return r == '[' || r == ']' || r == ','
	})
	if len(parts) != 4 {
		return Rect{}
	}
	values := make([]int, 4)
	for i, part := range parts {
		number, err := strconv.Atoi(part)
		if err != nil {
			return Rect{}
		}
		values[i] = number
	}
	return Rect{X1: values[0], Y1: values[1], X2: values[2], Y2: values[3]}
}

// match reports whether one node satisfies every field the selector sets.
func (s Selector) match(node Node) bool {
	if s.Text != "" && node.Text != s.Text {
		return false
	}
	if s.ID != "" && node.ID != s.ID {
		return false
	}
	if s.Desc != "" && node.Desc != s.Desc {
		return false
	}
	if s.TextContains != "" && !strings.Contains(node.Text, s.TextContains) {
		return false
	}
	if s.Class != "" && node.Class != s.Class {
		return false
	}
	return true
}

// Resolve returns the selector's node, or ErrNoMatch.
func Resolve(nodes []Node, sel Selector) (Node, error) {
	if sel.Empty() {
		return Node{}, fmt.Errorf("%w: empty selector", ErrNoMatch)
	}
	found := 0
	for _, node := range nodes {
		if !sel.match(node) {
			continue
		}
		if found == sel.Index {
			return node, nil
		}
		found++
	}
	return Node{}, fmt.Errorf("%w for %s (%d match(es))", ErrNoMatch, sel, found)
}

// HitTarget returns the node a tap on `node` should actually hit: the smallest
// checkable node containing the node's centre, else the smallest such clickable,
// else the node itself. A settings row reports state on its checkable ring while
// the label sits inside it, so the label is never the tap target.
//
// A row whose switch is a sibling to the right of the label (Yam Launcher's
// `Lock Settings`) is covered too: the switch is the checkable node sharing the
// label's row.
func HitTarget(nodes []Node, node Node) Node {
	x, y := node.Bounds.Centre()
	if target, ok := smallestCheckable(nodes, x, y); ok {
		return target
	}
	if target, ok := sameRowSwitch(nodes, node); ok {
		return target
	}
	if target, ok := smallestClickable(nodes, x, y); ok {
		return target
	}
	return node
}

func smallestCheckable(nodes []Node, x, y int) (Node, bool) {
	var best Node
	found := false
	for _, candidate := range nodes {
		if !candidate.Checkable || candidate.Bounds.Empty() || !candidate.Bounds.Contains(x, y) {
			continue
		}
		if !found || candidate.Bounds.Area() < best.Bounds.Area() {
			best, found = candidate, true
		}
	}
	return best, found
}

func smallestClickable(nodes []Node, x, y int) (Node, bool) {
	var best Node
	found := false
	for _, candidate := range nodes {
		if !candidate.Clickable || candidate.Bounds.Empty() || !candidate.Bounds.Contains(x, y) {
			continue
		}
		if !found || candidate.Bounds.Area() < best.Bounds.Area() {
			best, found = candidate, true
		}
	}
	return best, found
}

// sameRowSwitch finds a checkable node level with the label and to its right,
// picking the horizontally nearest one. Vertical containment keeps a switch from
// a different row out of the answer.
func sameRowSwitch(nodes []Node, label Node) (Node, bool) {
	_, y := label.Bounds.Centre()
	var best Node
	bestDistance := 0
	found := false
	for _, candidate := range nodes {
		if !candidate.Checkable || candidate.Bounds.Empty() {
			continue
		}
		if y < candidate.Bounds.Y1 || y > candidate.Bounds.Y2 {
			continue
		}
		if candidate.Bounds.X1 < label.Bounds.X2 {
			continue
		}
		distance := candidate.Bounds.X1 - label.Bounds.X2
		if !found || distance < bestDistance {
			best, bestDistance, found = candidate, distance, true
		}
	}
	return best, found
}

// Checked reads the state a selector points at: resolve the label, then report
// the switch that belongs to it.
func Checked(nodes []Node, sel Selector) (bool, error) {
	node, err := Resolve(nodes, sel)
	if err != nil {
		return false, err
	}
	return HitTarget(nodes, node).Checked, nil
}
