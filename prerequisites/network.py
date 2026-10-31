"""Institution-friendly downloads: proxy (explicit, environment or system), custom CA bundle, host allow-list, resume,
retries, size cap, SHA-256 and cancellation. HTTPS only (plain http is accepted for loopback in tests).

Python's default TLS context uses the Windows certificate store, so a corporate root CA that IT installed system-wide just works;
``ca_bundle`` covers machines where it is only available as a PEM file.
"""
from __future__ import annotations

import hashlib
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from prerequisites.audit import mask

DEFAULT_ALLOWED_HOSTS = (
    "github.com", "api.github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com",
    "github-releases.githubusercontent.com", "desktop.docker.com", "download.docker.com", "registry.npmjs.org",
    "pypi.org", "files.pythonhosted.org", "nodejs.org", "ollama.com", "aka.ms", "download.visualstudio.microsoft.com",
)
LOOPBACK = ("127.0.0.1", "localhost", "::1")
Progress = Callable[[int, Optional[int]], None]


class NetworkError(Exception):
    def __init__(self, kind: str, message: str, advice: str = ""):
        super().__init__(message)
        self.kind, self.advice = kind, advice


@dataclass
class DownloadResult:
    path: Path
    sha256: str
    size: int
    resumed: bool = False
    url: str = ""


def host_allowed(url: str, extra: Iterable[str] = ()) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if not host:
        return False
    allowed = [h.lower().lstrip(".") for h in (*DEFAULT_ALLOWED_HOSTS, *extra) if h]
    return any(host == h or host.endswith("." + h) for h in allowed)


def classify(exc: BaseException) -> NetworkError:
    """Turn a low-level failure into a kind + advice a non-expert (or the IT desk) can act on."""
    if isinstance(exc, NetworkError):
        return exc
    text = f"{type(exc).__name__}: {exc}"
    reason = getattr(exc, "reason", None)
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 407:
            return NetworkError("proxy-auth", text, "The proxy requires authentication. Set the proxy URL with credentials in the "
                                "Prerequisites settings (http://user:password@host:port), ask IT for an exception, or use the offline folder.")
        if exc.code in (401, 403):
            return NetworkError("blocked", text, "The server or a filter refused the request. Ask IT to allow the host, "
                                "use the internal mirror setting, or use the offline folder.")
        if exc.code == 404:
            return NetworkError("not-found", text, "The file is not at the expected address any more. Use winget, the offline folder, or the vendor page.")
        return NetworkError("http", text, "Temporary server problem? Try again later.")
    if isinstance(exc, ssl.SSLCertVerificationError) or isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in text:
        return NetworkError("tls-inspection", text, "TLS verification failed - typically a corporate proxy re-signing HTTPS traffic. "
                            "Install your institution's root certificate in the Windows certificate store, or set 'ca_bundle' to its PEM file.")
    if isinstance(exc, (socket.timeout, TimeoutError)) or isinstance(reason, (socket.timeout, TimeoutError)):
        return NetworkError("timeout", text, "The connection timed out. Check the proxy setting and firewall, or try the offline folder.")
    if isinstance(reason, socket.gaierror) or isinstance(exc, socket.gaierror):
        return NetworkError("dns", text, "The host name could not be resolved. You may be offline or behind a proxy that must be configured.")
    if isinstance(exc, (ConnectionRefusedError, ConnectionResetError)) or isinstance(reason, (ConnectionRefusedError, ConnectionResetError)):
        return NetworkError("connection", text, "The connection was refused or reset - a firewall or proxy is probably blocking it.")
    return NetworkError("network", text, "Network problem. Check connectivity, proxy and firewall settings.")


class _GuardRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, extra_hosts: Iterable[str], allow_http_loopback: bool):
        self.extra, self.loop = list(extra_hosts), allow_http_loopback

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _url_ok(newurl, self.extra, self.loop):
            raise NetworkError("blocked-redirect", f"Redirect to a host that is not allowed: {mask(newurl)}",
                               "Add the host to 'allowed_hosts' in the institution policy if you trust it.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _url_ok(url: str, extra: Iterable[str], allow_http_loopback: bool) -> bool:
    p = urllib.parse.urlparse(url)
    if p.scheme == "https":
        return host_allowed(url, extra)
    return bool(allow_http_loopback and p.scheme == "http" and (p.hostname or "") in LOOPBACK)


def _bypass(host: str, no_proxy: str) -> bool:
    items = [x.strip().lower().lstrip(".") for x in no_proxy.replace(";", ",").split(",") if x.strip()]
    host = host.lower()
    return any(host == i or host.endswith("." + i) or i == "*" for i in items)


class _ExplicitProxy(urllib.request.ProxyHandler):
    def __init__(self, proxy: str, no_proxy: str):
        super().__init__({"http": proxy, "https": proxy})
        self._no = no_proxy

    def proxy_open(self, req, proxy, type_):
        if _bypass(req.host.split(":")[0], self._no):
            return None
        return super().proxy_open(req, proxy, type_)


def build_opener(cfg, extra_hosts: Iterable[str] = (), allow_http_loopback: bool = False) -> urllib.request.OpenerDirector:
    handlers: List[urllib.request.BaseHandler] = []
    if cfg.proxy:
        handlers.append(_ExplicitProxy(cfg.proxy, cfg.no_proxy))
    else:
        handlers.append(urllib.request.ProxyHandler())       # environment, then Windows/macOS system settings
    try:
        ctx = ssl.create_default_context(cafile=cfg.ca_bundle or None)
    except (OSError, ssl.SSLError) as exc:
        raise NetworkError("ca-bundle", f"Cannot load CA bundle: {exc}", "Check the 'ca_bundle' path in the Prerequisites settings.") from exc
    handlers.append(urllib.request.HTTPSHandler(context=ctx))
    handlers.append(_GuardRedirect([*cfg.allowed_hosts, *extra_hosts], allow_http_loopback))
    opener = urllib.request.build_opener(*handlers)
    opener.addheaders = [("User-Agent", "AI-Software-Engineering-Agent-Prerequisites/1.0")]
    return opener


def download(url: str, dest: Path, cfg, *, expected_sha256: str = "", progress: Optional[Progress] = None,
             cancel: Optional[threading.Event] = None, opener=None, extra_hosts: Iterable[str] = (),
             allow_http_loopback: bool = False, retries: int = 3, backoff: float = 1.5) -> DownloadResult:
    """Download ``url`` to ``dest`` (resuming ``dest + .part``). Raises NetworkError. Verifies SHA-256 when given."""
    extra = [*cfg.allowed_hosts, *extra_hosts]
    if not cfg.allow_downloads:
        raise NetworkError("policy", "Downloads are disabled by policy.", "Use the offline folder or ask IT to install the software.")
    if not _url_ok(url, extra, allow_http_loopback):
        raise NetworkError("blocked-host", f"URL not allowed (HTTPS and an allow-listed host are required): {mask(url)}",
                           "Add the host to 'allowed_hosts' in the institution policy if it is trusted.")
    opener = opener or build_opener(cfg, extra_hosts, allow_http_loopback)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    limit = int(cfg.max_download_mb) * 1024 * 1024
    last: Optional[NetworkError] = None
    resumed = False
    for attempt in range(1, retries + 1):
        if cancel and cancel.is_set():
            raise NetworkError("cancelled", "Cancelled.")
        have = part.stat().st_size if part.exists() else 0
        req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
        try:
            with opener.open(req, timeout=cfg.timeout_seconds) as resp:
                status = getattr(resp, "status", 200)
                total = resp.headers.get("Content-Length")
                total_i = int(total) if total and total.isdigit() else None
                if have and status == 206:
                    resumed, mode = True, "ab"
                    total_i = (total_i + have) if total_i is not None else None
                else:
                    have, mode = 0, "wb"
                if total_i is not None and total_i > limit:
                    raise NetworkError("too-large", f"Download is {total_i // 1048576} MB; the limit is {cfg.max_download_mb} MB.",
                                       "Raise 'max_download_mb' in the policy if this is expected.")
                done = have
                with part.open(mode) as fh:
                    while True:
                        if cancel and cancel.is_set():
                            raise NetworkError("cancelled", "Cancelled.")
                        chunk = resp.read(262144)
                        if not chunk:
                            break
                        done += len(chunk)
                        if done > limit:
                            raise NetworkError("too-large", f"Download exceeded {cfg.max_download_mb} MB.", "")
                        fh.write(chunk)
                        if progress:
                            progress(done, total_i)
                if total_i is not None and done != total_i:
                    raise NetworkError("incomplete", f"Received {done} of {total_i} bytes.", "Retrying resumes where it stopped.")
            digest = hashlib.sha256()
            with part.open("rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(block)
            sha = digest.hexdigest()
            if expected_sha256 and sha.lower() != expected_sha256.lower():
                part.unlink(missing_ok=True)
                raise NetworkError("hash-mismatch", f"SHA-256 mismatch (expected {expected_sha256[:16]}..., got {sha[:16]}...).",
                                   "The file was altered or the checksum is wrong. It was deleted; do not run it.")
            os.replace(part, dest)
            return DownloadResult(dest, sha, dest.stat().st_size, resumed, mask(url))
        except NetworkError as exc:
            if exc.kind in ("incomplete",) and attempt < retries:
                last = exc
                time.sleep(backoff * attempt)
                continue
            raise
        except BaseException as exc:  # noqa: BLE001 - classified below
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            err = classify(exc)
            last = err
            if err.kind in ("timeout", "connection", "http", "network", "dns") and attempt < retries:
                time.sleep(backoff * attempt)
                continue
            raise err from exc
    raise last or NetworkError("network", "Download failed.")


def fetch_text(url: str, cfg, *, opener=None, max_bytes: int = 2_000_000, allow_http_loopback: bool = False) -> str:
    opener = opener or build_opener(cfg, allow_http_loopback=allow_http_loopback)
    if not _url_ok(url, cfg.allowed_hosts, allow_http_loopback):
        raise NetworkError("blocked-host", f"URL not allowed: {mask(url)}")
    try:
        with opener.open(urllib.request.Request(url), timeout=cfg.timeout_seconds) as resp:
            return resp.read(max_bytes).decode("utf-8", "replace")
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise classify(exc) from exc


def check_connectivity(cfg, hosts: Iterable[str] = ("github.com", "desktop.docker.com", "pypi.org", "registry.npmjs.org"),
                       opener=None, timeout: float = 8.0) -> List[Dict[str, str]]:
    """Per-host reachability through the configured proxy/CA. Never raises."""
    results: List[Dict[str, str]] = []
    try:
        opener = opener or build_opener(cfg)
    except NetworkError as exc:
        return [{"host": h, "ok": "no", "kind": exc.kind, "detail": str(exc), "advice": exc.advice} for h in hosts]
    for h in hosts:
        try:
            with opener.open(urllib.request.Request(f"https://{h}/", method="HEAD"), timeout=timeout) as r:
                results.append({"host": h, "ok": "yes", "kind": "", "detail": f"HTTP {getattr(r, 'status', 200)}", "advice": ""})
        except urllib.error.HTTPError as exc:       # an HTTP answer proves the host is reachable (405/403 on HEAD are common)
            ok = exc.code not in (407,)
            e = classify(exc)
            results.append({"host": h, "ok": "yes" if ok else "no", "kind": "" if ok else e.kind,
                            "detail": f"HTTP {exc.code}", "advice": "" if ok else e.advice})
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            e = classify(exc)
            results.append({"host": h, "ok": "no", "kind": e.kind, "detail": str(e)[:200], "advice": e.advice})
    return results


def child_env(cfg=None, base: Optional[Dict[str, str]] = None) -> Optional[Dict[str, str]]:
    """Environment for pip/npm/podman/docker/winget child processes carrying the institution's settings.
    Returns None when nothing is configured (the child then simply inherits this process's environment)."""
    if cfg is None:
        try:
            from prerequisites.config import load_config
            cfg = load_config()
        except Exception:  # noqa: BLE001
            return None
    extra: Dict[str, str] = {}
    if cfg.proxy:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            extra[k] = cfg.proxy
        if cfg.no_proxy:
            extra["NO_PROXY"] = extra["no_proxy"] = cfg.no_proxy
    if cfg.ca_bundle and os.path.isfile(cfg.ca_bundle):
        for k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "PIP_CERT", "NODE_EXTRA_CA_CERTS", "CURL_CA_BUNDLE"):
            extra[k] = cfg.ca_bundle
    if cfg.pip_index_url:
        extra["PIP_INDEX_URL"] = cfg.pip_index_url
    if cfg.npm_registry:
        extra["npm_config_registry"] = cfg.npm_registry
    if not extra:
        return None
    env = dict(base if base is not None else os.environ)
    env.update(extra)
    return env
