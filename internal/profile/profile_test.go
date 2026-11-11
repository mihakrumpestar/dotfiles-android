package profile

import (
	"os"
	"strings"
	"testing"
)

func TestParse(t *testing.T) {
	tests := []struct {
		name    string
		input   string
		want    []Entry
		wantErr string
	}{
		{
			name:  "sections and comments",
			input: "# leading\n[system]\na = \"1\"   # first\n\n[secure]\nb = \"2\"  # second\n",
			want: []Entry{
				{Namespace: "system", Key: "a", Value: "1", Reason: "first"},
				{Namespace: "secure", Key: "b", Value: "2", Reason: "second"},
			},
		},
		{
			name:  "why prefix is stripped",
			input: "[system]\na = \"1\"   # why: because\n",
			want:  []Entry{{Namespace: "system", Key: "a", Value: "1", Reason: "because"}},
		},
		{
			name:  "empty value allowed",
			input: "[secure]\na = \"\"   # nothing\n",
			want:  []Entry{{Namespace: "secure", Key: "a", Value: "", Reason: "nothing"}},
		},
		{
			name:    "missing reason",
			input:   "[system]\na = \"1\"\n",
			wantErr: "no trailing # reason",
		},
		{
			name:    "unquoted value",
			input:   "[system]\na = 1   # x\n",
			wantErr: "must be quoted",
		},
		{
			name:    "entry before section",
			input:   "a = \"1\"   # x\n",
			wantErr: "before any [namespace]",
		},
		{
			name:    "empty namespace",
			input:   "[]\na = \"1\"   # x\n",
			wantErr: "invalid namespace",
		},
		{
			name:    "trailing garbage",
			input:   "[system]\na = \"1\" tail   # x\n",
			wantErr: "unexpected text",
		},
		{
			name:    "empty profile",
			input:   "# nothing\n",
			wantErr: "profile is empty",
		},
		{
			name:    "duplicate key",
			input:   "[system]\na = \"1\"   # first\na = \"2\"   # second\n",
			wantErr: "duplicate system/a",
		},
		{
			name:    "empty key",
			input:   "[system]\n = \"1\"   # x\n",
			wantErr: "expected key",
		},
		{
			name:    "invalid namespace",
			input:   "[[system]]\na = \"1\"   # x\n",
			wantErr: "invalid namespace",
		},
		{
			name:    "unknown section",
			input:   "[applink]\na = \"1\"   # x\n",
			wantErr: "unknown section",
		},
		{
			name:    "invalid key",
			input:   "[system]\na; reboot = \"1\"   # x\n",
			wantErr: "expected key",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := Parse(tc.input)
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("err = %v, want containing %q", err, tc.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("Parse: %v", err)
			}
			if len(got) != len(tc.want) {
				t.Fatalf("got %d entries, want %d", len(got), len(tc.want))
			}
			for i := range tc.want {
				if got[i] != tc.want[i] {
					t.Errorf("entry %d = %+v, want %+v", i, got[i], tc.want[i])
				}
			}
		})
	}
}

// The namespace alone must decide the store. Getting this wrong is the silent
// no-op the split exists to prevent: `settings put` accepts a DeviceConfig key,
// reports success, and writes a row nothing reads.
func TestSectionStore(t *testing.T) {
	tests := map[string]bool{
		"system":             true,
		"secure":             true,
		"global":             true,
		"telephony":          true,
		"applink":            false,
		"unknown":            false,
		"permissions":        true,
		"appops":             true,
		"role":               true,
		"deviceidle":         true,
		"standby":            true,
		"locale":             true,
		"applinks":           true,
		"applinks_domain":    true,
		"applinks_selection": true,
	}
	for namespace, want := range tests {
		if _, ok := SectionStore(namespace); ok != want {
			t.Errorf("SectionStore(%q) ok = %v, want %v", namespace, ok, want)
		}
	}
}

// Every app-scoped section must select its own store, not the DeviceConfig
// fallback that would happily accept a key it cannot reach.
func TestStoreIdentity(t *testing.T) {
	tests := map[string]Store{
		"system":             Settings,
		"secure":             Settings,
		"global":             Settings,
		"telephony":          DeviceConfig,
		"permissions":        permissionsStore{},
		"appops":             appOpsStore{},
		"role":               roleStore{},
		"deviceidle":         deviceIdleStore{},
		"standby":            standbyStore{},
		"locale":             localeStore{},
		"applinks":           appLinksStore{},
		"applinks_domain":    appLinksDomainStore{},
		"applinks_selection": appLinksSelectionStore{},
	}
	for namespace, want := range tests {
		got, ok := SectionStore(namespace)
		if !ok {
			t.Errorf("SectionStore(%q) is not a known section", namespace)
			continue
		}
		if got != want {
			t.Errorf("SectionStore(%q) = %T, want %T", namespace, got, want)
		}
	}
}

// The key grammar must carry the separators the app-scoped stores split on.
func TestParseAppKeys(t *testing.T) {
	tests := []struct {
		name    string
		input   string
		want    string
		wantErr string
	}{
		{
			name:  "package and permission",
			input: "[permissions]\ncom.example.app:android.permission.CAMERA = \"granted\"   # camera\n",
			want:  "com.example.app:android.permission.CAMERA",
		},
		{
			name:  "package and app op",
			input: "[appops]\ncom.example.app:RECORD_AUDIO = \"ignore\"   # no mic\n",
			want:  "com.example.app:RECORD_AUDIO",
		},
		{
			name:  "role with dots and underscore",
			input: "[role]\nandroid.app.role.SMS = \"com.example.sms\"   # default sms\n",
			want:  "android.app.role.SMS",
		},
		{
			name:  "package with hyphen",
			input: "[standby]\ncom.example-app = \"rare\"   # parked\n",
			want:  "com.example-app",
		},
		{
			name:  "domain with hyphen",
			input: "[applinks_domain]\ncom.example.app:foo-bar.example = \"approved\"   # links\n",
			want:  "com.example.app:foo-bar.example",
		},
		{
			name:  "wildcard domain",
			input: "[applinks_selection]\ncom.example.app:*.discord.com = \"enabled\"   # links\n",
			want:  "com.example.app:*.discord.com",
		},
		{
			name:    "colon in namespace is still invalid",
			input:   "[foo:bar]\na = \"1\"   # x\n",
			wantErr: "invalid namespace",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			p, err := Parse(tc.input)
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("err = %v, want containing %q", err, tc.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("Parse: %v", err)
			}
			if len(p) != 1 || p[0].Key != tc.want {
				t.Fatalf("parsed %+v, want key %q", p, tc.want)
			}
		})
	}
}

// A readback is evidence only when the state says where the value came from:
// an unset key never matches, a derived value matches when it equals what the
// profile wants, and an app op reset to default reads back as the derived mode.
func TestSatisfied(t *testing.T) {
	tests := []struct {
		name  string
		entry Entry
		value string
		state State
		want  bool
	}{
		{"explicit match", Entry{Namespace: "standby", Value: "rare"}, "rare", Explicit, true},
		{"explicit mismatch", Entry{Namespace: "standby", Value: "rare"}, "never", Explicit, false},
		{"unset never matches", Entry{Namespace: "appops", Value: "allow"}, "", Unset, false},
		{"derived value that matches", Entry{Namespace: "standby", Value: "exempted"}, "exempted", Implicit, true},
		{"derived value that differs", Entry{Namespace: "standby", Value: "never"}, "exempted", Implicit, false},
		{"app op reset to default", Entry{Namespace: "appops", Value: "default"}, "allow", Implicit, true},
		{"app op default held explicitly", Entry{Namespace: "appops", Value: "default"}, "default", Explicit, true},
		{"domain code and token", Entry{Namespace: "applinks_domain", Value: "1"}, "verified", Explicit, true},
		{"domain token and code", Entry{Namespace: "applinks_domain", Value: "verified"}, "1", Explicit, true},
		{"domain mismatch", Entry{Namespace: "applinks_domain", Value: "approved"}, "denied", Explicit, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := Satisfied(tc.entry, tc.value, tc.state); got != tc.want {
				t.Errorf("Satisfied(%+v, %q, %v) = %v, want %v", tc.entry, tc.value, tc.state, got, tc.want)
			}
		})
	}
}

func TestResetOK(t *testing.T) {
	tests := []struct {
		name  string
		entry Entry
		value string
		state State
		want  bool
	}{
		{"settings delete leaves the key unset", Entry{Namespace: "secure", Key: "a"}, "", Unset, true},
		{"deviceidle lets go of a user row", Entry{Namespace: "deviceidle", Key: "com.example.app"}, "ignore", Explicit, true},
		{"deviceidle keeps a system-excidle row", Entry{Namespace: "deviceidle", Key: "com.example.app"}, "allow", Implicit, true},
		{"role clears to empty", Entry{Namespace: "role", Key: "android.app.role.SMS"}, "", Explicit, true},
		{"locale clears to empty", Entry{Namespace: "locale", Key: "com.example.app"}, "", Explicit, true},
		{"permission revoke reads revoked", Entry{Namespace: "permissions", Key: "com.example.app:android.permission.CAMERA"}, "revoked", Explicit, true},
		{"permission still granted", Entry{Namespace: "permissions", Key: "com.example.app:android.permission.CAMERA"}, "granted", Explicit, false},
		{"domain back to none", Entry{Namespace: "applinks_domain", Key: "com.example.app:foo.example"}, "none", Explicit, true},
		{"domain still approved", Entry{Namespace: "applinks_domain", Key: "com.example.app:foo.example"}, "approved", Explicit, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := ResetOK(tc.entry, tc.value, tc.state); got != tc.want {
				t.Errorf("ResetOK(%+v, %q, %v) = %v, want %v", tc.entry, tc.value, tc.state, got, tc.want)
			}
		})
	}
}

func TestShippedProfileParses(t *testing.T) {
	// Two files, one declaration: settings in profile.conf, app scoped state in
	// apps.conf. Splitting them must not weaken what a parse guarantees.
	files := []string{"../../policy/profile.conf", "../../policy/apps.conf"}
	var p Profile
	for _, file := range files {
		body, err := os.ReadFile(file)
		if err != nil {
			t.Fatalf("reading %s: %v", file, err)
		}
		parsed, err := Parse(string(body))
		if err != nil {
			t.Fatalf("%s: %v", file, err)
		}
		p = append(p, parsed...)
	}
	if len(p) < 30 {
		t.Errorf("profiles have only %d entries", len(p))
	}

	stores := map[Store]int{}
	seen := map[string]bool{}
	for _, entry := range p {
		if entry.Reason == "" {
			t.Errorf("%s/%s has no reason", entry.Namespace, entry.Key)
		}
		if seen[Key(entry)] {
			t.Errorf("%s is declared in both files", Key(entry))
		}
		seen[Key(entry)] = true
		stores[StoreFor(entry)]++
	}
	// Every store must be exercised, or the merged command is hiding one of them.
	if stores[Settings] == 0 || stores[DeviceConfig] == 0 {
		t.Errorf("profiles do not cover both key/value stores: %v", stores)
	}
	for _, name := range []string{"permissions", "appops", "role", "deviceidle"} {
		if stores[appStores[name]] == 0 {
			t.Errorf("no entry exercises the %s store", name)
		}
	}
}

func TestShippedProfileKeepsPlatformTrapsOut(t *testing.T) {
	// Verified on the work phone: these keys do not exist, belong to the other
	// store, or are owned by another app. A profile naming them would report
	// success and change nothing. Comments may mention them, entries may not.
	body, err := os.ReadFile("../../policy/profile.conf")
	if err != nil {
		t.Fatalf("reading policy/profile.conf: %v", err)
	}
	p, err := Parse(string(body))
	if err != nil {
		t.Fatalf("policy/profile.conf: %v", err)
	}
	forbidden := map[string]string{
		"night_display":      "Samsung uses blue_light_filter*",
		"alarm_vibration":    "no such key on this firmware",
		"screen_off_timeout": "owned by Keep Screen On",
	}
	for _, entry := range p {
		if why, bad := forbidden[entry.Key]; bad {
			t.Errorf("%s is in the profile: %s", entry.Key, why)
		}
	}
	for _, namespace := range []string{"system", "secure", "global"} {
		if store, ok := SectionStore(namespace); !ok || store != Settings {
			t.Errorf("%s must route to the Settings store", namespace)
		}
	}
	if store, ok := SectionStore("telephony"); !ok || store == Settings {
		t.Error("telephony must route to DeviceConfig, not settings put")
	}
	if _, ok := SectionStore("applink"); ok {
		t.Error("a typo must not be a known section")
	}
}

// The exact shell commands matter: `settings put` and `cmd device_config put`
// are the only difference between the stores, and picking the wrong one is a
// silent no-op rather than an error.
func TestStoreCommands(t *testing.T) {
	tests := []struct {
		store string
		parts []string
		want  string
	}{
		{"settings", []string{"put", "system", "a", "1"}, "settings put system a 1"},
		{"settings", []string{"get", "secure", "b"}, "settings get secure b"},
		{"settings", []string{"put", "secure", "b", ""}, "settings put secure b ''"},
		{"settings", []string{"put", "secure", "b", "two words"}, "settings put secure b 'two words'"},
		{"device_config", []string{"put", "telephony", "c", "2"}, "cmd device_config put telephony c 2"},
		{"device_config", []string{"get", "telephony", "d"}, "cmd device_config get telephony d"},
		{"device_config", []string{"get", "telephony", "a;reboot"}, "cmd device_config get telephony 'a;reboot'"},
		{"settings", []string{"delete", "secure", "b"}, "settings delete secure b"},
		{"device_config", []string{"delete", "telephony", "c"}, "cmd device_config delete telephony c"},
	}
	for _, tc := range tests {
		s := Settings
		if tc.store == "device_config" {
			s = DeviceConfig
		}
		if got := s.command(tc.parts...); got != tc.want {
			t.Errorf("%s command = %q, want %q", tc.store, got, tc.want)
		}
	}
}

// An empty value must reach the device as an empty argument. Joined without
// quoting, `settings put secure key` has one argument too few and fails, so a
// profile cannot express "clear this setting".
func TestQuote(t *testing.T) {
	tests := map[string]string{
		"1":             "1",
		"opportunistic": "opportunistic",
		"0.0":           "0.0",
		"":              "''",
		"two words":     "'two words'",
		"it's":          `'it'\''s'`,
		"$HOME":         "'$HOME'",
	}
	for value, want := range tests {
		if got := quote(value); got != want {
			t.Errorf("quote(%q) = %q, want %q", value, got, want)
		}
	}
}
