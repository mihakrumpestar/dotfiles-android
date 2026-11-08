package obtainium

import (
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fixture serves a release payload, an APK, and its checksum asset.
func fixture(t *testing.T, apkBody, checksumBody string) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	var server *httptest.Server
	mux.HandleFunc("/release", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
			"tag_name": "v9.9.9",
			"assets": [
				{"name": "app-arm64-v8a-release.apk", "browser_download_url": "` + server.URL + `/apk", "size": 1},
				{"name": "app-arm64-v8a-release.apk.sha256", "browser_download_url": "` + server.URL + `/sha", "size": 1}
			]
		}`))
	})
	mux.HandleFunc("/apk", func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(apkBody))
	})
	mux.HandleFunc("/sha", func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(checksumBody))
	})
	server = httptest.NewServer(mux)
	t.Cleanup(server.Close)
	return server
}

func TestAssetForPicksTheDeviceABI(t *testing.T) {
	cases := []struct {
		abilist []string
		want    string
	}{
		{[]string{"arm64-v8a"}, "app-arm64-v8a-release.apk"},
		{[]string{"arm64-v8a", "armeabi-v7a"}, "app-arm64-v8a-release.apk"},
		{[]string{"armeabi-v7a"}, "app-armeabi-v7a-release.apk"},
		{[]string{"x86_64"}, "app-x86_64-release.apk"},
		{[]string{"mips"}, "app-release.apk"},
		{nil, "app-release.apk"},
	}
	for _, testCase := range cases {
		if got := AssetFor(testCase.abilist); got != testCase.want {
			t.Errorf("AssetFor(%v) = %q, want %q", testCase.abilist, got, testCase.want)
		}
	}
}

func TestLatestReleaseAndFind(t *testing.T) {
	server := fixture(t, "abc", "deadbeef")
	release, err := LatestRelease(server.Client(), server.URL+"/release")
	if err != nil {
		t.Fatalf("LatestRelease: %v", err)
	}
	if release.Tag != "v9.9.9" {
		t.Errorf("tag = %q", release.Tag)
	}
	apk, err := release.Find("app-arm64-v8a-release.apk")
	if err != nil {
		t.Fatalf("Find: %v", err)
	}
	if _, err := release.FindChecksum(apk); err != nil {
		t.Fatalf("FindChecksum: %v", err)
	}
	if _, err := release.Find("nope.apk"); err == nil {
		t.Fatal("expected an error for a missing asset")
	}
}

func TestExpectedHashParsesTheDigest(t *testing.T) {
	digest := strings.Repeat("ab", 32)
	server := fixture(t, "", digest+"  app.apk\n")
	release, err := LatestRelease(server.Client(), server.URL+"/release")
	if err != nil {
		t.Fatal(err)
	}
	apk, _ := release.Find("app-arm64-v8a-release.apk")
	checksum, _ := release.FindChecksum(apk)
	got, err := ExpectedHash(server.Client(), checksum)
	if err != nil {
		t.Fatalf("ExpectedHash: %v", err)
	}
	if got != digest {
		t.Errorf("hash = %q, want %q", got, digest)
	}
}

func TestExpectedHashRejectsNonDigest(t *testing.T) {
	server := fixture(t, "", "not-a-digest\n")
	release, _ := LatestRelease(server.Client(), server.URL+"/release")
	apk, _ := release.Find("app-arm64-v8a-release.apk")
	checksum, _ := release.FindChecksum(apk)
	if _, err := ExpectedHash(server.Client(), checksum); err == nil {
		t.Fatal("expected an error for a non-digest checksum file")
	}
}

func TestDownloadVerifiesTheChecksum(t *testing.T) {
	body := "an apk, honestly"
	sum := sha256.Sum256([]byte(body))
	server := fixture(t, body, hex.EncodeToString(sum[:])+"\n")

	release, _ := LatestRelease(server.Client(), server.URL+"/release")
	apk, _ := release.Find("app-arm64-v8a-release.apk")
	checksumAsset, _ := release.FindChecksum(apk)
	want, err := ExpectedHash(server.Client(), checksumAsset)
	if err != nil {
		t.Fatal(err)
	}

	dir := t.TempDir()
	path, err := Download(server.Client(), apk, want, dir)
	if err != nil {
		t.Fatalf("Download: %v", err)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != body {
		t.Errorf("downloaded %q, want %q", got, body)
	}
}

func TestDownloadRejectsAMismatchAndRemovesTheFile(t *testing.T) {
	// A bootstrap that installs whatever GitHub served is an unsigned remote
	// code execution path on the user's phone.
	server := fixture(t, "the real apk", strings.Repeat("0", 64)+"\n")
	release, _ := LatestRelease(server.Client(), server.URL+"/release")
	apk, _ := release.Find("app-arm64-v8a-release.apk")
	checksumAsset, _ := release.FindChecksum(apk)
	want, err := ExpectedHash(server.Client(), checksumAsset)
	if err != nil {
		t.Fatal(err)
	}

	dir := t.TempDir()
	_, err = Download(server.Client(), apk, want, dir)
	if err == nil {
		t.Fatal("expected a checksum mismatch")
	}
	if !strings.Contains(err.Error(), "checksum mismatch") {
		t.Errorf("err = %v, want a checksum mismatch", err)
	}
	if _, statErr := os.Stat(filepath.Join(dir, apk.Name)); !os.IsNotExist(statErr) {
		t.Error("the mismatched file was left on disk")
	}
}

func TestDownloadRejectsAnHTTPError(t *testing.T) {
	server := fixture(t, "", "")
	dir := t.TempDir()
	missing := Asset{Name: "x.apk", URL: server.URL + "/missing"}
	if _, err := Download(server.Client(), missing, strings.Repeat("0", 64), dir); err == nil {
		t.Fatal("expected an error for a 404")
	}
}

func TestReleaseURLPointsAtGitHub(t *testing.T) {
	if !strings.Contains(ReleaseURL, "ImranR98/Obtainium") {
		t.Fatalf("ReleaseURL = %q", ReleaseURL)
	}
}

func TestDescribeShowsTheChecksumBeforeInstalling(t *testing.T) {
	release := Release{Tag: "v1.2.3"}
	asset := Asset{Name: "app.apk", URL: "https://example.invalid/app.apk"}
	got := Describe(release, asset, strings.Repeat("a", 64))
	for _, want := range []string{"v1.2.3", "app.apk", "example.invalid", "sha256"} {
		if !strings.Contains(got, want) {
			t.Errorf("Describe() = %q, missing %q", got, want)
		}
	}
}
