"""AgentPlan（火山方舟 /api/plan/v3）媒体生成薄客户端。

Key 约定：读 RELAY_API_KEY_CN（与 CN 四席同一把 AgentPlan key），ARK_API_KEY 兜底。
端点（用户确认）：图片 POST {base}/images/generations；视频任务 POST/GET/DELETE {base}/contents/generations/tasks。
产物 URL 仅保存 24 小时——拿到即下载落盘。生成请求不重试（避免重复扣费），网络层错误最多重试 2 次。
"""
from __future__ import annotations

import base64
import ipaddress
import json
import mimetypes
import os
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[4]  # 项目根（.agents/skills/storm-media/scripts/ 上四级）
BASE = os.environ.get("ARK_API_BASE_URL", "https://ark.cn-beijing.volces.com/api/plan/v3").rstrip("/")
IMAGE_URL = f"{BASE}/images/generations"
TASKS_URL = f"{BASE}/contents/generations/tasks"

# 日期版模型 ID（用户确认）。lite 别名在 AgentPlan 不受支持（实测 404 UnsupportedModel），
# 廉价档用 flash；别名 doubao-seedream-5.0-pro 可用但统一用日期版 ID。
MODEL_IMAGE = {"flash": "doubao-seedream-5-0-flash-260915", "pro": "doubao-seedream-5-0-pro-260628"}
MODEL_VIDEO = "doubao-seedance-2-5-260628"

TASK_TERMINAL = ("succeeded", "failed", "expired", "cancelled")


def get_key() -> str:
    for var in ("RELAY_API_KEY_CN", "ARK_API_KEY"):
        key = os.environ.get(var, "").strip()
        if key:
            return key
    raise SystemExit("未找到 API Key：请设置环境变量 RELAY_API_KEY_CN（或 ARK_API_KEY）")


def _headers() -> dict:
    return {"Content-Type": "application/json", "Authorization": f"Bearer {get_key()}"}


def image_generation(body: dict, timeout: float = 120.0) -> dict:
    """同步图片生成；返回响应 JSON（取 data[i].url）。失败抛 SystemExit 带服务端错误。"""
    last = None
    for attempt in range(2):
        try:
            resp = httpx.post(IMAGE_URL, headers=_headers(), json=body, timeout=timeout)
            if resp.status_code != 200:
                raise SystemExit(f"图片生成失败 HTTP {resp.status_code}：{resp.text[:500]}")
            return resp.json()
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last = e
            if attempt == 1:
                break
            time.sleep(2)
    raise SystemExit(f"图片生成网络错误：{last}")


def create_task(body: dict, timeout: float = 60.0) -> str:
    """创建视频任务，返回任务 id。"""
    last = None
    for attempt in range(2):
        try:
            resp = httpx.post(TASKS_URL, headers=_headers(), json=body, timeout=timeout)
            if resp.status_code != 200:
                raise SystemExit(f"创建视频任务失败 HTTP {resp.status_code}：{resp.text[:500]}")
            data = resp.json()
            tid = data.get("id") or data.get("task_id") or data.get("taskId")
            if not tid:
                raise SystemExit(f"创建视频任务响应无任务 id：{json.dumps(data, ensure_ascii=False)[:500]}")
            return tid
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last = e
            if attempt == 1:
                break
            time.sleep(2)
    raise SystemExit(f"创建视频任务网络错误：{last}")


def get_task(task_id: str, timeout: float = 60.0) -> dict:
    resp = httpx.get(f"{TASKS_URL}/{task_id}", headers=_headers(), timeout=timeout)
    if resp.status_code != 200:
        raise SystemExit(f"查询任务失败 HTTP {resp.status_code}：{resp.text[:500]}")
    return resp.json()


def poll_task(task_id: str, interval: float = 5.0, limit: float = 1200.0) -> dict:
    """轮询至终态（succeeded/failed/expired/cancelled），返回任务 JSON。"""
    start = time.monotonic()
    while True:
        try:
            data = get_task(task_id)
        except SystemExit:
            raise
        except Exception as e:
            print(f"  查询异常（继续轮询）：{e}")
            data = {}
        status = data.get("status", "")
        if status in TASK_TERMINAL:
            return data
        if time.monotonic() - start > limit:
            raise SystemExit(f"任务 {task_id} 轮询超时（{limit:.0f}s，当前状态 {status or '未知'}）")
        print(f"  任务 {task_id} 状态：{status or '未知'}，{interval:.0f}s 后重查……")
        time.sleep(interval)


def to_data_uri(path: str | Path) -> str:
    """本地图片/音频转 data URI（视频不支持 base64——2.5 限制，调用方勿传）。"""
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"素材文件不存在：{p}")
    if p.stat().st_size > 30 * 1024 * 1024:
        raise SystemExit(f"素材超过 30MB 上限：{p}")
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"


def resolve_asset(ref: str) -> str:
    """参考素材参数统一入口：http(s) URL / asset:// 原样返回，本地路径转 data URI。"""
    if ref.startswith(("http://", "https://", "asset://")):
        return ref
    return to_data_uri(ref)


TRUSTED_HOSTS = (".volces.com", ".volcengine.com", ".volcdns.com", ".volcengineapi.com")


def _public_https(url: str) -> None:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise SystemExit(f"非法下载地址：{url[:120]}")
    host = u.hostname.lower()
    # 火山官方对象存储域名：URL 来自 API 响应本身、非用户输入，可信；
    # 且本机代理 fake-ip 模式会把它们解析到 198.18.0.0/15 保留段，IP 检查必然误伤
    if any(host.endswith(d) for d in TRUSTED_HOSTS):
        return
    try:
        for info in socket.getaddrinfo(u.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                raise SystemExit(f"拒绝内网/回环下载地址：{url[:120]}")
    except socket.gaierror:
        raise SystemExit(f"下载地址域名无法解析：{url[:120]}")


REGISTRY = ROOT / "media" / "registry.json"


def register_asset(out: Path, url: str, kind: str, **meta) -> None:
    """资产登记：产物原始 URL 连同本地文件路径写入 media/registry.json。

    原始 URL 是信任链凭证（Seedream 自产人脸图以其原始 URL 喂给 Seedance 可免输入审核），
    但仅 24 小时有效——必须生成当场登记；过期后退化用本地文件 base64（信任链待实测）。
    """
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "kind": kind,  # image / video / last_frame
        "file": str(out.relative_to(ROOT)) if out.is_absolute() and str(out).startswith(str(ROOT)) else str(out),
        "url": url,
        **meta,
    }
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(REGISTRY.read_text(encoding="utf-8")) if REGISTRY.exists() else []
    data.append(entry)
    REGISTRY.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def download(url: str, out: str | Path, timeout: float = 300.0, retries: int = 3) -> Path:
    """下载产物到本地（产物 URL 仅 24 小时有效，拿到即存）。网络中断重试（产物 URL 不可再生）。"""
    _public_https(url)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with httpx.stream("GET", url, timeout=timeout, follow_redirects=False) as resp:
                if resp.status_code != 200:
                    raise SystemExit(f"下载失败 HTTP {resp.status_code}：{url[:120]}")
                with open(out, "wb") as f:
                    for chunk in resp.iter_bytes(1 << 16):
                        f.write(chunk)
            return out
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise SystemExit(f"下载网络错误（重试 {retries} 次仍失败）：{last}")
