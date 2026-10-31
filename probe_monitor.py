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
用法：
  python3 probe_monitor.py            # 正常执行（探测+通知+异常电话加急）
  python3 probe_monitor.py --probe-only  # 只探测并打印结果，不通知（用于本地验证）
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime

PROBE_URL = os.environ.get(
    "PROBE_URL",
    "https://zyjj4x.cmiiyl.com/api/ambulance/api/emergency/org/getOrgByParent",
)
APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
RECEIVE_OPEN_ID = os.environ.get("RECEIVE_OPEN_ID", "")
HEALTH_MARKER = "API NOT EXIST"
RETRY_INTERVAL_SECONDS = 10
MAX_ATTEMPTS = 3
HTTP_TIMEOUT = 20

OPEN_API_BASE = "https://open.feishu.cn/open-apis"


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
    payload = json.dumps(
        {"app_id": APP_ID, "app_secret": APP_SECRET}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{OPEN_API_BASE}/auth/v3/tenant_access_token/internal",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != 0:
        raise RuntimeError(f"获取 tenant_access_token 失败: {data}")
    return data["tenant_access_token"]


def send_message(token, text):
    """以应用身份给值班人发文本消息，返回 message_id"""
    payload = json.dumps(
        {
            "receive_id": RECEIVE_OPEN_ID,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{OPEN_API_BASE}/im/v1/messages?receive_id_type=open_id",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != 0:
        raise RuntimeError(f"发送消息失败: {data}")
    return data["data"]["message_id"]


def urgent_phone(token, message_id):
    """对刚发送的消息执行电话加急"""
    payload = json.dumps({}).encode("utf-8")
    req = urllib.request.Request(
        f"{OPEN_API_BASE}/im/v1/messages/{message_id}/urgent_phone",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="PATCH",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != 0:
        raise RuntimeError(f"电话加急失败: {data}")
    return data


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
        print("错误: 缺少 FEISHU_APP_ID / FEISHU_APP_SECRET / RECEIVE_OPEN_ID 环境变量", file=sys.stderr)
        sys.exit(2)

    token = get_tenant_access_token()
    message_id = send_message(token, text)
    print(f"[{now_str()}] 飞书消息已发送: message_id={message_id}")

    if not is_ok:
        urgent_phone(token, message_id)
        print(f"[{now_str()}] 电话加急已触发")

    sys.exit(0 if is_ok else 3)


if __name__ == "__main__":
    main()
#（注：内容由AI生成）
