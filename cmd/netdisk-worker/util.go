package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

func cleanPath(value string) string {
	parts := strings.Split(strings.ReplaceAll(strings.TrimSpace(value), "\\", "/"), "/")
	out := make([]string, 0, len(parts))
	invalid := regexp.MustCompile(`[\\/:*?"<>|\x00-\x1f]+`)
	for _, item := range parts {
		item = strings.TrimSpace(invalid.ReplaceAllString(item, " "))
		item = strings.Trim(item, " .")
		if item != "" {
			out = append(out, item)
		}
	}
	if len(out) == 0 {
		return "/"
	}
	return "/" + strings.Join(out, "/")
}

func shareID(raw string) string {
	re := regexp.MustCompile(`/s/([^/?#]+)`)
	m := re.FindStringSubmatch(raw)
	if len(m) > 1 {
		return m[1]
	}
	return ""
}

func jsonPath(m map[string]any, keys ...string) any {
	var cur any = m
	for _, key := range keys {
		obj, ok := cur.(map[string]any)
		if !ok {
			return nil
		}
		cur = obj[key]
	}
	return cur
}

func str(v any) string {
	switch x := v.(type) {
	case string:
		return x
	case json.Number:
		return x.String()
	case float64:
		return strconv.FormatFloat(x, 'f', -1, 64)
	case int:
		return strconv.Itoa(x)
	case int64:
		return strconv.FormatInt(x, 10)
	default:
		return ""
	}
}

func i64(v any) int64 {
	switch x := v.(type) {
	case bool:
		if x {
			return 1
		}
		return 0
	case float64:
		return int64(x)
	case json.Number:
		n, _ := x.Int64()
		return n
	case string:
		if strings.EqualFold(strings.TrimSpace(x), "true") {
			return 1
		}
		n, _ := strconv.ParseInt(x, 10, 64)
		return n
	default:
		return 0
	}
}

func intv(v any) int {
	return int(i64(v))
}

func boolv(v any) bool {
	if x, ok := v.(bool); ok {
		return x
	}
	return i64(v) != 0
}

func mapv(v any) map[string]any {
	if x, ok := v.(map[string]any); ok {
		return x
	}
	return nil
}

func listv(v any) []any {
	if x, ok := v.([]any); ok {
		return x
	}
	return nil
}

func doJSON(client *http.Client, method, rawURL string, headers map[string]string, body any) (map[string]any, error) {
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequest(method, rawURL, reader)
	if err != nil {
		return nil, err
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	if body != nil && req.Header.Get("Content-Type") == "" {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, string(data))
	}
	var result map[string]any
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	if err := dec.Decode(&result); err != nil {
		return nil, fmt.Errorf("JSON解析失败: %w", err)
	}
	return result, nil
}

func fingerprint(files []FileEntry) string {
	copyFiles := append([]FileEntry(nil), files...)
	sort.Slice(copyFiles, func(i, j int) bool { return copyFiles[i].Path < copyFiles[j].Path })
	data, _ := json.Marshal(copyFiles)
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func commonHeaders(cookie, referer string) map[string]string {
	return map[string]string{
		"Accept":       "application/json, text/plain, */*",
		"Content-Type": "application/json;charset=UTF-8",
		"Cookie":       cookie,
		"Referer":      referer,
		"User-Agent":   "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/147 Safari/537.36",
	}
}
