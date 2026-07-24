package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"

	netdisk "github.com/wgx0307/netdisk"
)

func baiduDecode(s string) string {
	if v, err := strconv.Unquote(`"` + s + `"`); err == nil {
		return v
	}
	return s
}

func inspectBaidu(req Request) (string, []FileEntry, error) {
	jar, _ := cookiejar.New(nil)
	client := &http.Client{Timeout: 45 * time.Second, Jar: jar}
	u, _ := url.Parse("https://pan.baidu.com")
	cookies := make([]*http.Cookie, 0)
	for _, item := range strings.Split(req.Credentials["cookie"], ";") {
		parts := strings.SplitN(strings.TrimSpace(item), "=", 2)
		if len(parts) == 2 {
			cookies = append(cookies, &http.Cookie{Name: parts[0], Value: parts[1], Path: "/"})
		}
	}
	jar.SetCookies(u, cookies)
	headers := map[string]string{"Referer": "https://pan.baidu.com/", "User-Agent": "Mozilla/5.0 Chrome/147 Safari/537.36"}
	tpl, err := doJSON(client, http.MethodGet, "https://pan.baidu.com/api/gettemplatevariable?clienttype=0&app_id=38824127&web=1&fields=%5B%22bdstoken%22%5D", headers, nil)
	if err != nil {
		return "", nil, err
	}
	bdstoken := str(jsonPath(tpl, "result", "bdstoken"))
	id := shareID(req.URL)
	if id == "" {
		return "", nil, fmt.Errorf("分享链接解析失败")
	}
	if req.Code != "" {
		surl := strings.TrimPrefix(id, "1")
		form := url.Values{"pwd": {req.Code}, "vcode": {""}, "vcode_str": {""}}
		verifyURL := "https://pan.baidu.com/share/verify?surl=" + url.QueryEscape(surl) + "&bdstoken=" + url.QueryEscape(bdstoken) + "&web=1&clienttype=0"
		hreq, _ := http.NewRequest(http.MethodPost, verifyURL, strings.NewReader(form.Encode()))
		hreq.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		for k, v := range headers {
			hreq.Header.Set(k, v)
		}
		resp, err := client.Do(hreq)
		if err != nil {
			return "", nil, err
		}
		data, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		var vr map[string]any
		_ = json.Unmarshal(data, &vr)
		if intv(vr["errno"]) != 0 {
			return "", nil, fmt.Errorf("百度提取码验证失败")
		}
	}
	pageURL := req.URL
	hreq, _ := http.NewRequest(http.MethodGet, pageURL, nil)
	for k, v := range headers {
		hreq.Header.Set(k, v)
	}
	resp, err := client.Do(hreq)
	if err != nil {
		return "", nil, err
	}
	defer resp.Body.Close()
	html, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", nil, err
	}
	text := string(html)
	nameRE := regexp.MustCompile(`"server_filename":"((?:\\.|[^"\\])*)"`)
	idRE := regexp.MustCompile(`"fs_id":(\d+)`)
	names := nameRE.FindAllStringSubmatch(text, -1)
	ids := idRE.FindAllStringSubmatch(text, -1)
	seen := map[string]bool{}
	files := make([]FileEntry, 0)
	for i, m := range names {
		name := baiduDecode(m[1])
		if name == "" || seen[name] {
			continue
		}
		seen[name] = true
		fid := ""
		if i < len(ids) {
			fid = ids[i][1]
		}
		files = append(files, FileEntry{ID: fid, Name: name, Path: name})
	}
	if len(files) == 0 {
		sum := sha256.Sum256(html)
		files = append(files, FileEntry{ID: hex.EncodeToString(sum[:]), Name: "分享内容", Path: "分享内容"})
	}
	title := files[0].Name
	return title, files, nil
}

func xunleiTokens(req Request, action string) (string, string, error) {
	client := &http.Client{Timeout: 30 * time.Second}
	access := strings.TrimSpace(req.Credentials["access_token"])
	refresh := strings.TrimSpace(req.Credentials["refresh_token"])
	baseHeaders := map[string]string{"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 Chrome/139 Safari/537.36", "x-client-id": "Xqp0kJBXWhwaTpB6", "x-device-id": "925b7631473a13716b791d7f28289cad"}
	if access == "" && refresh != "" {
		resp, err := doJSON(client, http.MethodPost, "https://xluser-ssl.xunlei.com/v1/auth/token", baseHeaders, map[string]any{"client_id": "Xqp0kJBXWhwaTpB6", "grant_type": "refresh_token", "refresh_token": refresh})
		if err != nil {
			return "", "", err
		}
		access = str(resp["access_token"])
	}
	if access == "" {
		return "", "", fmt.Errorf("迅雷 Access Token 为空")
	}
	captchaBody := map[string]any{"client_id": "Xqp0kJBXWhwaTpB6", "action": action, "device_id": "925b7631473a13716b791d7f28289cad", "meta": map[string]any{"username": "", "phone_number": "", "email": "", "package_name": "pan.xunlei.com", "client_version": "1.45.0", "captcha_sign": "1.fe2108ad808a74c9ac0243309242726c", "timestamp": strconv.FormatInt(time.Now().UnixMilli(), 10), "user_id": "0"}}
	capResp, err := doJSON(client, http.MethodPost, "https://xluser-ssl.xunlei.com/v1/shield/captcha/init", baseHeaders, captchaBody)
	if err != nil {
		return "", "", err
	}
	captcha := str(capResp["captcha_token"])
	if captcha == "" {
		return "", "", fmt.Errorf("迅雷 captcha_token 获取失败")
	}
	return access, captcha, nil
}

func inspectXunlei(req Request) (string, []FileEntry, error) {
	id := shareID(req.URL)
	if id == "" {
		return "", nil, fmt.Errorf("分享链接解析失败")
	}
	access, captcha, err := xunleiTokens(req, "get:/drive/v1/share")
	if err != nil {
		return "", nil, err
	}
	client := &http.Client{Timeout: 45 * time.Second}
	headers := map[string]string{"Accept": "*/*", "Content-Type": "application/json", "Origin": "https://pan.xunlei.com", "Referer": "https://pan.xunlei.com/", "User-Agent": "Mozilla/5.0 Chrome/139 Safari/537.36", "Authorization": "Bearer " + access, "x-captcha-token": captcha, "x-client-id": "Xqp0kJBXWhwaTpB6", "x-device-id": "925b7631473a13716b791d7f28289cad"}
	q := url.Values{"share_id": {id}, "pass_code": {req.Code}, "limit": {"100"}, "pass_code_token": {""}, "page_token": {""}, "thumbnail_size": {"SIZE_SMALL"}}
	root, err := doJSON(client, http.MethodGet, "https://api-pan.xunlei.com/drive/v1/share?"+q.Encode(), headers, nil)
	if err != nil {
		return "", nil, err
	}
	if data := mapv(root["data"]); data != nil {
		root = data
	}
	if status := str(root["share_status"]); status != "" && status != "OK" {
		return "", nil, fmt.Errorf("迅雷分享状态异常: %s", status)
	}
	title := str(root["title"])
	passToken := str(root["pass_code_token"])
	files := make([]FileEntry, 0)
	var walk func(items []any, parentPath string) error
	walk = func(items []any, parentPath string) error {
		for _, raw := range items {
			m := mapv(raw)
			if m == nil {
				continue
			}
			fid := str(m["id"])
			if fid == "" {
				fid = str(m["file_id"])
			}
			name := str(m["name"])
			if fid == "" || name == "" {
				continue
			}
			p := name
			if parentPath != "" {
				p = parentPath + "/" + name
			}
			isDir := str(m["kind"]) == "drive#folder" || intv(m["type"]) == 1
			files = append(files, FileEntry{ID: fid, Name: name, Path: p, Size: i64(m["size"]), IsDir: isDir, UpdatedAt: str(m["modify_time"])})
			if isDir && passToken != "" && len(files) < 5000 {
				dq := url.Values{"share_id": {id}, "pass_code_token": {passToken}, "parent_id": {fid}, "limit": {"100"}, "page_token": {""}, "thumbnail_size": {"SIZE_SMALL"}}
				detail, err := doJSON(client, http.MethodGet, "https://api-pan.xunlei.com/drive/v1/share/detail?"+dq.Encode(), headers, nil)
				if err != nil {
					return err
				}
				if data := mapv(detail["data"]); data != nil {
					detail = data
				}
				if err := walk(listv(detail["files"]), p); err != nil {
					return err
				}
			}
		}
		return nil
	}
	if err := walk(listv(root["files"]), ""); err != nil {
		return "", nil, err
	}
	if title == "" && len(files) > 0 {
		title = files[0].Name
	}
	return title, files, nil
}

func inspect(req Request) (string, []FileEntry, error) {
	switch netdisk.DetectPanType(req.URL) {
	case netdisk.PanBaidu:
		return inspectBaidu(req)
	case netdisk.PanQuark:
		return inspectQuark(req, false)
	case netdisk.PanUC:
		return inspectQuark(req, true)
	case netdisk.PanXunlei:
		return inspectXunlei(req)
	default:
		return "", nil, fmt.Errorf("无法识别网盘平台")
	}
}
