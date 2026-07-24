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

type Response struct {
	Success  bool   `json:"success"`
	Error    string `json:"error,omitempty"`
	Title    string `json:"title,omitempty"`
	ShareURL string `json:"share_url,omitempty"`
	Code     string `json:"code,omitempty"`
	FID      string `json:"fid,omitempty"`
}

func output(resp Response) {
	data, _ := json.Marshal(resp)
	fmt.Println(string(data))
}

func configure(req Request) {
	creds := req.Credentials
	cfg := &netdisk.Config{Debug: false}
	switch netdisk.DetectPanType(req.URL) {
	case netdisk.PanBaidu:
		cfg.BaiduCookie = creds["cookie"]
		cfg.BaiduSaveDir = req.SaveDir
	case netdisk.PanQuark:
		cfg.QuarkCookie = creds["cookie"]
		cfg.QuarkSaveDir = req.SaveDir
	case netdisk.PanUC:
		cfg.UCCookie = creds["cookie"]
		cfg.UCSaveDir = req.SaveDir
	case netdisk.PanXunlei:
		cfg.XunleiRefreshToken = creds["refresh_token"]
		cfg.XunleiAccessToken = creds["access_token"]
		// SDK 的配置检查要求 RefreshToken 非空；已有 AccessToken 时用占位值通过检查，实际请求仍优先使用 AccessToken。
		if cfg.XunleiRefreshToken == "" && cfg.XunleiAccessToken != "" {
			cfg.XunleiRefreshToken = "access-token-present"
		}
		cfg.XunleiSaveDir = req.SaveDir
	}
	netdisk.SetConfig(cfg)
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
	configure(req)

	switch strings.ToLower(req.Action) {
	case "transfer":
		if req.ExpiredType == 0 {
			req.ExpiredType = 1
		}
		result, err := netdisk.Transfer(req.URL, req.Code, req.ExpiredType, req.SharePwd)
		if err != nil {
			output(Response{Success: false, Error: err.Error()})
			return
		}
		output(Response{
			Success:  true,
			Title:    result.Title,
			ShareURL: result.ShareURL,
			Code:     result.Code,
			FID:      result.FID,
		})
	case "verify":
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
