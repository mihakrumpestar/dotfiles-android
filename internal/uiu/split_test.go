package uiu

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const splitFixture = `{
 "app_id": "eu.ottop.yamlauncher",
 "created_at": "2026-11-11T22:15:33",
 "data": {
  "location": {"value": "latitude=12.345&longitude=67.89", "type": "String"},
  "locationRegion": {"value": "Fixture City", "type": "String"},
  "clockSeconds": {"value": true, "type": "Boolean"}
 }
}`

var splitFields = []string{"data.location.value", "data.locationRegion.value"}

func mustSplit(t *testing.T, body []byte, fields []string) ([]byte, []byte) {
	t.Helper()
	public, private, err := splitExport(body, fields)
	if err != nil {
		t.Fatalf("split: %v", err)
	}
	return public, private
}

func TestSplitKeepsThePrivateValuesOutOfTheArtifact(t *testing.T) {
	public, private := mustSplit(t, []byte(splitFixture), splitFields)
	if private == nil {
		t.Fatal("no overlay was produced")
	}
	if strings.Contains(string(public), "12.345") || strings.Contains(string(public), "Fixture City") {
		t.Fatalf("private value survived in the artifact:\n%s", public)
	}
	if !strings.Contains(string(public), "clockSeconds") {
		t.Fatalf("the artifact lost its public content:\n%s", public)
	}

	var overlay map[string]any
	if err := json.Unmarshal(private, &overlay); err != nil {
		t.Fatalf("overlay is not JSON: %v\n%s", err, private)
	}
	// The overlay holds the private paths and nothing else, so reading it tells
	// you exactly what is not published.
	if _, present := overlay["app_id"]; present {
		t.Fatalf("overlay carries a public field:\n%s", private)
	}
	if !strings.Contains(string(private), "12.345") || !strings.Contains(string(private), "Fixture City") {
		t.Fatalf("overlay lost the values it exists for:\n%s", private)
	}

	// The zeroed half must be byte-identical to what a redacted pull produces,
	// so moving a path from redact to private does not rewrite the artifact.
	if got, want := string(public), string(Redact([]byte(splitFixture), splitFields)); got != want {
		t.Fatalf("artifact differs from a redacted export:\n got %q\nwant %q", got, want)
	}
}

func TestSplitAndMergeRoundTrip(t *testing.T) {
	public, private := mustSplit(t, []byte(splitFixture), splitFields)
	merged, err := mergeExport(public, private, splitFields)
	if err != nil {
		t.Fatalf("merge: %v", err)
	}

	var want, got any
	if err := json.Unmarshal([]byte(splitFixture), &want); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(merged, &got); err != nil {
		t.Fatalf("merged body is not JSON: %v\n%s", err, merged)
	}
	if !equalJSON(want, got) {
		t.Fatalf("merge did not restore the export:\n%s", merged)
	}
}

func TestPrivateCanEmptyAContainer(t *testing.T) {
	// A path may name a whole list, and then the list itself has to leave the
	// artifact: zeroing only its scalars would publish everything it holds.
	body := []byte(`{"apps": [{"id": "a"}, {"id": "b"}], "keep": 1}`)
	public, private := mustSplit(t, body, []string{"apps"})

	if strings.Contains(string(public), `"a"`) || strings.Contains(string(public), `"b"`) {
		t.Fatalf("a private list survived in the artifact:\n%s", public)
	}
	if !strings.Contains(string(private), `"a"`) {
		t.Fatalf("the overlay did not take the list:\n%s", private)
	}
	merged, err := mergeExport(public, private, []string{"apps"})
	if err != nil {
		t.Fatalf("merge: %v", err)
	}
	if !strings.Contains(string(merged), `"b"`) {
		t.Fatalf("the list did not come back on merge:\n%s", merged)
	}
}

func TestIdentitySelectorMovesOneEntry(t *testing.T) {
	// The point of the identity form: only the named entry leaves the artifact,
	// so the rest of the list stays published.
	body := []byte(`{"apps": [{"id": "bank", "name": "Bank"}, {"id": "music", "name": "Music"}, {"id": "chat", "name": "Chat"}]}`)
	public, private := mustSplit(t, body, []string{"apps[id=bank]"})

	if strings.Contains(string(public), "bank") {
		t.Fatalf("the named entry stayed in the artifact:\n%s", public)
	}
	for _, keep := range []string{"music", "chat"} {
		if !strings.Contains(string(public), keep) {
			t.Fatalf("%s was removed too:\n%s", keep, public)
		}
	}
	if !strings.Contains(string(private), "bank") {
		t.Fatalf("overlay does not hold the entry:\n%s", private)
	}

	merged, err := mergeExport(public, private, []string{"apps[id=bank]"})
	if err != nil {
		t.Fatalf("merge: %v", err)
	}
	if !strings.Contains(string(merged), "bank") || !strings.Contains(string(merged), "music") {
		t.Fatalf("merge did not put the entry back:\n%s", merged)
	}
}

func TestIdentitySelectorReplacingAnEntryDoesNotDuplicate(t *testing.T) {
	public := []byte(`{"apps": [{"id": "bank", "name": "Old"}, {"id": "music", "name": "Music"}]}`)
	overlay := []byte(`{"apps": [{"id": "bank", "name": "New"}]}`)
	merged, err := mergeExport(public, overlay, []string{"apps[id=bank]"})
	if err != nil {
		t.Fatalf("merge: %v", err)
	}
	if strings.Contains(string(merged), "Old") {
		t.Fatalf("the stale copy survived:\n%s", merged)
	}
	if strings.Count(string(merged), `"id": "bank"`) != 1 {
		t.Fatalf("the entry was duplicated:\n%s", merged)
	}
}

func TestIdentitySelectorThatMatchesNothingIsAnError(t *testing.T) {
	// Publishing the entry a recipe meant to hide is the one outcome to refuse.
	body := []byte(`{"apps": [{"id": "music"}]}`)
	if _, _, err := splitExport(body, []string{"apps[id=bank]"}); err == nil || !strings.Contains(err.Error(), "matches nothing") {
		t.Fatalf("a selector that matches nothing must fail, got %v", err)
	}
	// An overlay that lost the entry would silently drop the app on restore.
	if _, err := mergeExport(body, []byte(`{"apps": []}`), []string{"apps[id=bank]"}); err == nil || !strings.Contains(err.Error(), "holds no such entry") {
		t.Fatalf("a restore without the entry must fail, got %v", err)
	}
}

func TestPrivateSelectorSyntax(t *testing.T) {
	for _, field := range []string{"apps[id]", "apps[id=]", "apps[=bank]", "apps[id=bank"} {
		if _, err := parseSelection(field); err == nil {
			t.Fatalf("%q parsed as a selector", field)
		}
		if _, err := ParseBytes([]byte(recipeWithPrivate(field))); err == nil {
			t.Fatalf("%q passed recipe validation", field)
		}
	}
	chosen, err := parseSelection("apps[id=a.b-c_d]")
	if err != nil || !chosen.keyed || chosen.value != "a.b-c_d" || strings.Join(chosen.path, ".") != "apps" {
		t.Fatalf("selector = %+v, %v", chosen, err)
	}
}

func recipeWithPrivate(field string) string {
	quoted, err := json.Marshal(field)
	if err != nil {
		panic(err)
	}
	return `{"package": "a.b", "artifact": "s.json", "private": [` + string(quoted) + `],
 "backup": [{"pull": {"from": "/sdcard/s.json", "to": "{{artifact}}"}}],
 "restore": [{"push": {"from": "{{artifact}}", "to": "/sdcard/s.json"}}]}`
}

func TestSplitWritesAnEmptyOverlayWhenNothingIsPrivate(t *testing.T) {
	// An export that already has its private fields blanked must still produce an
	// overlay, because a missing file has to mean the private half was lost. The
	// file itself stays empty rather than claiming a value it does not hold.
	body := []byte(`{"data": {"location": {"value": ""}, "clock": {"value": true}}}`)
	public, private := mustSplit(t, body, splitFields)
	if private == nil {
		t.Fatal("no overlay for a recipe that declares private fields")
	}
	if strings.Contains(string(private), `"value": ""`) {
		t.Fatalf("overlay recorded a zero value:\n%s", private)
	}
	if !strings.Contains(string(public), "clock") {
		t.Fatalf("artifact lost its public content:\n%s", public)
	}
}

func TestMergeKeepsThePublicValueWhenTheOverlayLacksIt(t *testing.T) {
	public := Redact([]byte(splitFixture), splitFields)
	overlay := []byte(`{"data": {"location": {"value": "latitude=1&longitude=2"}}}`)
	merged, err := mergeExport(public, overlay, splitFields)
	if err != nil {
		t.Fatalf("merge: %v", err)
	}

	if !strings.Contains(string(merged), "latitude=1") {
		t.Fatalf("overlay value not merged in:\n%s", merged)
	}
	// A field the overlay dropped keeps whatever the artifact says, so an overlay
	// that lags the artifact does not blank a setting.
	if !strings.Contains(string(merged), `"locationRegion"`) {
		t.Fatalf("a field the overlay lacks was removed:\n%s", merged)
	}
}

func TestRecipeRefusesAmbiguousOrUnsafePrivateFields(t *testing.T) {
	tests := []struct {
		name    string
		body    string
		wantErr string
	}{
		{
			name: "a path is both thrown away and kept",
			body: `{"package": "a.b", "artifact": "s.json", "private": ["x.y"], "redact": ["x.y"],
 "backup": [{"pull": {"from": "/sdcard/s.json", "to": "{{artifact}}"}}],
 "restore": [{"push": {"from": "{{artifact}}", "to": "/sdcard/s.json"}}]}`,
			wantErr: "both redacted and private",
		},
		{
			name: "splitting a binary artifact",
			body: `{"package": "a.b", "artifact": "s.xml", "private": ["x.y"],
 "backup": [{"pull": {"from": "/sdcard/s.xml", "to": "{{artifact}}"}}],
 "restore": [{"push": {"from": "{{artifact}}", "to": "/sdcard/s.xml"}}]}`,
			wantErr: "need a JSON artifact",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, err := ParseBytes([]byte(test.body))
			if err == nil || !strings.Contains(err.Error(), test.wantErr) {
				t.Fatalf("error = %v, want it to contain %q", err, test.wantErr)
			}
		})
	}
}

// equalJSON compares two decoded JSON values, which avoids depending on the map
// order an encoder happens to emit.
func equalJSON(a, b any) bool {
	left, err := json.Marshal(a)
	if err != nil {
		return false
	}
	right, err := json.Marshal(b)
	if err != nil {
		return false
	}
	return string(left) == string(right)
}

func TestPrivateListCanLiveInTheExportTree(t *testing.T) {
	// A selector names the entry it hides, so a recipe that spells one out
	// publishes it. The list therefore belongs beside the exports.
	dir := t.TempDir()
	recipe := Recipe{Artifact: "state.json", DataDir: dir}
	if err := recipe.parsePrivate([]byte(`"private.json"`)); err != nil {
		t.Fatalf("file form: %v", err)
	}
	if recipe.PrivateFile != "private.json" || len(recipe.Private) != 0 {
		t.Fatalf("parsed as %q / %v", recipe.PrivateFile, recipe.Private)
	}
	if !recipe.IsPrivate() {
		t.Fatal("a recipe with a private file must count as split")
	}
	if err := recipe.LoadPrivate(); err == nil || !strings.Contains(err.Error(), "no such file") {
		t.Fatalf("a missing selector file must fail, got %v", err)
	}

	if err := os.WriteFile(filepath.Join(dir, "private.json"), []byte(`["settings.token", "apps[id=x]"]`), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := recipe.LoadPrivate(); err != nil {
		t.Fatalf("load: %v", err)
	}
	if len(recipe.Private) != 2 {
		t.Fatalf("selectors = %v", recipe.Private)
	}

	// An empty file would silently publish everything it was meant to hold.
	if err := os.WriteFile(filepath.Join(dir, "private.json"), []byte(`[]`), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := recipe.LoadPrivate(); err == nil || !strings.Contains(err.Error(), "no selectors") {
		t.Fatalf("an empty selector file must fail, got %v", err)
	}
}

func TestPrivateCannotBeBothAListAndAFile(t *testing.T) {
	body := `{"package": "a.b", "artifact": "s.json", "private": ["x.y"],
 "backup": [{"pull": {"from": "/sdcard/s.json", "to": "{{artifact}}"}}],
 "restore": [{"push": {"from": "{{artifact}}", "to": "/sdcard/s.json"}}]}`
	if _, err := ParseBytes([]byte(body)); err != nil {
		t.Fatalf("inline list must parse: %v", err)
	}
	if err := (&Recipe{}).parsePrivate([]byte(`"../outside.json"`)); err == nil {
		t.Fatal("a path outside the export directory must be refused")
	}
}
