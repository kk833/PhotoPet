"""网页抓取 (v0.21) —— 就业网抓取和"读网页"工具共用这一份。

**为什么要有 SSRF 防护**: 这个模块会被大模型间接调用（"帮我看看这个链接"），
而模型给的地址是不可信的 —— 它可能（被提示词带偏、或用户诱导）去读
`http://192.168.1.1/`、`http://localhost:8000/` 这类**内网地址**。
那等于把家里的路由器、公司内网服务暴露给一个外部模型去试探。
所以每次抓取前先解析域名、确认解析出来的 IP 是**公网地址**才放行。

解析域名时用 getaddrinfo 拿到**所有** IP 逐个检查 —— 只查第一个的话，
一个域名同时解析到公网和内网 IP 就能绕过去（DNS rebinding 的简化版）。
"""
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PhotoPet/1.0"
DEFAULT_TIMEOUT = 20
MAX_TEXT = 6000            # 交给模型的正文上限, 别把 token 烧穿


def is_safe_url(url: str) -> tuple[bool, str]:
    """这个地址能不能读。返回 (是否可以, 不可以的原因)。

    只放行 http/https 的公网地址 —— 内网、本机、链路本地地址一律拒绝。
    """
    try:
        parts = urllib.parse.urlparse((url or "").strip())
    except ValueError:
        return False, "地址格式不对"
    if parts.scheme not in ("http", "https"):
        return False, "只支持 http/https 开头的地址"
    host = parts.hostname or ""
    if not host:
        return False, "地址里没有域名"
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        return False, "不能读本机/局域网地址"
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError:
        return False, "这个域名解析不了（地址写错了？）"
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False, "不能读内网地址"
    return True, ""


def fetch_page(url: str, retries: int = 2,
               timeout: int = DEFAULT_TIMEOUT) -> str:
    """抓一个页面, 返回 HTML（失败返回空串）。源不稳定, 所以要重试。"""
    for _ in range(max(1, retries)):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return ""


def decode_hidden_payload(html: str) -> str:
    """有些国内 CMS 把正文 base64 + zlib 压缩后藏在 script 里（"jz" 混淆）。

    形如 `Base64.decode(unzip("eJy...").substr(77)).substr(40)`。

    **那两个 77/40 不能当成固定值**：它对应的是"前缀标记的长度"，而服务端不同
    变体给的前缀长度不一样（实测踩过：照抄固定偏移解出来是乱码）。
    稳妥的做法是**用正则找真正的 base64 段**、解码后再**从第一个 `<` 开始切**。
    解不出来就返回空串（对普通网页是常态，不算错）。
    """
    import base64
    import zlib

    chunks = []
    for blob in re.findall(r'unzip\("([A-Za-z0-9+/=]+)"\)', html):
        try:
            step = zlib.decompress(base64.b64decode(blob)).decode("utf-8", "replace")
            found = re.search(r"[A-Za-z0-9+/=]{40,}", step)
            if not found:
                continue
            raw = base64.b64decode(found.group(0)).decode("utf-8", "replace")
            start = raw.find("<")           # 跳过前面那段同样的前缀标记
            chunks.append(raw[start:] if start >= 0 else raw)
        except (zlib.error, ValueError, UnicodeDecodeError):
            continue
    return "\n".join(chunks)


def html_to_text(html: str, limit: int = MAX_TEXT) -> str:
    """HTML -> 可读正文。

    去掉 script/style（里面有压缩数据和一个又一个的埋点代码，对模型是纯噪声），
    再去标签、压缩空行 —— 让有限的 token 尽量花在正文上。

    **但如果这页把正文压缩藏在 script 里**（部分国内 CMS 会这么干，比如高校
    就业网的宣讲会列表），就要先把它解出来 —— 否则读到的只有页面框架，
    用户问"这个链接讲了什么"会得到一个空壳。
    """
    hidden = decode_hidden_payload(html)
    if hidden.strip():
        html = hidden
    body = re.sub(r"<script.*?</script>|<style.*?</style>|<!--.*?-->", " ",
                  html, flags=re.S | re.I)
    body = re.sub(r"<(br|/p|/div|/tr|/li|/h\d)[^>]*>", "\n", body, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", body)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&#160;", " "))
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    out = "\n".join(ln for ln in lines if ln)
    return out[:limit] + ("\n…（内容太长，只读了前面一部分）" if len(out) > limit else "")


def read_url(url: str) -> tuple[str, str]:
    """安全地抓一个网页并转成正文。返回 (正文, 错误说明)。"""
    ok, why = is_safe_url(url)
    if not ok:
        return "", why
    html = fetch_page(url)
    if not html:
        return "", "打不开这个地址（网络问题，或者地址写错了）"
    text = html_to_text(html)
    if not text.strip():
        return "", "这个页面里没读到文字内容（可能是纯图片或需要登录）"
    return text, ""
