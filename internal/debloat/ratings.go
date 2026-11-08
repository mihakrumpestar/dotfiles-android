package debloat

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// UADBranch is the upstream branch the ratings are read from.
//
// Always a branch, never a commit. Pinning a commit silently aged the list while
// the device kept updating; the ratings are an input to a human's decision, not
// a reproducible artifact, so latest is correct.
const UADBranch = "main"

// UADURL is the upstream Universal Android Debloater package list.
const UADURL = "https://raw.githubusercontent.com/Universal-Debloater-Alliance/" +
	"universal-android-debloater-next-generation/" + UADBranch +
	"/resources/assets/uad_lists.json"

// Rating is one upstream entry. Opinion, not authority: on a stock Samsung the
// Recommended package is often the dangerous one.
type Rating struct {
	Removal      string   `json:"removal"`
	List         string   `json:"list"`
	Description  string   `json:"description"`
	Dependencies []string `json:"dependencies"`
	NeededBy     []string `json:"neededBy"`
}

// Freshness says where the ratings came from, so nobody has to guess how stale
// they are.
type Freshness string

const (
	// FreshnessFetched means the list was downloaded this run.
	FreshnessFetched Freshness = "fetched"
	// FreshnessRevalidated means the cache was current (HTTP 304).
	FreshnessRevalidated Freshness = "revalidated"
	// FreshnessCache means the network was unavailable and a cache was used.
	FreshnessCache Freshness = "cache"
)

// Ratings holds the upstream list plus where it came from.
type Ratings struct {
	ByPackage map[string]Rating
	Source    Freshness
	Detail    string
}

// String renders the header line the report prints.
func (r Ratings) String() string {
	if r.Detail != "" {
		return string(r.Source) + " (" + r.Detail + ")"
	}
	return string(r.Source)
}

// LoadRatings fetches the upstream list, revalidating with an ETag so a warm
// cache costs one 304 rather than 1.6MB per report.
//
// Offline with a cache is not an error: describing the device is still useful.
// Offline without a cache is.
func LoadRatings(stateDir string) (Ratings, error) {
	return loadRatingsFrom(stateDir, UADURL)
}

// loadRatingsFrom is LoadRatings with an injectable URL, so the ETag and
// offline paths are testable without reaching the network.
func loadRatingsFrom(stateDir, url string) (Ratings, error) {
	cachePath := filepath.Join(stateDir, "uad_lists.json")
	etagPath := filepath.Join(stateDir, "uad_lists.etag")

	body, _ := os.ReadFile(cachePath)
	etag, _ := os.ReadFile(etagPath)

	request, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		return Ratings{}, fmt.Errorf("building ratings request: %w", err)
	}
	if len(body) > 0 && len(etag) > 0 {
		request.Header.Set("If-None-Match", strings.TrimRight(string(etag), "\r\n"))
	}

	client := &http.Client{Timeout: 60 * time.Second}
	response, err := client.Do(request)
	if err != nil {
		if len(body) > 0 {
			return decodeRatings(body, FreshnessCache, err.Error())
		}
		return Ratings{}, fmt.Errorf("fetching upstream ratings: %w", err)
	}
	defer response.Body.Close()

	switch response.StatusCode {
	case http.StatusNotModified:
		if len(body) == 0 {
			return Ratings{}, errors.New("upstream returned 304 but no cache exists")
		}
		return decodeRatings(body, FreshnessRevalidated, "")
	case http.StatusOK:
		fresh, err := io.ReadAll(response.Body)
		if err != nil {
			return Ratings{}, fmt.Errorf("reading ratings response: %w", err)
		}
		if err := os.MkdirAll(stateDir, 0o755); err != nil {
			return Ratings{}, fmt.Errorf("creating state directory: %w", err)
		}
		if err := os.WriteFile(cachePath, fresh, 0o644); err != nil {
			return Ratings{}, fmt.Errorf("caching ratings: %w", err)
		}
		if newETag := response.Header.Get("ETag"); newETag != "" {
			_ = os.WriteFile(etagPath, []byte(newETag+"\n"), 0o644)
		} else {
			_ = os.Remove(etagPath)
		}
		return decodeRatings(fresh, FreshnessFetched, "")
	default:
		if len(body) > 0 {
			return decodeRatings(body, FreshnessCache, response.Status)
		}
		return Ratings{}, fmt.Errorf("upstream ratings: %s", response.Status)
	}
}

func decodeRatings(body []byte, source Freshness, detail string) (Ratings, error) {
	var byPackage map[string]Rating
	if err := json.Unmarshal(body, &byPackage); err != nil {
		return Ratings{}, fmt.Errorf("parsing upstream ratings: %w", err)
	}
	return Ratings{ByPackage: byPackage, Source: source, Detail: detail}, nil
}
