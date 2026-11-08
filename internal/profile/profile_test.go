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
func TestStoreFor(t *testing.T) {
	tests := map[string]string{
		"system":           "settings",
		"secure":           "settings",
		"global":           "settings",
		"telephony":        "device_config",
		"activity_manager": "device_config",
		"unknown":          "device_config",
	}
	for namespace, want := range tests {
		if got := StoreFor(namespace).Name; got != want {
			t.Errorf("StoreFor(%q) = %q, want %q", namespace, got, want)
		}
	}
}

func TestShippedProfileParses(t *testing.T) {
	body, err := os.ReadFile("../../policy/profile.conf")
	if err != nil {
		t.Fatalf("reading policy/profile.conf: %v", err)
	}
	p, err := Parse(string(body))
	if err != nil {
		t.Fatalf("policy/profile.conf: %v", err)
	}
	if len(p) < 30 {
		t.Errorf("profile has only %d entries", len(p))
	}

	stores := map[string]int{}
	for _, entry := range p {
		if entry.Reason == "" {
			t.Errorf("%s/%s has no reason", entry.Namespace, entry.Key)
		}
		stores[StoreFor(entry.Namespace).Name]++
	}
	// Both stores must be exercised, or the merged command is hiding one of them.
	if stores["settings"] == 0 || stores["device_config"] == 0 {
		t.Errorf("profile does not cover both stores: %v", stores)
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
		if StoreFor(namespace).Name != Settings.Name {
			t.Errorf("%s must route to the Settings store", namespace)
		}
	}
	if StoreFor("telephony").Name == Settings.Name {
		t.Error("telephony must route to DeviceConfig, not settings put")
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
