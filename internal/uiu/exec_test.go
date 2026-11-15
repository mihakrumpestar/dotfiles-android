package uiu

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestPushableMergesTheOverlayWithoutTouchingTheArtifact(t *testing.T) {
	public := t.TempDir()
	private := t.TempDir()
	recipe := Recipe{
		Package:  "eu.ottop.yamlauncher",
		Artifact: "yamlauncher.json",
		Private:  splitFields,
		Dir:      public,
		DataDir:  private,
		BlobDir:  public,
	}
	if err := os.WriteFile(recipe.ArtifactPath(), Redact([]byte(splitFixture), splitFields), 0o644); err != nil {
		t.Fatal(err)
	}
	_, overlay, err := splitExport([]byte(splitFixture), splitFields)
	if err != nil {
		t.Fatalf("split: %v", err)
	}
	if len(overlay) == 0 {
		t.Fatal("fixture produced no overlay")
	}
	if err := os.WriteFile(recipe.PrivatePath(), overlay, 0o600); err != nil {
		t.Fatal(err)
	}

	run := &runner{recipe: recipe, tmp: t.TempDir()}
	path, err := run.pushable(recipe.ArtifactPath())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	merged, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(merged), "Fixture City") {
		t.Fatalf("the pushed file carries no private value:\n%s", merged)
	}
	// The artifact itself is the committed half and must never be the file that
	// carries the values, or the repository grows them back on the next commit.
	stored, err := os.ReadFile(recipe.ArtifactPath())
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(stored), "Fixture City") {
		t.Fatalf("push wrote the private value into the artifact:\n%s", stored)
	}
}

func TestPushableRefusesAMissingOverlay(t *testing.T) {
	public := t.TempDir()
	recipe := Recipe{
		Package:  "eu.ottop.yamlauncher",
		Artifact: "yamlauncher.json",
		Private:  splitFields,
		Dir:      public,
		DataDir:  t.TempDir(),
		BlobDir:  public,
	}
	if err := os.WriteFile(recipe.ArtifactPath(), Redact([]byte(splitFixture), splitFields), 0o644); err != nil {
		t.Fatal(err)
	}

	run := &runner{recipe: recipe, tmp: t.TempDir()}
	_, err := run.pushable(recipe.ArtifactPath())
	if err == nil || !strings.Contains(err.Error(), "needs it beside the artifact") {
		t.Fatalf("a restore without the overlay must refuse, got %v", err)
	}
	// Pushing the zeroed half would reset the phone to blanks, so nothing may be
	// returned for the caller to push.
	if _, statErr := os.Stat(filepath.Join(run.tmp, "push-yamlauncher.json")); statErr == nil {
		t.Fatal("a merged file was written despite the missing overlay")
	}
}

func TestMergeFileIsTheWholeExport(t *testing.T) {
	// A restore's undo copy is written from this, so it must carry the private
	// entries: the artifact alone cannot put them back.
	public := t.TempDir()
	recipe := Recipe{
		Package:  "eu.ottop.yamlauncher",
		Artifact: "yamlauncher.json",
		Private:  splitFields,
		Dir:      public,
		DataDir:  t.TempDir(),
		BlobDir:  public,
	}
	if err := os.WriteFile(recipe.ArtifactPath(), Redact([]byte(splitFixture), splitFields), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := MergeFile(recipe); err == nil {
		t.Fatal("a missing overlay must fail rather than return the published half")
	}

	_, overlay, err := splitExport([]byte(splitFixture), splitFields)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(recipe.PrivatePath(), overlay, 0o600); err != nil {
		t.Fatal(err)
	}
	merged, err := MergeFile(recipe)
	if err != nil {
		t.Fatalf("merge: %v", err)
	}
	if !strings.Contains(string(merged), "Fixture City") {
		t.Fatalf("undo copy is missing the private value:\n%s", merged)
	}
}
