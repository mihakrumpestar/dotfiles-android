// Package obtainium installs Obtainium itself, verified, so it can then own the
// apps.
//
// There is no app manifest in this repo on purpose. Obtainium's own export is
// the declarative artifact: you configure the list once in the app, export it,
// and re-import on a new device. Regenerating that file from a second source of
// truth would duplicate it and drift.
//
// The one thing that cannot be expressed in an export is Obtainium itself, so it
// is bootstrapped here from its GitHub release, with the publisher's checksum
// verified before the APK is pushed.
package obtainium

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// PackageID is Obtainium's application id.
const PackageID = "dev.imranr.obtainium"

// APKAsset maps an Android ABI to the release asset Obtainium publishes for it.
// The plain "-release" builds are universal; the f-droid ones share the same
// signing key but update through F-Droid, which is not what this installs.
var APKAsset = map[string]string{
	"arm64-v8a":   "app-arm64-v8a-release.apk",
	"armeabi-v7a": "app-armeabi-v7a-release.apk",
	"x86_64":      "app-x86_64-release.apk",
	"universal":   "app-release.apk",
}

// Client is the HTTP client used for every request, with a timeout so a stalled
// connection fails instead of hanging a bootstrap.
var Client = &http.Client{Timeout: 120 * time.Second}

// AssetFor picks the release asset for an Android abilist, most specific first.
func AssetFor(abilist []string) string {
	for _, abi := range abilist {
		if asset, ok := APKAsset[abi]; ok {
			return asset
		}
	}
	return APKAsset["universal"]
}

// ReleaseURL is the GitHub "latest release" endpoint.
var ReleaseURL = "https://api.github.com/repos/ImranR98/Obtainium/releases/latest"

// Release is the subset of GitHub's release payload this needs.
type Release struct {
	Tag    string  `json:"tag_name"`
	Assets []Asset `json:"assets"`
}

// Asset is one release file.
type Asset struct {
	Name string `json:"name"`
	URL  string `json:"browser_download_url"`
	Size int64  `json:"size"`
}

// LatestRelease fetches the current release metadata.
func LatestRelease(client *http.Client, url string) (Release, error) {
	var release Release
	response, err := client.Get(url)
	if err != nil {
		return release, fmt.Errorf("fetching Obtainium release: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return release, fmt.Errorf("obtainium release: %s", response.Status)
	}
	if err := json.NewDecoder(response.Body).Decode(&release); err != nil {
		return release, fmt.Errorf("parsing release metadata: %w", err)
	}
	return release, nil
}

// Find returns the asset with the given name.
func (r Release) Find(name string) (Asset, error) {
	for _, asset := range r.Assets {
		if asset.Name == name {
			return asset, nil
		}
	}
	return Asset{}, fmt.Errorf("release %s has no asset %q", r.Tag, name)
}

// FindChecksum returns the publisher's .sha256 asset for an APK asset.
func (r Release) FindChecksum(apk Asset) (Asset, error) {
	return r.Find(apk.Name + ".sha256")
}

// ExpectedHash downloads and parses a `.sha256` asset. The file holds the bare
// lowercase hex digest, sometimes followed by a filename.
func ExpectedHash(client *http.Client, asset Asset) (string, error) {
	response, err := client.Get(asset.URL)
	if err != nil {
		return "", fmt.Errorf("fetching %s: %w", asset.Name, err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return "", fmt.Errorf("%s: %s", asset.Name, response.Status)
	}
	body, err := io.ReadAll(response.Body)
	if err != nil {
		return "", fmt.Errorf("reading %s: %w", asset.Name, err)
	}
	fields := strings.Fields(string(body))
	if len(fields) == 0 {
		return "", fmt.Errorf("%s is empty", asset.Name)
	}
	digest := strings.ToLower(fields[0])
	if _, err := hex.DecodeString(digest); err != nil || len(digest) != 64 {
		return "", fmt.Errorf("%s does not hold a sha256 digest: %q", asset.Name, digest)
	}
	return digest, nil
}

// Download fetches an asset into dir and verifies it against want.
//
// The hash check is the point: a bootstrap that installs whatever GitHub served
// is an unsigned remote code execution path on the user's phone. The asset is
// removed on mismatch so a corrupted file cannot be used later.
func Download(client *http.Client, asset Asset, want, dir string) (string, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", fmt.Errorf("creating download directory: %w", err)
	}
	path := filepath.Join(dir, asset.Name)

	response, err := client.Get(asset.URL)
	if err != nil {
		return "", fmt.Errorf("fetching %s: %w", asset.Name, err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return "", fmt.Errorf("%s: %s", asset.Name, response.Status)
	}

	file, err := os.Create(path)
	if err != nil {
		return "", fmt.Errorf("creating %s: %w", path, err)
	}
	digest := sha256.New()
	if _, err := io.Copy(io.MultiWriter(file, digest), response.Body); err != nil {
		file.Close()
		os.Remove(path)
		return "", fmt.Errorf("downloading %s: %w", asset.Name, err)
	}
	if err := file.Close(); err != nil {
		os.Remove(path)
		return "", fmt.Errorf("closing %s: %w", path, err)
	}

	got := hex.EncodeToString(digest.Sum(nil))
	if got != want {
		os.Remove(path)
		return "", fmt.Errorf(
			"%s checksum mismatch\n  publisher says %s\n  downloaded     %s\n"+
				"refusing to install; retry, and if it persists, investigate the release",
			asset.Name, want, got,
		)
	}
	return path, nil
}

// Describe renders the plan for a human before anything runs.
func Describe(release Release, asset Asset, hash string) string {
	return fmt.Sprintf("%s %s\n  %s\n  sha256 %s", release.Tag, asset.Name, asset.URL, hash)
}
