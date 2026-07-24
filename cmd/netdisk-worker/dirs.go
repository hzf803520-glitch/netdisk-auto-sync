package main

import (
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"

	netdisk "github.com/wgx0307/netdisk"
)

func listCloudDir(client *http.Client, base, pr, cookie, parent string) ([]map[string]any, error) {
	headers := commonHeaders(cookie, "https://pan.quark.cn/")
	if pr == "UCBrowser" {
		headers["Referer"] = "https://drive.uc.cn/"
	}
	q := url.Values{"pr": {pr}, "fr": {"pc"}, "pdir_fid": {parent}, "_page": {"1"}, "_size": {"200"}, "_fetch_total": {"1"}, "_fetch_sub_dirs": {"0"}, "_sort": {"file_type:asc,file_name:asc"}}
	resp, err := doJSON(client, http.MethodGet, base+"/1/clouddrive/file/sort?"+q.Encode(), headers, nil)
	if err != nil {
		return nil, err
	}
	data := mapv(resp["data"])
	out := make([]map[string]any, 0)
	for _, raw := range listv(data["list"]) {
		if m := mapv(raw); m != nil {
			out = append(out, m)
		}
	}
	return out, nil
}

func resolveQuarkDir(req Request, uc bool, target string) (string, error) {
	target = cleanPath(target)
	if target == "/" {
		return "0", nil
	}
	base, pr, referer := "https://drive-pc.quark.cn", "ucpro", "https://pan.quark.cn/"
	if uc {
		base, pr, referer = "https://pc-api.uc.cn", "UCBrowser", "https://drive.uc.cn/"
	}
	client := &http.Client{Timeout: 35 * time.Second}
	headers := commonHeaders(req.Credentials["cookie"], referer)
	q := url.Values{"pr": {pr}, "fr": {"pc"}}
	_, _ = doJSON(client, http.MethodPost, base+"/1/clouddrive/file?"+q.Encode(), headers, map[string]any{"pdir_fid": "0", "file_name": "", "dir_path": target, "dir_init_lock": false})
	parent := "0"
	for _, seg := range strings.Split(strings.Trim(target, "/"), "/") {
		items, err := listCloudDir(client, base, pr, req.Credentials["cookie"], parent)
		if err != nil {
			return "", err
		}
		found := ""
		for _, item := range items {
			if str(item["file_name"]) == seg && (intv(item["type"]) == 1 || intv(item["dir"]) == 1) {
				found = str(item["fid"])
				break
			}
		}
		if found == "" {
			return "", fmt.Errorf("目标目录创建后未找到: %s", target)
		}
		parent = found
	}
	return parent, nil
}

func resolveXunleiDir(req Request, target string) (string, error) {
	target = cleanPath(target)
	if target == "/" {
		return "", nil
	}
	access, captcha, err := xunleiTokens(req, "get:/drive/v1/share")
	if err != nil {
		return "", err
	}
	client := &http.Client{Timeout: 35 * time.Second}
	headers := map[string]string{"Content-Type": "application/json", "Origin": "https://pan.xunlei.com", "Referer": "https://pan.xunlei.com/", "User-Agent": "Mozilla/5.0 Chrome/139 Safari/537.36", "Authorization": "Bearer " + access, "x-captcha-token": captcha, "x-client-id": "Xqp0kJBXWhwaTpB6", "x-device-id": "925b7631473a13716b791d7f28289cad"}
	parent := ""
	for _, seg := range strings.Split(strings.Trim(target, "/"), "/") {
		q := url.Values{"parent_id": {parent}, "limit": {"1000"}, "with_audit": {"true"}, "filters": {`{"phase":{"eq":"PHASE_TYPE_COMPLETE"},"trashed":{"eq":false}}`}}
		resp, err := doJSON(client, http.MethodGet, "https://api-pan.xunlei.com/drive/v1/files?"+q.Encode(), headers, nil)
		if err != nil {
			return "", err
		}
		items := listv(resp["files"])
		if data := mapv(resp["data"]); data != nil && len(items) == 0 {
			items = listv(data["files"])
		}
		found := ""
		for _, raw := range items {
			m := mapv(raw)
			if str(m["name"]) == seg && str(m["kind"]) == "drive#folder" {
				found = str(m["id"])
				break
			}
		}
		if found == "" {
			created, err := doJSON(client, http.MethodPost, "https://api-pan.xunlei.com/drive/v1/files", headers, map[string]any{"kind": "drive#folder", "name": seg, "parent_id": parent, "space": ""})
			if err != nil {
				return "", err
			}
			found = str(jsonPath(created, "file", "id"))
			if found == "" {
				found = str(jsonPath(created, "data", "file", "id"))
			}
			if found == "" {
				return "", fmt.Errorf("迅雷目标目录创建失败: %s", seg)
			}
		}
		parent = found
	}
	return parent, nil
}

func configure(req Request) (string, error) {
	creds := req.Credentials
	cfg := &netdisk.Config{Debug: false}
	saveDir := strings.TrimSpace(req.SaveDir)
	resolved := saveDir
	switch netdisk.DetectPanType(req.URL) {
	case netdisk.PanBaidu:
		if saveDir == "" {
			saveDir = "/资源"
		}
		saveDir = strings.Trim(cleanPath(saveDir), "/")
		cfg.BaiduCookie = creds["cookie"]
		cfg.BaiduSaveDir = saveDir
		resolved = "/" + saveDir
	case netdisk.PanQuark:
		if saveDir == "" {
			saveDir = "/资源"
		}
		fid, err := resolveQuarkDir(req, false, saveDir)
		if err != nil {
			return "", err
		}
		cfg.QuarkCookie = creds["cookie"]
		cfg.QuarkSaveDir = fid
		resolved = cleanPath(saveDir)
	case netdisk.PanUC:
		if saveDir == "" {
			saveDir = "/资源"
		}
		fid, err := resolveQuarkDir(req, true, saveDir)
		if err != nil {
			return "", err
		}
		cfg.UCCookie = creds["cookie"]
		cfg.UCSaveDir = fid
		resolved = cleanPath(saveDir)
	case netdisk.PanXunlei:
		if saveDir == "" {
			saveDir = "/资源"
		}
		fid, err := resolveXunleiDir(req, saveDir)
		if err != nil {
			return "", err
		}
		cfg.XunleiRefreshToken = creds["refresh_token"]
		cfg.XunleiAccessToken = creds["access_token"]
		if cfg.XunleiRefreshToken == "" && cfg.XunleiAccessToken != "" {
			cfg.XunleiRefreshToken = "access-token-present"
		}
		cfg.XunleiSaveDir = fid
		resolved = cleanPath(saveDir)
	default:
		return "", fmt.Errorf("无法识别网盘平台")
	}
	netdisk.SetConfig(cfg)
	return resolved, nil
}
