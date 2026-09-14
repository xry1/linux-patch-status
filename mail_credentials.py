"""Hidden interactive configuration and Windows per-user DPAPI storage."""
import base64
import copy
import ctypes
import getpass
import json
import os
import re
import sys
from ctypes import wintypes

SECRET_NAMES = ('PATCH_IMAP_PASSWORD', 'PATCH_GLM_API_KEY', 'PATCH_FEISHU_WEBHOOK', 'PATCH_FEISHU_SECRET')


def interactive_console():
    if not sys.stdin.isatty():
        return False
    if os.name != 'nt':
        return True
    # NUL is a character device too, so isatty() alone is insufficient on Windows.
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetConsoleMode.restype = wintypes.BOOL
    mode = wintypes.DWORD()
    return bool(kernel.GetConsoleMode(kernel.GetStdHandle(-10), ctypes.byref(mode)))


def dpapi(data, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('本地加密配置需要 Windows；其他系统请使用进程环境变量提供凭据。')

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise RuntimeError('Windows 凭据加密 / 解密失败；请用配置时的 Windows 用户运行。')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def encode_credentials(values):
    return {'schema_version': 1, 'dpapi': base64.b64encode(dpapi(json.dumps(values).encode())).decode()}


def read_credentials(directory):
    file = directory / 'credentials.json'
    if not file.is_file():
        return {}
    try:
        saved = json.loads(file.read_text(encoding='utf-8'))
        if saved['schema_version'] != 1:
            raise ValueError('schema')
        result = json.loads(dpapi(base64.b64decode(saved['dpapi'], validate=True), decrypt=True))
        if not isinstance(result, dict) or any(not isinstance(v, str) for v in result.values()):
            raise ValueError('secrets')
        return {name: result.get(name, '') for name in SECRET_NAMES}
    except Exception:
        raise RuntimeError('无法解密本地凭据。请使用原来的 Windows 用户，或重新配置授权码和 API Key。') from None


def interactive_config(directory, config, save, identity):
    if not interactive_console():
        raise RuntimeError('配置需要交互终端。请双击“配置邮箱提醒.cmd”，不要通过聊天发送密钥。')
    existing = read_credentials(directory)
    candidate = copy.deepcopy(config)
    print('163 学校 / 企业邮箱 → GLM → 飞书\n授权码和 API Key 隐藏输入，由 Windows 加密保存在本机。')
    print('相关 patch 新邮件会提交给 GLM，摘要发到你配置的飞书群；私人邮箱报告不发布到 GitHub。')

    def setting(prompt, default):
        return input(f'{prompt} [{default}]，回车保留：').strip() or default

    candidate['imap']['username'] = setting('完整邮箱地址', candidate['imap']['username'])
    candidate['imap']['host'] = setting('IMAP SSL 服务器（以学校客户端设置为准）', candidate['imap']['host'])
    candidate['imap']['folder'] = setting('监测文件夹', candidate['imap']['folder'])
    candidate['glm_model'] = setting('GLM 模型', candidate['glm_model'])
    candidate['feishu_keyword'] = setting('飞书机器人关键词', candidate['feishu_keyword'])
    if not re.fullmatch(r'[^\s@]+@[^\s@]+', candidate['imap']['username']) or not re.fullmatch(r'[A-Za-z0-9.-]+', candidate['imap']['host']):
        raise RuntimeError('邮箱地址或服务器格式无效，原配置已保留。')
    if (directory / 'state.json').is_file() and identity(candidate) != identity(config):
        raise RuntimeError('更换邮箱或文件夹前，请先备份并更名 local/mail-monitor 目录，再运行配置；原记录已保留。')
    values = {}
    prompts = ('邮箱客户端授权码（不是网页登录密码）', '智谱开放平台 GLM API Key',
               '飞书群自定义机器人 Webhook', '飞书签名密钥（未启用签名可留空）')
    for name, prompt in zip(SECRET_NAMES, prompts):
        values[name] = getpass.getpass(prompt + '（隐藏输入，回车保留已有值）：').strip() or existing.get(name, '')
        if name != 'PATCH_FEISHU_SECRET' and not values[name]:
            raise RuntimeError(prompt + ' 不能为空；原配置已保留。')
    if not re.fullmatch(r'https://open\.feishu\.cn/open-apis/bot/v2/hook/[A-Za-z0-9-]+', values['PATCH_FEISHU_WEBHOOK']):
        raise RuntimeError('飞书 Webhook 格式无效，原配置已保留。')
    save(directory / 'credentials.json', encode_credentials(values))
    save(directory / 'config.json', candidate)
    print('配置已加密保存。尚未连接邮箱、调用 GLM 或发送飞书消息。\n接下来运行“启动邮箱监测.cmd”，首次成功检查会建立起点。')
    return candidate
