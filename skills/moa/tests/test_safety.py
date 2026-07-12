"""§8 SAFETY: 敏感材料告警 + 密钥泄漏静态自查 的回归测试。

关键不变量: 检测器的输出**绝不回显原文密钥**(否则自查脚本自身就成了泄漏源)。
注意: 用例里的假密钥所在行不能含 fake/test/example/... 等占位符提示词,
否则会被 _PLACEHOLDER_HINTS 正确抑制,反而测不到检出路径。
"""
import argparse
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import moa  # noqa: E402

# 每个含密钥的行都刻意不含占位符提示词
DIRTY = (
    'authorization = "sk-live0011223344556677889900aabbccdd"\n'
    'db = "postgres://admin:supersecretpw@10.0.0.1:5432/app"\n'
    'aws = "AKIAZ8Q7R2W4T6Y1U3P5"\n'
    'password = "hunter2hunter2hunter2"\n'
    "-----BEGIN RSA PRIVATE KEY-----\n"
)
CLEAN = (
    "export OPENROUTER_API_KEY=...\n"
    'key = os.environ["OPENROUTER_API_KEY"]\n'
    "api_key_env: OPENROUTER_API_KEY\n"
    'token = "change-me"\n'
    'password = "your-password-here"\n'
)
RAW_SECRETS = ["sk-live0011223344556677889900aabbccdd", "supersecretpw",
               "AKIAZ8Q7R2W4T6Y1U3P5", "hunter2hunter2hunter2"]


def test_scan_detects_all_secret_families():
    cats = {h["category"] for h in moa.scan_secrets(DIRTY)}
    assert {"openai_key", "conn_string", "aws_access_key",
            "secret_assign", "private_key"} <= cats


def test_scan_previews_never_leak_raw_secret():
    # 最重要: 脱敏预览里不得出现任何原文密钥
    previews = " ".join(h["preview"] for h in moa.scan_secrets(DIRTY))
    for raw in RAW_SECRETS:
        assert raw not in previews, f"预览泄漏了原文: {raw}"
    assert "***" in previews and "len " in previews  # 确有脱敏发生


def test_redact_masks_middle_and_shows_length():
    r = moa._redact("sk-live0011223344556677889900aabbccdd")
    assert "sk-" in r and "len 37" in r
    assert "0011223344" not in r
    assert moa._redact("short") == "*****"  # 过短则全遮


def test_clean_text_has_no_findings():
    # 占位符 / 环境变量引用 / change-me / your- 均不算泄漏
    assert moa.scan_secrets(CLEAN) == []


def test_leak_check_flags_dirty_skips_clean_and_binary(tmp_path):
    (tmp_path / "leaky.txt").write_text(DIRTY, encoding="utf-8")
    (tmp_path / "ok.txt").write_text(CLEAN, encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b'sk-live0011223344556677889900aabbccdd binary')
    findings = moa.leak_check([str(tmp_path)])
    files = {os.path.basename(h["file"]) for h in findings}
    assert "leaky.txt" in files          # 脏文件被抓
    assert "ok.txt" not in files         # 干净文件不误报
    assert "image.png" not in files      # 二进制按扩展名跳过
    # 泄漏报告本身也脱敏
    for h in findings:
        for raw in RAW_SECRETS:
            assert raw not in h["preview"]


def test_leak_check_missing_path_is_noop():
    assert moa.leak_check(["nonexistent-dir-xyz"]) == []


def test_leak_check_zero_files_scanned_errors_not_clean(capsys):
    """P0-2 回归: 一个文件都没扫到(路径全不存在)时必须以退出码 2 报错, 不得冒充 clean。
    旧逻辑默认扫描面全是相对路径, 从非项目根运行时静默打 clean = 假阴性安全承诺。"""
    args = argparse.Namespace(paths=["nonexistent-dir-xyz-123"])
    with pytest.raises(SystemExit) as ei:
        moa.cmd_leak_check(args)
    assert ei.value.code == 2
    out = capsys.readouterr()
    assert "未扫描到任何文件" in out.err
    assert "clean" not in out.out  # 绝不能同时打 clean


def test_leak_check_scans_real_files_reports_clean(tmp_path, capsys):
    """有文件可扫且干净 → 正常 clean 退出(退出码 0, 不抛)。与 0 文件路径区分开。"""
    (tmp_path / "ok.txt").write_text(CLEAN, encoding="utf-8")
    args = argparse.Namespace(paths=[str(tmp_path)])
    moa.cmd_leak_check(args)  # 不抛 SystemExit
    assert "clean" in capsys.readouterr().out


def test_warn_sensitive_material_returns_hits_and_prints(capsys):
    hits = moa.warn_sensitive_material(DIRTY)
    assert len(hits) >= 4
    err = capsys.readouterr().err
    assert "敏感信息告警" in err
    for raw in RAW_SECRETS:
        assert raw not in err            # 告警文案也不回显原文


def test_warn_sensitive_material_clean_is_silent(capsys):
    assert moa.warn_sensitive_material(CLEAN) == []
    assert capsys.readouterr().err == ""
