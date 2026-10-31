#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
接口探活监控脚本（GitHub Actions 版本）
- 每轮最多探测 3 次，失败间隔 10 秒
- 判定：响应体含 "API NOT EXIST" 为正常；其他（HTTP 失败/超时/连接失败）为异常
- 正常/异常均发飞书消息；连续 3 次失败时对刚发消息执行电话加急
- 环境变量：
  FEISHU_APP_ID     飞书自建应用 App ID
  FEISHU_APP_SECRET 飞书自建应用 App Secret
  RECEIVE_OPEN_ID   接收通知的飞书用户 open_id
  (可选) PROBE_URL  探活接口地址，默认下方业务地址
  (可选) RECEIVE_MOBILE / RECEIVE_EMAIL  用于 --resolve-recipient 解析 open_id
用法：
  python3 probe_monitor.py                    # 正常执行（探测+通知+异常电话加急）
  python3 probe_monitor.py --probe-only       # 只探测并打印结果，不通知（本地验证）
  python3 probe_monitor.py --resolve-recipient # 用手机号/邮箱换应用视角的 open_id
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime

PROBE_URL = os.environ.get("PROBE_URL") or (
    "https://zyjj4x.cmiiyl.com/api/ambulance/api/emergency/org/getOrgByParent"
)
APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
RECEIVE_OPEN_ID = os.environ.get("RECEIVE_OPEN_ID", "")
RECEIVE_MOBILE = os.environ.get("RECEIVE_MOBILE", "18811395648")
RECEIVE_EMAIL = os.environ.get("RECEIVE_EMAIL", "")
HEALTH_MARKER = "API NOT EXIST"
RETRY_INTERVAL_SECONDS = 10
MAX_ATTEMPTS = 3
HTTP_TIMEOUT = 20

OPEN_API_BASE = "https://open.feishu.cn/open-apis"


def now_str():
    """返回北京时间（Asia/Shanghai）字符串；GitHub runner 默认 UTC，必须显式转换"""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        # 兜底：环境缺少 tzdata 时按 UTC+8 固定偏移（中国无夏令时）
        import time as _time

        return _time.strftime(
            "%Y-%m-%d %H:%M:%S", _time.localtime(_time.time() + 8 * 3600)
        )


def api_json(url, payload=None, token=None, method=None):
    """调用飞书 OpenAPI，统一处理 HTTP 错误并输出飞书返回的错误详情"""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = (
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if payload is not None
        else None
    )
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method or ("POST" if data is not None else "GET"),
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"飞书 API 返回 HTTP {e.code}: {body[:1000]}") from e


def probe_once():
    """发起一次探测请求，返回 (status_ok: bool, elapsed_ms: int, summary: str)"""
    import uuid

    headers = {
        "Content-Type": "application/json",
        "app-id": "db_probe_" + str(uuid.uuid4()),
        "timestamp": str(int(time.time() * 1000)),
        "app-sign": "db-probe-invalid-sign",
    }
    req = urllib.request.Request(
        PROBE_URL, data=b"{}", headers=headers, method="POST"
    )
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status_code = resp.getcode()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        status_code = e.code
    except Exception as e:
        elapsed_ms = int((time.time() - start) * 1000)
        return (False, elapsed_ms, f"请求异常: {type(e).__name__}: {e}")

    elapsed_ms = int((time.time() - start) * 1000)
    # 关键：HTTP 500 也可能是正常（服务按设计返回 500 + API NOT EXIST），必须解析响应体
    is_ok = HEALTH_MARKER in body
    summary = f"HTTP {status_code} | 耗时 {elapsed_ms}ms | 响应: {body[:200]}"
    return (is_ok, elapsed_ms, summary)


def get_tenant_access_token():
    """换取 tenant_access_token（应用身份，用于发消息与加急）"""
    data = api_json(
        f"{OPEN_API_BASE}/auth/v3/tenant_access_token/internal",
        {"app_id": APP_ID, "app_secret": APP_SECRET},
    )
    if data.get("code") != 0:
        raise RuntimeError(f"获取 tenant_access_token 失败: {data}")
    return data["tenant_access_token"]


def send_message(token, text, receive_id=None):
    """以应用身份给值班人发文本消息，返回 message_id；receive_id 缺省用 RECEIVE_OPEN_ID"""
    receive_id = receive_id or RECEIVE_OPEN_ID
    data = api_json(
        f"{OPEN_API_BASE}/im/v1/messages?receive_id_type=open_id",
        {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
        token=token,
    )
    if data.get("code") != 0:
        raise RuntimeError(f"发送消息失败(code={data.get('code')}): {data.get('msg')}")
    return data["data"]["message_id"]


def urgent_phone(token, message_id):
    """对刚发送的消息执行电话加急"""
    data = api_json(
        f"{OPEN_API_BASE}/im/v1/messages/{message_id}/urgent_phone",
        {},
        token=token,
        method="PATCH",
    )
    if data.get("code") != 0:
        raise RuntimeError(f"电话加急失败(code={data.get('code')}): {data.get('msg')}")
    return data


def resolve_recipient(token):
    """用手机号/邮箱换取当前应用视角下的 open_id（open_id 按应用隔离，必须用本应用解析）"""
    payload = {}
    if RECEIVE_MOBILE:
        payload["mobiles"] = [RECEIVE_MOBILE]
    if RECEIVE_EMAIL:
        payload["emails"] = [RECEIVE_EMAIL]
    data = api_json(
        f"{OPEN_API_BASE}/contact/v3/users/batch_get_id?user_id_type=open_id",
        payload,
        token=token,
    )
    if data.get("code") != 0:
        raise RuntimeError(f"解析 open_id 失败(code={data.get('code')}): {data.get('msg')}")
    users = data.get("data", {}).get("user_list", [])
    if not users:
        raise RuntimeError("未匹配到用户：请确认手机号/邮箱属于当前应用所在租户成员")
    for u in users:
        oid = (u.get("user_id") or "").strip()
        print(
            f"解析到 open_id: [{oid}] | 手机号: {u.get('mobile', '')} | 邮箱: {u.get('email', '')}"
        )
        if oid:
            print("→ 正在用该 open_id 发送一条测试消息验证可用性...")
            try:
                mid = send_message(
                    token, "✅ 探活监控测试消息：新 open_id 验证成功", receive_id=oid
                )
                print(
                    f"→ ✅ 测试消息发送成功(message_id={mid})：此 open_id 可用，"
                    "请原样填入 GitHub Secrets 的 RECEIVE_OPEN_ID"
                )
            except Exception as e:
                print(f"→ ❌ 测试消息发送失败：{e}")
    print("提示：若测试发送失败，说明该 open_id 不在应用可见范围内（常见于应用挂在'测试企业'环境）")


def build_message(ts, elapsed_ms, fail_count, is_ok, summaries=None):
    lines = [
        "🔍 接口探活结果",
        f"⏰ 探测时间：{ts}",
        f"🌐 接口名：/api/emergency/org/getOrgByParent",
        f"⚡ 接口耗时：{elapsed_ms} ms",
        f"🔁 异常次数：{fail_count}",
        f"✅ 判定结果：服务正常" if is_ok else "❌ 判定结果：服务异常",
    ]
    if not is_ok and summaries:
        lines.append("📋 三次请求摘要：")
        for i, s in enumerate(summaries, 1):
            lines.append(f"  第{i}次: {s}")
        lines.append("⚠️ 可能原因：数据库或 Redis 异常，请尽快处理！")
    return "\n".join(lines)


def main():
    if "--resolve-recipient" in sys.argv:
        if not APP_ID or not APP_SECRET:
            print("错误: 缺少 FEISHU_APP_ID / FEISHU_APP_SECRET 环境变量", file=sys.stderr)
            sys.exit(2)
        token = get_tenant_access_token()
        resolve_recipient(token)
        sys.exit(0)

    probe_only = "--probe-only" in sys.argv

    summaries = []
    elapsed_ms = 0
    fail_count = 0
    is_ok = False

    for attempt in range(1, MAX_ATTEMPTS + 1):
        ok, ms, summary = probe_once()
        summaries.append(summary)
        elapsed_ms = ms
        print(f"[{now_str()}] 第{attempt}次探测: {'正常' if ok else '异常'} | {summary}")
        if ok:
            is_ok = True
            fail_count = attempt - 1  # 第1次成功=0，第2次成功=1，第3次成功=2
            break
        fail_count = attempt  # 第1次失败后为1...连续3次失败为3
        if attempt < MAX_ATTEMPTS:
            print(f"  等待 {RETRY_INTERVAL_SECONDS} 秒后重试...")
            time.sleep(RETRY_INTERVAL_SECONDS)

    ts = now_str()
    text = build_message(ts, elapsed_ms, fail_count, is_ok, summaries)
    print("=" * 40)
    print(text)

    if probe_only:
        print("[probe-only] 未发送通知")
        sys.exit(0 if is_ok else 1)

    if not APP_ID or not APP_SECRET or not RECEIVE_OPEN_ID:
        print(
            "错误: 缺少 FEISHU_APP_ID / FEISHU_APP_SECRET / RECEIVE_OPEN_ID 环境变量",
            file=sys.stderr,
        )
        sys.exit(2)

    # 诊断：打印当前 Secrets 中 open_id 的长度与首尾片段，便于与解析值比对
    rid = RECEIVE_OPEN_ID.strip()
    print(
        f"[诊断] 当前 RECEIVE_OPEN_ID 长度={len(rid)} "
        f"前缀={rid[:8]} 后缀={rid[-4:]}"
    )

    token = get_tenant_access_token()
    message_id = send_message(token, text)
    print(f"[{now_str()}] 飞书消息已发送: message_id={message_id}")

    if not is_ok:
        urgent_phone(token, message_id)
        print(f"[{now_str()}] 电话加急已触发")

    sys.exit(0 if is_ok else 3)


if __name__ == "__main__":
    main()
