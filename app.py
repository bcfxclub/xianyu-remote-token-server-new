"""
闲鱼 IM Token 远程中转服务

功能：
1. 接收来自主平台的 HTTP POST 请求
2. 验证请求头 X-API-Key
3. 从中国大陆网络出口调用闲鱼 IM Token API
4. 返回统一格式的响应
5. （自修复）_m_h5_tk 过期时自动续期并取回 token，避免返回 “令牌过期” 报错

部署：
- Docker: docker run -e API_KEY=xxx -p 8000:8000 xianyu-remote-token-server
- 云函数: 配置环境变量 API_KEY，部署 main.handler
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import secrets
import time
from typing import Any, Dict, Optional, Tuple

import aiohttp
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ============= 配置 =============
API_KEY = os.environ.get("API_KEY", "").strip()
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "30"))
LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8000"))

# Cookie 续期（setLoginSettings）相关，镜像主平台 cookie_renew_api_service
_PASSPORT_HAS_LOGIN_WEB = "https://passport.goofish.com/newlogin/hasLogin.do"
_PASSPORT_SILENT_HAS_LOGIN = "https://passport.goofish.com/newlogin/silentHasLogin.do"
_PASSPORT_SET_LOGIN_SETTINGS = "https://passport.goofish.com/ac/account/setLoginSettings.do"
_RENEW_REQUEST_TIMEOUT = int(os.environ.get("RENEW_TIMEOUT", "20"))

# 日志配置
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


def api_key_is_valid(candidate: Optional[str]) -> bool:
    """以恒定时间方式校验 API Key，避免泄露密钥内容。"""
    if not API_KEY or not candidate:
        return False
    return secrets.compare_digest(candidate, API_KEY)

# FastAPI 应用
app = FastAPI(
    title="Xianyu Remote Token Server",
    description="闲鱼 IM Token 中转服务",
    version="1.0.0",
)

# ============= 数据模型 =============


class TokenRequest(BaseModel):
    """远程接口请求体"""

    type: str  # "xianyu_token"
    data: Dict[str, Any]  # {"cookies": "..."}


class TokenResponse(BaseModel):
    """远程接口响应体"""

    success: bool
    message: str
    data: Optional[Dict[str, str]] = None


# ============= 工具函数 =============


def trans_cookies(cookies_str: str) -> Dict[str, str]:
    """将 Cookie 字符串转换为字典

    Args:
        cookies_str: Cookie 字符串，格式如 "key1=value1; key2=value2"

    Returns:
        Cookie 字典

    Raises:
        ValueError: 如果 cookies 为空
    """
    if not cookies_str:
        raise ValueError("cookies 不能为空")

    cookies = {}
    for cookie in cookies_str.split(";"):
        cookie = cookie.strip()
        if not cookie:
            continue
        if "=" in cookie:
            key, value = cookie.split("=", 1)
            key = key.strip()
            if key:
                cookies[key] = value.strip()
    return cookies


def extract_account_user_id_from_cookie(cookies_str: str) -> str:
    """从 Cookie 中提取当前闲鱼账号标识

    优先使用 unb（加密用户名），其次 munb

    Args:
        cookies_str: Cookie 字符串

    Returns:
        用户 ID（unb 或 munb 的值），如果两者都不存在则返回空字符串
    """
    if not cookies_str:
        return ""

    try:
        cookie_map = trans_cookies(cookies_str)
    except ValueError:
        return ""

    return str(cookie_map.get("unb") or cookie_map.get("munb") or "").strip()


def generate_device_id(user_id: str) -> str:
    """生成设备 ID

    格式：UUID 格式字符串 + "-" + 用户 ID

    Args:
        user_id: 用户 ID

    Returns:
        设备 ID 字符串
    """
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    result = []

    for i in range(36):
        if i in [8, 13, 18, 23]:
            result.append("-")
        elif i == 14:
            result.append("4")
        else:
            if i == 19:
                rand_val = int(16 * random.random())
                result.append(chars[(rand_val & 0x3) | 0x8])
            else:
                rand_val = int(16 * random.random())
                result.append(chars[rand_val])

    return "".join(result) + "-" + user_id


def generate_sign(t: str, token: str, data: str, app_key: str = "34839810") -> str:
    """生成 API 签名

    签名算法：MD5(token&t&app_key&data)

    Args:
        t: 时间戳（毫秒）
        token: _m_h5_tk token（从 Cookie 中提取 & 前的部分）
        data: 请求数据（JSON 字符串）
        app_key: 应用 Key，默认为闲鱼的 Key

    Returns:
        签名字符串（MD5 十六进制）
    """
    msg = f"{token}&{t}&{app_key}&{data}"
    md5_hash = hashlib.md5()
    md5_hash.update(msg.encode("utf-8"))
    return md5_hash.hexdigest()


async def request_im_token(
    cookies_str: str,
    device_id: str,
    timeout_seconds: int = 30,
) -> Tuple[Dict[str, Any], Optional[str]]:
    """调用闲鱼 IM Token API

    Args:
        cookies_str: 账号 Cookie 字符串
        device_id: 设备 ID
        timeout_seconds: 请求超时秒数

    Returns:
        (API 响应 JSON, 本次 mtop 响应重新下发的 _m_h5_tk 或 None)

    Raises:
        ValueError: Cookie 格式错误
        aiohttp.ClientError: 网络请求失败
        asyncio.TimeoutError: 请求超时
    """
    # 解析 Cookie
    cookies = trans_cookies(cookies_str)

    # 获取签名所需的 token
    m_h5_token = cookies.get("_m_h5_tk", "").strip()
    if not m_h5_token:
        raise ValueError("Cookie 缺少 _m_h5_tk")
    signing_token = m_h5_token.split("_", 1)[0].strip()
    if not signing_token:
        raise ValueError("Cookie 中的 _m_h5_tk 格式无效")

    # 构造请求
    timestamp = str(int(time.time() * 1000))
    data_value = '{"appKey":"444e9908a51d1cb236a27862abc769c9","deviceId":"' + device_id + '"}'

    # 生成签名
    sign = generate_sign(timestamp, signing_token, data_value)

    params = {
        "jsv": "2.7.2",
        "appKey": "34839810",
        "t": timestamp,
        "sign": sign,
        "v": "1.0",
        "type": "originaljson",
        "accountSite": "xianyu",
        "dataType": "json",
        "timeout": "20000",
        "api": "mtop.taobao.idlemessage.pc.login.token",
        "sessionOption": "AutoLoginOnly",
        "dangerouslySetWindvaneParams": "%5Bobject%20Object%5D",
        "smToken": "token",
        "queryToken": "sm",
        "sm": "sm",
        "spm_cnt": "a21ybx.im.0.0",
        "spm_pre": "a21ybx.home.sidebar.1.4c053da6vYwnmf",
        "log_id": "4c053da6vYwnmf",
    }

    headers = {
        "accept": "application/json",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "cache-control": "no-cache",
        "content-type": "application/x-www-form-urlencoded",
        "pragma": "no-cache",
        "priority": "u=1, i",
        "sec-ch-ua": '"Not;A=Brand";v="99", "Google Chrome";v="139", "Chromium";v="139"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/139.0.0.0 Safari/537.36"
        ),
        "referer": "https://www.goofish.com/",
        "origin": "https://www.goofish.com",
        "cookie": cookies_str.replace("\n", "").replace("\r", ""),
    }

    url = "https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/"

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            params=params,
            data={"data": data_value},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout_seconds),
        ) as response:
            # 风控响应的 content-type 可能不是 json，统一放开校验避免解析异常
            response_json = await response.json(content_type=None)
            # 关键：从 mtop 响应的 Set-Cookie 捕获重新下发的 _m_h5_tk
            fresh_m_h5_tk = None
            for sc in response.headers.getall("set-cookie", []):
                parsed = _parse_single_set_cookie(sc)
                if parsed and parsed[0] == "_m_h5_tk":
                    fresh_m_h5_tk = parsed[1]
                    break
            return response_json, fresh_m_h5_tk


def extract_im_access_token(response_json: Any) -> Optional[str]:
    """从闲鱼 API 响应中提取 accessToken

    Args:
        response_json: 闲鱼 IM Token API 返回的 JSON

    Returns:
        accessToken 字符串，失败返回 None
    """
    if not isinstance(response_json, dict):
        return None

    ret_value = response_json.get("ret", []) or []
    if isinstance(ret_value, str):
        ret_value = [ret_value]

    if not any("SUCCESS::调用成功" in str(item) for item in ret_value):
        return None

    data = response_json.get("data")
    if not isinstance(data, dict):
        return None

    access_token = data.get("accessToken")
    return access_token if isinstance(access_token, str) and access_token else None


# ============= 自修复（_m_h5_tk 续期）辅助函数 =============


def _parse_single_set_cookie(raw_cookie: str) -> Optional[Tuple[str, str]]:
    """解析单个 Set-Cookie 头，返回 (name, value)。"""
    if not raw_cookie or "=" not in raw_cookie:
        return None
    pair = raw_cookie.split(";", 1)[0].strip()
    if "=" not in pair:
        return None
    name, value = pair.split("=", 1)
    name = name.strip()
    value = value.strip()
    if not name:
        return None
    return name, value


def _replace_cookie_value(cookies_str: str, name: str, value: str) -> str:
    """把 cookies 字符串中 name 的值替换为 value，返回新的 cookie 字符串。"""
    cookies = trans_cookies(cookies_str)
    cookies[name] = value
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def _merge_set_cookies(original_cookies_str: str, set_cookie_headers) -> str:
    """把响应里的所有 Set-Cookie 合并进原始 cookie 字符串（无差别，含 _m_h5_tk）。"""
    try:
        merged = dict(trans_cookies(original_cookies_str) if original_cookies_str else {})
    except Exception:
        merged = {}
    for raw in set_cookie_headers:
        parsed = _parse_single_set_cookie(raw)
        if parsed:
            merged[parsed[0]] = parsed[1]
    return "; ".join(f"{k}={v}" for k, v in merged.items())


def _is_token_expired(response_json: dict) -> bool:
    """判断 mtop 响应是否为令牌过期/为空（_m_h5_tk 失效）。"""
    ret = response_json.get("ret", []) or []
    ret_str = str(ret)
    return (
        "FAIL_SYS_TOKEN_EXPIRED" in ret_str
        or "FAIL_SYS_TOKEN_EMPTY" in ret_str
        or "令牌过期" in ret_str
        or "令牌为空" in ret_str
    )


async def _renew_session_cookies(cookies_str: str) -> Optional[str]:
    """直连 passport.goofish.com 续期会话（hasLogin → silentHasLogin → setLoginSettings）。

    注意：passport 域下不会签发 _m_h5_tk，本函数的作用是刷新登录会话，
    让随后的 mtop 调用能重新下发 _m_h5_tk。所有域名在国内均可直连。
    返回合并后的新 cookie 字符串；失败返回 None。
    """
    cookie_dict = trans_cookies(cookies_str) if cookies_str else {}
    xsrf = cookie_dict.get("XSRF-TOKEN", "")
    unb = cookie_dict.get("unb", "")
    cookie2 = cookie_dict.get("cookie2", "")
    tb_token = cookie_dict.get("_tb_token_", "")
    umid = cookie_dict.get("_uab_collina", "") or cookie_dict.get("cna", "")

    headers_base = {
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
        ),
        "cookie": cookies_str.replace("\n", "").replace("\r", ""),
    }

    all_set_cookies = []
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
            # 1) hasLogin.do
            if unb:
                rnd = str(random.random())
                page_trace = (
                    f"21504{int(time.time() * 1000)}{random.randint(100000, 999999)}"
                )
                haslogin_headers = dict(headers_base)
                haslogin_headers.update({
                    "accept": "application/json, text/plain, */*",
                    "content-type": "application/x-www-form-urlencoded",
                    "referer": (
                        f"https://passport.goofish.com/mini_login.htm?lang=zh_cn"
                        f"&appName=xianyu&appEntrance=web&styleType=vertical"
                        f"&bizParams=&notLoadSsoView=false&notKeepLogin=false"
                        f"&isMobile=false&qrCodeFirst=false&stie=77&rnd={rnd}"
                    ),
                })
                if xsrf:
                    haslogin_headers["x-xsrf-token"] = xsrf
                post_data = (
                    f"hid={unb}&ltl=true&appName=xianyu&appEntrance=web"
                    f"&_csrf_token={tb_token}&umidToken={umid}&hsiz={cookie2}"
                    f"&bizParams=taobaoBizLoginFrom%3Dweb%26renderRefer%3Dhttps%253A%252F%252Fwww.goofish.com%252F"
                    f"&mainPage=false&isMobile=false&lang=zh_CN&returnUrl=&fromSite=77"
                    f"&isIframe=true&documentReferer=https%3A%2F%2Fwww.goofish.com%2F"
                    f"&defaultView=hasLogin&umidTag=SERVER&deviceId=&pageTraceId={page_trace}"
                )
                try:
                    async with session.post(
                        _PASSPORT_HAS_LOGIN_WEB,
                        params={"appName": "xianyu", "fromSite": "77"},
                        data=post_data,
                        headers=haslogin_headers,
                        timeout=aiohttp.ClientTimeout(total=_RENEW_REQUEST_TIMEOUT),
                        allow_redirects=False,
                    ) as resp:
                        all_set_cookies.extend(resp.headers.getall("Set-Cookie", []))
                except Exception as e:
                    logger.warning(f"hasLogin.do 续期异常（忽略）: {e}")

            # 2) silentHasLogin.do
            silent_headers = dict(headers_base)
            silent_headers.update({
                "accept": "*/*",
                "Referer": "https://www.goofish.com/",
                "sec-fetch-site": "same-site",
                "sec-fetch-mode": "cors",
                "sec-fetch-dest": "empty",
            })
            try:
                async with session.post(
                    _PASSPORT_SILENT_HAS_LOGIN,
                    params={
                        "documentReferer": "https://www.goofish.com/",
                        "appName": "xianyu",
                        "appEntrance": "xianyu_sdkSilent",
                        "fromSite": "0",
                        "ltl": "true",
                    },
                    headers=silent_headers,
                    timeout=aiohttp.ClientTimeout(total=_RENEW_REQUEST_TIMEOUT),
                    allow_redirects=False,
                ) as resp:
                    all_set_cookies.extend(resp.headers.getall("Set-Cookie", []))
            except Exception as e:
                logger.warning(f"silentHasLogin.do 续期异常（忽略）: {e}")

            # 3) setLoginSettings.do（长登录续期）
            set_headers = dict(headers_base)
            set_headers.update({
                "accept": "application/json, text/plain, */*",
                "content-type": "application/x-www-form-urlencoded",
                "referer": "https://www.goofish.com/",
                "sec-fetch-site": "same-site",
                "sec-fetch-mode": "cors",
                "sec-fetch-dest": "empty",
            })
            try:
                async with session.post(
                    _PASSPORT_SET_LOGIN_SETTINGS,
                    params={"fromSite": "77", "appName": "xianyu", "bizEntrance": "web"},
                    data="status=0",
                    headers=set_headers,
                    timeout=aiohttp.ClientTimeout(total=_RENEW_REQUEST_TIMEOUT),
                    allow_redirects=False,
                ) as resp:
                    for sc in resp.headers.getall("Set-Cookie", []):
                        if "Max-Age=0" not in sc and "1970" not in sc:
                            all_set_cookies.append(sc)
            except Exception as e:
                logger.warning(f"setLoginSettings.do 续期异常（忽略）: {e}")
    except Exception as e:
        logger.error(f"会话续期整体异常: {e}")
        return None

    if not all_set_cookies:
        return None
    return _merge_set_cookies(cookies_str, all_set_cookies)


# ============= API 端点 =============


@app.get("/health", tags=["健康检查"])
async def health_check():
    """健康检查端点"""
    return {"status": "ok", "timestamp": time.time()}


@app.post("/invoke", tags=["Token 获取"], response_model=TokenResponse)
async def invoke_token(
    request: Request,
    body: TokenRequest,
    x_api_key: Optional[str] = Header(None),
):
    """获取闲鱼 IM Token

    中国大陆出口请求接口，解决海外机房风控问题。

    请求头：
    - X-API-Key: 32 位 API 密钥

    请求体：
    {
        "type": "xianyu_token",
        "data": {
            "cookies": "完整闲鱼 Cookie"
        }
    }

    响应：
    {
        "success": true,
        "message": "取Token成功",
        "data": {
            "token": "闲鱼IM Token",
            "device_id": "设备ID",
            "api_mode": "web"
        }
    }

    Args:
        request: FastAPI 请求对象
        body: 请求体
        x_api_key: X-API-Key 请求头

    Returns:
        统一格式的响应

    Raises:
        HTTPException: 验证失败或服务异常
    """
    client_ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or request.client.host
        if request.client
        else "unknown"
    )

    # ===== 1. 验证 API Key =====
    if not API_KEY:
        logger.error("API_KEY 未配置，服务不可用")
        raise HTTPException(status_code=500, detail="服务未配置")

    if not api_key_is_valid(x_api_key):
        reason = "缺失" if not x_api_key else "无效"
        logger.warning(f"非法请求 - IP: {client_ip}，API Key {reason}")
        raise HTTPException(status_code=403, detail="API Key 无效")

    # ===== 2. 验证请求格式 =====
    if body.type != "xianyu_token":
        return TokenResponse(
            success=False,
            message=f"不支持的请求类型: {body.type}",
        )

    cookies = body.data.get("cookies", "").strip() if body.data else ""
    if not cookies:
        return TokenResponse(
            success=False,
            message="Cookie 不能为空",
        )

    try:
        cookie_map = trans_cookies(cookies)
    except ValueError:
        return TokenResponse(
            success=False,
            message="Cookie 格式无效",
        )

    if not cookie_map.get("_m_h5_tk"):
        return TokenResponse(
            success=False,
            message="Cookie 缺少 _m_h5_tk",
        )

    if not (cookie_map.get("unb") or cookie_map.get("munb")):
        return TokenResponse(
            success=False,
            message="Cookie 中未找到用户标识（unb/munb）",
        )

    # ===== 3. 提取用户 ID 和生成设备 ID =====
    try:
        user_id = extract_account_user_id_from_cookie(cookies)
        if not user_id:
            logger.warning(f"Cookie 中未找到 unb/munb - IP: {client_ip}")
            return TokenResponse(
                success=False,
                message="Cookie 中未找到用户标识",
            )

        device_id = generate_device_id(user_id)
        logger.info(f"已生成设备 ID: {device_id[:20]}... - IP: {client_ip}")
    except Exception as e:
        logger.error(f"提取用户 ID 失败: {e} - IP: {client_ip}")
        return TokenResponse(
            success=False,
            message="Cookie 解析失败",
        )

    # ===== 4. 请求闲鱼 IM Token API（带 _m_h5_tk 自修复）=====
    try:
        logger.info(f"开始请求闲鱼 IM Token API - IP: {client_ip}")
        response_json, fresh_m_h5_tk = await request_im_token(
            cookies,
            device_id,
            timeout_seconds=REQUEST_TIMEOUT,
        )
        logger.info(f"闲鱼 API 响应成功 - IP: {client_ip}")
    except asyncio.TimeoutError:
        logger.error(f"闲鱼 API 请求超时 (>{REQUEST_TIMEOUT}s) - IP: {client_ip}")
        return TokenResponse(
            success=False,
            message=f"请求超时 (>{REQUEST_TIMEOUT}s)，请稍后重试",
        )
    except aiohttp.ClientError as e:
        logger.error(f"闲鱼 API 网络错误: {e} - IP: {client_ip}")
        return TokenResponse(
            success=False,
            message=f"网络请求失败: {type(e).__name__}",
        )
    except Exception as e:
        logger.error(f"闲鱼 API 请求异常: {e} - IP: {client_ip}")
        return TokenResponse(
            success=False,
            message="上游请求异常，请查看服务端日志",
        )

    # ===== 5. 解析响应；令牌过期则自修复后重试 =====
    access_token = extract_im_access_token(response_json)

    if not access_token and _is_token_expired(response_json):
        logger.warning(f"检测到令牌过期(_m_h5_tk 失效)，开始自修复 - IP: {client_ip}")
        # 5.1 优先用本次 mtop 响应重新下发的 _m_h5_tk 直接重试
        if fresh_m_h5_tk:
            retry_cookies = _replace_cookie_value(cookies, "_m_h5_tk", fresh_m_h5_tk)
            try:
                rj2, _ = await request_im_token(
                    retry_cookies, device_id, timeout_seconds=REQUEST_TIMEOUT
                )
                access_token = extract_im_access_token(rj2)
                if access_token:
                    logger.info("使用重新下发的 _m_h5_tk 重试成功")
            except Exception as e:
                logger.warning(f"_m_h5_tk 重试异常: {e}")
        # 5.2 仍失败则用 setLoginSettings 续会话后再取（mtop 会重新下发 _m_h5_tk）
        if not access_token:
            renewed = await _renew_session_cookies(cookies)
            if renewed:
                try:
                    rj3, fresh3 = await request_im_token(
                        renewed, device_id, timeout_seconds=REQUEST_TIMEOUT
                    )
                    access_token = extract_im_access_token(rj3)
                    if not access_token and fresh3:
                        rj4, _ = await request_im_token(
                            _replace_cookie_value(renewed, "_m_h5_tk", fresh3),
                            device_id,
                            timeout_seconds=REQUEST_TIMEOUT,
                        )
                        access_token = extract_im_access_token(rj4)
                    if access_token:
                        logger.info("会话续期后取 Token 成功")
                except Exception as e:
                    logger.warning(f"会话续期后取 Token 异常: {e}")

    if not access_token:
        # 记录完整响应用于调试（但不记录 Cookie）
        ret_msg = response_json.get("ret", ["未知错误"])
        if isinstance(ret_msg, list) and ret_msg:
            ret_msg = ret_msg[0]
        logger.warning(
            f"闲鱼 API 返回失败: {ret_msg} - IP: {client_ip}"
        )
        return TokenResponse(
            success=False,
            message=str(ret_msg) if ret_msg else "未返回有效 Token",
        )

    logger.info(f"Token 获取成功 - IP: {client_ip}")
    return TokenResponse(
        success=True,
        message="取Token成功",
        data={
            "token": access_token,
            "device_id": device_id,
            "api_mode": "web",
        },
    )


@app.post("/test", tags=["测试"], response_model=TokenResponse)
async def test_connection(
    x_api_key: Optional[str] = Header(None),
):
    """测试远程接口连通性

    用于验证 API Key 和基本连接，不需要传递 Cookie。

    响应 Cookie 为空时表示测试成功。

    Args:
        x_api_key: X-API-Key 请求头

    Returns:
        测试结果
    """
    # ===== 验证 API Key =====
    if not API_KEY:
        return TokenResponse(
            success=False,
            message="服务未配置 API_KEY",
        )

    if not api_key_is_valid(x_api_key):
        return TokenResponse(
            success=False,
            message="API Key 无效",
        )

    # ===== 连通性测试成功 =====
    logger.info("连通性测试成功")
    return TokenResponse(
        success=True,
        message="连通性测试成功，服务正常",
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """全局异常处理"""
    logger.error(f"未捕获的异常: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "message": "服务内部错误，请稍后重试",
        },
    )


# ============= 主入口 =============

if __name__ == "__main__":
    import uvicorn

    logger.info(f"启动闲鱼 Remote Token 服务")
    logger.info(f"监听地址: {LISTEN_HOST}:{LISTEN_PORT}")
    logger.info(f"日志级别: {LOG_LEVEL}")
    logger.info(f"请求超时: {REQUEST_TIMEOUT}s")

    if not API_KEY:
        logger.warning("⚠️  警告：API_KEY 未设置，服务将拒绝所有请求")

    uvicorn.run(
        app,
        host=LISTEN_HOST,
        port=LISTEN_PORT,
        log_level=LOG_LEVEL.lower(),
    )
