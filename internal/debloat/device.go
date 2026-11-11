// Package debloat reads a connected Android device and disables packages the
// human approved, one at a time.
//
// The tooling holds no policy and no package list. It reports facts about the
// phone and applies exactly what it is told, so the judgement stays with the
// person holding the device.
//
// There is one mutation in this package: `pm disable-user --user 0 <pkg>`, and
// its inverse `pm enable --user 0 <pkg>`. Uninstall is deliberately absent. On
// Android 17 (measured, API 37) `pm uninstall --user 0` is refused for packages
// in read-only partitions and deletes the APK of a removable preload in
// /data/app, where `cmd package install-existing` cannot restore it.
package debloat

import (
	"errors"
	"fmt"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
)

// Package states as reported by `pm list packages -e/-d`.
const (
	Enabled  = "enabled"
	Disabled = "disabled"
	Absent   = "absent"
)

// appSlots are the partition subdirectories that hold APKs. Anything else under
// a partition is a package path in its own right, e.g. /apex/<name>@<version>.
var appSlots = map[string]bool{"app": true, "priv-app": true, "overlay": true}

// packageRE is deliberately narrow. These names are interpolated into
// `adb shell`, so anything wider would be a shell-injection hole.
var packageRE = regexp.MustCompile(`^[A-Za-z0-9_.]+$`)

// ValidPackage reports whether name is a plain Android package id.
func ValidPackage(name string) bool {
	return packageRE.MatchString(name)
}

// ErrNoDevice is returned when the serial cannot be resolved.
var ErrNoDevice = fmt.Errorf("expected exactly one device")

// Device is a handle on one attached device. It shells out to adb, which is
// installed by the devenv shell.
type Device struct {
	Serial string
	ADB    string
	// memo caches read-only output for the life of the handle, so stores that
	// parse one dump ask the device once per run.
	memo map[string]string
}

// NewDevice resolves a serial, or picks the only attached device when empty.
func NewDevice(serial string) (*Device, error) {
	adb := "adb"
	if serial != "" {
		return &Device{Serial: serial, ADB: adb}, nil
	}
	out, err := exec.Command(adb, "devices").Output()
	if err != nil {
		return nil, fmt.Errorf("adb devices: %w", err)
	}
	var attached []string
	for _, line := range strings.Split(string(out), "\n")[1:] {
		fields := strings.Fields(line)
		if len(fields) < 2 || fields[1] != "device" {
			continue
		}
		attached = append(attached, fields[0])
	}
	if len(attached) != 1 {
		return nil, fmt.Errorf("found %d; pass --serial: %w", len(attached), ErrNoDevice)
	}
	return &Device{Serial: attached[0], ADB: adb}, nil
}

// Run executes one adb invocation and returns its stdout.
//
// It clears the read cache like Shell: `install` is the one mutation that does
// not go through Shell, and a stale dump after an install would be wrong.
func (d *Device) Run(args ...string) (string, error) {
	d.memo = nil
	full := append([]string{"-s", d.Serial}, args...)
	out, err := exec.Command(d.ADB, full...).Output()
	if err != nil {
		var exit *exec.ExitError
		if errors.As(err, &exit) {
			return "", fmt.Errorf("adb %s: %s: %w", strings.Join(args, " "), strings.TrimSpace(string(exit.Stderr)), err)
		}
		return "", fmt.Errorf("adb %s: %w", strings.Join(args, " "), err)
	}
	return string(out), nil
}

// Shell runs a device shell command and returns (exitCode, stdout, stderr).
//
// The exit code is what distinguishes a refused disable (255) from a no-op (0).
// Collapsing it into an error would throw away the device's own message, which is
// the only useful thing to show a person.
//
// Every mutation goes through Shell, so running one drops the read cache: a
// readback after a write must see the device, not the value read before it.
func (d *Device) Shell(command string) (int, string, string) {
	d.memo = nil
	return d.shell(command)
}

func (d *Device) shell(command string) (int, string, string) {
	cmd := exec.Command(d.ADB, "-s", d.Serial, "shell", command)
	var stdout, stderr strings.Builder
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err := cmd.Run()
	code := 0
	if err != nil {
		var exit *exec.ExitError
		if errors.As(err, &exit) {
			code = exit.ExitCode()
		} else {
			code = -1
			stderr.WriteString(err.Error())
		}
	}
	return code, stdout.String(), stderr.String()
}

// Read runs a read-only shell command once per handle and remembers its stdout.
// The stores parse a handful of device dumps that several entries share; asking
// once keeps a run to one round trip per dump instead of one per entry.
func (d *Device) Read(command string) (string, error) {
	if out, ok := d.memo[command]; ok {
		return out, nil
	}
	code, out, errOut := d.shell(command)
	if code != 0 {
		return "", fmt.Errorf("%s: %s", command, strings.TrimSpace(errOut))
	}
	if d.memo == nil {
		d.memo = map[string]string{}
	}
	d.memo[command] = out
	return out, nil
}

// Tier reduces an APK path to its partition slot, e.g. "system/priv-app".
func Tier(path string) string {
	if path == "" || path == "null" {
		return "unknown"
	}
	parts := []string{}
	for _, part := range strings.Split(path, "/") {
		if part != "" {
			parts = append(parts, part)
		}
	}
	if len(parts) == 0 {
		return "unknown"
	}
	if len(parts) > 1 && appSlots[parts[1]] {
		return parts[0] + "/" + parts[1]
	}
	return parts[0]
}

// States returns package -> enabled/disabled. Absent packages are not listed.
func (d *Device) States() (map[string]string, error) {
	result := map[string]string{}
	for _, flag := range []struct{ flag, state string }{{"-e", Enabled}, {"-d", Disabled}} {
		out, err := d.Run("shell", "pm list packages "+flag.flag)
		if err != nil {
			return nil, err
		}
		for _, line := range strings.Split(out, "\n") {
			if name, ok := strings.CutPrefix(line, "package:"); ok {
				result[strings.TrimSpace(name)] = flag.state
			}
		}
	}
	return result, nil
}

// Installed returns the packages present for user 0. `pm list packages` without
// --user covers every user, so a package installed only in a work profile would
// pass a skip check and then fail against user 0.
func (d *Device) Installed() (map[string]bool, error) {
	out, err := d.Run("shell", "pm list packages --user 0")
	if err != nil {
		return nil, err
	}
	installed := map[string]bool{}
	for _, line := range strings.Split(out, "\n") {
		if name, ok := strings.CutPrefix(strings.TrimSpace(line), "package:"); ok {
			installed[name] = true
		}
	}
	return installed, nil
}

// StateOf classifies one package against a States map.
//
// A missing key is Absent. This exists because Go returns "" for a missing key
// while Python's dict.get(pkg, ABSENT) returned ABSENT, and a preview that
// claims it would disable a package the device does not have is a lie.
func StateOf(states map[string]string, pkg string) string {
	switch state := states[pkg]; state {
	case Enabled, Disabled:
		return state
	default:
		return Absent
	}
}

// Tiers returns package -> partition slot, from the installed APK path.
func (d *Device) Tiers() (map[string]string, error) {
	out, err := d.Run("shell", "pm list packages -f")
	if err != nil {
		return nil, err
	}
	return parseTiers(out), nil
}

// parseTiers handles `package:<path>=<name>` where the path itself contains
// `==`, as every /data/app path does.
func parseTiers(out string) map[string]string {
	result := map[string]string{}
	for _, line := range strings.Split(out, "\n") {
		body, ok := strings.CutPrefix(strings.TrimSpace(line), "package:")
		if !ok {
			continue
		}
		separator := strings.LastIndexByte(body, '=')
		if separator < 0 {
			continue
		}
		path, name := body[:separator], body[separator+1:]
		if ValidPackage(name) {
			result[name] = Tier(path)
		}
	}
	return result
}

// packageBlocks splits `dumpsys package` into package name -> body. One call for
// the whole device is roughly 1.2s and 118k lines, far cheaper than one per
// package.
func (d *Device) packageBlocks() (map[string]string, error) {
	out, err := d.Run("shell", "dumpsys package")
	if err != nil {
		return nil, err
	}
	header := regexp.MustCompile(`(?m)^  Package \[([A-Za-z0-9_.]+)\] \([0-9a-f]+\):`)
	matches := header.FindAllStringSubmatchIndex(out, -1)
	blocks := make(map[string]string, len(matches))
	for i, match := range matches {
		name := out[match[2]:match[3]]
		start := match[1]
		end := len(out)
		if i+1 < len(matches) {
			end = matches[i+1][0]
		}
		blocks[name] = out[start:end]
	}
	return blocks, nil
}

// PackageFacts returns package -> shared uid and package -> target SDK from one
// `dumpsys package` call. The dump is ~118k lines, so reading it twice costs a
// second and the same data. A target SDK far behind the OS means abandoned,
// which is a hint, not a safety guarantee.
func (d *Device) PackageFacts() (map[string]string, map[string]int, error) {
	blocks, err := d.packageBlocks()
	if err != nil {
		return nil, nil, err
	}
	user := regexp.MustCompile(`sharedUser=SharedUserSetting\{[0-9a-f]+ ([^/]+)/\d+\}`)
	sdk := regexp.MustCompile(`targetSdk=(\d+)`)
	uids := map[string]string{}
	sdks := map[string]int{}
	for name, body := range blocks {
		if match := user.FindStringSubmatch(body); match != nil {
			uids[name] = match[1]
		}
		if match := sdk.FindStringSubmatch(body); match != nil {
			if value, err := strconv.Atoi(match[1]); err == nil {
				sdks[name] = value
			}
		}
	}
	return uids, sdks, nil
}

// RoleHolders returns role -> holding package, empty when nobody holds it. A
// role this firmware does not know and an empty role look the same, so only a
// transport failure is an error.
func (d *Device) RoleHolders() (map[string]string, error) {
	holders := map[string]string{}
	for _, role := range []string{"HOME", "DIALER", "SMS", "BROWSER", "ASSISTANT", "EMERGENCY", "WALLET"} {
		code, out, errOut := d.Shell("cmd role get-role-holders android.app.role." + role)
		if code == -1 {
			return nil, fmt.Errorf("reading %s role: %s", role, strings.TrimSpace(errOut))
		}
		holder := ""
		if code == 0 {
			if lines := strings.Split(strings.TrimSpace(out), "\n"); lines[0] != "" {
				holder = strings.TrimSpace(lines[0])
			}
		}
		holders[role] = holder
	}
	return holders, nil
}

// Launchers returns every package offering a HOME activity.
func (d *Device) Launchers() ([]string, error) {
	code, out, errOut := d.Shell(
		"cmd package query-activities --components " +
			"-a android.intent.action.MAIN -c android.intent.category.HOME")
	if code == -1 {
		return nil, fmt.Errorf("querying launchers: %s", strings.TrimSpace(errOut))
	}
	var found []string
	for _, line := range strings.Split(out, "\n") {
		if strings.Contains(line, "/") && !strings.HasPrefix(line, " ") {
			found = append(found, strings.SplitN(line, "/", 2)[0])
		}
	}
	return found, nil
}

// Keyboard returns the default input method's package, or "".
func (d *Device) Keyboard() (string, error) {
	code, out, errOut := d.Shell("settings get secure default_input_method")
	if code == -1 {
		return "", fmt.Errorf("reading default keyboard: %s", strings.TrimSpace(errOut))
	}
	out = strings.TrimSpace(out)
	if out == "" || out == "null" {
		return "", nil
	}
	return strings.SplitN(out, "/", 2)[0], nil
}

// Crashes counts FATAL EXCEPTION entries in the crash buffer. The buffer is
// historical, so this is information, not a health verdict.
func (d *Device) Crashes() (int, error) {
	code, out, errOut := d.Shell("logcat -d -b crash")
	if code == -1 {
		return 0, fmt.Errorf("reading crash buffer: %s", strings.TrimSpace(errOut))
	}
	return strings.Count(out, "FATAL EXCEPTION"), nil
}

// BootCompleted reports whether the device finished booting.
func (d *Device) BootCompleted() (bool, error) {
	code, out, errOut := d.Shell("getprop sys.boot_completed")
	if code == -1 {
		return false, fmt.Errorf("reading sys.boot_completed: %s", strings.TrimSpace(errOut))
	}
	return strings.TrimSpace(out) == "1", nil
}

// Mutate runs `pm <verb> --user 0 <pkg>` and returns the device's own message.
func (d *Device) Mutate(verb, pkg string) (bool, string, error) {
	if !ValidPackage(pkg) {
		return false, "", fmt.Errorf("not a package name: %q", pkg)
	}
	code, out, errOut := d.Shell(fmt.Sprintf("pm %s --user 0 %s", verb, pkg))
	return code == 0, explain(out + errOut), nil
}

// explain picks the sentence a human needs out of a `pm` failure.
//
// A refused disable prints "Exception occurred while executing", then a stack
// trace whose last line is always "at android.os.Binder.execTransact(...)".
// Neither tells anybody why. The reason is the line carrying the actual
// exception class or the platform's Failure message, so that is what is shown.
func explain(raw string) string {
	var prefix string
	for _, line := range strings.Split(raw, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		switch {
		case strings.HasPrefix(line, "at "):
			// Stack frame: never the answer.
		case strings.HasPrefix(line, "Exception occurred while executing"):
			prefix = line
		case strings.Contains(line, "Failure ["):
			return line
		case strings.Contains(line, "Exception") && !strings.HasPrefix(line, "Exception occurred"):
			return line
		case prefix == "":
			prefix = line
		}
	}
	return prefix
}

// Disable is the only destructive-adjacent action the toolchain offers, and it
// is reversible with Enable.
func (d *Device) Disable(pkg string) (bool, string, error) {
	return d.Mutate("disable-user", pkg)
}

// Enable reverses Disable.
func (d *Device) Enable(pkg string) (bool, string, error) {
	return d.Mutate("enable", pkg)
}
