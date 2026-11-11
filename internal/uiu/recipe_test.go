package uiu

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// minimal is the smallest recipe that exports an artifact, used as the base for
// the mutation cases so each test changes one thing.
const minimal = `{
 "package": "eu.ottop.yamlauncher",
 "artifact": "yamlauncher.json",
 "backup": [
  {"launch": {"component": "eu.ottop.yamlauncher/.settings.SettingsActivity"}},
  {"tap": {"selector": {"text": "Backup"}}},
  {"pull": {"from": "/sdcard/Download/{{artifact}}", "to": "{{artifact}}"}}
 ],
 "restore": [
  {"push": {"from": "{{artifact}}", "to": "/sdcard/Download/{{artifact}}"}},
  {"tap": {"selector": {"textContains": "{{artifact}}"}}}
 ]
}`

func TestParse(t *testing.T) {
	tests := []struct {
		name    string
		body    string
		wantErr string
		check   func(*testing.T, Recipe)
	}{
		{
			name: "valid recipe",
			body: minimal,
			check: func(t *testing.T, r Recipe) {
				if r.Package != "eu.ottop.yamlauncher" || r.Artifact != "yamlauncher.json" {
					t.Fatalf("unexpected header: %+v", r)
				}
				if len(r.Backup) != 3 || len(r.Restore) != 2 {
					t.Fatalf("want 3 backup and 2 restore steps, got %d/%d", len(r.Backup), len(r.Restore))
				}
				if got := r.Backup[0].Kind; got != "launch" {
					t.Fatalf("first step kind %q", got)
				}
			},
		},
		{
			name:    "unknown step kind",
			body:    strings.Replace(minimal, `{"tap": {"selector": {"text": "Backup"}}},`, `{"flick": {}},`, 1),
			wantErr: `unknown key "flick"`,
		},
		{
			name:    "two actions in one step",
			body:    strings.Replace(minimal, `{"tap": {"selector": {"text": "Backup"}}},`, `{"tap": {"selector": {"text": "Backup"}}, "sleep": {"seconds": 1}},`, 1),
			wantErr: "two actions",
		},
		{
			name:    "empty selector",
			body:    strings.Replace(minimal, `{"selector": {"text": "Backup"}}`, `{"selector": {}}`, 1),
			wantErr: "selector names no field",
		},
		{
			name:    "pull in restore",
			body:    strings.Replace(minimal, `{"push": {"from": "{{artifact}}", "to": "/sdcard/Download/{{artifact}}"}},`, `{"pull": {"from": "/sdcard/x", "to": "x"}},`, 1),
			wantErr: "pull belongs in backup",
		},
		{
			name:    "push in backup",
			body:    strings.Replace(minimal, `{"pull": {"from": "/sdcard/Download/{{artifact}}", "to": "{{artifact}}"}}`, `{"push": {"from": "{{artifact}}", "to": "/sdcard/Download/{{artifact}}"}}`, 1),
			wantErr: "push belongs in restore",
		},
		{
			name:    "bad package id",
			body:    strings.Replace(minimal, `"eu.ottop.yamlauncher"`, `"eu.ottop.yamlauncher; rm -rf /"`, 1),
			wantErr: "invalid package",
		},
		{
			name:    "absolute local path",
			body:    strings.Replace(minimal, `"to": "{{artifact}}"`, `"to": "/tmp/{{artifact}}"`, 1),
			wantErr: "must be relative to the app directory",
		},
		{
			name:    "local path overwrites the recipe",
			body:    strings.Replace(minimal, `"to": "{{artifact}}"`, `"to": "steps.json"`, 1),
			wantErr: "would overwrite the recipe",
		},
		{
			name:    "pull target is not the artifact",
			body:    strings.Replace(minimal, `"to": "{{artifact}}"`, `"to": "other.json"`, 1),
			wantErr: "never pulls the artifact",
		},
		{
			name: "backup with nothing to show",
			body: `{
 "package": "a.b",
 "backup": [{"launch": {"component": "a.b/.Main"}}]
}`,
			wantErr: "neither exports nor captures anything",
		},
		{
			name:    "trailing data after the recipe",
			body:    minimal + "\n{}",
			wantErr: "trailing data",
		},
		{
			name:    "bad artifact name",
			body:    strings.Replace(minimal, `"artifact": "yamlauncher.json"`, `"artifact": "../yamlauncher.json"`, 1),
			wantErr: "invalid artifact",
		},
		{
			name:    "pull target escapes the app directory",
			body:    strings.Replace(minimal, `"to": "{{artifact}}"`, `"to": "../elsewhere/{{artifact}}"`, 1),
			wantErr: "leaves the app directory",
		},
		{
			name:    "push source escapes the app directory",
			body:    strings.Replace(minimal, `{"push": {"from": "{{artifact}}"`, `{"push": {"from": "../{{artifact}}"`, 1),
			wantErr: "leaves the app directory",
		},
		{
			name: "placeholder expansion",
			body: minimal,
			check: func(t *testing.T, r Recipe) {
				expanded := r.Expand()
				wantFrom := "/sdcard/Download/yamlauncher.json"
				if got := expanded.Backup[2].From; got != wantFrom {
					t.Fatalf("from = %q, want %q", got, wantFrom)
				}
				wantTo := "yamlauncher.json"
				if got := expanded.Backup[2].To; got != wantTo {
					t.Fatalf("to = %q, want %q", got, wantTo)
				}
				if got := expanded.Restore[0].From; got != wantTo {
					t.Fatalf("restore from = %q, want %q", got, wantTo)
				}
			},
		},
		{
			name: "placeholder survives parse",
			body: minimal,
			check: func(t *testing.T, r Recipe) {
				if !strings.Contains(r.Backup[2].To, "{{artifact}}") {
					t.Fatalf("parse expanded placeholders: %q", r.Backup[2].To)
				}
			},
		},
		{
			name: "optional tap parses",
			body: `{
 "package": "a.b",
 "artifact": "ab.json",
 "backup": [
  {"tap": {"selector": {"text": "Maybe"}, "optional": true}},
  {"pull": {"from": "/sdcard/ab.json", "to": "ab.json"}}
 ],
 "restore": [
  {"push": {"from": "ab.json", "to": "/sdcard/ab.json"}},
  {"type": {"text": "y"}}
 ]
}`,
			check: func(t *testing.T, r Recipe) {
				if !r.Backup[0].Optional {
					t.Fatal("optional tap lost its flag")
				}
			},
		},
		{
			name:    "optional on a non-tap step",
			body:    strings.Replace(minimal, `{"tap": {"selector": {"text": "Backup"}}},`, `{"key": {"code": "KEYCODE_BACK", "optional": true}},`, 1),
			wantErr: "optional is only valid on tap",
		},
		{
			name: "capture-only recipe with no restore",
			body: `{
 "package": "com.kitsumed.shizucallrecorder",
 "backup": [
  {"launch": {"component": "com.kitsumed.shizucallrecorder/.MainActivity"}},
  {"capture": {"name": "main"}},
  {"scroll": {"direction": "down"}},
  {"capture": {"name": "main-scrolled"}}
 ]
}`,
			check: func(t *testing.T, r Recipe) {
				if len(r.Restore) != 0 || r.Artifact != "" {
					t.Fatalf("capture-only recipe kept restore/artifact: %+v", r)
				}
				if r.Backup[2].Times != 1 {
					t.Fatalf("scroll times default = %d, want 1", r.Backup[2].Times)
				}
				if got := r.CaptureFolder(); got != "captures" {
					t.Fatalf("capture folder = %q, want captures", got)
				}
			},
		},
		{
			name: "artifact path follows the recipe's own directory",
			body: `{
 "package": "eu.ottop.yamlauncher",
 "artifact": "yamlauncher.json",
 "backup": [{"pull": {"from": "/sdcard/Download/yamlauncher.json", "to": "yamlauncher.json"}}],
 "restore": [{"push": {"from": "yamlauncher.json", "to": "/sdcard/Download/yamlauncher.json"}}]
}`,
			check: func(t *testing.T, r Recipe) {
				with := r.WithDir("policy/data/yamlauncher")
				if got := with.ArtifactPath(); got != "policy/data/yamlauncher/yamlauncher.json" {
					t.Fatalf("artifact path = %q", got)
				}
				if got := with.CaptureFolder(); got != "yamlauncher.d" {
					t.Fatalf("capture folder = %q", got)
				}
				if got := with.Name(); got != "yamlauncher" {
					t.Fatalf("name = %q", got)
				}
			},
		},
		{
			name: "artifact with nothing to export is rejected",
			body: `{
 "package": "com.kitsumed.shizucallrecorder",
 "artifact": "recorder.json",
 "backup": [{"launch": {"component": "com.kitsumed.shizucallrecorder/.MainActivity"}}]
}`,
			wantErr: "artifact is set but there is no restore flow",
		},
		{
			name:    "a removed field is an error, not ignored",
			body:    strings.Replace(minimal, `"artifact": "yamlauncher.json",`, `"name": "Yam Launcher", "artifact": "yamlauncher.json",`, 1),
			wantErr: `"name" is the directory name now`,
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			recipe, err := ParseBytes([]byte(test.body))
			if test.wantErr != "" {
				if err == nil {
					t.Fatalf("want error containing %q, got none", test.wantErr)
				}
				if !strings.Contains(err.Error(), test.wantErr) {
					t.Fatalf("error %q does not contain %q", err, test.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if test.check != nil {
				test.check(t, recipe)
			}
		})
	}
}

func TestParseDefaults(t *testing.T) {
	recipe, err := ParseBytes([]byte(`{
 "package": "a.b",
 "artifact": "ab.json",
 "backup": [
  {"wait": {"selector": {"text": "x"}}},
  {"scroll": {"direction": "up"}},
  {"pull": {"from": "/sdcard/ab.json", "to": "ab.json"}}
 ],
 "restore": [
  {"push": {"from": "ab.json", "to": "/sdcard/ab.json"}},
  {"type": {"text": "y"}}
 ]
}`))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got := recipe.Backup[0].Timeout; got != 10 {
		t.Fatalf("wait timeout default = %v, want 10", got)
	}
	if got := recipe.Backup[1].Times; got != 1 {
		t.Fatalf("scroll times default = %d, want 1", got)
	}
}

func TestShippedRecipesParse(t *testing.T) {
	paths, err := filepath.Glob(filepath.Join("..", "..", "policy", "data", "*", StepsFile))
	if err != nil {
		t.Fatal(err)
	}
	if len(paths) == 0 {
		t.Fatal("no shipped recipes found")
	}
	seen := map[string]string{}
	for _, path := range paths {
		recipe, err := Parse(path)
		if err != nil {
			t.Errorf("%s: %v", path, err)
			continue
		}
		if other, ok := seen[recipe.Package]; ok {
			t.Errorf("%s and %s both declare %s", other, path, recipe.Package)
		}
		seen[recipe.Package] = path
		for _, step := range append(append([]Step{}, recipe.Backup...), recipe.Restore...) {
			local := step.To
			if step.Kind == "push" {
				local = step.From
			}
			if step.Kind != "pull" && step.Kind != "push" {
				continue
			}
			if local == "" || filepath.IsAbs(local) || local == StepsFile {
				t.Errorf("%s: %s local path %q is not a blob in the app folder", path, step.Kind, local)
			}
		}
	}
}

func TestParseFromDisk(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, "policy", "data", "yamlauncher")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, StepsFile), []byte(minimal), 0o644); err != nil {
		t.Fatal(err)
	}

	recipe, err := Parse(filepath.Join(dir, StepsFile))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if recipe.Name() != "yamlauncher" {
		t.Fatalf("name = %q, want the directory name", recipe.Name())
	}
	if got, want := recipe.ArtifactPath(), filepath.Join(dir, "yamlauncher.json"); got != want {
		t.Fatalf("artifact path = %q, want %q", got, want)
	}

	// The blobs sit beside the recipe, so the recipe must be that one file.
	err = os.WriteFile(filepath.Join(dir, "yamlauncher.json"), []byte(minimal), 0o644)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := Parse(filepath.Join(dir, "yamlauncher.json")); err == nil || !strings.Contains(err.Error(), StepsFile) {
		t.Fatalf("a blob parsed as a recipe: %v", err)
	}

	// The folder name is a normalized app name: a display name here would make
	// the same app appear under two spellings in the tree.
	display := filepath.Join(root, "policy", "data", "Yam Launcher")
	if err := os.MkdirAll(display, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(display, StepsFile), []byte(minimal), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := Parse(filepath.Join(display, StepsFile)); err == nil || !strings.Contains(err.Error(), "normalized app name") {
		t.Fatalf("display-cased folder accepted: %v", err)
	}
}

func TestExpandDoesNotMutateTheRecipe(t *testing.T) {
	recipe, err := ParseBytes([]byte(`{
 "package": "a.b",
 "artifact": "ab.json",
 "backup": [
  {"wait": {"selector": {"textContains": "{{artifact}}"}}},
  {"pull": {"from": "/sdcard/ab.json", "to": "{{artifact}}"}}
 ],
 "restore": [{"push": {"from": "{{artifact}}", "to": "/sdcard/ab.json"}}]
}`))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	expanded := recipe.Expand()
	if got := expanded.Backup[0].Selector.TextContains; got != "ab.json" {
		t.Fatalf("expanded selector = %q, want ab.json", got)
	}
	if got := recipe.Backup[0].Selector.TextContains; got != "{{artifact}}" {
		t.Fatalf("Expand rewrote the original recipe: %q", got)
	}
}

func TestCanonicalMasksVolatile(t *testing.T) {
	stored := []byte(`{"app_id":"a.b","created_at":"2026-11-10T17:00:00+0100","data":{"k":{"value":1,"type":"Int"}}}`)
	fresh := []byte(`{"created_at":"2026-11-11T09:00:00+0100","data":{"k":{"value":1,"type":"Int"}},"app_id":"a.b"}`)
	volatile := []string{"created_at"}

	old := Canonical(stored, volatile)
	new := Canonical(fresh, volatile)
	if string(old) != string(new) {
		t.Fatalf("canonical forms differ despite only a volatile field changing:\n%s\n%s", old, new)
	}

	changed := []byte(`{"app_id":"a.b","created_at":"2026-11-11T09:00:00+0100","data":{"k":{"value":2,"type":"Int"}}}`)
	if string(Canonical(changed, volatile)) == string(old) {
		t.Fatal("canonical form hid a real change")
	}
}

func TestCanonicalPassesNonJSONThrough(t *testing.T) {
	body := []byte("not json\n")
	if got := string(Canonical(body, nil)); got != string(body) {
		t.Fatalf("non-JSON body changed: %q", got)
	}
}

func TestRedact(t *testing.T) {
	const export = `{
        "app_id": "a.b.example",
        "data": {
            "location": {"value": "latitude=1.5&longitude=2.5", "type": "String"},
            "locationRegion": {"value": "Fixture City", "type": "String"},
            "weatherEnabled": {"value": true, "type": "Boolean"}
        }
    }`

	tests := []struct {
		name   string
		fields []string
		body   string
		check  func(*testing.T, map[string]any)
	}{
		{
			name:   "nested values are emptied and the key stays",
			fields: []string{"data.location.value", "data.locationRegion.value"},
			body:   export,
			check: func(t *testing.T, data map[string]any) {
				for _, key := range []string{"location", "locationRegion"} {
					entry, ok := data[key].(map[string]any)
					if !ok {
						t.Fatalf("%s entry missing, so a restore would delete the key", key)
					}
					if got, want := entry["value"], ""; got != want {
						t.Fatalf("%s value = %v, want %q", key, got, want)
					}
					if got, want := entry["type"], "String"; got != want {
						t.Fatalf("%s type = %v, want %q", key, got, want)
					}
				}
				if got := data["weatherEnabled"].(map[string]any)["value"]; got != true {
					t.Fatalf("a field outside the redact list changed: %v", got)
				}
			},
		},
		{
			name:   "a field the export does not carry is not an error",
			fields: []string{"data.missing", "created_at"},
			body:   export,
			check: func(t *testing.T, data map[string]any) {
				if _, present := data["missing"]; present {
					t.Fatal("redact invented a key")
				}
			},
		},
		{
			name:   "no fields leaves the export alone",
			fields: nil,
			body:   export,
			check: func(t *testing.T, data map[string]any) {
				if got := data["locationRegion"].(map[string]any)["value"]; got != "Fixture City" {
					t.Fatalf("value = %v, want the export untouched", got)
				}
			},
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got := Redact([]byte(test.body), test.fields)
			var parsed struct {
				Data map[string]any `json:"data"`
			}
			if err := json.Unmarshal(got, &parsed); err != nil {
				t.Fatalf("redacted body is not JSON: %v\n%s", err, got)
			}
			test.check(t, parsed.Data)
		})
	}
}

func TestRedactPassesNonJSONThrough(t *testing.T) {
	body := []byte("not json\n")
	if got := string(Redact(body, []string{"data.location"})); got != string(body) {
		t.Fatalf("non-JSON body changed: %q", got)
	}
}

func TestResolveErrors(t *testing.T) {
	if _, err := Resolve(nil, Selector{}); !errors.Is(err, ErrNoMatch) {
		t.Fatalf("empty selector error = %v", err)
	}
	if _, err := Resolve(nil, Selector{Text: "x"}); !errors.Is(err, ErrNoMatch) {
		t.Fatalf("miss error = %v", err)
	}
}
