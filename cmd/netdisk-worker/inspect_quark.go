package main

import (
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

func inspectQuark(req Request, uc bool) (string, []FileEntry, error) {
	id := shareID(req.URL)
	if id == "" {
		return "", nil, fmt.Errorf("分享链接解析失败")
	}
	client := &http.Client{Timeout: 45 * time.Second}
	base := "https://drive-pc.quark.cn"
	pr := "ucpro"
	referer := "https://pan.quark.cn/"
	if uc {
		base = "https://pc-api.uc.cn"
		pr = "UCBrowser"
		referer = "https://drive.uc.cn/"
	}
	headers := commonHeaders(req.Credentials["cookie"], referer)
	var token map[string]any
	var err error
	if uc {
		token, err = doJSON(client, http.MethodPost, base+"/1/clouddrive/share/sharepage/v2/detail?pr=UCBrowser&fr=pc", headers, map[string]any{"passcode": req.Code, "pwd_id": id})
	} else {
		token, err = doJSON(client, http.MethodPost, base+"/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc&uc_param_str=", headers, map[string]any{"passcode": req.Code, "pwd_id": id})
	}
	if err != nil {
		return "", nil, err
	}
	if intv(token["status"]) != 200 {
		return "", nil, fmt.Errorf("获取分享令牌失败: %s", str(token["message"]))
	}
	data := mapv(token["data"])
	stoken := ""
	title := ""
	if uc {
		info := mapv(data["token_info"])
		stoken, title = str(info["stoken"]), str(info["title"])
	} else {
		stoken, title = str(data["stoken"]), str(data["title"])
	}
	if stoken == "" {
		return "", nil, fmt.Errorf("分享令牌为空")
	}
	files := make([]FileEntry, 0)
	visited := map[string]bool{}
	var walk func(parentID, parentPath string) error
	walk = func(parentID, parentPath string) error {
		if visited[parentID] {
			return nil
		}
		visited[parentID] = true
		for pageNo := 1; pageNo <= 30; pageNo++ {
			q := url.Values{}
			q.Set("pr", pr)
			q.Set("fr", "pc")
			q.Set("pwd_id", id)
			q.Set("stoken", stoken)
			q.Set("pdir_fid", parentID)
			q.Set("force", "0")
			q.Set("_page", strconv.Itoa(pageNo))
			q.Set("_size", "100")
			q.Set("_fetch_banner", "0")
			q.Set("_fetch_share", "1")
			q.Set("_fetch_total", "1")
			q.Set("_sort", "file_type:asc,updated_at:desc")
			resp, err := doJSON(client, http.MethodGet, base+"/1/clouddrive/share/sharepage/detail?"+q.Encode(), headers, nil)
			if err != nil {
				return err
			}
			if intv(resp["status"]) != 200 {
				return fmt.Errorf("读取分享文件失败: %s", str(resp["message"]))
			}
			body := mapv(resp["data"])
			if strings.TrimSpace(title) == "" {
				title = str(jsonPath(body, "share", "title"))
			}
			items := listv(body["list"])
			if len(items) == 0 {
				break
			}
			for _, raw := range items {
				m := mapv(raw)
				name := str(m["file_name"])
				fid := str(m["fid"])
				if name == "" || fid == "" {
					continue
				}
				p := name
				if parentPath != "" {
					p = parentPath + "/" + name
				}
				isDir := intv(m["type"]) == 1 || boolv(m["dir"]) || intv(m["file_type"]) == 0 && boolv(m["is_dir"])
				files = append(files, FileEntry{ID: fid, Name: name, Path: p, Size: i64(m["size"]), IsDir: isDir, UpdatedAt: str(m["updated_at"])})
				if isDir && len(files) < 5000 {
					if err := walk(fid, p); err != nil {
						return err
					}
				}
			}
			meta := mapv(resp["metadata"])
			total := intv(meta["_total"])
			if total == 0 || pageNo*100 >= total {
				break
			}
		}
		return nil
	}
	if err := walk("0", ""); err != nil {
		return "", nil, err
	}
	if strings.TrimSpace(title) == "" {
		rootDirs := make([]FileEntry, 0)
		for _, item := range files {
			if item.IsDir && !strings.Contains(strings.Trim(item.Path, "/"), "/") {
				rootDirs = append(rootDirs, item)
			}
		}
		if len(rootDirs) == 1 {
			title = rootDirs[0].Name
		} else if len(files) == 1 {
			title = files[0].Name
		}
	}
	return strings.TrimSpace(title), files, nil
}
