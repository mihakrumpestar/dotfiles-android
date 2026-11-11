package commands

import (
	"bytes"
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
	root := appTree(t, recipeApp("good", "a.b"))
	if err := os.WriteFile(filepath.Join(root, "good", "state.json"), []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}
	root2 := t.TempDir()
	captures := filepath.Join(root2, "capturer", "captures")
	if err := os.MkdirAll(captures, 0o755); err != nil {
		t.Fatal(err)
	}
	body := `{"package": "e.f", "backup": [{"capture": {"name": "main"}}]}`
	if err := os.WriteFile(filepath.Join(root2, "capturer", uiu.StepsFile), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(captures, "main.xml"), []byte("<x/>"), 0o644); err != nil {
		t.Fatal(err)
	}

	for _, test := range []struct {
		root string
		want string
	}{
		{root: root, want: "stored"},
		{root: root2, want: "captured"},
	} {
		var out bytes.Buffer
		old := stdout
		stdout = &out
		err := (&UIListCmd{appsFlag{Apps: test.root}}).Run(nil)
		stdout = old
		if err != nil {
			t.Fatalf("%s: %v", test.root, err)
		}
		if !strings.Contains(out.String(), test.want) {
			t.Fatalf("%s: want %q in:\n%s", test.root, test.want, out.String())
		}
	}
}
