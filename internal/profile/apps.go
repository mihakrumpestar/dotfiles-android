package profile

import (
	"fmt"
	"regexp"
	"strings"

	"github.com/mihakrumpestar/dotfiles-android/internal/debloat"
)

// The app-scoped stores name a package (and sometimes a second part) in every
// command, so each part is validated against the narrowest grammar that still
// covers real devices.
var (
	opRE     = regexp.MustCompile(`^[A-Z0-9_]+$`)
	permRE   = regexp.MustCompile(`^[A-Za-z0-9_.]+$`)
	roleRE   = regexp.MustCompile(`^android\.app\.role\.[A-Z_]+$`)
	domainRE = regexp.MustCompile(`^(\*\.)?[a-z0-9]([a-z0-9.-]*[a-z0-9])?$`)
	localeRE = regexp.MustCompile(`^[a-z]{2,3}(-[A-Za-z0-9]+)*$`)
	modeRE   = regexp.MustCompile(`^[a-z]+$`)
)

// write runs one mutation and returns the device's own message.
func write(d *debloat.Device, command string) (bool, string) {
	code, out, errOut := d.Shell(command)
	message := strings.TrimSpace(out)
	if message == "" {
		message = strings.TrimSpace(errOut)
	}
	return code == 0, message
}

// splitPackageKey splits "<package>:<name>" and validates the package, the part
// every app-scoped command interpolates.
func splitPackageKey(key string) (pkg, name string, err error) {
	pkg, name, ok := strings.Cut(key, ":")
	if !ok || pkg == "" || name == "" {
		return "", "", fmt.Errorf("key %q must be <package>:<name>", key)
	}
	if !debloat.ValidPackage(pkg) {
		return "", "", fmt.Errorf("not a package name: %q", pkg)
	}
	return pkg, name, nil
}

func packageKey(key string) (string, error) {
	if !debloat.ValidPackage(key) {
		return "", fmt.Errorf("not a package name: %q", key)
	}
	return key, nil
}

// splitDomainKey splits "<package>:<domain>" and validates both parts. A
// wildcard domain keeps its leading "*.".
func splitDomainKey(key string) (pkg, domain string, err error) {
	pkg, domain, err = splitPackageKey(key)
	if err != nil {
		return "", "", err
	}
	if !domainRE.MatchString(domain) {
		return "", "", fmt.Errorf("invalid domain %q", domain)
	}
	return pkg, domain, nil
}

// Packages returns the packages an entry names, so a run can skip entries whose
// package the device does not install for user 0. A malformed key is a profile
// error, not a reason to exempt the entry from the skip rule.
func Packages(e Entry) ([]string, error) {
	switch e.Namespace {
	case "role":
		holder, err := roleHolder(e.Value)
		if err != nil {
			return nil, err
		}
		if holder == "" {
			return nil, nil
		}
		return []string{holder}, nil
	case "permissions", "appops":
		pkg, _, err := splitPackageKey(e.Key)
		if err != nil {
			return nil, err
		}
		return []string{pkg}, nil
	case "applinks_domain", "applinks_selection":
		pkg, _, err := splitDomainKey(e.Key)
		if err != nil {
			return nil, err
		}
		return []string{pkg}, nil
	case "deviceidle", "standby", "locale", "applinks":
		pkg, err := packageKey(e.Key)
		if err != nil {
			return nil, err
		}
		return []string{pkg}, nil
	default:
		return nil, nil
	}
}

// permissionsStore covers Android runtime permissions. The permission name is
// the only way to tell "revoked" from "this platform never heard of it": an
// absent line means the grant command is a silent no-op, so it must stay unset
// rather than compare equal to "revoked".
type permissionsStore struct{}

func (permissionsStore) Read(d *debloat.Device, e Entry) (string, State, error) {
	pkg, perm, err := splitPackageKey(e.Key)
	if err != nil {
		return "", Unset, err
	}
	if !permRE.MatchString(perm) {
		return "", Unset, fmt.Errorf("invalid permission %q", perm)
	}
	out, err := d.Read(render([]string{"dumpsys", "package"}, pkg))
	if err != nil {
		return "", Unset, err
	}
	value, state := parsePermission(out, perm)
	return value, state, nil
}

// parsePermission scans only the first `User 0:` block: the dump has one block
// per user, and a permission granted in another user says nothing about user 0.
// The block holds the runtime permissions; install permissions sit outside it
// and cannot be changed with `pm grant`, which is why they must not match.
func parsePermission(out, perm string) (string, State) {
	header := -1
	for _, raw := range strings.Split(out, "\n") {
		trimmed := strings.TrimSpace(raw)
		if trimmed == "" {
			continue
		}
		indent := len(raw) - len(strings.TrimLeft(raw, " \t"))
		if header < 0 {
			if strings.HasPrefix(trimmed, "User 0:") {
				header = indent
			}
			continue
		}
		if indent <= header {
			return "", Unset
		}
		name, rest, ok := strings.Cut(trimmed, ":")
		if !ok || name != perm {
			continue
		}
		switch value, _, _ := strings.Cut(strings.TrimSpace(rest), ","); value {
		case "granted=true":
			return "granted", Explicit
		case "granted=false":
			return "revoked", Explicit
		}
	}
	return "", Unset
}

func (s permissionsStore) Write(d *debloat.Device, e Entry) (bool, string) {
	command, err := s.writeCommand(e)
	if err != nil {
		return false, err.Error()
	}
	return write(d, command)
}

func (permissionsStore) writeCommand(e Entry) (string, error) {
	pkg, perm, err := splitPackageKey(e.Key)
	if err != nil {
		return "", err
	}
	if !permRE.MatchString(perm) {
		return "", fmt.Errorf("invalid permission %q", perm)
	}
	switch e.Value {
	case "granted":
		return render([]string{"pm", "grant"}, pkg, perm), nil
	case "revoked":
		return render([]string{"pm", "revoke"}, pkg, perm), nil
	default:
		return "", fmt.Errorf("permission value must be granted or revoked, got %q", e.Value)
	}
}

func (s permissionsStore) Reset(d *debloat.Device, e Entry) (bool, string) {
	command, err := s.resetCommand(e.Key)
	if err != nil {
		return false, err.Error()
	}
	return write(d, command)
}

func (permissionsStore) resetCommand(key string) (string, error) {
	pkg, perm, err := splitPackageKey(key)
	if err != nil {
		return "", err
	}
	if !permRE.MatchString(perm) {
		return "", fmt.Errorf("invalid permission %q", perm)
	}
	return render([]string{"pm", "revoke"}, pkg, perm), nil
}

// appOpsStore covers app ops. A stored row is Explicit; an op with no row only
// prints its default mode, which the platform derives, so it is Implicit.
type appOpsStore struct{}

func (appOpsStore) Read(d *debloat.Device, e Entry) (string, State, error) {
	pkg, op, err := splitPackageKey(e.Key)
	if err != nil {
		return "", Unset, err
	}
	if !opRE.MatchString(op) {
		return "", Unset, fmt.Errorf("invalid app op %q", op)
	}
	out, err := d.Read(render([]string{"cmd", "appops", "get"}, pkg, op))
	if err != nil {
		return "", Unset, err
	}
	value, state := parseAppOp(out, op)
	return value, state, nil
}

func parseAppOp(out, op string) (string, State) {
	fallback := ""
	for _, line := range strings.Split(out, "\n") {
		line = strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(line), "Uid mode:"))
		name, mode, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		name = strings.TrimSpace(name)
		mode = strings.TrimSpace(mode)
		if name == "Default mode" {
			if fallback == "" {
				fallback = mode
			}
			continue
		}
		if name != op {
			continue
		}
		if value, _, tail := strings.Cut(mode, ";"); tail {
			mode = strings.TrimSpace(value)
		}
		return mode, Explicit
	}
	if fallback != "" {
		return fallback, Implicit
	}
	return "", Unset
}

func (s appOpsStore) Write(d *debloat.Device, e Entry) (bool, string) {
	command, err := s.writeCommand(e)
	if err != nil {
		return false, err.Error()
	}
	return write(d, command)
}

func (appOpsStore) writeCommand(e Entry) (string, error) {
	pkg, op, err := splitPackageKey(e.Key)
	if err != nil {
		return "", err
	}
	if !opRE.MatchString(op) {
		return "", fmt.Errorf("invalid app op %q", op)
	}
	if !modeRE.MatchString(e.Value) {
		return "", fmt.Errorf("invalid app ops mode %q", e.Value)
	}
	return render([]string{"cmd", "appops", "set"}, pkg, op, e.Value), nil
}

func (s appOpsStore) Reset(d *debloat.Device, e Entry) (bool, string) {
	command, err := s.resetCommand(e.Key)
	if err != nil {
		return false, err.Error()
	}
	return write(d, command)
}

func (appOpsStore) resetCommand(key string) (string, error) {
	pkg, op, err := splitPackageKey(key)
	if err != nil {
		return "", err
	}
	if !opRE.MatchString(op) {
		return "", fmt.Errorf("invalid app op %q", op)
	}
	return render([]string{"cmd", "appops", "set"}, pkg, op, "default"), nil
}

// roleStore covers role holders. The key is the full role name; the value is one
// package, emptied with clear-role-holders.
type roleStore struct{}

func (roleStore) Read(d *debloat.Device, e Entry) (string, State, error) {
	if !roleRE.MatchString(e.Key) {
		return "", Unset, fmt.Errorf("invalid role %q", e.Key)
	}
	out, err := d.Read(render([]string{"cmd", "role", "get-role-holders"}, e.Key))
	if err != nil {
		return "", Unset, err
	}
	return parseRole(out), Explicit, nil
}

// parseRole joins the holders `cmd role` prints on one line separated by `;`.
// An unheld role prints a single empty line, which is an explicit empty value.
func parseRole(out string) string {
	var holders []string
	for _, holder := range strings.Split(out, ";") {
		if holder = strings.TrimSpace(holder); holder != "" {
			holders = append(holders, holder)
		}
	}
	return strings.Join(holders, ",")
}

func (s roleStore) Write(d *debloat.Device, e Entry) (bool, string) {
	command, err := s.writeCommand(e)
	if err != nil {
		return false, err.Error()
	}
	return write(d, command)
}

func (roleStore) writeCommand(e Entry) (string, error) {
	if !roleRE.MatchString(e.Key) {
		return "", fmt.Errorf("invalid role %q", e.Key)
	}
	holder, err := roleHolder(e.Value)
	if err != nil {
		return "", err
	}
	if holder == "" {
		return render([]string{"cmd", "role", "clear-role-holders"}, e.Key), nil
	}
	return render([]string{"cmd", "role", "add-role-holder"}, e.Key, holder), nil
}

// roleHolder validates a role's value: one package, or empty for no holder. The
// roles used here hold a single package; adding a second makes the platform
// evict the first, so a list would silently keep only its last name.
func roleHolder(value string) (string, error) {
	value = strings.TrimSpace(value)
	if value == "" {
		return "", nil
	}
	if strings.Contains(value, ",") {
		return "", fmt.Errorf("role value %q names more than one package; a role holds one holder and the platform evicts the previous one", value)
	}
	if !debloat.ValidPackage(value) {
		return "", fmt.Errorf("not a package name: %q", value)
	}
	return value, nil
}

func (s roleStore) Reset(d *debloat.Device, e Entry) (bool, string) {
	command, err := s.resetCommand(e.Key)
	if err != nil {
		return false, err.Error()
	}
	return write(d, command)
}

func (roleStore) resetCommand(key string) (string, error) {
	if !roleRE.MatchString(key) {
		return "", fmt.Errorf("invalid role %q", key)
	}
	return render([]string{"cmd", "role", "clear-role-holders"}, key), nil
}

// deviceIdleStore covers the doze whitelist. A user row means somebody put the
// package on the list and it can be removed; a system-excidle row is the
// platform's own and `cmd deviceidle whitelist -<pkg>` cannot remove it.
type deviceIdleStore struct{}

func (deviceIdleStore) Read(d *debloat.Device, e Entry) (string, State, error) {
	pkg, err := packageKey(e.Key)
	if err != nil {
		return "", Unset, err
	}
	out, err := d.Read(render([]string{"cmd", "deviceidle", "whitelist"}))
	if err != nil {
		return "", Unset, err
	}
	value, state := parseDeviceIdle(out, pkg)
	return value, state, nil
}

func parseDeviceIdle(out, pkg string) (string, State) {
	excidle := false
	for _, line := range strings.Split(out, "\n") {
		fields := strings.Split(strings.TrimSpace(line), ",")
		if len(fields) < 2 || fields[1] != pkg {
			continue
		}
		switch fields[0] {
		case "user":
			return "allow", Explicit
		case "system-excidle":
			excidle = true
		}
	}
	if excidle {
		return "allow", Implicit
	}
	return "ignore", Explicit
}

func (s deviceIdleStore) Write(d *debloat.Device, e Entry) (bool, string) {
	command, err := s.writeCommand(e)
	if err != nil {
		return false, err.Error()
	}
	return write(d, command)
}

func (deviceIdleStore) writeCommand(e Entry) (string, error) {
	if _, err := packageKey(e.Key); err != nil {
		return "", err
	}
	var sign string
	switch e.Value {
	case "allow":
		sign = "+"
	case "ignore":
		sign = "-"
	default:
		return "", fmt.Errorf("deviceidle value must be allow or ignore, got %q", e.Value)
	}
	return render([]string{"cmd", "deviceidle", "whitelist"}, sign+e.Key), nil
}

func (s deviceIdleStore) Reset(d *debloat.Device, e Entry) (bool, string) {
	command, err := s.resetCommand(e.Key)
	if err != nil {
		return false, err.Error()
	}
	return write(d, command)
}

func (deviceIdleStore) resetCommand(key string) (string, error) {
	if _, err := packageKey(key); err != nil {
		return "", err
	}
	return render([]string{"cmd", "deviceidle", "whitelist"}, "-"+key), nil
}

// standbyStore covers app standby buckets. The bucket always has a value, so
// there is nothing to reset; revert writes back the bucket that was read.
type standbyStore struct{}

var standbyBuckets = []struct {
	number, name string
	state        State
}{
	{"5", "exempted", Implicit},
	{"10", "active", Explicit},
	{"20", "working_set", Explicit},
	{"30", "frequent", Explicit},
	{"40", "rare", Explicit},
	{"45", "restricted", Explicit},
	{"50", "never", Explicit},
}

func (standbyStore) Read(d *debloat.Device, e Entry) (string, State, error) {
	pkg, err := packageKey(e.Key)
	if err != nil {
		return "", Unset, err
	}
	out, err := d.Read(render([]string{"am", "get-standby-bucket"}, pkg))
	if err != nil {
		return "", Unset, err
	}
	return parseStandby(out)
}

func parseStandby(out string) (string, State, error) {
	number := strings.TrimSpace(out)
	for _, bucket := range standbyBuckets {
		if bucket.number == number {
			return bucket.name, bucket.state, nil
		}
	}
	return "", Unset, fmt.Errorf("unexpected standby bucket %q", number)
}

func (s standbyStore) Write(d *debloat.Device, e Entry) (bool, string) {
	command, err := s.writeCommand(e)
	if err != nil {
		return false, err.Error()
	}
	return write(d, command)
}

func (standbyStore) writeCommand(e Entry) (string, error) {
	pkg, err := packageKey(e.Key)
	if err != nil {
		return "", err
	}
	if e.Value == "exempted" {
		return "", fmt.Errorf("standby bucket exempted is the platform's floor for a doze-whitelisted app; use active, working_set, frequent, rare, restricted or never")
	}
	for _, bucket := range standbyBuckets {
		if bucket.name == e.Value {
			return render([]string{"am", "set-standby-bucket"}, pkg, bucket.name), nil
		}
	}
	return "", fmt.Errorf("invalid standby bucket %q", e.Value)
}

func (standbyStore) Reset(*debloat.Device, Entry) (bool, string) {
	return false, "a standby bucket cannot be unset"
}

// localeStore covers per-app locales. An empty value clears the override, which
// takes the app back to the system language.
type localeStore struct{}

func (localeStore) Read(d *debloat.Device, e Entry) (string, State, error) {
	pkg, err := packageKey(e.Key)
	if err != nil {
		return "", Unset, err
	}
	out, err := d.Read(render([]string{"cmd", "locale", "get-app-locales"}, pkg))
	if err != nil {
		return "", Unset, err
	}
	value, err := parseLocales(out)
	if err != nil {
		return "", Unset, err
	}
	return value, Explicit, nil
}

// parseLocales refuses `Unknown package ...`, which the command prints to stdout
// and exits 0 for, rather than turning it into an empty locale list.
func parseLocales(out string) (string, error) {
	body := strings.TrimSpace(out)
	if strings.HasPrefix(body, "Unknown package ") {
		return "", fmt.Errorf("%s", body)
	}
	start := strings.IndexByte(body, '[')
	end := strings.LastIndexByte(body, ']')
	if start < 0 || end <= start {
		return "", fmt.Errorf("unexpected locale output %q", body)
	}
	return strings.TrimSpace(body[start+1 : end]), nil
}

func (s localeStore) Write(d *debloat.Device, e Entry) (bool, string) {
	command, err := s.writeCommand(e)
	if err != nil {
		return false, err.Error()
	}
	return write(d, command)
}

func (localeStore) writeCommand(e Entry) (string, error) {
	if _, err := packageKey(e.Key); err != nil {
		return "", err
	}
	if e.Value != "" {
		for _, locale := range strings.Split(e.Value, ",") {
			if !localeRE.MatchString(locale) {
				return "", fmt.Errorf("invalid locale %q", locale)
			}
		}
	}
	return render([]string{"cmd", "locale", "set-app-locales"}, e.Key, "--locales", e.Value), nil
}

func (s localeStore) Reset(d *debloat.Device, e Entry) (bool, string) {
	command, err := s.resetCommand(e.Key)
	if err != nil {
		return false, err.Error()
	}
	return write(d, command)
}

func (localeStore) resetCommand(key string) (string, error) {
	if _, err := packageKey(key); err != nil {
		return "", err
	}
	return render([]string{"cmd", "locale", "set-app-locales"}, key, "--locales", ""), nil
}

// appLinksStore covers whether an app may open its verified links by default.
// The platform prints `Verification link handling allowed:` for every app that
// declares a web link; an app that declares none has no line at all, so there is
// nothing to compare and the entry reads Unset.
type appLinksStore struct{}

func (appLinksStore) Read(d *debloat.Device, e Entry) (string, State, error) {
	pkg, err := packageKey(e.Key)
	if err != nil {
		return "", Unset, err
	}
	out, err := d.Read(render([]string{"pm", "get-app-links", "--user", "0"}, pkg))
	if err != nil {
		return "", Unset, err
	}
	value, state := parseAppLinks(out)
	return value, state, nil
}

var linkHandlingRE = regexp.MustCompile(`Verification link handling allowed: (true|false)`)

func parseAppLinks(out string) (string, State) {
	match := linkHandlingRE.FindStringSubmatch(out)
	if match == nil {
		return "", Unset
	}
	if match[1] == "true" {
		return "allowed", Explicit
	}
	return "disallowed", Explicit
}

func (s appLinksStore) Write(d *debloat.Device, e Entry) (bool, string) {
	command, err := s.writeCommand(e)
	if err != nil {
		return false, err.Error()
	}
	return write(d, command)
}

func (appLinksStore) writeCommand(e Entry) (string, error) {
	if _, err := packageKey(e.Key); err != nil {
		return "", err
	}
	var allowed string
	switch e.Value {
	case "allowed":
		allowed = "true"
	case "disallowed":
		allowed = "false"
	default:
		return "", fmt.Errorf("applinks value must be allowed or disallowed, got %q", e.Value)
	}
	return render([]string{"pm", "set-app-links-allowed", "--user", "0", "--package"}, e.Key, allowed), nil
}

func (appLinksStore) Reset(*debloat.Device, Entry) (bool, string) {
	return false, "link handling has no unset state"
}

// appLinksDomainStore covers one domain's verification state under an app. The
// `Domain verification state:` block seeds every declared domain, so a domain
// missing from it is one the app does not declare: a typo, not a "none" state.
type appLinksDomainStore struct{}

func (appLinksDomainStore) Read(d *debloat.Device, e Entry) (string, State, error) {
	pkg, domain, err := splitDomainKey(e.Key)
	if err != nil {
		return "", Unset, err
	}
	out, err := d.Read(render([]string{"pm", "get-app-links", "--user", "0"}, pkg))
	if err != nil {
		return "", Unset, err
	}
	value, state := parseDomainState(out, domain)
	return value, state, nil
}

// writableDomainStates are the tokens `pm set-app-links` accepts, plus the bare
// state codes the device prints for them.
var writableDomainStates = map[string]string{
	"0": "0", "none": "0",
	"1": "1", "verified": "1",
	"2": "2", "approved": "2",
	"3": "3", "denied": "3",
}

func parseDomainState(out, domain string) (string, State) {
	header := -1
	for _, line := range strings.Split(out, "\n") {
		trimmed := strings.TrimSpace(line)
		indent := len(line) - len(strings.TrimLeft(line, " \t"))
		if header < 0 {
			if strings.HasPrefix(trimmed, "Domain verification state:") {
				header = indent
			}
			continue
		}
		if trimmed == "" {
			continue
		}
		if indent <= header {
			break
		}
		name, token, ok := strings.Cut(trimmed, ":")
		if !ok {
			continue
		}
		if strings.TrimSpace(name) != domain {
			continue
		}
		token = strings.TrimSpace(token)
		if _, writable := writableDomainStates[token]; writable {
			return token, Explicit
		}
		return token, Implicit
	}
	return "", Unset
}

func (s appLinksDomainStore) Write(d *debloat.Device, e Entry) (bool, string) {
	command, err := s.writeCommand(e)
	if err != nil {
		return false, err.Error()
	}
	return write(d, command)
}

func (appLinksDomainStore) writeCommand(e Entry) (string, error) {
	pkg, domain, err := splitDomainKey(e.Key)
	if err != nil {
		return "", err
	}
	state, ok := writableDomainStates[e.Value]
	if !ok {
		return "", fmt.Errorf("applinks_domain value %q is not writable; use none (0), verified (1), approved (2) or denied (3)", e.Value)
	}
	return render([]string{"pm", "set-app-links", "--package"}, pkg, state, domain), nil
}

func (s appLinksDomainStore) Reset(d *debloat.Device, e Entry) (bool, string) {
	command, err := s.resetCommand(e.Key)
	if err != nil {
		return false, err.Error()
	}
	return write(d, command)
}

func (appLinksDomainStore) resetCommand(key string) (string, error) {
	pkg, domain, err := splitDomainKey(key)
	if err != nil {
		return "", err
	}
	return render([]string{"pm", "set-app-links", "--package"}, pkg, "0", domain), nil
}

// appLinksSelectionStore covers the user's per-domain choice between the app's
// verified links and the browser.
type appLinksSelectionStore struct{}

func (appLinksSelectionStore) Read(d *debloat.Device, e Entry) (string, State, error) {
	pkg, domain, err := splitDomainKey(e.Key)
	if err != nil {
		return "", Unset, err
	}
	out, err := d.Read(render([]string{"pm", "get-app-links", "--user", "0"}, pkg))
	if err != nil {
		return "", Unset, err
	}
	value, state := parseSelectionState(out, domain)
	return value, state, nil
}

func parseSelectionState(out, domain string) (string, State) {
	header := -1
	selection := ""
	for _, line := range strings.Split(out, "\n") {
		trimmed := strings.TrimSpace(line)
		indent := len(line) - len(strings.TrimLeft(line, " \t"))
		if header < 0 {
			if strings.HasPrefix(trimmed, "Selection state:") {
				header = indent
			}
			continue
		}
		if trimmed == "" {
			continue
		}
		if indent <= header {
			break
		}
		switch trimmed {
		case "Enabled:":
			selection = "enabled"
			continue
		case "Disabled:":
			selection = "disabled"
			continue
		}
		if strings.HasSuffix(trimmed, ":") {
			break
		}
		if selection != "" && trimmed == domain {
			return selection, Explicit
		}
	}
	return "", Unset
}

func (s appLinksSelectionStore) Write(d *debloat.Device, e Entry) (bool, string) {
	command, err := s.writeCommand(e)
	if err != nil {
		return false, err.Error()
	}
	return write(d, command)
}

func (appLinksSelectionStore) writeCommand(e Entry) (string, error) {
	pkg, domain, err := splitDomainKey(e.Key)
	if err != nil {
		return "", err
	}
	var selected string
	switch e.Value {
	case "enabled":
		selected = "true"
	case "disabled":
		selected = "false"
	default:
		return "", fmt.Errorf("applinks_selection value must be enabled or disabled, got %q", e.Value)
	}
	return render([]string{"pm", "set-app-links-user-selection", "--user", "0", "--package"}, pkg, selected, domain), nil
}

func (appLinksSelectionStore) Reset(*debloat.Device, Entry) (bool, string) {
	return false, "the platform shows every declared domain as disabled until it is chosen, so there is no unset state to restore"
}
