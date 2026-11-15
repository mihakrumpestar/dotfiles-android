package commands

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/mihakrumpestar/dotfiles-android/internal/uiu"
)

const testRecipe = `{
 "package": %q,
 "artifact": "state.json",
 "backup": [{"pull": {"from": "/sdcard/state.json", "to": "{{artifact}}"}}],
 "restore": [{"push": {"from": "{{artifact}}", "to": "/sdcard/state.json"}}]
}`

// appTree writes one app folder per name/package pair and returns the apps root.
func appTree(t *testing.T, apps ...struct{ name, pkg, body string }) string {
	t.Helper()
	root := t.TempDir()
	for _, app := range apps {
		dir := filepath.Join(root, app.name)
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, uiu.StepsFile), []byte(app.body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func recipeApp(name, pkg string) struct{ name, pkg, body string } {
	return struct{ name, pkg, body string }{name: name, pkg: pkg, body: strings.Replace(testRecipe, "%q", `"`+pkg+`"`, 1)}
}

func TestLoadFindsByDeclaredPackage(t *testing.T) {
	root := appTree(t, recipeApp("yamlauncher", "eu.ottop.yamlauncher"), recipeApp("shizucallrecorder", "com.kitsumed.shizucallrecorder"))
	flags := appsFlag{Apps: root}

	recipe, err := flags.load("com.kitsumed.shizucallrecorder")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if recipe.Name() != "shizucallrecorder" {
		t.Fatalf("name = %q", recipe.Name())
	}
	if _, err := flags.load("com.example.missing"); err == nil || !strings.Contains(err.Error(), "no recipe declares") {
		t.Fatalf("missing package error = %v", err)
	}
}

func TestLoadRejectsDuplicatePackage(t *testing.T) {
	root := appTree(t, recipeApp("one", "a.b"), recipeApp("two", "a.b"))
	_, err := (appsFlag{Apps: root}).load("a.b")
	if err == nil || !strings.Contains(err.Error(), "more than one recipe") {
		t.Fatalf("duplicate error = %v", err)
	}
}

func TestLoadIsStrictAboutBrokenSiblings(t *testing.T) {
	root := appTree(t,
		recipeApp("good", "a.b"),
		struct{ name, pkg, body string }{name: "broken", body: `{"package": "c.d"}`},
	)
	_, err := (appsFlag{Apps: root}).load("a.b")
	if err == nil || !strings.Contains(err.Error(), "broken") {
		t.Fatalf("want the broken recipe named, got %v", err)
	}
}

func TestListReportsGoodRecipesAndFailures(t *testing.T) {
	root := appTree(t,
		recipeApp("good", "a.b"),
		struct{ name, pkg, body string }{name: "broken", body: `{"package": "c.d"}`},
	)

	var out bytes.Buffer
	old := stdout
	stdout = &out
	defer func() { stdout = old }()

	err := (&UIListCmd{appsFlag{Apps: root}}).Run(nil)
	if err == nil {
		t.Fatal("a broken recipe must fail the command")
	}
	text := out.String()
	if !strings.Contains(text, "good") || !strings.Contains(text, "missing") {
		t.Fatalf("good recipe not listed:\n%s", text)
	}
	if !strings.Contains(text, "error") || !strings.Contains(text, "broken") {
		t.Fatalf("broken recipe not reported:\n%s", text)
	}
}

func TestListShowsStoredAndCaptured(t *testing.T) {
	// The recipe tree and the export tree are separate, and the listing has to
	// read the second one: an export beside its recipe would be committed.
	root := appTree(t, recipeApp("good", "a.b"))
	secrets := t.TempDir()
	if err := os.MkdirAll(filepath.Join(secrets, "good"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(secrets, "good", "state.json"), []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}

	root2 := t.TempDir()
	body := `{"package": "e.f", "backup": [{"capture": {"name": "main"}}]}`
	if err := os.MkdirAll(filepath.Join(root2, "capturer"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root2, "capturer", uiu.StepsFile), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	secrets2 := t.TempDir()
	captures := filepath.Join(secrets2, "capturer", "captures")
	if err := os.MkdirAll(captures, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(captures, "main.xml"), []byte("<x/>"), 0o644); err != nil {
		t.Fatal(err)
	}

	for _, test := range []struct {
		name    string
		root    string
		secrets string
		want    string
	}{
		{name: "stored", root: root, secrets: secrets, want: "stored"},
		{name: "captured", root: root2, secrets: secrets2, want: "captured"},
	} {
		var out bytes.Buffer
		old := stdout
		stdout = &out
		err := (&UIListCmd{appsFlag{Apps: test.root, Secrets: test.secrets}}).Run(nil)
		stdout = old
		if err != nil {
			t.Fatalf("%s: %v", test.name, err)
		}
		if !strings.Contains(out.String(), test.want) {
			t.Fatalf("%s: want %q in:\n%s", test.name, test.want, out.String())
		}
	}
}

func TestExportRootInsideTheRecipeTreeIsRefused(t *testing.T) {
	root := appTree(t, recipeApp("good", "a.b"))
	_, errs := appsFlag{Apps: root, Secrets: filepath.Join(root, "good")}.all()
	if len(errs) == 0 || !strings.Contains(errs[0].Error(), "must not sit where a commit can publish them") {
		t.Fatalf("an export root inside the recipe tree must be refused, got %v", errs)
	}
}

func probe(t *testing.T, nodes []uiu.Node, all bool) []string {
	t.Helper()
	var out bytes.Buffer
	old := stdout
	stdout = &out
	defer func() { stdout = old }()

	printProbe(nodes, "a.b/.Main", all)
	var lines []string
	for _, line := range strings.Split(out.String(), "\n") {
		if strings.HasPrefix(line, "screen") || strings.TrimSpace(line) == "" {
			continue
		}
		lines = append(lines, line)
	}
	return lines
}

// box builds a node with the given bounds, which the probe uses for the centre it
// taps and for spotting a container.
func box(x1, y1, x2, y2 int) uiu.Rect { return uiu.Rect{X1: x1, Y1: y1, X2: x2, Y2: y2} }

func TestProbeNamesControlsByTheirLabels(t *testing.T) {
	nodes := []uiu.Node{
		{Index: 0, ID: "a.b:id/root", Class: "android.widget.FrameLayout", Bounds: box(0, 0, 1080, 2340)},
		{Index: 1, Desc: "Settings", Class: "android.widget.Button", Clickable: true, Bounds: box(900, 100, 1060, 200)},
		{Index: 2, Text: "Exclude secrets", Class: "android.widget.TextView", Bounds: box(40, 300, 700, 400)},
		{Index: 3, Class: "android.widget.Switch", Checkable: true, Checked: true, Bounds: box(900, 300, 1060, 400)},
	}

	lines := probe(t, nodes, false)
	if len(lines) != 2 {
		t.Fatalf("want 2 controls, got %d:\n%s", len(lines), strings.Join(lines, "\n"))
	}
	if !strings.Contains(lines[0], `{"desc": "Settings"}`) {
		t.Fatalf("button not named by its desc: %s", lines[0])
	}
	// The label is a child of nothing; the switch is its sibling, so the row is
	// one control and the tap has to land on the switch that owns the state.
	if !strings.Contains(lines[1], `{"text": "Exclude secrets"}`) || !strings.Contains(lines[1], "switch, checked") {
		t.Fatalf("row did not resolve to its switch: %s", lines[1])
	}
}

func TestProbeCollapsesALabelOntoItsButton(t *testing.T) {
	// Flutter renders a label and the button it belongs to as siblings with the
	// same desc. One tap target, so one instruction, and no index to maintain.
	nodes := []uiu.Node{
		{Index: 0, Desc: "Export", Class: "android.view.View", Bounds: box(40, 100, 1000, 200)},
		{Index: 1, Desc: "Export", Class: "android.widget.Button", Clickable: true, Bounds: box(40, 100, 1000, 200)},
	}

	lines := probe(t, nodes, false)
	if len(lines) != 1 || !strings.Contains(lines[0], `{"desc": "Export"}`) {
		t.Fatalf("want one instruction naming the label, got:\n%s", strings.Join(lines, "\n"))
	}
}

func TestProbeTellsIdenticalControlsApartByIndex(t *testing.T) {
	// Two real controls share a label and a class, so only their order separates
	// them, and a selector must say which one it means.
	nodes := []uiu.Node{
		{Index: 0, Desc: "Delete", Class: "android.widget.Button", Clickable: true, Bounds: box(40, 400, 500, 500)},
		{Index: 1, Desc: "Delete", Class: "android.widget.Button", Clickable: true, Bounds: box(560, 400, 1040, 500)},
	}

	lines := probe(t, nodes, false)
	if len(lines) != 2 {
		t.Fatalf("want 2 controls, got:\n%s", strings.Join(lines, "\n"))
	}
	for index, line := range lines {
		if !strings.Contains(line, fmt.Sprintf(`"index": %d`, index)) {
			t.Fatalf("row %d carries no index: %s", index, line)
		}
	}
}

func TestProbeHidesStructureUnlessAsked(t *testing.T) {
	nodes := []uiu.Node{
		{Index: 0, ID: "a.b:id/root", Class: "android.widget.FrameLayout", Bounds: box(0, 0, 1080, 2340)},
		{Index: 1, ID: "a.b:id/icon", Class: "android.widget.ImageView", Bounds: box(40, 300, 120, 380)},
		{Index: 2, ID: "a.b:id/query", Class: "android.widget.EditText", Bounds: box(140, 300, 900, 380)},
	}

	lines := probe(t, nodes, false)
	if len(lines) != 1 || !strings.Contains(lines[0], "a.b:id/query") {
		t.Fatalf("want only the input field, got:\n%s", strings.Join(lines, "\n"))
	}
	if all := strings.Join(probe(t, nodes, true), "\n"); !strings.Contains(all, "a.b:id/icon") {
		t.Fatalf("--all must show the structure too, got:\n%s", all)
	}
}
