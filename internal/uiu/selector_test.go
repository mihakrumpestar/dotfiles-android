package uiu

import (
	"errors"
	"fmt"
	"strings"
	"testing"
)

// fixture is a slice of the two screens the layer has to understand: a settings
// page whose row label sits inside a checkable switch, and a SAF dialog whose
// file rows and file-name field share one id.
const fixture = `<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation="0">
<node index="0" text="" resource-id="" class="android.widget.FrameLayout" content-desc="" checkable="false" checked="false" clickable="false" bounds="[0,0][1080,2340]">
 <node index="0" text="" resource-id="" class="android.widget.LinearLayout" content-desc="" checkable="false" checked="false" clickable="true" bounds="[0,437][1080,588]">
  <node index="0" text="Lock Settings" resource-id="android:id/title" class="android.widget.TextView" content-desc="" checkable="false" checked="false" clickable="false" bounds="[203,482][482,543]">
   <node index="0" text="" resource-id="android:id/switch_widget" class="android.widget.Switch" content-desc="" checkable="true" checked="true" clickable="false" bounds="[203,474][482,550]"/>
  </node>
 </node>
 <node index="1" text="Record third-party apps" resource-id="android:id/title" class="android.widget.TextView" content-desc="" checkable="false" checked="false" clickable="false" bounds="[203,700][700,760]">
  <node index="0" text="" resource-id="android:id/switch_widget" class="android.widget.Switch" content-desc="" checkable="true" checked="false" clickable="false" bounds="[903,692][1035,768]"/>
 </node>
 <node index="2" text="Backup" resource-id="android:id/title" class="android.widget.TextView" content-desc="" checkable="false" checked="false" clickable="false" bounds="[203,1881][354,1942]"/>
 <node index="3" text="Backup and Restore" resource-id="android:id/title" class="android.widget.TextView" content-desc="" checkable="false" checked="false" clickable="false" bounds="[203,1760][1035,1813]"/>
 <node index="4" text="SAVE" resource-id="android:id/button1" class="android.widget.Button" content-desc="" checkable="false" checked="false" clickable="true" bounds="[793,2140][1041,2275]"/>
 <node index="5" text="New folder" resource-id="" class="android.widget.ImageButton" content-desc="New folder" checkable="false" checked="false" clickable="true" bounds="[809,101][945,236]"/>
 <node index="6" text="yamlauncher.json" resource-id="android:id/title" class="android.widget.EditText" content-desc="" checkable="false" checked="false" clickable="true" bounds="[203,2174][782,2264]"/>
 <node index="7" text="yamlauncher.json" resource-id="android:id/title" class="android.widget.TextView" content-desc="" checkable="false" checked="false" clickable="true" bounds="[208,1327][495,1380]"/>
</node>
</hierarchy>`

func TestParseDump(t *testing.T) {
	nodes, err := ParseDump([]byte(fixture))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(nodes) != 12 {
		t.Fatalf("got %d nodes, want 12", len(nodes))
	}
	for i, node := range nodes {
		if node.Index != i {
			t.Fatalf("node %d has index %d", i, node.Index)
		}
	}
	if nodes[2].Text != "Lock Settings" || nodes[2].ID != "android:id/title" {
		t.Fatalf("unexpected label node: %+v", nodes[2])
	}
	if !nodes[3].Checkable || !nodes[3].Checked {
		t.Fatalf("switch state not parsed: %+v", nodes[3])
	}
	if got := nodes[0].Bounds; got != (Rect{0, 0, 1080, 2340}) {
		t.Fatalf("bounds = %+v", got)
	}
}

func TestParseDumpRejectsGarbage(t *testing.T) {
	if _, err := ParseDump([]byte("not xml")); err == nil {
		t.Fatal("want error for non-XML input")
	}
	if _, err := ParseDump([]byte("<hierarchy/>")); err == nil {
		t.Fatal("want error for a dump with no nodes")
	}
}

func TestResolve(t *testing.T) {
	nodes := mustParse(t, fixture)
	tests := []struct {
		name    string
		sel     Selector
		wantErr bool
		check   func(*testing.T, Node)
	}{
		{
			name: "by text",
			sel:  Selector{Text: "Lock Settings"},
			check: func(t *testing.T, n Node) {
				if n.ID != "android:id/title" {
					t.Fatalf("id = %q", n.ID)
				}
			},
		},
		{
			name: "by id",
			sel:  Selector{ID: "android:id/button1"},
			check: func(t *testing.T, n Node) {
				if n.Text != "SAVE" {
					t.Fatalf("text = %q", n.Text)
				}
			},
		},
		{
			name: "by desc",
			sel:  Selector{Desc: "New folder"},
			check: func(t *testing.T, n Node) {
				if !n.Clickable {
					t.Fatal("desc node is not clickable")
				}
			},
		},
		{
			name: "by textContains",
			sel:  Selector{TextContains: "third-party"},
			check: func(t *testing.T, n Node) {
				if n.Text != "Record third-party apps" {
					t.Fatalf("text = %q", n.Text)
				}
			},
		},
		{
			name: "index picks the second of two",
			sel:  Selector{Text: "yamlauncher.json", Index: 1},
			check: func(t *testing.T, n Node) {
				if n.Class != "android.widget.TextView" {
					t.Fatalf("class = %q", n.Class)
				}
				if n.Index != 11 {
					t.Fatalf("document index = %d, want 11", n.Index)
				}
			},
		},
		{
			name: "id plus class names the field",
			sel:  Selector{ID: "android:id/title", Class: "android.widget.EditText"},
			check: func(t *testing.T, n Node) {
				if n.Text != "yamlauncher.json" || n.Index != 10 {
					t.Fatalf("unexpected node: %+v", n)
				}
			},
		},
		{
			name:    "miss",
			sel:     Selector{Text: "Nonexistent"},
			wantErr: true,
		},
		{
			name:    "index past the end",
			sel:     Selector{Text: "yamlauncher.json", Index: 2},
			wantErr: true,
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			node, err := Resolve(nodes, test.sel)
			if test.wantErr {
				if !errors.Is(err, ErrNoMatch) {
					t.Fatalf("error = %v, want ErrNoMatch", err)
				}
				return
			}
			if err != nil {
				t.Fatalf("resolve: %v", err)
			}
			test.check(t, node)
		})
	}
}

func TestHitTarget(t *testing.T) {
	nodes := mustParse(t, fixture)
	label, err := Resolve(nodes, Selector{Text: "Record third-party apps"})
	if err != nil {
		t.Fatal(err)
	}
	target := HitTarget(nodes, label)
	if !target.Checkable {
		t.Fatalf("hit target is not checkable: %+v", target)
	}
	if target.Bounds != (Rect{903, 692, 1035, 768}) {
		t.Fatalf("hit target bounds = %+v, want the switch", target.Bounds)
	}

	// A row whose switch is a child of the label is covered by containment.
	childLabel, err := Resolve(nodes, Selector{Text: "Lock Settings"})
	if err != nil {
		t.Fatal(err)
	}
	childTarget := HitTarget(nodes, childLabel)
	if !childTarget.Checkable || !childTarget.Checked {
		t.Fatalf("containment rule picked %+v", childTarget)
	}

	// A plain row with no switch falls back to the smallest clickable, then to
	// the node itself.
	row, err := Resolve(nodes, Selector{Text: "Backup"})
	if err != nil {
		t.Fatal(err)
	}
	if got := HitTarget(nodes, row); got.Index != row.Index {
		t.Fatalf("unrelated label hit %+v", got)
	}
}

func TestChecked(t *testing.T) {
	nodes := mustParse(t, fixture)
	tests := []struct {
		sel  Selector
		want bool
	}{
		{Selector{Text: "Record third-party apps"}, false},
		{Selector{Text: "Lock Settings"}, true},
	}
	for _, test := range tests {
		got, err := Checked(nodes, test.sel)
		if err != nil {
			t.Fatalf("checked %s: %v", test.sel, err)
		}
		if got != test.want {
			t.Fatalf("checked %s = %t, want %t", test.sel, got, test.want)
		}
	}
	if _, err := Checked(nodes, Selector{Text: "Nonexistent"}); !errors.Is(err, ErrNoMatch) {
		t.Fatalf("miss error = %v", err)
	}
}

func TestHitTargetSiblingSwitch(t *testing.T) {
	// Yam Launcher's own prefs rows put the switch in a sibling container to the
	// right, so the label's centre is not inside the switch; the row rule has to
	// find it anyway.
	const rows = `<hierarchy rotation="0">
<node index="0" text="" resource-id="" class="android.widget.LinearLayout" content-desc="" checkable="false" checked="false" clickable="true" bounds="[0,437][1080,588]">
 <node index="0" text="Lock Settings" resource-id="android:id/title" class="android.widget.TextView" content-desc="" checkable="false" checked="false" clickable="false" bounds="[203,482][482,543]"/>
 <node index="1" text="" resource-id="android:id/switch_widget" class="android.widget.Switch" content-desc="" checkable="true" checked="true" clickable="false" bounds="[903,474][1035,550]"/>
</node>
<node index="1" text="Backup" resource-id="android:id/title" class="android.widget.TextView" content-desc="" checkable="false" checked="false" clickable="false" bounds="[203,1881][354,1942]"/>
</hierarchy>`
	nodes := mustParse(t, rows)
	label, err := Resolve(nodes, Selector{Text: "Lock Settings"})
	if err != nil {
		t.Fatal(err)
	}
	target := HitTarget(nodes, label)
	if !target.Checkable || !target.Checked {
		t.Fatalf("row rule picked %+v", target)
	}
	checked, err := Checked(nodes, Selector{Text: "Lock Settings"})
	if err != nil {
		t.Fatal(err)
	}
	if !checked {
		t.Fatal("checked = false, want the switch state")
	}
}

func TestUnifiedDiff(t *testing.T) {
	from := []byte("{\n \"a\": 1,\n \"b\": 2,\n \"c\": 3\n}\n")
	to := []byte("{\n \"a\": 1,\n \"b\": 20,\n \"c\": 3\n}\n")
	diff := UnifiedDiff("x.json", from, to)
	for _, want := range []string{"--- a/x.json", "+++ b/x.json", `- "b": 2`, `+ "b": 20`} {
		if !contains(diff, want) {
			t.Fatalf("diff missing %q:\n%s", want, diff)
		}
	}
	if UnifiedDiff("x.json", from, from) != "--- a/x.json\n+++ b/x.json\n" {
		t.Fatal("identical inputs produced a hunk")
	}
}

func TestUnifiedDiffSeparatesDistantChanges(t *testing.T) {
	var old, new strings.Builder
	old.WriteString("{\n")
	new.WriteString("{\n")
	for i := range 30 {
		line := fmt.Sprintf(" \"k%d\": %d,\n", i, i)
		if i == 1 {
			old.WriteString(fmt.Sprintf(" \"k%d\": %d,\n", i, i))
			new.WriteString(fmt.Sprintf(" \"k%d\": %d,\n", i, 99))
			continue
		}
		if i == 25 {
			old.WriteString(fmt.Sprintf(" \"k%d\": %d,\n", i, i))
			new.WriteString(fmt.Sprintf(" \"k%d\": %d,\n", i, 77))
			continue
		}
		old.WriteString(line)
		new.WriteString(line)
	}
	old.WriteString("}\n")
	new.WriteString("}\n")

	diff := UnifiedDiff("x.json", []byte(old.String()), []byte(new.String()))
	if got := strings.Count(diff, "@@ "); got != 2 {
		t.Fatalf("want 2 hunks, got %d:\n%s", got, diff)
	}
	if strings.Count(diff, `- "k1": 1,`) != 1 || strings.Count(diff, `- "k25": 25,`) != 1 {
		t.Fatalf("hunks do not contain both changes:\n%s", diff)
	}
}

func mustParse(t *testing.T, body string) []Node {
	t.Helper()
	nodes, err := ParseDump([]byte(body))
	if err != nil {
		t.Fatalf("parse fixture: %v", err)
	}
	return nodes
}

func contains(haystack, needle string) bool {
	return len(needle) == 0 || (len(haystack) >= len(needle) && indexOf(haystack, needle) >= 0)
}

func indexOf(haystack, needle string) int {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return i
		}
	}
	return -1
}
