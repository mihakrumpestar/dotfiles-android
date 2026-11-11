package profile

import (
	"slices"
	"strings"
	"testing"
)

// dumpsysPackage has a work-profile user block above user 0, each granting a
// different permission. Only the user 0 block answers for user 0.
const dumpsysPackage = `Packages:
  Package [com.example.app] (2f3a1b):
    userId=10123
    install permissions:
      android.permission.INTERNET: granted=true
    User 10: ceDataInode=1
      runtime permissions:
        android.permission.CAMERA: granted=true
        android.permission.BODY_SENSORS: granted=true
    User 0: ceDataInode=2
      runtime permissions:
        android.permission.CAMERA: granted=false, flags=[ USER_SET ]
        android.permission.RECORD_AUDIO: granted=true, flags=[ USER_SET ]
`

func TestPermissionsStore(t *testing.T) {
	readTests := []struct {
		name  string
		perm  string
		want  string
		state State
	}{
		{"revoked for user 0 though granted for user 10", "android.permission.CAMERA", "revoked", Explicit},
		{"granted", "android.permission.RECORD_AUDIO", "granted", Explicit},
		{"granted to another user only", "android.permission.BODY_SENSORS", "", Unset},
		{"install permission outside the user block", "android.permission.INTERNET", "", Unset},
		{"absent on this platform", "android.permission.POST_NOTIFICATIONS", "", Unset},
		{"prefix of a listed permission", "android.permission.CAM", "", Unset},
	}
	for _, tc := range readTests {
		t.Run("read "+tc.name, func(t *testing.T) {
			got, state := parsePermission(dumpsysPackage, tc.perm)
			if got != tc.want || state != tc.state {
				t.Errorf("parsePermission(%q) = %q, %v; want %q, %v", tc.perm, got, state, tc.want, tc.state)
			}
		})
	}

	commandTests := []struct {
		name    string
		entry   Entry
		want    string
		wantErr string
	}{
		{
			name:  "grant",
			entry: Entry{Key: "com.example.app:android.permission.CAMERA", Value: "granted"},
			want:  "pm grant com.example.app android.permission.CAMERA",
		},
		{
			name:  "revoke",
			entry: Entry{Key: "com.example.app:android.permission.CAMERA", Value: "revoked"},
			want:  "pm revoke com.example.app android.permission.CAMERA",
		},
		{
			name:    "unknown value",
			entry:   Entry{Key: "com.example.app:android.permission.CAMERA", Value: "on"},
			wantErr: "granted or revoked",
		},
		{
			name:    "missing separator",
			entry:   Entry{Key: "com.example.app", Value: "granted"},
			wantErr: "<package>:<name>",
		},
		{
			name:    "invalid permission",
			entry:   Entry{Key: "com.example.app:bad perm", Value: "granted"},
			wantErr: "invalid permission",
		},
		{
			name:    "invalid package",
			entry:   Entry{Key: "bad package:android.permission.CAMERA", Value: "granted"},
			wantErr: "not a package name",
		},
	}
	for _, tc := range commandTests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := permissionsStore{}.writeCommand(tc.entry)
			checkCommand(t, got, err, tc.want, tc.wantErr)
		})
	}

	got, err := permissionsStore{}.resetCommand("com.example.app:android.permission.CAMERA")
	checkCommand(t, got, err, "pm revoke com.example.app android.permission.CAMERA", "")
}

func TestAppOpsStore(t *testing.T) {
	const output = `CAMERA: allow; time=+1h32m12s ago
Uid mode: RECORD_AUDIO: ignore; time=+3d2h ago
Default mode: deny
`
	readTests := []struct {
		name  string
		op    string
		want  string
		state State
	}{
		{"stored row is explicit", "CAMERA", "allow", Explicit},
		{"uid row is explicit", "RECORD_AUDIO", "ignore", Explicit},
		{"default is implicit", "VIBRATE", "deny", Implicit},
	}
	for _, tc := range readTests {
		t.Run("read "+tc.name, func(t *testing.T) {
			got, state := parseAppOp(output, tc.op)
			if got != tc.want || state != tc.state {
				t.Errorf("parseAppOp(%q) = %q, %v; want %q, %v", tc.op, got, state, tc.want, tc.state)
			}
		})
	}

	if got, state := parseAppOp("Error: Unknown operation string: NOPE", "NOPE"); got != "" || state != Unset {
		t.Errorf("parseAppOp(unknown op) = %q, %v; want unset", got, state)
	}

	commandTests := []struct {
		name    string
		entry   Entry
		want    string
		wantErr string
	}{
		{
			name:  "allow",
			entry: Entry{Key: "com.example.app:CAMERA", Value: "allow"},
			want:  "cmd appops set com.example.app CAMERA allow",
		},
		{
			name:  "any lowercase word",
			entry: Entry{Key: "com.example.app:RECORD_AUDIO", Value: "ignore"},
			want:  "cmd appops set com.example.app RECORD_AUDIO ignore",
		},
		{
			name:    "uppercase mode",
			entry:   Entry{Key: "com.example.app:CAMERA", Value: "ALLOW"},
			wantErr: "invalid app ops mode",
		},
		{
			name:    "lowercase op",
			entry:   Entry{Key: "com.example.app:camera", Value: "allow"},
			wantErr: "invalid app op",
		},
	}
	for _, tc := range commandTests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := appOpsStore{}.writeCommand(tc.entry)
			checkCommand(t, got, err, tc.want, tc.wantErr)
		})
	}

	got, err := appOpsStore{}.resetCommand("com.example.app:CAMERA")
	checkCommand(t, got, err, "cmd appops set com.example.app CAMERA default", "")
}

func TestRoleStore(t *testing.T) {
	readTests := []struct {
		name string
		out  string
		want string
	}{
		{"single holder", "com.example.sms\n", "com.example.sms"},
		{"holders joined with a semicolon", "com.example.sms;com.example.backup\n", "com.example.sms,com.example.backup"},
		{"unheld prints an empty line", "\n", ""},
		{"empty output", "", ""},
	}
	for _, tc := range readTests {
		t.Run("read "+tc.name, func(t *testing.T) {
			if got := parseRole(tc.out); got != tc.want {
				t.Errorf("parseRole(%q) = %q, want %q", tc.out, got, tc.want)
			}
		})
	}

	commandTests := []struct {
		name    string
		entry   Entry
		want    string
		wantErr string
	}{
		{
			name:  "one holder",
			entry: Entry{Key: "android.app.role.SMS", Value: "com.example.sms"},
			want:  "cmd role add-role-holder android.app.role.SMS com.example.sms",
		},
		{
			name:  "empty clears",
			entry: Entry{Key: "android.app.role.SMS", Value: ""},
			want:  "cmd role clear-role-holders android.app.role.SMS",
		},
		{
			name:    "two packages are rejected",
			entry:   Entry{Key: "android.app.role.SMS", Value: "com.example.sms,com.example.backup"},
			wantErr: "more than one package",
		},
		{
			name:    "invalid role",
			entry:   Entry{Key: "SMS", Value: "com.example.sms"},
			wantErr: "invalid role",
		},
		{
			name:    "invalid package",
			entry:   Entry{Key: "android.app.role.SMS", Value: "com.example.sms;reboot"},
			wantErr: "not a package name",
		},
	}
	for _, tc := range commandTests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := roleStore{}.writeCommand(tc.entry)
			checkCommand(t, got, err, tc.want, tc.wantErr)
		})
	}

	got, err := roleStore{}.resetCommand("android.app.role.SMS")
	checkCommand(t, got, err, "cmd role clear-role-holders android.app.role.SMS", "")
}

func TestDeviceIdleStore(t *testing.T) {
	const output = `system-excidle,com.example.app,10123
system,com.example.other,1000
user,com.example.allowed,10124
`
	readTests := []struct {
		name  string
		pkg   string
		want  string
		state State
	}{
		{"user row is explicit", "com.example.allowed", "allow", Explicit},
		{"system-excidle row is implicit", "com.example.app", "allow", Implicit},
		{"system row ignores", "com.example.other", "ignore", Explicit},
		{"absent package ignores", "com.example.absent", "ignore", Explicit},
	}
	for _, tc := range readTests {
		t.Run("read "+tc.name, func(t *testing.T) {
			got, state := parseDeviceIdle(output, tc.pkg)
			if got != tc.want || state != tc.state {
				t.Errorf("parseDeviceIdle(%q) = %q, %v; want %q, %v", tc.pkg, got, state, tc.want, tc.state)
			}
		})
	}

	commandTests := []struct {
		name    string
		entry   Entry
		want    string
		wantErr string
	}{
		{
			name:  "allow",
			entry: Entry{Key: "com.example.app", Value: "allow"},
			want:  "cmd deviceidle whitelist +com.example.app",
		},
		{
			name:  "ignore",
			entry: Entry{Key: "com.example.app", Value: "ignore"},
			want:  "cmd deviceidle whitelist -com.example.app",
		},
		{
			name:    "unknown value",
			entry:   Entry{Key: "com.example.app", Value: "deny"},
			wantErr: "allow or ignore",
		},
		{
			name:    "invalid package",
			entry:   Entry{Key: "com.example/app", Value: "allow"},
			wantErr: "not a package name",
		},
	}
	for _, tc := range commandTests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := deviceIdleStore{}.writeCommand(tc.entry)
			checkCommand(t, got, err, tc.want, tc.wantErr)
		})
	}

	got, err := deviceIdleStore{}.resetCommand("com.example.app")
	checkCommand(t, got, err, "cmd deviceidle whitelist -com.example.app", "")
}

func TestStandbyStore(t *testing.T) {
	readTests := []struct {
		name  string
		out   string
		want  string
		state State
	}{
		{"exempted is the doze floor", "5", "exempted", Implicit},
		{"active", "10\n", "active", Explicit},
		{"working set", "20", "working_set", Explicit},
		{"frequent", "30", "frequent", Explicit},
		{"rare", "40", "rare", Explicit},
		{"restricted", "45", "restricted", Explicit},
		{"never", "50", "never", Explicit},
	}
	for _, tc := range readTests {
		t.Run("read "+tc.name, func(t *testing.T) {
			got, state, err := parseStandby(tc.out)
			if err != nil {
				t.Fatalf("parseStandby(%q): %v", tc.out, err)
			}
			if got != tc.want || state != tc.state {
				t.Errorf("parseStandby(%q) = %q, %v; want %q, %v", tc.out, got, state, tc.want, tc.state)
			}
		})
	}
	if _, _, err := parseStandby("99"); err == nil {
		t.Error("parseStandby(99) succeeded, want an error")
	}

	commandTests := []struct {
		name    string
		entry   Entry
		want    string
		wantErr string
	}{
		{
			name:  "active",
			entry: Entry{Key: "com.example.app", Value: "active"},
			want:  "am set-standby-bucket com.example.app active",
		},
		{
			name:  "never is writable",
			entry: Entry{Key: "com.example.app", Value: "never"},
			want:  "am set-standby-bucket com.example.app never",
		},
		{
			name:    "exempted is derived",
			entry:   Entry{Key: "com.example.app", Value: "exempted"},
			wantErr: "doze-whitelisted",
		},
		{
			name:    "unknown bucket",
			entry:   Entry{Key: "com.example.app", Value: "sleeping"},
			wantErr: "invalid standby bucket",
		},
	}
	for _, tc := range commandTests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := standbyStore{}.writeCommand(tc.entry)
			checkCommand(t, got, err, tc.want, tc.wantErr)
		})
	}

	ok, message := standbyStore{}.Reset(nil, Entry{Namespace: "standby", Key: "com.example.app"})
	if ok || message != "a standby bucket cannot be unset" {
		t.Errorf("Reset() = %v, %q; want false, %q", ok, message, "a standby bucket cannot be unset")
	}
}

func TestLocaleStore(t *testing.T) {
	readTests := []struct {
		name    string
		out     string
		want    string
		wantErr string
	}{
		{"one locale", "Locales for com.example.app for user 0 are [en-US]", "en-US", ""},
		{"locale list", "Locales for com.example.app for user 0 are [en-US,sl-SI]", "en-US,sl-SI", ""},
		{"system default", "Locales for com.example.app for user 0 are []", "", ""},
		{"unknown package", "Unknown package com.example.notinstalled for userId 0", "", "Unknown package com.example.notinstalled"},
	}
	for _, tc := range readTests {
		t.Run("read "+tc.name, func(t *testing.T) {
			got, err := parseLocales(tc.out)
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("err = %v, want containing %q", err, tc.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("parseLocales: %v", err)
			}
			if got != tc.want {
				t.Errorf("parseLocales() = %q, want %q", got, tc.want)
			}
		})
	}

	commandTests := []struct {
		name    string
		entry   Entry
		want    string
		wantErr string
	}{
		{
			name:  "locale list",
			entry: Entry{Key: "com.example.app", Value: "en-US,sl-SI"},
			want:  "cmd locale set-app-locales com.example.app --locales en-US,sl-SI",
		},
		{
			name:  "empty clears",
			entry: Entry{Key: "com.example.app", Value: ""},
			want:  "cmd locale set-app-locales com.example.app --locales ''",
		},
		{
			name:    "underscore is not a locale separator",
			entry:   Entry{Key: "com.example.app", Value: "en_US"},
			wantErr: "invalid locale",
		},
		{
			name:    "invalid package",
			entry:   Entry{Key: "bad package", Value: "en-US"},
			wantErr: "not a package name",
		},
	}
	for _, tc := range commandTests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := localeStore{}.writeCommand(tc.entry)
			checkCommand(t, got, err, tc.want, tc.wantErr)
		})
	}

	got, err := localeStore{}.resetCommand("com.example.app")
	checkCommand(t, got, err, "cmd locale set-app-locales com.example.app --locales ''", "")
}

func TestAppLinksStore(t *testing.T) {
	readTests := []struct {
		name  string
		out   string
		want  string
		state State
	}{
		{"allowed", "com.example.app:\n  User 0:\n    Verification link handling allowed: true\n", "allowed", Explicit},
		{"disallowed", "com.example.app:\n  User 0:\n    Verification link handling allowed: false\n", "disallowed", Explicit},
		{"declares no web links", "com.morphe.android.youtube:\n  User 0:\n    Selection state:\n", "", Unset},
		{"empty output", "", "", Unset},
	}
	for _, tc := range readTests {
		t.Run("read "+tc.name, func(t *testing.T) {
			got, state := parseAppLinks(tc.out)
			if got != tc.want || state != tc.state {
				t.Errorf("parseAppLinks() = %q, %v; want %q, %v", got, state, tc.want, tc.state)
			}
		})
	}

	commandTests := []struct {
		name    string
		entry   Entry
		want    string
		wantErr string
	}{
		{
			name:  "allowed",
			entry: Entry{Key: "com.example.app", Value: "allowed"},
			want:  "pm set-app-links-allowed --user 0 --package com.example.app true",
		},
		{
			name:  "disallowed",
			entry: Entry{Key: "com.example.app", Value: "disallowed"},
			want:  "pm set-app-links-allowed --user 0 --package com.example.app false",
		},
		{
			name:    "unknown value",
			entry:   Entry{Key: "com.example.app", Value: "deny"},
			wantErr: "allowed or disallowed",
		},
	}
	for _, tc := range commandTests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := appLinksStore{}.writeCommand(tc.entry)
			checkCommand(t, got, err, tc.want, tc.wantErr)
		})
	}

	ok, message := appLinksStore{}.Reset(nil, Entry{Namespace: "applinks", Key: "com.example.app"})
	if ok || message != "link handling has no unset state" {
		t.Errorf("Reset() = %v, %q; want false, %q", ok, message, "link handling has no unset state")
	}
}

func TestAppLinksDomainStore(t *testing.T) {
	const output = `com.example.app:
  User 0:
    Verification link handling allowed: true
    Selection state:
      Enabled:
        foo-bar.example
    Domain verification state:
      foo-bar.example: approved
      other.example: denied
      configured.example: system_configured
      old.example: migrated
      failure.example: legacy_failure
      coded.example: 1024
      plain.example: none
`
	readTests := []struct {
		name   string
		domain string
		want   string
		state  State
	}{
		{"approved", "foo-bar.example", "approved", Explicit},
		{"denied", "other.example", "denied", Explicit},
		{"none is writable", "plain.example", "none", Explicit},
		{"system_configured is derived", "configured.example", "system_configured", Implicit},
		{"migrated is derived", "old.example", "migrated", Implicit},
		{"legacy_failure is derived", "failure.example", "legacy_failure", Implicit},
		{"codes from 1024 up are derived", "coded.example", "1024", Implicit},
		{"undeclared domain is unset", "absent.example", "", Unset},
	}
	for _, tc := range readTests {
		t.Run("read "+tc.name, func(t *testing.T) {
			got, state := parseDomainState(output, tc.domain)
			if got != tc.want || state != tc.state {
				t.Errorf("parseDomainState(%q) = %q, %v; want %q, %v", tc.domain, got, state, tc.want, tc.state)
			}
		})
	}

	// Each raw token the platform prints, with the state it implies.
	tokenTests := []struct {
		token string
		state State
	}{
		{"none", Explicit},
		{"verified", Explicit},
		{"approved", Explicit},
		{"denied", Explicit},
		{"0", Explicit},
		{"1", Explicit},
		{"2", Explicit},
		{"3", Explicit},
		{"system_configured", Implicit},
		{"pre_verified", Implicit},
		{"migrated", Implicit},
		{"restored", Implicit},
		{"legacy_failure", Implicit},
		{"1024", Implicit},
		{"4096", Implicit},
	}
	for _, tc := range tokenTests {
		t.Run("state "+tc.token, func(t *testing.T) {
			out := "Domain verification state:\n  foo-bar.example: " + tc.token + "\n"
			got, state := parseDomainState(out, "foo-bar.example")
			if got != tc.token || state != tc.state {
				t.Errorf("parseDomainState() = %q, %v; want %q, %v", got, state, tc.token, tc.state)
			}
		})
	}

	commandTests := []struct {
		name    string
		entry   Entry
		want    string
		wantErr string
	}{
		{
			name:  "none",
			entry: Entry{Key: "com.example.app:foo-bar.example", Value: "none"},
			want:  "pm set-app-links --package com.example.app 0 foo-bar.example",
		},
		{
			name:  "verified is writable",
			entry: Entry{Key: "com.example.app:foo-bar.example", Value: "verified"},
			want:  "pm set-app-links --package com.example.app 1 foo-bar.example",
		},
		{
			name:  "bare code is writable",
			entry: Entry{Key: "com.example.app:foo-bar.example", Value: "1"},
			want:  "pm set-app-links --package com.example.app 1 foo-bar.example",
		},
		{
			name:  "approved",
			entry: Entry{Key: "com.example.app:foo-bar.example", Value: "approved"},
			want:  "pm set-app-links --package com.example.app 2 foo-bar.example",
		},
		{
			name:  "denied",
			entry: Entry{Key: "com.example.app:foo-bar.example", Value: "denied"},
			want:  "pm set-app-links --package com.example.app 3 foo-bar.example",
		},
		{
			name:  "wildcard domain",
			entry: Entry{Key: "com.example.app:*.discord.com", Value: "approved"},
			want:  "pm set-app-links --package com.example.app 2 '*.discord.com'",
		},
		{
			name:    "platform-decided token is not writable",
			entry:   Entry{Key: "com.example.app:foo-bar.example", Value: "system_configured"},
			wantErr: "use none (0), verified (1), approved (2) or denied (3)",
		},
		{
			name:    "invalid domain",
			entry:   Entry{Key: "com.example.app:Foo.example", Value: "approved"},
			wantErr: "invalid domain",
		},
	}
	for _, tc := range commandTests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := appLinksDomainStore{}.writeCommand(tc.entry)
			checkCommand(t, got, err, tc.want, tc.wantErr)
		})
	}

	got, err := appLinksDomainStore{}.resetCommand("com.example.app:foo-bar.example")
	checkCommand(t, got, err, "pm set-app-links --package com.example.app 0 foo-bar.example", "")

	got, err = appLinksDomainStore{}.resetCommand("com.example.app:*.discord.com")
	checkCommand(t, got, err, "pm set-app-links --package com.example.app 0 '*.discord.com'", "")
}

func TestAppLinksSelectionStore(t *testing.T) {
	const output = `com.example.app:
  User 0:
    Verification link handling allowed: true
    Selection state:
      Enabled:
        *.discord.com
        foo-bar.example
      Disabled:
        disabled.example
    Domain verification state:
      foo-bar.example: approved
`
	readTests := []struct {
		name   string
		domain string
		want   string
		state  State
	}{
		{"enabled wildcard", "*.discord.com", "enabled", Explicit},
		{"enabled", "foo-bar.example", "enabled", Explicit},
		{"disabled", "disabled.example", "disabled", Explicit},
		{"absent from both lists", "absent.example", "", Unset},
	}
	for _, tc := range readTests {
		t.Run("read "+tc.name, func(t *testing.T) {
			got, state := parseSelectionState(output, tc.domain)
			if got != tc.want || state != tc.state {
				t.Errorf("parseSelectionState(%q) = %q, %v; want %q, %v", tc.domain, got, state, tc.want, tc.state)
			}
		})
	}

	commandTests := []struct {
		name    string
		entry   Entry
		want    string
		wantErr string
	}{
		{
			name:  "enabled",
			entry: Entry{Key: "com.example.app:foo-bar.example", Value: "enabled"},
			want:  "pm set-app-links-user-selection --user 0 --package com.example.app true foo-bar.example",
		},
		{
			name:  "disabled",
			entry: Entry{Key: "com.example.app:foo-bar.example", Value: "disabled"},
			want:  "pm set-app-links-user-selection --user 0 --package com.example.app false foo-bar.example",
		},
		{
			name:  "wildcard domain",
			entry: Entry{Key: "com.example.app:*.discord.com", Value: "enabled"},
			want:  "pm set-app-links-user-selection --user 0 --package com.example.app true '*.discord.com'",
		},
		{
			name:    "unknown value",
			entry:   Entry{Key: "com.example.app:foo-bar.example", Value: "allowed"},
			wantErr: "enabled or disabled",
		},
		{
			name:    "invalid domain",
			entry:   Entry{Key: "com.example.app:Foo.example", Value: "enabled"},
			wantErr: "invalid domain",
		},
	}
	for _, tc := range commandTests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := appLinksSelectionStore{}.writeCommand(tc.entry)
			checkCommand(t, got, err, tc.want, tc.wantErr)
		})
	}

	ok, message := appLinksSelectionStore{}.Reset(nil, Entry{Namespace: "applinks_selection", Key: "com.example.app:foo-bar.example"})
	if ok || !strings.Contains(message, "no unset state") {
		t.Errorf("Reset() = %v, %q; want false with a no-unset-state message", ok, message)
	}
}

func TestPackages(t *testing.T) {
	tests := []struct {
		name    string
		entry   Entry
		want    []string
		wantErr string
	}{
		{"settings names no package", Entry{Namespace: "global", Key: "window_animation_scale"}, nil, ""},
		{"permission", Entry{Namespace: "permissions", Key: "com.example.app:android.permission.CAMERA"}, []string{"com.example.app"}, ""},
		{"app op", Entry{Namespace: "appops", Key: "com.example.app:CAMERA"}, []string{"com.example.app"}, ""},
		{"role holder", Entry{Namespace: "role", Key: "android.app.role.SMS", Value: "com.example.sms"}, []string{"com.example.sms"}, ""},
		{"empty role", Entry{Namespace: "role", Key: "android.app.role.SMS", Value: ""}, nil, ""},
		{"deviceidle", Entry{Namespace: "deviceidle", Key: "com.example.app"}, []string{"com.example.app"}, ""},
		{"standby", Entry{Namespace: "standby", Key: "com.example.app"}, []string{"com.example.app"}, ""},
		{"locale", Entry{Namespace: "locale", Key: "com.example.app"}, []string{"com.example.app"}, ""},
		{"applinks", Entry{Namespace: "applinks", Key: "com.example.app"}, []string{"com.example.app"}, ""},
		{"applinks domain", Entry{Namespace: "applinks_domain", Key: "com.example.app:foo-bar.example"}, []string{"com.example.app"}, ""},
		{"applinks selection", Entry{Namespace: "applinks_selection", Key: "com.example.app:*.discord.com"}, []string{"com.example.app"}, ""},
		{"malformed permission key", Entry{Namespace: "permissions", Key: "com.example.app"}, nil, "<package>:<name>"},
		{"malformed standby key", Entry{Namespace: "standby", Key: "not a package"}, nil, "not a package name"},
		{"multi-package role", Entry{Namespace: "role", Key: "android.app.role.SMS", Value: "a.b,c.d"}, nil, "more than one package"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := Packages(tc.entry)
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("err = %v, want containing %q", err, tc.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("Packages: %v", err)
			}
			if !slices.Equal(got, tc.want) {
				t.Errorf("Packages() = %q, want %q", got, tc.want)
			}
		})
	}
}

// Every part of a mutated command must survive `adb shell`. An empty value and
// a value with a space must arrive as one argument; a hyphen is already safe
// and must not grow quotes, while a wildcard domain must be quoted so the shell
// does not expand it.
func TestMutatedCommandsQuoteEveryPart(t *testing.T) {
	tests := []struct {
		name   string
		prefix []string
		parts  []string
		want   string
	}{
		{
			name:   "empty value",
			prefix: []string{"cmd", "locale", "set-app-locales"},
			parts:  []string{"com.example.app", "--locales", ""},
			want:   "cmd locale set-app-locales com.example.app --locales ''",
		},
		{
			name:   "value with a space",
			prefix: []string{"pm", "grant"},
			parts:  []string{"com.example.app", "two words"},
			want:   "pm grant com.example.app 'two words'",
		},
		{
			name:   "domain with a hyphen",
			prefix: []string{"pm", "set-app-links", "--package"},
			parts:  []string{"com.example.app", "0", "foo-bar.example"},
			want:   "pm set-app-links --package com.example.app 0 foo-bar.example",
		},
		{
			name:   "wildcard domain",
			prefix: []string{"pm", "set-app-links-user-selection", "--user", "0", "--package"},
			parts:  []string{"com.example.app", "true", "*.discord.com"},
			want:   "pm set-app-links-user-selection --user 0 --package com.example.app true '*.discord.com'",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := render(tc.prefix, tc.parts...); got != tc.want {
				t.Errorf("render() = %q, want %q", got, tc.want)
			}
		})
	}
}

func checkCommand(t *testing.T, got string, err error, want, wantErr string) {
	t.Helper()
	if wantErr != "" {
		if err == nil || !strings.Contains(err.Error(), wantErr) {
			t.Fatalf("got %q, err = %v; want error containing %q", got, err, wantErr)
		}
		return
	}
	if err != nil {
		t.Fatalf("err = %v, want command %q", err, want)
	}
	if got != want {
		t.Errorf("command = %q, want %q", got, want)
	}
}
