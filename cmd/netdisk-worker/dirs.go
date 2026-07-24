package main

import (
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"

	netdisk "github.com/wgx0307/netdisk"
)

func cloudItemIsDir(item map[string]any) bool {
	return intv(item["type"]) == 1 || boolv(item["dir"]) || boolv(item["is_dir"]) || str(item["kind"]) == "drive#folder"
}

func cloudCreatedFID(resp map[string]any) string {
	paths := [][]string{
		{"data", "fid"},
		{"data", "file", "fid"},
		{"data", "file_info", "fid"},
		{"file", "fid"},
		{"fid"},
	}
	for _, path := range paths {
		if fid := str(jsonPath(resp, path...)); fid != "" {
			return fid
		}
	}
	return ""
}

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
	if status := intv(resp["status"]); status != 0 && status != 200 {
		return nil, fmt.Errorf("读取目标目录失败: %s", str(resp["message"]))
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

func findCloudChild(client *http.Client, base, pr, cookie, parent, name string) (string, error) {
	items, err := listCloudDir(client, base, pr, cookie, parent)
	if err != nil {
		return "", err
	}
	for _, item := range items {
		if str(item["file_name"]) == name && cloudItemIsDir(item) {
			return str(item["fid"]), nil
		}
	}
	return "", nil
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

	// 夸克/UC 支持一次传入完整 dir_path。优先使用响应中的目录 ID，
	// 避免刚创建目录后文件列表接口尚未刷新导致“创建后未找到”。
	created, createErr := doJSON(client, http.MethodPost, base+"/1/clouddrive/file?"+q.Encode(), headers, map[string]any{"pdir_fid": "0", "file_name": "", "dir_path": target, "dir_init_lock": false})
	if createErr == nil {
		if fid := cloudCreatedFID(created); fid != "" {
			return fid, nil
		}
	}

	parent := "0"
	for _, seg := range strings.Split(strings.Trim(target, "/"), "/") {
		found, err := findCloudChild(client, base, pr, req.Credentials["cookie"], parent, seg)
		if err != nil {
			return "", err
		}
		if found == "" {
			resp, err := doJSON(client, http.MethodPost, base+"/1/clouddrive/file?"+q.Encode(), headers, map[string]any{"pdir_fid": parent, "file_name": seg, "dir_path": "", "dir_init_lock": false})
			if err == nil {
				found = cloudCreatedFID(resp)
			}
		}
		if found == "" {
			// 网盘目录列表存在短暂最终一致性，最多等待约 8 秒。
			for retry := 0; retry < 10 && found == ""; retry++ {
				time.Sleep(time.Duration(400+retry*100) * time.Millisecond)
				found, err = findCloudChild(client, base, pr, req.Credentials["cookie"], parent, seg)
				if err != nil {
					return "", err
				}
			}
		}
		if found == "" {
			if createErr != nil {
				return "", fmt.Errorf("目标目录创建失败: %s（%v）", target, createErr)
			}
			return "", fmt.Errorf("目标目录创建后未找到: %s/%s", strings.TrimRight(target, "/"), seg)
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
