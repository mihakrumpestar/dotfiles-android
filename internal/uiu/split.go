package uiu

import (
	"encoding/json"
	"fmt"
	"strings"
)

// selection is one entry of a recipe's `private` list.
//
// Two forms are accepted. A dotted path such as `settings.github-creds` names a
// value, which is lifted out and replaced by the zero of its type. A path with an
// identity such as `apps[id=com.example.bank]` names one element of a list,
// which is moved into the overlay whole, so the artifact keeps every other
// element and a single entry can be kept out of the repository.
type selection struct {
	path  []string
	key   string
	value string
	keyed bool
}

// parseSelection reads a `private` entry.
func parseSelection(field string) (selection, error) {
	open := strings.LastIndex(field, "[")
	if open < 0 {
		return selection{path: strings.Split(field, ".")}, nil
	}
	if !strings.HasSuffix(field, "]") {
		return selection{}, fmt.Errorf("%q: an identity selector ends with ]", field)
	}
	body := field[open+1 : len(field)-1]
	equals := strings.Index(body, "=")
	if equals <= 0 || equals == len(body)-1 {
		return selection{}, fmt.Errorf("%q: an identity selector is written list[key=value]", field)
	}
	return selection{
		path:  strings.Split(field[:open], "."),
		key:   body[:equals],
		value: body[equals+1:],
		keyed: true,
	}, nil
}

// String renders the selection for error messages.
func (s selection) String() string {
	if s.keyed {
		return strings.Join(s.path, ".") + "[" + s.key + "=" + s.value + "]"
	}
	return strings.Join(s.path, ".")
}

// splitExport divides one export into the bytes that may be committed and the
// values that may not.
//
// A plain path is personal in a way that outlives the export: a home coordinates
// pair, a token. Zeroing it, which is what `redact` does, would lose it for good
// and leave a restore writing blanks to the phone, so it is lifted into an
// overlay and the public body carries the zero of that type. An identity selector
// moves one list element instead, leaving the rest of the list published, and a
// selector that matches nothing is an error: it would otherwise publish the very
// entry it was written to hide.
//
// The public body is exactly what Redact would have produced for a plain path, so
// moving a path from redact to private keeps the same committed bytes.
func splitExport(body []byte, fields []string) (public, private []byte, err error) {
	var value any
	if err := json.Unmarshal(body, &value); err != nil {
		return body, nil, nil
	}
	overlay := map[string]any{}
	for _, field := range fields {
		chosen, err := parseSelection(field)
		if err != nil {
			return nil, nil, err
		}
		if chosen.keyed {
			if err := liftEntry(value, overlay, chosen); err != nil {
				return nil, nil, err
			}
			continue
		}
		taken, present := valueAt(value, chosen.path)
		if !present {
			continue
		}
		// A value that is already the zero of its type carries nothing worth
		// keeping, and writing it down would claim the overlay holds a secret it
		// does not have.
		if !isZero(taken) {
			putAt(overlay, chosen.path, taken)
		}
		zero(value, chosen.path)
	}
	out, err := marshalCanonical(value)
	if err != nil {
		return body, nil, nil
	}
	// The overlay is written even when it is empty: its absence has to mean the
	// private half was lost, not that this app happened to have no values.
	private, err = marshalCanonical(overlay)
	if err != nil {
		return out, nil, nil
	}
	return out, private, nil
}

// liftEntry moves the elements an identity selector names out of the list and
// into the overlay, keeping their order.
func liftEntry(value any, overlay map[string]any, chosen selection) error {
	list, present := valueAt(value, chosen.path)
	if !present {
		return fmt.Errorf("%s matches nothing: the export has no %s", chosen, strings.Join(chosen.path, "."))
	}
	items, ok := list.([]any)
	if !ok {
		return fmt.Errorf("%s names a %s, not a list", chosen, strings.Join(chosen.path, "."))
	}
	var kept, lifted []any
	for _, item := range items {
		if identity(item, chosen.key) == chosen.value {
			lifted = append(lifted, item)
			continue
		}
		kept = append(kept, item)
	}
	if len(lifted) == 0 {
		return fmt.Errorf("%s matches nothing in %d element(s)", chosen, len(items))
	}
	putAt(value, chosen.path, kept)
	for _, item := range lifted {
		existing, _ := valueAt(overlay, chosen.path)
		overlayItems, _ := existing.([]any)
		putAt(overlay, chosen.path, append(overlayItems, item))
	}
	return nil
}

// mergeExport puts an overlay's values back into a public body, so a restore
// pushes the whole export instead of the published half. A plain path the overlay
// does not carry keeps the public value, which lets an overlay lag the artifact
// without wiping a field. An identity selector is different: if the entry is in
// neither the overlay nor the artifact, the restore would silently drop an app,
// so it fails instead.
func mergeExport(public, private []byte, fields []string) ([]byte, error) {
	var value any
	if err := json.Unmarshal(public, &value); err != nil {
		return public, nil
	}
	var overlay any
	if err := json.Unmarshal(private, &overlay); err != nil {
		return public, nil
	}
	for _, field := range fields {
		chosen, err := parseSelection(field)
		if err != nil {
			return nil, err
		}
		if chosen.keyed {
			if err := restoreEntry(value, overlay, chosen); err != nil {
				return nil, err
			}
			continue
		}
		taken, present := valueAt(overlay, chosen.path)
		if !present {
			continue
		}
		putAt(value, chosen.path, taken)
	}
	out, err := marshalCanonical(value)
	if err != nil {
		return public, nil
	}
	return out, nil
}

// restoreEntry puts one lifted element back, replacing an element with the same
// identity if the artifact already carries one.
func restoreEntry(value, overlay any, chosen selection) error {
	taken, present := valueAt(overlay, chosen.path)
	if !present {
		return fmt.Errorf("%s needs the private overlay, and it holds no such entry", chosen)
	}
	lifted, ok := taken.([]any)
	if !ok {
		return fmt.Errorf("%s: the overlay's %s is not a list", chosen, strings.Join(chosen.path, "."))
	}
	var matching []any
	for _, item := range lifted {
		if identity(item, chosen.key) == chosen.value {
			matching = append(matching, item)
		}
	}
	if len(matching) == 0 {
		return fmt.Errorf("%s needs the private overlay, and it holds no such entry", chosen)
	}

	current, _ := valueAt(value, chosen.path)
	items, _ := current.([]any)
	for _, item := range matching {
		replaced := false
		for i, existing := range items {
			if identity(existing, chosen.key) == chosen.value {
				items[i] = item
				replaced = true
				break
			}
		}
		if !replaced {
			items = append(items, item)
		}
	}
	putAt(value, chosen.path, items)
	return nil
}

// identity reads the field an identity selector matches on. A missing or
// non-string field cannot match, which keeps a malformed entry from being lifted.
func identity(item any, key string) string {
	object, ok := item.(map[string]any)
	if !ok {
		return ""
	}
	value, ok := object[key].(string)
	if !ok {
		return ""
	}
	return value
}

// marshalCanonical encodes a value the way every stored artifact is written, so a
// split or merged file differs from the original only in its values.
func marshalCanonical(value any) ([]byte, error) {
	pretty, err := json.MarshalIndent(value, "", " ")
	if err != nil {
		return nil, err
	}
	return append(pretty, '\n'), nil
}

// valueAt reads the value at path without creating anything. A list is walked in
// place, so a path reaches every element and the first match wins.
func valueAt(value any, path []string) (any, bool) {
	if len(path) == 0 {
		return nil, false
	}
	if list, ok := value.([]any); ok {
		for _, item := range list {
			if found, present := valueAt(item, path); present {
				return found, true
			}
		}
		return nil, false
	}
	object, ok := value.(map[string]any)
	if !ok {
		return nil, false
	}
	if len(path) == 1 {
		found, present := object[path[0]]
		return found, present
	}
	return valueAt(object[path[0]], path[1:])
}

// putAt writes a value at path, creating the objects on the way. It is the mirror
// of valueAt: the overlay holds exactly the shape the public file holds, minus
// everything that is not private.
func putAt(value any, path []string, taken any) {
	if len(path) == 0 {
		return
	}
	object, ok := value.(map[string]any)
	if !ok {
		return
	}
	if len(path) == 1 {
		object[path[0]] = taken
		return
	}
	next, ok := object[path[0]].(map[string]any)
	if !ok {
		next = map[string]any{}
		object[path[0]] = next
	}
	putAt(next, path[1:], taken)
}

// isZero reports whether a value carries nothing: an empty string, false, zero,
// or an empty container.
func isZero(value any) bool {
	switch taken := value.(type) {
	case nil:
		return true
	case string:
		return taken == ""
	case bool:
		return !taken
	case float64:
		return taken == 0
	case []any:
		return len(taken) == 0
	case map[string]any:
		return len(taken) == 0
	}
	return false
}
