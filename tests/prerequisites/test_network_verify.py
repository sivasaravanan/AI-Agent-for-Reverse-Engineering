import hashlib
import ssl
import threading
import urllib.error

import pytest

from prerequisites import network
from prerequisites.network import NetworkError, classify, download, host_allowed
from prerequisites.verify import parse_shasums, sha256_file, verify_installer
from tests.prerequisites.conftest import PAYLOAD, PAYLOAD_SHA, make_cfg


def dl(url, dest, cfg, **kw):
    return download(url, dest, cfg, allow_http_loopback=True, **kw)


def test_download_ok_with_hash(tmp_path, http_server):
    cfg = make_cfg(tmp_path)
    seen = []
    res = dl(http_server + "/file", tmp_path / "d" / "f.bin", cfg, expected_sha256=PAYLOAD_SHA, progress=lambda a, b: seen.append(a))
    assert res.sha256 == PAYLOAD_SHA and res.size == len(PAYLOAD) and (tmp_path / "d" / "f.bin").read_bytes() == PAYLOAD
    assert seen and not (tmp_path / "d" / "f.bin.part").exists()


def test_download_resumes_partial_file(tmp_path, http_server):
    cfg = make_cfg(tmp_path)
    (tmp_path / "f.bin.part").write_bytes(PAYLOAD[:300000])
    res = dl(http_server + "/file", tmp_path / "f.bin", cfg, expected_sha256=PAYLOAD_SHA)
    assert res.resumed and res.sha256 == PAYLOAD_SHA


def test_hash_mismatch_deletes_file_and_raises(tmp_path, http_server):
    cfg = make_cfg(tmp_path)
    with pytest.raises(NetworkError) as ei:
        dl(http_server + "/file", tmp_path / "f.bin", cfg, expected_sha256="0" * 64)
    assert ei.value.kind == "hash-mismatch" and not (tmp_path / "f.bin").exists() and not (tmp_path / "f.bin.part").exists()


def test_size_cap(tmp_path, http_server):
    cfg = make_cfg(tmp_path, max_download_mb=1)
    (tmp_path / "x").mkdir()
    big = make_cfg(tmp_path, max_download_mb=1)
    big.values["max_download_mb"] = 0                      # force tiny limit below the 1 MiB payload
    with pytest.raises(NetworkError) as ei:
        dl(http_server + "/file", tmp_path / "f.bin", big)
    assert ei.value.kind == "too-large"


def test_policy_and_host_allowlist(tmp_path):
    off = make_cfg(tmp_path, allow_downloads=False)
    with pytest.raises(NetworkError) as ei:
        download("https://github.com/x", tmp_path / "f", off)
    assert ei.value.kind == "policy"
    cfg = make_cfg(tmp_path)
    for bad in ("https://evil.example/x.exe", "http://github.com/x.exe", "ftp://github.com/x"):
        with pytest.raises(NetworkError) as ei:
            download(bad, tmp_path / "f", cfg)
        assert ei.value.kind == "blocked-host"
    assert host_allowed("https://objects.githubusercontent.com/a") and not host_allowed("https://github.com.evil.example/a")
    assert host_allowed("https://dl.corp.example/a", ["corp.example"])


def test_redirect_to_unlisted_host_is_blocked(tmp_path, http_server):
    cfg = make_cfg(tmp_path)
    with pytest.raises(NetworkError) as ei:
        dl(http_server + "/redirect", tmp_path / "f", cfg)
    assert ei.value.kind in ("blocked-redirect", "network", "blocked")


@pytest.mark.parametrize("path,kind", [("/auth", "proxy-auth"), ("/forbidden", "blocked"), ("/nothing", "not-found")])
def test_http_errors_are_classified_with_advice(tmp_path, http_server, path, kind):
    with pytest.raises(NetworkError) as ei:
        dl(http_server + path, tmp_path / "f", make_cfg(tmp_path))
    assert ei.value.kind == kind and ei.value.advice


def test_classify_tls_timeout_dns():
    assert classify(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED")).kind == "tls-inspection"
    assert classify(TimeoutError("t")).kind == "timeout"
    import socket
    assert classify(urllib.error.URLError(socket.gaierror(-2, "x"))).kind == "dns"
    assert "root certificate" in classify(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED")).advice


def test_cancel_stops_download(tmp_path, http_server):
    ev = threading.Event()
    ev.set()
    with pytest.raises(NetworkError) as ei:
        dl(http_server + "/file", tmp_path / "f", make_cfg(tmp_path), cancel=ev)
    assert ei.value.kind == "cancelled"


def test_bad_ca_bundle_is_reported(tmp_path):
    cfg = make_cfg(tmp_path, ca_bundle=str(tmp_path / "missing.pem"))
    with pytest.raises(NetworkError) as ei:
        network.build_opener(cfg)
    assert ei.value.kind == "ca-bundle"


def test_connectivity_report_with_fake_opener(tmp_path):
    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    class Op:
        def open(self, req, timeout=0):
            if "pypi" in req.full_url:
                raise urllib.error.URLError(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED"))
            if "github" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 405, "x", {}, None)
            return Resp()
    rows = {r["host"]: r for r in network.check_connectivity(make_cfg(tmp_path), ["github.com", "pypi.org", "nodejs.org"], opener=Op())}
    assert rows["github.com"]["ok"] == "yes" and rows["nodejs.org"]["ok"] == "yes"
    assert rows["pypi.org"]["ok"] == "no" and rows["pypi.org"]["kind"] == "tls-inspection"


def test_child_env(tmp_path):
    assert network.child_env(make_cfg(tmp_path)) is None
    pem = tmp_path / "ca.pem"
    pem.write_text("x")
    env = network.child_env(make_cfg(tmp_path, proxy="http://p:8080", no_proxy="corp", ca_bundle=str(pem), pip_index_url="https://idx/simple",
                                     npm_registry="https://npm.corp/"), base={"PATH": "x"})
    assert env["HTTPS_PROXY"] == "http://p:8080" and env["PIP_CERT"] == str(pem) and env["PIP_INDEX_URL"] == "https://idx/simple"
    assert env["npm_config_registry"] == "https://npm.corp/" and env["NO_PROXY"] == "corp" and env["PATH"] == "x"


def test_explicit_proxy_respects_no_proxy(tmp_path):
    cfg = make_cfg(tmp_path, proxy="http://proxy:3128", no_proxy="internal.corp")
    op = network.build_opener(cfg)
    handler = next(h for h in op.handlers if isinstance(h, network._ExplicitProxy))
    assert network._bypass("a.internal.corp", cfg.no_proxy) and not network._bypass("github.com", cfg.no_proxy)
    assert handler is not None


# ---------------------------------------------------------------- verification
def test_parse_shasums_formats():
    h = "a" * 64
    assert parse_shasums(f"{h}  podman.exe\n{h.upper()} *dir/other.msi\nnot a line") == {"podman.exe": h, "other.msi": h}


def test_verify_installer_rules(tmp_path):
    f = tmp_path / "setup.exe"
    f.write_bytes(b"installer")
    sha = hashlib.sha256(b"installer").hexdigest()
    assert verify_installer(f, expected_sha256=sha, windows=False).ok
    bad = verify_installer(f, expected_sha256="1" * 64, windows=False)
    assert not bad.ok and "mismatch" in bad.detail
    assert not verify_installer(f, windows=False).ok                                     # nothing to verify with -> fail closed
    assert verify_installer(f, windows=False, require=False).detail.startswith("UNVERIFIED")

    def signed(cmd):
        return 0, '{"Status":"Valid","Subject":"CN=Docker Inc, O=Docker Inc"}', ""

    def unsigned(cmd):
        return 0, '{"Status":"NotSigned","Subject":""}', ""

    def other(cmd):
        return 0, '{"Status":"Valid","Subject":"CN=Somebody Else"}', ""
    ok = verify_installer(f, publishers=["Docker Inc"], runner=signed)
    assert ok.ok and ok.method == "authenticode" and "Docker" in ok.publisher
    assert not verify_installer(f, publishers=["Docker Inc"], runner=unsigned).ok
    assert not verify_installer(f, publishers=["Docker Inc"], runner=other).ok
    assert verify_installer(f, expected_sha256=sha, publishers=["Docker Inc"], runner=signed).method == "sha256+authenticode"
    assert sha256_file(f) == sha
