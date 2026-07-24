package main

import "testing"

func TestBooleanDirectoryFields(t *testing.T) {
	if intv(true) != 1 || !boolv(true) {
		t.Fatalf("布尔 true 应识别为目录标记")
	}
	if !cloudItemIsDir(map[string]any{"dir": true, "type": 0}) {
		t.Fatalf("夸克返回 dir=true 时必须识别为文件夹")
	}
	if !cloudItemIsDir(map[string]any{"is_dir": true}) {
		t.Fatalf("is_dir=true 时必须识别为文件夹")
	}
	if cloudItemIsDir(map[string]any{"dir": false, "type": 0}) {
		t.Fatalf("普通文件不应识别为文件夹")
	}
}

func TestCloudCreatedFID(t *testing.T) {
	cases := []map[string]any{
		{"data": map[string]any{"fid": "fid-a"}},
		{"data": map[string]any{"file": map[string]any{"fid": "fid-b"}}},
		{"file": map[string]any{"fid": "fid-c"}},
	}
	want := []string{"fid-a", "fid-b", "fid-c"}
	for i, item := range cases {
		if got := cloudCreatedFID(item); got != want[i] {
			t.Fatalf("第%d种建目录响应解析失败: got=%q want=%q", i, got, want[i])
		}
	}
}
