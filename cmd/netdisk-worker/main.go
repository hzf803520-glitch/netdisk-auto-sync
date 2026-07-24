package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"strings"

	netdisk "github.com/wgx0307/netdisk"
)

type Request struct {
	Action      string            `json:"action"`
	URL         string            `json:"url"`
	Code        string            `json:"code"`
	SharePwd    string            `json:"share_pwd"`
	ExpiredType int               `json:"expired_type"`
	SaveDir     string            `json:"save_dir"`
	Credentials map[string]string `json:"credentials"`
	FIDs        []string          `json:"fids"`
}

type FileEntry struct {
	ID        string `json:"id"`
	Name      string `json:"name"`
	Path      string `json:"path"`
	Size      int64  `json:"size"`
	IsDir     bool   `json:"is_dir"`
	UpdatedAt string `json:"updated_at"`
}

type Response struct {
	Success     bool        `json:"success"`
	Error       string      `json:"error,omitempty"`
	Title       string      `json:"title,omitempty"`
	ShareURL    string      `json:"share_url,omitempty"`
	Code        string      `json:"code,omitempty"`
	FID         string      `json:"fid,omitempty"`
	Files       []FileEntry `json:"files,omitempty"`
	Fingerprint string      `json:"fingerprint,omitempty"`
	TargetDir   string      `json:"target_dir,omitempty"`
}

func output(resp Response) {
	data, _ := json.Marshal(resp)
	fmt.Println(string(data))
}

func main() {
	reader := bufio.NewReader(os.Stdin)
	decoder := json.NewDecoder(reader)
	var req Request
	if err := decoder.Decode(&req); err != nil {
		output(Response{Success: false, Error: "请求JSON解析失败: " + err.Error()})
		return
	}
	if req.URL == "" {
		output(Response{Success: false, Error: "分享链接为空"})
		return
	}

	switch strings.ToLower(req.Action) {
	case "inspect":
		if _, err := configure(Request{URL: req.URL, Credentials: req.Credentials, SaveDir: "/"}); err != nil && netdisk.DetectPanType(req.URL) != netdisk.PanBaidu {
			cfg := &netdisk.Config{Debug: false, BaiduCookie: req.Credentials["cookie"], QuarkCookie: req.Credentials["cookie"], UCCookie: req.Credentials["cookie"], XunleiRefreshToken: req.Credentials["refresh_token"], XunleiAccessToken: req.Credentials["access_token"]}
			netdisk.SetConfig(cfg)
		}
		title, files, err := inspect(req)
		if err != nil {
			output(Response{Success: false, Error: err.Error()})
			return
		}
		output(Response{Success: true, Title: title, Files: files, Fingerprint: fingerprint(files)})
	case "transfer":
		target, err := configure(req)
		if err != nil {
			output(Response{Success: false, Error: err.Error()})
			return
		}
		if req.ExpiredType == 0 {
			req.ExpiredType = 1
		}
		result, err := netdisk.Transfer(req.URL, req.Code, req.ExpiredType, req.SharePwd)
		if err != nil {
			output(Response{Success: false, Error: err.Error()})
			return
		}
		output(Response{Success: true, Title: result.Title, ShareURL: result.ShareURL, Code: result.Code, FID: result.FID, TargetDir: target})
	case "list_target":
		if len(req.FIDs) == 0 {
			output(Response{Success: false, Error: "目标文件ID为空"})
			return
		}
		if _, err := configure(Request{URL: req.URL, Credentials: req.Credentials, SaveDir: "/"}); err != nil {
			output(Response{Success: false, Error: err.Error()})
			return
		}
		panType := netdisk.DetectPanType(req.URL)
		if panType < 0 {
			output(Response{Success: false, Error: "无法识别网盘平台"})
			return
		}
		files := make([]FileEntry, 0)
		for _, fid := range req.FIDs {
			fid = strings.TrimSpace(fid)
			if fid == "" {
				continue
			}
			items, err := netdisk.GetFiles(panType, fid)
			if err != nil {
				output(Response{Success: false, Error: err.Error()})
				return
			}
			for _, item := range items {
				files = append(files, FileEntry{ID: item.FID, Name: item.FileName, Path: item.FileName, Size: item.Size, IsDir: item.IsDir, UpdatedAt: item.UpdatedAt})
			}
		}
		output(Response{Success: true, Files: files, Fingerprint: fingerprint(files)})
	case "verify":
		if _, err := configure(req); err != nil {
			output(Response{Success: false, Error: err.Error()})
			return
		}
		result, err := netdisk.Verify(req.URL, req.Code)
		if err != nil {
			output(Response{Success: false, Error: err.Error()})
			return
		}
		output(Response{Success: true, Title: result.Title, ShareURL: result.ShareURL})
	case "delete":
		if len(req.FIDs) == 0 {
			output(Response{Success: true})
			return
		}
		if _, err := configure(req); err != nil {
			output(Response{Success: false, Error: err.Error()})
			return
		}
		panType := netdisk.DetectPanType(req.URL)
		if panType < 0 {
			output(Response{Success: false, Error: "无法识别网盘平台"})
			return
		}
		if err := netdisk.Delete(panType, req.FIDs); err != nil {
			output(Response{Success: false, Error: err.Error()})
			return
		}
		output(Response{Success: true})
	default:
		output(Response{Success: false, Error: "不支持的执行动作"})
	}
}
