# -*- coding: utf-8 -*-
"""诊断 pip/urllib 的 SSL 报错：check_hostname requires server_hostname。"""
import os
import ssl
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

print("--- 相关环境变量 ---")
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
          "http_proxy", "https_proxy", "all_proxy", "no_proxy",
          "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
    v = os.environ.get(k)
    if v:
        print(f"  {k} = {v}")
print("  (以上为空则未设置)")

print("\n--- 直接 HTTPS 请求 ---")
try:
    with urllib.request.urlopen("https://mirrors.aliyun.com/pypi/simple/", timeout=15) as r:
        print("  OK", r.status, r.headers.get("content-type"))
except Exception as e:
    print("  FAIL", type(e).__name__, e)

print("\n--- 手动 SSLContext ---")
try:
    ctx = ssl.create_default_context()
    print("  verify_mode:", ctx.verify_mode, "check_hostname:", ctx.check_hostname)
    with urllib.request.urlopen("https://pypi.org/simple/", timeout=15, context=ctx) as r:
        print("  OK", r.status)
except Exception as e:
    print("  FAIL", type(e).__name__, e)

print("\n--- pip 内部会用到的 ssl 版本 ---")
print("  ssl:", ssl.OPENSSL_VERSION)
print("  python:", sys.version.split()[0])
